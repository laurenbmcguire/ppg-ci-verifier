import base64, io, json, os, sys, tempfile, threading, unittest, urllib.error, urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
from eos_fixture import build
from hosting import ai_review
from hosting.spreadsheet_inbox import SpreadsheetInbox

NAME = '2230-OWENS-BASCO - EOS - 10.07.26.xlsx'


class FakeClaude:
    def __init__(self, issues=None, status=200):
        self.issues = issues if issues is not None else [
            dict(severity='low', category='typo', sheet='1st Shift', row=17, issue='"Holes / Glue" spacing', why='x', check='y'),
            dict(severity='high', category='outlier', sheet='1st Shift', row=24, cell='J24', issue='Reject 29 of 34', why='85% reject rate', check='Confirm count'),
            dict(severity='weird', category='nope', issue='bad enums are normalised', why='', check='')]
        self.status, self.requests = status, []

    def __call__(self, request, timeout=None):
        self.requests.append(json.loads(request.data))
        assert request.get_header('X-api-key') == 'test-key'
        if self.status != 200:
            raise urllib.error.HTTPError(request.full_url, self.status, 'err', {}, io.BytesIO(b'{}'))
        body = {'model': 'claude-sonnet-5-5', 'content': [{'type': 'tool_use', 'name': 'report_possible_issues',
                                                          'input': {'summary': 'Mostly clean.', 'issues': self.issues}}]}
        return io.BytesIO(json.dumps(body).encode())


class AIReviewTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-key'}); self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_sheet_text_shows_formula_results_and_cells(self):
        text, truncated = ai_review.sheet_text(NAME, build())
        self.assertFalse(truncated)
        self.assertIn('8010808539', text)
        self.assertIn('→', text)  # formula with its saved result

    def test_review_sends_report_and_normalises_issues(self):
        with tempfile.TemporaryDirectory() as root:
            inbox = SpreadsheetInbox(root)
            raw = build(); item = inbox.ingest(NAME, raw)
            fake = FakeClaude()
            out = ai_review.review(item, raw, opener=fake)
            sent = fake.requests[0]
            self.assertEqual(sent['tool_choice']['name'], 'report_possible_issues')
            self.assertIn('8010808539', sent['messages'][0]['content'])
            self.assertEqual([i['severity'] for i in out['issues']], ['high', 'medium', 'low'])  # sorted, bad enum -> medium
            self.assertEqual(out['issues'][1]['category'], 'other')
            self.assertEqual(out['counts'], {'high': 1, 'medium': 1, 'low': 1})
            saved = inbox.record_ai_review(item['file_hash'], out)
            self.assertEqual(saved['ai_review']['status'], 'done')
            self.assertEqual(saved['internal_check_status'], item['internal_check_status'])  # suggestions never change status
            # A failed retry keeps the earlier review.
            kept = inbox.record_ai_review(item['file_hash'], dict(status='error', message='HTTP 529'))
            self.assertEqual(kept['ai_review']['status'], 'done')

    def test_errors_never_leak_key(self):
        with tempfile.TemporaryDirectory() as root:
            inbox = SpreadsheetInbox(root); raw = build(); item = inbox.ingest(NAME, raw)
            with self.assertRaises(ai_review.AIReviewError) as ctx:
                ai_review.review(item, raw, opener=FakeClaude(status=401))
            self.assertIn('HTTP 401', str(ctx.exception)); self.assertNotIn('test-key', str(ctx.exception))

    def test_not_configured(self):
        os.environ.pop('ANTHROPIC_API_KEY')
        self.assertFalse(ai_review.configured())
        with tempfile.TemporaryDirectory() as root:
            inbox = SpreadsheetInbox(root); raw = build(); item = inbox.ingest(NAME, raw)
            with self.assertRaises(ai_review.AIReviewNotConfigured):
                ai_review.review(item, raw, opener=FakeClaude())


class RouteTests(unittest.TestCase):
    def setUp(self):
        from api.index import handler
        from legacy_storage_fake import PostgresWorkspace
        from test_vercel import FakeDatabase
        self.env = patch.dict(os.environ, {'CI_LOCAL_DEV': '1', 'VERCEL': '0'}); self.env.start()
        for key in ('MS_TENANT_ID', 'MS_CLIENT_ID', 'MS_CLIENT_SECRET', 'ANTHROPIC_API_KEY'):
            os.environ.pop(key, None)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        self.server.workspace_store = PostgresWorkspace('postgresql://test.invalid/x', FakeDatabase().connect)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(); self.env.stop()

    def post(self, path, body):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(),
                                     headers={'Content-Type': 'application/json', 'Origin': self.base, 'X-Review-Token': 'same-origin'})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_ai_review_route(self):
        status, data = self.post('/api/spreadsheets/import', {'filename': NAME, 'content_base64': base64.b64encode(build()).decode()})
        self.assertEqual(status, 200)
        file_hash = data['item']['file_hash']
        self.assertNotIn('ai_review', data['item'])  # not configured: upload does not try
        status, data = self.post('/api/spreadsheets/ai-review', {'file_hash': file_hash})
        self.assertEqual(status, 200); self.assertEqual(data['item']['ai_review']['status'], 'not_configured')
        os.environ['ANTHROPIC_API_KEY'] = 'test-key'
        fake = FakeClaude()
        real = urllib.request.urlopen
        with patch('hosting.ai_review.urllib.request.urlopen', side_effect=lambda req, timeout=None: fake(req, timeout) if req.full_url == ai_review.API else real(req, timeout=timeout)):
            status, data = self.post('/api/spreadsheets/ai-review', {'file_hash': file_hash})
        self.assertEqual(status, 200)
        self.assertEqual(data['item']['ai_review']['status'], 'done')
        self.assertEqual(len(data['item']['ai_review']['issues']), 3)
        status, data = self.post('/api/spreadsheets/ai-review', {'file_hash': 'nope'})
        self.assertEqual(status, 409)


if __name__ == '__main__':
    unittest.main()
