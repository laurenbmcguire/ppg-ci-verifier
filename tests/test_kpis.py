"""KPI dashboard: errors stopped vs reached the client, inspection totals, and drill-down by client and job."""
import base64, os, sys, unittest
from datetime import date

sys.path.insert(0, os.path.dirname(__file__))
from hosting import kpis
from test_autopilot import Autopilot
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
        self.assertEqual((t['caught'], t['stopped'], t['reached'], t['waiting']), (12, 8, 1, 3))
        self.assertEqual((t['stopped_must'], t['reached_must']), (2, 1))
        self.assertEqual((t['reports'], t['jobs'], t['clients'], t['sent'], t['unsent'], t['hold']), (3, 3, 2, 2, 1, 1))
        self.assertEqual((t['inspected'], t['rejected'], t['certified'], t['rate']), (300, 30, 270, 10.0))
        self.assertEqual((t['on_time_pct'], t['turnaround']), (50.0, 4.0))

    def test_empty(self):
        t = kpis.totals([])
        self.assertEqual((t['reports'], t['rate'], t['on_time_pct'], t['turnaround']), (0, None, None, None))


class Endpoint(Autopilot):
    test_only_sure_fixes_and_never_sends = test_remove_duplicate_rows = None   # reuse the server setup only
    def upload(self, name, day, rows):
        st, out = self.call('/api/spreadsheets/import', {'filename': name, 'content_base64': base64.b64encode(build(rows, day=day)).decode()})
        self.assertEqual(st, 200, out)

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
            st, c = self.call('/api/kpis', {'days': 30, 'client': 'hmma'})
            self.assertEqual((c['totals']['reports'], c['client_name'], [j['job'] for j in c['jobs']]), (1, 'Hyundai Mobis', ['HANDS-HMMA-2219']))
            st, j = self.call('/api/kpis', {'days': 30, 'client': 'hmma', 'job': 'HANDS-HMMA-2219'})
            self.assertEqual(j['totals']['jobs'], 1)
            st, bad = self.call('/api/kpis', {'days': 12345})
            self.assertEqual(bad['days'], 30)
        finally:
            handler.clients_map = orig


if __name__ == '__main__':
    unittest.main()


class JobFromFilename(unittest.TestCase):
    def test_two_part_job_and_never_a_date(self):
        from hosting.source_compare import identify
        self.assertEqual(identify('GRAMMER-2250 - EOS - 2026-10-09.xlsx', {}), ('GRAMMER-2250', '2026-10-09'))
        self.assertEqual(identify('LEAR-T1-2240 - EOS - 2026-10-12.xlsx', {}), ('LEAR-T1-2240', '2026-10-12'))
        self.assertEqual(identify('report 2026-10-09.xlsx', {}), (None, '2026-10-09'))
        self.assertEqual(identify('GRAMMER 2250 EOS 10-09-2026.xlsx', {})[1], '2026-10-09')
