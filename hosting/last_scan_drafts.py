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
# How long a source is left alone after it asks for a wait (HTTP 429/503). Policy, pending an owner's decision:
DEFAULT_COOLDOWN = timedelta(minutes=15)    # nothing usable was stated (absent, malformed, negative, not a whole number)
MIN_COOLDOWN = timedelta(seconds=60)        # never sooner than this, whatever was stated (0, a tiny number, a date already past)
MAX_COOLDOWN = timedelta(days=7)            # never later than this; a longer stated wait is held here and flagged for a person


def hold_until(cool, observed, *, default=DEFAULT_COOLDOWN, minimum=MIN_COOLDOWN, maximum=MAX_COOLDOWN):
    """When a source that asked for a wait may be read again. Pure: uses only ``observed``, the caller's own clock
    reading at the moment it saw the answer, so it cannot disagree with the clock the caller later compares with.

    A relative wait is ``observed`` plus the stated seconds. An absolute date is kept as stated. Anything unusable
    waits the default. The result is never before ``observed + minimum`` and never after ``observed + maximum``; an
    excessive or unrepresentable stated wait is held at the maximum and flagged, never shortened to the default."""
    c = cool if isinstance(cool, dict) else {}
    until, basis, capped, seconds = None, 'default', False, c.get('seconds')
    if c.get('basis') == 'relative_seconds':
        if isinstance(seconds, int) and not isinstance(seconds, bool) and seconds >= 0:
            basis = 'relative_seconds'
            if seconds > maximum.total_seconds():
                until, capped = observed + maximum, True
            else:
                until = observed + timedelta(seconds=seconds)
        elif c.get('unrepresentable') is True:
            until, basis, capped = observed + maximum, 'relative_seconds', True
    elif c.get('known') is True and c.get('not_before'):
        try:
            until, basis = instant(c['not_before']), 'http_date'
        except (ValueError, OverflowError):
            until = None
    if until is None:
        until, basis = observed + default, 'default'
    if until > observed + maximum:
        until, capped = observed + maximum, True
    if until < observed + minimum:
        until = observed + minimum
    return dict(until=until, basis=basis, capped_at_maximum=capped,
                stated_seconds=seconds if basis == 'relative_seconds' and isinstance(seconds, int) and seconds < 10 ** 12 else None)


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


PHOTO_IDENTITY = ('record_id', 'record_hash', 'field', 'field_hash', 'status', 'sha256', 'bytes')

def photo_identity(photo):
    """What makes a photo "the same evidence" for the quiet-hour timer: which record and field name it, whether
    it was captured, and its exact bytes. WHY a download failed (HTTP status, wording of the reason) is kept in
    the capture and the checklist but is transport detail: a source answering 403 one minute and 404 the next has
    not changed, and must not restart the hour or create a new draft. Captured vs not captured still does."""
    return {k: photo.get(k) for k in PHOTO_IDENTITY}


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
    photos=[photo_identity(p) for p in snapshot.get('source_capture',{}).get('photos',[]) if p.get('record_id') in ids]
    # Rows of the day in no configured shift are warned about in the checklist, so a change in them is a change
    # in what the draft must say: it makes a new operation instead of reusing a draft without the warning.
    fingerprint = sf.digest(dict(rows=sorted(rows, key=lambda r:r['id']), photos=sorted(photos,key=sf.digest),
                                 unassigned_shifts=snapshot.get('unassigned_shifts') or {}))
    changed = p.get('fingerprint') != fingerprint
    revision = (p.get('revision', 0) + 1) if changed else p['revision']
    # Initial old-data capture can become due immediately; a subsequently observed
    # change resets a full hour to accommodate late uploads/backdated records.
    quiet_from = max(latest, at) if latest and p and changed else (latest if changed else instant(p['quiet_from']) if p.get('quiet_from') else None)
    end = instant(snapshot['not_before']) if snapshot.get('not_before') else None
    due = max(quiet_from + WAIT, end) if quiet_from and end else quiet_from + WAIT if quiet_from else None
    status = 'no_scans' if not rows else 'due' if at >= due else 'waiting'
    operation = sf.digest(dict(scope=s, fingerprint=fingerprint, render_version=snapshot.get('render_version'), policy='last-scan-draft-v1'))
    # A completed/uncertain dispatch for unchanged content must never be replayed. A receipt is only a cached
    # artifact: when content returns to an earlier version the hour restarted above, and the earlier receipt must
    # NOT make it ready early. It is consulted only once the renewed timer is due.
    receipts = deepcopy(p.get('receipts', {}))
    if operation in receipts and status == 'due':
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


# Arrival / save metadata and the business-date column are never the actual scan time, under any spelling.
FORBIDDEN_SCAN_FIELDS = {'date', 'date0', 'created', 'createddatetime', 'modified', 'lastmodifieddatetime',
                         'fields/created', 'fields/modified', 'created_x0020_date', 'last_x0020_modified', 'author', 'editor'}


def capture_adapter(scope, *, scan_field, shift_field, capture_fn, not_before, recorded_shifts=None):
    """Use existing complete, sealed SharePoint capture; never Date/Created fallback.

    Explicit scan_field/shift_field and not_before come from validated per-list
    configuration, not browser parameters or guessed defaults. capture_fn is the
    existing source_first_capture.capture in a live host; tests inject transport.
    """
    s=scope_key(scope)
    if not isinstance(scan_field,str) or not isinstance(shift_field,str) or not scan_field.strip() or not shift_field.strip() \
            or scan_field.strip().lower() in FORBIDDEN_SCAN_FIELDS:
        raise ValueError('Verified actual-scan field required, never date or arrival metadata.')   # before any read
    cap, blobs = capture_fn(sf.scope(s))
    sf.verify_seal(cap)
    if cap['scope'] != sf.scope(s) or cap.get('uncertain_dates'):
        raise ValueError('Uncertain business-day membership must be resolved before scheduling.')
    if not scan_field or not shift_field or scan_field in ('Date','createdDateTime','lastModifiedDateTime'):
        raise ValueError('Verified actual-scan field required, never date or arrival metadata.')
    rows=[];unassigned={}
    for record in cap['records']:
        raw=record['raw']
        if raw.get(shift_field) is None:
            raise ValueError('Missing shift mapping prevents trustworthy scope membership.')
        if str(raw[shift_field]) != s['shift']:
            # A row whose recorded shift no verified assignment covers belongs to no draft. It is counted and
            # reported with every draft of the day, never silently dropped and never guessed into a shift.
            if recorded_shifts is not None and str(raw[shift_field]) not in recorded_shifts:
                unassigned[str(raw[shift_field])]=unassigned.get(str(raw[shift_field]),0)+1
            continue
        instant(raw.get(scan_field))
        rows.append(dict(id=record['id'], scan_at=raw[scan_field], source=deepcopy(record)))
    return dict(scope=s, rows=rows, captured_at=cap['captured_at'], terminal=True, unassigned_shifts=unassigned,
                not_before=not_before, source_capture=cap, photo_blobs={k:__import__('base64').b64encode(v).decode() for k,v in blobs.items()})
