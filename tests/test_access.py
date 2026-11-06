"""Users, client assignments and sign-in: members only ever see their own clients' data."""
import base64, json, os, sys, threading, unittest, urllib.error, urllib.parse, urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
from eos_fixture import build, ROWS

CLIENTS = [dict(key='Owens', client='Owens', sites=[], jobs=[dict(job='2230-OWENS-BASCO', numbers=['2230'], site='Owens')]),
           dict(key='Lear', client='Lear', sites=[], jobs=[dict(job='LEAR-T1-2240', numbers=['2240'], site='Lear')])]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


class Access(unittest.TestCase):
    def setUp(self):
        from api.index import handler
        from legacy_storage_fake import PostgresWorkspace
        from test_vercel import FakeDatabase
        self.env = patch.dict(os.environ, {'CI_LOCAL_DEV': '1', 'VERCEL': '0'}); self.env.start()
        for k in ('MS_TENANT_ID', 'MS_CLIENT_ID', 'MS_CLIENT_SECRET', 'ANTHROPIC_API_KEY', 'MAIL_SENDER'):
            os.environ.pop(k, None)
        self.clients = patch.object(handler, 'clients_map', lambda self: CLIENTS); self.clients.start()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        self.server.workspace_store = PostgresWorkspace('postgresql://test.invalid/x', FakeDatabase().connect)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        self.opener = urllib.request.build_opener(NoRedirect)

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.clients.stop(); self.env.stop()

    def call(self, path, body=None, cookie=None):
        headers = {'Content-Type': 'application/json', 'Origin': self.base, 'X-Review-Token': 'same-origin'}
        if cookie:
            headers['Cookie'] = cookie
        req = urllib.request.Request(self.base + path, data=None if body is None else json.dumps(body).encode(), headers=headers)
        try:
            with self.opener.open(req, timeout=60) as r:
                return r.status, json.loads(r.read() or b'{}'), r.headers
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                data = json.loads(raw or b'{}')
            except ValueError:
                data = {}
            return e.code, data, e.headers

    def upload(self, name, day='2026-10-07'):
        status, out, _ = self.call('/api/spreadsheets/import', {'filename': name, 'content_base64': base64.b64encode(build([tuple(r) for r in ROWS], day=day)).decode()})
        self.assertEqual(status, 200, out)
        return out['item']['file_hash']

    def sign_in(self, user_id, cookie=None):
        status, out, _ = self.call('/api/users/link', {'id': user_id}, cookie)
        self.assertEqual(status, 200, out)
        path = urllib.parse.urlsplit(out['link'])
        status, _, headers = self.call(path.path + '?' + path.query)
        self.assertEqual(status, 302)
        return headers['Set-Cookie'].split(';')[0], out['link']

    def test_members_only_see_their_clients(self):
        owens = self.upload('2230-OWENS-BASCO - EOS - 2026-10-07.xlsx')
        lear = self.upload('LEAR-T1-2240 - EOS - 2026-10-07.xlsx', day='2026-10-08')
        status, me, _ = self.call('/api/me')
        self.assertTrue(me['user']['virtual'])                                  # not required yet: Vercel is the gate
        st, admin, _ = self.call('/api/users/save', {'email': 'Lauren.McGuire@ppgquality.com', 'name': 'Lauren', 'role': 'admin'})
        self.assertEqual(st, 200, admin)
        st, out, _ = self.call('/api/users/save', {'email': 'sam@ppgquality.com', 'role': 'member', 'clients': ['Owens', 'Bogus']})
        member = next(u for u in out['users'] if u['email'] == 'sam@ppgquality.com')
        self.assertEqual(member['clients'], ['Owens'])                          # unknown clients are dropped
        lauren = next(u for u in out['users'] if u['role'] == 'admin')
        self.assertEqual(self.call('/api/access/required', {'on': True})[0], 409)   # can't turn on without signing in yourself
        admin_cookie, link = self.sign_in(lauren['id'])
        st, out, _ = self.call('/api/access/required', {'on': True}, admin_cookie)
        self.assertEqual(st, 200, out); self.assertTrue(out['required'])
        path = urllib.parse.urlsplit(link)
        self.assertEqual(self.call(path.path + '?' + path.query)[2]['Location'].startswith('/app#signin='), True)   # links work once
        self.assertEqual(self.call('/api/spreadsheets')[0], 401)               # no sign-in, no data
        sam, _ = self.sign_in(member['id'], admin_cookie)
        st, listed, _ = self.call('/api/spreadsheets', cookie=sam)
        self.assertEqual([r['file_hash'] for r in listed['spreadsheets']], [owens])
        self.assertEqual(self.call('/api/spreadsheet?id=' + lear, cookie=sam)[0], 404)
        self.assertEqual(self.call('/api/spreadsheets/download?id=' + lear, cookie=sam)[0], 404)
        self.assertEqual(self.call('/api/spreadsheet?id=' + owens, cookie=sam)[0], 200)
        self.assertEqual(self.call('/api/spreadsheets/fixes', {'file_hash': lear}, sam)[0], 404)
        self.assertEqual(self.call('/api/periods', {'kind': 'week', 'day': '2026-10-08', 'job': 'LEAR-T1-2240'}, sam)[0], 403)
        st, p, _ = self.call('/api/periods', {'kind': 'week', 'day': '2026-10-07'}, sam)
        self.assertEqual(st, 200, p); self.assertEqual(p['jobs'], ['2230-OWENS-BASCO'])
        st, hits, _ = self.call('/api/vins/search', {'q': ROWS[0][0]}, sam)
        self.assertTrue(hits['results']); self.assertTrue(all(h['file_hash'] == owens for h in hits['results']))
        st, o, _ = self.call('/api/overview', {'days': 90}, sam)
        self.assertEqual([j['job'] for j in o['jobs']], ['2230-OWENS-BASCO'])
        self.assertEqual(self.call('/api/users', {}, sam)[0], 403)
        self.assertEqual(self.call('/api/keys', {}, sam)[0], 403)
        self.assertEqual(self.call('/api/overview/artifact', {'days': 30}, sam)[0], 403)
        self.assertEqual(self.call('/api/today', {'add': 'LEAR-T1-2240'}, sam)[0], 403)
        st, admin_list, _ = self.call('/api/spreadsheets', cookie=admin_cookie)
        self.assertEqual(len(admin_list['spreadsheets']), 2)                   # admins see everything
        st, out, headers = self.call('/api/auth/signout', {}, sam)
        self.assertIn('Max-Age=0', headers['Set-Cookie'])
        self.assertEqual(self.call('/api/spreadsheets', cookie=sam)[0], 401)
        self.assertEqual(self.call('/api/users/remove', {'id': lauren['id']}, admin_cookie)[0], 409)   # can't remove yourself

    def test_microsoft_sign_in(self):
        os.environ.update(MS_TENANT_ID='tenant-1', MS_CLIENT_ID='client-1', MS_CLIENT_SECRET='s')
        self.call('/api/users/save', {'email': 'lauren.mcguire@ppgquality.com', 'role': 'admin'})
        st, _, headers = self.call('/auth/microsoft')
        self.assertEqual(st, 302)
        loc = urllib.parse.urlsplit(headers['Location'])
        self.assertEqual(loc.netloc, 'login.microsoftonline.com')
        state = urllib.parse.parse_qs(loc.query)['state'][0]
        state_cookie = headers['Set-Cookie'].split(';')[0]

        def token(claims):
            body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')
            class R:
                def __enter__(s): return s
                def __exit__(s, *a): return False
                def read(s): return json.dumps({'id_token': f'x.{body}.y'}).encode()
            return lambda req, timeout=None: R()
        good = dict(aud='client-1', tid='tenant-1', preferred_username='Lauren.McGuire@ppgquality.com', name='Lauren McGuire')
        with patch('urllib.request.urlopen', token(good)):
            st, _, h = self.call(f'/auth/microsoft/callback?code=abc&state={state}', cookie=state_cookie)
        self.assertEqual((st, h['Location']), (302, '/app'))
        session = [c for c in h.get_all('Set-Cookie') if c.startswith('ppg_session=')][0].split(';')[0]
        st, me, _ = self.call('/api/me', cookie=session)
        self.assertEqual(me['user']['email'], 'lauren.mcguire@ppgquality.com'); self.assertEqual(me['user']['name'], 'Lauren McGuire')
        with patch('urllib.request.urlopen', token(dict(good, preferred_username='stranger@ppgquality.com'))):
            st, _, h = self.call(f'/auth/microsoft/callback?code=abc&state={state}', cookie=state_cookie)
        self.assertIn('not%20been%20given%20access', h['Location'])
        with patch('urllib.request.urlopen', token(dict(good, tid='other-tenant'))):
            st, _, h = self.call(f'/auth/microsoft/callback?code=abc&state={state}', cookie=state_cookie)
        self.assertIn('not%20a%20PPG%20account', h['Location'])
        st, _, h = self.call(f'/auth/microsoft/callback?code=abc&state=forged', cookie=state_cookie)
        self.assertIn('signin=', h['Location'])


if __name__ == '__main__':
    unittest.main()
