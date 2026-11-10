"""Execute actual patched handler methods without starting a server or loading secrets."""
import ast, contextlib, sys, types, unittest
from pathlib import Path
from unittest.mock import Mock, patch
from test_reconcile_integrity import STUBS, sp, rec, registry

class FakeInbox:
    item={'filename':'2207.xlsx','records':[{'source':{'item':'1'}}]}
    assignment={'status':'verified','site':'SL','site_id':'sid','list_id':'lid'}
    calls=[]
    def __init__(self,d):pass
    def get(self,h):return self.item
    def shift_schedule(self,j):return []
    def setting(self,key,default=None):return default
    def assignment_for(self,*a):return self.assignment
    def source_reconcile(self,*a,**kw):self.calls.append(('reconcile',a));return 'reconciled'
    def record_lookup(self,*a):self.calls.append(('lookup',a));return 'held'
    def compare_source(self,*a):self.calls.append(('compare',a));return 'compared'
    def history_for(self,*a,**k):raise AssertionError('job-only saved history must not be used')
    def record_baseline(self,h,b):return b

src=types.ModuleType('hosting.source_compare');src.identify=lambda *a:('2207','2026-11-07');src.SourceError=ValueError;src.parse_export=lambda *a:[]
review=types.ModuleType('hosting.review_summary');review.summarize=lambda *a,**k:{'day':'2026-11-07'}
base=types.ModuleType('hosting.baseline');base.MAX_DAYS=10;base.stats_from_result=lambda i:{};base.stats_from_rows=lambda r:{};base.compare_to_usual=lambda *a,**k:{'history':a[1]}
HANDLER_AST=ast.parse((Path(__file__).parents[1]/'api/index.py').read_text())
methods=[n for c in HANDLER_AST.body if isinstance(c,ast.ClassDef) for n in c.body if isinstance(n,ast.FunctionDef) and n.name in ('sharepoint_check','baseline_check')]
ns={'SpreadsheetInbox':FakeInbox,'debug_log':lambda:None,'sharepoint_configured':lambda:True}
exec(compile(ast.fix_missing_locations(ast.Module(body=methods,type_ignores=[])),'actual_handler_methods','exec'),ns)
class Harness:
    sharepoint_check=ns['sharepoint_check'];baseline_check=ns['baseline_check']
    def store(self):return types.SimpleNamespace(session=lambda:contextlib.nullcontext('directory'))

class ApiBindingTests(unittest.TestCase):
    def setUp(self):
        self.ctx=patch.dict(sys.modules,{**STUBS,'hosting.source_compare':src,'hosting.review_summary':review,'hosting.baseline':base});self.ctx.start();self.addCleanup(self.ctx.stop)
        FakeInbox.calls=[];FakeInbox.assignment={'status':'verified','site':'SL','site_id':'sid','list_id':'lid'};FakeInbox.item={'filename':'2207.xlsx','records':[{'source':{'item':'1'}}]}
    def test_primary_exact_reconcile_path_passes_stable_ids(self):
        with patch.object(rec,'snapshot',return_value={}) as snap:
            self.assertEqual(Harness().sharepoint_check('hash'),'reconciled')
            self.assertEqual(snap.call_args.args,('SL','lid'));self.assertEqual(snap.call_args.kwargs['site_id'],'sid')
    def test_unresolved_assignment_holds_without_lookup(self):
        FakeInbox.assignment={'status':'ambiguous','site':'SL','list_id':'lid'}
        with patch.object(rec,'snapshot',side_effect=AssertionError('no read')),patch.object(sp,'fetch_export',side_effect=AssertionError('no fallback')):
            self.assertEqual(Harness().sharepoint_check('hash'),'held')
    def test_no_row_ids_fallback_still_exact_bound(self):
        FakeInbox.item['records']=[]
        with patch.object(sp,'fetch_export',return_value=('file',b'data',{})) as export:
            self.assertEqual(Harness().sharepoint_check('hash'),'compared')
            self.assertEqual(export.call_args.kwargs['site_id'],'sid');self.assertEqual(export.call_args.kwargs['list_id'],'lid')
    def test_primary_capture_failure_saved_as_error(self):
        with patch.object(rec,'snapshot',side_effect=sp.SharePointError('failed page')):
            self.assertEqual(Harness().sharepoint_check('hash'),'held')
        self.assertIn('failed page',FakeInbox.calls[0][1][2])
    def test_baseline_uses_only_exact_list_no_legacy_exports(self):
        with patch.object(sp,'_list_items',return_value=('SL',{'displayName':'list'},{},[])) as items,patch.object(sp,'list_history',return_value=[]) as hist,patch.object(sp,'recent_exports',side_effect=AssertionError('no fallback')):
            result=Harness().baseline_check('hash')
        self.assertEqual(items.call_args.kwargs,{'site_id':'sid','list_id':'lid'});self.assertEqual(hist.call_args.kwargs['site_id'],'sid');self.assertEqual(result['source_identity']['list_id'],'lid')
    def test_baseline_unresolved_no_cross_site_history(self):
        FakeInbox.assignment={'status':'ambiguous'}
        with patch.object(sp,'_list_items',side_effect=AssertionError('no lookup')):
            result=Harness().baseline_check('hash')
        self.assertIn('Resolve',result['coverage_note']);self.assertEqual(result['history'],[])
if __name__=='__main__':unittest.main()
