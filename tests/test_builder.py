"""Building the end-of-shift report from SharePoint scans, shift-end readiness, and Nexus shift numbers."""
import base64, io, json, os, sys, threading, unittest, urllib.error, urllib.request
from datetime import datetime
from http.server import ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
from test_shifts import FakeGraph, JOB, LATE, ENV, seed_verified_source
from hosting import builder, sharepoint, shifts
from hosting.spreadsheets import inspect_spreadsheet
from hosting.review_summary import summarize


def reset():
    sharepoint._token.update(value=None, expires=0); sharepoint._list_cache.clear(); sharepoint._site_ids.clear()


class BuilderUnit(unittest.TestCase):
    def setUp(self):
        reset(); self.env = patch.dict(os.environ, ENV); self.env.start()

    def tearDown(self):
        self.env.stop(); reset()

    def test_rows_shifts_and_client_safe_file(self):
        rows, info = builder.day_rows(JOB, '2026-10-07', shifts.DEFAULT, opener=FakeGraph(), site_id='site-Owens', list_id='L1')
        by = {r['identifier']: r for r in rows}
        self.assertEqual(by[LATE]['shift'], '1')                  # Recorded shift is authoritative; clock time is only a suggestion
        self.assertEqual(by['NIGHT1']['shift'], '3')               # 1:15 AM on the 8th counts toward the 7th
        self.assertNotIn('NEXTDAY', by)
        self.assertEqual(by[LATE]['certified'], by[LATE]['inspected'] - by[LATE]['rejected'])
        raw = builder.build_xlsx(JOB, '2026-10-07', rows, shifts.DEFAULT, {'customer': 'Owens Corning'})
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(raw))
        self.assertEqual(wb.sheetnames, ['Summary', '1st Shift', '3rd Shift', 'Rejects'])
        text = ' '.join(str(c.value) for ws in wb for row in ws.iter_rows() for c in row if c.value is not None)
        for internal in ('Scan time', 'Picked shift', 'AM', 'PM', 'Auditor'):
            self.assertNotIn(internal, text)
        self.assertIn('Owens Corning', text)
        res = inspect_spreadsheet(builder.file_name(JOB, '2026-10-07'), raw)
        rv = summarize(dict(res, filename=builder.file_name(JOB, '2026-10-07')))
        self.assertEqual(rv['inspected'], sum(r['inspected'] for r in rows))   # totals formulas carry saved results
        self.assertEqual(rv['rejected'], sum(r['rejected'] for r in rows))
        self.assertFalse([i for i in rv['issues'] if 'total' in (i['code'] or '')])

    def test_inactivity_never_proves_shift_completion(self):
        last = '2026-10-07T14:00:00'
        for now in (datetime(2026, 10, 7, 14, 30), datetime(2026, 10, 7, 14, 45), datetime(2026, 10, 8)):
            ready = builder.readiness(last, now=now)
            self.assertEqual(ready['state'], 'provisional')
            self.assertFalse(ready['shift_end_confirmed'])
            self.assertIsNone(ready['ready_at'])
        self.assertEqual(builder.readiness(None)['state'], 'no_scans')

    def test_build_requires_exact_source_and_recorded_evidence(self):
        with self.assertRaisesRegex(builder.BuildError, 'verified site_id and list_id'):
            builder.day_rows(JOB, '2026-10-07', shifts.DEFAULT, opener=FakeGraph())
        from test_shifts import items
        for field in ('Date', 'Shift'):
            rows = items()
            rows[0]['fields'].pop(field)
            with patch('test_shifts.items', return_value=rows), self.assertRaises(builder.BuildError):
                builder.day_rows(JOB, '2026-10-07', shifts.DEFAULT, opener=FakeGraph(), site_id='site-Owens', list_id='L1')

    def test_missing_counts_remain_unknown_not_inferred(self):
        row = builder._row({'n': 8, 'bad': 1}, {'n': 'inspected', 'bad': 'rejected'})
        self.assertEqual(row['inspected'], 8)
        self.assertEqual(row['rejected'], 1)
        self.assertIsNone(row['certified'])
        self.assertIsNone(builder._row({}, {})['rejected'])

    def test_nexus_windows(self):
        mk = lambda h, m, n=10, rej=1: dict(time=datetime(2026, 10, 7, h, m), inspected=n, rejected=rej, certified=n - rej)
        out = builder.nexus_split([mk(6, 0), mk(14, 29), mk(14, 30), mk(22, 15), mk(22, 40), mk(5, 0), dict(time=None)])
        self.assertEqual([s['scans'] for s in out['shifts']], [2, 2, 2])  # 10:15 PM is in both 2 and 3: the first window wins
        self.assertEqual(sum(s['inspected'] for s in out['shifts']), 60)
        self.assertIsNone(out['total']['inspected'])
        self.assertEqual(out['total']['scans'], 7)
        self.assertEqual(out['unplaced'], 1)
        self.assertEqual(out['coverage'], dict(complete=False, source_records=7, placed_records=6))
        self.assertEqual(out['status'], 'held')

    def test_known_unplaced_quantities_remain_in_overall_totals(self):
        rows = [dict(time=datetime(2026, 10, 7, 8), inspected=10, rejected=1, certified=9),
                dict(time=None, inspected=7, rejected=2, certified=5)]
        out = builder.nexus_split(rows)
        self.assertEqual(out['total'], dict(scans=2, inspected=17, rejected=3, certified=14))
        self.assertEqual(sum(s['inspected'] for s in out['shifts']), 10)
        self.assertEqual(out['unplaced'], 1)
        self.assertEqual(out['coverage'], dict(complete=False, source_records=2, placed_records=1))
        self.assertEqual(out['status'], 'held')

    def test_column_roles_drop_people_and_times(self):
        roles = builder.column_roles({'Title': 'VIN', 'Auditor': 'Auditor', 'Created': 'Date', 'Qty_x0020_NOK': 'Qty NOK', 'Defect': 'Defect', 'Photo': 'Picture File'})
        self.assertEqual(set(roles.values()), {'identifier', 'rejected', 'defect'})


class BuildRoutes(unittest.TestCase):
    def setUp(self):
        from api.index import handler
        from legacy_storage_fake import PostgresWorkspace
        from test_vercel import FakeDatabase
        reset()
        self.env = patch.dict(os.environ, dict(ENV, CI_LOCAL_DEV='1', VERCEL='0')); self.env.start()
        os.environ.pop('ANTHROPIC_API_KEY', None)
        fake = FakeGraph()
        self.graph = patch('hosting.sharepoint._open', lambda request, opener=None: fake(request)); self.graph.start()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        self.server.workspace_store = PostgresWorkspace('postgresql://test.invalid/x', FakeDatabase().connect)
        seed_verified_source(self.server.workspace_store)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.graph.stop(); self.env.stop(); reset()

    def call(self, path, body=None, headers=None, method=None):
        req = urllib.request.Request(self.base + path, data=None if body is None else json.dumps(body).encode(), method=method,
                                     headers=dict({'Content-Type': 'application/json', 'Origin': self.base, 'X-Review-Token': 'same-origin'}, **(headers or {})))
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_build_from_app_and_nexus(self):
        status, out = self.call('/api/build', {'job': JOB.lower(), 'day': '2026-10-07', 'header': {'customer': 'Owens'}})
        self.assertEqual(status, 200, out)
        self.assertEqual(out['built']['scans'], 14); self.assertEqual(out['report']['job'], JOB)
        rid = out['report']['id']
        with urllib.request.urlopen(self.base + '/api/spreadsheet?id=' + rid, timeout=30) as r:
            item = json.loads(r.read())
        self.assertEqual(item['built']['nexus']['total']['scans'], 14)
        self.assertEqual(item['sharepoint']['status'], 'match', item['sharepoint'].get('message'))   # built from SharePoint, so it matches
        status, n = self.call('/api/nexus', {'file_hash': rid})
        self.assertEqual(status, 200, n); self.assertEqual(n['total']['inspected'], item['built']['nexus']['total']['inspected'])
        from hosting.spreadsheet_inbox import SpreadsheetInbox
        with self.server.workspace_store.session() as directory:
            self.assertEqual(SpreadsheetInbox(directory).setting('report_header|site-Owens|L1')['customer'], 'Owens')
        self.assertEqual(self.call('/api/build', {'job': 'NO-SUCH-JOB', 'day': '2026-10-07'})[0], 409)

    def test_build_is_provisional_even_when_forced(self):
        for force in (False, True):
            status, out = self.call('/api/build', {'job': JOB, 'day': '2026-10-07', 'force': force})
            self.assertEqual(status, 200, out)
            self.assertFalse(out['built']['shift_end_confirmed'])
            self.assertEqual(out['built']['ready']['state'], 'provisional')

    def test_build_over_api_and_connector(self):
        status, k = self.call('/api/keys/create', {'name': 'Scheduled task'})
        self.assertEqual(status, 200, k)
        key = k['key'].get('key') or k['key'].get('secret') or k['key'].get('token')
        auth = {'Authorization': 'Bearer ' + key}
        status, out = self.call('/api/v1/build', {'job': JOB, 'day': '2026-10-07'}, auth)
        self.assertEqual(status, 201, out); self.assertEqual(out['built']['scans'], 14)
        status, n = self.call(f'/api/v1/nexus?job={JOB}&day=2026-10-07', None, auth)
        self.assertEqual(status, 200, n); self.assertEqual(len(n['shifts']), 3)
        status, rpc = self.call('/mcp/' + key, {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': 'nexus_numbers', 'arguments': {'job': JOB, 'day': '2026-10-07'}}},
                                {'Accept': 'application/json, text/event-stream'})
        self.assertEqual(status, 200, rpc); self.assertFalse(rpc['result'].get('isError'), rpc)


if __name__ == '__main__':
    unittest.main()
