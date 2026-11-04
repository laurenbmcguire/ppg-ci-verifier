import unittest
from test_branded_reports import workbook,report,source,rehash
from hosting.branded_reports import ALL_FIELDS_TEMPLATE_ID,reconcile_result,SHIFT_SHEETS


def all_fields(reject=None):
    w=workbook();s=w['Summary']
    for coordinate in ('J9','K21','K24','N29','N30'):s[coordinate]=None
    for name in SHIFT_SHEETS:
        d=w[name];d['A7']='Project';d['A8']='Service type';d['C8']=None;d['A9']='Project description';d['C9']=None;d['I9']='Worksite'
        d['A19']='SHIFT INSPECTION RESULTS'
        for off,label in enumerate(('Inspection records','Parts inspected','Parts certified','Rejected quantity','Defect rate','Inspection pass rate'),20):d.cell(off,1,label)
        if name=='1st Shift':
            d['J16']=reject;d['E20']='=COUNTA(A16:A16)';d['E21']='=SUM(G16:G16)';d['E22']='=SUM(K16:K16)';d['E23']='=IF(COUNT(J16:J16)=0,"",IF(COUNT(J16:J16)=COUNT(G16:G16),SUM(J16:J16),SUM(J16:J16)&" known; partial"))'
    s['B65']='RECORDED RESULTS BY SHIFT'
    for c,v in ((2,'Shift'),(3,'Certified'),(4,'Known rejects')):s.cell(66,c,v)
    for r,name in enumerate(SHIFT_SHEETS,67):s.cell(r,2,name)
    s['C67']="=SUM('1st Shift'!K16:K16)"
    s['B99']='RESULTS BY PART NUMBER'
    for c,v in enumerate(('Part number','Part name','Inspected for','Inspected','Certified','Known rejects','Defect rate'),2):s.cell(100,c,v)
    s['B101']='Part-1';s['E101']="=SUMIF('1st Shift'!D16:D16,B101,'1st Shift'!G16:G16)";s['F101']="=SUMIF('1st Shift'!D16:D16,B101,'1st Shift'!K16:K16)"
    reg=w['Rejects & Photos'];reg['A15']='REJECT REGISTER'
    for c,v in enumerate(('Shift','Identifier / lot','Part number','Inspected','Known rejected','Reason','Comments','Photo'),1):reg.cell(16,c,v)
    reg['A20']='DEFECT PHOTOS'
    if reject is not None:
        s['J9']='=IF(COUNT(\'1st Shift\'!J16:J16)=0,"Unreported",SUM(\'1st Shift\'!J16:J16)&" known")';s['K21']='=J9';s['K24']='=J9';s['N29']='=J9';s['N30']='=J9';s['D67']="=SUM('1st Shift'!J16:J16)";s['G101']="=SUMIF('1st Shift'!D16:D16,B101,'1st Shift'!J16:J16)"
        if reject>0:
            for c,v in enumerate(('1st Shift','Lot-1','Part-1',10,reject),1):reg.cell(17,c,v)
    return w

class AllFieldsTests(unittest.TestCase):
    def test_exact_template_and_quantities(self):
        r=report(all_fields());self.assertEqual(r['template_id'],ALL_FIELDS_TEMPLATE_ID);self.assertEqual(r['template_mapping_status'],'matched');self.assertEqual(r['record_count'],1);self.assertEqual(r['numeric_cells_checked'],2)
        for code in ('template_scope_mismatch','section_formula_mapping_mismatch','summary_formula_mapping_mismatch','unmapped_formula','part_table_coverage_mismatch','reject_register_difference','unknown_total_reference'):self.assertNotIn(code,r['finding_counts'])
    def test_source_still_compared_and_held(self):
        r=reconcile_result(report(all_fields()),source());self.assertEqual(r['numeric_values_compared'],2);self.assertEqual(r['source_difference_count'],0);self.assertEqual(r['release_status'],'held')
    def test_missing_inactive_tab_is_unverified_coverage(self):
        w=all_fields();del w['2nd Shift'];r=report(w);self.assertEqual(r['template_mapping_status'],'matched');self.assertIn('2',r['absent_shift_ids']);self.assertEqual(r['release_status'],'held')
    def test_wrong_project_not_silently_accepted(self):
        w=all_fields();w['1st Shift']['C7']='Other';self.assertIn('template_scope_mismatch',report(w)['finding_counts'])
    def test_wrong_record_date_rejected(self):
        w=all_fields();w['1st Shift']['A16']='2026-11-02';self.assertIn('record_scope_mismatch',report(w)['finding_counts'])
    def test_wrong_footer_formula_rejected(self):
        w=all_fields();w['1st Shift']['E23']='=SUM(J16:J16)';self.assertIn('section_formula_mapping_mismatch',report(w)['finding_counts'])
    def test_all_unknown_rejects_stay_blank_and_null(self):
        r=report(all_fields());self.assertIsNone(r['totals']['rejected']['total']);self.assertIsNone(r['part_totals']['Part-1']['rejected']['total']);self.assertEqual(r['reject_register_records_checked'],0)
    def test_zero_rejects_known_but_not_register(self):
        r=report(all_fields(0));self.assertEqual(r['part_totals']['Part-1']['rejected']['total'],0);self.assertEqual(r['reject_register_records_checked'],0);self.assertNotIn('reject_register_difference',r['finding_counts'])
    def test_positive_reject_register_matches(self):
        r=report(all_fields(1));self.assertEqual(r['reject_register_records_checked'],1);self.assertNotIn('reject_register_difference',r['finding_counts'])
    def test_reject_register_discrepancy_detected(self):
        w=all_fields(1);w['Rejects & Photos']['E17']=2;self.assertIn('reject_register_difference',report(w)['finding_counts'])
    def test_duplicate_reject_register_entry_detected(self):
        w=all_fields(1)
        for c in range(1,6):w['Rejects & Photos'].cell(18,c).value=w['Rejects & Photos'].cell(17,c).value
        self.assertIn('reject_register_difference',report(w)['finding_counts'])
    def test_empty_source_never_compared_as_pass(self):
        s=source();s['records']=[];r=reconcile_result(report(all_fields()),rehash(s));self.assertEqual(r['numeric_values_compared'],0);self.assertIn('no_source_quantity_checks',r['finding_counts']);self.assertEqual(r['release_status'],'held')
    def test_unknown_source_never_compared_as_pass(self):
        s=source();s['records'][0]['values']={k:None for k in s['records'][0]['values']};s['records'][0]['field_presence']={}
        r=reconcile_result(report(all_fields()),rehash(s));self.assertEqual(r['numeric_values_compared'],0);self.assertIn('no_source_quantity_checks',r['finding_counts']);self.assertEqual(r['release_status'],'held')
    def test_shift_swap_not_hidden_by_equal_daily_totals(self):
        s=source();s['records'][0]['shift_id']='3';r=reconcile_result(report(all_fields()),rehash(s));self.assertEqual(r['source_difference_count'],2);self.assertEqual(r['source_reconciliation_status'],'mismatch')
    def test_reject_register_omission_detected(self):
        w=all_fields(1)
        for c in range(1,6):w['Rejects & Photos'].cell(17,c).value=None
        self.assertIn('reject_register_difference',report(w)['finding_counts'])
    def test_part_coverage_not_inferred_from_totals(self):
        w=all_fields();w['Summary']['B101']='Different';self.assertIn('part_table_coverage_mismatch',report(w)['finding_counts'])
    def test_duplicate_part_flagged(self):
        w=all_fields();w['Summary']['B102']='Part-1';self.assertIn('part_table_coverage_mismatch',report(w)['finding_counts'])
    def test_wrong_sumif_range_flagged(self):
        w=all_fields();w['Summary']['E101']="=SUMIF('1st Shift'!D16:D17,B101,'1st Shift'!G16:G17)";self.assertIn('summary_formula_mapping_mismatch',report(w)['finding_counts'])
    def test_unknown_part_reject_not_zero(self):
        w=all_fields();w['Summary']['G101']=0;self.assertIn('unknown_part_total_claim',report(w)['finding_counts'])
    def test_unknown_rate_not_zero(self):
        w=all_fields();w['Summary']['H101']=0;w['1st Shift']['E24']=0;r=report(w);self.assertEqual(r['finding_counts']['unknown_rate_claim'],2)
    def test_unknown_shift_chart_not_zero(self):
        w=all_fields();w['Summary']['C68']=0;self.assertIn('unobserved_shift_claim',report(w)['finding_counts'])
    def test_wildcard_part_criterion_not_exact(self):
        for part in ('*','Part?','~Part','>1','=Part','001'):
            w=all_fields();w['1st Shift']['D16']=part;w['Summary']['B101']=part;self.assertIn('part_sumif_criterion_ambiguous',report(w)['finding_counts'])
    def test_numeric_part_table_label_never_skips_bogus_totals(self):
        w=all_fields(1);w['1st Shift']['D16']='1';w['Summary']['B101']=1;w['Rejects & Photos']['C17']='1'
        for c in ('E101','F101','G101'):w['Summary'][c]=999
        self.assertIn('part_table_coverage_mismatch',report(w)['finding_counts'])
    def test_casefold_part_collision_rejected(self):
        w=all_fields();w['1st Shift']['D16']='ABC';w['Summary']['B101']='ABC'
        for c in range(1,14):w['1st Shift'].cell(17,c).value=w['1st Shift'].cell(16,c).value
        w['1st Shift']['D17']='abc';w['1st Shift']['E17']='Lot2';w['Summary']['B102']='abc'
        self.assertEqual(report(w)['finding_counts']['part_sumif_criterion_ambiguous'],2)
    def test_rate_semantics_not_guessed_even_with_known_counts(self):
        w=all_fields(1);w['Summary']['H101']=99;w['1st Shift']['E24']=99;self.assertEqual(report(w)['finding_counts']['rate_not_verified'],2)
    def test_register_after_boundary_not_silently_discarded(self):
        w=all_fields(1);w['Rejects & Photos']['E25']=1;self.assertIn('reject_record_outside_mapped_table',report(w)['finding_counts'])
    def test_blank_reference_not_known_zero(self):
        w=all_fields();w['Summary']['N30']='=J9';self.assertIn('unknown_total_reference',report(w)['finding_counts'])

if __name__=='__main__':unittest.main()
