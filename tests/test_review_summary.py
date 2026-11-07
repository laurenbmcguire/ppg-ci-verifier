import os, sys, tempfile, unittest
sys.path.insert(0, os.path.dirname(__file__))
from eos_fixture import build
from test_fixes import seeded, NAME
from hosting.review_summary import summarize, severity, check_run
from hosting.spreadsheet_inbox import SpreadsheetInbox


class SummaryTests(unittest.TestCase):
    def test_clean_report_is_clear_and_seeded_report_is_on_hold(self):
        with tempfile.TemporaryDirectory() as root:
            inbox = SpreadsheetInbox(root)
            clean = summarize(inbox.ingest(NAME, build()))
            self.assertEqual(clean['status'], 'clear'); self.assertEqual(clean['scans'], 13); self.assertEqual(clean['inspected'], 438)
            bad = summarize(inbox.ingest('other ' + NAME, seeded()))
            self.assertEqual(bad['status'], 'hold'); self.assertGreater(bad['counts']['high'], 0)
            self.assertFalse(any(i['text'] == i['code'] for i in bad['issues']))  # always plain language
            listed = inbox.list()['spreadsheets']
            self.assertEqual({r['review']['status'] for r in listed}, {'clear', 'hold'})
            self.assertNotIn('issues', listed[0]['review'])

    def test_all_sources_and_no_rows(self):
        result = dict(record_count=0, findings=[], filename='x.csv',
                      sharepoint={'status': 'differences', 'findings': [{'code': 'missing_from_report', 'message': 'm'}]},
                      baseline={'status': 'unusual', 'findings': [{'code': 'unusual_scans_volume', 'severity': 'medium', 'message': 'b'}]},
                      ai_review={'status': 'done', 'issues': [{'severity': 'low', 'issue': 'a', 'category': 'typo'}]})
        result['ai_review']['run'] = check_run(result)
        s = summarize(result)
        # the old day-level export comparison is a lead for a person (medium), never a record-by-record blocker
        self.assertEqual([i['source'] for i in s['issues']], ['Checks', 'SharePoint (day totals)', 'Usual day', 'AI review'])
        self.assertEqual(s['sharepoint']['status'], 'day_level')
        self.assertEqual(s['status'], 'hold')
        self.assertEqual(severity('labeled_total_unit_differs'), 'low'); self.assertEqual(severity('formula_results_used'), 'info')


if __name__ == '__main__':
    unittest.main()
