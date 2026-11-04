import copy
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import datetime,timedelta,timezone

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from hosting.report_workflow import ReportWorkflow,ReviewerIdentity,WorkflowError,WorkflowConflict,evaluate,fingerprint


def fixture():
    fields=[dict(name=n,type='integer',required=True,total=True,nonnegative=True) for n in ('inspected','certified','rejected')]
    scope=dict(site_id='synthetic-site',list_id='synthetic-list',client_id='synthetic-client',job_id='synthetic-job',shift_id='1',report_date='2026-11-03')
    records=[dict(id='row-1',values=dict(inspected=10,certified=9,rejected=1))]
    return dict(schema_version=2,synthetic=True,report_id='daily-summary',version='1',scope=scope,fields=fields,
        report=dict(records=copy.deepcopy(records),totals=dict(inspected=10,certified=9,rejected=1)),
        source_snapshot=dict(source_reference='synthetic://fixture',captured_at='2026-11-03T12:00:00Z',complete=True,
            consistency='confirmed',shift_complete=True,timezone_status='confirmed',scope=copy.deepcopy(scope),
            records=copy.deepcopy(records),field_definitions_hash=fingerprint(fields)))

IDENTITY=ReviewerIdentity('test-subject','Synthetic reviewer','test-only-provider',True)

class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.workflow=ReportWorkflow(self.temp.name)
    def tearDown(self):self.temp.cleanup()
    def ingest(self,**kwargs):
        m=fixture();m.update(kwargs);return self.workflow.ingest(m)
    def changed(self):
        m=fixture();m['report']['records'][0]['values']['certified']=8;m['report']['totals']['certified']=8;return m
    def test_match_is_not_source_authentication_or_release(self):
        result=self.ingest();self.assertEqual(result['status'],'matched_supplied_snapshot')
        self.assertEqual(result['source_authenticity'],'not_authenticated');self.assertEqual(result['release_status'],'held')
        self.assertFalse(result['approval_effective']);self.assertFalse(result['send_enabled']);self.assertFalse(result['source_write_enabled'])
    def test_certified_is_not_assumed_accepted(self):
        m=fixture()
        for target in (m['report'],m['source_snapshot']):target['records'][0]['values']['certified']=4
        m['report']['totals']['certified']=4
        self.assertEqual(evaluate(m)['finding_count'],0)
    def test_missing_rejected_is_never_zero(self):
        m=fixture()
        for target in (m['report'],m['source_snapshot']):target['records'][0]['values']['rejected']=None
        m['report']['totals']['rejected']=None
        result=evaluate(m);self.assertIsNone(result['report_totals']['rejected']);self.assertIsNone(result['source_totals']['rejected'])
        self.assertEqual(result['finding_counts']['missing_value'],2);self.assertFalse(result['proposals'])
    def test_optional_missing_total_remains_unknown(self):
        m=fixture();m['fields'][2]['required']=False;m['source_snapshot']['field_definitions_hash']=fingerprint(m['fields'])
        del m['report']['records'][0]['values']['rejected'];del m['source_snapshot']['records'][0]['values']['rejected']
        result=evaluate(m);self.assertIsNone(result['report_totals']['rejected']);self.assertIn('total_unknown',result['finding_counts'])
    def test_absent_null_blank_zero_false_distinguished(self):
        for value in (None,'',' ',False,'0'):
            m=fixture();m['report']['records'][0]['values']['rejected']=value
            self.assertTrue(evaluate(m)['finding_count'])
        m=fixture();m['report']['records'][0]['values']['rejected']=0
        self.assertNotIn('missing_value',evaluate(m)['finding_counts'])
    def test_duplicate_record_rejected(self):
        for side in ('report','source_snapshot'):
            m=fixture();m[side]['records'].append(copy.deepcopy(m[side]['records'][0]))
            with self.assertRaises(WorkflowError):evaluate(m)
    def test_duplicate_fields_rejected(self):
        m=fixture();m['fields'].append(copy.deepcopy(m['fields'][0]))
        with self.assertRaises(WorkflowError):evaluate(m)
    def test_unknown_field_and_text_total_rejected(self):
        m=fixture();m['report']['records'][0]['values']['accepted']=10
        with self.assertRaises(WorkflowError):evaluate(m)
        m=fixture();m['fields'][0]['type']='text'
        with self.assertRaises(WorkflowError):evaluate(m)
    def test_duplicate_json_keys_and_nonfinite_rejected(self):
        raw=json.dumps(fixture())[:-1]+',"version":"2"}'
        with self.assertRaises(WorkflowError):self.workflow.ingest(raw)
        m=fixture();m['report']['totals']['inspected']=float('nan')
        with self.assertRaises(WorkflowError):self.workflow.ingest(m)
    def test_scope_mismatch_no_proposals(self):
        m=self.changed();m['source_snapshot']['scope']['shift_id']='2'
        result=evaluate(m);self.assertIn('source_scope',result['finding_counts']);self.assertFalse(result['proposals'])
    def test_missing_source_no_proposals(self):
        m=self.changed();m['source_snapshot']=None
        result=evaluate(m);self.assertIn('source_missing',result['finding_counts']);self.assertFalse(result['proposals'])
    def test_incomplete_source_no_proposals(self):
        m=self.changed();m['source_snapshot']['complete']=False
        result=evaluate(m);self.assertIn('source_incomplete',result['finding_counts']);self.assertFalse(result['proposals'])
    def test_invalid_source_no_proposals(self):
        m=self.changed();m['source_snapshot']['records'][0]['values']['rejected']=None
        self.assertFalse(evaluate(m)['proposals'])
    def test_bad_timestamp_no_proposals(self):
        for value in (None,'2026-11-03','invalid',(datetime.now(timezone.utc)+timedelta(days=1)).isoformat()):
            m=self.changed();m['source_snapshot']['captured_at']=value
            self.assertFalse(evaluate(m)['proposals'])
    def test_uncertain_shift_snapshot_timezone_blocks_approval(self):
        m=fixture();m['source_snapshot'].update(consistency='unknown',shift_complete=None,timezone_status='unknown')
        result=self.workflow.ingest(m)
        for code in ('source_consistency_unconfirmed','shift_completion_unconfirmed','source_timezone_unconfirmed'):self.assertIn(code,result['finding_counts'])
        with self.assertRaises(WorkflowConflict):self.workflow.approve(result['manifest_hash'],True,IDENTITY)
    def test_unresolved_client_is_retained_but_blocking(self):
        m=fixture();m['scope']['client_id']=None;m['source_snapshot']['scope']['client_id']=None
        m['report']['records'][0]['values']['certified']=8
        result=self.workflow.ingest(m)
        self.assertIn('client_identity_missing',result['finding_counts']);self.assertFalse(result['proposals'])
    def test_unknown_mapping_blocks_proposals(self):
        m=self.changed();del m['source_snapshot']['field_definitions_hash']
        result=evaluate(m);self.assertIn('source_field_mapping',result['finding_counts']);self.assertFalse(result['proposals'])
    def test_idempotent_import(self):
        a=self.ingest();b=self.ingest();self.assertEqual(a['manifest_hash'],b['manifest_hash']);self.assertTrue(b['idempotent'])
        self.assertEqual(len(self.workflow.list()['reports']),1)
    def test_same_version_changed_content_rejected(self):
        self.ingest()
        with self.assertRaises(WorkflowConflict):self.workflow.ingest(self.changed())
        self.assertEqual(len(self.workflow.list()['reports']),1)
    def test_report_ids_are_scope_bound(self):
        self.ingest();m=fixture();m['scope']['job_id']='another-job';m['source_snapshot']['scope']=copy.deepcopy(m['scope'])
        self.workflow.ingest(m);self.assertEqual(len(self.workflow.list()['reports']),2)
    def test_individual_correction_does_not_silently_change_total(self):
        original=self.workflow.ingest(self.changed());cell=next(p for p in original['proposals'] if p['operation']=='set_value')
        draft=self.workflow.apply(original['manifest_hash'],[cell['id']],'2')
        self.assertIn('total_mismatch',draft['finding_counts']);self.assertEqual(draft['report_totals']['certified'],9)
        self.assertFalse(draft['approval_effective']);self.assertEqual(self.workflow.get(original['manifest_hash'])['report_hash'],original['report_hash'])
    def test_bulk_correction_exact_ids_and_no_approval_transfer(self):
        original=self.workflow.ingest(self.changed());draft=self.workflow.apply(original['manifest_hash'],[p['id'] for p in original['proposals']],'2')
        self.assertEqual(draft['finding_count'],0);self.assertEqual(draft['source_hash'],original['source_hash']);self.assertIsNone(draft['approval'])
        self.assertEqual(draft['release_status'],'held')
    def test_invalid_bulk_selection_atomic(self):
        original=self.workflow.ingest(self.changed());good=original['proposals'][0]['id']
        for ids in ([good,'0'*64],[good,good],[],[{}]):
            with self.assertRaises(WorkflowError):self.workflow.apply(original['manifest_hash'],ids,'2')
        self.assertEqual(len(self.workflow.list()['reports']),1)
    def test_stale_proposal_rejected(self):
        old=self.workflow.ingest(self.changed());self.workflow.apply(old['manifest_hash'],[p['id'] for p in old['proposals']],'2')
        with self.assertRaises(WorkflowConflict):self.workflow.apply(old['manifest_hash'],[old['proposals'][0]['id']],'3')
    def test_human_identity_required_not_boolean_json(self):
        r=self.ingest()
        for identity in (None,True,{'authenticated':True},ReviewerIdentity('x','x','x',False)):
            with self.assertRaises(WorkflowError):self.workflow.approve(r['manifest_hash'],True,identity)
        with self.assertRaises(WorkflowError):self.workflow.approve(r['manifest_hash'],False,IDENTITY)
    def test_exact_private_draft_approval(self):
        r=self.ingest();approved=self.workflow.approve(r['manifest_hash'],True,IDENTITY)
        self.assertTrue(approved['approval_effective']);self.assertFalse(approved['approval']['production_release']);self.assertEqual(approved['release_status'],'held')
    def test_new_version_invalidates_approval_effective(self):
        r=self.ingest();self.workflow.approve(r['manifest_hash'],True,IDENTITY)
        self.ingest(version='2')
        self.assertFalse(self.workflow.get(r['manifest_hash'])['approval_effective'])
        self.assertIsNotNone(self.workflow.get(r['manifest_hash'])['approval'])
    def test_new_source_invalidates_other_report_approval(self):
        r=self.ingest();self.workflow.approve(r['manifest_hash'],True,IDENTITY)
        m=fixture();m['report_id']='another-report';m['source_snapshot']['captured_at']='2026-11-03T12:01:00Z';m['source_snapshot']['records'][0]['values']['rejected']=2
        self.workflow.ingest(m)
        self.assertFalse(self.workflow.get(r['manifest_hash'])['current_source']);self.assertFalse(self.workflow.get(r['manifest_hash'])['approval_effective'])
    def test_equal_timestamp_conflicting_source_is_atomic(self):
        r=self.ingest();m=fixture();m['version']='2';m['source_snapshot']['records'][0]['values']['rejected']=2
        with self.assertRaises(WorkflowConflict):self.workflow.ingest(m)
        self.assertTrue(self.workflow.get(r['manifest_hash'])['current_version']);self.assertEqual(len(self.workflow.list()['reports']),1)
    def test_old_snapshot_does_not_reset_source_head(self):
        r=self.ingest();m=fixture();m['version']='2';m['source_snapshot']['captured_at']='2026-11-03T11:59:00Z'
        older=self.workflow.ingest(m);self.assertFalse(older['current_source'])
        with self.assertRaises(WorkflowConflict):self.workflow.approve(older['manifest_hash'],True,IDENTITY)
    def test_recipient_mapping_change_new_hash_not_copied_approval(self):
        first=self.ingest(recipient_mapping_version='mapping-1');self.workflow.approve(first['manifest_hash'],True,IDENTITY)
        second=self.ingest(version='2',recipient_mapping_version='mapping-2')
        self.assertNotEqual(first['manifest_hash'],second['manifest_hash']);self.assertIsNone(second['approval'])
    def test_tampered_stored_manifest_detected(self):
        r=self.ingest()
        with sqlite3.connect(self.workflow.db_path) as db:db.execute("UPDATE versions SET manifest_json=?",(json.dumps(self.changed()),))
        with self.assertRaises(WorkflowConflict):self.workflow.get(r['manifest_hash'])
    def test_missing_extra_record_proposals_are_explicit(self):
        m=fixture();m['report']['records'][0]['id']='extra'
        r=self.workflow.ingest(m);self.assertEqual({p['operation'] for p in r['proposals']},{'add_record','remove_record'})
        d=self.workflow.apply(r['manifest_hash'],[p['id'] for p in r['proposals']],'2');self.assertEqual(d['finding_count'],0)
    def test_presence_map_must_match(self):
        m=fixture();m['report']['records'][0]['field_presence']={'rejected':'absent'}
        with self.assertRaises(WorkflowError):evaluate(m)
    def test_decimal_sum_exact(self):
        m=fixture();m['fields']=[dict(name='quantity',type='number',required=True,total=True,nonnegative=True)]
        values=[dict(id='1',values={'quantity':.1}),dict(id='2',values={'quantity':.2})]
        m['report']={'records':copy.deepcopy(values),'totals':{'quantity':.3}};m['source_snapshot']['records']=values;m['source_snapshot']['field_definitions_hash']=fingerprint(m['fields'])
        self.assertEqual(evaluate(m)['finding_count'],0)
    def test_empty_report_not_clean(self):
        m=fixture();m['report']['records']=[];m['source_snapshot']['records']=[];m['report']['totals']=dict(inspected=0,certified=0,rejected=0)
        self.assertIn('no_data',evaluate(m)['finding_counts'])
    def test_concurrent_same_version_only_one_row(self):
        results=[]
        def ingest():results.append(self.workflow.ingest(fixture())['manifest_hash'])
        workers=[threading.Thread(target=ingest) for _ in range(4)]
        for w in workers:w.start()
        for w in workers:w.join()
        self.assertEqual(len(results),4);self.assertEqual(len(set(results)),1);self.assertEqual(len(self.workflow.list()['reports']),1)
    def test_pagination(self):
        for i in range(4):self.ingest(version=str(i))
        first=self.workflow.list(2);second=self.workflow.list(2,first['next_cursor'])
        self.assertEqual(len(first['reports']),2);self.assertEqual(len(second['reports']),2);self.assertIsNone(second['next_cursor'])
        self.assertFalse({r['manifest_hash'] for r in first['reports']}&{r['manifest_hash'] for r in second['reports']})
    def test_findings_and_proposals_bounded(self):
        m=fixture();source=[];report=[]
        for i in range(600):
            source.append(dict(id=str(i),values=dict(inspected=10,certified=9,rejected=1)))
            report.append(dict(id=str(i),values=dict(inspected=10,certified=8,rejected=1)))
        m['report']={'records':report,'totals':dict(inspected=6000,certified=4800,rejected=600)};m['source_snapshot']['records']=source
        r=evaluate(m);self.assertLessEqual(len(r['findings']),200);self.assertLessEqual(len(r['proposals']),500)
        self.assertTrue(r['findings_truncated']);self.assertTrue(r['proposals_truncated'])

class AuditRegressionTests(WorkflowTests):
    def test_large_fractional_total_is_not_rounded_clean(self):
        m=fixture();m['fields']=[dict(name='quantity',type='number',required=True,total=True,nonnegative=True)]
        rows=[dict(id='1',values={'quantity':999999999999999}),dict(id='2',values={'quantity':.000001})]
        m['report']={'records':copy.deepcopy(rows),'totals':{'quantity':999999999999999}}
        m['source_snapshot']['records']=rows;m['source_snapshot']['field_definitions_hash']=fingerprint(m['fields'])
        result=self.workflow.ingest(m);self.assertIn('aggregate_precision',result['finding_counts'])
        with self.assertRaises(WorkflowConflict):self.workflow.approve(result['manifest_hash'],True,IDENTITY)
    def test_huge_integer_blocks_without_overflow(self):
        m=fixture();m['report']['records'][0]['values']['inspected']=10**400
        self.assertIn('invalid_value',evaluate(m)['finding_counts'])
    def test_boolean_and_float_wrong_type_receive_proposals(self):
        for value in (True,1.0):
            m=fixture();m['report']['records'][0]['values']['rejected']=value;m['report']['totals']['rejected']=value
            proposals=evaluate(m)['proposals'];self.assertEqual({p['operation'] for p in proposals},{'set_value','set_total'})
    def test_uncertain_source_disables_proposals(self):
        for key,value in (('consistency','unknown'),('timezone_status','unknown'),('shift_complete',None)):
            m=self.changed();m['source_snapshot'][key]=value;self.assertFalse(evaluate(m)['proposals'])

for name in list(WorkflowTests.__dict__):
    if name.startswith('test_') and name not in AuditRegressionTests.__dict__:setattr(AuditRegressionTests,name,None)

class BatchWorkflowTests(WorkflowTests):
    def test_explicit_batch_imports_each_scope(self):
        first=fixture();second=fixture();second['scope']['shift_id']='2';second['source_snapshot']['scope']=copy.deepcopy(second['scope'])
        result=self.workflow.ingest_batch([first,second]);self.assertEqual(len(result['items']),2)
        self.assertEqual(len(self.workflow.list()['reports']),2)
    def test_batch_conflict_rolls_back_all(self):
        first=fixture();second=self.changed()
        with self.assertRaises(WorkflowConflict):self.workflow.ingest_batch([first,second])
        self.assertEqual(self.workflow.list()['reports'],[])

for name in list(WorkflowTests.__dict__):
    if name.startswith('test_') and name not in BatchWorkflowTests.__dict__:setattr(BatchWorkflowTests,name,None)
