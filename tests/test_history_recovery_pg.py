"""Marked disposable native PG recovery gates; never production."""
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import unittest
from tests import test_native_pg as native
from tests import test_version_reuse as reuse
from hosting.spreadsheet_inbox import SpreadsheetInbox
from hosting.storage import StorageUnavailable
from tests.test_history_recovery import ACTOR

@unittest.skipUnless(native.URL,'Set the marked disposable CI_PG_TEST_URL')
class RecoveryNative(unittest.TestCase):
    call=native.NativeTables.call
    def setUp(self):
        native.NativeTables.setUp(self)
        self.store=self.server.workspace_store
        with self.store.session() as d:
            box=SpreadsheetInbox(d);self.key=box.ingest(reuse.NAME,reuse.RAW)['file_hash']
            box._update(self.key,lambda r:r.update(
                fixed_from=dict(file_hash=self.key,revision=1),
                fixes_applied=[dict(fixed_file_hash=self.key,revision=2)],
                source_reconciliation=dict(status='incomplete',verified=False,stale=True,not_verified_because=['Synthetic missing evidence']),
                finding_decisions={'held':dict(action='defer',sheet='CSV',cell='F2',reason='Keep held.')},
                revision=2,root_hash=self.key))
    def tearDown(self):native.NativeTables.tearDown(self)
    def snapshot(self):
        with self.store.session() as d:
            box=SpreadsheetInbox(d)
            with box.connect() as db:records=[r[0] for r in db.execute('SELECT result_json FROM spreadsheet_checks ORDER BY seq')]
            return box.get(self.key),records,box.download(self.key)[1],box.working_copy(self.key)
    def preview(self):
        current=self.snapshot()[0]
        status,data=self.call('/api/spreadsheets/history-recovery',dict(file_hash=self.key,expected_check_hash=current['check_hash']))
        self.assertEqual(status,200,data);return data['result']
    def body(self,p):
        return dict(file_hash=self.key,expected_check_hash=p['expected_check_hash'],recovery_token=p['recovery_token'],apply=True,confirm=True)
    def test_http_commit_restart_preserves_bytes_old_history_holds_and_decisions(self):
        before=self.snapshot();p=self.preview()
        self.assertEqual(self.snapshot(),before)
        status,data=self.call('/api/spreadsheets/history-recovery',self.body(p))
        self.assertEqual(status,200,data);after=self.snapshot()
        self.assertEqual(after[1][:-1],before[1]);self.assertEqual(after[2],before[2]);self.assertEqual(after[3]['status'],'current')
        self.assertTrue(after[3]['history_recovery_verified'])
        for k in ('source_reconciliation','finding_decisions'):self.assertEqual(after[0][k],before[0][k])
        with self.store.session() as d:
            box=SpreadsheetInbox(d)
            with self.assertRaisesRegex(ValueError,'Reopen the rejected or deferred'):
                reuse.save(box,self.key,'original','second')
    def test_native_outer_failure_rolls_back_recovery(self):
        before=self.snapshot();p=self.preview()
        with self.assertRaises(StorageUnavailable):
            with self.store.session() as d:
                SpreadsheetInbox(d).recover_self_cycle(self.key,actor=ACTOR,
                    expected_check_hash=p['expected_check_hash'],recovery_token=p['recovery_token'],apply=True)
                raise StorageUnavailable('Synthetic outer abort')
        self.assertEqual(self.snapshot(),before)
    def test_concurrent_exact_confirmations_append_only_once(self):
        before=self.snapshot();p=self.preview()
        with ThreadPoolExecutor(2) as pool:
            results=list(pool.map(lambda _:self.call('/api/spreadsheets/history-recovery',self.body(p)),range(2)))
        self.assertEqual(sorted(x[0] for x in results),[200,409],results)
        after=self.snapshot();self.assertEqual(len(after[1]),len(before[1])+1);self.assertEqual(after[3]['status'],'current')
    def test_missing_confirmation_and_stale_inventory_do_not_write(self):
        before=self.snapshot();p=self.preview();body=self.body(p);body.pop('confirm')
        self.assertEqual(self.call('/api/spreadsheets/history-recovery',body)[0],409)
        self.assertEqual(self.snapshot(),before)
        with self.store.session() as d:SpreadsheetInbox(d).ingest('other.csv',reuse.RAW.replace(b'original',b'different'))
        snapshot=self.snapshot()
        self.assertEqual(self.call('/api/spreadsheets/history-recovery',self.body(p))[0],409)
        self.assertEqual(self.snapshot(),snapshot)
