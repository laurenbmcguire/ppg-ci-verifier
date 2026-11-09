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
    def test_recent_exports_newest_first_from_exact_assigned_list(self):
        from hosting import sharepoint
        env = {'MS_TENANT_ID': 't', 'MS_CLIENT_ID': 'c', 'MS_CLIENT_SECRET': 's'}
        dates = ['2026-10-01', '2026-10-02', '2026-10-03', '2026-10-06', '2026-10-08']
        capture = ('Synthetic', {'displayName': 'Data #' + JOB}, {'Date': 'Date', 'Qty': 'Quantity Inspected'},
                   [dict(id=str(n), fields={'Date': d, 'Qty': 34}) for n, d in enumerate(dates, 1)])
        with patch.dict(os.environ, env), patch.object(sharepoint, '_list_items', return_value=capture) as lookup:
            got = sharepoint.recent_exports(JOB, '2026-10-07', limit=3, site_id='site-exact', list_id='list-exact')
        self.assertEqual([d for d, _, _ in got], ['2026-10-06', '2026-10-03', '2026-10-02'])
        lookup.assert_called_once_with(JOB, None, site_id='site-exact', list_id='list-exact', refresh=False)
        self.assertTrue(all(b'Quantity Inspected' in raw for _, _, raw in got))

    def test_job_label_only_history_cannot_choose_cross_site_exports(self):
        from hosting import sharepoint
        with patch.object(sharepoint, 'list_history') as history:
            with self.assertRaisesRegex(sharepoint.SharePointError, 'verified site_id and list_id'):
                sharepoint.recent_exports(JOB, '2026-10-07')
        history.assert_not_called()



if __name__ == '__main__':
    unittest.main()


class SharePointHistoryFirstTests(unittest.TestCase):
    def test_earlier_days_come_from_the_sharepoint_list(self):
        import tempfile
        from pathlib import Path
        import api.index as app
        from hosting.sqlite_storage import SQLiteWorkspace
        from eos_fixture import build, ROWS
        store = SQLiteWorkspace(self.enterContext(tempfile.TemporaryDirectory()))
        h = app.handler.__new__(app.handler)
        h.store = lambda: store
        with store.session() as d:
            item = app.SpreadsheetInbox(d).ingest('2230-OWENS-BASCO - EOS - 10.07.26.xlsx', build(ROWS))
        def csv_for(n):
            lines = ['Identifier,Part Number,Quantity Inspected,Quantity Rejected']
            lines += [f'80100{i:05d},80139189,34,2' for i in range(n)]
            return '\n'.join(lines).encode()
        days = [(f'2026-10-0{d}', f'x {d}.csv', csv_for(500)) for d in (6, 5, 4, 3, 2)]
        assignment = dict(status='source', site_id='site-exact', list_id='list-exact')
        with patch.object(app, 'sharepoint_configured', return_value=True), \
             patch.object(app.SpreadsheetInbox, 'assignment_for', return_value=assignment), \
             patch('hosting.sharepoint._list_items', return_value=('site', {'displayName': 'Data #2230-OWENS-BASCO'}, {}, [])), \
             patch('hosting.sharepoint.list_history', return_value=days) as history, \
             patch('hosting.sharepoint.recent_exports', return_value=[]):
            result = h.baseline_check(item['file_hash'])
        bl = result['baseline']
        self.assertEqual(bl['source_identity'], {'site_id': 'site-exact', 'list_id': 'list-exact'})
        self.assertEqual(history.call_args.kwargs['site_id'], 'site-exact')
        self.assertEqual(history.call_args.kwargs['list_id'], 'list-exact')
        self.assertEqual(bl['sharepoint_days'], 5)
        self.assertIn('SharePoint list "Data #2230-OWENS-BASCO"', bl['history_source'])
        self.assertIn('from the SharePoint list', bl['message'])
        self.assertEqual(bl['status'], 'unusual')  # 13 rows today vs 500 usual

        # A formerly successful source read cannot make an unresolved assignment current.
        with patch.object(app.SpreadsheetInbox, 'assignment_for', return_value={}), \
             patch('hosting.sharepoint._list_items') as lookup:
            held = h.baseline_check(item['file_hash'])['baseline']
        lookup.assert_not_called()
        self.assertEqual(held['history_source'], 'unavailable')
        self.assertEqual(held['sharepoint_days'], 0)
        self.assertTrue(held['coverage_note'])
