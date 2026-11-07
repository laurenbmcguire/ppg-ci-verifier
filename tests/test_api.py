"""Verifier API v1: keys, upload, fixes, download, approve, send (fake Microsoft 365; nothing is sent)."""
import base64, json, os, sys, threading, unittest, urllib.error, urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
from eos_fixture import build, ROWS
from test_send import FakeMail, ENV
from hosting import sharepoint

NAME = '2230-OWENS-BASCO - EOS - 10.07.26.xlsx'


def seeded():
    rows = [list(r) for r in ROWS]
    rows[2][4] = 'Holes /  Glue '   # double + trailing space: a check fix
    return build([tuple(r) for r in rows])


class ApiTests(unittest.TestCase):
    def setUp(self):
        from api.index import handler
        from legacy_storage_fake import PostgresWorkspace
        from test_vercel import FakeDatabase
        sharepoint._token.update(value=None, expires=0)
        self.env = patch.dict(os.environ, dict(ENV, CI_LOCAL_DEV='1', VERCEL='0', PPG_AUTOFORMAT_ON_UPLOAD='1')); self.env.start()   # opt-in cleanup is covered here
        os.environ.pop('ANTHROPIC_API_KEY', None)
        self.fake = FakeMail()
        self.graph = patch('hosting.sharepoint._open', lambda request, opener=None: self.fake(request)); self.graph.start()
        self.lookup = patch('hosting.sharepoint.fetch_export', side_effect=sharepoint.SharePointNotFound('none')); self.lookup.start()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        self.server.workspace_store = PostgresWorkspace('postgresql://test.invalid/x', FakeDatabase().connect)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.graph.stop(); self.lookup.stop(); self.env.stop()
        sharepoint._token.update(value=None, expires=0)

    def app(self, path, body):   # the review screen (same-origin)
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(),
                                     headers={'Content-Type': 'application/json', 'Origin': self.base, 'X-Review-Token': 'same-origin'})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())

    def call(self, method, path, body=None, key=None, raw=False):
        headers = {'Authorization': 'Bearer ' + key} if key else {}
        if body is not None:
            headers['Content-Type'] = 'application/json'
        req = urllib.request.Request(self.base + path, method=method, headers=headers, data=None if body is None else json.dumps(body).encode())
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()
                return r.status, (data if raw else json.loads(data))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_large_upload_in_parts(self):
        import hashlib
        made = self.app('/api/keys/create', {'name': 'Big files'}); key = made['key']['key']
        raw = seeded(); digest = hashlib.sha256(raw).hexdigest()
        status, start = self.call('POST', '/api/v1/uploads', {}, key)
        self.assertEqual(status, 201); uid, size = start['upload_id'], start['part_size']
        pieces = [raw[:len(raw) // 2], raw[len(raw) // 2:]]
        for n, piece in enumerate(pieces):
            st, _ = self.call('POST', f'/api/v1/uploads/{uid}/parts/{n}', {'content_base64': base64.b64encode(piece).decode()}, key)
            self.assertEqual(st, 200)
        status, rep = self.call('POST', f'/api/v1/uploads/{uid}/finish', {'filename': NAME, 'part_count': 2, 'sha256': digest}, key)
        self.assertEqual(status, 201, rep); self.assertEqual(rep['job'], '2230-OWENS-BASCO')

    def test_full_flow(self):
        status, idx = self.call('GET', '/api/v1')
        self.assertEqual(status, 200); self.assertTrue(any(e['path'] == '/api/v1/reports/{id}/send' for e in idx['endpoints']))
        self.assertEqual(self.call('GET', '/api/v1/reports')[0], 401)
        self.assertEqual(self.call('GET', '/api/v1/reports', key='ppgv_wrong')[0], 401)
        made = self.app('/api/keys/create', {'name': 'Power Automate'})
        key = made['key']['key']; self.assertTrue(key.startswith('ppgv_')); self.assertFalse(made['key']['can_send'])
        self.assertNotIn('key', made['keys'][0])                                   # the key is shown once only
        # upload
        upload = {'filename': NAME, 'content_base64': base64.b64encode(seeded()).decode()}   # built once: the file carries a timestamp
        status, rep = self.call('POST', '/api/v1/reports', upload, key)
        self.assertEqual(status, 201, rep); rid = rep['id']
        self.assertEqual(rep['job'], '2230-OWENS-BASCO'); self.assertIn(rep['status'], ('hold', 'check', 'clear'))
        # formatting-only fixes are made on upload: the report that comes back is the cleaned copy
        self.assertTrue(rep['auto_formatting']['count']); self.assertTrue(rep['corrected_from'])
        oid = rep['corrected_from']['id']
        status, again = self.call('POST', '/api/v1/reports', upload, key)
        self.assertEqual(status, 200); self.assertTrue(again['already_uploaded']); self.assertEqual(again['id'], rid)
        status, lst = self.call('GET', '/api/v1/reports?job=2230-OWENS-BASCO', key=key)
        self.assertEqual(lst['reports'][0]['id'], rid)
        status, orig = self.call('GET', f'/api/v1/reports/{oid}', key=key)
        self.assertEqual(orig['corrected_copy']['id'], rid)
        status, fx = self.call('GET', f'/api/v1/reports/{rid}/fixes', key=key)
        self.assertEqual(status, 200); self.assertFalse([f for f in fx['fixes'] if f.get('sure')])   # nothing sure left to do
        fixed = rep
        status, body = self.call('GET', f'/api/v1/reports/{fixed["id"]}/download', key=key, raw=True)
        self.assertEqual(status, 200); self.assertTrue(body.startswith(b'PK'))
        # approve, preview, send
        status, sent = self.call('POST', f'/api/v1/reports/{fixed["id"]}/send', {'mode': 'draft'}, key)
        self.assertEqual(status, 403)                                              # this key cannot send
        sender = self.app('/api/keys/create', {'name': 'Sender', 'can_send': True})['key']['key']
        status, sent = self.call('POST', f'/api/v1/reports/{fixed["id"]}/send', {'mode': 'send'}, sender)
        self.assertEqual(status, 409); self.assertIn('Approve', sent['error'])
        status, ok = self.call('POST', f'/api/v1/reports/{fixed["id"]}/approve', {'approver': 'Chris', 'override': True}, key)
        self.assertEqual(status, 200, ok); self.assertEqual(ok['approval']['by'], 'Chris')
        status, prev = self.call('GET', f'/api/v1/reports/{fixed["id"]}/send', key=key)
        self.assertEqual(prev['to'], ['buyer@client.com']); self.assertEqual(prev['attachment']['name'], NAME)   # no "(fixed)"
        status, sent = self.call('POST', f'/api/v1/reports/{fixed["id"]}/send', {'mode': 'send'}, sender)
        self.assertEqual(status, 200, sent)
        self.assertTrue(sent['record']['reply_all']); self.assertEqual(sent['record']['via'], 'api:Sender')
        self.assertEqual(sent['record']['to'], ['buyer@client.com'])
        self.assertEqual(self.call('POST', f'/api/v1/reports/{fixed["id"]}/send', {'mode': 'send'}, sender)[0], 409)
        # revoke
        kid = [k for k in self.app('/api/keys', {})['keys'] if k['name'] == 'Power Automate'][0]['id']
        self.app('/api/keys/revoke', {'id': kid})
        self.assertEqual(self.call('GET', '/api/v1/reports', key=key)[0], 401)

    def test_errors(self):
        key = self.app('/api/keys/create', {'name': 'k'})['key']['key']
        self.assertEqual(self.call('GET', '/api/v1/reports/' + 'a' * 64, key=key)[0], 404)
        self.assertEqual(self.call('GET', '/api/v1/nope', key=key)[0], 404)
        self.assertEqual(self.call('POST', '/api/v1/reports', {'filename': 'x.xlsx'}, key)[0], 400)
        self.assertEqual(self.call('POST', '/api/v1/reports', {'filename': 'x.xlsx', 'content_base64': '!!'}, key)[0], 400)


if __name__ == '__main__':
    unittest.main()
