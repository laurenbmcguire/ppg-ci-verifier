"""Hosted entry point for the last-scan draft runner. PREPARED, NOT CONNECTED.

Nothing imports this module: there is no route, no cron entry and no scheduled task that reaches it, and adding one
is a separate, reviewed activation step (docs/LAST_SCAN_ACTIVATION_CHECKLIST.md). It contains no network client, no
secret and no sender. Everything it needs is handed in through ``Deps`` by whoever wires it up later.

``handle`` is one bounded, authenticated call:
  * no token configured on the host            -> 503, nothing read
  * wrong or missing bearer token              -> 401, nothing read
  * mode "dry_run" (the default)               -> shows the plan and the preflight; reads no source, writes nothing
  * mode "run"                                 -> only if the stored configuration is enabled, carries an approval for
                                                  exactly this configuration, and every preflight check passes
A run builds DRAFTS only. Delivery transport stays disabled (hosting.last_scan_transport has no verified provider).
"""
import hmac
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from hosting import source_first as sf
from hosting import last_scan_runner as runner
from hosting import last_scan_transport as transport

SETTING_KEY = 'last_scan_runner'        # where a host would keep the configuration; absent means disabled
MAX_BUDGET_SECONDS, MAX_SCOPES, MIN_TOKEN = 45, 50, 32
MODES = ('dry_run', 'run')


@dataclass
class Deps:
    """Everything the entry point touches, supplied explicitly. Defaults mean "not configured"."""
    expected_token: Optional[str] = None            # the host's secret, read by the HOST from its own secret store
    config: Any = None                              # the stored configuration (see SETTING_KEY), or None
    entries: Any = None                             # verified job registry entries (runner.registry_entries), or None
    store: Any = None                               # hosting.last_scan_store store, tables already installed
    router: Any = None                              # RendererRouter built from the same assignments
    fetch: Optional[Callable] = None                # source adapter: fetch(scope, settings) -> snapshot
    clock: Callable = sf.now
    monotonic: Callable = time.monotonic
    provider_contract: Any = None                   # stays None: no provider has been verified
    extra: dict = field(default_factory=dict)


def config_digest(config):
    """Hash of the configuration that an approval must name. The approval itself is not part of it."""
    return sf.digest({k: v for k, v in (config or {}).items() if k != 'activation'})


def approved(config):
    a = (config or {}).get('activation')
    return (isinstance(a, dict) and a.get('config_digest') == config_digest(config)
            and all(isinstance(a.get(k), str) and a[k].strip() for k in ('approved_by', 'approved_at')))


def preflight(deps):
    """Read-only checks. Every one must be ok before a run. Reads no source and writes nothing."""
    config, checks = deps.config, []
    def check(name, ok, detail=''):
        checks.append(dict(check=name, ok=bool(ok), detail=detail))
    check('configuration_present', isinstance(config, dict), 'No configuration is stored.' if not isinstance(config, dict) else '')
    config = config if isinstance(config, dict) else {}
    check('enabled_is_exactly_true', config.get('enabled') is True, 'The runner is off unless enabled is exactly true.')
    check('approval_matches_this_configuration', approved(config), 'An approval must name the hash of exactly this configuration.')
    check('job_registry_available', deps.entries is not None, 'The verified job registry was not supplied.')
    check('renderer_router_available', callable(getattr(deps.router, 'for_scope', None)), 'One renderer per exact client/site/list/job is required.')
    check('source_adapter_configured', callable(deps.fetch), 'No source adapter was supplied; nothing can be read.')
    store_ok, detail = False, 'No durable store was supplied.'
    if deps.store is not None and all(hasattr(deps.store, n) for n in ('atomic', 'put_blob', 'ready')):
        try:
            store_ok = deps.store.ready() is True
            detail = '' if store_ok else 'The store tables do not exist. Creating them is an approved migration, not done here.'
        except Exception as error:
            detail = 'The store could not be checked: ' + type(error).__name__
    check('durable_store_ready', store_ok, detail)
    scopes, issues = [], []
    if isinstance(config.get('assignments'), list) and deps.entries is not None and callable(getattr(deps.router, 'for_scope', None)):
        try:
            scopes, issues = runner.plan(config, deps.entries, deps.clock(), deps.router)
        except Exception as error:
            issues = [dict(code='plan_failed:' + type(error).__name__)]
    blocking = [i for i in issues if i.get('code') != 'built_by_external_process']
    check('at_least_one_runnable_scope', bool(scopes), 'No assignment is complete, verified, in the registry and routed.')
    check('no_assignment_issues', not blocking, ', '.join(sorted({i['code'] for i in blocking})))
    check('delivery_transport_disabled', not transport.verified(deps.provider_contract),
          'This entry point builds drafts only. A provider contract must not be attached here.')
    return checks, scopes, issues


def _respond(code, **body):
    return code, body


def handle(method, headers, body, deps):
    """(status code, JSON-able body). Never raises for bad input; never echoes the token or configuration secrets."""
    if not isinstance(deps.expected_token, str) or len(deps.expected_token) < MIN_TOKEN:
        return _respond(503, status='not_configured')
    supplied = str((headers or {}).get('authorization') or (headers or {}).get('Authorization') or '')
    if not supplied.startswith('Bearer ') or not hmac.compare_digest(supplied[7:].encode(), deps.expected_token.encode()):
        return _respond(401, status='unauthorized')
    if method != 'POST':
        return _respond(405, status='method_not_allowed')
    if not isinstance(body, dict) or set(body) - {'run_id', 'mode', 'budget_seconds', 'max_scopes'}:
        return _respond(400, status='bad_request', detail='Allowed fields: run_id, mode, budget_seconds, max_scopes.')
    run_id, mode = body.get('run_id'), body.get('mode', 'dry_run')
    if not isinstance(run_id, str) or not run_id.strip() or len(run_id) > 200 or mode not in MODES:
        return _respond(400, status='bad_request', detail='run_id (text, up to 200 characters) and mode dry_run or run.')
    limits = {}
    for name, cap, default in (('budget_seconds', MAX_BUDGET_SECONDS, 40), ('max_scopes', MAX_SCOPES, 25)):
        value = body.get(name, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            return _respond(400, status='bad_request', detail=name + ' must be a positive number.')
        limits[name] = min(value, cap)
    checks, scopes, issues = preflight(deps)
    ready = all(c['ok'] for c in checks)
    report = dict(mode=mode, run_id=run_id, config_digest=config_digest(deps.config) if isinstance(deps.config, dict) else None,
                  preflight=checks, ready_to_run=ready, planned_scopes=len(scopes),
                  planned=[dict(scope=x['key'], job=x['scope']['job'], client_id=x['scope']['client_id'],
                                business_date=x['scope']['business_date'], shift=x['scope']['shift']) for x in scopes],
                  issues=issues, limits=limits, transport='disabled', client_release='held')
    if mode == 'dry_run':
        return _respond(200, status='dry_run', sources_read=0, records_written=0, **report)
    if not ready:
        return _respond(409, status='not_activated', sources_read=0, records_written=0, **report)
    summary = runner.run_once(deps.store, deps.config, fetch=deps.fetch, render=deps.router, run_id=run_id,
                              entries=deps.entries, clock=deps.clock, monotonic=deps.monotonic,
                              budget_seconds=limits['budget_seconds'], max_scopes=limits['max_scopes'])
    return _respond(200, status='ran', summary=summary, **report)
