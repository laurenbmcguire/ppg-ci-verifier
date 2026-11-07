"""Data the app already has is filled in; SharePoint differences are listed as issues with fixes."""
import io
import tempfile
import unittest

from openpyxl import load_workbook

from hosting import registry
from hosting.review_summary import summarize
from hosting.spreadsheet_inbox import SpreadsheetInbox
from tests.sl_daily_fixture import sl_daily

NAME = 'SL Alabama - 2214 - 2026-11-06.xlsx'
SNAP = [dict(client='SL Alabama', key='SLAlabama', sites=[dict(site='SLAlabama_AlexanderCity', location='Alexander City')],
             jobs=[dict(job='2214', numbers=['2214'], site='SLAlabama_AlexanderCity', list_id='l-2214')])]


def workbook(edit):
    wb = load_workbook(io.BytesIO(sl_daily()))
    edit(wb)
    buf = io.BytesIO(); wb.save(buf)
    return buf.getvalue()


class FillKnown(unittest.TestCase):
    def setUp(self):
        self.inbox = SpreadsheetInbox(tempfile.mkdtemp())
        self.inbox.set_setting(registry.SNAPSHOT_KEY, SNAP)

    def props(self, raw):
        item = self.inbox.ingest(NAME, raw)
        return item, {(p['sheet'], p['cell']): p for p in self.inbox.fix_proposals(item['file_hash'])['proposals']}

    def test_blank_client_name_is_filled_from_the_settled_client(self):
        def edit(wb):
            s = wb['Daily Inspection Summary']; s['A9'] = 'Client'; s['A10'] = 'Job type'
        def fill(wb):
            edit(wb); wb['Inspection Detail']['I4'] = 'Sort'
        _, got = self.props(workbook(fill))
        self.assertEqual(got[('Daily Inspection Summary', 'B9')]['after'], 'SL Alabama')
        self.assertTrue(got[('Daily Inspection Summary', 'B9')]['sure'])
        self.assertEqual(got[('Daily Inspection Summary', 'B10')]['after'], 'Sort')       # from 'Service performed' on the rows

    def test_blank_record_values_are_filled_from_the_sharepoint_export(self):
        _, got = self.props(workbook(lambda wb: wb['Inspection Detail'].__setitem__('J5', None)))
        p = got[('Inspection Detail', 'J5')]
        self.assertEqual((p['after'], p['source'], p['sure']), ('80', 'SharePoint', True))

    def test_live_sharepoint_differences_become_issues_with_a_fix(self):
        item = self.inbox.ingest(NAME, sl_daily())
        rec = dict(status='discrepancies', rows=[dict(status='value_differs', item='433', field='inspected', report='80', sharepoint='72',
                                                      sheet='Inspection Detail', row=5, url='https://sp/433')],
                   proposed_corrections=[dict(item='433', sheet='Inspection Detail', row=5, column=10, field='inspected', before='80', after='72')])
        self.inbox._update(item['file_hash'], lambda r: r.update(source_reconciliation=rec))
        fresh = self.inbox.get(item['file_hash'])
        issue = next(i for i in summarize(fresh)['issues'] if i['code'] == 'sharepoint_value_differs')
        self.assertEqual(issue['cell'], 'J5')
        self.assertIn('80 in the report, 72 on SharePoint', issue['text'])
        fix = next(p for p in self.inbox.fix_proposals(item['file_hash'])['proposals'] if p['cell'] == 'J5')
        self.assertEqual((fix['after'], fix['sure']), ('72', True))


if __name__ == '__main__':
    unittest.main()
