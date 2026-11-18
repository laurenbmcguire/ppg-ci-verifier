"""Hosted entry point for the last-scan draft runner. PREPARED, NOT CONNECTED.

Nothing imports this module: there is no route, no cron entry and no scheduled task that reaches it, and adding one
is a separate, reviewed activation step (docs/LAST_SCAN_ACTIVATION_CHECKLIST.md). It contains no network client, no
secret, no identity provider and no sender. Everything it needs is handed in through ``Deps``, and every one of
those defaults to "not configured", which means DENY.

Five things are deliberately NOT implemented here, only given an interface, because they must come from systems
this code does not own:
  * ``Authorizer``        who is calling (a verified principal). No shared static token is accepted.
  * ``ApprovalAuthority`` whether exactly this configuration + renderers + job registry was approved, and for
                          which principals. An approval written into the configuration itself counts for nothing.
  * ``Quota``             how many runs a principal may start for an approved snapshot per hour.
  * ``CaptureAuthority``  whether one exact schema snapshot was captured by a known collector, for which site and
                          list, and when. A hash, signature or "trusted" field written inside the snapshot or the
                          configuration counts for nothing: only this authority's own record does.
  * ``SchemaProbe``       a read-only look at a list's current column definitions, used to confirm the approved
                          schema is still the live one immediately before each source read.
None of these exists for production. The only implementations are TEST-ONLY fakes in the test suite. A receipt from
a CaptureAuthority is reported as "reported by capture authority"; this module cannot verify the authority itself
and never calls a snapshot trusted on its own account.

Source-mapping readiness is a MANDATORY preflight check with no switch to skip it. For every configured assignment
exactly one captured schema snapshot must be SUPPLIED through ``Deps.schemas`` (this module looks nothing up), and
``hosting.last_scan_readiness.validate`` must find the mapping ready against it. The identity of each snapshot and
the mapping contract checked against it are part of the approved snapshot digest, so an approval covers one exact
schema and one exact contract and stops counting when either changes. When it is blocked no renderer is built, no
scope is planned and there is nothing to approve. Attestations and ``verified`` flags inside the configuration are
inputs to that check; they are never the approval (that is the ApprovalAuthority) and never runtime proof.

``handle`` is one bounded call:
  * no authorizer / principal not verified          -> 503 / 401, nothing read, nothing written
  * mode "dry_run" (the default)                    -> the plan and the preflight; reads no source, writes nothing
  * mode "run"                                      -> only if enabled, attested for this exact snapshot and this
                                                       principal, every preflight check passes, the request's
                                                       single-use nonce is reserved and the quota allows it
A run builds DRAFTS only. Delivery transport stays disabled (hosting.last_scan_transport has no verified provider).
"""
import math
from copy import deepcopy
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any, Callable, Optional

from hosting import source_first as sf
from hosting import last_scan_readiness as readiness
from hosting import last_scan_runner as runner
from hosting import last_scan_transport as transport
from hosting.last_scan_drafts import instant
from hosting.last_scan_local import ArtifactStore, StoredDraftRouter
from hosting.last_scan_service import binding

SETTING_KEY = 'last_scan_runner'        # where a host would keep the configuration; absent means disabled
MAX_BUDGET_SECONDS, MAX_SCOPES = 45, 50
# Bounds on schema evidence. Placeholder values pending an owner's decision; they are limits, not approvals.
MAX_SCHEMA_CAPTURE_AGE_SECONDS = 24 * 3600   # an approved snapshot older than this no longer counts
SCHEMA_FUTURE_SKEW_SECONDS = 300             # a capture or probe time further in the future than this is refused
MAX_SCHEMA_PROBE_AGE_SECONDS = 60            # a live probe result must have been observed within this window
REQUEST_WINDOW_SECONDS = 300            # a request older or newer than this is refused
MODES = ('dry_run', 'run')
FIELDS = {'run_id', 'mode', 'budget_seconds', 'max_scopes', 'timestamp', 'nonce'}


@dataclass(frozen=True)
class Principal:
    id: str                              # stable identity of the verified caller


class Authorizer:
    """Verifies the caller. A real one belongs to the host's identity system; none is provided here.
    ``verify`` gets the method, the headers and the hash of the exact request body, so a signature can cover the
    body (including its timestamp and nonce). It returns a Principal, or None for anyone it cannot verify."""
    def verify(self, method, headers, body_digest):
        raise NotImplementedError


class ApprovalAuthority:
    """Answers, from a place the runner cannot write to, whether one exact snapshot was approved.
    ``attestation(snapshot_digest)`` returns None, or
    {snapshot_digest, approved_by, approved_at, principals: [ids allowed to run it]}."""
    def attestation(self, snapshot_digest):
        raise NotImplementedError


class CaptureAuthority:
    """Answers, from a place neither the runner nor the configuration can write to, where a schema snapshot came from.
    ``receipt(snapshot_identity)`` returns None, or
    {snapshot_identity, site_id, list_id, schema_digest, collector, captured_at}."""
    def receipt(self, snapshot_identity):
        raise NotImplementedError


class SchemaProbe:
    """Read-only. ``current(site_id, list_id)`` returns the list's column definitions as they are now, in the same
    shape as a captured snapshot, with ``captured_at`` set to when it was observed. It must read metadata only."""
    def current(self, site_id, list_id):
        raise NotImplementedError


class SchemaDrift(Exception):
    """Raised in place of a source read when the live schema could not be confirmed to be the approved one."""


@dataclass(frozen=True)
class Quota:
    runs_per_hour: int                   # per principal and approved snapshot


@dataclass
class Deps:
    """Everything the entry point touches, supplied explicitly. Every default means "not configured" = deny."""
    authorizer: Any = None               # Authorizer
    approvals: Any = None                # ApprovalAuthority
    quota: Any = None                    # Quota
    config: Any = None                   # the stored configuration (see SETTING_KEY), or None
    entries: Any = None                  # verified job registry entries (runner.registry_entries), or None
    schemas: Any = None                  # captured schema snapshots (a list), SUPPLIED by the host; never fetched here
    captures: Any = None                 # CaptureAuthority: where each supplied snapshot came from
    collectors: Any = None               # collector identities the host accepts (server-side; never from config or request)
    schema_probe: Any = None             # SchemaProbe: read-only live column definitions, checked before each source read
    store: Any = None                    # hosting.last_scan_store store, tables already installed
    artifacts: Any = None                # ArtifactStore for drafts; renderers are BUILT HERE from the configuration
    fetch: Optional[Callable] = None     # source adapter: fetch(scope, settings) -> snapshot
    clock: Callable = sf.now
    monotonic: Callable = time.monotonic
    provider_contract: Any = None        # stays None: no provider has been verified


def _router(deps):
    """Renderers come only from the configuration being checked; a caller cannot hand in its own."""
    config = deps.config if isinstance(deps.config, dict) else {}
    if not isinstance(deps.artifacts, ArtifactStore) or not isinstance(config.get('assignments'), list):
        return None
    return StoredDraftRouter(deps.artifacts, config['assignments'])


def _collectors(deps):
    c = deps.collectors
    if isinstance(c, (frozenset, set, tuple, list)) and c and all(isinstance(x, str) and x.strip() and x == x.strip() for x in c):
        return frozenset(c)
    return None


def capture_problems(deps, schema, row):
    """(reasons, capture). Whether the capture authority vouches for exactly this snapshot, from an accepted collector,
    for this site and list, at a valid time within bounds. Nothing inside the snapshot itself is consulted as proof."""
    if not isinstance(deps.captures, CaptureAuthority):
        return ['no_capture_authority_configured'], None
    accepted = _collectors(deps)
    if accepted is None:
        return ['no_accepted_collectors_configured'], None
    identity = readiness.snapshot_identity(schema)
    try:
        record = deepcopy(deps.captures.receipt(identity))
    except Exception:
        return ['capture_authority_unreadable'], None
    if not isinstance(record, dict) or record.get('snapshot_identity') != identity:
        return ['no_capture_receipt_for_this_exact_snapshot'], None
    reasons = []
    if record.get('site_id') != row['site_id'] or record.get('list_id') != row['list_id']:
        reasons.append('capture_receipt_is_for_another_site_or_list')
    if record.get('schema_digest') != readiness.schema_digest(schema):
        reasons.append('capture_receipt_schema_digest_mismatch')
    collector = record.get('collector')
    if not isinstance(collector, str) or collector not in accepted:
        reasons.append('capture_collector_not_accepted')
    try:
        at = instant(record.get('captured_at'))
    except (ValueError, OverflowError):
        return reasons + ['capture_time_missing_or_malformed'], None
    try:
        own = instant(schema.get('captured_at'))
    except (ValueError, OverflowError):
        own = None
    if own != at:
        reasons.append('snapshot_capture_time_contradicts_receipt')
    age = (instant(deps.clock()) - at).total_seconds()
    if age > MAX_SCHEMA_CAPTURE_AGE_SECONDS:
        reasons.append('capture_too_old')
    if age < -SCHEMA_FUTURE_SKEW_SECONDS:
        reasons.append('capture_time_in_the_future')
    capture = dict(collector=collector, captured_at=at.isoformat(), origin='reported_by_capture_authority')
    return reasons, (None if reasons else capture)


class _DriftGuard:
    """Confirms, read-only, that each list's live column definitions are still the approved ones. Used once for every
    list before a run starts and again immediately before every source read. Anything short of a fresh, well-formed,
    matching answer is drift. There remains an unavoidable gap between this look and the read that follows it."""
    def __init__(self, deps, mapping):
        self.deps, self.count = deps, 0
        self.approved = {(m['site_id'], m['list_id']): m['schema_digest'] for m in mapping}

    def problem(self, site_id, list_id):
        self.count += 1
        approved = self.approved.get((site_id, list_id))
        if not isinstance(approved, str) or not isinstance(self.deps.schema_probe, SchemaProbe):
            return 'no_approved_schema_or_probe_for_this_list'
        try:
            live = deepcopy(self.deps.schema_probe.current(site_id, list_id))
        except Exception as error:
            return 'schema_probe_failed:' + type(error).__name__
        if not isinstance(live, dict) or live.get('site_id') != site_id or live.get('list_id') != list_id:
            return 'schema_probe_answer_is_malformed_or_for_another_list'
        try:
            age = (instant(self.deps.clock()) - instant(live.get('captured_at'))).total_seconds()
        except (ValueError, OverflowError):
            return 'schema_probe_answer_is_undated'
        if age > MAX_SCHEMA_PROBE_AGE_SECONDS or age < -SCHEMA_FUTURE_SKEW_SECONDS:
            return 'schema_probe_answer_is_not_fresh'
        if readiness.schema_digest(live) != approved:
            return 'live_schema_differs_from_the_approved_snapshot'
        return None

    def check_all(self):
        out = []
        for site_id, list_id in sorted(self.approved, key=str):
            problem = self.problem(site_id, list_id)
            if problem:
                out.append(dict(site_id=site_id, list_id=list_id, problem=problem))
        return out

    def fetch(self, scope, settings):
        problem = self.problem(scope.get('site_id'), scope.get('list_id'))
        if problem:
            raise SchemaDrift(problem)                                       # the source is not read
        return self.deps.fetch(scope, settings)


NO_PROOF = dict(schema_capture_origin='reported_by_capture_authority_not_verified_by_this_module', runtime_source_proof='not_established', scan_time_semantics='not_established_by_this_tool', sample_evidence='not_performed',
                statement='Mapping readiness compares the configuration with a captured schema snapshot. It reads no source, examines no '
                          'sample rows here, and establishes nothing about what the live source returns or what the scan field means. '
                          'Attestations in the configuration are inputs, not the approval.')


def mapping_readiness(config, entries, schemas, capture_check=None):
    """Per configured assignment: exactly one supplied snapshot for its site and list, validated offline, and (when
    ``capture_check`` is given, as it always is from preflight) vouched for by the capture authority."""
    assignments = config.get('assignments') if isinstance(config, dict) else None
    supplied = schemas if isinstance(schemas, list) else None
    out = []
    for index, a in enumerate(assignments if isinstance(assignments, list) else []):
        d = a if isinstance(a, dict) else {}
        row = dict(assignment=index, client_id=d.get('client_id'), job=d.get('job'), site_id=d.get('site_id'), list_id=d.get('list_id'),
                   ready=False, blocked_reasons=[], schema_snapshot=None, schema_digest=None, mapping_contract=None, capture=None,
                   not_performed=[], proof=dict(NO_PROOF))
        matches = [s for s in supplied or [] if isinstance(s, dict) and isinstance(d.get('site_id'), str) and isinstance(d.get('list_id'), str)
                   and s.get('site_id') == d['site_id'] and s.get('list_id') == d['list_id']]
        if supplied is None:
            row['blocked_reasons'] = ['no_schema_snapshots_supplied']
        elif not matches:
            row['blocked_reasons'] = ['no_schema_snapshot_for_this_site_and_list']
        elif len(matches) > 1:
            row['blocked_reasons'] = ['more_than_one_schema_snapshot_for_this_site_and_list']
        else:
            report = readiness.validate(a, matches[0], entries)              # offline; no sample rows are examined here
            row.update(ready=report['ready_for_dry_run'] is True, blocked_reasons=list(report['blocked_reasons']),
                       schema_snapshot=readiness.snapshot_identity(matches[0]), schema_digest=report['schema_digest'],
                       mapping_contract=report['attestation_bindings'], not_performed=[n['check'] for n in report['not_performed']],
                       proof=dict(NO_PROOF, schema_dry_run_readiness=report['proof']['schema_dry_run_readiness'], sample_evidence=report['proof']['sample_evidence']))
            if capture_check is not None:
                reasons, capture = capture_check(matches[0], row)
                row.update(capture=capture, blocked_reasons=row['blocked_reasons'] + reasons, ready=row['ready'] and not reasons)
        out.append(row)
    return out


def snapshot_digest(config, entries, router, schemas=None, mapping=None):
    """One hash over everything an approval must cover: the configuration, the renderer version of every routed
    assignment (mapping, header, pinned template, generator code, binding), the job registry as supplied, the
    identity of every supplied schema snapshot, and per assignment the snapshot and mapping contract it was checked
    against together with the outcome."""
    renders = {}
    for a in (config or {}).get('assignments') or []:
        try:
            renders[sf.digest(binding(a))] = router.for_scope(a).render_version()
        except Exception:
            continue
    registry = sorted(sf.digest(e) for e in entries or [])
    return sf.digest(dict(config={k: v for k, v in (config or {}).items() if k != 'activation'},
                          render_versions=renders, registry=registry,
                          schema_snapshots=sorted(readiness.snapshot_identity(s) for s in schemas) if isinstance(schemas, list) else None,
                          mapping_readiness=[{k: m[k] for k in ('assignment', 'client_id', 'job', 'site_id', 'list_id', 'ready', 'blocked_reasons',
                                                                'schema_snapshot', 'schema_digest', 'mapping_contract', 'capture')} for m in mapping or []]))


def _attested(deps, digest, principal):
    if not isinstance(deps.approvals, ApprovalAuthority):
        return False, 'No approval authority is configured.'
    try:
        record = deps.approvals.attestation(digest)
    except Exception as error:
        return False, 'The approval authority could not be read: ' + type(error).__name__
    if not isinstance(record, dict) or record.get('snapshot_digest') != digest:
        return False, 'No approval exists for exactly this configuration, renderers and job registry.'
    if not all(isinstance(record.get(k), str) and record[k].strip() for k in ('approved_by', 'approved_at')):
        return False, 'The approval does not say who approved it and when.'
    allowed = record.get('principals')
    if not isinstance(allowed, (list, tuple)) or principal.id not in allowed:
        return False, 'This caller is not named in the approval.'
    return True, ''


def preflight(deps, principal):
    """Read-only checks. Every one must be ok before a run. Reads no source and writes nothing."""
    config, checks = deps.config, []
    def check(name, ok, detail=''):
        checks.append(dict(check=name, ok=bool(ok), detail='' if ok else detail))
    check('configuration_present', isinstance(config, dict), 'No configuration is stored.')
    config = config if isinstance(config, dict) else {}
    check('enabled_is_exactly_true', config.get('enabled') is True, 'The runner is off unless enabled is exactly true.')
    check('job_registry_available', isinstance(deps.entries, list), 'The verified job registry was not supplied.')
    check('schema_snapshots_supplied', isinstance(deps.schemas, list) and bool(deps.schemas),
          'No captured schema snapshot was supplied. This entry point looks none up.')
    check('capture_authority_configured', isinstance(deps.captures, CaptureAuthority),
          'No capture authority is configured, so no snapshot can be shown to come from a collector.')
    check('accepted_collectors_configured', _collectors(deps) is not None, 'No accepted collector identities are configured on the server side.')
    check('live_schema_probe_configured', isinstance(deps.schema_probe, SchemaProbe),
          'No read-only schema probe is configured, so the approved schema cannot be confirmed live before a read.')
    mapping = mapping_readiness(config, deps.entries if isinstance(deps.entries, list) else None, deps.schemas,
                                capture_check=lambda schema, row: capture_problems(deps, schema, row))
    mapping_ok = bool(mapping) and all(m['ready'] for m in mapping)
    check('source_mapping_ready_for_every_assignment', mapping_ok,
          'No assignment is configured.' if not mapping else '; '.join(
              f"assignment {m['assignment']} ({m['client_id']} / {m['job']}): {', '.join(m['blocked_reasons'])}" for m in mapping if not m['ready']))
    router = None
    if not mapping_ok:
        check('renderers_built_from_configuration', False, 'Not built: source-mapping readiness is blocked, so no renderer is touched.')
    else:
        try:
            router = _router(deps)
        except Exception as error:
            check('renderers_built_from_configuration', False, 'Renderers could not be built: ' + type(error).__name__)
        else:
            check('renderers_built_from_configuration', router is not None, 'Draft storage or assignments are missing.')
    check('source_adapter_configured', callable(deps.fetch), 'No source adapter was supplied; nothing can be read.')
    store_ok, detail = False, 'No durable store was supplied.'
    if deps.store is not None and all(hasattr(deps.store, n) for n in ('atomic', 'put_blob', 'ready')):
        try:
            store_ok = deps.store.ready() is True
            detail = 'The store tables do not exist. Creating them is an approved migration, not done here.'
        except Exception as error:
            detail = 'The store could not be checked: ' + type(error).__name__
    check('durable_store_ready', store_ok, detail)
    scopes, issues = [], []
    if router is not None and isinstance(deps.entries, list):
        try:
            scopes, issues = runner.plan(config, deps.entries, deps.clock(), router)
        except Exception as error:
            issues = [dict(code='plan_failed:' + type(error).__name__)]
    blocking = [i for i in issues if i.get('code') != 'built_by_external_process']
    check('at_least_one_runnable_scope', bool(scopes), 'No assignment is complete, verified, in the registry and routed.')
    check('no_assignment_issues', not blocking, ', '.join(sorted({i['code'] for i in blocking})))
    digest = snapshot_digest(config, deps.entries if isinstance(deps.entries, list) else [], router, deps.schemas, mapping) if router is not None else None
    ok, why = _attested(deps, digest, principal) if digest else (False, 'There is nothing complete enough to approve.')
    check('approval_attested_for_this_snapshot_and_principal', ok, why)
    check('quota_configured', isinstance(deps.quota, Quota) and isinstance(deps.quota.runs_per_hour, int)
          and not isinstance(deps.quota.runs_per_hour, bool) and deps.quota.runs_per_hour > 0, 'No run quota is configured.')
    check('delivery_transport_disabled', not transport.verified(deps.provider_contract),
          'This entry point builds drafts only. A provider contract must not be attached here.')
    return checks, scopes, issues, digest, router, mapping


def _number(value, cap, whole):
    """A finite positive number within the cap (never NaN, infinity, a boolean or text)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        return None
    if whole and (not isinstance(value, int)):
        return None
    return min(value, cap)


def _reserve(deps, principal, digest, nonce, run_id):
    """Single-use nonce and hourly quota, taken together in one transaction BEFORE any source is read."""
    now = deps.clock()
    hour = instant(now).strftime('%Y-%m-%dT%H')
    nonce_key = 'auth:nonce:' + sf.digest(dict(principal=principal.id, nonce=nonce))
    quota_key = 'auth:quota:' + sf.digest(dict(principal=principal.id, snapshot=digest, hour=hour))
    with deps.store.atomic([nonce_key, quota_key]) as records:
        if records[nonce_key]:
            return 'replayed_request'
        used = int(records[quota_key].get('runs') or 0)
        if used >= deps.quota.runs_per_hour:
            return 'quota_exceeded'
        records[nonce_key].update(used_at=now, run_id=run_id, principal=principal.id)
        records[quota_key].update(runs=used + 1, hour=hour, principal=principal.id, last_run_id=run_id, last_at=now)
    return None


def _respond(code, **body):
    return code, body


def handle(method, headers, body, deps):
    """(status code, JSON-able body). Fails closed on anything malformed; never echoes credentials."""
    if not isinstance(deps.authorizer, Authorizer):
        return _respond(503, status='not_configured')
    if not isinstance(headers, Mapping) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in headers.items()):
        return _respond(401, status='unauthorized')
    # Own the authorization inputs before any dependency callback can mutate their originals.
    # The same private snapshot is used for hashing, planning, approval and execution.
    try:
        body = deepcopy(body)
        headers = dict(headers)
        deps = replace(deps, config=deepcopy(deps.config), entries=deepcopy(deps.entries), schemas=deepcopy(deps.schemas))
        body_digest = sf.digest(body)             # refuses NaN / infinity / non-JSON values anywhere in the body
    except Exception:
        return _respond(400, status='bad_request', detail='The body must be plain JSON with finite numbers.')
    try:
        principal = deps.authorizer.verify(method, dict(headers), body_digest)
    except Exception:
        principal = None
    if not isinstance(principal, Principal) or not isinstance(principal.id, str) or not principal.id.strip():
        return _respond(401, status='unauthorized')
    if method != 'POST':
        return _respond(405, status='method_not_allowed')
    if not isinstance(body, dict) or set(body) - FIELDS:
        return _respond(400, status='bad_request', detail='Allowed fields: ' + ', '.join(sorted(FIELDS)) + '.')
    run_id, mode, nonce = body.get('run_id'), body.get('mode', 'dry_run'), body.get('nonce')
    if not isinstance(run_id, str) or not run_id.strip() or len(run_id) > 200 or not isinstance(mode, str) or mode not in MODES:
        return _respond(400, status='bad_request', detail='run_id (text, up to 200 characters) and mode dry_run or run.')
    budget = _number(body.get('budget_seconds', 40), MAX_BUDGET_SECONDS, whole=False)
    scopes_cap = _number(body.get('max_scopes', 25), MAX_SCOPES, whole=True)
    if budget is None or scopes_cap is None:
        return _respond(400, status='bad_request', detail='budget_seconds must be a finite positive number and max_scopes a positive whole number.')
    limits = dict(budget_seconds=budget, max_scopes=scopes_cap)
    try:
        age = abs((instant(deps.clock()) - instant(body.get('timestamp'))).total_seconds())
    except Exception:
        age = None
    if age is None or age > REQUEST_WINDOW_SECONDS:
        return _respond(401, status='stale_or_undated_request')
    if mode == 'run' and (not isinstance(nonce, str) or not 16 <= len(nonce) <= 200):
        return _respond(400, status='bad_request', detail='A run needs a single-use nonce of 16 to 200 characters.')
    try:
        checks, scopes, issues, digest, router, mapping = preflight(deps, principal)
    except Exception as error:
        return _respond(409, status='not_activated', sources_read=0, records_written=0, detail='Preflight failed: ' + type(error).__name__)
    ready = all(c['ok'] for c in checks)
    report = dict(mode=mode, run_id=run_id, principal=principal.id, snapshot_digest=digest, preflight=checks, ready_to_run=ready,
                  planned_scopes=len(scopes),
                  planned=[dict(scope=x['key'], job=x['scope']['job'], client_id=x['scope']['client_id'],
                                business_date=x['scope']['business_date'], shift=x['scope']['shift']) for x in scopes],
                  issues=issues, limits=limits, transport='disabled', client_release='held',
                  mapping_readiness=mapping, proof=dict(NO_PROOF),
                  live_schema_drift_check='not_performed_in_dry_run' if mode == 'dry_run' else 'not_performed')
    if mode == 'dry_run':
        return _respond(200, status='dry_run', sources_read=0, records_written=0, **report)
    if not ready:
        return _respond(409, status='not_activated', sources_read=0, records_written=0, **report)
    refused = _reserve(deps, principal, digest, nonce, run_id)
    if refused:
        return _respond(409 if refused == 'replayed_request' else 429, status=refused, sources_read=0, **report)
    # Live schema confirmation, read-only: every list once now, then again immediately before each source read.
    guard = _DriftGuard(deps, mapping)
    drift = guard.check_all()
    if drift:
        return _respond(409, status='schema_drift', sources_read=0, schema_probes=guard.count, drift=drift,
                        **dict(report, live_schema_drift_check='failed_before_any_read'))
    report['live_schema_drift_check'] = 'performed_before_each_read'
    try:
        summary = runner.run_once(deps.store, deps.config, fetch=guard.fetch, render=router, run_id=run_id,
                                  receipt_namespace=sf.digest(dict(principal=principal.id, snapshot=digest)),
                                  entries=deps.entries, clock=deps.clock, monotonic=deps.monotonic,
                                  budget_seconds=limits['budget_seconds'], max_scopes=limits['max_scopes'])
    except Exception as error:
        return _respond(500, status='error', error_type=type(error).__name__, schema_probes=guard.count, **report)
    return _respond(200, status='ran', summary=summary, schema_probes=guard.count, **report)
