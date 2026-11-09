"""Real HTTP handler, synthetic in-memory backing store; no hosted service or secrets."""
import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from api.index import handler
from hosting import access
from hosting.spreadsheet_inbox import SpreadsheetInbox
from legacy_storage_fake import PostgresWorkspace
from test_vercel import FakeDatabase


class HostedAuthBoundary(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            'VERCEL': '1', 'VERCEL_ENV': 'preview', 'CI_VERCEL_PROTECTED_PREVIEW': '1',
            'VERCEL_URL': 'synthetic.vercel.app', 'VERCEL_BRANCH_URL': 'branch.vercel.app',
        }, clear=True)
        self.env.start()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        self.server.workspace_store = PostgresWorkspace('postgresql://test.invalid/x', FakeDatabase().connect)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.env.stop()

    def call(self, path, body=None, cookie=None, headers=None):
        hs = {'Host': 'synthetic.vercel.app', 'Origin': 'https://synthetic.vercel.app',
              'Content-Type': 'application/json', 'X-Review-Token': 'same-origin'}
        if cookie:
            hs['Cookie'] = cookie
        hs.update(headers or {})
        req = urllib.request.Request(self.base + path, headers=hs,
                                     data=None if body is None else json.dumps(body).encode())
        try:
            r = self.opener.open(req, timeout=10)
        except urllib.error.HTTPError as e:
            r = e
        with r:
            raw = r.read()
            try:
                data = json.loads(raw)
            except ValueError:
                data = raw.decode()
            return r.status, data

    def seed(self, required=None):
        with self.server.workspace_store.session() as directory:
            inbox = SpreadsheetInbox(directory)
            if required is not None:
                inbox.set_setting('access_required', required)
            user = access.save_user(inbox, dict(email='admin@example.invalid', role='admin'))
            token = access.start_session(inbox, user)
            key = inbox.create_key('synthetic')
        return 'ppg_session=' + token, key['key']

    def test_missing_false_true_settings_always_require_hosted_session(self):
        for required in (None, False, True):
            with self.subTest(required=required):
                cookie, _ = self.seed(required)
                for bad_cookie in (None, 'ppg_session=invalid'):
                    status, me = self.call('/api/me', cookie=bad_cookie)
                    self.assertEqual(status, 200)
                    self.assertIsNone(me['user'])
                    self.assertTrue(me['required'])
                    self.assertTrue(me['hosted_signin_required'])
                    for path in ('/api/spreadsheets', '/api/state', '/api/workflow', '/api/spreadsheets/download?id=' + 'a'*64):
                        self.assertEqual(self.call(path, cookie=bad_cookie)[0], 401, path)
                    for path, body in (('/api/users/save', {'email':'intruder@example.invalid','role':'admin'}),
                                       ('/api/access/required', {'on':False}), ('/api/keys/create', {'name':'intruder'})):
                        self.assertEqual(self.call(path, body, cookie=bad_cookie)[0], 401, path)
                self.assertEqual(self.call('/api/spreadsheets', cookie=cookie)[0], 200)
                status, me = self.call('/api/me', cookie=cookie)
                self.assertEqual(me['user']['email'], 'admin@example.invalid')
                self.assertFalse(me['user'].get('virtual', False))
                status, users = self.call('/api/users', {}, cookie=cookie)
                self.assertEqual(status, 200)
                self.assertEqual(len(users['users']), 1)
                self.assertTrue(users['required'])
                self.assertTrue(users['hosted_signin_required'])
                self.assertEqual(self.call('/api/access/required', {'on':False}, cookie=cookie)[0], 409)

    def test_empty_workspace_fails_closed_without_bootstrap(self):
        self.assertIsNone(self.call('/api/me')[1]['user'])
        self.assertEqual(self.call('/api/users/save', {'email':'admin@example.invalid','role':'admin'})[0], 401)

    def test_spoofed_identity_headers_and_hosts_do_not_authenticate(self):
        self.seed(False)
        spoof = {'x-vercel-id':'pretend', 'x-vercel-protection-bypass':'pretend',
                 'x-vercel-authenticated-user-email':'admin@example.invalid', 'X-Forwarded-Host':'synthetic.vercel.app',
                 'Authorization':'Bearer pretend', 'Sec-Fetch-Site':'same-origin'}
        self.assertEqual(self.call('/api/state', headers=spoof)[0], 401)
        self.assertEqual(self.call('/api/keys/create', {'name':'intruder'}, headers=spoof)[0], 401)
        self.assertEqual(self.call('/api/state', headers=dict(spoof, Host='evil.invalid'))[0], 403)
        self.assertEqual(self.call('/api/state', headers={'Host':'branch.vercel.app'})[0], 401)
        for flag in ('', '0'):
            with patch.dict(os.environ, {'CI_VERCEL_PROTECTED_PREVIEW':flag}):
                self.assertEqual(self.call('/api/me')[0], 403)

    def test_connector_keys_are_independent_and_public_exceptions_survive(self):
        cookie, key = self.seed(False)
        self.assertEqual(self.call('/api/v1')[0], 200)
        self.assertEqual(self.call('/api/v1/reports')[0], 401)
        self.assertEqual(self.call('/api/v1/reports', cookie=cookie)[0], 401)
        self.assertEqual(self.call('/api/v1/reports', headers={'Authorization':'Bearer '+key})[0], 200)
        msg = {'jsonrpc':'2.0','id':1,'method':'initialize','params':{}}
        self.assertEqual(self.call('/mcp/invalid', msg, cookie=cookie)[0], 401)
        self.assertEqual(self.call('/mcp/'+key, msg)[0], 200)
        self.assertEqual(self.call('/api/health')[0], 200)
        self.assertEqual(self.call('/app')[0], 200)
        self.assertEqual(self.call('/api/auth/signout', {})[0], 200)
        self.assertEqual(self.call('/api/auth/email', {'email':'none@example.invalid'})[0], 409)

    def test_revoked_session_does_not_fall_back_to_owner(self):
        cookie, _ = self.seed(False)
        with self.server.workspace_store.session() as directory:
            access.end_session(SpreadsheetInbox(directory), cookie.split('=',1)[1])
        self.assertEqual(self.call('/api/state', cookie=cookie)[0], 401)


if __name__ == '__main__':
    unittest.main()
