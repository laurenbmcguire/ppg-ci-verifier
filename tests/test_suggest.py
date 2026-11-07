"""Blank details get a suggested value from what is around them (never applied automatically)."""
import os, sys, tempfile, unittest

sys.path.insert(0, os.path.dirname(__file__))
from eos_fixture import build, ROWS
from hosting.spreadsheet_inbox import SpreadsheetInbox
from hosting import suggest, sharepoint_jobs
from test_change_summary import job_list


class Suggest(unittest.TestCase):
    def setUp(self):
        self.inbox = SpreadsheetInbox(tempfile.mkdtemp())
        self.inbox.set_setting(sharepoint_jobs.KEY, sharepoint_jobs.parse(job_list()))

    def props(self, overrides):
        r = self.inbox.ingest('2230-OWENS-BASCO - EOS - 2026-10-07.xlsx', build([tuple(x) for x in ROWS], overrides=overrides))
        return [p for p in self.inbox.fix_proposals(r['file_hash'])['proposals'] if p['source'] in ('Suggested', 'Known detail')]

    def test_paying_client_worksite_and_area(self):
        over = {('Summary', 'B5'): 'Paying Client', ('Summary', 'B6'): 'Worksite'}
        for i in range(len(ROWS)):
            if i != 4:
                over[('1st Shift', f'C{16 + i}')] = 'Line 2'
        got = {(p['sheet'], p['cell']): p for p in self.props(over)}
        self.assertEqual(got[('Summary', 'D5')]['after'], 'Owens')                 # the client the job belongs to (SharePoint job list)
        self.assertEqual(got[('Summary', 'D6')]['after'], 'Owens, Basco')
        self.assertEqual(got[('1st Shift', 'C20')]['after'], 'Line 2')            # the rows around it
        self.assertFalse(any(p['sure'] for p in got.values()))                    # a person accepts suggestions
        self.assertNotIn(('Summary', 'C3'), got)                                  # "Project" is filled (two cells over)

    def test_twin_label_wins(self):
        got = {(p['sheet'], p['cell']): p for p in self.props({('Summary', 'B5'): 'Client', ('Summary', 'D5'): 'Owens Corning', ('Summary', 'B6'): 'Paying client'})}
        self.assertEqual(got[('Summary', 'D6')]['after'], 'Owens Corning')
        self.assertTrue(got[('Summary', 'D6')]['sure'])   # the report already says it: filled in, not just suggested

    def test_header(self):
        self.assertEqual(suggest.header(suggest.context('2230-OWENS-BASCO', None, sharepoint_jobs.parse(job_list()))),
                         dict(customer='Owens', worksite='Owens, Basco', prepared_by='PPG Quality'))
        self.assertEqual(suggest.header(suggest.context('9999-X', None, None)), dict(prepared_by='PPG Quality'))


if __name__ == '__main__':
    unittest.main()
