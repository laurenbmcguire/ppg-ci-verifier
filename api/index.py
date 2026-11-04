"""Verifier review app. Local SQLite copy is loopback-only; Vercel runs only in protected preview mode (Postgres, Vercel Authentication required)."""
import base64
import json
import re
import os
from pathlib import Path
import sys
import traceback
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))
sys.path.insert(0, str(ROOT))
from model import Review, Conflict
from report_intake import ReportInbox, IntakeError
from hosting.spreadsheet_inbox import SpreadsheetInbox
from hosting.spreadsheet_demo import SpreadsheetDemo
from hosting.report_workflow import ReportWorkflow, parse as parse_workflow
from hosting.source_adapter import convert_private_envelope
from hosting.storage import StorageUnavailable
from hosting.sqlite_storage import local_environment, SQLiteWorkspace

FILES = {'/development-examples.js': 'development-examples.js', '/': 'index.html', '/ui.js': 'ui.js', '/report-ui.js': 'report-ui.js',
         '/style.css': 'style.css', '/spreadsheet-ui.js': 'spreadsheet-ui.js', '/workflow-ui.js':'workflow-ui.js', '/assets/ppg-client-intelligence.png': 'assets/ppg-client-intelligence.png'}
TYPES = {'html': 'text/html; charset=utf-8', 'js': 'text/javascript; charset=utf-8',
         'css': 'text/css; charset=utf-8', 'png': 'image/png'}
MAX_RESPONSE_BYTES = 3 * 1024 * 1024

def encode_json(data):
    body = json.dumps(data, allow_nan=False).encode()
    if len(body) > MAX_RESPONSE_BYTES:
        raise Conflict('Result exceeds this private pilot’s response limit. Split the input into smaller reports; nothing was saved.')
    return body

ACTIONS = {'edit', 'validate', 'approve', 'generate', 'request', 'timeout', 'leave', 'download'}


def hosted_preview():
    """True only on Vercel with the operator's protected-preview flag set (Vercel Authentication must be enabled)."""
    return (os.environ.get('VERCEL') == '1' and os.environ.get('VERCEL_ENV') == 'preview'
            and os.environ.get('CI_VERCEL_PROTECTED_PREVIEW') == '1')


def hosted_hosts():
    return {h.lower() for h in (os.environ.get('VERCEL_URL', ''), os.environ.get('VERCEL_BRANCH_URL', '')) if h}


_HOSTED_STORE = None


def hosted_store():
    global _HOSTED_STORE
    if _HOSTED_STORE is None:
        from hosting.postgres_storage import PostgresWorkspace  # psycopg is needed only on Vercel
        _HOSTED_STORE = PostgresWorkspace(os.environ.get('DATABASE_URL'))
    return _HOSTED_STORE


def sharepoint_configured():
    from hosting.sharepoint import configured
    return configured()


def allowed_host(host):
    if not host or any(c in host for c in '/\\@ \t\r\n'):
        return False
    if hosted_preview():
        return host.lower() in hosted_hosts()
    return (local_environment() and os.environ.get('CI_LOCAL_DEV') == '1'
            and urlsplit('http://' + host).hostname in ('127.0.0.1', 'localhost'))


def debug_log():
    """Operator-enabled diagnostics only (CI_DEBUG_LOG=1): exception type and message to stderr, never request bodies."""
    if os.environ.get('CI_DEBUG_LOG') == '1':
        exc_type, exc, tb = sys.exc_info()
        frame = traceback.extract_tb(tb)[-1] if tb else None
        where = f'{frame.filename.rsplit("/", 1)[-1]}:{frame.lineno}' if frame else '?'
        cause = exc.__cause__ if exc is not None else None
        tail = f' <- {type(cause).__name__}: {cause}' if cause is not None else ''
        print(f'[ci-verifier] {exc_type.__name__ if exc_type else "?"} at {where}: {exc}{tail}', file=sys.stderr, flush=True)


class handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # Never write report contents or secrets into platform logs.

    def send(self, status, data, content_type='application/json; charset=utf-8', headers=None):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'private, no-store, max-age=0')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Permissions-Policy', 'camera=(), microphone=(), geolocation=()')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def fail(self, status, message):
        return self.send(status, json.dumps({'error': message, 'fail_closed': True}).encode())

    def store(self):
        # Test injection exists on the test HTTPServer only, never from requests.
        store = getattr(self.server, 'workspace_store', None)
        if store is None and hosted_preview():
            store = hosted_store()
        if store is None:
            raise StorageUnavailable('Start this LOCAL SQLITE COPY with hosting/sqlite_server.py. No storage is configured.')
        return store

    def demo_enabled(self):
        return (os.environ.get('CI_DEVELOPMENT_DEMO') == '1' and local_environment()
                and os.environ.get('CI_LOCAL_DEV') == '1' and isinstance(self.store(), SQLiteWorkspace)
                and self.checked_route() is not None)

    def workflow(self, directory):
        return ReportWorkflow(directory, development_demo=self.demo_enabled())

    def checked_route(self):
        if hosted_preview():
            if not allowed_host(self.headers.get('Host')):
                return None
        elif (not allowed_host(self.headers.get('Host'))
            or self.server.server_address[0] not in ('127.0.0.1', '::1', 'localhost')
            or self.client_address[0] not in ('127.0.0.1', '::1')):
            return None
        route = urlsplit(self.path)
        if parse_qs(route.query).get('mode', ['synthetic']) != ['synthetic']:
            return None
        return route.path

    def sharepoint_check(self, file_hash):
        from hosting.sharepoint import fetch_export, SharePointNotConfigured, SharePointNotFound, SharePointError
        from hosting.source_compare import identify, SourceError
        with self.store().session() as directory:
            item = SpreadsheetInbox(directory).get(file_hash)
        job, day = identify(item.get('filename'), item)
        try:
            filename, content, meta = fetch_export(job, day)
        except SharePointNotConfigured as exc:
            status, message = 'not_configured', str(exc)
        except SharePointNotFound as exc:
            status, message = 'not_found', str(exc)
        except SharePointError as exc:
            status, message = 'error', str(exc)
        else:
            try:
                with self.store().session() as directory:
                    return SpreadsheetInbox(directory).compare_source(file_hash, filename, content,
                                                                      dict(meta, origin='sharepoint', source_label='SharePoint daily export'))
            except SourceError as exc:
                status, message = 'error', str(exc)
        with self.store().session() as directory:
            return SpreadsheetInbox(directory).record_lookup(file_hash, status, message, job, day)

    def do_GET(self):
        route = self.checked_route()
        if route is None:
            return self.fail(403, 'LOCAL SQLITE COPY is available only on this computer’s loopback address.')
        try:
            if route in ('/api/spreadsheets/demo/review','/api/spreadsheets/demo/download'):
                if not self.demo_enabled():return self.fail(403,'Synthetic workbook simulation is disabled.')
                file_hash=parse_qs(urlsplit(self.path).query).get('id',[''])[0]
                with self.store().session() as directory:
                    demo=SpreadsheetDemo(directory,enabled=True)
                    if route.endswith('/download'):
                        body,filename=demo.download(file_hash)
                        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
                        headers={'Content-Disposition':'attachment; filename="'+filename+'"'}
                    else:
                        body=encode_json(demo.get(file_hash));content_type='application/json; charset=utf-8';headers={}
                return self.send(200,body,content_type,headers)
            if route in FILES:
                name = FILES[route]
                return self.send(200, (ROOT / 'web' / name).read_bytes(), TYPES[name.rsplit('.', 1)[1]])
            if route == '/api/workflow/sample':
                return self.send(200, (ROOT / 'samples' / 'workflow-report.json').read_bytes())
            if route == '/api/report-sample':
                sample = parse_qs(urlsplit(self.path).query).get('name', [''])[0]
                name = {'match': 'matched-report.json', 'errors': 'mismatched-report.json'}.get(sample)
                if not name:
                    return self.fail(404, 'Unknown synthetic example.')
                return self.send(200, (ROOT / 'samples' / name).read_bytes())
            if route not in ('/api/state', '/api/reports', '/api/health', '/api/spreadsheets', '/api/spreadsheet', '/api/workflow', '/api/workflow/version'):
                return self.fail(404, 'Not found.')
            with self.store().session() as directory:
                review, inbox = Review(directory), ReportInbox(directory / 'report-intake')
                if route == '/api/state':
                    data = {'state': review.view(), 'token': 'same-origin', 'storage': getattr(self.store(), 'storage_label', 'unconfigured')}
                elif route == '/api/reports':
                    data = dict(inbox.state(), storage=getattr(self.store(), 'storage_label', 'unconfigured'))
                elif route == '/api/workflow':
                    cursor=parse_qs(urlsplit(self.path).query).get('before',[None])[0]
                    data=self.workflow(directory).list(before=int(cursor) if cursor is not None else None)
                    data['reviewer_identity_available']=False
                elif route == '/api/workflow/version':
                    manifest_hash=parse_qs(urlsplit(self.path).query).get('hash',[''])[0]
                    data=self.workflow(directory).get(manifest_hash)
                    data['reviewer_identity_available']=False
                elif route == '/api/spreadsheets':
                    data = SpreadsheetInbox(directory).list()
                elif route == '/api/spreadsheet':
                    file_hash = parse_qs(urlsplit(self.path).query).get('id', [''])[0]
                    if not re.fullmatch(r'[0-9a-f]{64}', file_hash):
                        raise Conflict('Valid spreadsheet version ID required.')
                    data = SpreadsheetInbox(directory).get(file_hash)
                else:
                    data = {'status': 'ready', 'storage': getattr(self.store(), 'storage_label', 'unconfigured'), 'live_connectors_enabled': False, 'sharepoint_configured': sharepoint_configured(), 'spreadsheet_scope': 'internal_checks_only', 'send_enabled': False, 'reviewer_identity_available': False, 'prototype': True, 'local_only': not hosted_preview(), 'development_demo': self.demo_enabled()}
                body = encode_json(data)
            return self.send(200, body)
        except (Conflict, IntakeError, ValueError) as exc:
            return self.fail(409, str(exc))
        except StorageUnavailable as exc:
            debug_log()
            return self.fail(503, str(exc))
        except Exception:
            debug_log()
            return self.fail(503, 'Durable storage is unavailable. No success was confirmed; reload before retrying.')

    def do_POST(self):
        route = self.checked_route()
        if route is None:
            return self.fail(403, 'LOCAL SQLITE COPY requires loopback access.')
        expected_origin = ('https://' if os.environ.get('VERCEL') == '1' else 'http://') + self.headers.get('Host', '')
        if (self.headers.get('Origin') != expected_origin
            or self.headers.get('Sec-Fetch-Site', 'same-origin') != 'same-origin'
            or self.headers.get('X-Review-Token') != 'same-origin'):
            return self.fail(403, 'Same-origin browser request required.')
        if self.headers.get('Content-Type', '').split(';', 1)[0].strip().lower() != 'application/json':
            return self.fail(415, 'JSON content type required.')
        if route not in ('/api/spreadsheets/demo/decisions', '/api/spreadsheets/demo/apply', '/api/reports/import', '/api/spreadsheets/import', '/api/spreadsheets/upload/part', '/api/spreadsheets/upload/finish', '/api/spreadsheets/sharepoint', '/api/spreadsheets/source-upload', '/api/spreadsheets/reconcile', '/api/workflow/import', '/api/workflow/batch', '/api/workflow/apply', '/api/workflow/approve', '/api/workflow/demo/decision', '/api/workflow/demo/decisions', '/api/workflow/demo/apply') and route.removeprefix('/api/') not in ACTIONS:
            return self.fail(404, 'Not found.')
        try:
            limit = 4 * 1024 * 1024 + 16384 if route in ('/api/spreadsheets/import', '/api/spreadsheets/upload/part', '/api/spreadsheets/source-upload') else (2 * 1024 * 1024 if route in ('/api/workflow/import','/api/workflow/batch','/api/spreadsheets/reconcile') else (1024 * 1024 if route == '/api/reports/import' else 100000))
            if not route.startswith('/api/workflow/') and route not in ('/api/spreadsheets/import','/api/spreadsheets/upload/part','/api/spreadsheets/upload/finish','/api/spreadsheets/sharepoint','/api/spreadsheets/source-upload','/api/spreadsheets/reconcile','/api/spreadsheets/demo/decisions','/api/spreadsheets/demo/apply','/api/reports/import'): limit=10000
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= limit:
                return self.fail(413, 'Request size is invalid.')
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise Conflict('Incomplete request body.')
            content_type, headers = 'application/json; charset=utf-8', {}
            if route == '/api/spreadsheets/sharepoint':
                payload = json.loads(raw)
                file_hash = payload.get('file_hash') if isinstance(payload, dict) else None
                if not isinstance(file_hash, str) or not re.fullmatch(r'[0-9a-f]{64}', file_hash):
                    raise Conflict('Valid spreadsheet version ID required.')
                return self.send(200, encode_json({'item': self.sharepoint_check(file_hash)}))
            with self.store().session() as directory:
                review, inbox = Review(directory), ReportInbox(directory / 'report-intake')
                if route in ('/api/spreadsheets/demo/decisions','/api/spreadsheets/demo/apply'):
                    if not self.demo_enabled():raise ValueError('Synthetic workbook simulation is disabled.')
                    payload=parse_workflow(raw);demo=SpreadsheetDemo(directory,enabled=True)
                    data={'review':demo.decide(payload)} if route.endswith('/decisions') else demo.apply(payload)
                    body=encode_json(data)
                elif route == '/api/workflow/batch':
                    payload=parse_workflow(raw)
                    batch=payload.get('manifests')
                    if not isinstance(batch,list): raise ValueError('Explicit manifests array required.')
                    converted=[]
                    for value in batch:
                        manifest=parse_workflow(value)
                        converted.append(convert_private_envelope(manifest) if manifest.get('schema_version')=='private-sharepoint-envelope/1' else manifest)
                    data=self.workflow(directory).ingest_batch(converted)
                    body=encode_json(data)
                elif route == '/api/workflow/import':
                    manifest=parse_workflow(raw)
                    if manifest.get('schema_version')=='private-sharepoint-envelope/1': manifest=convert_private_envelope(manifest)
                    data={'item':self.workflow(directory).ingest(manifest)}
                    body=encode_json(data)
                elif route in ('/api/workflow/demo/decision','/api/workflow/demo/decisions','/api/workflow/demo/apply'):
                    if not self.demo_enabled():raise ValueError('Development simulation is disabled.')
                    payload=parse_workflow(raw)
                    required={'manifest_hash','report_hash','source_hash'} | ({'proposal_id','decision'} if route.endswith('/decision') else {'proposal_ids','decision'} if route.endswith('/decisions') else {'proposal_ids','new_version'})
                    if set(payload)!=required:raise ValueError('Supply only exact simulation bindings and explicit proposal selection; identity fields are not accepted.')
                    workflow=self.workflow(directory)
                    if route.endswith('/decision'):
                        item=workflow.demo_decide(payload['manifest_hash'],payload['proposal_id'],payload['decision'],payload['report_hash'],payload['source_hash'])
                    elif route.endswith('/decisions'):
                        item=workflow.demo_decide_batch(payload['manifest_hash'],payload['proposal_ids'],payload['decision'],payload['report_hash'],payload['source_hash'])
                    else:
                        item=workflow.demo_apply(payload['manifest_hash'],payload['proposal_ids'],payload['new_version'],payload['report_hash'],payload['source_hash'])
                    body=encode_json({'item':item})
                elif route in ('/api/workflow/apply','/api/workflow/approve'):
                    payload=parse_workflow(raw)
                    workflow=self.workflow(directory)
                    if route.endswith('/apply'):
                        data={'item':workflow.apply(payload.get('manifest_hash'),payload.get('proposal_ids'),payload.get('new_version'))}
                    else:
                        # No request field can supply an authenticated identity.
                        data={'item':workflow.approve(payload.get('manifest_hash'),payload.get('reviewed'),identity=None)}
                    body=encode_json(data)
                elif route == '/api/spreadsheets/reconcile':
                    payload=parse_workflow(raw)
                    data={'item':SpreadsheetInbox(directory).reconcile(payload.get('file_hash'),payload.get('source'))}
                    body=encode_json(data)
                elif route == '/api/spreadsheets/upload/part':
                    # Chunked upload: no file-size limit; each part is at most one request body.
                    upload = json.loads(raw)
                    if not isinstance(upload, dict) or not isinstance(upload.get('upload_id'), str) or not isinstance(upload.get('content_base64'), str):
                        raise Conflict('Upload id, part index and base64 content are required.')
                    part = base64.b64decode(upload['content_base64'], validate=True)
                    data = {'part': SpreadsheetInbox(directory).store_part(upload['upload_id'], upload.get('index'), part)}
                    body = encode_json(data)
                elif route == '/api/spreadsheets/upload/finish':
                    upload = json.loads(raw)
                    if not isinstance(upload, dict) or not isinstance(upload.get('upload_id'), str) or not isinstance(upload.get('filename'), str):
                        raise Conflict('Upload id and filename are required.')
                    data = {'item': SpreadsheetInbox(directory).finish_upload(upload['upload_id'], upload['filename'], upload.get('part_count'), upload.get('sha256'))}
                    body = encode_json(data)
                elif route == '/api/spreadsheets/source-upload':
                    upload = json.loads(raw)
                    if not isinstance(upload, dict) or not all(isinstance(upload.get(k), str) for k in ('file_hash', 'filename', 'content_base64')):
                        raise Conflict('Report version, export filename and base64 content are required.')
                    if not re.fullmatch(r'[0-9a-f]{64}', upload['file_hash']):
                        raise Conflict('Valid spreadsheet version ID required.')
                    content = base64.b64decode(upload['content_base64'], validate=True)
                    data = {'item': SpreadsheetInbox(directory).compare_source(upload['file_hash'], upload['filename'], content,
                                                                                {'origin': 'uploaded', 'source_label': 'SharePoint export (uploaded)'})}
                    body = encode_json(data)
                elif route == '/api/spreadsheets/import':
                    upload = json.loads(raw)
                    if not isinstance(upload, dict) or not isinstance(upload.get('filename'), str) or not isinstance(upload.get('content_base64'), str):
                        raise Conflict('Spreadsheet filename and base64 content are required.')
                    content = base64.b64decode(upload['content_base64'], validate=True)
                    data = {'item': SpreadsheetInbox(directory).ingest(upload['filename'], content)}
                    body = encode_json(data)
                elif route == '/api/reports/import':
                    data = {'item': inbox.ingest(raw), 'state': inbox.state()}
                    body = encode_json(data)
                else:
                    payload = json.loads(raw)
                    if not isinstance(payload, dict) or type(payload.get('revision')) is not int:
                        raise Conflict('A valid revision is required.')
                    action = route.removeprefix('/api/')
                    if action == 'download':
                        body = review.download(payload['revision'])
                        content_type = 'text/csv; charset=utf-8'
                        headers = {'Content-Disposition': 'attachment; filename="UNAPPROVED_SYNTHETIC_private-copy.csv"'}
                    else:
                        review.act(action, payload['revision'], payload)
                        body = encode_json(review.view())
            if route in ('/api/spreadsheets/upload/finish', '/api/spreadsheets/import') and sharepoint_configured():
                try:
                    item = json.loads(body)['item']
                    if not item.get('idempotent') or not (item.get('sharepoint') or {}).get('status'):
                        body = encode_json({'item': self.sharepoint_check(item['file_hash'])})
                except Exception:
                    debug_log()  # the upload itself was saved; lookup can be retried from the UI
            return self.send(200, body, content_type, headers)
        except (Conflict, IntakeError, ValueError, TypeError, KeyError) as exc:
            return self.fail(409, str(exc))
        except StorageUnavailable as exc:
            debug_log()
            return self.fail(503, str(exc))
        except Exception:
            debug_log()
            return self.fail(503, 'Durable storage is unavailable. No success was confirmed; reload before retrying.')
