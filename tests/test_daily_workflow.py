"""Daily job isolation, explicit corrections, and physical-source freshness."""
import copy
import sqlite3
import tempfile
import unittest

from test_report_workflow import fixture, IDENTITY
from test_source_adapter import envelope
from hosting.report_workflow import ReportWorkflow, WorkflowError, WorkflowConflict, evaluate, fingerprint
from hosting.source_adapter import convert_private_envelope


def daily_fixture():
    m=fixture();physical=copy.deepcopy(m['scope'])
    del m['scope']['shift_id'];m['scope']['report_granularity']='daily'
    records=[];members=[];breakdown={}
    for number in (1,2):
        shift=str(number);row=copy.deepcopy(m['report']['records'][0]);row.update(id='row-'+shift,shift_id=shift);records.append(row)
        member_scope=dict(physical,shift_id=shift)
        members.append(dict(scope=member_scope,source_revision=('a' if number==1 else 'b')*64,
            captured_at='2026-11-03T12:00:00Z',complete=True,consistency='confirmed',shift_complete=True,
            timezone_status='confirmed',record_ids=[row['id']]))
        breakdown[shift]=dict(record_count=1,totals=copy.deepcopy(m['report']['totals']))
    m['report'].update(records=copy.deepcopy(records),totals={name:value*2 for name,value in m['report']['totals'].items()},shift_breakdown=breakdown)
    m['source_snapshot'].update(scope=copy.deepcopy(m['scope']),records=records,member_shifts=members,
        captured_at='2026-11-03T12:02:00Z',shift_coverage_complete=True,source_revision='c'*64)
    return m


def physical_fixture(daily,shift='1'):
    m=fixture();member=next(item for item in daily['source_snapshot']['member_shifts'] if item['scope']['shift_id']==shift)
    m['scope']=copy.deepcopy(member['scope']);m['fields']=copy.deepcopy(daily['fields'])
    records=[copy.deepcopy(row) for row in daily['source_snapshot']['records'] if row['shift_id']==shift]
    for row in records:del row['shift_id']
    m['report']=dict(records=copy.deepcopy(records),totals=copy.deepcopy(daily['report']['shift_breakdown'][shift]['totals']))
    m['source_snapshot'].update({key:copy.deepcopy(member[key]) for key in ('scope','source_revision','captured_at','complete','consistency','shift_complete','timezone_status')})
    m['source_snapshot']['records']=records;m['source_snapshot']['field_definitions_hash']=fingerprint(m['fields'])
    return m


def rehash(e):
    e.pop('manifest_sha256',None);e['manifest_sha256']=fingerprint(e);return e


def daily_envelope():
    m=daily_fixture()
    return rehash(dict(schema_version='private-sharepoint-envelope/1',scope=dict(site_id=m['scope']['site_id'],
        list_id=m['scope']['list_id'],client_id=m['scope']['client_id'],job_id=m['scope']['job_id'],
        business_date=m['scope']['report_date'],report_granularity='daily'),source_revision='c'*64,
        source_snapshot=m['source_snapshot'],field_definitions=m['fields'],report=m['report'],
        report_id=m['report_id'],version=m['version'],generated_from_snapshot_at='2026-11-03T12:02:00Z'))


class DailyWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.workflow=ReportWorkflow(self.temp.name)
    def tearDown(self):self.temp.cleanup()
    def approved(self):
        m=daily_fixture();result=self.workflow.ingest(m);self.workflow.approve(result['manifest_hash'],True,IDENTITY)
        return m,result['manifest_hash']

    def test_daily_match_retains_two_shifts_and_held_release(self):
        result=self.workflow.ingest(daily_fixture())
        self.assertEqual(result['finding_count'],0);self.assertEqual(set(result['report_shift_breakdown']),{'1','2'})
        self.assertEqual(result['report_totals']['inspected'],20);self.assertTrue(result['current_member_sources'])
        self.assertEqual(result['release_status'],'held');self.assertFalse(result['send_enabled']);self.assertFalse(result['source_write_enabled'])

    def test_every_member_scope_component_must_match(self):
        for key in ('site_id','list_id','client_id','job_id','report_date'):
            m=daily_fixture();m['source_snapshot']['member_shifts'][0]['scope'][key]='2026-11-02' if key=='report_date' else 'different'
            with self.subTest(key=key),self.assertRaises(WorkflowError):evaluate(m)

    def test_daily_scope_cannot_include_physical_shift(self):
        m=daily_fixture();m['scope']['shift_id']='all'
        with self.assertRaises(WorkflowError):evaluate(m)

    def test_daily_rows_require_shift_ids(self):
        for side in ('report','source_snapshot'):
            m=daily_fixture();del m[side]['records'][0]['shift_id']
            with self.subTest(side=side),self.assertRaises(WorkflowError):evaluate(m)

    def test_source_less_rows_must_exist_in_breakdown(self):
        m=daily_fixture();m['source_snapshot']=None;m['report']['records'][0]['shift_id']='outside'
        with self.assertRaises(WorkflowError):evaluate(m)

    def test_source_less_valid_daily_is_retained_blocked(self):
        m=daily_fixture();m['source_snapshot']=None;r=evaluate(m)
        self.assertIn('source_missing',r['finding_counts']);self.assertFalse(r['proposals'])

    def test_member_partition_is_exact_unique_and_shift_bound(self):
        for mutation in ('missing','extra','duplicate','wrong_shift','duplicate_member'):
            m=daily_fixture();members=m['source_snapshot']['member_shifts']
            if mutation=='missing':members[0]['record_ids']=[]
            elif mutation=='extra':members[0]['record_ids'].append('not-a-row')
            elif mutation=='duplicate':members[1]['record_ids'].append('row-1')
            elif mutation=='wrong_shift':m['source_snapshot']['records'][0]['shift_id']='2'
            else:members.append(copy.deepcopy(members[0]))
            with self.subTest(mutation=mutation),self.assertRaises(WorkflowError):evaluate(m)

    def test_breakdown_and_member_sets_must_match(self):
        m=daily_fixture();del m['report']['shift_breakdown']['2'];m['report']['records']=m['report']['records'][:1]
        with self.assertRaises(WorkflowError):evaluate(m)

    def test_member_unknown_evidence_disables_all_corrections(self):
        for key,value in (('complete',False),('consistency','unknown'),('shift_complete',None),('shift_complete',False),('timezone_status','unknown')):
            m=daily_fixture();m['source_snapshot']['member_shifts'][0][key]=value;m['report']['records'][0]['values']['certified']=8
            with self.subTest(key=key,value=value):
                r=evaluate(m);self.assertIn('daily_member_unconfirmed',r['finding_counts']);self.assertFalse(r['proposals'])

    def test_unconfirmed_shift_coverage_blocks(self):
        m=daily_fixture();m['source_snapshot']['shift_coverage_complete']=False
        r=self.workflow.ingest(m);self.assertIn('daily_shift_coverage_unconfirmed',r['finding_counts'])
        with self.assertRaises(WorkflowConflict):self.workflow.approve(r['manifest_hash'],True,IDENTITY)

    def test_member_capture_after_daily_capture_blocks(self):
        m=daily_fixture();m['source_snapshot']['member_shifts'][0]['captured_at']='2026-11-03T12:03:00Z'
        r=evaluate(m);self.assertIn('daily_member_timestamp',r['finding_counts']);self.assertFalse(r['proposals'])

    def test_empty_member_is_unknown_not_zero_and_cannot_approve(self):
        m=daily_fixture();m['source_snapshot']['records']=m['source_snapshot']['records'][:1];m['source_snapshot']['member_shifts'][1]['record_ids']=[]
        m['report']['records']=m['report']['records'][:1];m['report']['totals']=dict(inspected=10,certified=9,rejected=1)
        m['report']['shift_breakdown']['2']=dict(record_count=0,totals=dict(inspected=0,certified=0,rejected=0))
        r=self.workflow.ingest(m);self.assertIn('daily_member_no_data',r['finding_counts'])
        self.assertIsNone(r['source_shift_breakdown']['2']['totals']['inspected']);self.assertFalse(r['proposals'])
        with self.assertRaises(WorkflowConflict):self.workflow.approve(r['manifest_hash'],True,IDENTITY)

    def test_unknown_quantity_propagates_to_day_and_shift(self):
        m=daily_fixture()
        for side in ('report','source_snapshot'):m[side]['records'][0]['values']['rejected']=None
        r=evaluate(m)
        for key in ('report_totals','source_totals'):self.assertIsNone(r[key]['rejected'])
        for key in ('report_shift_breakdown','source_shift_breakdown'):self.assertIsNone(r[key]['1']['totals']['rejected'])
        self.assertFalse(r['proposals'])

    def test_report_row_moved_between_known_shifts_is_not_quantity_fix(self):
        m=daily_fixture();m['report']['records'][0]['shift_id']='2'
        r=evaluate(m);self.assertIn('daily_record_shift',r['finding_counts']);self.assertFalse(r['proposals'])

    def test_bulk_correction_updates_explicit_day_and_shift_totals(self):
        m=daily_fixture();m['report']['records'][0]['values']['certified']=8;m['report']['totals']['certified']=17;m['report']['shift_breakdown']['1']['totals']['certified']=8
        before=copy.deepcopy(m);r=self.workflow.ingest(m)
        self.assertEqual({p['operation'] for p in r['proposals']},{'set_value','set_total','set_shift_total'})
        for proposal in r['proposals']:self.assertNotEqual(proposal['before'],proposal['after'])
        fixed=self.workflow.apply(r['manifest_hash'],[p['id'] for p in r['proposals']],'2')
        self.assertEqual(fixed['finding_count'],0);self.assertIsNone(fixed['approval']);self.assertEqual(m,before)
        self.assertEqual(fixed['source_hash'],r['source_hash']);self.assertIn('proposals_applied_to_draft',{event['event'] for event in fixed['events']})

    def test_partial_quantity_correction_does_not_rewrite_summary(self):
        m=daily_fixture();m['report']['records'][0]['values']['certified']=8;m['report']['totals']['certified']=17;m['report']['shift_breakdown']['1']['totals']['certified']=8
        r=self.workflow.ingest(m);proposal=next(p for p in r['proposals'] if p['operation']=='set_value')
        fixed=self.workflow.apply(r['manifest_hash'],[proposal['id']],'2')
        self.assertIn('total_mismatch',fixed['finding_counts']);self.assertIn('shift_total_mismatch',fixed['finding_counts'])

    def test_missing_record_correction_includes_explicit_shift_count(self):
        m=daily_fixture();m['report']['records']=m['report']['records'][:1];m['report']['totals']=dict(inspected=10,certified=9,rejected=1)
        m['report']['shift_breakdown']['2']=dict(record_count=0,totals=dict(inspected=0,certified=0,rejected=0))
        r=self.workflow.ingest(m);self.assertIn('set_shift_record_count',{p['operation'] for p in r['proposals']})
        fixed=self.workflow.apply(r['manifest_hash'],[p['id'] for p in r['proposals']],'2');self.assertEqual(fixed['finding_count'],0)

    def test_one_daily_head_independent_of_report_id(self):
        m,old=self.approved();m['report_id']='alternate-name';m['version']='2';new=self.workflow.ingest(m)
        self.assertTrue(new['current_version']);self.assertFalse(self.workflow.get(old)['current_version']);self.assertFalse(self.workflow.get(old)['approval_effective'])

    def test_same_daily_version_with_different_report_id_conflicts(self):
        m=daily_fixture();self.workflow.ingest(m);m['report_id']='alternate'
        with self.assertRaises(WorkflowConflict):self.workflow.ingest(m)

    def test_distinct_jobs_have_independent_daily_heads(self):
        m,old=self.approved();m['scope']['job_id']='another-job';m['source_snapshot']['scope']['job_id']='another-job'
        for member in m['source_snapshot']['member_shifts']:member['scope']['job_id']='another-job'
        self.workflow.ingest(m);self.assertTrue(self.workflow.get(old)['approval_effective'])

    def test_newer_physical_capture_same_revision_invalidates_daily(self):
        m,old=self.approved();physical=physical_fixture(m);physical['source_snapshot']['captured_at']='2026-11-03T12:01:00Z'
        self.workflow.ingest(physical);r=self.workflow.get(old)
        self.assertFalse(r['current_member_sources']);self.assertFalse(r['approval_effective'])

    def test_same_capture_same_revision_changed_quantity_conflicts_atomically(self):
        m,old=self.approved();physical=physical_fixture(m);physical['source_snapshot']['records'][0]['values']['rejected']=99
        with self.assertRaises(WorkflowConflict):self.workflow.ingest(physical)
        self.assertEqual(len(self.workflow.list()['reports']),1);self.assertTrue(self.workflow.get(old)['approval_effective'])

    def test_same_capture_same_revision_changed_evidence_conflicts(self):
        m,old=self.approved()
        for key,value in (('shift_complete',False),('complete',False),('consistency','unknown'),('timezone_status','unknown'),('evidence_blockers',['new blocker'])):
            physical=physical_fixture(m);physical['source_snapshot'][key]=value
            with self.subTest(key=key),self.assertRaises(WorkflowConflict):self.workflow.ingest(physical)

    def test_physical_without_revision_still_invalidates_daily(self):
        m,old=self.approved();physical=physical_fixture(m);del physical['source_snapshot']['source_revision'];physical['source_snapshot']['captured_at']='2026-11-03T12:01:00Z'
        self.workflow.ingest(physical);self.assertFalse(self.workflow.get(old)['approval_effective'])

    def test_equivalent_physical_rows_and_timezone_do_not_conflict(self):
        m,old=self.approved();physical=physical_fixture(m)
        physical['source_snapshot']['captured_at']='2026-11-03T07:00:00-05:00'
        physical['source_snapshot']['records'][0]['field_presence']={'rejected':'present'}
        self.workflow.ingest(physical);self.assertTrue(self.workflow.get(old)['approval_effective'])

    def test_known_shift_omission_is_blocking(self):
        m=daily_fixture();self.workflow.ingest(physical_fixture(m,'2'))
        m['source_snapshot']['member_shifts']=m['source_snapshot']['member_shifts'][:1];m['source_snapshot']['records']=m['source_snapshot']['records'][:1]
        m['report']['records']=m['report']['records'][:1];m['report']['totals']=dict(inspected=10,certified=9,rejected=1);del m['report']['shift_breakdown']['2']
        r=self.workflow.ingest(m);self.assertIn('daily_known_shift_missing',r['finding_counts']);self.assertFalse(r['current_source'])
        with self.assertRaises(WorkflowConflict):self.workflow.approve(r['manifest_hash'],True,IDENTITY)

    def test_newly_known_shift_invalidates_prior_daily_approval(self):
        m,old=self.approved();physical=physical_fixture(m);physical['scope']['shift_id']='3';physical['source_snapshot']['scope']['shift_id']='3'
        self.workflow.ingest(physical);r=self.workflow.get(old)
        self.assertIn('daily_known_shift_missing',r['finding_counts']);self.assertFalse(r['approval_effective'])

    def test_older_physical_capture_cannot_reset_member_head(self):
        m,old=self.approved();physical=physical_fixture(m);physical['source_snapshot']['captured_at']='2026-11-03T11:59:00Z'
        r=self.workflow.ingest(physical);self.assertFalse(r['current_source']);self.assertTrue(self.workflow.get(old)['approval_effective'])

    def test_upgrade_backfills_existing_physical_sources(self):
        m=daily_fixture();self.workflow.ingest(physical_fixture(m,'2'))
        with sqlite3.connect(self.workflow.db_path) as db:
            db.execute('DELETE FROM member_evidence_heads');db.execute('DELETE FROM workflow_migrations')
        self.workflow=ReportWorkflow(self.temp.name)
        with sqlite3.connect(self.workflow.db_path) as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM member_evidence_heads').fetchone()[0],1)
        self.assertTrue(self.workflow.ingest(physical_fixture(m,'2'))['idempotent'])


class DailyAdapterTests(unittest.TestCase):
    def test_daily_conversion_preserves_exact_members(self):
        e=daily_envelope();m=convert_private_envelope(e)
        self.assertEqual(evaluate(m)['finding_count'],0);self.assertEqual(m['source_snapshot']['member_shifts'],e['source_snapshot']['member_shifts'])

    def test_outer_job_and_client_conflicts_are_rejected(self):
        for make in (envelope,daily_envelope):
            for container in ('scope','top'):
                for key in ('job_id','client_id'):
                    e=make();(e['scope'] if container=='scope' else e)[key]='different'
                    with self.subTest(make=make.__name__,container=container,key=key),self.assertRaises(WorkflowError):convert_private_envelope(rehash(e))

    def test_matching_outer_identities_are_supported(self):
        e=envelope();e['scope'].update({key:e['source_snapshot']['scope'][key] for key in ('job_id','client_id')})
        self.assertEqual(evaluate(convert_private_envelope(rehash(e)))['finding_count'],0)

    def test_fake_daily_physical_alias_rejected(self):
        for key in ('shift_key','shift_id'):
            e=daily_envelope();e['scope'][key]='all'
            with self.subTest(key=key),self.assertRaises(WorkflowError):convert_private_envelope(rehash(e))

    def test_conflicting_date_alias_rejected(self):
        e=daily_envelope();e['scope']['report_date']='2026-11-02'
        with self.assertRaises(WorkflowError):convert_private_envelope(rehash(e))


if __name__=='__main__':unittest.main()
