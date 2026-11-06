"""Recommendations (rules and Claude), the new common data checks, and VINs already reported on another day."""
import base64, json, os, sys, threading, unittest, urllib.error, urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
from eos_fixture import build, ROWS
from hosting import recommend
from hosting.common_issues import vin_check_digit
from hosting.spreadsheets import inspect_spreadsheet

def _valid(v):
    return v[:8] + vin_check_digit(v) + v[9:]


GOOD_VINS = ['1M8GDM9AXKP042788', '1HGCM82633A004352'] + [_valid(v) for v in ('1FTFW1ET0DFC10312', '2T1BURHE0JC034461', '5YJSA1E10HF199401', '1G1ZD5ST0JF100001', '1C4RJFAG0FC625797', '3VWDX7AJ0BM339496', '1N4AL3AP0JC231503', '2HGFC2F50LH512345')]


def codes(rows):
    return [f['code'] for f in inspect_spreadsheet('2230-OWENS-BASCO - EOS - 2026-10-07.xlsx', build([tuple(r) for r in rows]))['findings']]


class DataChecks(unittest.TestCase):
    def test_check_digit(self):
        self.assertEqual([vin_check_digit(v) for v in GOOD_VINS[:2]], ['X', '3'])

    def test_vin_letters_and_check_digit(self):
        rows = [(v, '80139189', 34, 2, 'Holes', '2 holes') for v in GOOD_VINS]
        self.assertNotIn('vin_check_digit_wrong', codes(rows))
        bad = list(rows); bad[1] = ('1HGCM82633A004353',) + bad[1][1:]          # last digit mistyped
        bad[2] = (GOOD_VINS[2][:13] + 'O' + GOOD_VINS[2][14:],) + bad[2][1:]                              # letter O for a zero
        got = codes(bad)
        self.assertIn('vin_check_digit_wrong', got); self.assertIn('vin_has_i_o_q', got)

    def test_length_fill_series_and_leading_zeros(self):
        rows = [(f'8010{n:06d}', '80139189', 34, 2, 'Holes', '2 holes') for n in (808539, 812001, 820554, 833871, 840002, 855131)]
        rows[3] = ('801083387',) + rows[3][1:]                                   # a digit short
        self.assertIn('identifier_unusual_length', codes(rows))
        series = [(f'8010{n:06d}', '80139189', 34, 2, 'Holes', '2 holes') for n in (700001, 700002, 700003, 700004, 910555, 120933, 455120, 610001, 333210)]
        self.assertIn('fill_series_identifiers', codes(series))
        self.assertNotIn('fill_series_identifiers', codes([(f'8010{n:06d}',) + r[1:] for n, r in zip(range(500, 513), ROWS)]))  # numbered scans are fine
        zeros = [tuple(r) for r in ROWS]; zeros[0] = (zeros[0][0], '080139189') + zeros[0][2:]
        self.assertIn('leading_zeros_lost', codes(zeros))


class Rules(unittest.TestCase):
    def facts(self, **kw):
        f = dict(job='2230', day='2026-10-07', status='hold', counts=dict(high=2, medium=1, low=0), numbers={}, issues=[],
                 sharepoint=dict(status='matched', wrong_shift=3), timeline_flags=['A 60 minute gap at 9:10 AM.'], usual_day={},
                 fixes_waiting=4, corrected_from=None, changes_in_this_copy=0, corrected_copy_exists=False, approved=False, sent=False, review=None)
        f.update(kw); return f

    def test_report_rules(self):
        recs = recommend.report_rules(self.facts())
        self.assertEqual([r['do'] for r in recs[:3]], ['fix_shifts', 'fix_issues', 'review_fixes'])
        self.assertTrue(all(r['priority'] in ('high', 'medium', 'low') for r in recs))
        clean = recommend.report_rules(self.facts(status='ready', counts=dict(high=0), fixes_waiting=0, sharepoint=dict(status='matched'), timeline_flags=[]))
        self.assertEqual(clean[-1]['do'], 'send_for_review')
        self.assertEqual(recommend.report_rules(self.facts(approved=True, counts=dict(high=0), fixes_waiting=0, sharepoint={'status': 'matched'}))[-1]['do'], 'approve_send')
        other = recommend.report_rules(self.facts(vins_on_other_days_count=2, vins_on_other_days=[dict(vin='X1', day='2026-10-06')]))
        self.assertTrue(any('another day' in r['title'] for r in other))

    def test_empty_week_is_not_called_clean(self):
        p = dict(kind='week', totals=dict(reports=0, sent=0), findings=[])
        self.assertTrue(recommend.period_rules(p)[0]['title'].startswith('Nothing to review'))
        self.assertIn('is clean', recommend.period_rules(dict(p, totals=dict(reports=3, sent=3)))[0]['title'])

    def test_ai_and_fallback(self):
        fake = {'content': [{'type': 'tool_use', 'name': 'give_recommendations', 'input': {'recommendations': [
            {'title': 'Fix the 2 must-fix issues', 'why': 'Two totals are off.', 'priority': 'high', 'do': 'fix_issues', 'who': 'you'},
            {'title': 'Bad action', 'why': 'x', 'priority': 'urgent', 'do': 'delete_everything'}]}}]}
        with patch('hosting.ai_review._post', return_value=fake):
            out = recommend.make(self.facts(), 'report', recommend.report_rules, True)
        self.assertEqual(out['source'], 'ai')
        self.assertEqual(out['items'][1]['priority'], 'medium'); self.assertIsNone(out['items'][1]['do'])  # unknown values are cleaned
        with patch('hosting.ai_review._post', side_effect=RuntimeError('down')):
            out = recommend.make(self.facts(), 'report', recommend.report_rules, True)
        self.assertEqual(out['source'], 'rules'); self.assertIn('could not be reached', out['note'])

    def test_cache(self):
        f = self.facts()
        cached = dict(fp=recommend.fingerprint(f), recs=dict(items=[], source='ai', made_at='x'))
        self.assertEqual(recommend.answer(cached, f, recommend.report_rules, 'r', False, True)[0]['source'], 'ai')
        out, save = recommend.answer(cached, self.facts(fixes_waiting=1), recommend.report_rules, 'r', False, True)
        self.assertEqual(out['source'], 'rules'); self.assertIn('changed', out['note']); self.assertIsNone(save)


class Endpoints(unittest.TestCase):
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

    def upload(self, day, rows=ROWS):
        status, data = self.post('/api/spreadsheets/import', {'filename': f'2230-OWENS-BASCO - EOS - {day}.xlsx', 'content_base64': base64.b64encode(build([tuple(r) for r in rows], day=day)).decode()})
        self.assertEqual(status, 200, data)
        return data['item']['file_hash']

    def test_report_recs_and_other_days(self):
        h1 = self.upload('2026-10-06')
        h2 = self.upload('2026-10-07', [tuple(r) for r in ROWS[:3]] + [('8010999999',) + tuple(ROWS[3][1:])])
        status, out = self.post('/api/recommendations', {'file_hash': h2})
        self.assertEqual(status, 200, out)
        self.assertEqual(out['source'], 'rules'); self.assertFalse(out['ai_on'])
        self.assertTrue(any('another day' in r['title'] for r in out['items']))
        with urllib.request.urlopen(self.base + '/api/spreadsheet?id=' + h2, timeout=30) as r:
            item = json.loads(r.read())
        self.assertEqual(sorted(x['vin'] for x in item['other_days']), sorted(r[0] for r in ROWS[:3]))
        self.assertTrue(all(x['file_hash'] == h1 and x['day'] == '2026-10-06' for x in item['other_days']))
        self.assertEqual(self.post('/api/recommendations', {'file_hash': 'nope'})[0], 409)

    def test_period_and_overview_recs(self):
        self.upload('2026-10-06')
        status, out = self.post('/api/periods', {'kind': 'week', 'day': '2026-10-06', 'job': '2230'})
        self.assertEqual(status, 200, out); self.assertIn(out['recs']['source'], ('rules',)); self.assertTrue(out['recs']['items'])
        status, out = self.post('/api/overview', {'days': 90})
        self.assertEqual(status, 200, out); self.assertTrue(out['recs']['items'])


if __name__ == '__main__':
    unittest.main()


class IndexBudget(unittest.TestCase):
    def test_index_missing_stops_on_time(self):
        import time
        from hosting.spreadsheet_inbox import SpreadsheetInbox
        inbox = SpreadsheetInbox.__new__(SpreadsheetInbox)
        calls = []

        class DB:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def execute(self, *a):
                class R:
                    def fetchall(_): return [{'file_hash': str(i)} for i in range(10)]
                return R()
        inbox.connect = lambda: DB()
        inbox.index_vins = lambda h: (calls.append(h), time.sleep(0.2))
        left = inbox.index_missing(seconds=0.5)
        self.assertLessEqual(len(calls), 4); self.assertEqual(left, 10 - len(calls))


class BigFileIndex(unittest.TestCase):
    def test_huge_file_is_marked_done_without_reading_it(self):
        import tempfile, pathlib
        from hosting.spreadsheet_inbox import SpreadsheetInbox
        with tempfile.TemporaryDirectory() as d:
            inbox = SpreadsheetInbox(pathlib.Path(d))
            item = inbox.ingest('2230-OWENS-BASCO - EOS - 2026-10-07.xlsx', build([tuple(r) for r in ROWS]))
            h = item['file_hash']
            with inbox.connect() as db:
                db.execute('DELETE FROM vin_indexed'); db.execute('DELETE FROM vin_index')
            with patch.object(SpreadsheetInbox, 'VIN_INDEX_MAX_BYTES', 10), patch('hosting.spreadsheets.report_records', side_effect=AssertionError('read the big file')):
                inbox.index_vins(h)
                self.assertEqual(inbox.index_missing(), 0)
            with inbox.connect() as db:
                self.assertTrue(db.execute('SELECT 1 FROM vin_indexed WHERE file_hash=?', (h,)).fetchone())
                self.assertEqual(db.execute('SELECT COUNT(*) FROM vin_index').fetchone()[0], 0)
