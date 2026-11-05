"""Claude connector for the verifier: a remote MCP server (Streamable HTTP, stateless, JSON replies).

Address: /mcp/<api key>. Claude's custom connectors cannot send extra headers, so the key is part of
the address; while the app is behind Vercel sign-in the address also carries Vercel's automation
bypass (?x-vercel-protection-bypass=...). The app builds the full link on its API page.
Every tool uses the same operations and rules as the screen and the HTTP API: sending email needs a
key made with "Can send to clients" and an approved report.
"""
import json

from hosting import public_api as ops
from hosting.public_api import ApiError
from hosting.spreadsheet_inbox import SpreadsheetInbox

VERSIONS = ('2025-06-18', '2025-03-26', '2024-11-05')
INSTRUCTIONS = (
    'PPG Verifier checks PPG end-of-shift reports (the Excel files sent to clients) against SharePoint, '
    'the job\'s usual day and AI review, suggests cell fixes, and sends the approved report to the client '
    'from data@ppgquality.com as a Reply All on the job\'s last report email. Start with list_reports or '
    'get_report. A corrected copy is a new report id; the original is never changed. Never approve or send '
    'unless the person asked you to in this conversation; prefer mode "draft" for sending unless they clearly '
    'said to send. Never put scan times, inspector names or internal check results in a client email.')

S = lambda **props: {'type': 'object', 'properties': props, 'additionalProperties': False}
ID = {'type': 'string', 'description': 'The 64-character report id from list_reports.'}
TOOLS = [
    dict(name='list_reports', title='List reports',
         description='Latest 100 end-of-shift reports with status (hold = must fix, check = items to look at, clear), job, day, approval and send state.',
         inputSchema=S(status={'type': 'string', 'enum': ['hold', 'check', 'clear']},
                       job={'type': 'string', 'description': 'Part of a job number or filename, e.g. HANDS or 2219.'}),
         annotations=dict(readOnlyHint=True)),
    dict(name='get_report', title='Get a report',
         description='One report: status, numbers (scans, inspected, rejected), every issue with sheet/cell, SharePoint and usual-day comparison, approval and send history, corrected copy.',
         inputSchema=dict(S(id=ID), required=['id']), annotations=dict(readOnlyHint=True)),
    dict(name='get_fixes', title='Suggested fixes',
         description='Suggested cell changes for a report (from the checks and from Claude), each with sheet, cell, before, after and reason.',
         inputSchema=dict(S(id=ID), required=['id']), annotations=dict(readOnlyHint=True)),
    dict(name='apply_fixes', title='Make a corrected copy',
         description='Writes a corrected copy with the chosen fixes and checks it again. Returns the NEW report (new id). The original file is never changed.',
         inputSchema=dict(S(id=ID, accept_all={'type': 'boolean', 'description': 'Apply every suggested fix.'},
                            ids={'type': 'array', 'items': {'type': 'string'}, 'description': 'Fix ids from get_fixes to apply.'},
                            changes={'type': 'array', 'description': 'Your own cell changes.', 'items': {
                                'type': 'object', 'required': ['sheet', 'cell', 'after'], 'properties': {
                                    'sheet': {'type': 'string'}, 'cell': {'type': 'string', 'description': 'e.g. G20'},
                                    'after': {'type': 'string'}, 'before': {'type': 'string', 'description': 'Current value, to be safe.'},
                                    'reason': {'type': 'string'}}}}), required=['id']),
         annotations=dict(destructiveHint=False, idempotentHint=False)),
    dict(name='draft_ai_fixes', title='Ask Claude in the app for fixes',
         description='Has the verifier\'s AI draft fixes for the open issues. Then call get_fixes to see them.',
         inputSchema=dict(S(id=ID), required=['id']), annotations=dict(destructiveHint=False)),
    dict(name='recheck_report', title='Check again',
         description='Runs the SharePoint comparison and/or usual-day comparison and/or AI review again.',
         inputSchema=dict(S(id=ID, checks={'type': 'array', 'items': {'type': 'string', 'enum': ['sharepoint', 'baseline', 'ai']}}), required=['id']),
         annotations=dict(destructiveHint=False, idempotentHint=True)),
    dict(name='approve_report', title='Approve as final',
         description='Records that a named person approved this exact version as the final report. Needed before sending. override approves even with must-fix issues open; withdraw removes an approval that was not sent.',
         inputSchema=dict(S(id=ID, approver={'type': 'string', 'description': 'Name of the person approving.'},
                            note={'type': 'string'}, override={'type': 'boolean'}, withdraw={'type': 'boolean'}), required=['id']),
         annotations=dict(destructiveHint=False)),
    dict(name='preview_client_email', title='Preview the client email',
         description='Shows the email that would go to the client: Reply All recipients from the job\'s last report email, Cc, subject, message, attachment name. Sends nothing.',
         inputSchema=dict(S(id=ID), required=['id']), annotations=dict(readOnlyHint=True, openWorldHint=True)),
    dict(name='send_to_client', title='Send the report to the client',
         description='Emails the approved report to the client from data@ppgquality.com. mode "draft" saves it in Drafts for a person to review and send; mode "send" sends it now. to, cc, subject and body default to preview_client_email. A second real send needs again=true. Only with the person\'s go-ahead in this conversation.',
         inputSchema=dict(S(id=ID, mode={'type': 'string', 'enum': ['draft', 'send']},
                            to={'type': 'array', 'items': {'type': 'string'}}, cc={'type': 'array', 'items': {'type': 'string'}},
                            subject={'type': 'string'}, body={'type': 'string'}, again={'type': 'boolean'}), required=['id', 'mode']),
         annotations=dict(destructiveHint=True, openWorldHint=True, idempotentHint=False)),
    dict(name='search', title='Search reports',
         description='Search end-of-shift reports by job number, client, filename, date (YYYY-MM-DD) or status (hold, check, clear, approved, sent). Returns ids to pass to fetch or get_report.',
         inputSchema=dict(S(query={'type': 'string'}), required=['query']), annotations=dict(readOnlyHint=True)),
    dict(name='fetch', title='Fetch a report',
         description='The full text of one report found with search: status, numbers, every issue, approval and send history.',
         inputSchema=dict(S(id=ID), required=['id']), annotations=dict(readOnlyHint=True)),
]


def _link(h, file_hash):
    return f'https://{h.headers.get("Host", "")}/#r={file_hash}'


def _search(h, query):
    words = [w for w in str(query or '').lower().split() if w]
    rows = ops.op_list(h)['reports']
    def text(r):
        return ' '.join(str(x or '') for x in (r['job'], r['filename'], r['day'], r['status'],
                                                 'approved' if r.get('approved_by') else '', 'sent' if r.get('sent_at') else '')).lower()
    hits = [r for r in rows if all(w in text(r) for w in words)] if words else rows
    return dict(results=[dict(id=r['id'], title=f'{r["job"] or r["filename"]}, {r["day"] or "no date"}: {ops.STATUS_LABEL.get(r["status"], r["status"])}',
                              url=_link(h, r['id'])) for r in hits[:25]])


def _fetch(h, file_hash):
    r = ops.op_get(h, file_hash)
    n = r['numbers']
    lines = [f'{r["job"] or r["filename"]}, report day {r["day"] or "unknown"} ({r["filename"]}, version {r["version"]}).',
             f'Status: {r["status_label"]}. Must fix {r["counts"].get("high", 0)}, to check {r["counts"].get("medium", 0) + r["counts"].get("low", 0)}.',
             f'Scans/rows {n["scans"]}, inspected {n["inspected"]}, {"failed audits" if n["rejected_unit"] == "rows" else "rejected"} {n["rejected"]}.',
             f'SharePoint: {r["sharepoint"].get("message") or r["sharepoint"].get("status")}', f'Usual day: {r["usual_day"].get("message") or r["usual_day"].get("status")}']
    if r['issues']:
        lines.append('Issues:')
        lines += [f'- [{i.get("severity")}] {i.get("where") or (str(i.get("sheet") or "") + " " + str(i.get("cell") or i.get("row") or "")).strip()}: {i.get("text")}' for i in r['issues'][:60]]
    if r['approval']:
        lines.append(f'Approved by {r["approval"]["by"]} at {r["approval"]["at"]}.')
    for x in r['sent']:
        lines.append(f'{"Sent" if x.get("sent") else "Draft saved"} {x.get("at")} to {", ".join(x.get("to") or [])}: "{x.get("subject")}".')
    if r['corrected_copy']:
        lines.append(f'A corrected copy exists: {r["corrected_copy"]["filename"]} (id {r["corrected_copy"]["id"]}).')
    return dict(id=r['id'], title=f'{r["job"] or r["filename"]}, {r["day"]}', text='\n'.join(lines), url=_link(h, r['id']),
                metadata=dict(status=r['status'], job=r['job'] or '', day=r['day'] or ''))


def _call(h, key, name, a):
    if name == 'search':
        return _search(h, a.get('query'))
    if name == 'fetch':
        return _fetch(h, a.get('id'))
    if name == 'list_reports':
        return ops.op_list(h, a.get('status'), a.get('job'))
    if name == 'get_report':
        return ops.op_get(h, a.get('id'))
    if name == 'get_fixes':
        return ops.op_fixes(h, a.get('id'))
    if name == 'apply_fixes':
        return ops.op_apply(h, a.get('id'), 'all' if a.get('accept_all') else None, a.get('ids'), a.get('changes'))
    if name == 'draft_ai_fixes':
        return ops.op_ai_fixes(h, a.get('id'))
    if name == 'recheck_report':
        return ops.op_recheck(h, a.get('id'), a.get('checks'))
    if name == 'approve_report':
        return ops.op_approve(h, a.get('id'), a.get('approver'), a.get('note'), a.get('override') is True, a.get('withdraw') is True)
    if name == 'preview_client_email':
        return ops.op_send_preview(h, a.get('id'))
    if name == 'send_to_client':
        data = {k: a[k] for k in ('mode', 'to', 'cc', 'subject', 'body', 'again') if k in a}
        return ops.op_send(h, dict(key, via='mcp'), a.get('id'), data)
    raise ApiError(404, f'Unknown tool "{name}".')


def _rpc(h, key, msg):
    """One JSON-RPC message → a reply dict, or None for notifications."""
    if not isinstance(msg, dict) or msg.get('jsonrpc') != '2.0' or not isinstance(msg.get('method'), str):
        return dict(jsonrpc='2.0', id=None, error=dict(code=-32600, message='Invalid request'))
    mid, method, params = msg.get('id'), msg['method'], msg.get('params') or {}
    if 'id' not in msg:
        return None  # notifications (initialized, cancelled) need no reply
    ok = lambda result: dict(jsonrpc='2.0', id=mid, result=result)
    if method == 'initialize':
        asked = params.get('protocolVersion')
        return ok(dict(protocolVersion=asked if asked in VERSIONS else VERSIONS[0],
                       capabilities=dict(tools=dict(listChanged=False)),
                       serverInfo=dict(name='ppg-verifier', title='PPG Verifier', version='1.0.0'),
                       instructions=INSTRUCTIONS))
    if method == 'ping':
        return ok({})
    if method == 'tools/list':
        return ok(dict(tools=TOOLS))
    if method == 'tools/call':
        name, args = params.get('name'), params.get('arguments') or {}
        if not isinstance(args, dict):
            args = {}
        try:
            result = _call(h, key, name, args)
            text = json.dumps(result, ensure_ascii=False, default=str)
            return ok(dict(content=[dict(type='text', text=text)], structuredContent=result if isinstance(result, dict) else {'result': result}, isError=False))
        except (ApiError, ValueError) as exc:
            return ok(dict(content=[dict(type='text', text=str(exc))], isError=True))
    if method in ('resources/list', 'prompts/list'):
        return ok({method.split('/')[0]: []})
    return dict(jsonrpc='2.0', id=mid, error=dict(code=-32601, message=f'Method not found: {method}'))


def handle(h, method, route, send):
    """Serve /mcp/<key>. send(status, payload_or_bytes_or_None, content_type=None, headers=None)."""
    token = route.split('/mcp/', 1)[1].strip('/') if '/mcp/' in route else ''
    with h.store().session() as directory:
        key = SpreadsheetInbox(directory).check_key(token) if token else None
    if not key:
        return send(401, {'error': 'This connector link is not valid or was revoked. Make a new one on the verifier\'s API page.'})
    if method == 'GET':
        return send(405, {'error': 'This connector answers POST requests (MCP Streamable HTTP).'}, None, {'Allow': 'POST'})
    if method == 'DELETE':
        return send(204, b'', 'text/plain')
    length = int(h.headers.get('Content-Length') or 0)
    if not 0 < length <= 6 * 1024 * 1024:
        return send(413, {'error': 'Request size is invalid.'})
    try:
        body = json.loads(h.rfile.read(length))
    except ValueError:
        return send(400, dict(jsonrpc='2.0', id=None, error=dict(code=-32700, message='Parse error')))
    if isinstance(body, list):
        replies = [r for r in (_rpc(h, key, m) for m in body) if r is not None]
        return send(200, replies) if replies else send(202, b'', 'text/plain')
    reply = _rpc(h, key, body)
    return send(200, reply) if reply is not None else send(202, b'', 'text/plain')
