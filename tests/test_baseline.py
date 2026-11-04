import base64, io, json, os, tempfile, unittest, urllib.error
from decimal import Decimal as D
from unittest.mock import patch

from hosting.baseline import compare_to_usual
from hosting.spreadsheet_inbox import SpreadsheetInbox

JOB = '2230-OWENS-BASCO'


def day(d, scans, ins, rej, source='saved report'):
    return dict(day=d, scans=D(scans), inspected=D(ins), rejected=D(rej), source=source)


HISTORY = [day(f'2026-10-0{i}', 500 + i * 5, 17000 + i * 100, 1700 + i * 10) for i in range(1, 7)]


def csv_report(d, n, nok=1):
    lines = ['Date,Shift,Identifier,Part Number,Qty Inspected,Qty OK,Qty NOK,Defect']
    for i in range(n):
        lines.append(f'{d},1,80108{d[-2:]}{i:04d},80139189,34,{34 - nok},{nok},{"Holes" if nok else ""}')
    return ('\n'.join(lines) + '\n').encode()


class CompareToUsualTests(unittest.TestCase):
    def test_fifty_scans_on_a_five_hundred_scan_job(self):
        out = compare_to_usual(dict(scans=D(50), inspected=D(1700), rejected=D(170)), HISTORY, JOB, '2026-10-07')
        self.assertEqual(out['status'], 'unusual')
        f = {x['code']: x for x in out['findings']}
        self.assertEqual(f['unusual_scans_volume']['severity'], 'high')
        self.assertIn('90% fewer', f['unusual_scans_volume']['message'])
        self.assertIn('unusual_inspected_volume', f)
        self.assertNotIn('unusual_reject_rate', f)  # 10% vs ~10% usual

    def test_normal_day_and_spike_in_reject_rate(self):
        out = compare_to_usual(dict(scans=D(510), inspected=D(17200), rejected=D(1710)), HISTORY, JOB, '2026-10-07')
        self.assertEqual(out['status'], 'normal'); self.assertEqual(out['findings'], [])
        out = compare_to_usual(dict(scans=D(510), inspected=D(17200), rejected=D(8000)), HISTORY, JOB, '2026-10-07')
        self.assertEqual([x['code'] for x in out['findings']], ['unusual_reject_rate'])

    def test_double_volume_and_zero(self):
        out = compare_to_usual(dict(scans=D(1200), inspected=D(40000), rejected=D(4000)), HISTORY, JOB, '2026-10-07')
        self.assertIn('2.3× more', out['findings'][0]['message']); self.assertIn('about 518', out['findings'][0]['message'])
        out = compare_to_usual(dict(scans=D(0), inspected=D(0), rejected=D(0)), HISTORY, JOB, '2026-10-07')
        self.assertTrue(all(x['severity'] == 'high' for x in out['findings'] if x['code'] != 'unusual_reject_rate'))

    def test_needs_three_earlier_days_and_ignores_same_day(self):
        out = compare_to_usual(dict(scans=D(50), inspected=None, rejected=None), HISTORY[:2] + [day('2026-10-07', 1, 1, 0)], JOB, '2026-10-07')
        self.assertEqual(out['status'], 'not_enough_history'); self.assertEqual(out['days'], 2)


class InboxHistoryTests(unittest.TestCase):
    def test_history_comes_from_saved_reports_of_the_same_job(self):
        with tempfile.TemporaryDirectory() as root:
            inbox = SpreadsheetInbox(root)
            for d in ('2026-10-01', '2026-10-02', '2026-10-03', '2026-10-05'):
                inbox.ingest(f'{JOB} EOS {d}.csv', csv_report(d, 40))
            inbox.ingest(f'9999-OTHER-JOB EOS 2026-10-04.csv', csv_report('2026-10-04', 400))
            today = inbox.ingest(f'{JOB} EOS 2026-10-07.csv', csv_report('2026-10-07', 4))
            history = inbox.history_for(JOB, '2026-10-07', exclude_hash=today['file_hash'])
            self.assertEqual([h['day'] for h in history], ['2026-10-05', '2026-10-03', '2026-10-02', '2026-10-01'])
            self.assertEqual(history[0]['scans'], D(40))
            from hosting.baseline import stats_from_result
            out = compare_to_usual(stats_from_result(today), history, JOB, '2026-10-07')
            self.assertEqual(out['status'], 'unusual')
            saved = inbox.record_baseline(today['file_hash'], out)
            self.assertEqual(saved['baseline']['status'], 'unusual')
            self.assertEqual(saved['internal_check_status'], today['internal_check_status'])


class FakeGraph:
    def __init__(self, days):
        self.days = days

    def __call__(self, request, timeout=None):
        url = request.full_url
        if 'login.microsoftonline.com' in url:
            return io.BytesIO(json.dumps({'access_token': 't', 'expires_in': 3600}).encode())
        if '/sites/ppgquality.sharepoint.com:' in url:
            return io.BytesIO(json.dumps({'id': 'site1'}).encode())
        if url.endswith(':/children?$select=name,folder&$top=500'):
            return io.BytesIO(json.dumps({'value': [{'name': d, 'folder': {}} for d in self.days] + [{'name': 'notes.txt'}]}).encode())
        for d in self.days:
            if f'%20{d}.csv' in url and '/content' not in url:
                return io.BytesIO(json.dumps({'id': 'item-' + d}).encode())
            if url.endswith(f'/items/item-{d}/content'):
                return io.BytesIO(csv_report(d, 500))
        raise urllib.error.HTTPError(url, 404, 'nf', {}, io.BytesIO(b''))


class SharePointHistoryTests(unittest.TestCase):
    def test_recent_exports_newest_first_before_the_report_day(self):
        from hosting import sharepoint
        env = {'MS_TENANT_ID': 't', 'MS_CLIENT_ID': 'c', 'MS_CLIENT_SECRET': 's', 'SHAREPOINT_SITES': 'Site'}
        sharepoint._token.update(value=None, expires=0); sharepoint._site_ids.clear()
        with patch.dict(os.environ, env):
            got = sharepoint.recent_exports(JOB, '2026-10-07', limit=3, opener=FakeGraph(['2026-10-01', '2026-10-02', '2026-10-03', '2026-10-06', '2026-10-08']))
        self.assertEqual([d for d, _, _ in got], ['2026-10-06', '2026-10-03', '2026-10-02'])
        sharepoint._token.update(value=None, expires=0); sharepoint._site_ids.clear()


if __name__ == '__main__':
    unittest.main()
