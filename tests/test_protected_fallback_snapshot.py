"""Synthetic protected snapshot regression. No live state, mutations, or recovery executor."""
import copy
import unittest
import test_ai_false_clear_repair as flow_tests
from test_ai_false_clear_repair import still_open
from hosting.review_summary import summarize
from hosting import public_api

class ProtectedFallbackSnapshot(unittest.TestCase):
    def setUp(self):
        self.flow = flow_tests.FixWithAIFlow()
        self.flow.setUp()
        self.flow.store_legacy_fallback([x['key'] for x in still_open(self.flow.summary())])

    def tearDown(self):
        self.flow.tearDown()

    def test_protection_matrix_preserves_historical_counts_and_all_saved_state(self):
        for protection in ({'approval': {'by':'Reviewer','reviewed_check_hash':'old'}},
                           {'approval': {}}, {'send_pending': {'request':'synthetic'}},
                           {'send_in_progress': {'request':'synthetic'}},
                           {'sent': [{'sent': True, 'at':'2001-02-03T00:00:00Z'}]},
                           {'sent': [{'sent': False, 'status':'unknown'}]}):
            with self.subTest(protection=protection):
                r = self.flow.child(); r.update(protection); before=copy.deepcopy(r)
                s=summarize(r)
                self.assertEqual(r,before)
                self.assertEqual(s['status'],'clear')
                self.assertEqual(s['reconcile']['open'],0)
                self.assertEqual(s['reconcile']['historical_fallback_records'],57)
                self.assertEqual(s['reconcile']['kept_by_person'],0)
                self.assertTrue(s['reconcile']['adds_up'])
                self.assertEqual(s['historical_integrity']['unresolved_count'],57)
                self.assertTrue(s['historical_integrity']['snapshot_preserved'])
                api=public_api.report(r)
                row=public_api.report_row(dict(r,review=s))
                self.assertEqual(api['historical_integrity'],s['historical_integrity'])
                self.assertEqual(row['historical_integrity'],s['historical_integrity'])
                self.assertIn('integrity review required',api['status_label'])
                self.assertEqual(api['approval'],r.get('approval'))
                self.assertEqual(api['sent'],r.get('sent') or [])
                self.assertEqual(r,before)

    def test_unprotected_is_actionable_and_does_not_change_history(self):
        r=self.flow.child();before=copy.deepcopy(r);s=summarize(r)
        self.assertEqual(s['reconcile']['open'],57)
        self.assertEqual(s['reconcile']['ai_fallback_unresolved'],57)
        self.assertNotIn('historical_integrity',s)
        self.assertEqual(r,before)

    def test_current_approval_remains_current(self):
        from hosting.review_summary import check_run, decisions_basis, approved_state_fingerprint, approval_current
        r=self.flow.child()
        r['approval']={'by':'Synthetic Reviewer','run':check_run(r),'decisions_basis':decisions_basis(r)}
        r['approval']['approved_state_hash']=approved_state_fingerprint(r)
        self.assertTrue(approval_current(r))
        before=copy.deepcopy(r);s=summarize(r)
        self.assertTrue(s['approval_current'])
        self.assertEqual(s['historical_integrity']['unresolved_count'],57)
        self.assertEqual(r,before)
