"""Working-copy contract against a safety-marked disposable PostgreSQL only."""
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from tests import test_native_pg as native
from tests import test_working_copy as working
from tests import test_version_reuse as reuse
from hosting.spreadsheet_inbox import SpreadsheetInbox

@unittest.skipUnless(native.URL, 'Set the marked local disposable CI_PG_TEST_URL')
class WorkingCopyPostgres(working.WorkingCopyHTTPTests):
    def setUp(self):
        native.NativeTables.setUp(self)
        self.store = self.server.workspace_store
        with self.store.session() as directory:
            box = SpreadsheetInbox(directory)
            self.a = box.ingest(reuse.NAME, reuse.RAW)['file_hash']
            self.b = reuse.save(box, self.a, 'original', 'second')['file_hash']
            self.c = reuse.save(box, self.b, 'second', 'third')['file_hash']
    def tearDown(self):
        native.NativeTables.tearDown(self)

    def test_competing_saves_create_only_one_child(self):
        barrier = threading.Barrier(2); local = threading.local()
        original = SpreadsheetInbox._check_changes
        def prepare(box, *args, **kwargs):
            result = original(box, *args, **kwargs)
            if not getattr(local, 'prepared', False):
                local.prepared = True
                barrier.wait(timeout=15)
            return result
        with self.store.session() as directory:
            expected = SpreadsheetInbox(directory).get(self.c)['check_hash']
        def save(value):
            with self.store.session() as directory:
                box = SpreadsheetInbox(directory)
                try:
                    return box.apply_fixes(self.c, [reuse.change('third', value)], expected_check_hash=expected)['file_hash']
                except ValueError as exc:
                    return str(exc)
        with patch.object(SpreadsheetInbox, '_check_changes', prepare), ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(save, ('parallel one', 'parallel two')))
        self.assertEqual(sum(len(value) == 64 for value in outcomes), 1, outcomes)
        self.assertTrue(any('changed' in value or 'superseded' in value for value in outcomes), outcomes)
        with self.store.session() as directory:
            box = SpreadsheetInbox(directory)
            self.assertEqual(box.list()['count'], 4)
            self.assertEqual(box.working_copy(self.a)['status'], 'superseded')
