"""Hosted entry point for the last-scan draft runner. PREPARED, NOT CONNECTED.

Nothing imports this module: there is no route, no cron entry and no scheduled task that reaches it, and adding one
is a separate, reviewed activation step (docs/LAST_SCAN_ACTIVATION_CHECKLIST.md). It contains no network client, no
secret, no identity provider and no sender. Everything it needs is handed in through ``Deps``, and every one of
those defaults to "not configured", which means DENY.

Three things are deliberately NOT implemented here, only given an interface, because they must come from systems
this code does not own:
  * ``Authorizer``        who is calling (a verified principal). No shared static token is accepted.
  * ``ApprovalAuthority`` whether exactly this configuration + renderers + job registry was approved, and for
                          which principals. An approval written into the configuration itself counts for nothing.
  * ``Quota``             how many runs a principal may start for an approved snapshot per hour.

``handle`` is one bounded call:
  * no authorizer / principal not verified          -> 503 / 401, nothing read, nothing written
  * mode "dry_run" (the default)                    -> the plan and the preflight; reads no source, writes nothing
  * mode "run"                                      -> only if enabled, attested for this exact snapshot and this
                                                       principal, every preflight check passes, the request's
                                                       single-use nonce is reserved and the quota allows it
A run builds DRAFTS only. Delivery transport stays disabled (hosting.last_scan_transport has no verified provider).
"""
import math
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Callable, Optional

from hosting import source_first as sf
from hosting import last_scan_runner as runner
from hosting import last_scan_transport as transport
from hosting.last_scan_drafts import instant
from hosting.last_scan_local import ArtifactStore, StoredDraftRouter
from hosting.last_scan_service import binding

SETTING_KEY = 'last_scan_runner'        # where a host would keep the configuration; absent means disabled
MAX_BUDGET_SECONDS, MAX_SCOPES = 45, 50
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


def snapshot_digest(config, entries, router):
    """One hash over everything an approval must cover: the configuration, the renderer version of every routed
    assignment (mapping, header, pinned template, generator code, binding) and the job registry as supplied."""
    renders = {}
    for a in (config or {}).get('assignments') or []:
        try:
            renders[sf.digest(binding(a))] = router.for_scope(a).render_version()
        except Exception:
            continue
    registry = sorted(sf.digest(e) for e in entries or [])
    return sf.digest(dict(config={k: v for k, v in (config or {}).items() if k != 'activation'},
                          render_versions=renders, registry=registry))


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
    router = None
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
    digest = snapshot_digest(config, deps.entries if isinstance(deps.entries, list) else [], router) if router is not None else None
    ok, why = _attested(deps, digest, principal) if digest else (False, 'There is nothing complete enough to approve.')
    check('approval_attested_for_this_snapshot_and_principal', ok, why)
    check('quota_configured', isinstance(deps.quota, Quota) and isinstance(deps.quota.runs_per_hour, int)
          and not isinstance(deps.quota.runs_per_hour, bool) and deps.quota.runs_per_hour > 0, 'No run quota is configured.')
    check('delivery_transport_disabled', not transport.verified(deps.provider_contract),
          'This entry point builds drafts only. A provider contract must not be attached here.')
    return checks, scopes, issues, digest, router


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
    try:
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
        checks, scopes, issues, digest, router = preflight(deps, principal)
    except Exception as error:
        return _respond(409, status='not_activated', sources_read=0, records_written=0, detail='Preflight failed: ' + type(error).__name__)
    ready = all(c['ok'] for c in checks)
    report = dict(mode=mode, run_id=run_id, principal=principal.id, snapshot_digest=digest, preflight=checks, ready_to_run=ready,
                  planned_scopes=len(scopes),
                  planned=[dict(scope=x['key'], job=x['scope']['job'], client_id=x['scope']['client_id'],
                                business_date=x['scope']['business_date'], shift=x['scope']['shift']) for x in scopes],
                  issues=issues, limits=limits, transport='disabled', client_release='held')
    if mode == 'dry_run':
        return _respond(200, status='dry_run', sources_read=0, records_written=0, **report)
    if not ready:
        return _respond(409, status='not_activated', sources_read=0, records_written=0, **report)
    refused = _reserve(deps, principal, digest, nonce, run_id)
    if refused:
        return _respond(409 if refused == 'replayed_request' else 429, status=refused, sources_read=0, **report)
    try:
        summary = runner.run_once(deps.store, deps.config, fetch=deps.fetch, render=router, run_id=run_id,
                                  entries=deps.entries, clock=deps.clock, monotonic=deps.monotonic,
                                  budget_seconds=limits['budget_seconds'], max_scopes=limits['max_scopes'])
    except Exception as error:
        return _respond(500, status='error', error_type=type(error).__name__, **report)
    return _respond(200, status='ran', summary=summary, **report)
