"""Verifier review app. Local SQLite copy is loopback-only; Vercel runs only in protected preview mode (Postgres, Vercel Authentication required)."""
import base64
from datetime import datetime, timezone
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
         '/style.css': 'style.css', '/spreadsheet-ui.js': 'spreadsheet-ui.js', '/workflow-ui.js':'workflow-ui.js', '/assets/ppg-client-intelligence.png': 'assets/ppg-client-intelligence.png',
         '/app.js': 'app.js', '/app.css': 'app.css',
         **{f'/assets/fonts/{n}': f'assets/fonts/{n}' for n in ('dm-sans-latin-400-normal.woff2', 'dm-sans-latin-500-normal.woff2', 'dm-sans-latin-600-normal.woff2',
                                                                'dm-sans-latin-700-normal.woff2', 'libre-caslon-text-latin-700-normal.woff2')}}
TYPES = {'html': 'text/html; charset=utf-8', 'js': 'text/javascript; charset=utf-8',
         'css': 'text/css; charset=utf-8', 'png': 'image/png', 'woff2': 'font/woff2'}
MAX_RESPONSE_BYTES = 3 * 1024 * 1024


def review_page_size():
    """How many findings go in one response; the rest are fetched page by page (counts always use all of them)."""
    try:
        return max(1, min(5000, int(os.environ.get('CI_REVIEW_PAGE', '2000'))))
    except ValueError:
        return 2000

def encode_json(data):
    body = json.dumps(data, allow_nan=False).encode()
    if len(body) > MAX_RESPONSE_BYTES:
        raise Conflict('Result exceeds this private pilot’s response limit. Split the input into smaller reports; nothing was saved.')
    return body

ACTIONS = {'edit', 'validate', 'approve', 'generate', 'request', 'timeout', 'leave', 'download'}


def hosted_preview():
    """True only on Vercel with the operator's protected flag set (Vercel Authentication must cover all deployments)."""
    return (os.environ.get('VERCEL') == '1' and os.environ.get('VERCEL_ENV') == 'preview'
            and os.environ.get('CI_VERCEL_PROTECTED_PREVIEW') == '1')


APP_HOST = 'ppg-ci-verifier-git-preview-lauren-mc-guire-llc.vercel.app'


def production_forward():
    """Production holds no data or credentials; its addresses forward to the protected preview app."""
    return os.environ.get('VERCEL') == '1' and os.environ.get('VERCEL_ENV') == 'production'


def hosted_hosts():
    """This deployment's own URL, its branch alias, the production URL, and operator-listed project domains."""
    names = [os.environ.get(k, '') for k in ('VERCEL_URL', 'VERCEL_BRANCH_URL')]
    names += os.environ.get('CI_ALLOWED_HOSTS', '').split(',')
    return {h.strip().lower() for h in names if h.strip()}


_HOSTED_STORE = None


def hosted_store():
    global _HOSTED_STORE
    if _HOSTED_STORE is None:
        from hosting.postgres_storage import PostgresWorkspace  # psycopg is needed only on Vercel
        _HOSTED_STORE = PostgresWorkspace(os.environ.get('DATABASE_URL'))
    return _HOSTED_STORE


def ai_review_configured():
    from hosting.ai_review import configured
    return configured()


def sharepoint_configured():
    from hosting.sharepoint import configured
    return configured()


def mail_configured():
    from hosting import mailer
    return mailer.configured()


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
        if 'Content-Security-Policy' not in (headers or {}):
            self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Permissions-Policy', 'camera=(), microphone=(), geolocation=()')
        headers = dict(headers or {})
        if 'Content-Security-Policy' not in headers:
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)
        # True means "a reply has been sent": gate() relies on it, so a refusal always stops the request
        return True

    def report_view(self, inbox, file_hash):
        """Everything the report screen shows, from one read: the same state for the header, the corrections panel,
        the sheet badges and the decision history."""
        from hosting.review_summary import summarize
        from hosting import sharepoint_jobs, registry
        data = inbox.get(file_hash)
        try:
            repeats = inbox.repeats_elsewhere(file_hash, index_old=False)   # never slow down opening a report
        except Exception:
            debug_log(); repeats = []
        rv = summarize(data)
        try:
            assignment = inbox.assignment_for(data, rv)
            assignment['history'] = registry.history(inbox, file_hash)
        except Exception:
            debug_log(); assignment = None
        try:
            holds = inbox.holds(data, rv)
        except Exception:
            debug_log(); holds = [dict(code='source_assignment_unavailable', hard=True, text='Current report holds could not be verified. Reload before proceeding.')]
        source = rv.get('sharepoint') or {}
        rec = data.get('source_reconciliation') or {}
        assigned = assignment or {}
        source['source_verified'] = bool(source.get('source_evidence_complete') and assigned.get('status') in registry.RESOLVED and not assigned.get('needs_person') and all(assigned.get(k) and assigned.get(k) == rec.get(k) for k in ('site_id','list_id')) and not any(str(h.get('code','')).startswith('source_') for h in holds))
        if source.get('status') == 'match' and not source['source_verified']:
            source['status'] = 'unverified'
            source['message'] = ' '.join(h['text'] for h in holds if str(h.get('code','')).startswith('source_')) or 'Current exact-source assignment is not verified.'
        rv['sharepoint'] = source
        # counts, holds and approval use every finding; the list itself is sent in pages so a very large report still opens
        page = review_page_size()
        rv['page'] = dict(size=page, issues_total=len(rv.get('issues') or []), dismissed_total=len(rv.get('dismissed') or []))
        rv['issues'], rv['dismissed'] = (rv.get('issues') or [])[:page], (rv.get('dismissed') or [])[:page]
        rv['page'].update(issues_returned=len(rv['issues']), dismissed_returned=len(rv['dismissed']))
        return dict(data, review=rv, other_days=repeats, built=inbox.setting(f'built|{file_hash}', None), assignment=assignment, holds=holds,
                    sp_job=sharepoint_jobs.lookup(rv.get('job'), inbox.setting(sharepoint_jobs.KEY)))

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

    # ---------------- who is asking, and which clients they can see
    def secure(self):
        return os.environ.get('VERCEL') == '1'

    def origin(self):
        return ('https://' if self.secure() else 'http://') + self.headers.get('Host', '')

    def current_user(self):
        from hosting import access
        cookie_header = self.headers.get('Cookie') or ''
        cached = getattr(self, '_user_cache', None)
        if cached and cached[0] == cookie_header:
            return cached[1]
        token = access.read_cookie(cookie_header, access.COOKIE)
        from hosting import pg_tables
        with self.store().session() as directory:
            if pg_tables.current.get() is None and not (directory / 'spreadsheet_inbox.sqlite3').exists():
                user, needed = None, False      # brand-new workspace: nothing to look up, and nothing gets written
            else:
                inbox = SpreadsheetInbox(directory)
                user = access.session_user(inbox, token) if token else None
                needed = hosted_preview() or access.required(inbox)
        if not user and not needed and local_environment() and os.environ.get('CI_LOCAL_DEV') == '1':
            user = dict(access.OWNER)       # explicit loopback development only; never infer hosted identity
        self._signin_required = hosted_preview() or needed
        self._user_cache = (cookie_header, user)
        return user

    def is_admin(self):
        u = self.current_user()
        return bool(u and u.get('role') == 'admin')

    def clients_map(self):
        if getattr(self, '_clients', None) is None:
            from hosting import sharepoint
            self._clients = []
            if sharepoint.configured():
                try:
                    self._clients = sharepoint.clients()
                except Exception:
                    debug_log()
        return self._clients

    def can_job(self, job):
        from hosting import access
        return self.is_admin() or access.can_see_job(self.current_user(), job, self.clients_map())

    def can_hash(self, file_hash):
        if self.is_admin():
            return True
        if not isinstance(file_hash, str) or not re.fullmatch(r'[0-9a-f]{64}', file_hash):
            return False
        from hosting import registry
        from hosting.review_summary import summarize
        try:
            with self.store().session() as directory:
                inbox = SpreadsheetInbox(directory)
                item = inbox.get(file_hash)
                a = inbox.assignment_for(item, summarize(item, with_issues=False))   # the one client/job resolver
        except ValueError:
            return False
        return self.can_report(a, (summarize(item, with_issues=False) or {}).get('job'))

    def can_report(self, assignment, job):
        """The one rule for who sees a report: its settled client; when several clients fit, admins only; when no
        client list matches at all, the job's client as before."""
        from hosting import registry
        if self.is_admin():
            return True
        a = assignment or {}
        if a.get('status') in registry.RESOLVED:
            return a.get('client_key') in set((self.current_user() or {}).get('clients') or [])
        if a.get('status') in ('ambiguous', 'conflict'):
            return False
        return self.can_job(job)

    def redirect(self, location, cookies=()):
        self.send_response(302)
        self.send_header('Location', location)
        self.send_header('Cache-Control', 'no-store')
        for c in cookies:
            self.send_header('Set-Cookie', c)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def auth_get(self, route):
        """Microsoft sign-in, its callback, and one-time sign-in links."""
        import secrets as _secrets
        from urllib.parse import quote
        from hosting import access
        q = parse_qs(urlsplit(self.path).query)
        callback = self.origin() + '/auth/microsoft/callback'
        session_cookie = lambda token: access.cookie(access.COOKIE, token, access.SESSION_DAYS * 86400, self.secure())
        oops = lambda msg: self.redirect('/app#signin=' + quote(msg), [access.cookie(access.STATE_COOKIE, '', 0, self.secure())])
        try:
            if route == '/auth/microsoft':
                if not access.microsoft_ready():
                    return oops('Microsoft sign-in is not set up yet.')
                state = _secrets.token_urlsafe(24)
                return self.redirect(access.microsoft_url(callback, state), [access.cookie(access.STATE_COOKIE, state, 600, self.secure())])
            if route == '/auth/microsoft/callback':
                if q.get('error'):
                    return oops((q.get('error_description') or ['Microsoft sign-in was cancelled.'])[0][:200])
                state = access.read_cookie(self.headers.get('Cookie'), access.STATE_COOKIE)
                if not state or q.get('state', [''])[0] != state:
                    return oops('The sign-in took too long or was opened in another browser. Try again.')
                email, name = access.microsoft_email(q.get('code', [''])[0], callback, state=state)
                with self.store().session() as directory:
                    inbox = SpreadsheetInbox(directory)
                    user = access.get_user(inbox, email=email)
                    if not user:
                        return oops(f'{email} has not been given access yet. Ask an admin to add you.')
                    if name and not user['name']:
                        access.save_user(inbox, dict(user, name=name))
                    token = access.start_session(inbox, user)
                return self.redirect('/app', [session_cookie(token), access.cookie(access.STATE_COOKIE, '', 0, self.secure())])
            if route == '/auth/link':
                with self.store().session() as directory:
                    inbox = SpreadsheetInbox(directory)
                    user = access.use_link(inbox, q.get('t', [''])[0])
                    token = access.start_session(inbox, user)
                return self.redirect('/app', [session_cookie(token)])
        except access.AccessError as exc:
            return oops(str(exc))
        return self.fail(404, 'Not found.')

    @staticmethod
    def shown(payload):
        """(seen, run) from a decision request: the check run the reviewer's screen showed and the fingerprint of each
        finding it names. None when the page did not send them (an out-of-date page): the caller answers 409."""
        run, seen = payload.get('run'), payload.get('seen', {})
        if not isinstance(run, dict) or not isinstance(seen, dict):
            return None
        return {str(k): str(v) for k, v in seen.items()}, run

    def gate(self, method, route, payload=None):
        """None when the request may go ahead; otherwise the reply that refuses it."""
        if route in ('/api/health', '/api/me') or route.startswith('/api/auth/'):
            return None
        user = self.current_user()
        if not user:
            return self.send(401, encode_json({'error': 'Sign in to continue.', 'signin': True}))
        if user.get('role') == 'admin':
            return None
        admin_only = (route.startswith(('/api/keys', '/api/users', '/api/access', '/api/workflow', '/api/spreadsheets/demo'))
                      or route in ('/api/settings', '/api/settings/shifts', '/api/settings/rules', '/api/state', '/api/reports', '/api/reports/import')
                      or route.startswith('/api/overview/artifact'))
        if admin_only:
            return self.fail(403, 'Only an admin can do that.')
        nope = lambda: self.fail(404, 'Not found, or not one of your clients.')
        if method == 'GET':
            q = parse_qs(urlsplit(self.path).query)
            if q.get('id') and not self.can_hash(q['id'][0]):
                return nope()
            if route == '/api/artifacts/view':
                from hosting import periods, overview as ov
                key = q.get('key', [''])[0]
                parsed = periods.parse_key(key)
                if ov.parse_key(key) or not parsed or not self.can_job(parsed[0]):
                    return nope()
            return None
        if isinstance(payload, dict):
            if payload.get('file_hash') and not self.can_hash(payload['file_hash']):
                return nope()
            for field in ('job', 'add'):
                if payload.get(field) and not self.can_job(str(payload[field])):
                    return self.fail(403, "You don't have access to that client.")
            if route.startswith('/api/periods/artifact') and payload.get('key'):
                from hosting import periods
                parsed = periods.parse_key(payload['key'])
                if not parsed or not self.can_job(parsed[0]):
                    return nope()
        return None

    def account_post(self, route, payload):
        """Sign-in, sign-out, and (admins) managing people and the sign-in requirement."""
        from hosting import access, mailer
        payload = payload if isinstance(payload, dict) else {}
        if route == '/api/auth/signout':
            token = access.read_cookie(self.headers.get('Cookie'), access.COOKIE)
            with self.store().session() as directory:
                access.end_session(SpreadsheetInbox(directory), token)
            return self.send(200, encode_json({'signed_out': True}), headers={'Set-Cookie': access.cookie(access.COOKIE, '', 0, self.secure())})
        if route == '/api/auth/email':
            if not mailer.configured():
                raise Conflict("Email sign-in isn't set up yet. Ask an admin for a sign-in link, or use Microsoft.")
            try:
                email = access._clean_email(payload.get('email'))
            except access.AccessError as exc:
                raise Conflict(str(exc))
            with self.store().session() as directory:
                inbox = SpreadsheetInbox(directory)
                user = access.get_user(inbox, email=email)
                token = access.sign_in_link(inbox, user, access.LINK_MINUTES) if user else None
            if token:
                try:
                    mailer.send_plain([email], 'Your PPG Verifier sign-in link',
                                      f'Sign in to the PPG Verifier:\n\n{self.origin()}/auth/link?t={token}\n\nThis link works once and expires in {access.LINK_MINUTES} minutes. '
                                      'If you did not ask for it, you can ignore this email.')
                except mailer.MailError:
                    debug_log()
            return self.send(200, encode_json({'sent': True, 'message': 'If that email has access, a sign-in link is on its way.'}))
        # admins only from here (the gate already refused everyone else)
        valid = {c['key'] for c in self.clients_map()}
        try:
            with self.store().session() as directory:
                inbox = SpreadsheetInbox(directory)
                if route == '/api/access/required':
                    if hosted_preview() and payload.get('on') is not True:
                        raise access.AccessError('Hosted sign-in cannot be disabled.')
                    access.set_required(inbox, payload.get('on') is True, self.current_user())
                elif route == '/api/users/save':
                    saved = access.save_user(inbox, payload, valid or None)
                    me = self.current_user()
                    if me and not me.get('virtual') and saved['id'] == me['id'] and saved['role'] != 'admin':
                        access.save_user(inbox, dict(saved, role='admin'))
                        raise access.AccessError("You can't remove your own admin rights.")
                elif route == '/api/users/remove':
                    access.remove_user(inbox, str(payload.get('id') or ''), self.current_user())
                elif route in ('/api/users/link', '/api/users/email'):
                    user = access.get_user(inbox, str(payload.get('id') or ''))
                    if not user:
                        raise access.AccessError('That person was removed. Reload the page.')
                    hours = access.INVITE_HOURS if route.endswith('/link') else access.LINK_MINUTES / 60
                    token = access.sign_in_link(inbox, user, int(hours * 60))
                    link = f'{self.origin()}/auth/link?t={token}'
                    if route.endswith('/link'):
                        return self.send(200, encode_json({'link': link, 'hours': access.INVITE_HOURS, 'email': user['email']}))
                    if not mailer.configured():
                        raise access.AccessError('Email is not set up yet; copy the sign-in link and send it yourself.')
                    mailer.send_plain([user['email']], 'You have access to the PPG Verifier',
                                      f'You have been given access to the PPG Verifier.\n\nSign in here:\n{link}\n\nThe link works once and expires in '
                                      f'{access.LINK_MINUTES} minutes. After that, sign in with your PPG Microsoft account or ask for a new link at {self.origin()}/app')
                    return self.send(200, encode_json({'sent': True, 'email': user['email']}))
                users = access.list_users(inbox)
                needed = hosted_preview() or access.required(inbox)
        except access.AccessError as exc:
            raise Conflict(str(exc))
        except mailer.MailError as exc:
            raise Conflict(str(exc))
        return self.send(200, encode_json({'users': users, 'required': needed, 'hosted_signin_required': hosted_preview(), 'microsoft': access.microsoft_ready(), 'email': mailer.configured(),
                                           'callback': self.origin() + '/auth/microsoft/callback', 'me': self.current_user()}))

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
        from hosting.sharepoint import fetch_export, assignment_identity, SharePointNotConfigured, SharePointNotFound, SharePointError
        from hosting.source_compare import identify, SourceError
        from hosting import registry, reconcile
        from hosting.review_summary import summarize
        with self.store().session() as directory:
            inbox = SpreadsheetInbox(directory)
            item = inbox.get(file_hash)
            job, day = identify(item.get('filename'), item)
            schedule = inbox.shift_schedule(job)
            try:
                rv = summarize(item, with_issues=False)
                a = inbox.assignment_for(item, rv)
            except Exception:
                a, rv = {}, {}
        try:
            binding = assignment_identity(a)
        except (SharePointError, SharePointNotConfigured) as exc:
            with self.store().session() as directory:
                return SpreadsheetInbox(directory).record_lookup(file_hash, 'error', str(exc), job, day)
        has_ids = any((rec.get('source') or {}).get('item') for rec in item.get('records') or [])
        if (rv.get('day') or day) and (has_ids or item.get('dataset_selection', {}).get('identified_records')):
            try:
                snap = reconcile.snapshot(a.get('site'), binding['list_id'], schedule=schedule, site_id=binding['site_id'])
                with self.store().session() as directory:
                    return SpreadsheetInbox(directory).source_reconcile(file_hash, snap, rv.get('day') or day, expected_check_hash=item.get('check_hash'), expected_assignment=a)
            except Exception as exc:
                debug_log()
                with self.store().session() as directory:
                    return SpreadsheetInbox(directory).record_lookup(file_hash, 'error', f'SharePoint source capture failed: {exc}', job, day)
        if not job or not day:   # say why instead of leaving "Not checked yet"
            with self.store().session() as directory:
                return SpreadsheetInbox(directory).record_lookup(file_hash, 'not_found',
                    'Could not tell this report\'s ' + ' and '.join(x for x, v in (('job number', job), ('date', day)) if not v) + ', so SharePoint was not checked. Use Check again after fixing the report title.', job, day)
        try:
            filename, content, meta = fetch_export(job, day, schedule=schedule, **binding)
        except SharePointNotConfigured as exc:
            status, message = 'not_configured', str(exc)
        except SharePointNotFound as exc:
            status, message = 'not_found', str(exc)
        except SharePointError as exc:
            status, message = 'error', str(exc)
        except Exception as exc:   # never leave the report silently "Not checked yet"
            debug_log()
            status, message = 'error', f'SharePoint check failed: {exc}'
        else:
            try:
                with self.store().session() as directory:
                    return SpreadsheetInbox(directory).compare_source(file_hash, filename, content,
                                                                      dict(meta, origin='sharepoint', source_label='SharePoint daily export'))
            except SourceError as exc:
                status, message = 'error', str(exc)
        with self.store().session() as directory:
            return SpreadsheetInbox(directory).record_lookup(file_hash, status, message, job, day)

    def baseline_check(self, file_hash):
        """Compare the report with the job's usual day. Earlier days come from SharePoint (the job's "Data #" list,
        then the daily exports); saved reports only fill days SharePoint doesn't have."""
        from hosting.sharepoint import assignment_identity, SharePointError, SharePointNotConfigured
        from hosting.review_summary import summarize
        from hosting.baseline import compare_to_usual, stats_from_result, stats_from_rows, MAX_DAYS
        from hosting.source_compare import identify, parse_export, SourceError
        with self.store().session() as directory:
            inbox = SpreadsheetInbox(directory)
            item = inbox.get(file_hash)
            job, day = identify(item.get('filename'), item)
            assignment = inbox.assignment_for(item, summarize(item, with_issues=False))
            # Legacy history_for is keyed by job only, so it cannot safely fill gaps
            # when the same job label exists at another site. Keep it out until bound.
            saved = []
            schedule = inbox.shift_schedule(job)
        history, sp_note, coverage_note = [], None, None
        try:
            binding = assignment_identity(assignment)
        except (SharePointError, SharePointNotConfigured) as exc:
            binding = None
            coverage_note = str(exc)
        if job and day and binding and sharepoint_configured():
            from hosting.sharepoint import list_history, recent_exports, _list_items, SharePointError
            def add(entries, label):
                have = {h['day'] for h in history}
                for d, name, raw in entries:
                    if d in have:
                        continue
                    try:
                        history.append(dict(stats_from_rows(parse_export(name, raw)), day=d, source=label))
                        have.add(d)
                    except SourceError:
                        continue
            try:
                found = _list_items(job, **binding)
                if found:
                    sp_note = found[1].get('displayName')
                    add(list_history(job, day, limit=MAX_DAYS, schedule=schedule, **binding), f'SharePoint list ({sp_note})')

            except (SharePointError, SharePointNotConfigured) as exc:
                coverage_note = str(exc)
                debug_log()
        have = {h['day'] for h in history}
        history += [h for h in saved if h['day'] not in have]
        recent = sorted((h for h in history if h.get('day') and h['day'] != day), key=lambda h: h['day'], reverse=True)[:MAX_DAYS]
        sp_used = sum(1 for h in recent if str(h.get('source', '')).startswith('SharePoint'))
        label = (f'the SharePoint list "{sp_note}"' if sp_note else 'SharePoint') if sp_used == len(recent) and recent else None
        baseline = compare_to_usual(stats_from_result(item), history, job, day, source_label=label)
        sp_days = sum(1 for h in (baseline.get('history') or []) if str(h.get('source', '')).startswith('SharePoint'))
        baseline['history_source'] = (f'SharePoint list "{sp_note}"' if sp_note else 'SharePoint') if sp_days else 'unavailable'
        baseline['sharepoint_days'] = sp_days
        baseline['source_identity'] = binding
        baseline['coverage_note'] = coverage_note
        baseline['saved_history_excluded'] = 'Legacy saved history is job-keyed, not verified site/list-bound.'
        with self.store().session() as directory:
            return SpreadsheetInbox(directory).record_baseline(file_hash, baseline)

    def ai_review(self, file_hash):
        from hosting.ai_review import review, AIReviewNotConfigured, AIReviewError
        with self.store().session() as directory:
            inbox = SpreadsheetInbox(directory)
            item = inbox.get(file_hash)
            _, raw = inbox.original_bytes(file_hash)
            from hosting.source_compare import identify
            rules = inbox.rules_for(identify(item.get('filename'), item)[0])
        try:
            outcome = review(item, raw, rules=rules)  # network call outside any DB transaction
        except AIReviewNotConfigured as exc:
            outcome = dict(status='not_configured', message=str(exc))
        except AIReviewError as exc:
            outcome = dict(status='error', message=str(exc))
        with self.store().session() as directory:
            return SpreadsheetInbox(directory).record_ai_review(file_hash, outcome)

    def forward_to_app(self):
        target = 'https://' + (os.environ.get('CI_APP_HOST') or APP_HOST) + (self.path if self.path.startswith('/') else '/')
        self.send_response(307)
        self.send_header('Location', target)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', '0')
        self.end_headers()

    def do_DELETE(self):
        route = self.checked_route()
        if route is not None and route.startswith('/mcp/'):
            return self.public_api('DELETE', route, mcp=True)
        return self.fail(405, 'Method not allowed.')

    def public_api(self, method, route, mcp=False):
        from hosting import public_api, mcp_server
        def reply(status, data, content_type=None, headers=None):
            if not isinstance(data, (bytes, bytearray)):
                data, content_type = encode_json(data), content_type or 'application/json; charset=utf-8'
            return self.send(status, data, content_type, headers)
        try:
            return (mcp_server if mcp else public_api).handle(self, method, route, reply)
        except StorageUnavailable:
            debug_log()
            return reply(503, {'error': 'The report store is not available right now. Try again in a minute.'})
        except Exception:
            debug_log()
            return reply(500, {'error': 'Something went wrong on the server. Nothing was sent.'})

    def do_GET(self):
        if production_forward():
            return self.forward_to_app()
        route = self.checked_route()
        if route is not None and route.startswith('/api/v1'):
            return self.public_api('GET', route)
        if route is not None and route.startswith('/mcp/'):
            return self.public_api('GET', route, mcp=True)
        if route is None:
            return self.fail(403, f'This address is not one of the verifier’s approved addresses. Open https://{APP_HOST} instead.' if hosted_preview() else 'LOCAL SQLITE COPY is available only on this computer’s loopback address.')
        try:
            if route.startswith('/auth/'):
                return self.auth_get(route)
            if route.startswith('/api/'):
                refused = self.gate('GET', route)
                if refused:
                    return refused
            if route == '/api/me':
                from hosting import access, mailer
                user = self.current_user()
                return self.send(200, encode_json({'user': user, 'required': self._signin_required, 'hosted_signin_required': hosted_preview(), 'microsoft': access.microsoft_ready(), 'email': mailer.configured()}))
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
            if route == '/api/artifacts/view':
                from hosting import periods
                q = parse_qs(urlsplit(self.path).query)
                art = q.get('key', [''])[0]
                from hosting import overview as ov
                parsed, over = periods.parse_key(art), ov.parse_key(art)
                if not parsed and not over:
                    raise Conflict('Unknown dashboard or slide show.')
                with self.store().session() as directory:
                    dash = SpreadsheetInbox(directory).dashboard(art)
                if not dash:
                    return self.fail(404, 'Not created yet.')
                headers = {'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src data:; font-src data:; "
                                                      "base-uri 'none'; form-action 'none'; frame-ancestors 'self'; sandbox allow-scripts allow-popups allow-modals"}
                if q.get('download') == ['1'] and over:
                    headers['Content-Disposition'] = f'attachment; filename="PPG Quality Overview {"Slides" if over[2] == "slides" else "Dashboard"} {over[1].isoformat()}.html"'
                elif q.get('download') == ['1']:
                    job, kind, start, what = parsed
                    headers['Content-Disposition'] = f'attachment; filename="{re.sub(r"[^A-Za-z0-9 ._()-]", "_", job)} {"Weekly" if kind == "week" else "Monthly"} {"Slides" if what == "slides" else "Dashboard"} {start.isoformat()}.html"'
                return self.send(200, dash['html'].encode('utf-8'), 'text/html; charset=utf-8', headers)
            if route == '/api/spreadsheets/dashboard/view':
                q = parse_qs(urlsplit(self.path).query)
                file_hash = q.get('id', [''])[0]
                if not re.fullmatch(r'[0-9a-f]{64}', file_hash):
                    raise Conflict('Valid spreadsheet version ID required.')
                with self.store().session() as directory:
                    inbox = SpreadsheetInbox(directory)
                    dash = inbox.dashboard(file_hash)
                    item = inbox.get(file_hash) if dash else None
                if not dash:
                    return self.fail(404, 'No dashboard for this report yet.')
                # the page runs in its own sandbox: no network, no access to the app, framed only by the app
                headers = {'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src data:; font-src data:; "
                                                      "base-uri 'none'; form-action 'none'; frame-ancestors 'self'; sandbox allow-scripts allow-popups allow-modals"}
                if q.get('download') == ['1']:
                    from hosting.dashboard import file_name
                    headers['Content-Disposition'] = f'attachment; filename="{file_name(item)}"'
                return self.send(200, dash['html'].encode('utf-8'), 'text/html; charset=utf-8', headers)
            if route == '/api/change-summary':
                rid = parse_qs(urlsplit(self.path).query).get('id', [''])[0]
                if not re.fullmatch(r'[0-9a-f]{20}', rid):
                    raise Conflict('Valid change summary ID required.')
                from hosting import change_summary
                rec, pdf = change_summary.pdf_for(self, rid)
                if not self.can_job(rec.get('job')):
                    return self.fail(404, 'Change summary not found.')
                safe = re.sub(r'[^A-Za-z0-9 ._()-]', '_', rec.get('filename') or 'Change summary.pdf')[:180]
                disp = 'inline' if parse_qs(urlsplit(self.path).query).get('view') == ['1'] else 'attachment'
                return self.send(200, pdf, 'application/pdf', {'Content-Disposition': f'{disp}; filename="{safe}"'})
            if route == '/api/settings/sharepoint-cert':   # admins: the public certificate to upload in Entra ID (the private key never leaves the server)
                if not self.is_admin():
                    return self.fail(403, 'Only admins can get the SharePoint certificate.')
                from hosting import photo_embed
                with self.store().session() as directory:
                    inbox = SpreadsheetInbox(directory)
                    stored = inbox.setting(photo_embed.CERT_KEY, None)
                    if not stored or not photo_embed.use_stored_certificate(stored):
                        stored, _ = photo_embed.make_certificate()
                        inbox.set_setting(photo_embed.CERT_KEY, stored)
                return self.send(200, stored['cert_pem'].encode(), 'application/x-x509-ca-cert',
                                 {'Content-Disposition': 'attachment; filename="PPG Verifier SharePoint photos.cer"',
                                  'X-Cert-Thumbprint': stored['thumbprint'], 'X-Cert-Expires': stored['expires']})
            if route == '/api/spreadsheets/download-photos':   # a copy with each record's SharePoint photos; the original is unchanged
                file_hash = parse_qs(urlsplit(self.path).query).get('id', [''])[0]
                if not re.fullmatch(r'[0-9a-f]{64}', file_hash):
                    raise Conflict('Valid spreadsheet version ID required.')
                if not self.can_hash(file_hash):
                    return self.fail(404, 'Report not found.')
                from hosting import photo_embed, registry, sharepoint
                from hosting.review_summary import summarize
                if not sharepoint.configured():
                    raise Conflict('SharePoint is not connected, so the photos cannot be fetched.')
                with self.store().session() as directory:
                    inbox = SpreadsheetInbox(directory)
                    found = inbox.get(file_hash)
                    a = inbox.assignment_for(found, summarize(found, with_issues=False))
                    filename, raw = inbox.download(file_hash)
                if not filename.lower().endswith('.xlsx'):
                    raise Conflict('Photos can only be added to Excel (.xlsx) reports.')
                if (a['status'] not in registry.RESOLVED or a.get('needs_person')
                        or not a.get('site_id') or not a.get('list_id')):
                    raise Conflict('Choose the client and exact source first, so the photos come from the right SharePoint list.')
                with self.store().session() as directory:
                    photo_embed.use_stored_certificate(SpreadsheetInbox(directory).setting(photo_embed.CERT_KEY, None))
                try:
                    binding = sharepoint.assignment_identity(a)
                    exact = sharepoint._find_list(None, **binding)
                    if not exact or exact[1] != binding['site_id'] or exact[2].get('id') != binding['list_id']:
                        raise photo_embed.PhotoError('The exact assigned SharePoint list could not be verified.')
                    name, data, info = photo_embed.build(filename, raw, exact[0], binding['list_id'],
                                                        site_id=binding['site_id'], list_url=exact[2].get('webUrl'))
                except (photo_embed.PhotoError, sharepoint.SharePointError, sharepoint.SharePointNotFound,
                        sharepoint.SharePointNotConfigured) as exc:
                    raise Conflict(str(exc))
                safe = re.sub(r'[^A-Za-z0-9 ._()-]', '_', name)[:180]
                return self.send(200, data, 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                                 {'Content-Disposition': f'attachment; filename="{safe}"', 'X-Photos-Embedded': str(info['embedded']),
                                  'X-Photos-Missing': str(info['missing']), 'X-Photos-Records': str(info['with_photos']), 'X-Photos-Tab': info['tab']})
            if route == '/api/spreadsheets/download':
                file_hash = parse_qs(urlsplit(self.path).query).get('id', [''])[0]
                if not re.fullmatch(r'[0-9a-f]{64}', file_hash):
                    raise Conflict('Valid spreadsheet version ID required.')
                with self.store().session() as directory:
                    filename, raw = SpreadsheetInbox(directory).download(file_hash)
                safe = re.sub(r'[^A-Za-z0-9 ._()-]', '_', filename)[:180] or 'report'
                kind = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' if safe.lower().endswith('.xlsx') else 'text/csv; charset=utf-8'
                return self.send(200, raw, kind, {'Content-Disposition': f'attachment; filename="{safe}"'})
            if route == '/api/spreadsheets/media':   # one embedded picture's original bytes, for the sheet view
                q = parse_qs(urlsplit(self.path).query)
                file_hash, media = q.get('id', [''])[0], q.get('media', [''])[0]
                if not re.fullmatch(r'[0-9a-f]{64}', file_hash):
                    raise Conflict('Valid spreadsheet version ID required.')
                if not self.can_hash(file_hash):
                    return self.fail(404, 'Report not found.')
                from hosting import photos
                with self.store().session() as directory:
                    _, raw = SpreadsheetInbox(directory).download(file_hash)
                data = photos.media_bytes(raw, media) if raw[:2] == b'PK' else None
                if data is None:
                    return self.fail(404, 'That picture is not in this report.')
                kind = {'png': 'image/png', 'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'gif': 'image/gif', 'bmp': 'image/bmp', 'webp': 'image/webp'}.get(media.rsplit('.', 1)[-1].lower(), 'application/octet-stream')
                return self.send(200, data, kind, {'Cache-Control': 'private, max-age=3600', 'X-Content-Type-Options': 'nosniff', 'Content-Disposition': 'inline'})
            if route == '/' and hosted_preview() or route == '/app':
                return self.send(200, (ROOT / 'web' / 'app.html').read_bytes(), TYPES['html'])
            if route == '/classic':
                route = '/'
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
                    if not self.is_admin():
                        from hosting import registry
                        mine = set((self.current_user() or {}).get('clients') or [])
                        data['spreadsheets'] = [r for r in data['spreadsheets'] if self.can_report(r.get('assignment'), (r.get('review') or {}).get('job'))]
                        data['count'] = len(data['spreadsheets'])
                elif route == '/api/spreadsheet':
                    file_hash = parse_qs(urlsplit(self.path).query).get('id', [''])[0]
                    if not re.fullmatch(r'[0-9a-f]{64}', file_hash):
                        raise Conflict('Valid spreadsheet version ID required.')
                    data = self.report_view(SpreadsheetInbox(directory), file_hash)
                else:
                    data = {'status': 'ready', 'storage': getattr(self.store(), 'storage_label', 'unconfigured'), 'live_connectors_enabled': False, 'sharepoint_configured': sharepoint_configured(), 'ai_review_configured': ai_review_configured(), 'spreadsheet_scope': 'internal_checks_only', 'send_enabled': mail_configured(), 'reviewer_identity_available': False, 'prototype': True, 'local_only': not hosted_preview(), 'development_demo': self.demo_enabled()}
                body = encode_json(data)
            return self.send(200, body)
        except (Conflict, IntakeError, ValueError) as exc:
            return self.fail(409, str(exc))
        except StorageUnavailable as exc:
            debug_log()
            return self.fail(503, str(exc))
        except Exception:
            debug_log()
            return self.fail(503, 'Something went wrong on the server, so nothing was saved. Reload and try again; if it keeps happening, tell your admin.')

    def do_POST(self):
        route = self.checked_route()
        if route is None:
            return self.fail(403, 'LOCAL SQLITE COPY requires loopback access.')
        if route.startswith('/api/v1'):
            return self.public_api('POST', route)
        if route.startswith('/mcp/'):
            return self.public_api('POST', route, mcp=True)
        expected_origin = ('https://' if os.environ.get('VERCEL') == '1' else 'http://') + self.headers.get('Host', '')
        if (self.headers.get('Origin') != expected_origin
            or self.headers.get('Sec-Fetch-Site', 'same-origin') != 'same-origin'
            or self.headers.get('X-Review-Token') != 'same-origin'):
            return self.fail(403, 'Same-origin browser request required.')
        if self.headers.get('Content-Type', '').split(';', 1)[0].strip().lower() != 'application/json':
            return self.fail(415, 'JSON content type required.')
        if route not in ('/api/spreadsheets/demo/decisions', '/api/spreadsheets/demo/apply', '/api/reports/import', '/api/spreadsheets/import', '/api/spreadsheets/upload/part', '/api/spreadsheets/upload/finish', '/api/spreadsheets/sharepoint', '/api/spreadsheets/ai-review', '/api/spreadsheets/ai-proposals','/api/spreadsheets/ai-fixes', '/api/spreadsheets/ai-fix','/api/spreadsheets/auto-clean','/api/spreadsheets/mark-fine', '/api/spreadsheets/decision', '/api/spreadsheets/decide', '/api/spreadsheets/decision-status', '/api/spreadsheets/findings', '/api/spreadsheets/auto-fix', '/api/spreadsheets/revert-fix', '/api/spreadsheets/assign', '/api/spreadsheets/source-reconcile', '/api/spreadsheets/recheck', '/api/sharepoint/confirm-columns', '/api/change-summary', '/api/change-summaries', '/api/settings/sharepoint-jobs', '/api/spreadsheets/ai-verify', '/api/spreadsheets/ai-summary', '/api/spreadsheets/chat', '/api/keys', '/api/keys/create', '/api/keys/revoke', '/api/settings', '/api/settings/shifts', '/api/settings/rules', '/api/spreadsheets/shift-fix', '/api/spreadsheets/remove-rows', '/api/spreadsheets/dashboard', '/api/spreadsheets/dashboard/chat', '/api/spreadsheets/dashboard/undo', '/api/vins/search', '/api/spreadsheets/review', '/api/recommendations', '/api/build', '/api/nexus', '/api/settings/report-header', '/api/clients', '/api/auth/email', '/api/auth/signout', '/api/users', '/api/users/save', '/api/users/remove', '/api/users/link', '/api/users/email', '/api/access/required', '/api/overview', '/api/kpis', '/api/overview/artifact', '/api/overview/artifact/chat', '/api/overview/artifact/undo', '/api/today', '/api/today/job', '/api/autopilot', '/api/periods', '/api/periods/check-file', '/api/periods/artifact', '/api/periods/artifact/chat', '/api/periods/artifact/undo', '/api/spreadsheets/approve', '/api/spreadsheets/send', '/api/spreadsheets/send-info', '/api/spreadsheets/baseline', '/api/spreadsheets/fixes', '/api/spreadsheets/cells', '/api/spreadsheets/sheet', '/api/spreadsheets/apply-fixes', '/api/spreadsheets/source-upload', '/api/spreadsheets/reconcile', '/api/workflow/import', '/api/workflow/batch', '/api/workflow/apply', '/api/workflow/approve', '/api/workflow/demo/decision', '/api/workflow/demo/decisions', '/api/workflow/demo/apply') and route.removeprefix('/api/') not in ACTIONS:
            return self.fail(404, 'Not found.')
        try:
            limit = 4 * 1024 * 1024 + 16384 if route in ('/api/spreadsheets/import', '/api/spreadsheets/upload/part', '/api/spreadsheets/source-upload', '/api/periods/check-file', '/api/spreadsheets/apply-fixes', '/api/spreadsheets/remove-rows') else (2 * 1024 * 1024 if route in ('/api/workflow/import','/api/workflow/batch','/api/spreadsheets/reconcile') else (1024 * 1024 if route == '/api/reports/import' else 100000))
            if not route.startswith('/api/workflow/') and route not in ('/api/periods/check-file', '/api/settings/sharepoint-jobs', '/api/spreadsheets/import','/api/spreadsheets/upload/part','/api/spreadsheets/upload/finish','/api/spreadsheets/sharepoint','/api/spreadsheets/ai-review','/api/spreadsheets/ai-proposals','/api/spreadsheets/ai-fixes','/api/spreadsheets/ai-fix','/api/spreadsheets/auto-fix','/api/spreadsheets/revert-fix','/api/spreadsheets/auto-clean','/api/spreadsheets/mark-fine','/api/spreadsheets/decide','/api/spreadsheets/decision-status','/api/spreadsheets/findings','/api/spreadsheets/ai-verify','/api/spreadsheets/ai-summary','/api/spreadsheets/chat','/api/spreadsheets/baseline','/api/spreadsheets/fixes','/api/spreadsheets/cells','/api/spreadsheets/sheet','/api/spreadsheets/apply-fixes','/api/spreadsheets/source-upload','/api/spreadsheets/reconcile','/api/spreadsheets/demo/decisions','/api/spreadsheets/demo/apply','/api/reports/import'): limit=10000
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= limit:
                return self.fail(413, 'Request size is invalid.')
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise Conflict('Incomplete request body.')
            try:
                gate_payload = json.loads(raw)
            except ValueError:
                gate_payload = None
            refused = self.gate('POST', route, gate_payload)
            if refused:   # a refusal has been sent: nothing below may run (send() returns True)
                return refused
            if route.startswith(('/api/auth/', '/api/users', '/api/access/')):
                return self.account_post(route, gate_payload)
            content_type, headers = 'application/json; charset=utf-8', {}
            if route in ('/api/spreadsheets/fixes', '/api/spreadsheets/cells', '/api/spreadsheets/sheet', '/api/spreadsheets/apply-fixes'):
                payload = json.loads(raw)
                file_hash = payload.get('file_hash') if isinstance(payload, dict) else None
                if not isinstance(file_hash, str) or not re.fullmatch(r'[0-9a-f]{64}', file_hash):
                    raise Conflict('Valid spreadsheet version ID required.')
                if not self.can_hash(file_hash):
                    return self.fail(404, 'Report not found.')
                from hosting.fixes import FixError
                try:
                    if route.endswith('/fixes'):
                        with self.store().session() as directory:
                            return self.send(200, encode_json(SpreadsheetInbox(directory).fix_proposals(file_hash)))
                    if route.endswith('/sheet'):
                        with self.store().session() as directory:
                            return self.send(200, encode_json(SpreadsheetInbox(directory).sheet_grid(file_hash, payload.get('sheet'))))
                    if route.endswith('/cells'):
                        with self.store().session() as directory:
                            return self.send(200, encode_json(SpreadsheetInbox(directory).row_cells(file_hash, payload.get('sheet'), payload.get('row'))))
                    changes = payload.get('changes')
                    if isinstance(changes, list):
                        changes = [dict(c, kind='auto') if isinstance(c, dict) and c.get('kind') not in ('number', 'text', 'auto') else c for c in changes]
                    with self.store().session() as directory:
                        fixed = SpreadsheetInbox(directory).apply_fixes(file_hash, changes, expected_check_hash=payload.get('expected_check_hash'), recommended_only=payload.get('recommended_only') is True)
                except FixError as exc:
                    raise Conflict(str(exc))
                already_applied = fixed.get('already_applied') is True
                for step in (self.sharepoint_check if sharepoint_configured() else None, self.baseline_check):
                    if step:
                        try:
                            fixed = step(fixed['file_hash'])
                        except Exception:
                            debug_log()
                return self.send(200, encode_json({'item': fixed, 'already_applied': already_applied}))
            if route == '/api/spreadsheets/ai-summary':
                payload = json.loads(raw)
                file_hash = payload.get('file_hash') if isinstance(payload, dict) else None
                if not isinstance(file_hash, str) or not re.fullmatch(r'[0-9a-f]{64}', file_hash):
                    raise Conflict('Valid spreadsheet version ID required.')
                from hosting import ai_review
                from hosting.review_summary import summarize
                from hosting.fixes import proposals
                with self.store().session() as directory:
                    inbox = SpreadsheetInbox(directory)
                    item, data, _ = inbox.ai_fix_inputs(file_hash)
                    changes = inbox.fix_proposals(file_hash)['proposals']
                rv = summarize(item)
                extra = [c for c in (payload.get('extra') or []) if isinstance(c, dict) and c.get('id')][:200] if isinstance(payload.get('extra'), list) else []
                skip = set(x for x in (payload.get('skip') or []) if isinstance(x, str)) if isinstance(payload.get('skip'), list) else set()
                changes = [c for c in changes if c['id'] not in skip] + extra
                numbers = {k: rv.get(k) for k in ('status', 'counts', 'job', 'day', 'scans', 'inspected', 'rejected', 'rejected_unit')}
                try:
                    out = ai_review.summarize_fixes(item, [i for i in rv['issues'] if i['severity'] != 'info'], changes, numbers)
                except ai_review.AIReviewNotConfigured as exc:
                    raise Conflict(str(exc))
                except ai_review.AIReviewError as exc:
                    raise Conflict(str(exc))
                return self.send(200, encode_json(out))
            if route == '/api/spreadsheets/ai-verify':
                payload = json.loads(raw)
                file_hash = payload.get('file_hash') if isinstance(payload, dict) else None
                if not isinstance(file_hash, str) or not re.fullmatch(r'[0-9a-f]{64}', file_hash):
                    raise Conflict('Valid spreadsheet version ID required.')
                from hosting.spreadsheet_inbox import make_ai_verify
                from hosting.source_compare import identify
                with self.store().session() as directory:
                    inbox = SpreadsheetInbox(directory)
                    item, data, issues = inbox.ai_fix_inputs(file_hash, include_dismissed=True)
                    rules = inbox.rules_for(identify(item.get('filename'), item)[0])
                record = make_ai_verify(item, data, issues, rules=rules)  # network call outside any DB transaction
                if record.get('status') != 'done':
                    raise Conflict(record.get('message') or 'The double-check failed.')
                with self.store().session() as directory:
                    SpreadsheetInbox(directory).record_ai_verify(file_hash, record)
                return self.send(200, encode_json({'verified': len(record.get('verdicts') or {}),
                                                   'dismissed': sum(1 for v in (record.get('verdicts') or {}).values() if v.get('verdict') == 'not_an_issue')}))
            if route == '/api/spreadsheets/chat':
                payload = json.loads(raw)
                file_hash = payload.get('file_hash') if isinstance(payload, dict) else None
                if not isinstance(file_hash, str) or not re.fullmatch(r'[0-9a-f]{64}', file_hash):
                    raise Conflict('Valid spreadsheet version ID required.')
                if not self.can_hash(file_hash):
                    return self.fail(404, 'Report not found.')
                from hosting import report_chat
                conversation = report_chat.messages(payload.get('messages'))
                expected = payload.get('expected_check_hash')
                with self.store().session() as directory:
                    inbox = SpreadsheetInbox(directory)
                    item, review, holds, fingerprint = report_chat.snapshot(inbox, file_hash, expected)
                    if len(conversation) > 1 and payload.get('expected_context_hash') != fingerprint:
                        raise Conflict('Report source or review context changed. Reload before continuing this conversation.')
                    _, data = inbox.original_bytes(file_hash)
                out = report_chat.answer(item, data, review, holds, conversation)
                # Provider work happens outside a transaction. Recheck authorization and
                # exact evidence before returning any answer, including revoked sessions.
                self._user_cache = None
                self._clients = None
                refused = self.gate('POST', route, payload)
                if refused:
                    return refused
                if not self.can_hash(file_hash):
                    return self.fail(404, 'Report not found.')
                with self.store().session() as directory:
                    current = report_chat.snapshot(SpreadsheetInbox(directory), file_hash, expected)
                    if current[3] != fingerprint:
                        raise Conflict('Report evidence or access changed while Dex was answering. Reload before asking again.')
                out['context_hash'] = fingerprint
                return self.send(200, encode_json(out))
            if route == '/api/change-summary':   # make a PDF change summary now (kept on record)
                payload = json.loads(raw)
                file_hash = payload.get('file_hash') if isinstance(payload, dict) else None
                if not isinstance(file_hash, str) or not re.fullmatch(r'[0-9a-f]{64}', file_hash):
                    raise Conflict('Valid spreadsheet version ID required.')
                if not self.can_hash(file_hash):
                    return self.fail(404, 'Report not found.')
                from hosting import change_summary
                u = self.current_user() or {}
                who = None if u.get('virtual') else (u.get('name') or u.get('email'))
                return self.send(200, encode_json({'record': change_summary.make(self, file_hash, payload.get('by') or who, 'requested')}))
            if route == '/api/change-summaries':
                payload = json.loads(raw) or {}
                from hosting import change_summary
                with self.store().session() as directory:
                    recs = SpreadsheetInbox(directory).setting(change_summary.RECORDS, []) or []
                recs = [r for r in recs if self.can_job(r.get('job'))]
                fh = payload.get('file_hash') if isinstance(payload, dict) else None
                if isinstance(fh, str):
                    recs = [r for r in recs if fh in (r.get('file_hash'), r.get('first_hash'))]
                return self.send(200, encode_json({'records': recs[:500]}))
            if route == '/api/settings/sharepoint-jobs':   # the "PPG SharePoint — Clients & Jobs" workbook
                payload = json.loads(raw) or {}
                from hosting import sharepoint_jobs
                with self.store().session() as directory:
                    inbox = SpreadsheetInbox(directory)
                    if payload.get('content_base64'):
                        if not self.is_admin():
                            return self.fail(403, 'Only admins can change the SharePoint job list.')
                        cat = sharepoint_jobs.parse(base64.b64decode(payload['content_base64'], validate=True))
                        cat.update(filename=str(payload.get('filename') or '')[:200], uploaded_at=datetime.now(timezone.utc).isoformat())
                        inbox.set_setting(sharepoint_jobs.KEY, cat)
                    cat = inbox.setting(sharepoint_jobs.KEY) or {}
                return self.send(200, encode_json({k: cat.get(k) for k in ('as_of', 'filename', 'uploaded_at')} | dict(jobs=len(cat.get('jobs') or []),
                                 real=sum(1 for j in cat.get('jobs') or [] if (j.get('type') or 'Job') == 'Job'), active=sum(1 for j in cat.get('jobs') or [] if (j.get('status') or '').lower() == 'active'))))
            if route in ('/api/spreadsheets/auto-fix', '/api/spreadsheets/revert-fix'):   # fix what can be fixed; undo any single fix
                payload = json.loads(raw)
                file_hash = payload.get('file_hash') if isinstance(payload, dict) else None
                if not isinstance(file_hash, str) or not re.fullmatch(r'[0-9a-f]{64}', file_hash):
                    raise Conflict('Valid spreadsheet version ID required.')
                if not self.can_hash(file_hash):
                    return self.fail(404, 'Report not found.')
                from hosting import autofix
                from hosting.fixes import FixError
                me = self.current_user() or {}
                try:
                    if route.endswith('/auto-fix'):   # one short step at a time: sure fixes, Claude batches, finish
                        out = autofix.step(self, file_hash, str(payload.get('phase') or 'sure'))
                        return self.send(200, encode_json({'item': out['item'], 'auto_fixed': out['summary'], 'next': out['next']}))
                    cells = payload.get('cells') if isinstance(payload.get('cells'), list) else []
                    fine = payload.get('fine') if isinstance(payload.get('fine'), list) else []
                    if len(cells) + len(fine) > 1000 or not (cells or fine) or not all(isinstance(c, dict) for c in cells):
                        raise Conflict('Pick the fixes to revert.')
                    item = autofix.revert(self, file_hash, cells, [str(t)[:500] for t in fine], payload.get('by') or me.get('name') or me.get('email') or '')
                    return self.send(200, encode_json({'item': item}))
                except (autofix.AutoFixError, FixError) as exc:
                    raise Conflict(str(exc))
            if route == '/api/spreadsheets/decide':   # reject / defer / confirm / undo findings of this exact version, all or nothing
                from hosting import review_decisions
                payload = json.loads(raw)
                if not isinstance(payload, dict):
                    raise Conflict('Send a decision.')
                file_hash = payload.get('file_hash')
                if not isinstance(file_hash, str) or not re.fullmatch(r'[0-9a-f]{64}', file_hash):
                    raise Conflict('Valid spreadsheet version ID required.')
                if not self.can_hash(file_hash):
                    return self.fail(404, 'Report not found.')
                ids = payload.get('ids') if isinstance(payload.get('ids'), list) else []
                evidence = payload.get('evidence') if isinstance(payload.get('evidence'), list) else []
                seen, run = payload.get('expected_fps', payload.get('seen')), payload.get('expected_run', payload.get('run'))
                # every decision names the check run the reviewer was looking at and, except an undo, the exact reading of
                # each finding; the inbox checks both under the report's lock
                if not isinstance(run, dict) or (payload.get('action') != 'undo' and not isinstance(seen, dict)):
                    return self.send(409, encode_json({'error': 'This page is out of date (it did not say what you were looking at). Reload the report and decide again.',
                                                       'reload': True}))
                me = self.current_user() or {}
                by = str(me.get('name') or me.get('email') or payload.get('by') or '').strip()[:80]   # a signed-in reviewer is who they are
                try:
                    with self.store().session() as directory:
                        inbox = SpreadsheetInbox(directory)
                        item, entry = inbox.decide_findings(file_hash, ids=[str(k) for k in ids], action=str(payload.get('action') or ''),
                                                            reason=str(payload.get('reason') or ''), by=by, owner=str(payload.get('owner') or ''),
                                                            evidence=[str(e) for e in evidence], request_id=str(payload.get('request_id') or ''),
                                                            expected_rev=payload.get('expected_rev'), expected_fps=payload.get('expected_fps', seen),
                                                            expected_run=payload.get('expected_run', run), undo_of=payload.get('undo_of'))
                        view = self.report_view(inbox, file_hash)
                except review_decisions.DecisionConflict as exc:
                    return self.send(409, encode_json({'error': str(exc), **exc.detail}))
                except review_decisions.DecisionError as exc:
                    raise Conflict(str(exc))
                return self.send(200, encode_json({'ok': True, 'entry': {k: v for k, v in entry.items() if k != 'prev'}, 'report': view}))
            if route == '/api/spreadsheets/findings':   # the next page of findings (open or decided) of one version
                from hosting.review_summary import summarize
                payload = json.loads(raw)
                file_hash = payload.get('file_hash') if isinstance(payload, dict) else None
                if not isinstance(file_hash, str) or not re.fullmatch(r'[0-9a-f]{64}', file_hash):
                    raise Conflict('Valid spreadsheet version ID required.')
                if not self.can_hash(file_hash):
                    return self.fail(404, 'Report not found.')
                which = 'dismissed' if payload.get('list') == 'dismissed' else 'issues'
                offset, limit = max(0, int(payload.get('offset') or 0)), max(1, min(review_page_size(), int(payload.get('limit') or review_page_size())))
                with self.store().session() as directory:
                    rv = summarize(SpreadsheetInbox(directory).get(file_hash))
                items = rv.get(which) or []
                return self.send(200, encode_json({'list': which, 'offset': offset, 'total': len(items), 'items': items[offset:offset + limit],
                                                   'rev': (rv.get('decisions') or {}).get('rev', 0)}))
            if route == '/api/spreadsheets/decision-status':   # after a lost response: was this request saved?
                payload = json.loads(raw)
                file_hash = payload.get('file_hash') if isinstance(payload, dict) else None
                if not isinstance(file_hash, str) or not re.fullmatch(r'[0-9a-f]{64}', file_hash):
                    raise Conflict('Valid spreadsheet version ID required.')
                if not self.can_hash(file_hash):
                    return self.fail(404, 'Report not found.')
                with self.store().session() as directory:
                    item = SpreadsheetInbox(directory).get(file_hash)
                rid = str(payload.get('request_id') or '')
                did = (item.get('finding_requests') or {}).get(rid)
                return self.send(200, encode_json({'saved': bool(did), 'decision_id': did, 'rev': int(item.get('finding_decisions_rev') or 0)}))
            if route == '/api/spreadsheets/decision':   # a reviewer's note, "keep as recorded" or "reject this suggestion", on this exact version
                raise Conflict('Reload the report and use scoped finding decisions with current fingerprints.')
                payload = json.loads(raw)
                file_hash = payload.get('file_hash') if isinstance(payload, dict) else None
                if not isinstance(file_hash, str) or not re.fullmatch(r'[0-9a-f]{64}', file_hash):
                    raise Conflict('Valid spreadsheet version ID required.')
                if not self.can_hash(file_hash):
                    return self.fail(404, 'Report not found.')
                me = self.current_user() or {}
                by = str(payload.get('by') or me.get('name') or me.get('email') or '').strip()[:80]
                keys = payload.get('keys') if isinstance(payload.get('keys'), list) else [payload.get('key')]
                if not 1 <= len(keys) <= 500:
                    raise Conflict('Send 1 to 500 items.')
                shown = self.shown(payload)
                if shown is None:
                    return self.send(409, encode_json({'error': 'This page is out of date (it did not say what you were looking at). Reload the report and decide again.', 'reload': True}))
                from hosting import review_decisions
                try:
                    with self.store().session() as directory:
                        inbox = SpreadsheetInbox(directory)
                        item = inbox.record_decisions(file_hash, [str(k or '') for k in keys], str(payload.get('action') or ''), str(payload.get('note') or ''), by, *shown)
                except review_decisions.DecisionConflict as exc:
                    return self.send(409, encode_json({'error': str(exc), **exc.detail}))
                return self.send(200, encode_json({'ok': True, 'decisions': item.get('review_decisions') or {}}))
            if route == '/api/spreadsheets/mark-fine':   # a person said these flags are fine: kept on this version and every later one
                raise Conflict('Reload the report and use scoped finding decisions with current fingerprints.')
                payload = json.loads(raw)
                file_hash = payload.get('file_hash') if isinstance(payload, dict) else None
                issues = payload.get('issues') if isinstance(payload, dict) else None
                if not isinstance(file_hash, str) or not re.fullmatch(r'[0-9a-f]{64}', file_hash):
                    raise Conflict('Valid spreadsheet version ID required.')
                if not isinstance(issues, list) or not 1 <= len(issues) <= 1000 or not all(isinstance(i, dict) for i in issues):
                    raise Conflict('Send 1 to 1000 issues.')
                if not self.can_hash(file_hash):
                    return self.fail(404, 'Report not found.')
                by = str(payload.get('by') or '').strip()[:80]
                clean = lambda v, n=500: None if v is None else str(v)[:n]
                add = [dict(code=clean(i.get('code'), 80), sheet=clean(i.get('sheet'), 120), row=i.get('row') if isinstance(i.get('row'), (int, str)) else None,
                            cell=clean(i.get('cell'), 12), key=clean(i.get('key'), 300),   # the exact cell: never settles another cell on the same row
                            text=clean(i.get('text')) or '', reason=f'Marked fine as is by {by}.' if by else 'Marked fine as is by a reviewer.', by_person=True) for i in issues]
                shown = self.shown(payload)
                if shown is None:
                    return self.send(409, encode_json({'error': 'This page is out of date (it did not say what you were looking at). Reload the report and decide again.', 'reload': True}))
                from hosting import review_decisions
                try:
                    with self.store().session() as directory:
                        SpreadsheetInbox(directory).mark_fine(file_hash, add, *shown)
                except review_decisions.DecisionConflict as exc:
                    return self.send(409, encode_json({'error': str(exc), **exc.detail}))
                return self.send(200, encode_json({'ok': True, 'marked': len(add)}))
            if route == '/api/spreadsheets/assign':   # a person chooses the client and job; kept with who, when and why
                payload = json.loads(raw)
                file_hash = payload.get('file_hash') if isinstance(payload, dict) else None
                choice = payload.get('choice') if isinstance(payload, dict) else None
                if not isinstance(file_hash, str) or not re.fullmatch(r'[0-9a-f]{64}', file_hash):
                    raise Conflict('Valid spreadsheet version ID required.')
                if not isinstance(choice, dict):
                    raise Conflict('Choose a client and a job.')
                if not self.can_hash(file_hash):
                    return self.fail(404, 'Report not found.')
                from hosting import registry
                from hosting.review_summary import summarize
                clean = {k: (' '.join(str(choice.get(k)).split())[:120] if choice.get(k) not in (None, '') else None) for k in ('client_key', 'client', 'job', 'site', 'list_id', 'source')}
                with self.store().session() as directory:
                    inbox = SpreadsheetInbox(directory)
                    found = inbox.get(file_hash)
                    current = inbox.assignment_for(found, summarize(found, with_issues=False))
                    ctx = inbox.assignment_context()
                    known = {e.get('client_key') for e in ctx['registry']} | {c.get('key') for c in ctx['snapshot']}
                    if clean['client_key'] not in known:
                        raise Conflict('That client is not on SharePoint or in the job registry.')
                    record = registry.assign(inbox, file_hash, clean, self.current_user(), payload.get('reason'), current, remember=payload.get('remember') is True)
                    after = inbox.assignment_for(found, summarize(found, with_issues=False))   # read back what was saved
                return self.send(200, encode_json({'ok': True, 'assignment': after, 'saved': record}))
            if route == '/api/sharepoint/confirm-columns':   # admins: which SharePoint column holds each quantity, for one list
                if not self.is_admin():
                    return self.fail(403, 'Only admins can confirm SharePoint columns.')
                payload = json.loads(raw)
                if not isinstance(payload, dict) or not isinstance(payload.get('site_id'), str) or not payload.get('site_id') or not isinstance(payload.get('list_id'), str) or not isinstance(payload.get('mapping'), dict) or 'expected_mapping_revision' not in payload:
                    raise Conflict('Choose the exact source columns from the displayed comparison.')
                file_hash = payload.get('file_hash')
                if not isinstance(file_hash, str) or not re.fullmatch(r'[0-9a-f]{64}', file_hash) or not self.can_hash(file_hash):
                    return self.fail(404, 'Report not found.')
                with self.store().session() as directory:
                    saved = SpreadsheetInbox(directory).confirm_columns(payload['list_id'], payload['mapping'],
                            (self.current_user() or {}).get('email') or 'admin', site_id=payload['site_id'],
                            expected_mapping_revision=payload['expected_mapping_revision'], file_hash=file_hash,
                            expected_check_hash=payload.get('expected_check_hash'), expected_snapshot_id=payload.get('expected_snapshot_id'))
                return self.send(200, encode_json({'ok': True, 'mapping': saved}))
            if route == '/api/spreadsheets/recheck':   # show what the current checks would change; save only when a person applies them
                payload = json.loads(raw)
                file_hash = payload.get('file_hash') if isinstance(payload, dict) else None
                if not isinstance(file_hash, str) or not re.fullmatch(r'[0-9a-f]{64}', file_hash):
                    raise Conflict('Valid spreadsheet version ID required.')
                if not self.can_hash(file_hash):
                    return self.fail(404, 'Report not found.')
                with self.store().session() as directory:
                    inbox = SpreadsheetInbox(directory)
                    if payload.get('apply') is True:
                        by = payload.get('by') or (self.current_user() or {}).get('name') or (self.current_user() or {}).get('email')
                        item = inbox.apply_recheck(file_hash, by)
                if payload.get('apply') is True:
                    # the old SharePoint check belonged to the earlier checks: run it again so the new result is verified, not assumed
                    reconciled = None
                    if sharepoint_configured():
                        try:
                            reconciled = (self.sharepoint_check(file_hash) or {}).get('checker_version') is not None
                        except Exception:
                            debug_log()
                    return self.send(200, encode_json({'ok': True, 'applied': True, 'checker_version': item.get('checker_version'), 'reconciled': reconciled}))
                with self.store().session() as directory:
                    inbox = SpreadsheetInbox(directory)
                    return self.send(200, encode_json({'ok': True, 'proposal': inbox.propose_recheck(file_hash)}))
            if route == '/api/spreadsheets/source-reconcile':   # item-by-item against the live SharePoint list (read-only)
                payload = json.loads(raw)
                file_hash = payload.get('file_hash') if isinstance(payload, dict) else None
                if not isinstance(file_hash, str) or not re.fullmatch(r'[0-9a-f]{64}', file_hash):
                    raise Conflict('Valid spreadsheet version ID required.')
                if not self.can_hash(file_hash):
                    return self.fail(404, 'Report not found.')
                from hosting import reconcile, registry, sharepoint
                from hosting.review_summary import summarize
                with self.store().session() as directory:
                    inbox = SpreadsheetInbox(directory)
                    found = inbox.get(file_hash)
                    rv = summarize(found, with_issues=False)
                    a = inbox.assignment_for(found, rv)
                    schedule = inbox.shift_schedule(a.get('job') or rv.get('job'))
                if a['status'] not in registry.RESOLVED or not a.get('site') or not a.get('list_id'):
                    raise Conflict('Choose the client and job (with its SharePoint list) first, so the right list is read.')
                if not rv.get('day'):
                    raise Conflict('The report has no business date, so SharePoint cannot be matched to a day.')
                if not sharepoint.configured():
                    raise Conflict('SharePoint is not connected.')
                binding = sharepoint.assignment_identity(a)
                snap = reconcile.snapshot(a.get('site'), binding['list_id'], schedule=schedule, site_id=binding['site_id'])
                with self.store().session() as directory:
                    item = SpreadsheetInbox(directory).source_reconcile(file_hash, snap, rv['day'], expected_check_hash=found.get('check_hash'), expected_assignment=a)
                return self.send(200, encode_json({'ok': True, 'source_reconciliation': item.get('source_reconciliation')}))
            if route == '/api/spreadsheets/auto-clean':   # an older report (uploaded before the automatic cleanup): make the sure fixes now
                payload = json.loads(raw)
                file_hash = payload.get('file_hash') if isinstance(payload, dict) else None
                if not isinstance(file_hash, str) or not re.fullmatch(r'[0-9a-f]{64}', file_hash):
                    raise Conflict('Valid spreadsheet version ID required.')
                if not self.can_hash(file_hash):
                    return self.fail(404, 'Report not found.')
                with self.store().session() as directory:
                    current = SpreadsheetInbox(directory).get(file_hash)
                if current.get('approval') or any(x.get('sent') for x in current.get('sent') or []):
                    raise Conflict('This report is already approved, so it is left as it is.')
                from hosting import autoformat
                cleaned = autoformat.run(self, file_hash)
                return self.send(200, encode_json({'item': cleaned or current, 'changed': bool(cleaned)}))
            if route == '/api/spreadsheets/ai-fix':   # Fix with AI: Claude resolves the picked issues (or all) and the changes are made
                payload = json.loads(raw)
                file_hash = payload.get('file_hash') if isinstance(payload, dict) else None
                if not isinstance(file_hash, str) or not re.fullmatch(r'[0-9a-f]{64}', file_hash):
                    raise Conflict('Valid spreadsheet version ID required.')
                keys = payload.get('keys')
                if keys is not None and (not isinstance(keys, list) or not all(isinstance(k, str) and len(k) < 300 for k in keys) or len(keys) > 500):
                    raise Conflict('Pick up to 500 issues.')
                if not self.can_hash(file_hash):
                    return self.fail(404, 'Report not found.')
                from hosting import ai_fix
                try:
                    fixed, summary = ai_fix.run(self, file_hash, keys, auto=payload.get('auto') is True)
                except ai_fix.AIFixError as exc:
                    raise Conflict(str(exc))
                return self.send(200, encode_json({'item': fixed, 'ai_fixed': summary}))
            if route == '/api/spreadsheets/ai-proposals':
                payload = json.loads(raw)
                from hosting import ai_proposals
                ai_proposals.validate_request(payload)
                if not self.can_hash(payload['file_hash']):
                    return self.fail(404, 'Report not found.')
                record = ai_proposals.run(self, payload)
                return self.send(200, encode_json({'ai_proposals': record}))
            if route == '/api/spreadsheets/ai-fixes':
                payload = json.loads(raw)
                file_hash = payload.get('file_hash') if isinstance(payload, dict) else None
                if not isinstance(file_hash, str) or not re.fullmatch(r'[0-9a-f]{64}', file_hash):
                    raise Conflict('Valid spreadsheet version ID required.')
                if not self.can_hash(file_hash):
                    return self.fail(404, 'Report not found.')
                expected_check_hash = payload.get('expected_check_hash')
                from hosting.spreadsheet_inbox import make_ai_fixes
                with self.store().session() as directory:
                    item, data, issues = SpreadsheetInbox(directory).proposal_inputs(file_hash, expected_check_hash)
                record = make_ai_fixes(item, data, issues)  # network call outside any DB transaction
                if record.get('status') == 'done':
                    with self.store().session() as directory:
                        SpreadsheetInbox(directory).record_ai_fixes(file_hash, record, expected_check_hash=expected_check_hash)
                if record.get('status') in ('error', 'not_configured'):
                    raise Conflict(record.get('message') or 'AI fixes failed.')
                with self.store().session() as directory:
                    view = self.report_view(SpreadsheetInbox(directory), file_hash)
                return self.send(200, encode_json({'ai_fixes': record, 'report': view}))
            if route in ('/api/settings', '/api/settings/shifts', '/api/settings/rules'):
                from hosting import shifts as shift_times
                from hosting.check_catalog import catalog
                payload = json.loads(raw)
                if not isinstance(payload, dict):
                    raise Conflict('Send a JSON object.')
                rechecked = moved = 0
                if route.endswith('/shifts'):
                    job = ' '.join(str(payload.get('job') or '').split()).upper()[:60] or None
                    with self.store().session() as directory:
                        inbox = SpreadsheetInbox(directory)
                        current = inbox.setting('shifts', None) or {'default': shift_times.DEFAULT, 'jobs': {}}
                        if payload.get('remove') is True and job:
                            current.setdefault('jobs', {}).pop(job, None)
                        else:
                            cleaned = shift_times.clean(payload.get('shifts'))
                            if job:
                                current.setdefault('jobs', {})[job] = cleaned
                            else:
                                current['default'] = cleaned
                        inbox.set_setting('shifts', current)
                        rows = inbox.list()['spreadsheets']
                    # put everything in the right shift with the new times: compare the affected reports again
                    if sharepoint_configured():
                        jobs_with_own = {k.upper() for k in current.get('jobs') or {}}
                        for r in rows[:15]:
                            rjob = ((r.get('review') or {}).get('job') or '').upper()
                            if (job and rjob != job) or (not job and rjob in jobs_with_own):
                                continue
                            try:
                                updated = self.sharepoint_check(r['file_hash'])
                                rechecked += 1
                                moved += (updated.get('sharepoint') or {}).get('wrong_shift') or 0
                            except Exception:
                                debug_log()
                elif route.endswith('/rules'):
                    with self.store().session() as directory:
                        inbox = SpreadsheetInbox(directory)
                        rules = inbox.setting('ai_rules', []) or []
                        if payload.get('remove'):
                            rules = [r for r in rules if r.get('id') != payload['remove']]
                        else:
                            text = ' '.join(str(payload.get('text') or '').split())[:400]
                            if not text:
                                raise Conflict('Type what Claude should look for.')
                            if len(rules) >= 30:
                                raise Conflict('There are already 30 rules. Remove one first.')
                            import secrets
                            rules.append(dict(id='rule_' + secrets.token_hex(4), text=text,
                                              job=' '.join(str(payload.get('job') or '').split()).upper()[:60] or None))
                        inbox.set_setting('ai_rules', rules)
                with self.store().session() as directory:
                    inbox = SpreadsheetInbox(directory)
                    out = dict(shifts=inbox.setting('shifts', None) or {'default': shift_times.DEFAULT, 'jobs': {}},
                               rules=inbox.setting('ai_rules', []) or [], checks=catalog(),
                               jobs=sorted({(r.get('review') or {}).get('job') for r in inbox.list()['spreadsheets'] if (r.get('review') or {}).get('job')}),
                               ai_on=ai_review_configured(), sharepoint_on=sharepoint_configured())
                for group in out['shifts'].get('jobs', {}).values():
                    for x in group:
                        x['label'] = shift_times.label(x)
                for x in out['shifts']['default']:
                    x['label'] = shift_times.label(x)
                return self.send(200, encode_json(dict(out, rechecked=rechecked, moved=moved)))
            if route in ('/api/spreadsheets/dashboard', '/api/spreadsheets/dashboard/chat', '/api/spreadsheets/dashboard/undo'):
                payload = json.loads(raw)
                file_hash = payload.get('file_hash') if isinstance(payload, dict) else None
                if not isinstance(file_hash, str) or not re.fullmatch(r'[0-9a-f]{64}', file_hash):
                    raise Conflict('Valid spreadsheet version ID required.')
                from hosting import dashboard as dash_mod
                with self.store().session() as directory:
                    inbox = SpreadsheetInbox(directory)
                    item = inbox.get(file_hash)
                    _, data = inbox.original_bytes(file_hash)
                    current = inbox.dashboard(file_hash)
                def done(d):
                    return self.send(200, encode_json(dict({k: d[k] for k in ('undo', 'messages', 'updated_at')}, ai_on=ai_review_configured(),
                                                           view=f'/api/spreadsheets/dashboard/view?id={file_hash}&v={d["updated_at"]}',
                                                           download=f'/api/spreadsheets/dashboard/view?id={file_hash}&download=1')))
                if route.endswith('/undo'):
                    with self.store().session() as directory:
                        return done(SpreadsheetInbox(directory).undo_dashboard(file_hash))
                facts = dash_mod.data_for(item, data)
                if route.endswith('/dashboard'):
                    if current and payload.get('rebuild') is not True:
                        return done(current)
                    with self.store().session() as directory:
                        return done(SpreadsheetInbox(directory).save_dashboard(file_hash, dash_mod.build(facts), reset=True))
                request = ' '.join(str(payload.get('message') or '').split())[:2000]
                if not request:
                    raise Conflict('Say what to change.')
                if not current:
                    raise Conflict('Create the dashboard first.')
                from hosting import ai_review
                try:
                    page, reply = ai_review.edit_dashboard(current['html'], facts, current['messages'], request)  # network call outside any DB transaction
                except (ai_review.AIReviewNotConfigured, ai_review.AIReviewError) as exc:
                    raise Conflict(str(exc))
                now = datetime.now(timezone.utc).isoformat()
                with self.store().session() as directory:
                    return done(SpreadsheetInbox(directory).save_dashboard(file_hash, page, [dict(role='user', text=request, at=now), dict(role='assistant', text=reply, at=now)]))
            if route == '/api/spreadsheets/remove-rows':
                from hosting import public_api as ops
                payload = json.loads(raw) or {}
                file_hash = payload.get('file_hash')
                if not isinstance(file_hash, str) or not re.fullmatch(r'[0-9a-f]{64}', file_hash):
                    raise Conflict('Valid spreadsheet version ID required.')
                if not self.can_hash(file_hash):
                    return self.fail(404, 'Report not found.')
                rows = [(r.get('sheet'), r.get('row')) for r in (payload.get('rows') or []) if isinstance(r, dict) and r.get('sheet') and str(r.get('row', '')).isdigit()]
                try:
                    item = ops.remove_rows(self, file_hash, rows)
                except ops.ApiError as exc:
                    raise Conflict(exc.message if hasattr(exc, 'message') else str(exc))
                return self.send(200, encode_json(dict(item=dict(file_hash=item['file_hash'], filename=item['filename']), removed=len(rows))))
            if route == '/api/autopilot':
                from hosting import autopilot
                from zoneinfo import ZoneInfo
                payload = json.loads(raw) or {}
                if not autopilot.enabled():   # paused for now: nothing is built or cleaned in the background
                    return self.send(200, encode_json(dict(on=False, ran=False, paused=True, last=None, sharepoint_on=sharepoint_configured(), can_change=False)))
                if 'on' in payload:
                    if not self.is_admin():
                        return self.fail(403, 'Only an admin can turn autopilot on or off.')
                    with self.store().session() as directory:
                        autopilot.set_on(SpreadsheetInbox(directory), payload['on'] is True, (self.current_user() or {}).get('email'))
                if payload.get('run'):
                    out = autopilot.run(self, datetime.now(ZoneInfo('America/Chicago')).date(), force=payload.get('force') is True and self.is_admin())
                else:
                    with self.store().session() as directory:
                        cfg = autopilot.settings(SpreadsheetInbox(directory))
                    out = dict(on=bool(cfg.get('on')), ran=False, last=cfg.get('last'))
                return self.send(200, encode_json(dict(out, sharepoint_on=sharepoint_configured(), can_change=self.is_admin())))
            if route in ('/api/today', '/api/today/job'):
                from hosting import periods
                from zoneinfo import ZoneInfo
                payload = json.loads(raw) or {}
                day = payload.get('day') or datetime.now(ZoneInfo('America/Chicago')).date().isoformat()
                try:
                    d0 = datetime.fromisoformat(day).date()
                except ValueError:
                    raise Conflict('Pick a valid date.')
                if route.endswith('/job'):
                    job = ' '.join(str(payload.get('job') or '').split()).upper()
                    if not job:
                        raise Conflict('Which job?')
                    with self.store().session() as directory:
                        schedule = SpreadsheetInbox(directory).shift_schedule(job)
                    try:
                        sp = periods.day_scans(job, d0.isoformat(), schedule)  # network call outside any DB transaction
                    except Exception as exc:
                        debug_log()
                        sp = dict(error=str(exc)[:200])
                    return self.send(200, encode_json(dict(job=job, sharepoint=sp)))
                with self.store().session() as directory:
                    inbox = SpreadsheetInbox(directory)
                    watch = inbox.setting('today_jobs', []) or []
                    add = ' '.join(str(payload.get('add') or '').split()).upper()[:60]
                    if add and add not in watch:
                        watch = inbox.set_setting('today_jobs', watch + [add])
                    if payload.get('remove'):
                        watch = inbox.set_setting('today_jobs', [j for j in watch if j != str(payload['remove']).upper()])
                    from datetime import timedelta
                    recent = set()
                    listed = inbox.list()
                    for r in listed['spreadsheets']:
                        rv = r.get('review') or {}
                        if rv.get('job') and rv.get('day') and rv['day'] >= (d0 - timedelta(days=21)).isoformat():
                            recent.add(rv['job'].upper())
                    jobs = [j for j in sorted(set(watch) | recent) if self.can_job(j)]
                    rows = []
                    for job in jobs:
                        reps = inbox.reports_for(job, d0 - timedelta(days=1), d0)
                        rows.append(dict(job=job, watched=job in watch, today=reps.get(d0.isoformat()), yesterday=reps.get((d0 - timedelta(days=1)).isoformat())))
                from hosting import sharepoint as _sp
                disc = dict(_sp.last_discovery)
                scanned, total = len(listed['spreadsheets']), listed.get('count') or len(listed['spreadsheets'])
                # say what "missing" covers: only these jobs, from this many reports, and whether SharePoint discovery was whole
                coverage = dict(basis='Jobs you watch plus jobs with a report in the last 21 days. A job with no report and no watch is not listed.',
                                reports_scanned=scanned, reports_total=total, capped=total > scanned,
                                discovery=dict(ran=bool(disc.get('at')), search_failed=disc.get('search_failed'),
                                               unreadable_sites=[x.get('site') for x in disc.get('unreadable_sites') or []]),
                                complete=total <= scanned and not disc.get('search_failed') and not disc.get('unreadable_sites'))
                return self.send(200, encode_json(dict(day=d0.isoformat(), jobs=rows, sharepoint_on=sharepoint_configured(), coverage=coverage)))
            if route == '/api/clients':
                from hosting import sharepoint
                payload = json.loads(raw or b'{}') or {}
                if not sharepoint.configured():
                    return self.send(200, encode_json(dict(clients=[], sharepoint=False)))
                try:
                    found = sharepoint.clients(refresh=payload.get('refresh') is True)
                    discovery = dict(sharepoint.last_discovery)
                    try:   # remembered so report assignment works without calling SharePoint on every list
                        from hosting import registry
                        with self.store().session() as directory:
                            inbox = SpreadsheetInbox(directory)
                            if inbox.setting(registry.SNAPSHOT_KEY, None) != found:
                                inbox.set_setting(registry.SNAPSHOT_KEY, found)
                    except Exception:
                        debug_log()
                    if not self.is_admin():
                        mine = set((self.current_user() or {}).get('clients') or [])
                        found = [c for c in found if c['key'] in mine]
                    return self.send(200, encode_json(dict(clients=found, sharepoint=True, coverage=dict(complete=not discovery.get("search_failed") and not discovery.get("unreadable_sites"), search_failed=discovery.get("search_failed"), unreadable_sites=discovery.get("unreadable_sites")))))
                except (sharepoint.SharePointError, OSError) as exc:
                    return self.send(200, encode_json(dict(clients=[], sharepoint=True, error=str(exc))))
            if route in ('/api/build', '/api/nexus', '/api/settings/report-header'):
                from hosting import public_api as ops
                payload = json.loads(raw) or {}
                if route == '/api/settings/report-header':
                    job = ' '.join(str(payload.get('job') or '').split()).upper()
                    if not job:
                        raise Conflict('Which job?')
                    with self.store().session() as directory:
                        inbox = SpreadsheetInbox(directory)
                        try:
                            binding = ops._build_binding(inbox, job, payload.get('source'))
                        except ops.ApiError as exc:
                            raise Conflict(str(exc))
                        header_key = f'report_header|{binding["site_id"]}|{binding["list_id"]}'
                        if isinstance(payload.get('header'), dict):
                            inbox.set_setting(header_key, {k: ' '.join(str(payload['header'].get(k) or '').split())[:120] for k in ('customer', 'worksite', 'program', 'prepared_by')})
                        return self.send(200, encode_json(dict(job=job, source_identity=binding, header=inbox.setting(header_key, {}) or {}, suggested={})))
                try:
                    if route == '/api/build':
                        return self.send(200, encode_json(ops.op_build(self, payload.get('job'), payload.get('day'), payload.get('force') is True, payload.get('header'), payload.get('source'))))
                    job, day = payload.get('job'), payload.get('day')
                    source = payload.get('source')
                    if payload.get('file_hash'):
                        from hosting.source_compare import identify
                        with self.store().session() as directory:
                            inbox = SpreadsheetInbox(directory)
                            item = inbox.get(str(payload['file_hash']))
                            from hosting.review_summary import summarize
                            from hosting import registry
                            assigned = inbox.assignment_for(item, summarize(item, with_issues=False))
                            if assigned.get('status') not in registry.RESOLVED:
                                raise ops.ApiError(409, 'Resolve this report source before retrieving Nexus values.')
                            source = {k: assigned.get(k) for k in ('site','site_id','list_id') if assigned.get(k)}
                        job, day = identify(item.get('filename'), item)
                    return self.send(200, encode_json(ops.op_nexus(self, job, day, source)))
                except ops.ApiError as exc:
                    raise Conflict(str(exc))
            if route == '/api/recommendations':
                from hosting import recommend
                from hosting.review_summary import summarize
                payload = json.loads(raw)
                file_hash = payload.get('file_hash') if isinstance(payload, dict) else None
                if not isinstance(file_hash, str) or not re.fullmatch(r'[0-9a-f]{64}', file_hash):
                    raise Conflict('Valid spreadsheet version ID required.')
                with self.store().session() as directory:
                    inbox = SpreadsheetInbox(directory)
                    item = inbox.get(file_hash)
                    try:
                        rejected = {k[4:] for k, d in (item.get('review_decisions') or {}).items() if k.startswith('fix:') and d.get('action') in ('reject', 'keep')}
                        # the same count the Corrections panel shows: suggestions on this version a person has not turned down
                        waiting = 0 if item.get('fixes_applied') else sum(1 for p in inbox.fix_proposals(file_hash)['proposals'] if str(p.get('id')) not in rejected)
                    except Exception:
                        waiting = 0
                    try:
                        repeats = inbox.repeats_elsewhere(file_hash, index_old=False)
                    except Exception:
                        repeats = []
                    facts = dict(recommend.report_facts(item, summarize(item), waiting), vins_on_other_days=repeats[:10], vins_on_other_days_count=len(repeats))
                    cached = inbox.setting(f'recs|{file_hash}', None)
                out, save = recommend.answer(cached, facts, recommend.report_rules, 'single end-of-shift report', payload.get('ask_ai') is True, ai_review_configured())
                if save:
                    with self.store().session() as directory:
                        SpreadsheetInbox(directory).set_setting(f'recs|{file_hash}', save)
                return self.send(200, encode_json(out))
            if route == '/api/spreadsheets/review':
                payload = json.loads(raw)
                file_hash = payload.get('file_hash') if isinstance(payload, dict) else None
                if not isinstance(file_hash, str) or not re.fullmatch(r'[0-9a-f]{64}', file_hash):
                    raise Conflict('Valid spreadsheet version ID required.')
                with self.store().session() as directory:
                    item = SpreadsheetInbox(directory).review_action(file_hash, payload.get('action'), payload.get('by'), payload.get('note'))
                return self.send(200, encode_json({'item': item}))
            if route == '/api/kpis':
                from hosting import kpis, access
                from hosting.source_compare import identify
                from zoneinfo import ZoneInfo
                payload = json.loads(raw) or {}
                days = payload.get('days') if type(payload.get('days')) is int and payload['days'] in (7, 30, 90, 365) else 30
                end = datetime.now(ZoneInfo('America/Chicago')).date()
                with self.store().session() as directory:
                    inbox = SpreadsheetInbox(directory)
                    results = inbox.all_results()
                    from hosting.review_summary import summarize
                    context = inbox.assignment_context()
                    results = [dict(r, _statistics_assignment=inbox.assignment_for(r, summarize(r, with_issues=False), context)) for r in results]
                    if not self.is_admin():
                        results = [r for r in results if self.can_report(r['_statistics_assignment'], identify(r.get('filename'), r)[0])]
                clients = self.clients_map()
                client = payload.get('client') if isinstance(payload.get('client'), str) and len(payload.get('client')) < 200 else None
                job = payload.get('job') if isinstance(payload.get('job'), str) and len(payload.get('job')) < 200 else None
                return self.send(200, encode_json(kpis.build(results, days, end, clients, lambda j: access.client_of(j, clients), client or None, job or None)))
            if route.startswith('/api/overview'):
                from hosting import overview as ov
                from hosting import dashboard as dash_mod
                from hosting.source_compare import identify
                from urllib.parse import quote
                from zoneinfo import ZoneInfo
                payload = json.loads(raw) or {}
                days = payload.get('days') if type(payload.get('days')) is int and payload['days'] in (7, 30, 90, 365) else 30
                end = datetime.now(ZoneInfo('America/Chicago')).date()
                with self.store().session() as directory:
                    inbox = SpreadsheetInbox(directory)
                    results = inbox.all_results()
                    from hosting.review_summary import summarize
                    context = inbox.assignment_context()
                    results = [dict(r, _statistics_assignment=inbox.assignment_for(r, summarize(r, with_issues=False), context)) for r in results]
                    if not self.is_admin():
                        results = [r for r in results if self.can_report(r['_statistics_assignment'], identify(r.get('filename'), r)[0])]
                o = ov.build(results, days, end)
                if route == '/api/overview':
                    from hosting import recommend
                    facts = recommend.overview_facts(o)
                    with self.store().session() as directory:
                        cached = SpreadsheetInbox(directory).setting(f'recs|overview|{days}', None)
                    recs, save = recommend.answer(cached, facts, lambda f: recommend.overview_rules(o), f'whole-operation overview ({o["label"]})', payload.get('ask_ai') is True, ai_review_configured())
                    if save:
                        with self.store().session() as directory:
                            SpreadsheetInbox(directory).set_setting(f'recs|overview|{days}', save)
                    return self.send(200, encode_json(dict(o, recs=recs)))
                what = 'slides' if payload.get('what') == 'slides' else 'dashboard'
                art_key = ov.key(days, end, what)
                def art_reply(d):
                    return self.send(200, encode_json(dict({k: d[k] for k in ('undo', 'messages', 'updated_at')}, ai_on=ai_review_configured(), what=what,
                                                           view=f'/api/artifacts/view?key={quote(art_key)}&v={quote(d["updated_at"])}',
                                                           download=f'/api/artifacts/view?key={quote(art_key)}&download=1')))
                with self.store().session() as directory:
                    current = SpreadsheetInbox(directory).dashboard(art_key)
                if route.endswith('/undo'):
                    with self.store().session() as directory:
                        return art_reply(SpreadsheetInbox(directory).undo_dashboard(art_key))
                if route == '/api/overview/artifact':
                    if current and payload.get('rebuild') is not True:
                        return art_reply(current)
                    page = dash_mod.build_overview_slides(o) if what == 'slides' else dash_mod.build_overview_page(o)
                    with self.store().session() as directory:
                        return art_reply(SpreadsheetInbox(directory).save_dashboard(art_key, page, reset=True))
                request = ' '.join(str(payload.get('message') or '').split())[:2000]
                if not request or not current:
                    raise Conflict('Say what to change.' if current else 'Create it first.')
                from hosting import ai_review
                try:
                    page, reply = ai_review.edit_dashboard(current['html'], o, current['messages'], request, what=what)
                except (ai_review.AIReviewNotConfigured, ai_review.AIReviewError) as exc:
                    raise Conflict(str(exc))
                now = datetime.now(timezone.utc).isoformat()
                with self.store().session() as directory:
                    return art_reply(SpreadsheetInbox(directory).save_dashboard(art_key, page, [dict(role='user', text=request, at=now), dict(role='assistant', text=reply, at=now)]))
            if route == '/api/vins/search':
                payload = json.loads(raw)
                with self.store().session() as directory:
                    found = SpreadsheetInbox(directory).find_vin((payload or {}).get('q'))
                if not self.is_admin():
                    found['results'] = [x for x in found['results'] if self.can_job(x.get('job'))]
                return self.send(200, encode_json(found))
            if route.startswith('/api/periods'):
                from hosting import periods
                from urllib.parse import quote
                payload = json.loads(raw)
                if not isinstance(payload, dict):
                    raise Conflict('Send a JSON object.')
                kind = payload.get('kind') if payload.get('kind') in periods.KINDS else 'week'
                try:
                    start, end, label = periods.bounds(kind, payload.get('day') or datetime.now(timezone.utc).date().isoformat())
                except ValueError:
                    raise Conflict('Pick a valid date.')
                with self.store().session() as directory:
                    inbox = SpreadsheetInbox(directory)
                    jobs = [j for j in inbox.jobs() if self.can_job(j)]
                    job = ' '.join(str(payload.get('job') or '').split()).upper() or (jobs[0] if jobs else '')
                    if not job:
                        raise Conflict('Upload a daily report first, so there is a job to look at.')
                    reports = inbox.reports_for(job, start, end)
                    schedule = inbox.shift_schedule(job)
                    file_check = inbox.setting(f'filecheck|{job}|{kind}|{start.isoformat()}', None)
                sp_days, sp_note = periods.sharepoint_days(job, start, end, schedule)  # network call outside any DB transaction
                period = periods.assemble(job, kind, start, end, label, reports, sp_days, sp_note)
                if len(reports) > 1:
                    try:
                        with self.store().session() as directory:
                            got = SpreadsheetInbox(directory).vins_in([r['file_hash'] for r in reports.values()])
                        by_hash = {r['file_hash']: d for d, r in reports.items()}
                        extra = periods.cross_day({by_hash[h]: v for h, v in got.items()})
                        period['findings'] = sorted(extra + period['findings'], key=lambda f: ({'high': 0, 'medium': 1, 'low': 2}[f['severity']], f['day'] or ''))
                    except Exception:
                        debug_log()
                what = 'slides' if payload.get('what') == 'slides' else 'dashboard'
                art_key = periods.key(job, kind, start, what)
                def art_reply(d):
                    return self.send(200, encode_json(dict({k: d[k] for k in ('undo', 'messages', 'updated_at')}, ai_on=ai_review_configured(), what=what,
                                                           view=f'/api/artifacts/view?key={quote(art_key)}&v={quote(d["updated_at"])}',
                                                           download=f'/api/artifacts/view?key={quote(art_key)}&download=1')))
                if route == '/api/periods':
                    with self.store().session() as directory:
                        inbox = SpreadsheetInbox(directory)
                        made = {w: bool(inbox.dashboard(periods.key(job, kind, start, w))) for w in ('dashboard', 'slides')}
                    from hosting import recommend
                    rkey = f'recs|{job}|{kind}|{start.isoformat()}'
                    with self.store().session() as directory:
                        cached = SpreadsheetInbox(directory).setting(rkey, None)
                    recs, save = recommend.answer(cached, recommend.period_facts(period), lambda f: recommend.period_rules(period),
                                                  f'{kind} of daily reports for one job', payload.get('ask_ai') is True, ai_review_configured())
                    if save:
                        with self.store().session() as directory:
                            SpreadsheetInbox(directory).set_setting(rkey, save)
                    return self.send(200, encode_json(dict(jobs=jobs, period=period, file_check=file_check, made=made, recs=recs,
                                                           ai_on=ai_review_configured(), sharepoint_on=sharepoint_configured())))
                if route == '/api/periods/check-file':
                    name, content = payload.get('filename'), payload.get('content_base64')
                    if not isinstance(name, str) or not (isinstance(content, str) or isinstance(payload.get('upload_id'), str)):
                        raise Conflict('Choose a weekly or monthly report file.')
                    if isinstance(payload.get('upload_id'), str):   # sent in parts (files up to 50 MB)
                        if not re.fullmatch(r'[0-9a-f]{32}', payload['upload_id']) or type(payload.get('part_count')) is not int or not 0 < payload['part_count'] <= 100000:
                            raise Conflict('The file could not be read. Try again.')
                        with self.store().session() as directory:
                            from hosting.files import files_for
                            chunks = files_for(directory).take_parts(payload['upload_id'])
                        if len(chunks) != payload['part_count']:
                            raise Conflict(f'Upload incomplete: {len(chunks)} of {payload["part_count"]} parts arrived. Try again.')
                        file_raw = b''.join(chunks)
                    else:
                        try:
                            file_raw = base64.b64decode(content, validate=True)
                        except ValueError:
                            raise Conflict('The file could not be read.')
                    result = periods.check_file(name, file_raw, period)
                    with self.store().session() as directory:
                        SpreadsheetInbox(directory).set_setting(f'filecheck|{job}|{kind}|{start.isoformat()}', result)
                    return self.send(200, encode_json(dict(file_check=result)))
                with self.store().session() as directory:
                    current = SpreadsheetInbox(directory).dashboard(art_key)
                if route.endswith('/undo'):
                    with self.store().session() as directory:
                        return art_reply(SpreadsheetInbox(directory).undo_dashboard(art_key))
                from hosting import dashboard as dash_mod
                if route == '/api/periods/artifact' and current and payload.get('rebuild') is not True:
                    return art_reply(current)
                dailies = []
                with self.store().session() as directory:
                    inbox = SpreadsheetInbox(directory)
                    for x in period['days']:
                        if x['report']:
                            try:
                                it = inbox.get(x['report']['file_hash'])
                                dailies.append(dash_mod.data_for(it, inbox.original_bytes(x['report']['file_hash'])[1]))
                            except Exception:
                                debug_log()
                facts = periods.data_for(period, dailies)
                if route == '/api/periods/artifact':
                    page = dash_mod.build_slides(facts) if what == 'slides' else dash_mod.build_period(facts)
                    with self.store().session() as directory:
                        return art_reply(SpreadsheetInbox(directory).save_dashboard(art_key, page, reset=True))
                request = ' '.join(str(payload.get('message') or '').split())[:2000]
                if not request or not current:
                    raise Conflict('Say what to change.' if current else 'Create it first.')
                from hosting import ai_review
                try:
                    page, reply = ai_review.edit_dashboard(current['html'], facts, current['messages'], request, what=what)
                except (ai_review.AIReviewNotConfigured, ai_review.AIReviewError) as exc:
                    raise Conflict(str(exc))
                now = datetime.now(timezone.utc).isoformat()
                with self.store().session() as directory:
                    return art_reply(SpreadsheetInbox(directory).save_dashboard(art_key, page, [dict(role='user', text=request, at=now), dict(role='assistant', text=reply, at=now)]))
            if route == '/api/spreadsheets/shift-fix':
                payload = json.loads(raw)
                file_hash = payload.get('file_hash') if isinstance(payload, dict) else None
                if not isinstance(file_hash, str) or not re.fullmatch(r'[0-9a-f]{64}', file_hash):
                    raise Conflict('Valid spreadsheet version ID required.')
                if not self.can_hash(file_hash):
                    return self.fail(404, 'Report not found.')
                from hosting.public_api import shift_fix, ApiError
                try:
                    fixed, moved, removed = shift_fix(self, file_hash)
                except ApiError as exc:
                    raise Conflict(str(exc))
                return self.send(200, encode_json({'item': fixed, 'moved': moved, 'removed': removed}))
            if route in ('/api/keys', '/api/keys/create', '/api/keys/revoke'):
                payload = json.loads(raw)
                if not isinstance(payload, dict):
                    raise Conflict('Send a JSON object.')
                with self.store().session() as directory:
                    inbox = SpreadsheetInbox(directory)
                    if route.endswith('/create'):
                        made = inbox.create_key(payload.get('name'), payload.get('can_send') is True)
                        if payload.get('connector') is True:
                            bypass = os.environ.get('VERCEL_AUTOMATION_BYPASS_SECRET', '')
                            made['connector_url'] = ('https://' if os.environ.get('VERCEL') == '1' else 'http://') + self.headers.get('Host', '') + '/mcp/' + made['key'] \
                                + ('?x-vercel-protection-bypass=' + bypass if bypass else '')
                            made['bypass_ready'] = bool(bypass) or not hosted_preview()
                        return self.send(200, encode_json({'key': made, 'keys': inbox.list_keys()}))
                    if route.endswith('/revoke'):
                        inbox.revoke_key(payload.get('id'))
                    return self.send(200, encode_json({'keys': inbox.list_keys(), 'bypass_ready': bool(os.environ.get('VERCEL_AUTOMATION_BYPASS_SECRET')) or not hosted_preview()}))
            if route in ('/api/spreadsheets/approve', '/api/spreadsheets/send-info', '/api/spreadsheets/send'):
                payload = json.loads(raw)
                file_hash = payload.get('file_hash') if isinstance(payload, dict) else None
                if not isinstance(file_hash, str) or not re.fullmatch(r'[0-9a-f]{64}', file_hash):
                    raise Conflict('Valid spreadsheet version ID required.')
                if not self.can_hash(file_hash):
                    return self.fail(404, 'Report not found.')
                from hosting import delivery
                if route.endswith('/approve'):
                    item = delivery.approve(self.store(), file_hash, payload.get('approver'), payload.get('note'), payload.get('override'), payload.get('withdraw'),
                                            expected_check_hash=payload.get('expected_check_hash'), expected_run=payload.get('expected_run'))
                    summary = None
                    if not payload.get('withdraw'):   # at the end: a PDF change summary on record when the report differed from SharePoint or was changed
                        try:
                            from hosting import change_summary
                            summary = change_summary.make(self, file_hash, payload.get('approver'), 'approved', only_if_differs=True)
                        except Exception:
                            debug_log()
                    return self.send(200, encode_json({'item': item, 'change_summary': summary}))
                if route.endswith('/send-info'):
                    return self.send(200, encode_json(delivery.info(self.store(), file_hash)))
                item, record = delivery.send(self.store(), file_hash, payload)
                return self.send(200, encode_json({'item': item, 'record': record}))
            if route in ('/api/spreadsheets/sharepoint', '/api/spreadsheets/ai-review', '/api/spreadsheets/baseline'):
                payload = json.loads(raw)
                file_hash = payload.get('file_hash') if isinstance(payload, dict) else None
                if not isinstance(file_hash, str) or not re.fullmatch(r'[0-9a-f]{64}', file_hash):
                    raise Conflict('Valid spreadsheet version ID required.')
                run = {'sharepoint': self.sharepoint_check, 'ai-review': self.ai_review, 'baseline': self.baseline_check}[route.rsplit('/', 1)[1]]
                return self.send(200, encode_json({'item': run(file_hash)}))
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
            if route in ('/api/spreadsheets/upload/finish', '/api/spreadsheets/import'):
                try:
                    item = json.loads(body)['item']
                    if not (item.get('idempotent') and (item.get('baseline') or {}).get('status')):
                        updated = self.baseline_check(item['file_hash'])
                        if item.get('idempotent'):
                            updated = dict(updated, idempotent=True)
                        body = encode_json({'item': updated})
                except Exception:
                    debug_log()  # the upload itself was saved; the comparison can be re-run from the UI
            if route in ('/api/spreadsheets/upload/finish', '/api/spreadsheets/import'):
                try:   # nothing is fixed on upload unless PPG_AUTOFORMAT_ON_UPLOAD=1; a re-upload of a cleaned file opens its cleaned copy
                    item = json.loads(body)['item']
                    if item.get('idempotent') and item.get('auto_formatted_to'):   # the same file again: open its cleaned copy
                        with self.store().session() as directory:
                            body = encode_json({'item': dict(SpreadsheetInbox(directory).get(item['auto_formatted_to']), idempotent=True)})
                    elif not item.get('idempotent') and not item.get('fixed_from') and os.environ.get('PPG_AUTOFORMAT_ON_UPLOAD') == '1':
                        from hosting import autoformat   # off by default: the person decides what gets fixed (Approve, Fix with AI, or Ignore)
                        cleaned = autoformat.run(self, item['file_hash'])
                        if cleaned:
                            body = encode_json({'item': cleaned})
                except Exception:
                    debug_log()  # the upload itself was saved; the formatting can still be fixed from the report
            # Uploading preserves the user's manual-vs-AI choice. Provider preparation
            # requires a separate explicit action; configuration is not consent.
            return self.send(200, body, content_type, headers)
        except (Conflict, IntakeError, ValueError, TypeError, KeyError) as exc:
            return self.fail(409, str(exc))
        except StorageUnavailable as exc:
            debug_log()
            return self.fail(503, str(exc))
        except Exception:
            debug_log()
            return self.fail(503, 'Something went wrong on the server, so nothing was saved. Reload and try again; if it keeps happening, tell your admin.')
