"""The same HTTP reuse/auth/page contract on a marked local disposable PostgreSQL."""
import unittest
from tests import test_native_pg as native
from tests import test_version_reuse as reuse
from hosting.spreadsheet_inbox import SpreadsheetInbox

@unittest.skipUnless(native.URL, 'Set the marked local disposable CI_PG_TEST_URL')
class VersionReusePostgres(reuse.VersionReuseHTTPTests):
    def setUp(self):
        native.NativeTables.setUp(self)
        self.store=self.server.workspace_store
        with self.store.session() as directory:
            box=SpreadsheetInbox(directory);self.a=box.ingest(reuse.NAME,reuse.RAW)['file_hash']
            self.b=reuse.save(box,self.a,'original','second')['file_hash']
            self.c=reuse.save(box,self.b,'second','third')['file_hash']
    def tearDown(self):
        native.NativeTables.tearDown(self)
