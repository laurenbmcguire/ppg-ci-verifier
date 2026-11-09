"""Synthetic exact-source counts: quantities are not row counts and unknown is not zero."""
import copy
import unittest
from hosting import reconcile
from hosting.spreadsheets import CHECKER_VERSION

DAY = '2026-11-07'
def fixture():
    columns = {'I': {'name':'I','displayName':'Quantity Inspected'}, 'R': {'name':'R','displayName':'Quantity Rejected'}}
    snap = dict(site='Plant',site_id='sid',list_id='lid',list={'id':'lid','url':'https://example.test/sites/Plant/Lists/Data'},
                captured_at='2026-11-08T00:00:00Z', columns=columns, quantity_candidates={'inspected':['I'],'rejected':['R']},
                coverage={'complete':True},items=[dict(item=str(n),day=DAY,day_basis='business_date',raw={'I':i,'R':r}) for n,i,r in [(1,10,0),(2,20,3)]])
    records=[dict(sheet='Detail',row=n+1,source={'site_id':'sid','list_id':'lid','item':str(n)},values={'inspected':i,'rejected':r},columns={'inspected':'B','rejected':'C'}) for n,i,r in [(1,10,0),(2,20,3)]]
    return records,snap

def compare(records,snap):
    return reconcile.compare(DAY,records,[],snap,report={'file_hash':'abc','checker_version':CHECKER_VERSION})

class CountContract(unittest.TestCase):
    def test_full_counts_and_quantities_are_distinct_and_input_unchanged(self):
        records,snap=fixture();before=copy.deepcopy((records,snap));r=compare(records,snap);c=r['count_reconciliation']
        self.assertTrue(r['verified']);self.assertEqual(c['status'],'verified');self.assertEqual(c['record_counts']['report_rows'],2)
        self.assertEqual(c['quantities']['inspected']['report']['total'],'30');self.assertEqual(c['quantities']['rejected']['source']['total'],'3')
        self.assertEqual(c['rejected_records']['report']['total'],'1');self.assertEqual((records,snap),before)
    def test_blank_rejections_hold_but_explicit_zero_verifies(self):
        for value in (None,'',float('nan'),float('inf')):
            with self.subTest(value=value):
                records,snap=fixture();records[0]['values']['rejected']=value;snap['items'][0]['raw']['R']=value
                r=compare(records,snap);self.assertFalse(r['verified']);self.assertIn(r['status'],('unverified','discrepancies'))
                self.assertIsNone(r['count_reconciliation']['quantities']['rejected']['source']['total'])
        records,snap=fixture()
        for record in records:record['values']['rejected']=0
        for item in snap['items']:item['raw']['R']=0
        self.assertTrue(compare(records,snap)['verified'])
    def test_duplicate_is_explained_without_exclusion_or_deletion(self):
        records,snap=fixture();records.append(dict(copy.deepcopy(records[0]),row=9));r=compare(records,snap);c=r['count_reconciliation']
        self.assertFalse(r['verified']);self.assertEqual(c['duplicates'][0]['row'],9);self.assertIn('remain unchanged',c['duplicates'][0]['evidence'])
        self.assertEqual(c['record_counts']['duplicate_report_rows'],1);self.assertEqual(len(records),3);self.assertFalse(c['exclusions_applied'])
    def test_missing_source_item_and_mismatch_hold(self):
        records,snap=fixture();records.pop();r=compare(records,snap);self.assertFalse(r['verified'])
        self.assertEqual(r['count_reconciliation']['record_counts']['missing_source_items'],1)
        records,snap=fixture();records[1]['values']['rejected']=2;r=compare(records,snap)
        self.assertEqual(r['count_reconciliation']['quantities']['rejected']['status'],'differs')
        self.assertTrue(any(x['status']=='value_differs' for x in r['rows']))
    def test_exclusions_require_actual_business_date_evidence(self):
        records,snap=fixture();snap['items'] += [dict(item='3',day='2026-11-06',day_basis='business_date',raw={'I':5,'R':0}),dict(item='4',day=None,day_basis='unknown',raw={})]
        snap['coverage']['undated']=1;r=compare(records,snap);self.assertFalse(r['verified'])
        self.assertEqual([x['item'] for x in r['count_reconciliation']['exclusions']],['3'])
    def test_coverage_gap_and_wrong_identity_never_verify(self):
        for mutate in (lambda s:s['coverage'].update(complete=False),lambda s:s.update(site_id='other')):
            records,snap=fixture();mutate(snap);r=compare(records,snap);self.assertFalse(r['verified'])
            self.assertEqual(r['count_reconciliation']['status'],'held')
    def test_missing_rejection_column_and_empty_sources_are_unavailable(self):
        records,snap=fixture();snap['quantity_candidates'].pop('rejected');snap['columns'].pop('R')
        r=compare(records,snap);self.assertFalse(r['verified']);self.assertEqual(r['count_reconciliation']['quantities']['rejected']['status'],'unavailable')
        r=compare([],dict(snap,items=[]));self.assertIsNone(r['count_reconciliation']['quantities']['inspected']['source']['total'])

    def test_full_counts_survive_displayed_mismatch_truncation(self):
        records,snap=fixture()
        records=[dict(copy.deepcopy(records[0]),row=n+1) for n in range(2105)]
        result=compare(records,snap);counts=result['count_reconciliation']
        self.assertEqual(len(result['rows']),2000);self.assertTrue(result['rows_truncated']);self.assertFalse(result['verified'])
        self.assertEqual(counts['record_counts']['report_rows'],2105)
        self.assertEqual(counts['record_counts']['duplicate_report_rows'],2104)
        self.assertEqual(len(counts['duplicates']),2104)
        self.assertEqual(counts['quantities']['inspected']['report']['total'],'21050')

    def test_negative_reject_quantity_cannot_be_verified(self):
        records,snap=fixture();records[0]['values']['rejected']=-1;snap['items'][0]['raw']['R']=-1
        self.assertFalse(compare(records,snap)['verified'])

    def test_nexus_totals_preserve_unplaced_source_records(self):
        from hosting import builder
        from datetime import datetime
        rows=[dict(time=datetime(2026,11,7,8),inspected=10,rejected=0,certified=10),dict(time=None,inspected=20,rejected=3,certified=17)]
        result=builder.nexus_split(rows)
        self.assertEqual(result['total'],dict(scans=2,inspected=30,rejected=3,certified=27))
        self.assertEqual(result['unplaced'],1);self.assertEqual(result['status'],'held');self.assertFalse(result['coverage']['complete'])
        self.assertEqual(sum(s['scans'] for s in result['shifts']),1)
