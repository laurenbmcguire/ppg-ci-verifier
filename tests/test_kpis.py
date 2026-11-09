"""KPI dashboard: errors stopped vs reached the client, inspection totals, and drill-down by client and job."""
import base64, os, sys, unittest
from datetime import date
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
from hosting import kpis
import test_autopilot
from eos_fixture import build, ROWS


def rec(**kw):
    base = dict(job='J1', day='2026-10-01', client='a', uploads=1, corrected=False, inspected=100, rejected=10, certified=90,
                caught=0, must_fix=0, remaining=0, remaining_must=0, sent=False, on_time=False, hours=None, hold=False)
    base.update(kw)
    return base


class Totals(unittest.TestCase):
    def test_stopped_reached_waiting(self):
        t = kpis.totals([
            rec(caught=5, must_fix=2, remaining=0, sent=True, on_time=True, hours=3, corrected=True),   # all fixed, sent
            rec(job='J2', caught=4, must_fix=1, remaining=1, remaining_must=1, sent=True, hours=5),      # one went out
            rec(job='J3', client='b', caught=3, remaining=3, hold=True),                                  # not sent yet
        ])
        self.assertEqual((t['caught'], t['stopped'], t['reached'], t['waiting']), (12, None, None, 3))
        self.assertEqual((t['stopped_must'], t['reached_must']), (None, None))
        self.assertEqual((t['reports'], t['jobs'], t['clients'], t['sent'], t['unsent'], t['hold']), (3, 3, 2, 2, 1, 1))
        self.assertEqual((t['inspected'], t['rejected'], t['certified'], t['rate']), (300, 30, 270, 10.0))
        self.assertEqual((t['on_time_pct'], t['turnaround']), (50.0, 4.0))

    def test_empty(self):
        t = kpis.totals([])
        self.assertEqual((t['reports'], t['rate'], t['on_time_pct'], t['turnaround']), (0, None, None, None))


class Endpoint(test_autopilot.Autopilot):
    test_only_sure_fixes_and_never_sends = test_remove_duplicate_rows = test_paused_by_default = None   # reuse the server setup only
    def setUp(self):
        super().setUp()
        # Explicit synthetic assignments, not inferred client matches or live registry data.
        p = patch('hosting.spreadsheet_inbox.SpreadsheetInbox.assignment_for', side_effect=lambda r,rv,ctx=None:
                  dict(status='manual',job=rv['job'],business_date=rv['day'],client_key='hmma' if 'HMMA' in rv['job'] else None,site_id='synthetic',list_id=rv['job']))
        p.start(); self.addCleanup(p.stop)
    def upload(self, name, day, rows):
        st, out = self.call('/api/spreadsheets/import', {'filename': name, 'content_base64': base64.b64encode(build(rows, day=day)).decode()})
        self.assertEqual(st, 200, out)

    def test_duration_requires_an_exact_allowed_integer(self):
        from urllib.parse import urlsplit,parse_qs
        from hosting import overview
        for invalid in (7.0,30.0,365.0,True,False,'7'):
            for route in ('/api/kpis','/api/overview'):
                with self.subTest(route=route,days=invalid):
                    status,out=self.call(route,{'days':invalid})
                    self.assertEqual(status,200,out)
                    self.assertEqual(out['days'],30)
                    self.assertIs(type(out['days']),int)
            status,out=self.call('/api/overview/artifact',{'days':invalid})
            self.assertEqual(status,200,out)
            key=parse_qs(urlsplit(out['view']).query)['key'][0]
            self.assertEqual(overview.parse_key(key)[0],30)
        for valid in (7,30,90,365):
            for route in ('/api/kpis','/api/overview'):
                status,out=self.call(route,{'days':valid})
                self.assertEqual(status,200,out)
                self.assertEqual(out['days'],valid)

    def test_drill_down(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        today = datetime.now(ZoneInfo('America/Chicago')).date()
        d1, d2 = today.replace(day=1).isoformat() if today.day > 2 else today.isoformat(), today.isoformat()
        self.upload(f'HANDS-HMMA-2219 - EOS - {d1}.xlsx', d1, [tuple(r) for r in ROWS])
        self.upload(f'LEAR-T1-2240 - EOS - {d2}.xlsx', d2, [tuple(r) for r in ROWS[:5]])
        clients = [dict(key='hmma', client='Hyundai Mobis', sites=[], jobs=[dict(job='HANDS-HMMA-2219', numbers=['2219'])])]
        from api.index import handler
        orig = handler.clients_map
        handler.clients_map = lambda self: clients
        try:
            st, k = self.call('/api/kpis', {'days': 30})
            self.assertEqual(st, 200, k)
            self.assertEqual(k['totals']['reports'], 2)
            self.assertEqual({c['key'] for c in k['clients']}, {'hmma', '_none'})
            self.assertEqual(len(k['trend']), 30)
            st, o = self.call('/api/overview', {'days':30})
            self.assertEqual(st,200,o)
            self.assertEqual(k['totals'],o['totals'])
            st, c = self.call('/api/kpis', {'days': 30, 'client': 'hmma'})
            self.assertEqual((c['totals']['reports'], c['client_name'], [j['job'] for j in c['jobs']]), (1, 'Hyundai Mobis', ['HANDS-HMMA-2219']))
            st, j = self.call('/api/kpis', {'days': 30, 'client': 'hmma', 'job': 'HANDS-HMMA-2219'})
            self.assertEqual(j['totals']['jobs'], 1)
            st, bad = self.call('/api/kpis', {'days': 12345})
            self.assertEqual(bad['days'], 30)
        finally:
            handler.clients_map = orig

    def test_hidden_reports_never_enter_statistics_or_coverage(self):
        from api.index import handler
        from hosting.spreadsheet_inbox import SpreadsheetInbox
        from datetime import datetime
        from zoneinfo import ZoneInfo
        day=datetime.now(ZoneInfo('America/Chicago')).date().isoformat()
        self.upload(f'HANDS-HMMA-2219 - EOS - {day}.xlsx', day, [tuple(r) for r in ROWS])
        self.upload(f'LEAR-T1-2240 - EOS - {day}.xlsx', day, [tuple(r) for r in ROWS[:5]])
        seen=[]
        def assigned(r,rv,ctx=None):
            seen.append(rv['job'])
            return dict(status='manual',job=rv['job'],business_date=rv['day'],client_key='hmma' if 'HMMA' in rv['job'] else 'private',site_id='synthetic',list_id='allowed')
        with patch.object(handler,'is_admin',return_value=False), patch.object(handler,'current_user',return_value=dict(role='member',clients=['hmma'])), patch.object(handler,'can_job',side_effect=lambda job: bool(job and 'HMMA' in job)), patch.object(SpreadsheetInbox,'assignment_for',side_effect=assigned):
            for route in ('/api/kpis','/api/overview'):
                status,out=self.call(route,{'days':30})
                self.assertEqual(status,200,out)
                self.assertEqual(out['totals']['reports'],1)
                self.assertEqual(out['statistics']['coverage']['excluded_assignment'],0)
                self.assertNotIn('LEAR',str(out))
        self.assertTrue(seen)

    def test_assignment_cannot_expose_a_new_unauthorized_job(self):
        from api.index import handler
        from hosting.spreadsheet_inbox import SpreadsheetInbox
        from datetime import datetime
        from zoneinfo import ZoneInfo
        day=datetime.now(ZoneInfo('America/Chicago')).date().isoformat()
        self.upload(f'HANDS-HMMA-2219 - EOS - {day}.xlsx',day,[tuple(r) for r in ROWS])
        with patch.object(handler,'is_admin',return_value=False), patch.object(handler,'current_user',return_value=dict(role='member',clients=['hmma'])), patch.object(handler,'can_job',return_value=True), patch.object(SpreadsheetInbox,'assignment_for',return_value=dict(status='manual',job='PRIVATE-2000',business_date=day,client_key='private')):
            for route in ('/api/kpis','/api/overview'):
                status,out=self.call(route,{'days':30})
                self.assertEqual(status,200,out)
                self.assertEqual(out['totals']['reports'],0)
                self.assertNotIn('PRIVATE-2000',str(out))

    def test_allowed_manual_reassignment_not_lost_to_filename_filter(self):
        from api.index import handler
        from hosting.spreadsheet_inbox import SpreadsheetInbox
        from datetime import datetime
        from zoneinfo import ZoneInfo
        day=datetime.now(ZoneInfo('America/Chicago')).date().isoformat()
        self.upload(f'LEAR-T1-2240 - EOS - {day}.xlsx',day,[tuple(r) for r in ROWS])
        assigned=dict(status='manual',job='CORRECT-2000',business_date=day,client_key='hmma',site_id='synthetic',list_id='assigned-source')
        with patch.object(handler,'is_admin',return_value=False), patch.object(handler,'current_user',return_value=dict(role='member',clients=['hmma'])), patch.object(handler,'can_job',return_value=False), patch.object(SpreadsheetInbox,'assignment_for',return_value=assigned):
            for route in ('/api/kpis','/api/overview'):
                status,out=self.call(route,{'days':30})
                self.assertEqual(status,200,out)
                self.assertEqual(out['totals']['reports'],1)
                self.assertEqual(out['records'][0]['job'],'CORRECT-2000')
                self.assertEqual(out['statistics']['coverage']['excluded_assignment'],0)


if __name__ == '__main__':
    unittest.main()


class JobFromFilename(unittest.TestCase):
    def test_two_part_job_and_never_a_date(self):
        from hosting.source_compare import identify
        self.assertEqual(identify('GRAMMER-2250 - EOS - 2026-10-09.xlsx', {}), ('GRAMMER-2250', '2026-10-09'))
        self.assertEqual(identify('LEAR-T1-2240 - EOS - 2026-10-12.xlsx', {}), ('LEAR-T1-2240', '2026-10-12'))
        self.assertEqual(identify('report 2026-10-09.xlsx', {}), (None, '2026-10-09'))
        self.assertEqual(identify('GRAMMER 2250 EOS 10-09-2026.xlsx', {})[1], '2026-10-09')
