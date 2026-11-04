"""Synthetic simulation decisions never become authenticated human approval."""
import copy
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from test_report_workflow import fixture
import test_sqlite_copy as sqlite_tests
from hosting.report_workflow import ReportWorkflow, WorkflowError, WorkflowConflict, fingerprint
from hosting.sqlite_storage import SERVERLESS_MARKERS, SQLiteWorkspace


def changed():
    m=fixture();m['report']['records'][0]['values']['certified']=8;m['report']['totals']['certified']=8
    return m

class DemoModelTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.env=patch.dict(os.environ,{**{k:'' for k in SERVERLESS_MARKERS},'CI_LOCAL_DEV':'1','CI_DEVELOPMENT_DEMO':'1'});self.env.start()
        self.w=ReportWorkflow(self.temp.name,development_demo=True);self.original=changed();self.state=self.w.ingest(self.original);self.mh=self.state['manifest_hash'];self.ids=[p['id'] for p in self.state['proposals']]
    def tearDown(self):self.env.stop();self.temp.cleanup()
    def decide(self,decision='approve',pid=None):return self.w.demo_decide(self.mh,pid or self.ids[0],decision,self.state['report_hash'],self.state['source_hash'])
    def apply(self,ids=None):return self.w.demo_apply(self.mh,self.ids if ids is None else ids,'demo-2',self.state['report_hash'],self.state['source_hash'])
    def test_approve_deny_hold_are_audited_not_identity(self):
        for choice in ('approve','deny','hold'):
            state=self.decide(choice);d=state['demo_decisions'][0]
            self.assertEqual(d['decision'],choice);self.assertEqual(d['actor'],'development-demo-simulator');self.assertFalse(d['identity_authenticated']);self.assertTrue(d['simulation_only'])
            self.assertEqual(state['release_status'],'held');self.assertFalse(state['approval_effective']);self.assertIsNone(state['approval'])
        self.assertEqual(len([e for e in state['events'] if e['event']=='demo_proposal_decision']),3)
        reopened=ReportWorkflow(self.temp.name,development_demo=True).get(self.mh);self.assertEqual(reopened['demo_decisions'],state['demo_decisions'])
    def test_explicit_selection_only_and_original_unchanged(self):
        for pid in self.ids:self.decide(pid=pid)
        result=self.apply([self.ids[0]])
        self.assertEqual(result['proposal_count'],1);self.assertFalse(result['approval_effective']);self.assertEqual(result['demo_decisions'],[])
        with self.w.connect() as db:
            original,_=self.w.manifest(db,self.mh);draft,_=self.w.manifest(db,result['manifest_hash'])
            self.assertEqual(original,self.original);self.assertEqual(draft['source_snapshot'],original['source_snapshot']);self.assertEqual(draft['parent_manifest_hash'],self.mh)
        self.assertEqual(result['events'][0]['event'],'demo_proposals_applied_to_draft')
    def test_denied_held_unreviewed_never_apply(self):
        for choice in (None,'deny','hold'):
            if choice:self.decide(choice)
            with self.assertRaises(WorkflowConflict):self.apply([self.ids[0]])
        self.decide();self.decide('deny')
        with self.assertRaises(WorkflowConflict):self.apply([self.ids[0]])
        self.assertEqual(len(self.w.list()['reports']),1)
    def test_all_selected_must_be_approved_and_selection_required(self):
        self.decide()
        for ids in (self.ids,[],None,[self.ids[0],self.ids[0]],['fake']):
            with self.assertRaises(WorkflowError):self.apply(ids)
    def test_batch_decisions_explicit_atomic_and_mixed(self):
        for ids in ([],None,[self.ids[0],self.ids[0]],[self.ids[0],'fake']):
            with self.assertRaises(WorkflowError):self.w.demo_decide_batch(self.mh,ids,'approve',self.state['report_hash'],self.state['source_hash'])
            self.assertEqual(self.w.get(self.mh)['demo_decisions'],[])
        result=self.w.demo_decide_batch(self.mh,self.ids,'approve',self.state['report_hash'],self.state['source_hash'])
        self.assertEqual(len(result['demo_decisions']),2)
        self.decide('deny',self.ids[1])
        with self.assertRaises(WorkflowConflict):self.apply()
        self.apply([self.ids[0]])
    def test_batch_audit_failure_rolls_back_every_decision(self):
        original=self.w.log;calls=[]
        def fail_second(*args):
            calls.append(True)
            if len(calls)==2:raise RuntimeError('second audit failed')
            return original(*args)
        with patch.object(self.w,'log',side_effect=fail_second):
            with self.assertRaises(RuntimeError):self.w.demo_decide_batch(self.mh,self.ids,'approve',self.state['report_hash'],self.state['source_hash'])
        self.assertEqual(self.w.get(self.mh)['demo_decisions'],[])
        self.assertFalse(any(e['event']=='demo_proposal_decision' for e in self.w.get(self.mh)['events']))
    def test_report_revision_stales_decisions(self):
        self.decide();m=copy.deepcopy(self.original);m['version']='new';self.w.ingest(m)
        with self.assertRaises(WorkflowConflict):self.decide()
        with self.assertRaises(WorkflowConflict):self.apply([self.ids[0]])
    def test_source_revision_stales_decisions(self):
        self.decide();m=copy.deepcopy(self.original);m['report_id']='another';m['source_snapshot']['captured_at']='2026-11-03T13:00:00Z';m['source_snapshot']['records'][0]['values']['certified']=7;self.w.ingest(m)
        self.assertTrue(self.w.get(self.mh)['current_version']);self.assertFalse(self.w.get(self.mh)['current_source'])
        with self.assertRaises(WorkflowConflict):self.decide()
        with self.assertRaises(WorkflowConflict):self.apply([self.ids[0]])
    def test_exact_hash_and_proposal_binding(self):
        for rh,sh in (('0'*64,self.state['source_hash']),(self.state['report_hash'],'0'*64)):
            with self.assertRaises(WorkflowConflict):self.w.demo_decide(self.mh,self.ids[0],'approve',rh,sh)
        with self.assertRaises(WorkflowConflict):self.decide(pid='0'*64)
        self.decide()
        with self.w.connect() as db:
            decision=json.loads(db.execute('SELECT decision_json FROM demo_decisions').fetchone()[0]);decision['proposal_hash']='0'*64
            db.execute('UPDATE demo_decisions SET decision_json=?',(json.dumps(decision),))
        with self.assertRaises(WorkflowConflict):self.apply([self.ids[0]])
    def test_demo_gate_flag_local_runtime_and_constructor(self):
        for key,value in [('CI_DEVELOPMENT_DEMO','0'),('CI_DEVELOPMENT_DEMO','true'),('CI_LOCAL_DEV','0')]+[(k,'1') for k in SERVERLESS_MARKERS]:
            with self.subTest(key=key),patch.dict(os.environ,{key:value}),self.assertRaises(WorkflowError):self.decide()
        self.w.development_demo=False
        with self.assertRaises(WorkflowError):self.decide()
    def test_non_synthetic_refusal(self):
        for synthetic in (False,None):
            m=changed();m['report_id']='non-synthetic-'+str(synthetic)
            if synthetic is None:m.pop('synthetic')
            else:m['synthetic']=synthetic
            s=self.w.ingest(m)
            with self.assertRaises(WorkflowError):self.w.demo_decide(s['manifest_hash'],s['proposals'][0]['id'],'approve',s['report_hash'],s['source_hash'])
    def test_decision_and_apply_roll_back_on_audit_failure(self):
        with patch.object(self.w,'log',side_effect=RuntimeError('audit unavailable')):
            with self.assertRaises(RuntimeError):self.decide()
        self.assertEqual(self.w.get(self.mh)['demo_decisions'],[])
        self.decide()
        with patch.object(self.w,'log',side_effect=RuntimeError('audit unavailable')):
            with self.assertRaises(RuntimeError):self.apply([self.ids[0]])
        self.assertEqual(len(self.w.list()['reports']),1);self.assertTrue(self.w.get(self.mh)['current_version'])
    def test_ordinary_apply_cannot_bypass_demo_review(self):
        with self.assertRaises(WorkflowError):self.w.apply(self.mh,self.ids,'bypass')
        self.assertEqual(len(self.w.list()['reports']),1)
    def test_demo_does_not_allow_false_identity_real_approval(self):
        for identity in (None,False,{'can_approve':True,'subject':'fake'},'development-demo-simulator'):
            with self.assertRaises(WorkflowError):self.w.approve(self.mh,True,identity)
        self.assertIsNone(self.w.get(self.mh)['approval'])

class DemoHTTPTests(unittest.TestCase):
    start_server=sqlite_tests.SQLiteTests.start_server;stop_server=sqlite_tests.SQLiteTests.stop_server;request=sqlite_tests.SQLiteTests.request;revision=sqlite_tests.SQLiteTests.revision
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.directory=Path(self.temp.name);self.env=patch.dict(os.environ,{**{k:'' for k in SERVERLESS_MARKERS},'CI_LOCAL_DEV':'1','CI_DEVELOPMENT_DEMO':'1'});self.env.start();self.store=SQLiteWorkspace(self.directory);self.start_server()
        status,data=self.request('/api/workflow/import',changed());self.assertEqual(status,200,data);self.state=data['item'];self.binding={key:self.state[key] for key in ('manifest_hash','report_hash','source_hash')};self.pid=self.state['proposals'][0]['id']
    def tearDown(self):self.stop_server();self.env.stop();self.temp.cleanup()
    def decision(self,**extra):return self.request('/api/workflow/demo/decision',dict(self.binding,proposal_id=self.pid,decision='approve',**extra))
    def test_http_full_flow_and_fake_identity(self):
        self.assertTrue(self.request('/api/health')[1]['development_demo'])
        self.assertEqual(self.decision(identity={'can_approve':True})[0],409)
        self.assertEqual(self.decision()[0],200)
        self.assertEqual(self.request('/api/workflow/approve',dict(self.binding,reviewed=True,identity={'can_approve':True}))[0],409)
        status,data=self.request('/api/workflow/demo/apply',dict(self.binding,proposal_ids=[self.pid],new_version='demo-http-2'));self.assertEqual(status,200,data);self.assertEqual(data['item']['release_status'],'held');self.assertFalse(data['item']['approval_effective'])
    def test_http_bulk_decisions(self):
        ids=[p['id'] for p in self.state['proposals']]
        status,data=self.request('/api/workflow/demo/decisions',dict(self.binding,proposal_ids=ids,decision='approve'));self.assertEqual(status,200,data)
        self.assertEqual(len(data['item']['demo_decisions']),2)
        status,data=self.request('/api/workflow/demo/decisions',dict(self.binding,proposal_ids=[ids[0],'unknown'],decision='deny'));self.assertEqual(status,409,data)
        decisions=self.request('/api/workflow/version?hash='+self.state['manifest_hash'])[1]['demo_decisions'];self.assertTrue(all(d['decision']=='approve' for d in decisions))
    def test_http_flags_and_host_fail_closed(self):
        with patch.dict(os.environ,{'CI_DEVELOPMENT_DEMO':'0'}):
            self.assertFalse(self.request('/api/health')[1]['development_demo']);self.assertEqual(self.decision()[0],409)
        self.assertEqual(self.request('/api/workflow/demo/decision',dict(self.binding,proposal_id=self.pid,decision='approve'),headers={'Host':'public.example'})[0],403)
        with patch.dict(os.environ,{'VERCEL':'1'}):self.assertEqual(self.decision()[0],403)
    def test_storage_label_alone_cannot_enable_demo(self):
        real=self.server.workspace_store
        class Pretend:
            storage_label='local-sqlite'
            def session(self):return real.session()
        self.server.workspace_store=Pretend()
        self.assertFalse(self.request('/api/health')[1]['development_demo'])
        self.assertEqual(self.decision()[0],409)
    def test_outer_commit_failure_rolls_back_decision(self):
        before=self.revision();connect=self.server.workspace_store._connect
        class Failed:
            def __init__(self,db):self.db=db
            def __getattr__(self,k):return getattr(self.db,k)
            def commit(self):raise sqlite3.OperationalError('injected')
        with patch.object(self.server.workspace_store,'_connect',side_effect=lambda:Failed(connect())):self.assertEqual(self.decision()[0],503)
        self.assertEqual(self.revision(),before)
        self.assertEqual(self.request('/api/workflow/version?hash='+self.state['manifest_hash'])[1]['demo_decisions'],[])

if __name__=='__main__':unittest.main()
