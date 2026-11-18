"""Opt-in draft-only scheduler core. No HTTP route, cron or production activation.

The caller owns authoritative configuration, full capture, a transactional state
store and an idempotent renderer. A quiet period is not evidence of completion.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from hosting import source_first as sf

WAIT = timedelta(hours=1)
MAX_AGE = timedelta(minutes=5)


def instant(value):
    if not isinstance(value, str) or 'T' not in value:
        raise ValueError('An explicit offset-aware timestamp is required.')
    d = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if d.tzinfo is None:
        raise ValueError('Naive or date-only scan times cannot start a timer.')
    return d.astimezone(timezone.utc)


def scope_key(scope):
    base = sf.scope(scope)
    for key in ('client_id', 'shift', 'zone'):
        if not isinstance(scope.get(key), str) or not scope[key].strip():
            raise ValueError('Explicit client, recorded shift and timezone required.')
        base[key] = scope[key]
    ZoneInfo(base['zone'])
    return base


def observe(previous, scope, snapshot, now):
    """Pure state transition; caller serializes and persists before acknowledging.

    snapshot rows must already be selected with verified source business-date and
    recorded shift mappings. Row hash includes all source facts, including photos.
    not_before is a trusted, offset-aware shift-end/closure bound, NOT a default.
    """
    s = scope_key(scope); at = instant(now)
    p = deepcopy(previous or {})
    if p and p['scope'] != s:
        raise ValueError('State cannot cross source/job/day/shift boundaries.')
    if snapshot.get('scope') != s or snapshot.get('terminal') is not True:
        raise ValueError('Exact terminal source capture required; partial reads cannot advance timers.')
    captured = instant(snapshot['captured_at'])
    if not timedelta(0) <= at - captured <= MAX_AGE:
        raise ValueError('Capture is stale or future-dated.')
    if p and captured < instant(p['snapshot']['captured_at']):
        raise ValueError('Older capture cannot overwrite newer scheduler state.')
    rows = snapshot['rows']; ids = [r['id'] for r in rows]
    if len(ids) != len(set(ids)) or any(not isinstance(i,str) or not i for i in ids):
        raise ValueError('Missing/repeated source IDs.')
    latest = max((instant(r['scan_at']) for r in rows), default=None)
    if latest and latest > captured:
        raise ValueError('Future scan requires clock review.')
    photos=[p for p in snapshot.get('source_capture',{}).get('photos',[]) if p.get('record_id') in ids]
    fingerprint = sf.digest(dict(rows=sorted(rows, key=lambda r:r['id']), photos=sorted(photos,key=sf.digest)))
    changed = p.get('fingerprint') != fingerprint
    revision = (p.get('revision', 0) + 1) if changed else p['revision']
    # Initial old-data capture can become due immediately; a subsequently observed
    # change resets a full hour to accommodate late uploads/backdated records.
    quiet_from = max(latest, at) if latest and p and changed else (latest if changed else instant(p['quiet_from']) if p.get('quiet_from') else None)
    end = instant(snapshot['not_before']) if snapshot.get('not_before') else None
    due = max(quiet_from + WAIT, end) if quiet_from and end else quiet_from + WAIT if quiet_from else None
    status = 'no_scans' if not rows else 'due' if at >= due else 'waiting'
    operation = sf.digest(dict(scope=s, fingerprint=fingerprint, render_version=snapshot.get('render_version'), policy='last-scan-draft-v1'))
    # A completed/uncertain dispatch for unchanged content must never be replayed.
    receipts = deepcopy(p.get('receipts', {}))
    if operation in receipts:
        status = receipts[operation]['state']
    return dict(scope=s, fingerprint=fingerprint, revision=revision, captured_at=now,
                quiet_from=quiet_from.isoformat() if quiet_from else None,
                due_at=due.isoformat() if due else None, status=status,
                operation_id=operation, receipts=receipts, day_completion='not_established',
                client_release='held', timing_basis='verified_schedule' if end else 'provisional_quiet_window_schedule_unknown', snapshot=deepcopy(snapshot))


class Runner:
    """Serialized state storage and idempotent draft publisher injected by host.

    transaction(key) yields a mutable dict; it MUST commit atomically on success.
    render(snapshot, operation_id) returns an artifact/checklist receipt and must
    reconcile the same operation ID rather than repeat after uncertain outcomes.
    Never enable this with a normal client-email transport.
    """
    def __init__(self, transaction, capture, render, *, enabled=False):
        self.transaction, self.capture, self.render, self.enabled = transaction, capture, render, enabled

    def poll(self, scope, now):
        if not self.enabled:
            return {'status':'disabled'}
        key = sf.digest(scope_key(scope))
        snap = self.capture(scope)
        if hasattr(self.render,'render_version'):
            snap['render_version']=self.render.render_version()
        with self.transaction(key) as record:
            current = observe(record.get('state'), scope, snap, now)
            record['state'] = current
            if current['status'] != 'due':
                return deepcopy(current)
            op = current['operation_id']
            current['receipts'][op] = dict(state='uncertain', claimed_at=now)
            current['status'] = 'uncertain'  # crash after claim is reconcile-only
            request = deepcopy(current)
        try:
            receipt = self.render(request['snapshot'], op)
            if not isinstance(receipt,dict) or receipt.get('operation_id') != op or not receipt.get('workbook_sha256') or not receipt.get('checklist_sha256'):
                raise ValueError('Renderer must return matching workbook and checklist receipt.')
        except Exception as error:
            return dict(status='uncertain', operation_id=op, error_type=type(error).__name__)
        with self.transaction(key) as record:
            latest = record['state']
            latest['receipts'][op] = dict(state='draft_ready', receipt=receipt)
            if latest['operation_id'] == op:
                latest['status'] = 'draft_ready'
            return dict(status='draft_ready' if latest['operation_id']==op else 'superseded', operation_id=op, receipt=receipt)


def capture_adapter(scope, *, scan_field, shift_field, capture_fn, not_before):
    """Use existing complete, sealed SharePoint capture; never Date/Created fallback.

    Explicit scan_field/shift_field and not_before come from validated per-list
    configuration, not browser parameters or guessed defaults. capture_fn is the
    existing source_first_capture.capture in a live host; tests inject transport.
    """
    s=scope_key(scope)
    cap, blobs = capture_fn(sf.scope(s))
    sf.verify_seal(cap)
    if cap['scope'] != sf.scope(s) or cap.get('uncertain_dates'):
        raise ValueError('Uncertain business-day membership must be resolved before scheduling.')
    if not scan_field or not shift_field or scan_field in ('Date','createdDateTime','lastModifiedDateTime'):
        raise ValueError('Verified actual-scan field required, never date or arrival metadata.')
    rows=[]
    for record in cap['records']:
        raw=record['raw']
        if raw.get(shift_field) is None:
            raise ValueError('Missing shift mapping prevents trustworthy scope membership.')
        if str(raw[shift_field]) != s['shift']:
            continue
        instant(raw.get(scan_field))
        rows.append(dict(id=record['id'], scan_at=raw[scan_field], source=deepcopy(record)))
    return dict(scope=s, rows=rows, captured_at=cap['captured_at'], terminal=True,
                not_before=not_before, source_capture=cap, photo_blobs={k:__import__('base64').b64encode(v).decode() for k,v in blobs.items()})
