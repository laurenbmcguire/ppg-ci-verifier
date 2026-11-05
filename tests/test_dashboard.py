"""Create dashboard: built from the report (no AI), viewed in a sandbox, changed by Claude on request, undo, download."""
import base64, json, os, sys, threading, unittest, urllib.error, urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
from eos_fixture import build
from hosting import ai_review

NAME = '2230-OWENS-BASCO - EOS - 10.07.26.xlsx'


class DashboardTests(unittest.TestCase):
    def setUp(self):
        from api.index import handler
        from legacy_storage_fake import PostgresWorkspace
        from test_vercel import FakeDatabase
        self.env = patch.dict(os.environ, {'CI_LOCAL_DEV': '1', 'VERCEL': '0'}); self.env.start()
        for k in ('MS_TENANT_ID', 'MS_CLIENT_ID', 'MS_CLIENT_SECRET', 'ANTHROPIC_API_KEY'):
            os.environ.pop(k, None)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        self.server.workspace_store = PostgresWorkspace('postgresql://test.invalid/x', FakeDatabase().connect)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.env.stop()

    def post(self, path, body):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(),
                                     headers={'Content-Type': 'application/json', 'Origin': self.base, 'X-Review-Token': 'same-origin'})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=30) as r:
            return r.headers, r.read().decode()

    def test_create_view_change_undo_download(self):
        status, data = self.post('/api/spreadsheets/import', {'filename': NAME, 'content_base64': base64.b64encode(build()).decode()})
        h = data['item']['file_hash']
        status, d = self.post('/api/spreadsheets/dashboard', {'file_hash': h})
        self.assertEqual(status, 200, d); self.assertFalse(d['undo'])
        headers, page = self.get(d['view'])
        self.assertIn('2230-OWENS-BASCO', page); self.assertIn('438', page); self.assertIn('Holes / Glue', page)
        self.assertNotIn('Robert', page)                                         # no inspector names
        self.assertIn('sandbox allow-scripts', headers['Content-Security-Policy']); self.assertIn("default-src 'none'", headers['Content-Security-Policy'])
        self.assertIsNone(headers.get('X-Frame-Options'))
        headers, _ = self.get(f'/api/spreadsheets/dashboard/view?id={h}&download=1')
        self.assertIn('attachment; filename="2230-OWENS-BASCO Dashboard 2026-10-07.html"', headers['Content-Disposition'])
        # change it: no AI key yet
        status, err = self.post('/api/spreadsheets/dashboard/chat', {'file_hash': h, 'message': 'Make the header navy'})
        self.assertEqual(status, 409); self.assertIn('ANTHROPIC_API_KEY', err['error'])
        seen = {}
        def fake_post(body, opener=None, timeout=None):
            seen['prompt'] = body['messages'][0]['content']; seen['timeout'] = timeout
            return {'content': [{'type': 'tool_use', 'input': {'reply': 'Made the header navy.', 'html': '<!doctype html><html><body style="background:navy">438</body></html>'}}]}
        with patch.object(ai_review, '_post', fake_post), patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'x'}):
            status, d2 = self.post('/api/spreadsheets/dashboard/chat', {'file_hash': h, 'message': 'Make the header navy'})
        self.assertEqual(status, 200, d2); self.assertTrue(d2['undo'])
        self.assertEqual([m['role'] for m in d2['messages']], ['user', 'assistant'])
        self.assertIn('"inspected": 438', seen['prompt']); self.assertIn('<current_page>', seen['prompt']); self.assertEqual(seen['timeout'], 280)
        self.assertIn('navy', self.get(d2['view'])[1])
        status, d3 = self.post('/api/spreadsheets/dashboard/undo', {'file_hash': h})
        self.assertEqual(status, 200); self.assertNotIn('navy', self.get(d3['view'])[1])
        self.assertEqual(self.post('/api/spreadsheets/dashboard/undo', {'file_hash': h})[0], 409)
        status, d4 = self.post('/api/spreadsheets/dashboard', {'file_hash': h, 'rebuild': True})
        self.assertEqual(d4['messages'], [])

    def test_bad_ai_page_changes_nothing(self):
        def fake_post(body, opener=None, timeout=None):
            return {'content': [{'type': 'tool_use', 'input': {'reply': 'ok', 'html': 'just text'}}]}
        with patch.object(ai_review, '_post', fake_post), patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'x'}):
            with self.assertRaises(ai_review.AIReviewError):
                ai_review.edit_dashboard('<!doctype html><html></html>', {}, [], 'x')


if __name__ == '__main__':
    unittest.main()
