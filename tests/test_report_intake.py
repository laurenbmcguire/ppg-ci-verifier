import copy
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from report_intake import ReportInbox, IntakeError, IntakeConflict


def sample():
    period = {'start': '2026-10-12', 'end': '2026-10-18'}
    records = [{'id': 'SYN-A', 'inspected': 10, 'accepted': 8, 'rejected': 2},
               {'id': 'SYN-B', 'inspected': 5, 'accepted': 4, 'rejected': 1}]
    return dict(schema_version=1, synthetic=True, report_id='SYN-REPORT-001', version='1',
        job_id='SYN-JOB-001', period=period, source_reference='synthetic:fixture-001',
        report=dict(records=copy.deepcopy(records), totals=dict(inspected=15, accepted=12, rejected=3)),
        source_snapshot=dict(source_reference='synthetic:fixture-001', job_id='SYN-JOB-001',
            period=copy.deepcopy(period), complete=True, fetched_at='2026-10-19T09:00:00Z', records=records))

class InboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.inbox = ReportInbox(self.tmp.name)
    def codes(self, result):
        return {f['code'] for f in result['findings']}
    def test_matching_snapshot_not_approval(self):
        r = self.inbox.ingest(sample())
        self.assertEqual(r['status'], 'matched_supplied_snapshot')
        self.assertEqual(r['release_status'], 'held')
        self.assertEqual(r['source_authenticity'], 'not_authenticated')
        self.assertEqual(r['calculated_report_totals'], {'inspected':15,'accepted':12,'rejected':3})
        self.assertEqual(r['findings'], [])
    def test_duplicate_idempotent_outbox_dedup(self):
        one = self.inbox.ingest(sample())
        two = self.inbox.ingest(json.dumps(sample(), indent=2))
        self.assertFalse(one['idempotent'])
        self.assertTrue(two['idempotent'])
        self.assertEqual(one['created_at'], two['created_at'])
        self.assertEqual(len(self.inbox.list_reports()), 1)
        self.assertEqual(len(self.inbox.list_outbox()), 1)
        self.assertEqual(self.inbox.list_outbox()[0]['status'], 'preview_only')
        self.assertFalse(self.inbox.list_outbox()[0]['send_enabled'])
    def test_conflict_preserves_original(self):
        first = self.inbox.ingest(sample())
        altered = sample(); altered['report']['totals']['accepted'] = 11
        with self.assertRaises(IntakeConflict): self.inbox.ingest(altered)
        self.assertEqual(self.inbox.get('SYN-REPORT-001','1')['report_hash'], first['report_hash'])
        self.assertEqual(len(self.inbox.list_outbox()), 1)
    def test_new_version_separate(self):
        self.inbox.ingest(sample())
        m = sample(); m['version'] = '2'; m['report']['totals']['accepted'] = 11
        self.assertEqual(self.inbox.ingest(m)['status'], 'mismatch')
        self.assertEqual(len(self.inbox.list_reports()), 2)
        self.assertEqual(len(self.inbox.list_outbox()), 2)
    def test_reconcile_fields_and_balance(self):
        m=sample(); m['report']['records'][0]['accepted']=7
        r=self.inbox.ingest(m)
        self.assertEqual(r['status'],'mismatch')
        self.assertTrue({'record_mismatch','record_balance','total_mismatch'} <= self.codes(r))
    def test_missing_extra_ids(self):
        m=sample(); m['report']['records'][0]['id']='SYN-EXTRA'
        self.assertTrue({'missing_id','extra_id'} <= self.codes(self.inbox.ingest(m)))
    def test_duplicate_report_ids(self):
        m=sample(); m['report']['records'].append(copy.deepcopy(m['report']['records'][0]))
        r=self.inbox.ingest(m)
        self.assertEqual(r['status'],'mismatch'); self.assertIn('duplicate_id',self.codes(r))
    def test_duplicate_source_ids_unverifiable(self):
        m=sample(); m['source_snapshot']['records'].append(copy.deepcopy(m['source_snapshot']['records'][0]))
        self.assertEqual(self.inbox.ingest(m)['status'],'unable_to_verify')
    def test_missing_source(self):
        m=sample(); del m['source_snapshot']
        r=self.inbox.ingest(m)
        self.assertEqual(r['status'],'unable_to_verify'); self.assertIsNone(r['source_hash'])
    def test_partial_source(self):
        m=sample(); m['source_snapshot']['complete']=False
        self.assertEqual(self.inbox.ingest(m)['status'],'unable_to_verify')
    def test_source_scope_and_time(self):
        for field,value in [('job_id','other'),('period',{'start':'2026-10-01','end':'2026-10-02'}),
                            ('source_reference','other'),('fetched_at','2026-10-19'),('complete',1)]:
            with self.subTest(field=field):
                m=sample(); m['version']=field; m['source_snapshot'][field]=value
                self.assertEqual(self.inbox.ingest(m)['status'],'unable_to_verify')
    def test_invalid_counts(self):
        for index,value in enumerate([True,False,-1,1.5,'10',None,10**16]):
            with self.subTest(value=value):
                m=sample();m['version']=str(index);m['report']['records'][0]['inspected']=value
                r=self.inbox.ingest(m)
                self.assertIn('invalid_numeric', self.codes(r));self.assertNotEqual(r['status'],'matched_supplied_snapshot')
                self.assertIsNone(r['calculated_report_totals'])
    def test_invalid_source_count(self):
        m=sample();m['source_snapshot']['records'][0]['accepted']=True
        self.assertEqual(self.inbox.ingest(m)['status'],'unable_to_verify')
    def test_nonfinite_rejected(self):
        for value in [float('nan'),float('inf'),float('-inf')]:
            m=sample();m['report']['records'][0]['accepted']=value
            with self.assertRaises(IntakeError):self.inbox.ingest(m)
            with self.assertRaises(IntakeError):self.inbox.ingest(json.dumps(m))
        self.assertEqual(self.inbox.list_reports(),[])
    def test_malformed(self):
        for data in ['{', '[]', '{"schema_version":1,"schema_version":1}', b'\xff']:
            with self.assertRaises(IntakeError):self.inbox.ingest(data)
        self.assertEqual(self.inbox.list_reports(),[])
    def test_strict_identifiers_and_demo(self):
        for field,value in [('version',1),('version',True),('report_id',''),('job_id',None),('schema_version',True),('synthetic',False)]:
            m=sample();m[field]=value
            with self.assertRaises(IntakeError):self.inbox.ingest(m)
    def test_period_validation(self):
        for period in [{'start':'bad','end':'2026-10-12'}, {'start':'2026-10-19','end':'2026-10-12'}]:
            m=sample();m['period']=period
            with self.assertRaises(IntakeError):self.inbox.ingest(m)
    def test_record_scope(self):
        m=sample();m['report']['records'][0]['job_id']='wrong'
        self.assertIn('record_scope',self.codes(self.inbox.ingest(m)))
    def test_restart_durability_and_hashes(self):
        r=self.inbox.ingest(sample())
        restarted=ReportInbox(self.tmp.name)
        self.assertEqual(restarted.get(r['report_id'],r['version'])['manifest_fingerprint'],r['manifest_fingerprint'])
        self.assertEqual(len(restarted.list_outbox()),1)
        self.assertTrue(restarted.ingest(sample())['idempotent'])
        m=sample();m['version']='2';m['report']['totals']['inspected']=16
        r2=restarted.ingest(m)
        self.assertEqual(r['source_hash'],r2['source_hash'])
        self.assertNotEqual(r['report_hash'],r2['report_hash'])
    def test_empty_report_and_source_unverifiable(self):
        m=sample();m['report']['records']=[];m['source_snapshot']['records']=[]
        m['report']['totals']=dict(inspected=0,accepted=0,rejected=0)
        r=self.inbox.ingest(m)
        self.assertEqual(r['status'],'unable_to_verify')
        self.assertIn('no_data',self.codes(r))
    def test_future_source_timestamp_unverifiable(self):
        m=sample()
        m['source_snapshot']['fetched_at']=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()
        r=self.inbox.ingest(m)
        self.assertEqual(r['status'],'unable_to_verify')
        self.assertIn('source_future_timestamp',self.codes(r))
    def test_small_clock_skew_allowed(self):
        m=sample()
        m['source_snapshot']['fetched_at']=(datetime.now(timezone.utc)+timedelta(minutes=2)).isoformat()
        self.assertEqual(self.inbox.ingest(m)['status'],'matched_supplied_snapshot')
    def test_full_synchronous_durability(self):
        with self.inbox._connect() as db:
            self.assertEqual(db.execute('PRAGMA synchronous').fetchone()[0],2)
    def test_unknown_report(self):
        with self.assertRaises(IntakeError):self.inbox.get('missing','1')

if __name__=='__main__':unittest.main()
