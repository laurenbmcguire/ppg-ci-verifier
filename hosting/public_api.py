"""Verifier API v1: the review desk for other programs (Power Automate, scripts, other PPG apps).

Authentication: an API key made on the app's API page, sent as "Authorization: Bearer ppgv_..." or
"X-API-Key: ppgv_...". Keys can be revoked any time; only their SHA-256 fingerprint is stored.
Sending email to a client needs a key created with "can send" ticked, and the same approval the
review screen needs. Everything returns JSON except /download.
"""
import base64
import json
import re

from hosting.review_summary import summarize
from hosting.spreadsheet_inbox import SpreadsheetInbox

ID = r'([0-9a-f]{64})'
MAX_UPLOAD = 4 * 1024 * 1024 + 16384
ISSUE_FIELDS = ('key', 'severity', 'source', 'text', 'where', 'sheet', 'cell', 'row', 'why', 'check', 'verdict', 'verdict_reason')
STATUS_LABEL = {'hold': 'Hold: must be fixed', 'check': 'Check: items to look at', 'clear': 'Clear: nothing flagged'}

ENDPOINTS = [
    ('GET', '/api/v1', 'This list. No key needed.'),
    ('GET', '/api/v1/reports', 'Latest 100 reports with status. Optional ?status=hold|check|clear&job=...'),
    ('POST', '/api/v1/reports', 'Upload a report: {"filename": "...xlsx", "content_base64": "..."} (up to 4 MB in one request; bigger files go in parts, below). Runs every check (SharePoint, usual day, AI when on) and returns the result. Nothing in the report is changed.'),
    ('POST', '/api/v1/uploads', 'Start a large upload (any size): returns {"upload_id", "part_size"}.'),
    ('POST', '/api/v1/uploads/{upload_id}/parts/{n}', 'Send part n (0, 1, 2...) of the file: {"content_base64": "..."}, each part at most part_size bytes before base64.'),
    ('POST', '/api/v1/uploads/{upload_id}/finish', 'Put the parts together and check the report: {"filename": "...xlsx", "part_count": N, "sha256": "<hex SHA-256 of the whole file>"}. Same result as POST /api/v1/reports.'),
    ('GET', '/api/v1/vins?q=...', 'Which reports a VIN or identifier is in (full or partial, at least 4 characters).'),
    ('POST', '/api/v1/build', 'Build the legacy-layout provisional report from an exact verified SharePoint source: {"job": "SLAL-HYMOBAL-2221", "day": "YYYY-MM-DD", "source": {"site_id": "...", "list_id": "..."}}. Recorded shifts are retained; inactivity never proves completion. Selected Briggs-template generation is a separate worker stage. Nothing is sent.'),
    ('GET', '/api/v1/nexus?job=...&day=...', 'Read the exact-source day with recorded shifts, quantities and unresolved-field holds. Supply site_id and list_id when the job is ambiguous; no shift-completion inference.'),
    ('GET', '/api/v1/reports/{id}', 'One report: status, numbers, every issue, approval and send history.'),
    ('GET', '/api/v1/reports/{id}/fixes', 'Suggested cell changes (checks and Claude).'),
    ('POST', '/api/v1/reports/{id}/fixes', 'Make a reviewed corrected copy with expected_check_hash and {"ids": [...]} and/or {"changes": [{"sheet","cell","after","reason"}]}. Returns the new report (it is checked again). The original is never changed. Automatic accept-all is paused.'),
    ('POST', '/api/v1/reports/{id}/ai-fixes', 'Ask Claude to draft fixes for the open issues, then list them with GET .../fixes.'),
    ('POST', '/api/v1/reports/{id}/shift-fix', 'Row relocation is paused; requests fail without changing the workbook.'),
    ('POST', '/api/v1/reports/{id}/recheck', 'Run checks again: {"checks": ["sharepoint", "baseline", "ai"]} (default: sharepoint and baseline).'),
    ('GET', '/api/v1/reports/{id}/download', 'The spreadsheet file itself.'),
    ('POST', '/api/v1/reports/{id}/approve', 'Record internal approval for the exact reviewed version; this does not establish source verification or authorize delivery: {"approver": "Name", "note": "", "override": false}. {"withdraw": true} removes an approval that was not sent.'),
    ('GET', '/api/v1/reports/{id}/send', 'Local approval/download details. Delivery is paused; no mailbox is read.'),
    ('POST', '/api/v1/reports/{id}/send', 'Sending and draft creation are paused; all requests fail without provider calls.'),
]


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def index():
    return dict(name='PPG Verifier API', version='v1',
                auth='Send an API key from the app\'s API page as "Authorization: Bearer ppgv_..." (or X-API-Key).',
                endpoints=[dict(method=m, path=p, description=d) for m, p, d in ENDPOINTS])


def report_row(row):
    rv = row.get('review') or {}
    return dict(id=row['file_hash'], filename=row['filename'], version=row.get('version'), uploaded_at=row.get('received_at'),
                job=rv.get('job'), day=rv.get('day'), status=rv.get('status'), counts=rv.get('counts'),
                approved_by=rv.get('approved'), sent_at=rv.get('sent_at'), corrected_from=rv.get('fixed_from'))


def report(item):
    rv = summarize(item)
    applied = (item.get('fixes_applied') or [])[-1:] or [None]
    return dict(
        id=item['file_hash'], check_hash=item.get('check_hash'), run=rv['run'], filename=item['filename'], version=item.get('version'), uploaded_at=item.get('received_at'),
        job=rv['job'], day=rv['day'], status=rv['status'], status_label=STATUS_LABEL.get(rv['status']), counts=rv['counts'],
        numbers=dict(scans=rv.get('scans'), inspected=rv.get('inspected'), rejected=rv.get('rejected'), certified=rv.get('certified'),
                     rejected_unit=rv.get('rejected_unit')),
        issues=[{k: i[k] for k in ISSUE_FIELDS if i.get(k) not in (None, '')} for i in rv['issues']],
        dismissed_by_ai=len(rv.get('dismissed') or []),
        sharepoint=rv['sharepoint'], usual_day=rv['usual'], ai_review=rv['ai'],
        approval=item.get('approval'), sent=item.get('sent') or [],
        corrected_copy=applied[0] and dict(id=applied[0]['fixed_file_hash'], filename=applied[0]['fixed_filename']),
        corrected_from=item.get('fixed_from') and dict(id=item['fixed_from']['file_hash'], filename=item['fixed_from']['filename'],
                                                       changes=item['fixed_from'].get('changes')),
        auto_formatting=item.get('auto_formatting') and {k: item['auto_formatting'][k] for k in ('count', 'kinds', 'examples')},
        download=f'/api/v1/reports/{item["file_hash"]}/download')


def _key(h):
    auth = h.headers.get('Authorization') or ''
    token = auth[7:].strip() if auth.lower().startswith('bearer ') else (h.headers.get('X-API-Key') or '').strip()
    if not token:
        raise ApiError(401, 'Send an API key: "Authorization: Bearer ppgv_...". Make one on the app\'s API page.')
    with h.store().session() as directory:
        key = SpreadsheetInbox(directory).check_key(token)
    if not key:
        raise ApiError(401, 'That API key is not valid or was revoked.')
    return key


def _body(h, limit):
    if h.headers.get('Content-Type', '').split(';', 1)[0].strip().lower() != 'application/json':
        raise ApiError(415, 'Send JSON with "Content-Type: application/json".')
    length = int(h.headers.get('Content-Length') or 0)
    if not 0 < length <= limit:
        raise ApiError(413, f'The request body must be 1 byte to {limit // 1024} KB.')
    raw = h.rfile.read(length)
    try:
        data = json.loads(raw)
    except ValueError:
        raise ApiError(400, 'The body is not valid JSON.') from None
    if not isinstance(data, dict):
        raise ApiError(400, 'Send a JSON object.')
    return data


def _get(h, file_hash):
    with h.store().session() as directory:
        try:
            return SpreadsheetInbox(directory).get(file_hash)
        except ValueError:
            raise ApiError(404, 'No report with that id.') from None


def _after_change(h, file_hash, checks=('sharepoint', 'baseline')):
    from api.index import sharepoint_configured, ai_review_configured, debug_log
    steps = {'sharepoint': h.sharepoint_check if sharepoint_configured() else None, 'baseline': h.baseline_check,
             'ai': h.ai_review if ai_review_configured() else None}
    ran = []
    for name in checks:
        step = steps.get(name)
        if step:
            try:
                step(file_hash); ran.append(name)
            except Exception:
                debug_log()
    return ran


def _id(file_hash):
    if not isinstance(file_hash, str) or not re.fullmatch(ID, file_hash.strip().lower()):
        raise ApiError(400, 'A report id is the 64-character id from list_reports.')
    return file_hash.strip().lower()


# ---------------- operations shared by the HTTP API and the Claude connector (MCP)
def op_list(h, status=None, job=None):
    with h.store().session() as directory:
        rows = [report_row(r) for r in SpreadsheetInbox(directory).list()['spreadsheets']]
    if status:
        rows = [r for r in rows if r['status'] == status]
    if job:
        rows = [r for r in rows if job.lower() in (r['job'] or '').lower() or job.lower() in r['filename'].lower()]
    return dict(reports=rows, count=len(rows))


PART_SIZE = 3 * 1024 * 1024


def _b64(content):
    try:
        return base64.b64decode(content, validate=True)
    except (ValueError, TypeError):
        raise ApiError(400, '"content_base64" is not valid base64.') from None


def op_upload(h, name, content):
    if not isinstance(name, str) or not isinstance(content, str):
        raise ApiError(400, 'Send "filename" and "content_base64".')
    raw = _b64(content)
    if len(raw) > MAX_UPLOAD:
        raise ApiError(413, 'Up to 4 MB fits in one request. For bigger reports use POST /api/v1/uploads and send the file in parts (any size).')
    with h.store().session() as directory:
        try:
            item = SpreadsheetInbox(directory).ingest(name, raw)
        except ValueError as exc:
            raise ApiError(400, str(exc)) from None
    return _checked(h, item)


def _checked(h, item):
    """Run the checks on a new upload. Nothing in the report is changed: the person decides every fix."""
    import os
    if item.get('idempotent') and item.get('auto_formatted_to'):   # the same file again: point at its cleaned copy
        item = dict(_get(h, item['auto_formatted_to']), idempotent=True)
    if not item.get('idempotent'):
        _after_change(h, item['file_hash'], ('sharepoint', 'baseline'))
        if os.environ.get('PPG_AUTOFORMAT_ON_UPLOAD') == '1':
            from hosting import autoformat
            try:
                cleaned = autoformat.run(h, item['file_hash'])
            except Exception:
                cleaned = None
            if cleaned:
                item = cleaned
        _after_change(h, item['file_hash'], ('ai',))
    return bool(item.get('idempotent')), dict(report(_get(h, item['file_hash'])), already_uploaded=bool(item.get('idempotent')))


def op_upload_start():
    import secrets
    return dict(upload_id=secrets.token_hex(16), part_size=PART_SIZE)


def op_upload_part(h, upload_id, index, content):
    if not re.fullmatch(r'[0-9a-f]{32}', upload_id or ''):
        raise ApiError(400, 'Unknown upload_id. Start with POST /api/v1/uploads.')
    raw = _b64(content)
    if len(raw) > PART_SIZE:
        raise ApiError(413, f'Each part can be at most {PART_SIZE} bytes before base64.')
    with h.store().session() as directory:
        try:
            out = SpreadsheetInbox(directory).store_part(upload_id, index, raw)
        except ValueError as exc:
            raise ApiError(400, str(exc)) from None
    return dict(out, upload_id=upload_id)


def op_upload_finish(h, upload_id, name, part_count, sha256):
    if not re.fullmatch(r'[0-9a-f]{32}', upload_id or ''):
        raise ApiError(400, 'Unknown upload_id. Start with POST /api/v1/uploads.')
    with h.store().session() as directory:
        try:
            item = SpreadsheetInbox(directory).finish_upload(upload_id, name, part_count, sha256)
        except ValueError as exc:
            raise ApiError(400, str(exc)) from None
    return _checked(h, item)


def op_get(h, file_hash):
    return report(_get(h, _id(file_hash)))


def op_fixes(h, file_hash):
    file_hash = _id(file_hash); _get(h, file_hash)
    with h.store().session() as directory:
        out = SpreadsheetInbox(directory).fix_proposals(file_hash)
    return dict(id=file_hash, fixes=out['proposals'], applied=out['applied'], ai=out['ai'])


def op_apply(h, file_hash, accept=None, ids=None, changes=None, expected_check_hash=None):
    from hosting.fixes import FixError
    if not expected_check_hash:
        raise ApiError(409, 'Reload and review the corrections; the displayed check revision is required.')
    file_hash = _id(file_hash); _get(h, file_hash)
    with h.store().session() as directory:
        props = SpreadsheetInbox(directory).fix_proposals(file_hash)['proposals']
    everything = accept == 'all' or accept is True
    if everything:   # "accept all" is automatic: what a person kept as recorded or deferred is left out
        with h.store().session() as directory:
            inbox = SpreadsheetInbox(directory)
            held = inbox.held_cells(inbox.get(file_hash))
        props = [p for p in props if (p.get('sheet'), str(p.get('cell') or '').upper()) not in held]
    chosen = props if everything else [p for p in props if p['id'] in set(ids or [])]
    extra = [dict(c) for c in changes or [] if isinstance(c, dict)]
    for c in extra:
        c.setdefault('reason', 'Changed through the API'); c.setdefault('source', 'API'); c.setdefault('kind', 'auto')
        if c.get('kind') not in ('number', 'text', 'auto'):
            c['kind'] = 'auto'  # formulas are only written by the shift move
    taken = {(c.get('sheet'), str(c.get('cell') or '').upper()) for c in extra}
    todo = [p for p in chosen if (p['sheet'], p['cell']) not in taken] + extra
    if not todo:
        raise ApiError(400, 'Nothing to apply. Accept all, list fix ids, or send changes.')
    try:
        with h.store().session() as directory:
            fixed = SpreadsheetInbox(directory).apply_fixes(file_hash, todo, expected_check_hash=expected_check_hash, auto=everything)
    except FixError as exc:
        raise ApiError(409, str(exc)) from None
    _after_change(h, fixed['file_hash'])
    return dict(report(_get(h, fixed['file_hash'])), applied=len(todo))


def op_ai_fixes(h, file_hash, expected_check_hash=None):
    from hosting.spreadsheet_inbox import make_ai_fixes
    file_hash = _id(file_hash); _get(h, file_hash)
    with h.store().session() as directory:
        item, raw, issues = SpreadsheetInbox(directory).proposal_inputs(file_hash, expected_check_hash)
    record = make_ai_fixes(item, raw, issues)
    if record.get('status') in ('error', 'not_configured'):
        raise ApiError(409, record.get('message') or 'AI fixes failed.')
    if record.get('status') == 'done':
        with h.store().session() as directory:
            SpreadsheetInbox(directory).record_ai_fixes(file_hash, record, expected_check_hash=expected_check_hash)
    return dict(status=record.get('status'), drafted=len(record.get('fixes') or []), left_for_a_person=record.get('skipped') or [])


def op_recheck(h, file_hash, checks=None):
    file_hash = _id(file_hash); _get(h, file_hash)
    checks = [c for c in checks or ['sharepoint', 'baseline'] if c in ('sharepoint', 'baseline', 'ai')]
    ran = _after_change(h, file_hash, checks)
    return dict(report(_get(h, file_hash)), ran=ran)


def op_approve(h, file_hash, approver=None, note=None, override=False, withdraw=False, expected_check_hash=None, expected_run=None):
    from hosting import delivery
    file_hash = _id(file_hash); _get(h, file_hash)
    return report(delivery.approve(h.store(), file_hash, approver, note, override, withdraw, expected_check_hash=expected_check_hash, expected_run=expected_run))


def op_send_preview(h, file_hash):
    from hosting import delivery
    file_hash = _id(file_hash); _get(h, file_hash)
    return delivery.info(h.store(), file_hash)


def op_find_vin(h, q):
    with h.store().session() as directory:
        return SpreadsheetInbox(directory).find_vin(q)


def _job_day(job, day):
    from zoneinfo import ZoneInfo
    from datetime import datetime as _dt, date as _date
    job = ' '.join(str(job or '').split()).upper()
    if not job:
        raise ApiError(400, 'Which job? Send "job", e.g. SLAL-HYMOBAL-2221.')
    day = str(day or _dt.now(ZoneInfo('America/Chicago')).date().isoformat())
    try:
        _date.fromisoformat(day)
    except ValueError:
        raise ApiError(400, 'day must be YYYY-MM-DD.') from None
    return job, day


def _build_binding(inbox, job, source=None):
    """Select only a verified registry source; explicit source narrows, never grants."""
    from hosting import registry, sharepoint
    entries = [e for e in registry.entries(inbox.setting(registry.REGISTRY_KEY, []))
               if e.get('verified') and e.get('list_id') and registry._same_job(job,e.get('job'))]
    if source is not None and not isinstance(source,dict):
        raise ApiError(400,'source must identify a verified site/list object.')
    if source:
        if not source.get('list_id') or not (source.get('site_id') or source.get('site')):
            raise ApiError(400,'source requires list_id and site_id or site.')
        entries = [e for e in entries if e.get('list_id') == source['list_id'] and
                   (not source.get('site') or e.get('site') == source['site'])]
    choices = {(e.get('site_id') or e.get('site'),e['list_id']):e for e in entries}
    if len(choices) != 1 and not (source and source.get('site_id')):
        raise ApiError(409,'Select one verified client/site/list source before building. No source was guessed.')
    # Resolve registry site names when stable IDs are not saved. Every supplied
    # selector must agree; a matching name must never override a conflicting ID.
    resolved = {}
    try:
        for entry in choices.values():
            binding = sharepoint.assignment_identity(dict(entry,status='verified'))
            if source and source.get('site_id') and binding['site_id'] != source['site_id']:
                continue
            resolved[(binding['site_id'],binding['list_id'])] = binding
    except (sharepoint.SharePointError,sharepoint.SharePointNotConfigured) as exc:
        raise ApiError(409,str(exc)) from None
    if len(resolved) != 1:
        raise ApiError(409,'Select one verified client/site/list source with consistent identifiers. No source was guessed.')
    return next(iter(resolved.values()))


def op_build(h, job, day=None, force=False, header=None, source=None):
    """Build the job's report for the day from SharePoint, save it, and run the checks. Returns the report and how it was built."""
    from datetime import datetime as _dt, timezone as _tz
    from hosting import builder
    job, day = _job_day(job, day)
    with h.store().session() as directory:
        inbox = SpreadsheetInbox(directory)
        binding = _build_binding(inbox, job, source)
        schedule = inbox.shift_schedule(job)
        saved = inbox.setting(f'report_header|{binding["site_id"]}|{binding["list_id"]}', {}) or {}
        if isinstance(header, dict):
            saved = inbox.set_setting(f'report_header|{binding["site_id"]}|{binding["list_id"]}', {k: ' '.join(str(header.get(k) or '').split())[:120] for k in ('customer', 'worksite', 'program', 'prepared_by')})
        windows = inbox.setting('nexus_windows', None)
    # Header values are explicitly saved/user supplied; never guess a customer from job text.
    try:
        rows, info = builder.day_rows(job, day, schedule, **binding)        # network call outside any DB transaction
    except builder.BuildError as exc:
        raise ApiError(409, str(exc)) from None
    if not rows:
        raise ApiError(409, f'SharePoint has no scans for {job} on {day}.')
    ready = builder.readiness(info['last_scan'])
    raw = builder.build_xlsx(job, day, rows, schedule, saved)
    # 45 quiet minutes only means nobody scanned for a while: it never proves the shift is over, so the built report says so
    built = dict(info, built_at=_dt.now(_tz.utc).isoformat(), ready=ready, nexus=builder.nexus_split(rows, windows),
                 shift_end_confirmed=False,
                 completeness_note='Built from the scans in SharePoint at this time. A pause in scanning does not prove the shift is over; build again later if more scans may come.',
                 shifts={sh['name']: sum(1 for r in rows if r['shift_name'] == sh['name']) for sh in schedule})
    with h.store().session() as directory:
        inbox = SpreadsheetInbox(directory)
        item = inbox.ingest(builder.file_name(job, day), raw)
        inbox.set_setting(f'built|{item["file_hash"]}', built)
    _after_change(h, item['file_hash'])
    return dict(report=report(_get(h, item['file_hash'])), built={k: v for k, v in built.items() if k != 'columns'})


def op_nexus(h, job, day=None, source=None):
    from hosting import builder
    job, day = _job_day(job, day)
    with h.store().session() as directory:
        inbox = SpreadsheetInbox(directory)
        binding = _build_binding(inbox, job, source)
        schedule, windows = inbox.shift_schedule(job), inbox.setting('nexus_windows', None)
    try:
        rows, info = builder.day_rows(job, day, schedule, **binding)
    except builder.BuildError as exc:
        raise ApiError(409, str(exc)) from None
    return dict(job=job, day=day, last_scan=info['last_scan'], **builder.nexus_split(rows, windows))


def shift_fix(h, file_hash):
    """Corrected copy with every wrong-shift row on its shift's tab (or taken out). Returns (item, moved, removed)."""
    raise ApiError(409, 'Row removal and relocation are disabled until the complete affected-cell plan can be reviewed.')
    from hosting.fixes import relocate_rows, FixError
    with h.store().session() as directory:
        inbox = SpreadsheetInbox(directory)
        moves = inbox.shift_moves(file_hash)
        item = inbox.get(file_hash)
        _, data = inbox.original_bytes(file_hash)
    if not moves:
        raise ApiError(409, 'No rows are on the wrong shift by their scan times.')
    try:
        changes = relocate_rows(item['filename'], data, moves)
        with h.store().session() as directory:
            fixed = SpreadsheetInbox(directory).apply_fixes(file_hash, changes)
    except FixError as exc:
        raise ApiError(409, str(exc)) from None
    _after_change(h, fixed['file_hash'])
    return _get(h, fixed['file_hash']), sum(1 for m in moves if m[2]), sum(1 for m in moves if not m[2])


def remove_rows(h, file_hash, rows):
    """Corrected copy with the given report rows taken out (extra copies of duplicate rows). Rows below move up;
    totals and formulas are never touched. rows: [(sheet, row)]. Returns the new item."""
    raise ApiError(409, 'Row removal and relocation are disabled until the complete affected-cell plan can be reviewed.')
    from hosting.fixes import relocate_rows, FixError
    rows = [(str(s_), int(r)) for s_, r in rows]
    if not rows:
        raise ApiError(409, 'Pick at least one row to remove.')
    with h.store().session() as directory:
        inbox = SpreadsheetInbox(directory)
        item = inbox.get(file_hash)
        _, data = inbox.original_bytes(file_hash)
    try:
        changes = relocate_rows(item['filename'], data, [(s_, r, None) for s_, r in rows])
        for c in changes:
            c['reason'], c['source'] = 'Extra copy of a duplicate row taken out', 'Row taken out'
        with h.store().session() as directory:
            fixed = SpreadsheetInbox(directory).apply_fixes(file_hash, changes)
    except FixError as exc:
        raise ApiError(409, str(exc)) from None
    _after_change(h, fixed['file_hash'])
    return _get(h, fixed['file_hash'])


def op_shift_fix(h, file_hash):
    file_hash = _id(file_hash); _get(h, file_hash)
    fixed, moved, removed = shift_fix(h, file_hash)
    return dict(report(fixed), moved=moved, removed=removed)


def op_send(h, key, file_hash, data):
    from hosting import delivery
    if not key['can_send']:
        raise ApiError(403, 'This key cannot send email to clients. Make a key (or connector link) with "Can send to clients" ticked.')
    raise ApiError(409, 'Sending and draft creation are paused pending delivery safety checks. No email was sent. Use the approved download for manual delivery.')
    file_hash = _id(file_hash); _get(h, file_hash)
    data = dict(data, via=('connector:' if key.get('via') == 'mcp' else 'api:') + key['name'])
    updated, record = delivery.send(h.store(), file_hash, data, defaults=delivery.info(h.store(), file_hash))
    return dict(record=record, report=report(updated))


def handle(h, method, route, send):
    """Serve one /api/v1 request. send(status, payload_or_bytes, content_type=None, headers=None)."""
    try:
        path = route.rstrip('/') or route
        if method == 'GET' and path == '/api/v1':
            return send(200, index())
        key = _key(h)
        query = dict(p.split('=', 1) for p in (h.path.split('?', 1)[1].split('&') if '?' in h.path else []) if '=' in p)
        if path == '/api/v1/vins' and method == 'GET':
            from urllib.parse import unquote_plus
            return send(200, op_find_vin(h, unquote_plus(query.get('q', ''))))
        if path == '/api/v1/build' and method == 'POST':
            data = _body(h, 10000)
            return send(201, op_build(h, data.get('job'), data.get('day'), data.get('force') is True, data.get('header'), data.get('source')))
        if path == '/api/v1/nexus' and method == 'GET':
            from urllib.parse import unquote_plus
            return send(200, op_nexus(h, unquote_plus(query.get('job', '')), query.get('day'),
                {k: unquote_plus(query[k]) for k in ('site','site_id','list_id') if query.get(k)} or None))
        if path == '/api/v1/uploads' and method == 'POST':
            return send(201, op_upload_start())
        m = re.fullmatch(r'/api/v1/uploads/([0-9a-f]{32})/(?:parts/(\d{1,5})|(finish))', path)
        if m and method == 'POST':
            if m.group(2) is not None:
                data = _body(h, MAX_UPLOAD)
                return send(200, op_upload_part(h, m.group(1), int(m.group(2)), data.get('content_base64')))
            data = _body(h, 10000)
            again, out = op_upload_finish(h, m.group(1), data.get('filename'), data.get('part_count'), data.get('sha256'))
            return send(200 if again else 201, out)
        if path == '/api/v1/reports':
            if method == 'GET':
                return send(200, op_list(h, query.get('status'), query.get('job', '').replace('%20', ' ') or None))
            if method == 'POST':
                data = _body(h, MAX_UPLOAD)
                again, out = op_upload(h, data.get('filename'), data.get('content_base64'))
                return send(200 if again else 201, out)
        m = re.fullmatch(r'/api/v1/reports/' + ID + r'(?:/([a-z-]+))?', path)
        if not m:
            raise ApiError(404, 'Unknown endpoint. GET /api/v1 lists them.')
        file_hash, action = m.group(1), m.group(2)
        if action is None and method == 'GET':
            return send(200, op_get(h, file_hash))
        if action == 'download' and method == 'GET':
            _get(h, file_hash)
            with h.store().session() as directory:
                filename, raw = SpreadsheetInbox(directory).download(file_hash)
            safe = re.sub(r'[^A-Za-z0-9 ._()-]', '_', filename)[:180] or 'report'
            kind = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' if safe.lower().endswith('.xlsx') else 'text/csv; charset=utf-8'
            return send(200, raw, kind, {'Content-Disposition': f'attachment; filename="{safe}"'})
        if action == 'fixes' and method == 'GET':
            return send(200, op_fixes(h, file_hash))
        if action == 'fixes' and method == 'POST':
            data = _body(h, MAX_UPLOAD)
            return send(201, op_apply(h, file_hash, data.get('accept'), data.get('ids'), data.get('changes'), data.get('expected_check_hash')))
        if action == 'ai-fixes' and method == 'POST':
            data = _body(h, 10000)
            return send(200, op_ai_fixes(h, file_hash, data.get('expected_check_hash')))
        if action == 'shift-fix' and method == 'POST':
            return send(201, op_shift_fix(h, file_hash))
        if action == 'recheck' and method == 'POST':
            data = _body(h, 10000) if int(h.headers.get('Content-Length') or 0) else {}
            return send(200, op_recheck(h, file_hash, data.get('checks')))
        if action == 'approve' and method == 'POST':
            data = _body(h, 10000)
            return send(200, op_approve(h, file_hash, data.get('approver'), data.get('note'), data.get('override'), data.get('withdraw'), data.get('expected_check_hash'), data.get('expected_run')))
        if action == 'send' and method == 'GET':
            return send(200, op_send_preview(h, file_hash))
        if action == 'send' and method == 'POST':
            return send(200, op_send(h, key, file_hash, _body(h, 100000)))
        raise ApiError(405 if action in (None, 'download', 'fixes', 'ai-fixes', 'shift-fix', 'recheck', 'approve', 'send') else 404,
                       'That method is not available here. GET /api/v1 lists the endpoints.')
    except ApiError as exc:
        return send(exc.status, {'error': str(exc)})
    except ValueError as exc:
        return send(409, {'error': str(exc)})
