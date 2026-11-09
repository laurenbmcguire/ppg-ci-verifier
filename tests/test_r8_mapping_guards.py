"""Exact-source map migration, concurrent edits and stale verification."""
import tempfile
import unittest
from unittest.mock import patch
from hosting import reconcile, change_summary
from hosting.spreadsheet_inbox import SpreadsheetInbox
from test_r8_count_contract import fixture, DAY
from eos_fixture import build, ROWS

class MappingGuards(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.box=SpreadsheetInbox(self.tmp.name)
        self.h=self.box.ingest('report.xlsx',build(rows=list(ROWS)[:2]))['file_hash']
        self.a=dict(status='source',site='Plant',site_id='sid',list_id='lid',needs_person=False)
    def capture(self):
        records,snap=fixture()
        with patch('hosting.spreadsheets.report_and_export_records',return_value=(records,[])):
            return self.box.source_reconcile(self.h,snap,DAY, expected_check_hash=self.box.get(self.h)['check_hash'])
    def test_same_list_on_two_sites_never_shares_confirmed_columns(self):
        self.box.confirm_columns('lid',{'inspected':'I','rejected':'R'},'reviewer',site_id='sid',expected_mapping_revision=None)
        self.assertEqual(self.box.source_mapping('sid','lid')['mapping']['rejected'],'R')
        self.assertEqual(self.box.source_mapping('other','lid')['mapping'],{})
        self.box.confirm_columns('lid',{'rejected':'Other'},'reviewer',site_id='other',expected_mapping_revision=None)
        self.assertEqual(self.box.source_mapping('sid','lid')['mapping']['rejected'],'R')
        with self.assertRaisesRegex(ValueError,'site_id'):self.box.confirm_columns('lid',{'rejected':'R'},'reviewer')
    def test_legacy_map_preserved_but_requires_exact_reconfirmation(self):
        legacy={'inspected':'LegacyWrong','rejected':'R'};self.box.set_setting('column_map|lid',legacy)
        result=self.capture();rec=result['source_reconciliation'];self.assertFalse(rec['verified'])
        self.assertEqual(rec['column_mapping']['inspected']['status'],'needs_confirmation');self.assertEqual(rec['column_mapping']['inspected']['column'],'I')
        self.assertTrue(rec['mapping_binding']['legacy_requires_confirmation']);self.assertEqual(rec['mapping_binding']['mapping'],{})
        self.assertEqual(self.box.setting('column_map|lid'),legacy)
        self.box.confirm_columns('lid',{'inspected':'I','rejected':'R'},'reviewer',site_id='sid',expected_mapping_revision=None)
        fresh=self.capture();self.assertTrue(fresh['source_reconciliation']['verified'])
        self.assertEqual(self.box.setting('column_map|lid'),legacy)
    def test_changed_mapping_during_capture_is_discarded(self):
        records,snap=fixture();compare=reconcile.compare
        def race(*a,**kw):
            result=compare(*a,**kw);self.box.confirm_columns('lid',{'rejected':'R'},'reviewer',site_id='sid',expected_mapping_revision=None);return result
        with patch('hosting.spreadsheets.report_and_export_records',return_value=(records,[])),patch.object(reconcile,'compare',side_effect=race):
            with self.assertRaisesRegex(ValueError,'mapping changed'):self.box.source_reconcile(self.h,snap,DAY, expected_check_hash=self.box.get(self.h)['check_hash'])
        self.assertNotIn('source_reconciliation',self.box.get(self.h))
    def test_map_change_invalidates_hold_and_pdf_but_leaves_report_bytes(self):
        result=self.capture();before=self.box.original_bytes(self.h)[1]
        with patch.object(SpreadsheetInbox,'assignment_for',return_value=self.a):
            self.assertFalse([h for h in self.box.holds(result) if h['code'].startswith('source_')])
        self.box.confirm_columns('lid',{'rejected':'R'},'reviewer',site_id='sid',expected_mapping_revision=None)
        with patch.object(SpreadsheetInbox,'assignment_for',return_value=self.a):
            self.assertTrue(any(h['code']=='source_mapping_changed' for h in self.box.holds(result)))
            data=change_summary.collect(self.box,self.h)
            from api.index import handler
            view=handler.__new__(handler).report_view(self.box,self.h)
            self.assertFalse(view['review']['sharepoint']['source_verified'])
            self.assertEqual(view['review']['sharepoint']['status'],'unverified')
        self.assertEqual(data['source_receipt']['status'],'held');self.assertEqual(self.box.original_bytes(self.h)[1],before)
        fresh=self.capture()
        with patch.object(SpreadsheetInbox,'assignment_for',return_value=self.a):
            self.assertFalse([h for h in self.box.holds(fresh) if h['code'].startswith('source_')])

    def test_changed_mapping_refuses_old_source_corrections(self):
        from tests.test_correction_workflow import reviewed, chosen
        from tests.review_request_fixture import apply_displayed
        from hosting.fixes import FixError
        import shutil
        box,h,raw,props=reviewed();self.addCleanup(shutil.rmtree,box.root,True)
        rec=box.get(h)['source_reconciliation']
        box.confirm_columns(rec['list_id'],{'rejected':'QtyNOK'},'reviewer',site_id=rec['site_id'],expected_mapping_revision=rec['mapping_binding']['revision'])
        with self.assertRaisesRegex(FixError,'mapping changed'):apply_displayed(box,h,chosen(props))
        self.assertEqual(box.original_bytes(h)[1],raw);self.assertFalse(box.get(h).get('fixes_applied'))

    def test_concurrent_confirmations_compare_and_swap_revision(self):
        from concurrent.futures import ThreadPoolExecutor
        def confirm(_):
            try:
                self.box.confirm_columns('lid',{'rejected':'R'},'reviewer',site_id='sid',expected_mapping_revision=None)
                return 'saved'
            except ValueError as exc:
                self.assertIn('changed after',str(exc));return 'stale'
        with ThreadPoolExecutor(max_workers=2) as pool:self.assertEqual(sorted(pool.map(confirm,range(2))),['saved','stale'])
        revision=self.box.source_mapping('sid','lid')['revision']
        self.box.confirm_columns('lid',{'inspected':'I'},'reviewer',site_id='sid',expected_mapping_revision=revision)
        self.assertNotEqual(revision,self.box.source_mapping('sid','lid')['revision'])
    def test_report_bound_confirmation_checks_receipt_and_candidates(self):
        result=self.capture();rec=result['source_reconciliation']
        kwargs=dict(site_id='sid',expected_mapping_revision=None,file_hash=self.h,
                    expected_check_hash=result['check_hash'],expected_snapshot_id=rec['snapshot_id'])
        with patch.object(SpreadsheetInbox,'assignment_for',return_value=self.a):
            for change in ({'expected_check_hash':'old'},{'expected_snapshot_id':'old'}):
                with self.assertRaisesRegex(ValueError,'comparison changed'):
                    self.box.confirm_columns('lid',{'rejected':'R'},'reviewer',**dict(kwargs,**change))
            with self.assertRaisesRegex(ValueError,'present in the displayed'):
                self.box.confirm_columns('lid',{'rejected':'NotCaptured'},'reviewer',**kwargs)
            self.box.confirm_columns('lid',{'rejected':'R'},'reviewer',**kwargs)
            with self.assertRaisesRegex(ValueError,'changed after'):
                self.box.confirm_columns('lid',{'rejected':'R'},'reviewer',**kwargs)
