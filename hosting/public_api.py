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
    ('POST', '/api/v1/reports', 'Upload a report: {"filename": "...xlsx", "content_base64": "..."} (up to 4 MB). Runs every check (SharePoint, usual day, AI when on) and returns the result.'),
    ('GET', '/api/v1/reports/{id}', 'One report: status, numbers, every issue, approval and send history.'),
    ('GET', '/api/v1/reports/{id}/fixes', 'Suggested cell changes (checks and Claude).'),
    ('POST', '/api/v1/reports/{id}/fixes', 'Make a corrected copy: {"accept": "all"} or {"ids": [...]} and/or {"changes": [{"sheet","cell","after","reason"}]}. Returns the new report (it is checked again). The original is never changed.'),
    ('POST', '/api/v1/reports/{id}/ai-fixes', 'Ask Claude to draft fixes for the open issues, then list them with GET .../fixes.'),
    ('POST', '/api/v1/reports/{id}/recheck', 'Run checks again: {"checks": ["sharepoint", "baseline", "ai"]} (default: sharepoint and baseline).'),
    ('GET', '/api/v1/reports/{id}/download', 'The spreadsheet file itself.'),
    ('POST', '/api/v1/reports/{id}/approve', 'Approve as final: {"approver": "Name", "note": "", "override": false}. {"withdraw": true} removes an approval that was not sent.'),
    ('GET', '/api/v1/reports/{id}/send', 'Preview the client email: Reply All recipients, subject, message, attachment.'),
    ('POST', '/api/v1/reports/{id}/send', 'Needs a key with "can send" and an approved report. {"mode": "draft"|"send"}; to, cc, subject, body are optional and default to the preview. A second real send needs {"again": true}.'),
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
        id=item['file_hash'], filename=item['filename'], version=item.get('version'), uploaded_at=item.get('received_at'),
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


def handle(h, method, route, send):
    """Serve one /api/v1 request. send(status, payload_or_bytes, content_type=None, headers=None)."""
    try:
        path = route.rstrip('/') or route
        if method == 'GET' and path == '/api/v1':
            return send(200, index())
        key = _key(h)
        query = dict(p.split('=', 1) for p in (h.path.split('?', 1)[1].split('&') if '?' in h.path else []) if '=' in p)
        if path == '/api/v1/reports':
            if method == 'GET':
                with h.store().session() as directory:
                    rows = [report_row(r) for r in SpreadsheetInbox(directory).list()['spreadsheets']]
                if query.get('status'):
                    rows = [r for r in rows if r['status'] == query['status']]
                if query.get('job'):
                    rows = [r for r in rows if (r['job'] or '').lower() == query['job'].replace('%20', ' ').lower()]
                return send(200, dict(reports=rows, count=len(rows)))
            if method == 'POST':
                data = _body(h, MAX_UPLOAD)
                name, content = data.get('filename'), data.get('content_base64')
                if not isinstance(name, str) or not isinstance(content, str):
                    raise ApiError(400, 'Send "filename" and "content_base64".')
                try:
                    raw = base64.b64decode(content, validate=True)
                except ValueError:
                    raise ApiError(400, '"content_base64" is not valid base64.') from None
                with h.store().session() as directory:
                    item = SpreadsheetInbox(directory).ingest(name, raw)
                if not item.get('idempotent'):
                    _after_change(h, item['file_hash'], ('sharepoint', 'baseline', 'ai'))
                return send(200 if item.get('idempotent') else 201, dict(report(_get(h, item['file_hash'])), already_uploaded=bool(item.get('idempotent'))))
        m = re.fullmatch(r'/api/v1/reports/' + ID + r'(?:/([a-z-]+))?', path)
        if not m:
            raise ApiError(404, 'Unknown endpoint. GET /api/v1 lists them.')
        file_hash, action = m.group(1), m.group(2)
        item = _get(h, file_hash)
        from hosting import delivery
        if action is None and method == 'GET':
            return send(200, report(item))
        if action == 'download' and method == 'GET':
            with h.store().session() as directory:
                filename, raw = SpreadsheetInbox(directory).download(file_hash)
            safe = re.sub(r'[^A-Za-z0-9 ._()-]', '_', filename)[:180] or 'report'
            kind = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' if safe.lower().endswith('.xlsx') else 'text/csv; charset=utf-8'
            return send(200, raw, kind, {'Content-Disposition': f'attachment; filename="{safe}"'})
        if action == 'fixes' and method == 'GET':
            with h.store().session() as directory:
                out = SpreadsheetInbox(directory).fix_proposals(file_hash)
            return send(200, dict(id=file_hash, fixes=out['proposals'], applied=out['applied'], ai=out['ai']))
        if action == 'fixes' and method == 'POST':
            from hosting.fixes import FixError
            data = _body(h, 200000)
            with h.store().session() as directory:
                props = SpreadsheetInbox(directory).fix_proposals(file_hash)['proposals']
            chosen = props if data.get('accept') == 'all' else [p for p in props if p['id'] in set(data.get('ids') or [])]
            extra = [c for c in data.get('changes') or [] if isinstance(c, dict)]
            for c in extra:
                c.setdefault('reason', 'Changed through the API'); c.setdefault('source', 'API'); c.setdefault('kind', 'auto')
            taken = {(c.get('sheet'), str(c.get('cell') or '').upper()) for c in extra}
            changes = [p for p in chosen if (p['sheet'], p['cell']) not in taken] + extra
            if not changes:
                raise ApiError(400, 'Nothing to apply. Send {"accept": "all"}, {"ids": [...]} or {"changes": [...]}.')
            try:
                with h.store().session() as directory:
                    fixed = SpreadsheetInbox(directory).apply_fixes(file_hash, changes)
            except FixError as exc:
                raise ApiError(409, str(exc)) from None
            _after_change(h, fixed['file_hash'])
            return send(201, dict(report(_get(h, fixed['file_hash'])), applied=len(changes)))
        if action == 'ai-fixes' and method == 'POST':
            from hosting.spreadsheet_inbox import make_ai_fixes
            with h.store().session() as directory:
                item, raw, issues = SpreadsheetInbox(directory).ai_fix_inputs(file_hash)
            record = make_ai_fixes(item, raw, issues)
            if record.get('status') in ('error', 'not_configured'):
                raise ApiError(409, record.get('message') or 'AI fixes failed.')
            if record.get('status') == 'done':
                with h.store().session() as directory:
                    SpreadsheetInbox(directory).record_ai_fixes(file_hash, record)
            return send(200, dict(status=record.get('status'), drafted=len(record.get('fixes') or []), left_for_a_person=record.get('skipped') or []))
        if action == 'recheck' and method == 'POST':
            data = _body(h, 10000) if int(h.headers.get('Content-Length') or 0) else {}
            checks = [c for c in data.get('checks') or ['sharepoint', 'baseline'] if c in ('sharepoint', 'baseline', 'ai')]
            ran = _after_change(h, file_hash, checks)
            return send(200, dict(report(_get(h, file_hash)), ran=ran))
        if action == 'approve' and method == 'POST':
            data = _body(h, 10000)
            approved = delivery.approve(h.store(), file_hash, data.get('approver'), data.get('note'), data.get('override'), data.get('withdraw'))
            return send(200, report(approved))
        if action == 'send' and method == 'GET':
            return send(200, delivery.info(h.store(), file_hash))
        if action == 'send' and method == 'POST':
            if not key['can_send']:
                raise ApiError(403, 'This API key cannot send email. Make a key with "Can send to clients" ticked.')
            data = _body(h, 100000)
            data['via'] = 'api:' + key['name']
            updated, record = delivery.send(h.store(), file_hash, data, defaults=delivery.info(h.store(), file_hash))
            return send(200, dict(record=record, report=report(updated)))
        raise ApiError(405 if action in (None, 'download', 'fixes', 'ai-fixes', 'recheck', 'approve', 'send') else 404,
                       'That method is not available here. GET /api/v1 lists the endpoints.')
    except ApiError as exc:
        return send(exc.status, {'error': str(exc)})
    except ValueError as exc:
        return send(409, {'error': str(exc)})
