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
The accepted-collector policy (``Deps.collectors``) is copied into a private immutable set before the first callback,
used unchanged for the whole request, and included in the approved snapshot digest. Handing these objects in is a
wiring boundary, not cryptographic authenticity: nothing here proves that an authority, a collector or a policy is
genuine, only that the request was judged against exactly what the host supplied when it arrived.
Records from the two authorities must meet the versioned record contract in hosting.last_scan_authority, from an
issuer the host accepts (``Deps.issuers``, frozen like the collector policy and part of the approved digest), and
their validity is judged on the host clock: before a run, and again before EVERY source read, where the approval and
the capture receipt must also be the same records as when the run was authorized. A host clock more than the allowed
skew behind the last time the store saw is refused. A clock that is simply wrong is not detected.
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
from hosting import last_scan_authority as authority
from hosting import last_scan_readiness as readiness
from hosting import last_scan_runner as runner
from hosting import last_scan_transport as transport
from hosting.last_scan_drafts import instant
from hosting.last_scan_local import ArtifactStore, StoredDraftRouter
from hosting.last_scan_service import binding, record_site_hold, site_hold

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
    ``attestation(snapshot_digest)`` returns None, or exactly ONE version 1 approval record as plain data
    (hosting.last_scan_authority): contract, version, record_id, issuer, status "active", snapshot_digest, approved_by,
    approved_at, principals, not_before, expires_at, and optionally revoked_at / revoked_by / revocation_reason.
    A withdrawn approval is either no record, or a record that says so; it is never a record with a field of the
    authority's own invention, because unknown fields are refused."""
    def attestation(self, snapshot_digest):
        raise NotImplementedError


class CaptureAuthority:
    """Answers, from a place neither the runner nor the configuration can write to, where a schema snapshot came from.
    ``receipt(snapshot_identity)`` returns None, or exactly ONE version 1 capture receipt as plain data
    (hosting.last_scan_authority): contract, version, record_id, issuer, status "active", snapshot_identity, site_id,
    list_id, schema_digest, collector, captured_at, expires_at, and optionally the revocation fields."""
    def receipt(self, snapshot_identity):
        raise NotImplementedError


class SchemaProbe:
    """Read-only. ``current(site_id, list_id)`` returns the list's column definitions as they are now, in the same
    shape as a captured snapshot, with ``captured_at`` set to when it was observed. It must read metadata only."""
    def current(self, site_id, list_id):
        raise NotImplementedError


class AuthorityWithdrawn(Exception):
    """Raised in place of a source read when an approval or capture receipt no longer counts, or the host clock went back."""


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
    issuers: Any = None                  # {'approval': [...], 'capture': [...]}: authority identities the host accepts (server-side)
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


def _freeze_collectors(value):
    """The accepted-collector policy as one private immutable set, or None (deny). The host's container is read exactly
    once, into a tuple, and only that tuple is validated and kept, so nothing that changes the container afterwards
    (a callback, another thread) can change the policy a request is judged under."""
    if isinstance(value, frozenset):
        taken = tuple(value)
    elif isinstance(value, (set, tuple, list)):
        try:
            taken = tuple(value)
        except RuntimeError:                                                 # a set resized by another thread mid-copy
            return None
    else:
        return None
    if taken and all(isinstance(x, str) and x.strip() and x == x.strip() for x in taken):
        return frozenset(taken)
    return None


def _collectors(deps):
    return _freeze_collectors(deps.collectors)


def _issuers(deps):
    return authority.freeze_issuers(deps.issuers)


CLOCK = 'auth:clock'                    # the latest host-clock reading a reservation in this store was made at


def clock_problem(deps):
    """Read-only. The host clock is the only clock validity is judged on, so a clock that has gone back by more than
    the allowed skew since this store last saw it is refused: it could bring an expired record back to life."""
    try:
        with deps.store.transaction(CLOCK) as seen:
            high = seen.get('high_water')
        if clock_behind(high, deps.clock()):
            return 'The host clock is behind the last time this store saw (' + high + ').'
    except Exception as error:
        return 'The clock record could not be read: ' + type(error).__name__
    return None


def clock_behind(high, now):
    return bool(high) and (instant(high) - instant(now)).total_seconds() > SCHEMA_FUTURE_SKEW_SECONDS


def time_gate(deps, started, bounds, max_seconds=None):
    """None, or why no source may be touched and nothing sent NOW. PURE: it asks no authority. It reads the store's
    last-seen time and then the host clock, once, and compares them with what is already on record for this call:
    when it started and the validity bounds of the records it was authorized under. It is the LAST thing before any
    source IO, after every authority callback, because a callback can take time or run while the clock, the
    watermark or a validity bound moves."""
    try:
        with deps.store.transaction(CLOCK) as seen:
            high = seen.get('high_water')
        now, begun = instant(deps.clock()), instant(started)
        if now < begun:
            return 'host_clock_went_backwards_during_the_call'
        if clock_behind(high, now.isoformat()):
            return 'host_clock_is_behind_the_last_seen_time'
        if not bounds or any(now >= instant(b) for b in bounds):
            return 'a_validity_bound_of_this_call_has_passed'
        if max_seconds is not None and (now - begun).total_seconds() > max_seconds:
            return 'call_took_too_long'
    except Exception as error:
        return 'time_gate_failed:' + type(error).__name__
    return None


def mark_clock(record, now):
    if not record.get('high_water') or instant(now) > instant(record['high_water']):
        record['high_water'] = instant(now).isoformat()


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
        record = authority.plain(deps.captures.receipt(identity))           # ONE read, one private copy of plain data
    except Exception:
        return ['capture_authority_unreadable'], None
    if not isinstance(record, dict) or record.get('snapshot_identity') != identity:
        return ['no_capture_receipt_for_this_exact_snapshot'], None
    # Lifecycle under the record contract: active, from an accepted issuer, within its stated validity on the HOST clock.
    reasons = ['capture_receipt_contract:' + r for r in authority.problems(record, 'capture', _issuers(deps), deps.clock())]
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
    if reasons:
        return reasons, None
    # What an approval covers of this receipt. A renewed receipt (new record_id or validity) is another receipt.
    capture = dict(collector=collector, captured_at=at.isoformat(), origin='reported_by_capture_authority',
                   record_id=record['record_id'], issuer=record['issuer'], expires_at=instant(record['expires_at']).isoformat())
    return reasons, capture


class _DriftGuard:
    """Confirms, read-only, that each list's live column definitions are still the approved ones. Used once for every
    list before a run starts and again immediately before every source read. Anything short of a fresh, well-formed,
    matching answer is drift. There remains an unavoidable gap between this look and the read that follows it."""
    def __init__(self, deps, mapping, recheck=None, gate=None):
        self.deps, self.count, self.skipped_held, self.recheck, self.gate, self.withdrawn = deps, 0, [], recheck, gate, None
        self.approved = {(m['site_id'], m['list_id']): m['schema_digest'] for m in mapping}

    def stop(self, scope):
        """None, or why the source must not be touched. Two phases, in this order: first the authority callbacks
        (``recheck``), which can block; then the pure time gate, which asks nobody and is the last thing before IO."""
        return (self.recheck(scope) if self.recheck else None) or (self.gate() if self.gate else None)

    def held(self, site_id):
        """The site's stored wait, if one is in force. A held site is asked NOTHING: no metadata, no columns, no items."""
        return site_hold(self.deps.store, site_id, self.deps.clock()) if isinstance(site_id, str) else None

    def problem(self, site_id, list_id):
        approved = self.approved.get((site_id, list_id))
        if not isinstance(approved, str) or not isinstance(self.deps.schema_probe, SchemaProbe):
            return 'no_approved_schema_or_probe_for_this_list'
        if self.held(site_id):
            return 'site_is_on_a_provider_wait'                              # not probed; the read that follows is refused too
        self.count += 1
        try:
            live = deepcopy(self.deps.schema_probe.current(site_id, list_id))
        except Exception as error:
            evidence = getattr(error, 'evidence', None)
            cool = evidence.get('cooldown') if isinstance(evidence, dict) else None
            if isinstance(cool, dict):                                       # the probe itself was told to wait: the whole site waits
                record_site_hold(self.deps.store, site_id, cool, instant(self.deps.clock()), self.deps.clock())
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
            if self.held(site_id):                                           # nothing is asked of a held site, and it is not drift:
                self.skipped_held.append(dict(site_id=site_id, list_id=list_id))   # the runner reports its scopes as cooldown, unread
                continue
            # A probe can take time. The authorities and the clock are asked before it and again after it, and the
            # first refusal ends the round: no further probe is made and no item is ever read.
            self.withdrawn = self.stop(dict(site_id=site_id, list_id=list_id))
            if self.withdrawn:
                break
            problem = self.problem(site_id, list_id)
            self.withdrawn = self.stop(dict(site_id=site_id, list_id=list_id))
            if self.withdrawn:
                break
            if problem:
                out.append(dict(site_id=site_id, list_id=list_id, problem=problem))
        return out

    def fetch(self, scope, settings):
        withdrawn = self.stop(scope)                                         # authorities, then the pure time gate; never the source
        if withdrawn:
            raise AuthorityWithdrawn(withdrawn)                              # the source is not probed and not read
        problem = self.problem(scope.get('site_id'), scope.get('list_id'))
        if problem:
            raise SchemaDrift(problem)                                       # the source is not read
        withdrawn = self.stop(scope)                                         # again: the probe just made may have taken time
        if withdrawn:
            raise AuthorityWithdrawn(withdrawn)                              # no item is read
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


def snapshot_digest(config, entries, router, schemas=None, mapping=None, collectors=None, issuers=None):
    """One hash over everything an approval must cover: the configuration, the renderer version of every routed
    assignment (mapping, header, pinned template, generator code, binding), the job registry as supplied, the
    identity of every supplied schema snapshot, and per assignment the snapshot and mapping contract it was checked
    against together with the outcome, and the accepted-collector policy in force."""
    renders = {}
    for a in (config or {}).get('assignments') or []:
        try:
            renders[sf.digest(binding(a))] = router.for_scope(a).render_version()
        except Exception:
            continue
    registry = sorted(sf.digest(e) for e in entries or [])
    return sf.digest(dict(config={k: v for k, v in (config or {}).items() if k != 'activation'},
                          render_versions=renders, registry=registry,
                          accepted_collectors=sorted(collectors) if isinstance(collectors, frozenset) else None,
                          accepted_issuers=[[k, list(v)] for k, v in issuers] if isinstance(issuers, tuple) else None,
                          schema_snapshots=sorted(readiness.snapshot_identity(s) for s in schemas) if isinstance(schemas, list) else None,
                          mapping_readiness=[{k: m[k] for k in ('assignment', 'client_id', 'job', 'site_id', 'list_id', 'ready', 'blocked_reasons',
                                                                'schema_snapshot', 'schema_digest', 'mapping_contract', 'capture')} for m in mapping or []]))


def _attestation(deps, digest):
    """(record, why). ONE read of the approval authority, kept as one private copy of plain data. Every question about
    an approval is put to this one record: nothing is ever combined from two answers, and an object that answers
    differently each time it is asked is reduced to what it actually holds, once."""
    if not isinstance(deps.approvals, ApprovalAuthority):
        return None, 'No approval authority is configured.'
    try:
        record = authority.plain(deps.approvals.attestation(digest))
    except Exception as error:
        return None, 'The approval authority could not be read: ' + type(error).__name__
    return record, ''


def _judge(deps, record, digest, principal, action=None, margin=0):
    """Whether this one record approves exactly this digest and action for this caller, now, on the host clock."""
    if not isinstance(record, dict) or record.get('snapshot_digest') != digest:
        return False, 'No approval exists for exactly this configuration, renderers and job registry.'
    if not all(isinstance(record.get(k), str) and record[k].strip() for k in ('approved_by', 'approved_at')):
        return False, 'The approval does not say who approved it and when.'
    allowed = record.get('principals')
    if not isinstance(allowed, (list, tuple)) or principal.id not in allowed:
        return False, 'This caller is not named in the approval.'
    lifecycle = authority.problems(record, 'approval', _issuers(deps), deps.clock(), margin)
    if lifecycle:
        return False, 'The approval record does not count under the record contract: ' + ', '.join(lifecycle) + '.'
    if record.get('action') != action:
        return False, 'The approval is for another action.'
    return True, ''


def _approval(deps, digest, principal, action=None, margin=0):
    """(ok, why, what was approved). ONE read, ONE record, judged whole. ``identity`` covers the whole record, so a
    renewal (a new record_id or new validity bounds) is a different approval and has to be authorized afresh."""
    record, why = _attestation(deps, digest)
    if why:
        return False, why, None
    ok, why = _judge(deps, record, digest, principal, action, margin)
    if not ok:
        return False, why, None
    return True, '', dict(identity=authority.identity(record, 'approval'), record_id=record['record_id'], issuer=record['issuer'],
                          not_before=instant(record['not_before']).isoformat(), expires_at=instant(record['expires_at']).isoformat())


def _attested(deps, digest, principal):
    return _approval(deps, digest, principal)[:2]


def still_authorized(deps, principal, digest, approval, mapping, started, scope):
    """None, or why a run authorized at ``started`` may no longer read this scope. Asks the authorities again, each as
    ONE record judged whole, and requires the SAME approval record and the SAME capture receipt as when it started."""
    if instant(deps.clock()) < instant(started):
        return 'host_clock_went_backwards_during_the_call'
    if clock_problem(deps):                                                  # a concurrent worker has since seen a later time
        return 'host_clock_is_behind_the_last_seen_time'
    ok, why, now_ = _approval(deps, digest, principal)
    if not ok:
        return 'run_approval_withdrawn: ' + why
    if now_['identity'] != approval['identity']:
        return 'run_approval_is_another_record_than_the_one_this_call_was_authorized_under'
    rows = [m for m in mapping if m['site_id'] == scope.get('site_id') and m['list_id'] == scope.get('list_id')
            and all(k not in scope or m[k] == scope[k] for k in ('client_id', 'job'))]
    if not rows:
        return 'capture_receipt:no_approved_schema_for_this_scope'
    for row in rows:
        schema = next((s for s in deps.schemas or [] if readiness.snapshot_identity(s) == row['schema_snapshot']), None)
        if schema is None:
            return 'capture_receipt:no_approved_schema_for_this_scope'
        reasons, capture = capture_problems(deps, schema, row)
        if not reasons and capture != row['capture']:
            reasons = ['identity_changed_since_approval']
        if reasons:
            return 'capture_receipt:' + ','.join(reasons)
    return None


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
    check('accepted_authority_issuers_configured', _issuers(deps) is not None,
          'No accepted approval and capture authority identities are configured on the server side.')
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
    behind = clock_problem(deps) if store_ok else None
    check('host_clock_not_behind_the_last_seen_time', not behind, behind or '')
    scopes, issues = [], []
    if router is not None and isinstance(deps.entries, list):
        try:
            scopes, issues = runner.plan(config, deps.entries, deps.clock(), router)
        except Exception as error:
            issues = [dict(code='plan_failed:' + type(error).__name__)]
    blocking = [i for i in issues if i.get('code') != 'built_by_external_process']
    check('at_least_one_runnable_scope', bool(scopes), 'No assignment is complete, verified, in the registry and routed.')
    check('no_assignment_issues', not blocking, ', '.join(sorted({i['code'] for i in blocking})))
    digest = snapshot_digest(config, deps.entries if isinstance(deps.entries, list) else [], router, deps.schemas, mapping,
                             _collectors(deps), _issuers(deps)) if router is not None else None
    ok, why, approval = _approval(deps, digest, principal) if digest else (False, 'There is nothing complete enough to approve.', None)
    check('approval_attested_for_this_snapshot_and_principal', ok, why)
    check('quota_configured', isinstance(deps.quota, Quota) and isinstance(deps.quota.runs_per_hour, int)
          and not isinstance(deps.quota.runs_per_hour, bool) and deps.quota.runs_per_hour > 0, 'No run quota is configured.')
    check('delivery_transport_disabled', not transport.verified(deps.provider_contract),
          'This entry point builds drafts only. A provider contract must not be attached here.')
    return checks, scopes, issues, digest, router, mapping, approval


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
    with deps.store.atomic([nonce_key, quota_key, CLOCK]) as records:
        # In THIS transaction, before anything is taken: the look in preflight may be out of date by now.
        if clock_behind(records[CLOCK].get('high_water'), now):
            return 'host_clock_behind'
        if records[nonce_key]:
            return 'replayed_request'
        mark_clock(records[CLOCK], now)
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
    # The accepted-collector policy is frozen here too, first, so no callback can widen or narrow it mid-request.
    policy, issuers = _freeze_collectors(deps.collectors), authority.freeze_issuers(deps.issuers)
    try:
        body = deepcopy(body)
        headers = dict(headers)
        deps = replace(deps, config=deepcopy(deps.config), entries=deepcopy(deps.entries), schemas=deepcopy(deps.schemas), collectors=policy,
                       issuers=issuers)
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
        started = deps.clock()
        checks, scopes, issues, digest, router, mapping, approval = preflight(deps, principal)
    except Exception as error:
        return _respond(409, status='not_activated', sources_read=0, records_written=0, detail='Preflight failed: ' + type(error).__name__)
    ready = all(c['ok'] for c in checks)
    report = dict(mode=mode, run_id=run_id, principal=principal.id, snapshot_digest=digest, preflight=checks, ready_to_run=ready,
                  planned_scopes=len(scopes),
                  planned=[dict(scope=x['key'], job=x['scope']['job'], client_id=x['scope']['client_id'],
                                business_date=x['scope']['business_date'], shift=x['scope']['shift']) for x in scopes],
                  issues=issues, limits=limits, transport='disabled', client_release='held',
                  mapping_readiness=mapping, proof=dict(NO_PROOF), accepted_collectors=sorted(policy) if policy is not None else None,
                  approval={k: approval[k] for k in ('record_id', 'issuer', 'not_before', 'expires_at')} if approval else None,
                  live_schema_drift_check='not_performed_in_dry_run' if mode == 'dry_run' else 'not_performed')
    if mode == 'dry_run':
        return _respond(200, status='dry_run', sources_read=0, records_written=0, **report)
    if not ready:
        return _respond(409, status='not_activated', sources_read=0, records_written=0, **report)
    refused = _reserve(deps, principal, digest, nonce, run_id)
    if refused:
        return _respond(429 if refused == 'quota_exceeded' else 409, status=refused, sources_read=0, **report)
    # Live schema confirmation, read-only: every list once now, then again immediately before each source read.
    # Before EVERY source read the approval and the capture receipt are asked for again: same records, still valid.
    # The pure time gate runs after those callbacks, last before any probe or read: host clock, watermark, and the
    # validity bounds of the approval and of every receipt this run was authorized under.
    bounds = [approval['expires_at']] + [m['capture']['expires_at'] for m in mapping]
    guard = _DriftGuard(deps, mapping, recheck=lambda scope: still_authorized(deps, principal, digest, approval, mapping, started, scope),
                        gate=lambda: time_gate(deps, started, bounds))
    drift = guard.check_all()
    if guard.withdrawn:                                                      # noticed around a run-start probe: nothing else is asked of any source
        return _respond(409, status='authority_withdrawn', reason=guard.withdrawn, sources_read=0, schema_probes=guard.count,
                        **dict(report, live_schema_drift_check='stopped_authority_or_clock'))
    if drift:
        return _respond(409, status='schema_drift', sources_read=0, schema_probes=guard.count, drift=drift,
                        sites_on_provider_wait=guard.skipped_held, **dict(report, live_schema_drift_check='failed_before_any_read'))
    report['live_schema_drift_check'] = 'performed_before_each_read'
    report['sites_on_provider_wait'] = guard.skipped_held                    # not probed and not read in this run
    try:
        summary = runner.run_once(deps.store, deps.config, fetch=guard.fetch, render=router, run_id=run_id,
                                  receipt_namespace=sf.digest(dict(principal=principal.id, snapshot=digest)),
                                  entries=deps.entries, clock=deps.clock, monotonic=deps.monotonic,
                                  budget_seconds=limits['budget_seconds'], max_scopes=limits['max_scopes'])
    except Exception as error:
        return _respond(500, status='error', error_type=type(error).__name__, schema_probes=guard.count, **report)
    return _respond(200, status='ran', summary=summary, schema_probes=guard.count, **report)
