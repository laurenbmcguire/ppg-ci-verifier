import base64
import copy
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from io import BytesIO
import urllib.request
import xml.etree.ElementTree as ET
from zipfile import ZipFile
import test_sqlite_copy as sqlite_tests
from hosting.spreadsheet_demo import SpreadsheetDemo,EXAMPLES,corrected_xlsx,digest,NS,sheet_paths
from hosting.spreadsheet_inbox import SpreadsheetInbox
from hosting.sqlite_storage import SQLiteWorkspace,SERVERLESS_MARKERS
from hosting.report_workflow import WorkflowError,WorkflowConflict

class SpreadsheetDemoTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.directory=Path(self.temp.name);self.env=patch.dict(os.environ,{**{k:'' for k in SERVERLESS_MARKERS},'CI_LOCAL_DEV':'1','CI_DEVELOPMENT_DEMO':'1'});self.env.start();self.store=SQLiteWorkspace(self.directory)
        self.raw=(EXAMPLES/'synthetic_ppg_source_mismatch.xlsx').read_bytes();self.source=json.loads((EXAMPLES/'synthetic_ppg_source_mismatch.source.json').read_text())
        with self.store.session() as root:
            i=SpreadsheetInbox(root);s=i.ingest('synthetic.xlsx',self.raw);i.reconcile(s['file_hash'],self.source);self.state=SpreadsheetDemo(root,True).get(s['file_hash'])
        self.binding={k:self.state[k] for k in ('file_hash','check_hash','source_sha256')};self.payload=dict(self.binding,proposal_ids=[p['id'] for p in self.state['proposals']])
    def tearDown(self):self.env.stop();self.temp.cleanup()
    def decide(self,choice='approve',payload=None):
        with self.store.session() as root:return SpreadsheetDemo(root,True).decide(dict(payload or self.payload,decision=choice))
    def apply(self,payload=None):
        with self.store.session() as root:return SpreadsheetDemo(root,True).apply(payload or self.payload)
    def get(self):
        with self.store.session() as root:return SpreadsheetDemo(root,True).get(self.binding['file_hash'])
    def test_exact_proposal_and_durable_decisions(self):
        p=self.state['proposals'][0];self.assertEqual((p['sheet'],p['row'],p['column'],p['cell'],p['before'],p['after']),('1st Shift',17,11,'K17',24,23))
        for choice in ('approve','deny','hold'):
            r=self.decide(choice);self.assertEqual(r['decisions'][0]['decision'],choice);self.assertFalse(r['decisions'][0]['identity_authenticated']);self.assertEqual(r['release_status'],'held')
        self.assertEqual(len(self.get()['events']),3)
    def test_corrected_xlsx_original_formulas_cells_and_package_preserved(self):
        self.decide();result=self.apply();self.assertEqual(result['item']['source_difference_count'],0);self.assertEqual(result['item']['release_status'],'held');self.assertFalse(result['item']['approval_effective']);self.assertEqual(result['review']['proposals'],[])
        with self.store.session() as root:
            self.assertEqual((root/'uploads'/(self.binding['file_hash']+'.xlsx')).read_bytes(),self.raw)
            new,name=SpreadsheetDemo(root,True).download(result['item']['file_hash']);self.assertTrue(name.startswith('DEMO_UNAPPROVED_'))
            self.assertEqual(result['review']['download_url'],SpreadsheetDemo(root,True).get(result['item']['file_hash'])['download_url'])
        paths=sheet_paths(self.raw)
        with ZipFile(BytesIO(self.raw)) as a,ZipFile(BytesIO(new)) as b:
            self.assertEqual(a.namelist(),b.namelist())
            for name in a.namelist():
                if name not in paths.values() and name!='xl/workbook.xml':self.assertEqual(a.read(name),b.read(name),name)
            for sheet,path in paths.items():
                old={c.get('r'):c for c in ET.fromstring(a.read(path)).findall('.//m:c',NS)};updated={c.get('r'):c for c in ET.fromstring(b.read(path)).findall('.//m:c',NS)}
                self.assertEqual(old.keys(),updated.keys())
                for cell,c in old.items():
                    target=updated[cell]
                    if sheet=='1st Shift' and cell=='K17':self.assertEqual(target.find('m:v',NS).text,'23');continue
                    if c.find('m:f',NS) is not None:
                        self.assertEqual(ET.tostring(c.find('m:f',NS)),ET.tostring(target.find('m:f',NS)))
                        for v in list(c.findall('m:v',NS)):c.remove(v)
                        self.assertIsNone(target.find('m:v',NS))
                    self.assertEqual(ET.tostring(c),ET.tostring(target),(sheet,cell))
            calc=ET.fromstring(b.read('xl/workbook.xml')).find('m:calcPr',NS);self.assertEqual(calc.get('forceFullCalc'),'1');self.assertEqual(calc.get('fullCalcOnLoad'),'1')
    def test_new_draft_supersedes_parent_decisions_and_apply(self):
        self.decide();result=self.apply();parent=self.get()
        self.assertFalse(parent['current_version']);self.assertEqual(parent['superseded_by'],result['item']['file_hash']);self.assertTrue(result['review']['current_version'])
        with self.assertRaises(WorkflowConflict):self.apply()
        with self.assertRaises(WorkflowConflict):self.decide()
        with self.store.session() as root:self.assertEqual(SpreadsheetInbox(root).list()['count'],2)
    def test_reject_hold_undecided_and_selection_fail_closed(self):
        for choice in (None,'deny','hold'):
            if choice:self.decide(choice)
            with self.assertRaises(WorkflowConflict):self.apply()
        for ids in ([],None,['unknown'],[self.payload['proposal_ids'][0]]*2,[self.payload['proposal_ids'][0],'unknown']):
            with self.assertRaises(WorkflowError):self.decide(payload=dict(self.payload,proposal_ids=ids))
        self.assertEqual(len(self.get()['decisions']),1)
    def test_faked_identity_rejected(self):
        with self.assertRaises(WorkflowError):self.decide(payload=dict(self.payload,identity={'can_approve':True}))
        with self.assertRaises(WorkflowError):self.apply(dict(self.payload,identity='fake'))
    def test_stale_check_and_source_refused(self):
        self.decide()
        for key in ('check_hash','source_sha256'):
            with self.assertRaises(WorkflowConflict):self.apply(dict(self.payload,**{key:'0'*64}))
        with self.store.session() as root:
            inbox=SpreadsheetInbox(root)
            with inbox.connect() as db:
                current=inbox.get(self.binding['file_hash']);current.pop('check_hash');current.pop('checked_at');current['test_revision']='new comparison';inbox.save_check(db,current)
        with self.assertRaises(WorkflowConflict):self.apply()
    def test_source_not_exact_fixture_refused(self):
        source=copy.deepcopy(self.source);source['captured_at']='2026-11-04T01:00:00Z';source.pop('source_sha256')
        from hosting.report_workflow import fingerprint
        source['source_sha256']=fingerprint(source)
        with self.store.session() as root:SpreadsheetInbox(root).reconcile(self.binding['file_hash'],source)
        self.assertFalse(self.get()['eligible'])
        with self.assertRaises(WorkflowConflict):self.apply()
    def test_nonallowlisted_and_original_download_refused(self):
        with self.store.session() as root:
            inbox=SpreadsheetInbox(root);other=inbox.ingest('other.csv',b'label,count\nsynthetic,1\n');demo=SpreadsheetDemo(root,True)
            self.assertFalse(demo.get(other['file_hash'])['eligible'])
            with self.assertRaises(WorkflowError):demo.download(self.binding['file_hash'])
    def test_flags_and_serverless(self):
        for key in ('CI_DEVELOPMENT_DEMO','CI_LOCAL_DEV','VERCEL'):
            with patch.dict(os.environ,{key:'1' if key=='VERCEL' else '0'}),self.assertRaises(WorkflowError):SpreadsheetDemo(self.directory,True)
        with self.assertRaises(WorkflowError):SpreadsheetDemo(self.directory,False)
    def test_outer_transaction_rolls_back_new_file_and_checks_on_audit_error(self):
        self.decide()
        with self.store.session() as root:before=SpreadsheetInbox(root).list();uploads=sorted(p.name for p in (root/'uploads').iterdir())
        with patch.object(SpreadsheetDemo,'log',side_effect=RuntimeError('audit unavailable')),self.assertRaises(RuntimeError):self.apply()
        with self.store.session() as root:
            self.assertEqual(SpreadsheetInbox(root).list(),before);self.assertEqual(sorted(p.name for p in (root/'uploads').iterdir()),uploads)
    def test_formula_and_unknown_cell_mutation_refused(self):
        p=copy.deepcopy(self.state['proposals'][0]);p.update(cell='E22',before=34)
        with self.assertRaises(WorkflowError):corrected_xlsx(self.raw,[p])
        p.update(cell='J17',before=0)
        with self.assertRaises(WorkflowError):corrected_xlsx(self.raw,[p])

class SpreadsheetDemoHTTPTests(SpreadsheetDemoTests):
    # HTTP cases inherit model coverage intentionally, exercising the same fixture.
    start_server=sqlite_tests.SQLiteTests.start_server;stop_server=sqlite_tests.SQLiteTests.stop_server;request=sqlite_tests.SQLiteTests.request
    def test_http_routes_download_and_guards(self):
        self.start_server()
        try:
            status,s=self.request('/api/spreadsheets/demo/review?id='+self.binding['file_hash']);self.assertEqual(status,200,s);self.assertTrue(s['eligible'])
            status,s=self.request('/api/spreadsheets/demo/decisions',dict(self.payload,decision='approve'));self.assertEqual(status,200,s)
            status,s=self.request('/api/spreadsheets/demo/apply',self.payload);self.assertEqual(status,200,s)
            with urllib.request.urlopen(self.base+s['download_url']) as response:
                self.assertEqual(response.headers['Content-Type'],'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet');self.assertEqual(digest(response.read()),s['item']['file_hash'])
            with patch.dict(os.environ,{'CI_DEVELOPMENT_DEMO':'0'}):self.assertEqual(self.request('/api/spreadsheets/demo/review?id='+self.binding['file_hash'])[0],403)
            self.assertEqual(self.request('/api/spreadsheets/demo/download?id='+self.binding['file_hash'])[0],409)
            with patch.dict(os.environ,{'VERCEL':'1'}):self.assertEqual(self.request('/api/spreadsheets/demo/apply',self.payload)[0],403)
        finally:self.stop_server()

class SpreadsheetDemoTwoProposalTests(unittest.TestCase):
    """Exact allowlisted two-cell fixture in a temporary directory, never shipped samples."""
    decide=SpreadsheetDemoTests.decide
    apply=SpreadsheetDemoTests.apply
    get=SpreadsheetDemoTests.get
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.directory=Path(self.temp.name)/'workspace'
        env=patch.dict(os.environ,{**{k:'' for k in SERVERLESS_MARKERS},'CI_LOCAL_DEV':'1','CI_DEVELOPMENT_DEMO':'1'});env.start();self.addCleanup(env.stop)
        self.package_before={p.name:digest(p.read_bytes()) for p in EXAMPLES.iterdir() if p.is_file()}
        self.store=SQLiteWorkspace(self.directory)
        fixture_dir=Path(self.temp.name)/'exact-synthetic-fixtures';fixture_dir.mkdir()
        original=(EXAMPLES/'synthetic_ppg_source_mismatch.xlsx').read_bytes()
        # A second deliberate mismatch, without deriving or changing source evidence.
        self.raw=corrected_xlsx(original,[dict(sheet='1st Shift',cell='K16',before=10,after=11)])
        source_raw=(EXAMPLES/'synthetic_ppg_source_mismatch.source.json').read_bytes();self.source=json.loads(source_raw)
        (fixture_dir/'two-differences.xlsx').write_bytes(self.raw)
        (fixture_dir/'separate-source.json').write_bytes(source_raw)
        index=dict(synthetic=True,examples=[dict(synthetic=True,artifact=dict(path='two-differences.xlsx',sha256=digest(self.raw)),source=dict(path='separate-source.json',sha256=digest(source_raw)))])
        (fixture_dir/'index.json').write_text(json.dumps(index))
        examples=patch('hosting.spreadsheet_demo.EXAMPLES',fixture_dir);examples.start();self.addCleanup(examples.stop)
        with self.store.session() as root:
            inbox=SpreadsheetInbox(root);item=inbox.ingest('two-differences.xlsx',self.raw);item=inbox.reconcile(item['file_hash'],self.source)
            self.state=SpreadsheetDemo(root,True).get(item['file_hash'])
        self.assertEqual(len(self.state['proposals']),2)
        self.assertEqual({(p['cell'],p['before'],p['after']) for p in self.state['proposals']},{('K16',11,10),('K17',24,23)})
        self.binding={k:self.state[k] for k in ('file_hash','check_hash','source_sha256')}
        self.payload=dict(self.binding,proposal_ids=[p['id'] for p in self.state['proposals']])
    def tearDown(self):
        self.assertEqual({p.name:digest(p.read_bytes()) for p in EXAMPLES.iterdir() if p.is_file()},self.package_before)
    def test_accept_both_atomically_and_apply_exact_two_cells(self):
        state=self.decide()
        self.assertEqual(len(state['decisions']),2);self.assertTrue(all(d['decision']=='approve' for d in state['decisions']))
        self.assertEqual(len(state['events']),2)
        result=self.apply();self.assertEqual(result['item']['source_difference_count'],0);self.assertEqual(result['review']['proposals'],[])
        self.assertEqual(result['item']['release_status'],'held');self.assertFalse(result['item']['approval_effective'])
        with self.store.session() as root:
            self.assertEqual((root/'uploads'/(self.binding['file_hash']+'.xlsx')).read_bytes(),self.raw)
            new,_=SpreadsheetDemo(root,True).download(result['item']['file_hash'])
        from hosting.spreadsheet_demo import numeric_cell
        path=sheet_paths(new)['1st Shift'];self.assertEqual(numeric_cell(new,path,'K16'),10);self.assertEqual(numeric_cell(new,path,'K17'),23)
    def test_rejected_selection_blocks_mixed_apply_and_explicit_subset_leaves_difference(self):
        self.decide();ids={p['cell']:p['id'] for p in self.state['proposals']}
        state=self.decide('deny',dict(self.payload,proposal_ids=[ids['K17']]))
        self.assertEqual({d['decision'] for d in state['decisions']},{'approve','deny'})
        with self.assertRaises(WorkflowConflict):self.apply()
        with self.store.session() as root:self.assertEqual(SpreadsheetInbox(root).list()['count'],1)
        result=self.apply(dict(self.payload,proposal_ids=[ids['K16']]))
        self.assertEqual(result['item']['source_difference_count'],1)
        self.assertEqual([(p['cell'],p['before'],p['after']) for p in result['review']['proposals']],[('K17',24,23)])
        self.assertEqual(result['review']['decisions'],[])
        parent=self.get();self.assertFalse(parent['current_version']);self.assertEqual(parent['superseded_by'],result['item']['file_hash'])
        with self.assertRaises(WorkflowConflict):self.decide()
        with self.assertRaises(WorkflowConflict):self.apply(dict(self.payload,proposal_ids=[ids['K16']]))
        self.assertTrue(result['review']['current_version'])
    def test_second_audit_failure_rolls_back_entire_batch(self):
        original_log=SpreadsheetDemo.log;calls=[]
        def fail_second(instance,*args,**kwargs):
            calls.append(True)
            if len(calls)==2:raise RuntimeError('injected second audit failure')
            return original_log(instance,*args,**kwargs)
        with patch.object(SpreadsheetDemo,'log',new=fail_second),self.assertRaisesRegex(RuntimeError,'second audit'):
            self.decide()
        self.assertEqual(len(calls),2)
        self.assertEqual(self.get()['decisions'],[]);self.assertEqual(self.get()['events'],[])
        with self.assertRaises(WorkflowConflict):self.apply()
        self.assertEqual(len(self.decide()['decisions']),2)
    def test_invalid_batch_never_records_partial_decisions(self):
        for ids in ([],[self.payload['proposal_ids'][0]]*2,[self.payload['proposal_ids'][0],'unknown']):
            with self.assertRaises(WorkflowError):self.decide(payload=dict(self.payload,proposal_ids=ids))
            self.assertEqual(self.get()['decisions'],[]);self.assertEqual(self.get()['events'],[])

if __name__=='__main__':unittest.main()
