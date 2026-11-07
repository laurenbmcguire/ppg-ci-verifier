import copy
from datetime import datetime
import hashlib
import io
import json
import tempfile
import unittest
from openpyxl import Workbook,load_workbook
from hosting.spreadsheets import inspect_spreadsheet
from hosting.branded_reports import HEADERS,SHIFT_SHEETS,SUMMARY_BRAND,DETAIL_BRAND,fingerprint,reconcile_result
from hosting.spreadsheet_inbox import SpreadsheetInbox
from hosting.report_workflow import WorkflowError


def workbook():
    w=Workbook();s=w.active;s.title='Summary';s['B1']=SUMMARY_BRAND;s['B13']='Project Number';s['D13']='Synthetic-Job';s['B14']='Report Date';s['D14']=datetime(2026,11,3)
    for coordinate,value in {'B8':'Records','D8':'Parts inspected','G8':'Parts certified','J8':'Parts rejected','B20':'Shift','F20':'Records','G20':'Inspected','I20':'Certified','K20':'Rejected'}.items():s[coordinate]=value
    for name,shift in SHIFT_SHEETS.items():
        detail=w.create_sheet(name);detail['A1']=DETAIL_BRAND;detail['A3']=name;detail['C7']='Synthetic-Job';detail['C8']=datetime(2026,11,3);detail['C9']='Shift '+shift;detail['A14']='INSPECTION RECORDS'
        for i,h in enumerate(HEADERS,1):detail.cell(15,i,h)
        if shift=='1':
            for i,value in enumerate([datetime(2026,11,3),1,'A','Part-1','Lot-1',None,10,'OK',None,None,10,None,'Not available'],1):detail.cell(16,i,value)
        row=20+int(shift);s.cell(row,2,'Shift '+shift)
        for col in (6,7,9,11):s.cell(row,col,'n/a')
    ref="'1st Shift'!J16:J16";formulas={'B9':'=SUM(F21:F23)','D9':'=SUM(G21:G23)','G9':'=SUM(I21:I23)','J9':f'=IF(COUNT({ref})=0,"Unreported",SUM({ref})&" known")','F21':"=COUNTA('1st Shift'!A16:A16)",'G21':"=SUM('1st Shift'!G16:G16)",'I21':"=SUM('1st Shift'!K16:K16)",'K21':f'=IF(COUNT({ref})=0,"Unreported",SUM({ref})&" known")','F24':'=B9','G24':'=D9','I24':'=G9','K24':'=J9','J29':'=D9','L29':'=G9','N29':'=J9','J30':'=D9','L30':'=G9','N30':'=J9'}
    for coordinate,value in formulas.items():s[coordinate]=value
    w.create_sheet('Rejects & Photos');return w

def raw(w=None):
    b=io.BytesIO();(w or workbook()).save(b);return b.getvalue()

def report(w=None):return inspect_spreadsheet('Synthetic.xlsx',raw(w))

def source():
    s=dict(schema='verifier-spreadsheet-source/1',scope=dict(site_id='site',list_id='list',job_id='job',project_number='Synthetic-Job',report_date='2026-11-03'),provenance_kind='independently_retrieved',captured_at='2026-11-04T00:00:00Z',source_reference='synthetic://separate-source',complete=True,consistency='unknown',shift_coverage_complete=False,timezone_status='unknown',records=[dict(id='1',date='2026-11-03',shift_id='1',part='Part-1',identifier='Lot-1',values=dict(inspected=10,certified=10,rejected=None),field_presence=dict(inspected='present',certified='present',rejected='absent'))])
    return rehash(s)

def rehash(s):s.pop('source_sha256',None);s['source_sha256']=fingerprint(s);return s


def assert_layout_fallback(test, r, difference=None):
    """A recognized PPG layout that differs from the exact template falls back to automatic column detection,
    records why, and keeps the exact-template differences as information."""
    test.assertEqual(r['mapping_mode'], 'adaptive')
    test.assertTrue(r['recognized_template'].startswith('ppg-'))
    test.assertIn('known_template_layout_differs', r['finding_counts'])
    test.assertTrue(r['template_differences'])
    if difference:
        test.assertIn(difference, r['template_differences'])
    test.assertEqual(r['release_status'], 'held')

class BrandedTests(unittest.TestCase):
    def test_explicit_row15_mapping_checks_quantities(self):
        r=report();self.assertEqual(r['record_count'],1);self.assertEqual(r['numeric_cells_checked'],2);self.assertEqual(r['template_mapping_status'],'matched')
        self.assertEqual(r['sheets'][0]['header_row'],15);self.assertIn('certified',r['sheets'][0]['numeric_columns_checked']);self.assertIsNone(r['totals']['rejected']['total'])
    def test_generic_unmapped_input_is_unverified(self):
        r=inspect_spreadsheet('x.csv',b'label,qty\nx,123\n');self.assertEqual(r['internal_check_status'],'unverified');self.assertIn('no_quantity_mapping',r['finding_counts'])
    def test_wrong_header_never_falls_back_to_guess(self):
        w=workbook();w['1st Shift']['G15']='Qty';r=report(w);assert_layout_fallback(self,r,'template_header_mapping_mismatch')
        # A bare 'Qty' is ambiguous, so it is never treated as the inspected column.
        self.assertNotIn('inspected',[f for s in r['sheets'] if s['name']=='1st Shift' for t in s['tables'] for f in t['columns']])
    def test_unknown_sheet_is_not_silently_ignored(self):
        w=workbook();w.create_sheet('Another Job');r=report(w);assert_layout_fallback(self,r,'template_sheet_mapping_mismatch');self.assertIn('Another Job',[s['name'] for s in r['sheets']])
    def test_summary_formula_wrong_range_is_detected(self):
        w=workbook();w['Summary']['G21']="=SUM('1st Shift'!G16:G15)";r=report(w);self.assertIn('summary_formula_mapping_mismatch',r['finding_counts'])
    def test_formula_in_numeric_data_not_executed(self):
        w=workbook();w['1st Shift']['G16']='=5+5';r=report(w);self.assertIn('quantity_invalid',r['finding_counts']);self.assertIn('unmapped_formula',r['finding_counts']);self.assertFalse(r['formulas_evaluated'])
    def test_report_and_formula_bytes_unchanged(self):
        data=raw();before=hashlib.sha256(data).hexdigest();r=inspect_spreadsheet('x.xlsx',data)
        self.assertEqual(before,r['file_sha256']);self.assertEqual(hashlib.sha256(data).hexdigest(),before);self.assertTrue(r['formulas_preserved']);self.assertEqual(len(r['formulas']),18)
    def test_missing_reject_is_unknown_not_zero(self):
        r=report();self.assertEqual(r['totals']['rejected']['known_sum'],0);self.assertIsNone(r['totals']['rejected']['total']);self.assertEqual(r['totals']['rejected']['missing_count'],1)
    def test_zero_rejected_is_known(self):
        w=workbook();w['1st Shift']['J16']=0;r=report(w);self.assertEqual(r['totals']['rejected']['total'],0);self.assertEqual(r['numeric_cells_checked'],3)
    def test_negative_fractional_and_text_quantities_flagged(self):
        for value in (-1,1.5,'10',True):
            w=workbook();w['1st Shift']['G16']=value
            with self.subTest(value=value):self.assertIn('quantity_invalid',report(w)['finding_counts'])
    def test_wrong_scope_flagged(self):
        w=workbook();w['1st Shift']['C7']='Different Job';self.assertIn('template_scope_mismatch',report(w)['finding_counts'])
        w=workbook();w['1st Shift']['B16']=2;self.assertIn('record_scope_mismatch',report(w)['finding_counts'])
    def test_equal_totals_do_not_imply_row_equality(self):
        s=source();s['records'][0]['values']['certified']=9;rehash(s);r=reconcile_result(report(),s)
        self.assertEqual(r['source_reconciliation_status'],'mismatch');self.assertEqual(r['source_differences'][0]['before'],10);self.assertEqual(r['source_differences'][0]['source_value'],9)
    def test_matching_known_values_still_hold_missing_unknowns(self):
        r=reconcile_result(report(),source());self.assertEqual(r['numeric_values_compared'],2);self.assertEqual(r['source_difference_count'],0);self.assertEqual(r['release_status'],'held');self.assertFalse(r['approval_effective']);self.assertIn('source_quantity_missing',r['finding_counts'])
    def test_new_source_record_marks_stale_workbook(self):
        s=source();extra=copy.deepcopy(s['records'][0]);extra.update(id='2',identifier='Lot-2');s['records'].append(extra);rehash(s);r=reconcile_result(report(),s)
        self.assertIn('source_record_missing_from_workbook',r['finding_counts']);self.assertEqual(r['source_reconciliation_status'],'mismatch');self.assertEqual(r['source_totals']['inspected']['total'],20)
    def test_cross_job_never_compared(self):
        s=source();s['scope']['project_number']='Other';rehash(s);r=reconcile_result(report(),s);self.assertEqual(r['numeric_values_compared'],0);self.assertIn('source_scope_mismatch',r['finding_counts'])
    def test_source_hash_tampering_rejected(self):
        s=source();s['records'][0]['values']['inspected']=8
        with self.assertRaises(WorkflowError):reconcile_result(report(),s)
    def test_source_record_duplicate_rejected(self):
        s=source();s['records'].append(copy.deepcopy(s['records'][0]));rehash(s)
        with self.assertRaises(WorkflowError):reconcile_result(report(),s)
    def test_contradictory_presence_rejected(self):
        s=source();s['records'][0]['field_presence']['inspected']='absent';rehash(s)
        with self.assertRaises(WorkflowError):reconcile_result(report(),s)
    def test_review_binding_changes_with_workbook_or_source(self):
        a=reconcile_result(report(),source());s=source();s['captured_at']='2026-11-04T00:01:00Z';rehash(s);b=reconcile_result(report(),s);self.assertNotEqual(a['review_binding_sha256'],b['review_binding_sha256'])
    def test_reconciliation_persisted_as_new_check_preserving_original(self):
        with tempfile.TemporaryDirectory() as directory:
            inbox=SpreadsheetInbox(directory);data=raw();original=inbox.ingest('x.xlsx',data);r=inbox.reconcile(original['file_hash'],source())
            self.assertNotEqual(r['check_hash'],original['check_hash']);self.assertEqual(inbox.get(original['file_hash'])['check_hash'],r['check_hash'])
            from pathlib import Path
            self.assertEqual((Path(directory)/original['original_path']).read_bytes(),data)
            with inbox.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM spreadsheet_checks').fetchone()[0],2)
            self.assertEqual(inbox.ingest('x.xlsx',data)['check_hash'],r['check_hash'])


    def test_mismatched_known_values_still_count_as_compared(self):
        s=source();s['records'][0]['values'].update(inspected=9,certified=8);rehash(s);r=reconcile_result(report(),s)
        self.assertEqual(r['numeric_values_compared'],2);self.assertNotIn('no_source_quantity_checks',r['finding_counts']);self.assertEqual(r['source_difference_count'],2)
    def test_hash_alone_cannot_override_other_project(self):
        r=report();s=source();s.update(provenance_kind='retained_supplied_snapshot',expected_workbook_sha256=r['file_sha256']);s['scope']['project_number']='Other Job';rehash(s)
        checked=reconcile_result(r,s);self.assertEqual(checked['numeric_values_compared'],0);self.assertIn('source_scope_mismatch',checked['finding_counts'])
    def test_alias_requires_explicit_complete_binding(self):
        r=report();s=source();s['scope']['project_number']='Different Ordering';s['workbook_scope_binding']={'workbook_project_number':'Synthetic-Job','workbook_sha256':r['file_sha256'],'evidence_reference':'synthetic://binding'};rehash(s)
        with self.assertRaises(WorkflowError):reconcile_result(r,s)
    def test_alias_cannot_carry_to_changed_workbook_hash(self):
        r=report();s=source();s['scope']['project_number']='Alternate';s['workbook_scope_binding']=dict(workbook_project_number='Synthetic-Job',source_project_number='Alternate',report_date='2026-11-03',workbook_sha256='0'*64,evidence_reference='synthetic://binding');rehash(s)
        self.assertIn('source_scope_mismatch',reconcile_result(r,s)['finding_counts'])
    def test_old_reconciliation_cannot_become_current_again(self):
        with tempfile.TemporaryDirectory() as directory:
            inbox=SpreadsheetInbox(directory);original=inbox.ingest('x.xlsx',raw());a=source();first=inbox.reconcile(original['file_hash'],a)
            b=source();b['captured_at']='2026-11-04T00:01:00Z';b['records'][0]['values']['certified']=9;rehash(b);second=inbox.reconcile(original['file_hash'],b)
            with self.assertRaises(ValueError):inbox.reconcile(original['file_hash'],a)
            self.assertEqual(inbox.get(original['file_hash'])['check_hash'],second['check_hash'])
            self.assertNotEqual(first['check_hash'],second['check_hash'])
    def test_same_capture_conflicting_source_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            inbox=SpreadsheetInbox(directory);original=inbox.ingest('x.xlsx',raw());a=source();first=inbox.reconcile(original['file_hash'],a)
            a['records'][0]['values']['certified']=9;rehash(a)
            with self.assertRaises(ValueError):inbox.reconcile(original['file_hash'],a)
            self.assertEqual(inbox.get(original['file_hash'])['check_hash'],first['check_hash'])
    def test_repeating_current_comparison_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            inbox=SpreadsheetInbox(directory);original=inbox.ingest('x.xlsx',raw());a=inbox.reconcile(original['file_hash'],source());b=inbox.reconcile(original['file_hash'],source())
            self.assertEqual(a['check_hash'],b['check_hash']);self.assertTrue(b['idempotent'])
    def test_single_observed_shift_reject_alias_is_structurally_safe(self):
        w=workbook();w['Summary']['K21']='=J9';r=report(w);self.assertNotIn('summary_formula_mapping_mismatch',r['finding_counts'])
    def test_large_aggregate_not_rounded(self):
        w=workbook();w['1st Shift']['G16']=10**15
        for i,value in enumerate([datetime(2026,11,3),1,'A','Part-2','Lot-2',None,1,'OK',None,None,1,None,'Not available'],1):w['1st Shift'].cell(17,i,value)
        r=report(w);self.assertIsNone(r['totals']['inspected']['total']);self.assertIn('quantity_aggregate_limit',r['finding_counts'])

    def test_integral_source_float_compares_numerically(self):
        s=source();s['records'][0]['values'].update(inspected=10.0,certified=10.0);rehash(s);r=reconcile_result(report(),s)
        self.assertEqual(r['source_difference_count'],0);self.assertEqual(r['numeric_values_compared'],2)

    def test_legacy_checks_recomputed_without_changing_original(self):
        with tempfile.TemporaryDirectory() as directory:
            inbox=SpreadsheetInbox(directory);data=raw();original=inbox.ingest('x.xlsx',data)
            with inbox.connect() as db:
                old=dict(original);old.pop('checker_version');old.pop('check_hash');old.pop('checked_at');db.execute('DELETE FROM spreadsheet_checks');db.execute('UPDATE spreadsheets SET result_json=?',(json.dumps(old),))
            self.assertEqual(inbox.list()['spreadsheets'][0]['internal_check_status'],'outdated_checks_require_recheck')
            # opening a report never reclassifies it: it stays marked out of date until a person re-checks it
            current=inbox.get(original['file_hash']);self.assertEqual(current['internal_check_status'],'outdated_checks_require_recheck');self.assertNotIn('recheck_applied',current)
            current=inbox.apply_recheck(original['file_hash'],'Reviewer');self.assertEqual(current['numeric_cells_checked'],2);self.assertEqual(current['recheck_applied'][0]['by'],'Reviewer')
            from pathlib import Path
            self.assertEqual((Path(directory)/original['original_path']).read_bytes(),data)

    def test_future_source_cannot_poison_review_freshness(self):
        s=source();s['captured_at']='2099-01-01T00:00:00Z';rehash(s)
        with self.assertRaises(WorkflowError):reconcile_result(report(),s)

if __name__=='__main__':unittest.main()
