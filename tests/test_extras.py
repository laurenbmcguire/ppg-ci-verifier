"""Scan timeline red flags, VIN finder and cross-day duplicates, and the Today board."""
import base64, io, json, os, sys, threading, unittest, urllib.error, urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
from eos_fixture import build
from hosting import sharepoint, shifts
from test_shifts import FakeGraph, ENV, JOB, NAME


class TimelineTests(unittest.TestCase):
    def test_flags(self):
        def it(stamp, who, pick='1'):
            return {'createdDateTime': stamp, 'createdBy': {'user': {'displayName': who}}, 'fields': {'Date0': stamp, 'ShiftPicked': pick}}
        rows = [it(f'2026-10-07T12:{m:02d}:00Z', 'Ana') for m in range(0, 50, 10)]           # 7:00-7:40 AM
        rows += [it('2026-10-07T14:30:00Z', 'Ana', '2'), it('2026-10-07T14:40:00Z', 'Ana', '1')]   # gap, pick flips 1,2,1
        rows += [it('2026-10-07T20:00:00Z', 'Ana')]                                                 # 3 PM: 2nd shift, same person
        rows += [it('2026-10-07T11:00:00Z', 'Bo')] * 0
        t = sharepoint.timeline(rows, shifts.DEFAULT)
        text = ' '.join(t['flags'])
        self.assertIn('No scans for', text); self.assertIn('flips back and forth', text); self.assertIn('Ana scanned on more than one shift', text)
        self.assertEqual(t['scans'], 8); self.assertEqual(t['shifts'][0]['name'], '1st')


class ExtrasRouteTests(unittest.TestCase):
    def setUp(self):
        from api.index import handler
        from legacy_storage_fake import PostgresWorkspace
        from test_vercel import FakeDatabase
        sharepoint._token.update(value=None, expires=0); sharepoint._list_cache.clear(); sharepoint._site_ids.clear()
        self.env = patch.dict(os.environ, dict(ENV, CI_LOCAL_DEV='1', VERCEL='0')); self.env.start()
        os.environ.pop('ANTHROPIC_API_KEY', None)
        fake = FakeGraph()
        self.graph = patch('hosting.sharepoint._open', lambda request, opener=None: fake(request)); self.graph.start()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        self.server.workspace_store = PostgresWorkspace('postgresql://test.invalid/x', FakeDatabase().connect)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.graph.stop(); self.env.stop()
        sharepoint._list_cache.clear(); sharepoint._token.update(value=None, expires=0); sharepoint._site_ids.clear()

    def post(self, path, body):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(),
                                     headers={'Content-Type': 'application/json', 'Origin': self.base, 'X-Review-Token': 'same-origin'})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_vin_finder_timeline_today_and_cross_day(self):
        status, data = self.post('/api/spreadsheets/import', {'filename': NAME, 'content_base64': base64.b64encode(build()).decode()})
        item = data['item']
        self.assertTrue(item['sharepoint']['timeline']['scans'] >= 13)
        # the same car on the next day's report too
        status, d2 = self.post('/api/spreadsheets/import', {'filename': '2230-OWENS-BASCO - EOS - 2026-10-06.xlsx', 'content_base64': base64.b64encode(build(day='2026-10-06')).decode()})
        status, found = self.post('/api/vins/search', {'q': '8010808537'})
        self.assertEqual(status, 200, found)
        self.assertEqual({m['day'] for m in found['results'] if m['exact']}, {'2026-10-06', '2026-10-07'})
        self.assertEqual(found['results'][0]['sheet'], '1st Shift')
        self.assertEqual(self.post('/api/vins/search', {'q': '12'})[0], 409)
        status, p = self.post('/api/periods', {'job': JOB, 'kind': 'week', 'day': '2026-10-07'})
        dup = [f for f in p['period']['findings'] if 'Inspected twice, or billed twice' in f['text']]
        self.assertEqual(len(dup), 13)
        status, today = self.post('/api/today', {'day': '2026-10-07'})
        self.assertEqual(status, 200, today)
        row = [r for r in today['jobs'] if r['job'] == JOB][0]
        self.assertEqual(row['today']['file_hash'], item['file_hash']); self.assertEqual(row['yesterday']['file_hash'], d2['item']['file_hash'])
        status, sp = self.post('/api/today/job', {'job': JOB, 'day': '2026-10-07'})
        self.assertEqual(sp['sharepoint']['today'], 14); self.assertEqual(sp['sharepoint']['shifts']['2nd'], 1)
        status, today = self.post('/api/today', {'day': '2026-10-07', 'add': 'hands-hmma-2219'})
        self.assertIn('HANDS-HMMA-2219', [r['job'] for r in today['jobs'] if r['watched']])


if __name__ == '__main__':
    unittest.main()
