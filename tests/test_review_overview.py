"""Review and sign-off, and the executive overview."""
import base64, json, os, sys, threading, unittest, urllib.error, urllib.request
from datetime import date, timedelta
from http.server import ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
from eos_fixture import build, ROWS
from hosting import overview

from test_send import displayed_binding


class ReviewOverviewTests(unittest.TestCase):
    def setUp(self):
        from api.index import handler
        from legacy_storage_fake import PostgresWorkspace
        from test_vercel import FakeDatabase
        self.env = patch.dict(os.environ, {'CI_LOCAL_DEV': '1', 'VERCEL': '0'}); self.env.start()
        assignments = patch('hosting.spreadsheet_inbox.SpreadsheetInbox.assignment_for', side_effect=lambda r,rv,ctx=None:
                            dict(status='manual',job=rv['job'],business_date=rv['day'],client_key='synthetic',site_id='synthetic',list_id=rv['job']))
        assignments.start(); self.addCleanup(assignments.stop)
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
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def upload(self, day, rows=ROWS):
        status, data = self.post('/api/spreadsheets/import', {'filename': f'2230-OWENS-BASCO - EOS - {day}.xlsx', 'content_base64': base64.b64encode(build([tuple(r) for r in rows], day=day)).decode()})
        self.assertEqual(status, 200, data)
        return data['item']['file_hash']

    def test_review_flow(self):
        h = self.upload('2026-10-07')
        self.assertEqual(self.post('/api/spreadsheets/review', {'file_hash': h, 'action': 'request', 'by': ''})[0], 409)
        status, d = self.post('/api/spreadsheets/review', {'file_hash': h, 'action': 'request', 'by': 'Lauren', 'note': 'Please check totals'})
        self.assertEqual(d['item']['review_request']['status'], 'waiting')
        with urllib.request.urlopen(self.base + '/api/spreadsheets', timeout=30) as r:
            row = [x for x in json.loads(r.read())['spreadsheets'] if x['file_hash'] == h][0]
        self.assertEqual(row['review']['review_state'], 'waiting')
        self.assertEqual(self.post('/api/spreadsheets/review', {'file_hash': h, 'action': 'send_back', 'by': 'Chris'})[0], 409)   # needs a comment
        status, d = self.post('/api/spreadsheets/review', {'file_hash': h, 'action': 'send_back', 'by': 'Chris', 'note': 'Row 18 VIN is wrong'})
        self.assertEqual(d['item']['review_request']['status'], 'sent_back'); self.assertEqual(d['item']['review_request']['comment'], 'Row 18 VIN is wrong')
        self.post('/api/spreadsheets/review', {'file_hash': h, 'action': 'request', 'by': 'Lauren', 'note': 'Fixed'})
        status, d = self.post('/api/spreadsheets/approve', {'file_hash': h, 'approver': 'Chris', 'override': True, 'note': 'Synthetic fixture reviewed', **displayed_binding(self.base, h)})
        rr = d['item']['review_request']
        self.assertEqual((rr['status'], rr['reviewer']), ('approved', 'Chris')); self.assertEqual([x['action'] for x in rr['history']], ['request', 'send_back', 'request', 'approve'])

    def test_overview(self):
        today = date.today()
        d1, d2 = (today - timedelta(days=2)).isoformat(), (today - timedelta(days=1)).isoformat()
        rows = [list(r) for r in ROWS]; rows[1][0] = rows[0][0]                       # a duplicate VIN to catch
        bad = self.upload(d1, rows)
        status, fx = self.post('/api/spreadsheets/fixes', {'file_hash': bad})
        self.upload(d2)
        status, o = self.post('/api/overview', {'days': 7})
        self.assertEqual(status, 200, o)
        self.assertEqual(o['totals']['reports'], 2); self.assertEqual(o['totals']['jobs'], 1)
        self.assertGreaterEqual(o['totals']['caught'], 1)
        self.assertIn('Duplicate VINs or rows', [c['reason'] for c in o['categories']])
        for what, marker in (('slides', 'Quality overview'), ('dashboard', 'What was caught')):
            status, a = self.post('/api/overview/artifact', {'days': 7, 'what': what})
            self.assertEqual(status, 200, a)
            with urllib.request.urlopen(self.base + a['view'], timeout=30) as r:
                self.assertIn(marker, r.read().decode())
            with urllib.request.urlopen(self.base + a['download'], timeout=30) as r:
                self.assertIn('PPG Quality Overview', r.headers['Content-Disposition'])

    def test_categories(self):
        self.assertEqual(overview.category({'code': 'duplicate_identifier'}), 'Duplicate VINs or rows')
        self.assertEqual(overview.category({'code': 'wrong_shift'}), 'Scans on the wrong shift')
        self.assertEqual(overview.category({'source': 'AI review', 'code': 'ai'}), 'Found by AI review')


if __name__ == '__main__':
    unittest.main()
