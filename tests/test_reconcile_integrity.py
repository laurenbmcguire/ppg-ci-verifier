import importlib.util, sys, types, unittest
from pathlib import Path
from unittest.mock import patch
from hosting import source_patterns
from hosting.adaptive_reports import exact_label_for_header, shape
ROOT=Path(__file__).parents[1]
def module(name,file):
    spec=importlib.util.spec_from_file_location(name,ROOT/file);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
rec=module('reconcile','hosting/reconcile.py');sp=module('sp','hosting/sharepoint.py')
adaptive=types.ModuleType('hosting.adaptive_reports')
adaptive.header_text=lambda x:x
adaptive.exact_label_for_header=exact_label_for_header
adaptive.shape=shape
adaptive.field_for_header=lambda x:{'Qty':'inspected','NOK':'rejected'}.get(x)
hosting=types.ModuleType('hosting');hosting.sharepoint=sp;hosting.reconcile=rec
# Preserve package discovery for real lazy helpers; do not depend on test collection order.
hosting.__path__=[str(ROOT/'hosting')]
registry=types.ModuleType('hosting.registry');registry.RESOLVED=('manual','verified','matched','source');hosting.registry=registry
STUBS={'hosting':hosting,'hosting.adaptive_reports':adaptive,'hosting.reconcile':rec,'hosting.registry':registry,'hosting.sharepoint':sp,'hosting.source_patterns':source_patterns}
class ReconcileIntegrity(unittest.TestCase):
    def setUp(self):self.ctx=patch.dict(sys.modules,STUBS);self.ctx.start();self.addCleanup(self.ctx.stop)
    def pages(self):return [(200,{'id':'list','webUrl':'https://ppgquality.sharepoint.com/sites/SL/Lists/Data'}),(200,{'value':[{'name':'Date','dateTime':{'format':'dateOnly'}}],'@odata.nextLink':'/sites/site/lists/list/columns?$skiptoken=2'}),(200,{'value':[{'name':'Qty','displayName':'Qty'}]}),(200,{'value':[{'id':'1','fields':{'Date':'2026-11-07','Qty':10,'Photo':'actual','Shift':2}}]})]
    def capture(self,pages):
        with patch.object(rec,'_fetch',side_effect=pages),patch.object(rec,'site_id_for',side_effect=AssertionError('must use ID')):
            return rec.snapshot('SL','list',site_id='site')
    def test_full_columns_raw_fields_and_identity_preserved(self):
        snap=self.capture(self.pages());self.assertTrue(snap['coverage']['complete']);self.assertFalse(snap['coverage'].incomplete());self.assertEqual(snap['coverage']['column_pages'],2)
        self.assertEqual(snap['items'][0]['raw']['Photo'],'actual');self.assertEqual(snap['items'][0]['site_id'],'site');self.assertEqual(snap['site_id'],'site')
    def test_column_failure_prevents_verified(self):
        pages=self.pages();pages[2]=(403,None);s=self.capture(pages)
        self.assertTrue(s['coverage'].incomplete());self.assertFalse(s['coverage']['complete'])
    def test_wrong_list_response_rejected(self):
        s=self.capture([(200,{'id':'wrong'})]);self.assertFalse(s['coverage']['complete']);self.assertEqual(s['items'],[])
    def test_duplicate_items_and_repeated_cursor_incomplete(self):
        for last in [{'value':[{'id':'1'},{'id':'1'}]}, {'value':[],'@odata.nextLink':'/sites/site/lists/list/items?$expand=fields&$top=999'}]:
            p=self.pages();p[-1]=(200,last);s=self.capture(p);self.assertTrue(s['coverage'].incomplete());self.assertGreater(s['coverage']['failed_pages'],0)
    def test_malformed_items_do_not_become_empty_success(self):
        p=self.pages();p[-1]=(200,{});s=self.capture(p);self.assertTrue(s['coverage'].incomplete())
    def test_terminal_empty_list_retains_complete_evidence(self):
        p=self.pages();p[-1]=(200,{'value':[]});s=self.capture(p);self.assertFalse(s['coverage'].incomplete());self.assertEqual(s['coverage']['items'],0)
    def test_exact_cap_terminal_not_truncated(self):
        with patch.object(rec,'MAX_ITEMS',1):s=self.capture(self.pages())
        self.assertFalse(s['coverage']['truncated']);self.assertTrue(s['coverage']['complete'])
    def test_cap_with_more_pages_is_incomplete(self):
        p=self.pages();p[-1][1]['@odata.nextLink']='/sites/site/lists/list/items?$skiptoken=2'
        with patch.object(rec,'MAX_ITEMS',1):s=self.capture(p)
        self.assertTrue(s['coverage']['truncated']);self.assertFalse(s['coverage']['complete'])
    def test_foreign_pagination_never_fetched(self):
        p=self.pages();p[-1][1]['@odata.nextLink']='https://evil.example/steal'
        s=self.capture(p);self.assertTrue(s['coverage'].incomplete())
    def test_comparison_rejects_wrong_stable_site_id(self):
        s=self.capture(self.pages());r={'source':{'site_id':'other','list_id':'list','item':'1'},'values':{'inspected':10},'sheet':'Detail','row':2,'columns':{'inspected':2}}
        v=rec.compare('2026-11-07',[r],[],s);self.assertFalse(v['verified']);self.assertIn('wrong_list',[x['status'] for x in v['rows']])
    def test_snapshot_hash_changes_with_raw_values_even_unchanged_versions(self):
        s=self.capture(self.pages());v1=rec.compare('2026-11-07',[],[],s)['snapshot_id'];s['items'][0]['raw']['Qty']=11
        v2=rec.compare('2026-11-07',[],[],s)['snapshot_id'];self.assertNotEqual(v1,v2)
    def test_binding_resolves_only_assigned_site(self):
        with patch.object(rec,'site_id_for',return_value='sid') as lookup:
            self.assertEqual(sp.assignment_identity({'status':'verified','site':'SL','list_id':'list'}),{'site_id':'sid','list_id':'list'})
            lookup.assert_called_once_with('SL',None)
        with self.assertRaises(sp.SharePointError):sp.assignment_identity({'status':'ambiguous','site_id':'sid','list_id':'list'})
    def test_legacy_unbound_export_and_history_disabled(self):
        with patch.object(sp,'configured',return_value=True):
            with self.assertRaises(sp.SharePointError):sp.fetch_export('2207','2026-11-07')
        with self.assertRaises(sp.SharePointError):sp.recent_exports('2207','2026-11-07')
    def test_bound_history_cannot_fallback_to_exports(self):
        with patch.object(sp,'list_history',return_value=[]) as history:
            self.assertEqual(sp.recent_exports('2207','2026-11-07',site_id='sid',list_id='lid'),[])
            self.assertEqual(history.call_args.kwargs,{'site_id':'sid','list_id':'lid'})
    def test_invalid_plain_date_not_accepted(self):
        self.assertIsNone(rec.business_day('2026-99-99'))
    def test_datetime_business_date_beats_submission(self):
        self.assertIsNone(sp._item_day({'fields':{'Date':'2026-11-07T18:00:00Z','Date0':'2026-11-08T08:00:00Z'}}))
        self.assertIsNone(sp._item_day({'fields':{'Date0':'2026-11-08T08:00:00Z'}}))
    def test_submission_fallback_cannot_verify(self):
        pages=self.pages();pages[-1][1]['value'][0]['fields']={'Date0':'2026-11-07T18:00:00Z','Qty':10}
        snap=self.capture(pages);self.assertTrue(snap['coverage'].incomplete());self.assertEqual(snap['coverage']['date_fallbacks'],1)
    def test_shift_suggestion_does_not_overwrite_recorded_shift(self):
        from datetime import datetime
        shifts=types.ModuleType('hosting.shifts');shifts.local_time=lambda _:datetime(2026,11,7,15,0);shifts.assign=lambda *a:({'name':'Second'},'2026-11-07');shifts.number=lambda _:2;shifts.label=lambda _:'Second'
        with patch.dict(sys.modules,{'hosting.shifts':shifts}):
            _,rows,_=sp._by_time({'Shift':'Shift'},[{'id':'1','fields':{'Shift':1,'Date':'2026-11-07','Identifier':'A'}}],[])
        self.assertEqual(rows[0]['fields']['Shift'],1);self.assertEqual(rows[0]['fields']['ShiftFromTime'],2)
if __name__=='__main__':unittest.main()
