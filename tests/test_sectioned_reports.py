import unittest
from datetime import datetime
from test_branded_reports import workbook,report,source
from hosting.branded_reports import SECTIONED_TEMPLATE_ID,reconcile_result


def sectioned():
    w=workbook();s=w['1st Shift'];summary=w['Summary']
    for name in ('2nd Shift','3rd Shift','Rejects & Photos'):del w[name]
    for r in (22,23):
        for c in (2,4,6,7,9,11):summary.cell(r,c).value=None
    for c in ('J8','J9','K20','K21','K24'):summary[c]=None
    for c in ('J15','M15','M16'):s[c]=None
    s['A19']='SHIFT INSPECTION RESULTS'
    for r,label,formula in ((20,'Inspection records','=COUNTA(A16:A16)'),(21,'Parts inspected','=SUM(G16:G16)'),(22,'Parts certified','=SUM(K16:K16)')):s.cell(r,1,label);s.cell(r,5,formula)
    summary['B42']='RECORDED RESULTS BY SHIFT';summary['B44']='Shift';summary['C44']='Certified';summary['B45']='1st Shift';summary['C45']="=SUM('1st Shift'!K16:K16)"
    return w


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

class SectionedTests(unittest.TestCase):
    def test_actual_core_rows_not_appended_results(self):
        r=report(sectioned());self.assertEqual(r['template_id'],SECTIONED_TEMPLATE_ID);self.assertEqual(r['template_mapping_status'],'matched');self.assertEqual(r['record_count'],1);self.assertEqual(r['numeric_cells_checked'],2)
        self.assertEqual(r['totals']['inspected']['total'],10);self.assertEqual(r['absent_shift_ids'],['2','3'])
    def test_removed_optional_field_only_allowed_if_all_cells_empty(self):
        w=sectioned();w['1st Shift']['G15']=None;r=report(w);assert_layout_fallback(self,r)
        w=sectioned();w['1st Shift']['D15']=None;r=report(w);assert_layout_fallback(self,r)
    def test_omitted_reject_header_does_not_create_zero(self):
        r=report(sectioned());self.assertIsNone(r['totals']['rejected']['total']);self.assertIn('quantity_missing',r['finding_counts']);self.assertNotIn('rejected',r['sheets'][0]['numeric_columns_checked'])
    def test_blank_reject_reference_not_accepted_as_zero(self):
        r=report(sectioned());self.assertEqual(r['finding_counts']['unknown_total_reference'],2)
    def test_omitted_shift_not_assumed_complete(self):
        r=report(sectioned());self.assertEqual(r['finding_counts']['shift_activity_unverified'],2);self.assertEqual(r['release_status'],'held')
    def test_section_formula_range_checked(self):
        w=sectioned();w['1st Shift']['E21']='=SUM(G16:G19)';r=report(w);self.assertIn('section_formula_mapping_mismatch',r['finding_counts'])
    def test_extra_section_not_claimed_verified(self):
        w=sectioned();w['1st Shift']['A28']='Hours';w['1st Shift']['E28']=17;r=report(w);self.assertIn('additional_sections_not_verified',r['finding_counts']);self.assertEqual(r['record_count'],1)
    def test_unmapped_formula_still_blocked(self):
        w=sectioned();w['1st Shift']['E28']='=SUM(E20:E22)';self.assertIn('unmapped_formula',report(w)['finding_counts'])
    def test_record_after_boundary_not_silently_discarded(self):
        w=sectioned();w['1st Shift']['A28']=datetime(2026,11,3);w['1st Shift']['G28']=10;r=report(w);self.assertIn('record_outside_mapped_table',r['finding_counts'])
    def test_duplicate_boundary_rejected(self):
        w=sectioned();w['1st Shift']['A30']='SHIFT INSPECTION RESULTS';r=report(w);assert_layout_fallback(self,r,'record_boundary_unverified')
    def test_source_comparison_still_works_and_held(self):
        r=reconcile_result(report(sectioned()),source());self.assertEqual(r['numeric_values_compared'],2);self.assertEqual(r['source_difference_count'],0);self.assertEqual(r['release_status'],'held')
    def test_blank_chart_reject_is_not_complete_zero(self):
        w=sectioned();w['Summary']['D44']='Recorded rejects';w['Summary']['D45']="=SUM('1st Shift'!J16:J16)";self.assertIn('unknown_reject_sum_claim',report(w)['finding_counts'])

if __name__=='__main__':unittest.main()
