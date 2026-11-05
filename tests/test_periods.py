"""Weekly / monthly: the period from daily reports, its checks, a weekly file check, and its dashboard and slide show."""
import base64, json, os, sys, threading, unittest, urllib.error, urllib.parse, urllib.request
from datetime import date
from http.server import ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
from eos_fixture import build
from hosting import ai_review, periods


class PeriodUnitTests(unittest.TestCase):
    def test_bounds(self):
        self.assertEqual(periods.bounds('week', '2026-10-07')[:2], (date(2026, 10, 5), date(2026, 10, 11)))
        self.assertEqual(periods.bounds('week', '2026-10-07')[2], 'Week of October 5 to 11, 2026')
        self.assertEqual(periods.bounds('month', '2026-02-10')[:2], (date(2026, 2, 1), date(2026, 2, 28)))
        k = periods.key('2230-owens-basco', 'week', date(2026, 10, 5), 'slides')
        self.assertEqual(periods.parse_key(k), ('2230-OWENS-BASCO', 'week', date(2026, 10, 5), 'slides'))
        self.assertIsNone(periods.parse_key('period|x|week|2026-10-05|evil'))


class PeriodRouteTests(unittest.TestCase):
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
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_week(self):
        for d in ('2026-10-05', '2026-10-06', '2026-10-07'):
            name = f'2230-OWENS-BASCO - EOS - {d}.xlsx'
            status, data = self.post('/api/spreadsheets/import', {'filename': name, 'content_base64': base64.b64encode(build(day=d)).decode()})
            self.assertEqual(status, 200, data)
        status, p = self.post('/api/periods', {'job': '2230-owens-basco', 'kind': 'week', 'day': '2026-10-07'})
        self.assertEqual(status, 200, p)
        self.assertIn('2230-OWENS-BASCO', p['jobs'])
        period = p['period']
        self.assertEqual(period['label'], 'Week of October 5 to 11, 2026')
        self.assertEqual(period['totals']['reports'], 3); self.assertEqual(period['totals']['inspected'], 438 * 3)
        self.assertTrue(any('Not approved or sent' in f['text'] for f in period['findings']))
        # a weekly file with only one day in it
        weekly = build(day='2026-10-07')
        status, fc = self.post('/api/periods/check-file', {'job': '2230-OWENS-BASCO', 'kind': 'week', 'day': '2026-10-07',
                                                         'filename': 'OWENS weekly.xlsx', 'content_base64': base64.b64encode(weekly).decode()})
        self.assertEqual(status, 200, fc); fc = fc['file_check']
        self.assertEqual(fc['status'], 'hold')
        self.assertEqual(sum(1 for f in fc['findings'] if 'missing from the file' in f['text']), 2)
        self.assertTrue(any('Total parts inspected in the file: 438' in f['text'] for f in fc['findings']))
        status, p2 = self.post('/api/periods', {'job': '2230-OWENS-BASCO', 'kind': 'week', 'day': '2026-10-06'})
        self.assertEqual(p2['file_check']['filename'], 'OWENS weekly.xlsx')                 # remembered for the week
        # dashboard and slides
        for what, marker in (('dashboard', 'Weekly dashboard'), ('slides', 'class="slide title"')):
            status, a = self.post('/api/periods/artifact', {'job': '2230-OWENS-BASCO', 'kind': 'week', 'day': '2026-10-07', 'what': what})
            self.assertEqual(status, 200, a)
            with urllib.request.urlopen(self.base + a['view'], timeout=30) as r:
                page = r.read().decode(); csp = r.headers['Content-Security-Policy']
            self.assertIn(marker, page); self.assertIn('1,314', page); self.assertIn('sandbox', csp)
            with urllib.request.urlopen(self.base + a['download'], timeout=30) as r:
                self.assertIn(f'2230-OWENS-BASCO Weekly {"Slides" if what == "slides" else "Dashboard"} 2026-10-05.html', r.headers['Content-Disposition'])
        def fake_post(body, opener=None, timeout=None):
            self.assertIn('HTML slide show', body['system'])
            return {'content': [{'type': 'tool_use', 'input': {'reply': 'Added a summary slide.', 'html': '<!doctype html><html><body><section class="slide">new</section></body></html>'}}]}
        with patch.object(ai_review, '_post', fake_post), patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'x'}):
            status, a = self.post('/api/periods/artifact/chat', {'job': '2230-OWENS-BASCO', 'kind': 'week', 'day': '2026-10-07', 'what': 'slides', 'message': 'Add a summary slide'})
        self.assertEqual(status, 200, a); self.assertTrue(a['undo']); self.assertEqual(a['messages'][-1]['text'], 'Added a summary slide.')
        status, a = self.post('/api/periods/artifact/undo', {'job': '2230-OWENS-BASCO', 'kind': 'week', 'day': '2026-10-07', 'what': 'slides'})
        self.assertEqual(status, 200); self.assertFalse(a['undo'])
        status, p3 = self.post('/api/periods', {'job': '2230-OWENS-BASCO', 'kind': 'month', 'day': '2026-10-07'})
        self.assertEqual(p3['period']['label'], 'October 2026'); self.assertEqual(p3['made'], {'dashboard': False, 'slides': False})


if __name__ == '__main__':
    unittest.main()
