"""Source capture is read-only and may not persist against changed report/assignment."""
import tempfile
import unittest
from unittest.mock import patch
from hosting.spreadsheet_inbox import SpreadsheetInbox
from hosting import reconcile
from test_r8_count_contract import fixture, DAY
from eos_fixture import build, ROWS

class SourceSaveGuards(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.inbox=SpreadsheetInbox(self.tmp.name);item=self.inbox.ingest('report.xlsx',build(rows=list(ROWS)[:2]));self.h=item['file_hash']
        self.item=self.inbox.get(self.h);self.a=dict(status='source',site='Plant',site_id='sid',list_id='lid',job='2214',needs_person=False)
    def capture(self):
        records,snap=fixture()
        with patch('hosting.spreadsheets.report_and_export_records',return_value=(records,[])),patch.object(SpreadsheetInbox,'assignment_for',return_value=self.a):
            return self.inbox.source_reconcile(self.h,snap,DAY,expected_check_hash=self.item['check_hash'],expected_assignment=dict(self.a))
    def test_exact_capture_preserves_bytes_and_human_decisions(self):
        self.inbox._update(self.h,lambda r:r.update(finding_decisions={'x':{'action':'defer','reason':'owner must answer'}}));self.item=self.inbox.get(self.h)
        before=self.inbox.original_bytes(self.h)[1];out=self.capture()
        self.assertTrue(out['source_reconciliation']['verified']);self.assertEqual(out['finding_decisions'],self.item['finding_decisions'])
        self.assertEqual(self.inbox.original_bytes(self.h)[1],before)
    def test_report_change_during_compare_discards_capture(self):
        records,snap=fixture();original=reconcile.compare
        def changed(*a,**kw):
            out=original(*a,**kw);self.inbox._update(self.h,lambda r:r.update(note='changed during capture'));return out
        with patch('hosting.spreadsheets.report_and_export_records',return_value=(records,[])),patch.object(reconcile,'compare',side_effect=changed):
            with self.assertRaisesRegex(ValueError,'checks changed'):self.inbox.source_reconcile(self.h,snap,DAY, expected_check_hash=self.inbox.get(self.h)['check_hash'])
        self.assertNotIn('source_reconciliation',self.inbox.get(self.h))
    def test_changed_assignment_discards_capture(self):
        records,snap=fixture()
        with patch('hosting.spreadsheets.report_and_export_records',return_value=(records,[])),patch.object(SpreadsheetInbox,'assignment_for',return_value=dict(self.a,site_id='other')):
            with self.assertRaisesRegex(ValueError,'assignment changed'):
                self.inbox.source_reconcile(self.h,snap,DAY,expected_assignment=self.a, expected_check_hash=self.inbox.get(self.h)['check_hash'])
        self.assertNotIn('source_reconciliation',self.inbox.get(self.h))
    def test_failed_recapture_invalidates_old_success(self):
        self.capture();self.inbox.record_lookup(self.h,'error','Synthetic denied capture')
        saved=self.inbox.get(self.h);self.assertIsNone(reconcile.current(saved));self.assertEqual(saved['sharepoint']['status'],'error')
    def test_partial_legacy_binding_is_not_current(self):
        for bound in ({'file_hash':self.h},{'checker_version':self.item['checker_version']}):
            self.assertIsNone(reconcile.current(dict(self.item,source_reconciliation={'bound_to':bound})))

    def test_current_verified_source_has_no_source_hold(self):
        result=self.capture()
        with patch.object(SpreadsheetInbox,'assignment_for',return_value=self.a):
            holds=self.inbox.holds(result)
        self.assertFalse([h for h in holds if h['code'].startswith('source_')],holds)

    def test_absent_or_historical_source_never_claims_verification(self):
        from hosting.review_summary import summarize
        review=summarize(self.item)['sharepoint']
        self.assertFalse(review['source_verified']);self.assertIn('No current complete',review['message'])
        result=self.capture();result['checker_version']='old';result['source_reconciliation']['bound_to']['checker_version']='old'
        review=summarize(result)['sharepoint'];self.assertFalse(review['source_verified']);self.assertEqual(review['status'],'stale')

    def test_registry_preserves_stable_id_through_pick_and_manual_assignment(self):
        from hosting import registry
        entry=dict(client_key='Plant',client='Plant',job='2214',site='Plant',site_id='sid',list_id='lid',verified=True)
        resolved=registry.resolve({},'Plant - 2214.xlsx','2214',DAY,[entry],[])
        self.assertEqual(resolved['site_id'],'sid')
        saved=registry.assign(self.inbox,self.h,entry,{'email':'synthetic@example.test'},'Synthetic source choice',remember=True)
        self.assertEqual(saved['site_id'],'sid')
        manual=registry.resolve({},'Plant - 2214.xlsx','2214',DAY,[],[],manual=saved)
        self.assertEqual(manual['site_id'],'sid')
        self.assertEqual(self.inbox.setting(registry.REGISTRY_KEY)[-1]['site_id'],'sid')

    def test_two_lists_with_same_client_site_job_remain_ambiguous(self):
        from hosting import registry
        base=dict(client_key='Plant',client='Plant',job='2214',site='Plant',site_id='sid',verified=True)
        entries=registry.entries([dict(base,list_id='one'),dict(base,list_id='two')])
        candidates=registry.candidates_for('2214',entries,[])
        self.assertEqual({c['list_id'] for c in candidates},{'one','two'})
        resolved=registry.resolve({},'Plant - 2214.xlsx','2214',DAY,entries,[])
        self.assertEqual(resolved['status'],'ambiguous')
