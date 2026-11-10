"""Read-only labels use actual current source/hold evidence, not zero findings."""
import copy
from datetime import date
import tempfile
import unittest
from unittest.mock import patch
from hosting.spreadsheet_inbox import SpreadsheetInbox
from hosting.spreadsheets import CHECKER_VERSION
from tests.test_reconcile import sl_daily, snap, ALL, SITE, LIST


class HeldReportPresentation(unittest.TestCase):
    def test_list_and_today_carry_current_context_without_mutating_reports(self):
        assigned = dict(status='manual', site=SITE, site_id='site-1', list_id=LIST, needs_person=False)
        for unknown_rejects in (False, True):
            with self.subTest(unknown_rejects=unknown_rejects), tempfile.TemporaryDirectory() as root, patch.object(SpreadsheetInbox, 'assignment_for', return_value=assigned):
                box = SpreadsheetInbox(root)
                raw = sl_daily(nok_blank=unknown_rejects)
                original = box.ingest('Synthetic SLAL 2214.xlsx', raw)
                key = original['file_hash']
                source = [dict(x, fields={k: v for k, v in x['fields'].items() if k != 'QtyNOK'}) for x in ALL] if unknown_rejects else ALL
                box.source_reconcile(key, snap([source]), '2026-11-06', expected_check_hash=original['check_hash'], expected_assignment=assigned)
                before = copy.deepcopy(box.get(key))
                listed = box.list()['spreadsheets'][0]
                today = box.reports_for('2214', date(2026, 11, 6), date(2026, 11, 6))
                # The fixture's report header supplies its full job name.
                if not today:
                    today = box.reports_for(listed['review']['job'], date(2026, 11, 6), date(2026, 11, 6))
                daily = today['2026-11-06']
                self.assertEqual(daily['verification'], listed['verification'])
                self.assertEqual(daily['status'], listed['review']['status'])
                self.assertEqual(daily['status'], 'clear')
                self.assertEqual(listed['verification']['source_verified'], not unknown_rejects)
                self.assertTrue(listed['verification']['checks_current'])
                self.assertEqual(daily['approval_current'], listed['review']['approval_current'])
                if unknown_rejects:
                    self.assertTrue(listed['verification']['holds'])
                    self.assertIsNone(daily['rejected'])
                else:
                    self.assertEqual(listed['verification']['holds'], [])
                self.assertNotIn('issues', listed['review'])
                self.assertNotIn('records', daily)
                self.assertEqual(box.get(key), before)
                self.assertEqual(box.download(key)[1], raw)
                # A stale source assignment must dominate a saved matching receipt.
                wrong = dict(assigned, list_id='different-list')
                with patch.object(SpreadsheetInbox, 'assignment_for', return_value=wrong):
                    fresh = box.list()['spreadsheets'][0]
                    self.assertFalse(fresh['verification']['source_verified'])
                    self.assertTrue(any(h['code'] == 'source_assignment_changed' for h in fresh['verification']['holds']))
                self.assertEqual(box.get(key), before)

    def test_summary_context_fails_closed_on_errors_and_old_checks(self):
        box = object.__new__(SpreadsheetInbox)
        item = dict(checker_version=CHECKER_VERSION, internal_check_status='no_findings_in_checks_performed',
                    source_reconciliation=dict(site_id='site', list_id='list'))
        review = dict(status='clear', sharepoint=dict(status='match', source_evidence_complete=True))
        assigned = dict(status='manual', site_id='site', list_id='list')
        before = copy.deepcopy((item, review))
        with patch.object(box, 'assignment_for', return_value=assigned), patch.object(box, 'holds', return_value=[]):
            self.assertTrue(box.verification_presentation(item, review)['source_verified'])
            for change in (dict(checker_version='old'), dict(internal_check_status='could_not_load_latest_check'), dict(internal_check_status='unknown')):
                self.assertFalse(box.verification_presentation(dict(item, **change), review)['checks_current'])
            self.assertFalse(box.verification_presentation(item, None)['source_verified'])
            self.assertFalse(box.verification_presentation(item, None)['checks_current'])
            with patch.object(box, 'holds', side_effect=ValueError('Synthetic load failure')):
                result = box.verification_presentation(item, review)
                self.assertFalse(result['source_verified'])
                self.assertEqual(result['source_status'], 'unverified')
                self.assertEqual(result['holds'][0]['code'], 'verification_unavailable')
        self.assertEqual((item, review), before)

    def test_advisory_holds_and_approval_do_not_disappear_from_context(self):
        box = object.__new__(SpreadsheetInbox)
        item = dict(checker_version=CHECKER_VERSION, internal_check_status='no_findings_in_checks_performed',
                    approval=dict(by='Synthetic reviewer'), source_reconciliation=dict(site_id='site', list_id='list'))
        review = dict(status='clear', approval_current=True, sharepoint=dict(status='match', source_evidence_complete=True))
        for hard in (False, True):
            holds = [dict(code='awaiting_source_owner', hard=hard, text='Synthetic hold')]
            with patch.object(box, 'assignment_for', return_value=dict(status='manual', site_id='site', list_id='list')), patch.object(box, 'holds', return_value=holds):
                result = box.verification_presentation(item, review)
                self.assertEqual(result['holds'], holds)
                self.assertTrue(result['source_verified'])  # source match is not a hold-free report
                self.assertEqual(review['status'], 'clear')

if __name__ == '__main__':
    unittest.main()
