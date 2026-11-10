"""Source-first outer PostgresWorkspace gates. Disposable local test DB only.

Run serially with CI_PG_TEST_URL set by scripts/pg-test-db.sh, never production.
These tests intentionally reset the marked disposable database before each case.
"""
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import json, os, sys, unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parent))
from source_first_fixture import capture, MAP, SCOPE
from test_source_first_daily import FakeHandler
import test_source_first_daily as local
from hosting import source_first as sf, source_first_service as svc
from hosting.postgres_storage import PostgresWorkspace
from hosting.spreadsheet_inbox import SpreadsheetInbox
from hosting.files import files_for
from hosting.storage import StorageUnavailable

URL=os.environ.get('CI_PG_TEST_URL')

@unittest.skipUnless(URL,'set CI_PG_TEST_URL to a marked disposable local database')
class SourceFirstNativePG(unittest.TestCase):
    rid=local.Durable.rid
    new=local.Durable.new
    action=local.Durable.action
    approved=local.Durable.approved
    test_pending_checkpoint_commits_before_any_provider_read=local.PrivateArtifact.test_pending_checkpoint_commits_before_any_provider_read
    def setUp(self):
        import psycopg
        from pg_safety import assert_disposable
        assert_disposable(URL)
        with psycopg.connect(URL.replace('ci_verifier:test@','postgres@'),autocommit=True) as c:
            c.execute('TRUNCATE ci_verifier.ci_verifier_state, ci_verifier.ci_verifier_files, ci_verifier.ci_verifier_upload_parts, ci_verifier.spreadsheets, ci_verifier.spreadsheet_checks, ci_verifier.spreadsheet_sources, ci_verifier.client_contacts, ci_verifier.settings, ci_verifier.vin_index, ci_verifier.vin_indexed, ci_verifier.dashboards, ci_verifier.api_keys RESTART IDENTITY')
        self.env=patch.dict(os.environ,{'CI_NATIVE_TABLES':'1','CI_LOCAL_DEV':'1','VERCEL':'0'});self.env.start()
        self.store=PostgresWorkspace(URL);self.h=FakeHandler(self.store);self.counter=0
        self.bind=patch('hosting.public_api._build_binding',return_value={k:SCOPE[k] for k in ('site_id','list_id')});self.bind.start()
        with self.store.session() as d:SpreadsheetInbox(d).set_setting('job_registry',[dict(SCOPE,verified=True,client_key='Synthetic',client='Synthetic Client',site='SyntheticSite')])
    def tearDown(self):self.bind.stop();self.env.stop()
    def test_real_outer_commit_restart_readback_and_private_bytes(self):
        s=self.approved();p=dict(request_id=self.rid(),binding_hash=s['binding_hash']);s=svc.generate(self.h,s['id'],p)
        restarted=FakeHandler(PostgresWorkspace(URL));again=svc.generate(restarted,s['id'],p)
        self.assertEqual(s['report'],again['report'])
        with restarted.store().session() as d:
            box=SpreadsheetInbox(d);self.assertEqual(box.list()['count'],0)
            with box.connect() as db:state=svc._get(db,s['id'])
            self.assertEqual(sf.digest(files_for(d).get(state['report']['artifact_path'])),s['report']['file_hash'])
    def test_outer_failure_rolls_back_receipt_and_private_bytes(self):
        s=self.approved();real=self.store
        class Abort:
            @contextmanager
            def session(self):
                with real.session() as d:
                    yield d
                    raise StorageUnavailable('Synthetic outer abort')
        self.h._store=Abort()
        with self.assertRaises(StorageUnavailable):svc.generate(self.h,s['id'],dict(request_id=self.rid(),binding_hash=s['binding_hash']))
        self.h._store=real;self.assertIsNone(svc.view(self.h,s['id'])['report'])
        import psycopg
        with psycopg.connect(URL) as c:self.assertEqual(c.execute("SELECT count(*) FROM ci_verifier_files WHERE path LIKE 'uploads/source-first-report-%'").fetchone()[0],0)
    def test_parallel_decisions_serialize_and_changed_replay_rejects(self):
        s=self.approved();payloads=[dict(request_id='pg-concurrent-header-'+str(i),snapshot_hash=s['snapshot_hash'],binding_hash=s['binding_hash'],header={'area':'Area '+str(i),'evidence':'Checked source.'}) for i in range(2)]
        def work(p):
            try:return svc.mutate(FakeHandler(PostgresWorkspace(URL)),s['id'],'header',p)
            except sf.ReviewError as ex:return str(ex)
        with ThreadPoolExecutor(2) as pool:out=list(pool.map(work,payloads))
        self.assertEqual(sum(isinstance(x,dict) for x in out),1)
        accepted=next(i for i,x in enumerate(out) if isinstance(x,dict))
        with self.assertRaises(sf.ReviewError):svc.mutate(self.h,s['id'],'header',dict(payloads[accepted],header={}))
    def test_source_change_reopens_and_initial_freshness_is_not_final(self):
        s=self.approved();s=svc.generate(self.h,s['id'],dict(request_id=self.rid(),binding_hash=s['binding_hash']))
        self.assertFalse(s['report']['current_sharepoint'])
        s=svc.refresh(self.h,s['id'],dict(request_id=self.rid(),snapshot_hash=s['snapshot_hash']),capture_fn=lambda _:(capture(),{}))
        self.assertTrue(s['report']['current_sharepoint'])
        c=capture();c['records'][0]['version']='changed';c=capture(c['records'])
        s=svc.refresh(self.h,s['id'],dict(request_id=self.rid(),snapshot_hash=s['snapshot_hash']),capture_fn=lambda _:(c,{}))
        self.assertFalse(s['approved']);self.assertIsNone(s['report']);self.assertEqual(s['header'],{})
