"""Synthetic local SQLite acceptance tests; no network databases or credentials."""
import base64, hashlib, json, os, sqlite3, subprocess, sys, tempfile, threading, time, unittest
from pathlib import Path
from unittest.mock import patch
from http.server import ThreadingHTTPServer
import urllib.request, urllib.error
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from hosting.sqlite_storage import SQLiteWorkspace, StorageUnavailable, SERVERLESS_MARKERS
from hosting.sqlite_backup import backup
from api.index import handler, allowed_host

def child(directory,script):
    return subprocess.Popen([sys.executable,'-c','from pathlib import Path\nfrom hosting.sqlite_storage import SQLiteWorkspace\nimport sys,time,os,sqlite3\nstore=SQLiteWorkspace(sys.argv[1])\n'+script,str(directory)],cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)

class SQLiteTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.directory=Path(self.temp.name)
        self.env=patch.dict(os.environ,{**{k:'' for k in SERVERLESS_MARKERS},'CI_LOCAL_DEV':'1'});self.env.start()
        self.store=SQLiteWorkspace(self.directory)
    def tearDown(self):self.env.stop();self.temp.cleanup()
    def write(self,value):
        with self.store.session() as root:(root/'review.sqlite3').write_bytes(value)
    def read(self):
        with self.store.session() as root:return (root/'review.sqlite3').read_bytes()
    def revision(self):
        with sqlite3.connect(self.store.database) as db:return db.execute('SELECT revision FROM workspace').fetchone()[0]
    def test_restart_in_independent_process(self):
        self.write(b'persisted')
        p=child(self.directory,"with store.session() as root: print((root/'review.sqlite3').read_text())\n")
        out,err=p.communicate(timeout=10);self.assertEqual(p.returncode,0,err);self.assertEqual(out.strip(),'persisted')
    def test_wal_full_timeout_permissions(self):
        self.write(b'1');db=self.store._connect()
        try:
            for pragma,want in [('journal_mode','wal'),('synchronous',2),('busy_timeout',5000)]:self.assertEqual(db.execute('PRAGMA '+pragma).fetchone()[0],want)
        finally:db.close()
        self.assertEqual(self.store.database.stat().st_mode&0o777,0o600)
    def test_read_preserves_revision(self):
        self.write(b'1');rev=self.revision();self.assertEqual(self.read(),b'1');self.assertEqual(self.revision(),rev)
    def test_exception_atomic_rollback(self):
        self.write(b'old');rev=self.revision()
        with self.assertRaises(ValueError):
            with self.store.session() as root:
                (root/'review.sqlite3').write_bytes(b'new');(root/'uploads').mkdir();(root/'uploads/new.csv').write_bytes(b'synthetic')
                raise ValueError('injected')
        self.assertEqual(self.read(),b'old');self.assertEqual(self.revision(),rev)
        with self.store.session() as root:self.assertFalse((root/'uploads').exists())
    def test_independent_process_concurrency(self):
        self.write(b'0')
        script="for i in range(12):\n with store.session() as root:\n  p=root/'review.sqlite3'\n  n=int(p.read_text())\n  time.sleep(.003)\n  p.write_text(str(n+1))\n"
        processes=[child(self.directory,script) for _ in range(4)]
        for p in processes:
            out,err=p.communicate(timeout=30);self.assertEqual(p.returncode,0,err)
        self.assertEqual(self.read(),b'48')
    def test_lock_timeout(self):
        self.write(b'old')
        with sqlite3.connect(self.store.database) as db:
            db.execute('BEGIN IMMEDIATE');start=time.monotonic()
            with self.assertRaises(StorageUnavailable):
                with SQLiteWorkspace(self.directory,timeout=.05).session():pass
            self.assertLess(time.monotonic()-start,2);db.rollback()
        self.assertEqual(self.read(),b'old')
    def crash(self,script):
        self.write(b'old');p=child(self.directory,script)
        try:self.assertEqual(p.stdout.readline().strip(),'ready');p.kill();p.communicate(timeout=10)
        finally:
            if p.poll() is None:p.kill();p.communicate()
        self.assertEqual(self.read(),b'old')
    def test_crash_during_feature_operation(self):
        self.crash("with store.session() as root:\n (root/'review.sqlite3').write_bytes(b'new')\n print('ready',flush=True)\n time.sleep(60)\n")
    def test_crash_after_db_update_before_commit(self):
        self.crash("db=sqlite3.connect(store.database)\ndb.execute('BEGIN IMMEDIATE')\ndb.execute(\"UPDATE workspace SET payload=x'00'\")\nprint('ready',flush=True)\ntime.sleep(60)\n")
    def test_commit_failure(self):
        self.write(b'old');connect=self.store._connect
        class Failed:
            def __init__(self,db):self.db=db
            def __getattr__(self,key):return getattr(self.db,key)
            def commit(self):raise sqlite3.OperationalError('injected commit failure')
        with patch.object(self.store,'_connect',side_effect=lambda:Failed(connect())):
            with self.assertRaises(StorageUnavailable):self.write(b'new')
        self.assertEqual(self.read(),b'old')
    def test_immutable_originals(self):
        for folder in ('original','uploads'):
            with self.store.session() as root:(root/folder).mkdir(exist_ok=True);(root/folder/'sample.csv').write_bytes(b'original')
        for folder in ('original','uploads'):
            for delete in (True,False):
                with self.subTest(folder=folder,delete=delete),self.assertRaises(StorageUnavailable):
                    with self.store.session() as root:
                        p=root/folder/'sample.csv'
                        if delete:p.unlink()
                        else:p.chmod(0o600);p.write_bytes(b'changed')
        with self.store.session() as root:
            for folder in ('original','uploads'):self.assertEqual((root/folder/'sample.csv').read_bytes(),b'original')
    def test_bounds(self):
        self.write(b'old')
        for key,value in [('MAX_SNAPSHOT_BYTES',2),('MAX_FILES',0)]:
            with patch('hosting.sqlite_storage.'+key,value),self.assertRaises(StorageUnavailable):self.write(b'new')
            self.assertEqual(self.read(),b'old')
    def test_directory_symlink_rejected(self):
        self.write(b'old')
        with self.assertRaises(StorageUnavailable):
            with self.store.session() as root:(root/'uploads').symlink_to(self.directory,target_is_directory=True)
        self.assertEqual(self.read(),b'old')
    def test_corruption(self):
        self.write(b'old')
        with sqlite3.connect(self.store.database) as db:db.execute("UPDATE workspace SET payload=x'00'")
        with self.assertRaises(StorageUnavailable):self.read()
    def test_stored_blob_rejected_before_loading(self):
        self.write(b'old')
        with sqlite3.connect(self.store.database) as db:
            db.execute('UPDATE workspace SET payload=zeroblob(16777217)')
        with self.assertRaisesRegex(StorageUnavailable,'exceeds'):
            self.read()
    def test_stored_text_rejected(self):
        self.write(b'old')
        with sqlite3.connect(self.store.database) as db:
            db.execute("UPDATE workspace SET payload='not a blob'")
        with self.assertRaisesRegex(StorageUnavailable,'invalid'):
            self.read()
    def test_foreign_database_unchanged(self):
        with sqlite3.connect(self.store.database) as db:
            db.execute('CREATE TABLE unrelated (value TEXT)')
            db.execute("INSERT INTO unrelated VALUES ('do not modify')")
        before=self.store.database.read_bytes()
        with self.assertRaisesRegex(StorageUnavailable,'not a LOCAL SQLITE COPY'):
            with self.store.session():pass
        self.assertEqual(self.store.database.read_bytes(),before)
        self.assertFalse(Path(str(self.store.database)+'-wal').exists())
    def test_foreign_application_id_unchanged(self):
        with sqlite3.connect(self.store.database) as db:db.execute('PRAGMA application_id=123')
        before=self.store.database.read_bytes()
        with self.assertRaises(StorageUnavailable):
            with self.store.session():pass
        self.assertEqual(self.store.database.read_bytes(),before)
    def test_database_url_ignored(self):
        with patch.dict(os.environ,{'DATABASE_URL':'postgresql://do-not-connect.invalid/existing'}),patch('socket.create_connection',side_effect=AssertionError('network forbidden')):
            self.write(b'local');self.assertEqual(self.read(),b'local')
    def test_serverless_refused(self):
        for marker in SERVERLESS_MARKERS:
            with self.subTest(marker=marker),patch.dict(os.environ,{marker:'1'}):
                self.assertFalse(allowed_host('localhost:8766'))
                with self.assertRaises(StorageUnavailable):SQLiteWorkspace(self.directory)
                with self.assertRaises(StorageUnavailable):
                    with self.store.session():pass
    def test_backup_live_wal_restore(self):
        self.write(b'old');reader=sqlite3.connect(self.store.database);reader.execute('SELECT * FROM workspace').fetchall()
        try:
            self.write(b'new');dest=self.directory/'backup.sqlite3';backup(self.store.database,dest)
            with self.assertRaises(FileExistsError):backup(self.store.database,dest)
            restored=self.directory/'restored';restored.mkdir();(restored/'workspace.sqlite3').write_bytes(dest.read_bytes())
            with SQLiteWorkspace(restored).session() as root:self.assertEqual((root/'review.sqlite3').read_bytes(),b'new')
        finally:reader.close()
    def start_server(self):
        self.server=ThreadingHTTPServer(('127.0.0.1',0),handler);self.server.workspace_store=SQLiteWorkspace(self.directory)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start();self.base='http://127.0.0.1:'+str(self.server.server_port)
    def stop_server(self):self.server.shutdown();self.server.server_close();self.thread.join()
    def request(self,path,body=None,headers=None):
        h={'Content-Type':'application/json','Origin':self.base,'X-Review-Token':'same-origin'};h.update(headers or {})
        req=urllib.request.Request(self.base+path,data=json.dumps(body).encode() if body is not None else None,headers=h)
        try:
            with urllib.request.urlopen(req,timeout=10) as response:return response.status,json.loads(response.read())
        except urllib.error.HTTPError as exc:return exc.code,json.loads(exc.read())
    def test_synthetic_upload_http_restart(self):
        self.start_server()
        try:
            raw=b'label,inspected,accepted,rejected\nsynthetic,10,8,2\n'
            status,data=self.request('/api/spreadsheets/import',{'filename':'synthetic.csv','content_base64':base64.b64encode(raw).decode()});self.assertEqual(status,200,data)
            before=self.request('/api/spreadsheets')[1];health=self.request('/api/health')[1]
            self.assertEqual(health['storage'],'local-sqlite');self.assertFalse(health['reviewer_identity_available']);self.assertTrue(health['local_only'])
        finally:self.stop_server()
        self.start_server()
        try:
            self.assertEqual(before,self.request('/api/spreadsheets')[1])
            status,data=self.request('/api/workflow/approve',{'manifest_hash':'0'*64,'reviewed':True,'identity':'fake'});self.assertEqual(status,409,data)
            with self.store.session() as root:self.assertEqual((root/'uploads'/(hashlib.sha256(raw).hexdigest()+'.csv')).read_bytes(),raw)
        finally:self.stop_server()
    def test_http_commit_failure_returns_no_success(self):
        self.start_server()
        try:
            self.assertEqual(self.request('/api/health')[0],200)
            before=self.revision()
            connect=self.server.workspace_store._connect
            class Failed:
                def __init__(self,db):self.db=db
                def __getattr__(self,key):return getattr(self.db,key)
                def commit(self):raise sqlite3.OperationalError('injected commit failure')
            with patch.object(self.server.workspace_store,'_connect',side_effect=lambda:Failed(connect())):
                status,data=self.request('/api/spreadsheets/import',{'filename':'synthetic.csv','content_base64':base64.b64encode(b'label,count\na,1\n').decode()})
                self.assertEqual(status,503,data)
            self.assertEqual(self.revision(),before)
            self.assertEqual(self.request('/api/spreadsheets')[1]['count'],0)
        finally:self.stop_server()
    def test_nonloopback_bound_server_denied(self):
        self.start_server()
        try:
            # Keep the real socket loopback-bound while presenting a nonlocal bind to the guard.
            self.server.server_address=('0.0.0.0',self.server.server_port)
            self.assertEqual(self.request('/api/health')[0],403)
        finally:self.stop_server()
    def test_http_guards_no_fallback(self):
        self.start_server()
        try:
            self.assertEqual(self.request('/api/health',headers={'Host':'public.example'})[0],403)
            self.assertEqual(self.request('/api/edit',{},headers={'Origin':'https://evil.example'})[0],403)
            with patch.dict(os.environ,{'VERCEL':'1','VERCEL_ENV':'preview','CI_VERCEL_PROTECTED_PREVIEW':'1'}):self.assertEqual(self.request('/api/health')[0],403)
            del self.server.workspace_store
            with patch.dict(os.environ,{'DATABASE_URL':'postgresql://existing.invalid/no-touch'}):self.assertEqual(self.request('/api/health')[0],503)
        finally:self.stop_server()
if __name__=='__main__':unittest.main()
