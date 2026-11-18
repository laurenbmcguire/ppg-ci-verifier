"""Bounded, off-by-default invocation of the last-scan draft worker.

Nothing here registers a schedule, opens a route or sends anything. A host that has been approved to do so calls
``run_once`` periodically with a unique ``run_id`` per firing. With no configuration, or ``enabled`` not exactly
True, it does nothing and reads nothing.

What a run may touch is decided only by explicit, verified assignments that ALSO match a verified entry of the
job registry (client / site / list / job). Nothing is matched by name, guessed, or defaulted: an assignment that
is incomplete, unverified or not in the registry becomes an issue in the run receipt and is skipped.

Each run is bounded (time budget, maximum scopes), starts where the previous run stopped, keeps one scope's
failure away from the others, and honours provider cooldowns: a site that asked us to wait is not read again
before its time, by this run or a later one. Run receipts are internal records; they notify nobody.
"""
import time
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from hosting import source_first as sf
from hosting import registry as job_registry
from hosting.last_scan_drafts import instant, scope_key
from hosting.last_scan_service import ScheduledWorker

# Arrival / save metadata and the business-date column are never the actual scan time.
FORBIDDEN_SCAN_FIELDS = {'date', 'date0', 'created', 'createddatetime', 'modified', 'lastmodifieddatetime',
                         'fields/created', 'fields/modified'}
REQUIRED = ('client_id', 'job', 'site_id', 'list_id', 'zone', 'scan_field', 'shift_field')
DEFAULT_COOLDOWN = timedelta(minutes=15)
CURSOR, HOLD, RUN = 'runner:cursor', 'runner:hold:', 'run:'


def registry_entries(inbox):
    """The live job registry (seed + entries people verified in the app). Read-only."""
    return job_registry.entries(inbox.setting(job_registry.REGISTRY_KEY, []) or [])


def _in_registry(a, entries):
    for e in entries or []:
        if e.get('verified') is True and e.get('list_id') == a['list_id'] and e.get('site_id') == a['site_id'] \
                and e.get('job') and job_registry._same_job(str(a['job']), str(e['job'])):
            return True
    return False


def _dates(a, now):
    if a.get('business_dates'):
        return list(a['business_dates'])
    local = instant(now).astimezone(ZoneInfo(a['zone'])).date()   # yesterday too: an overnight shift is still open
    return [(local - timedelta(days=1)).isoformat(), local.isoformat()]


def plan(config, entries, now):
    """Explicit assignments -> ([{scope, settings}], issues). Pure; reads no source."""
    scopes, issues, seen = [], [], set()
    for i, a in enumerate(config.get('assignments') or []):
        where = dict(index=i, site_id=a.get('site_id'), list_id=a.get('list_id'), job=a.get('job'))
        def issue(code):
            issues.append(dict(where, code=code))
        if a.get('active') is False:
            continue
        if any(not isinstance(a.get(k), str) or not a[k].strip() for k in REQUIRED):
            issue('incomplete_assignment'); continue
        if a.get('active') is not True or a.get('verified') is not True:
            issue('unverified_assignment'); continue
        if a.get('timestamp_semantics_verified') is not True:
            issue('scan_time_meaning_not_verified'); continue
        if a['scan_field'].strip().lower() in FORBIDDEN_SCAN_FIELDS:
            issue('scan_field_is_date_or_arrival_metadata'); continue
        shifts = a.get('recorded_shifts')
        if not isinstance(shifts, list) or not shifts or any(not isinstance(x, str) or not x.strip() for x in shifts):
            issue('recorded_shifts_missing'); continue
        if entries is None:
            issue('job_registry_unavailable'); continue
        if not _in_registry(a, entries):
            issue('not_in_verified_job_registry'); continue
        if a.get('externally_built') is True:      # another approved process builds this job: never build it twice
            issue('built_by_external_process'); continue
        try:
            for day in _dates(a, now):
                for shift in shifts:
                    s = scope_key(dict(site_id=a['site_id'], list_id=a['list_id'], job=a['job'], client_id=a['client_id'],
                                       business_date=day, shift=shift, zone=a['zone']))
                    key = sf.digest(s)
                    if key in seen:
                        issue('duplicate_scope'); continue
                    seen.add(key)
                    scopes.append(dict(key=key, scope=s, settings=deepcopy(a)))
        except Exception as error:
            issue('invalid_scope:' + type(error).__name__)
    return sorted(scopes, key=lambda x: x['key']), issues


def run_once(store, config, *, fetch, render, run_id, entries=None, clock=sf.now, budget_seconds=40, max_scopes=25,
             lease_seconds=300, monotonic=time.monotonic):
    """One bounded pass. Safe to call again with the same run_id: a finished run is replayed from its receipt
    without reading any source; an interrupted one resumes, and every scope step is idempotent by operation."""
    if not isinstance(config, dict) or config.get('enabled') is not True:
        return dict(status='disabled')
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError('A unique run_id per firing is required.')
    run_key = RUN + sf.digest(run_id)
    started = monotonic()
    with store.transaction(run_key) as record:
        if record.get('status') == 'finished':
            return dict(deepcopy(record['summary']), replayed=True)
        record.update(status='started', run_id=run_id, attempts=int(record.get('attempts') or 0) + 1)
        record.setdefault('started_at', clock())
    scopes, issues = plan(config, entries, clock())
    with store.transaction(CURSOR) as cursor:
        after = cursor.get('after')
    order = [x for x in scopes if after is None or x['key'] > after] + [x for x in scopes if after is not None and x['key'] <= after]

    seen_cooldown = {}
    def guarded(scope, settings):
        try:
            return fetch(scope, settings)
        except Exception as error:
            cool = (getattr(error, 'evidence', None) or {}).get('cooldown') if isinstance(getattr(error, 'evidence', None), dict) else None
            if cool:
                seen_cooldown[scope['site_id']] = cool
            raise
    worker = ScheduledWorker(store, guarded, render, clock=clock, enabled=True, lease_seconds=lease_seconds, owner=run_id)

    results, deferred, last = [], 0, None
    for n, item in enumerate(order):
        if len(results) - sum(1 for r in results if r['status'] == 'cooldown') >= max_scopes or monotonic() - started >= budget_seconds:
            deferred = len(order) - n
            break
        s = item['scope']
        row = dict(scope=item['key'], job=s['job'], business_date=s['business_date'], shift=s['shift'])
        hold_key = HOLD + sf.digest(s['site_id'])
        with store.transaction(hold_key) as hold:
            held_until = hold.get('not_before')
        if held_until and instant(clock()) < instant(held_until):
            results.append(dict(row, status='cooldown', not_before=held_until)); last = item['key']
            continue
        try:
            out = worker.tick(s, item['settings'])
        except Exception as error:                 # one scope never stops the others
            out = dict(status='error', error_type=type(error).__name__)
        cool = seen_cooldown.pop(s['site_id'], None)
        if cool:
            until = cool.get('not_before') if cool.get('known') and cool.get('not_before') else None
            try:
                until = instant(until).isoformat() if until else None
            except Exception:
                until = None
            until = until or (instant(clock()) + DEFAULT_COOLDOWN).isoformat()
            with store.transaction(hold_key) as hold:
                hold.update(not_before=until, recorded_at=clock(), provider_stated=bool(cool.get('known')))
            out = dict(out, cooldown_until=until)
        results.append(dict(row, **{k: out[k] for k in ('status', 'operation_id', 'error_type', 'reconcile', 'superseded_by',
                                                         'due_at', 'timing_basis', 'cooldown_until') if k in out}))
        last = item['key']
    if last is not None:
        with store.transaction(CURSOR) as cursor:
            cursor.update(after=last, at=clock())
    counts = {}
    for r in results:
        counts[r['status']] = counts.get(r['status'], 0) + 1
    summary = dict(status='finished', run_id=run_id, planned=len(order), processed=len(results), deferred=deferred,
                   counts=counts, results=results, issues=issues, finished_at=clock(),
                   client_release='held', notifications_sent=0)
    with store.transaction(run_key) as record:
        record.update(status='finished', summary=summary)
    return deepcopy(summary)
