import copy
import unittest
from test_all_fields_reports import all_fields
from test_branded_reports import report,source,rehash
from hosting.branded_reports import ACTIVE_TABS_TEMPLATE_ID,reconcile_result


def active_tabs(reject=None):
    w=all_fields(reject)
    for name in ('2nd Shift','3rd Shift'):del w[name]
    if reject is None:del w['Rejects & Photos']
    else:
        reg=w['Rejects & Photos'];reg.title='Rejects';reg['H16']=None;reg['A20']=None
    return w

class ActiveTabsTests(unittest.TestCase):
    def test_dynamic_tabs_map_active_rows_without_false_completion(self):
        r=report(active_tabs());self.assertEqual(r['template_id'],ACTIVE_TABS_TEMPLATE_ID);self.assertEqual(r['template_mapping_status'],'matched');self.assertEqual(r['record_count'],1);self.assertEqual(r['numeric_cells_checked'],2);self.assertEqual(r['absent_shift_ids'],['2','3']);self.assertEqual(r['finding_counts']['shift_activity_unverified'],2)
    def test_absent_photos_never_implies_clean_or_complete(self):
        r=report(active_tabs());self.assertIn('photo_evidence_not_verified',r['finding_counts']);self.assertIn('reject_register_not_present',r['finding_counts']);self.assertFalse(r['approval_effective']);self.assertEqual(r['release_status'],'held')
    def test_known_rejects_remain_checked_on_renamed_tab(self):
        r=report(active_tabs(1));self.assertEqual(r['reject_register_records_checked'],1);self.assertNotIn('reject_register_mapping_mismatch',r['finding_counts']);self.assertNotIn('reject_register_difference',r['finding_counts']);self.assertIn('photo_evidence_not_verified',r['finding_counts'])
    def test_renamed_register_value_change_detected(self):
        w=active_tabs(1);w['Rejects']['E17']=2;self.assertIn('reject_register_difference',report(w)['finding_counts'])
    def test_renamed_register_extra_row_not_ignored(self):
        w=active_tabs(1)
        for c in range(1,6):w['Rejects'].cell(25,c).value=w['Rejects'].cell(17,c).value
        self.assertIn('reject_register_difference',report(w)['finding_counts'])
    def test_conflicting_register_tabs_rejected(self):
        w=active_tabs(1);w.create_sheet('Rejects & Photos');self.assertEqual(report(w)['template_mapping_status'],'incomplete')
    def test_unsupported_extra_sheet_rejected(self):
        w=active_tabs();w.create_sheet('At a glance');self.assertEqual(report(w)['template_mapping_status'],'incomplete')
    def test_all_shift_tabs_removed_rejected(self):
        w=active_tabs();del w['1st Shift'];r=report(w);self.assertEqual(r['template_mapping_status'],'incomplete');self.assertEqual(r['numeric_cells_checked'],0);self.assertIn('no_quantity_checks_performed',r['finding_counts'])
    def test_absent_shift_zero_in_summary_not_accepted(self):
        w=active_tabs();w['Summary']['G22']=0;self.assertIn('unobserved_shift_claim',report(w)['finding_counts'])
    def test_source_activity_on_omitted_shift_detected(self):
        s=source();new=copy.deepcopy(s['records'][0]);new.update(id='2',shift_id='2',identifier='Other');s['records'].append(new);r=reconcile_result(report(active_tabs()),rehash(s));self.assertEqual(r['source_difference_count'],1);self.assertEqual(r['source_reconciliation_status'],'mismatch')
    def test_matching_observed_source_still_held(self):
        r=reconcile_result(report(active_tabs()),source());self.assertEqual(r['numeric_values_compared'],2);self.assertEqual(r['source_difference_count'],0);self.assertEqual(r['release_status'],'held');self.assertIn('photo_evidence_not_verified',r['finding_counts'])
    def test_unexpected_photo_header_requires_new_mapping(self):
        w=active_tabs(1);w['Rejects']['H16']='Photo';self.assertIn('reject_register_mapping_mismatch',report(w)['finding_counts'])

if __name__=='__main__':unittest.main()
