"""Inherited HTTP feature tests using a fake transaction protocol; local-only gate adapted for this copy. SQLite integration is tested in test_sqlite_copy.py."""
import copy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.request
import urllib.error
import zipfile
from http.server import ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from api.index import handler, allowed_host
from hosting.storage import StorageUnavailable, pack, restore, NAMESPACE
from legacy_storage_fake import PostgresWorkspace

class Cursor:
    def __init__(self, row=None): self.row = row
    def fetchone(self): return self.row

class FakeDatabase:
    def __init__(self):
        self.state = None
        self.lock = threading.RLock()
        self.statements = []
        self.fail_commit = False
        self.commits = 0
    def connect(self, *args, **kwargs): return FakeConnection(self)

class FakeConnection:
    def __init__(self, database): self.database = database
    def __enter__(self):
        self.database.lock.acquire()
        self.working = copy.deepcopy(self.database.state)
        return self
    def __exit__(self, typ, value, traceback):
        try:
            if typ is None:
                if self.database.fail_commit: raise RuntimeError('simulated commit failure; SECRET must not escape')
                self.database.state = self.working
                self.database.commits += 1
        finally: self.database.lock.release()
    def execute(self, sql, params=None):
        self.database.statements.append(sql)
        if sql.startswith('INSERT'):
            if self.working is None: self.working = (None, 0)
        elif sql.startswith('SELECT'): return Cursor(self.working)
        elif sql.startswith('UPDATE'): self.working = (params[0], params[1])
        elif not sql.startswith('SET LOCAL'): raise AssertionError(sql)
        return Cursor()

class StorageTests(unittest.TestCase):
    def setUp(self):
        self.database = FakeDatabase()
        self.store = PostgresWorkspace('postgresql://test.invalid/verifier', self.database.connect)
    def test_requires_storage(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(StorageUnavailable):
                with PostgresWorkspace().session(): pass
    def test_cross_request_persistence_and_cleanup(self):
        with self.store.session() as root:
            path = root
            (root/'review.sqlite3').write_bytes(b'durable content')
        self.assertFalse(path.exists())
        with self.store.session() as root:
            self.assertEqual((root/'review.sqlite3').read_bytes(), b'durable content')
        self.assertTrue(any('FOR UPDATE' in s for s in self.database.statements))
    def test_exception_rolls_back(self):
        with self.store.session() as root: (root/'review.sqlite3').write_bytes(b'original')
        before = self.database.state
        with self.assertRaises(ValueError):
            with self.store.session() as root:
                (root/'review.sqlite3').write_bytes(b'changed')
                raise ValueError('cancel')
        self.assertEqual(self.database.state, before)
    def test_commit_failure_not_success(self):
        self.database.fail_commit = True
        with self.assertRaises(RuntimeError):
            with self.store.session() as root: (root/'review.sqlite3').write_bytes(b'unsaved')
        self.assertIsNone(self.database.state)
    def test_pack_is_deterministic(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root)/'review.sqlite3').write_bytes(b'x')
            self.assertEqual(pack(root), pack(root))
    def test_restore_rejects_traversal_and_unknown_files(self):
        for name in ('../escape', '/tmp/escape', 'approved/../escape', 'approved/a/b', 'secrets.txt', 'working\\file'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as root:
                raw = io.BytesIO()
                with zipfile.ZipFile(raw, 'w') as archive: archive.writestr(name, b'bad')
                with self.assertRaises(StorageUnavailable): restore(root, raw.getvalue())
    def test_restore_rejects_corruption(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(StorageUnavailable): restore(root, b'not a zip')
    def test_size_limit(self):
        with tempfile.TemporaryDirectory() as root, patch('hosting.storage.MAX_SNAPSHOT_BYTES', 8):
            (Path(root)/'review.sqlite3').write_bytes(b'123456789')
            with self.assertRaises(StorageUnavailable): pack(root)
    def test_read_only_transaction_keeps_revision(self):
        with self.store.session() as root: (root/'review.sqlite3').write_bytes(b'x')
        revision = self.database.state[1]
        with self.store.session(): pass
        self.assertEqual(self.database.state[1], revision)

class VercelHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = patch.dict(os.environ, {'CI_LOCAL_DEV': '1', 'VERCEL': '0'})
        cls.env.start()
    @classmethod
    def tearDownClass(cls): cls.env.stop()
    def setUp(self):
        self.database = FakeDatabase()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        self.server.workspace_store = PostgresWorkspace('postgresql://test.invalid/verifier', self.database.connect)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_port)
        self.sample = json.loads((ROOT/'samples/matched-report.json').read_text())
    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()
    def request(self, path, body=None, headers=None):
        h = {'Content-Type': 'application/json', 'Origin': self.base, 'X-Review-Token': 'same-origin'}
        h.update(headers or {})
        if body is not None and not isinstance(body, bytes): body = json.dumps(body).encode()
        request = urllib.request.Request(self.base+path, data=body, headers=h)
        try:
            with urllib.request.urlopen(request, timeout=5) as response: return response.status, response.read(), dict(response.headers)
        except urllib.error.HTTPError as error: return error.code, error.read(), dict(error.headers)
    def test_ui_and_source_allowlist(self):
        for path in ('/', '/ui.js', '/report-ui.js', '/style.css', '/assets/ppg-client-intelligence.png'):
            self.assertEqual(self.request(path)[0], 200)
        for path in ('/app/model.py', '/hosting/storage.py', '/outputs/review.sqlite3', '/.env'):
            self.assertEqual(self.request(path)[0], 404)
    def test_header_protections(self):
        code, raw, headers = self.request('/')
        self.assertIn('no-store', headers['Cache-Control'])
        self.assertEqual(headers['X-Frame-Options'], 'DENY')
        self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])
    def test_unknown_hosts_and_historical_mode_fail(self):
        self.assertEqual(self.request('/api/state', headers={'Host': 'attacker.example'})[0], 403)
        self.assertEqual(self.request('/api/state?mode=historical')[0], 403)
    def test_cross_origin_and_missing_origin_fail(self):
        for headers in ({'Origin':'https://attacker.example'}, {'Origin':''}, {'Sec-Fetch-Site':'cross-site'}, {'X-Review-Token':''}):
            self.assertEqual(self.request('/api/reports/import', self.sample, headers)[0], 403)
        self.assertIsNone(self.database.state)
    def test_non_json_fails(self):
        self.assertEqual(self.request('/api/reports/import', self.sample, {'Content-Type':'text/plain'})[0], 415)
    def test_import_persists_across_requests(self):
        self.assertEqual(self.request('/api/reports/import', self.sample)[0], 200)
        code, raw, _ = self.request('/api/reports')
        self.assertEqual(code, 200)
        state = json.loads(raw)
        self.assertEqual(len(state['reports']), 1)
        self.assertEqual(state['reports'][0]['status'], 'matched_supplied_snapshot')
        self.assertFalse(state['send_enabled'])
        self.assertIsNone(state['outbox'][0]['recipient'])
    def test_import_idempotency_and_conflict(self):
        self.request('/api/reports/import', self.sample)
        code, raw, _ = self.request('/api/reports/import', self.sample)
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(raw)['item']['idempotent'])
        altered = copy.deepcopy(self.sample); altered['report']['totals']['inspected'] += 1
        self.assertEqual(self.request('/api/reports/import', altered)[0], 409)
        self.assertEqual(len(json.loads(self.request('/api/reports')[1])['reports']), 1)
    def test_non_synthetic_and_malformed_rejected(self):
        sample = copy.deepcopy(self.sample); sample['synthetic'] = False
        self.assertEqual(self.request('/api/reports/import', sample)[0], 409)
        self.assertEqual(self.request('/api/reports/import', b'{broken')[0], 409)
        self.assertEqual(self.request('/api/edit', {'revision': True})[0], 409)
    def test_edit_persistence_and_stale_revision(self):
        initial = json.loads(self.request('/api/state')[1])['state']
        edit = {'revision':initial['revision'], 'row':0, 'col':2, 'value':'Motor launch'}
        self.assertEqual(self.request('/api/edit', edit)[0], 200)
        self.assertEqual(self.request('/api/edit', edit)[0], 409)
        current = json.loads(self.request('/api/state')[1])['state']
        self.assertEqual(current['rows'][0][2], 'Motor launch')
        self.assertEqual(current['original'][0][2], 'M0tor launch')
    def test_approval_generate_then_edit_invalidates(self):
        revision = json.loads(self.request('/api/state')[1])['state']['revision']
        payload = {'revision': revision, 'role':'reviewer', 'reviewer':'Demo reviewer', 'reviewed':True}
        self.assertEqual(self.request('/api/approve', payload)[0], 409)
        self.assertEqual(self.request('/api/validate', payload)[0], 200)
        self.assertEqual(self.request('/api/approve', payload)[0], 200)
        self.assertEqual(self.request('/api/generate', payload)[0], 200)
        self.assertEqual(self.request('/api/edit', dict(payload,row=0,col=2,value='Motor launch'))[0], 200)
        state = json.loads(self.request('/api/state')[1])['state']
        self.assertIsNone(state['approval']); self.assertIsNone(state['simulated_report'])
    def test_commit_failure_returns_503_without_secret(self):
        self.database.fail_commit = True
        code, raw, _ = self.request('/api/reports/import', self.sample)
        self.assertEqual(code, 503); self.assertNotIn(b'SECRET', raw)
        self.assertIsNone(self.database.state)
    def test_storage_missing_fails_closed(self):
        self.server.workspace_store = PostgresWorkspace()
        self.assertEqual(self.request('/api/state')[0], 503)
        self.assertEqual(self.request('/api/reports/import', self.sample)[0], 503)
    def test_download_watermarked(self):
        revision = json.loads(self.request('/api/state')[1])['state']['revision']
        code, raw, headers = self.request('/api/download', {'revision':revision})
        self.assertEqual(code, 200); self.assertIn(b'UNAPPROVED', raw)
        self.assertIn('attachment', headers['Content-Disposition'])
    def test_concurrent_imports_do_not_drop_or_duplicate(self):
        results = []
        def run(): results.append(self.request('/api/reports/import', self.sample)[0])
        threads = [threading.Thread(target=run) for _ in range(4)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(results, [200]*4)
        self.assertEqual(len(json.loads(self.request('/api/reports')[1])['reports']), 1)
    def test_bad_routes_and_oversized_body(self):
        self.assertEqual(self.request('/api/send', {'revision':1})[0], 404)
        self.assertEqual(self.request('/api/edit', b'x'*10001)[0], 413)
    def test_preview_gate_and_production_denied(self):
        with patch.dict(os.environ, {'VERCEL':'1','VERCEL_ENV':'preview','VERCEL_URL':'verifier.example','CI_VERCEL_PROTECTED_PREVIEW':'1'}):
            self.assertTrue(allowed_host('verifier.example'))
            self.assertFalse(allowed_host('attacker.example'))
            with patch.dict(os.environ, {'VERCEL_ENV':'production'}): self.assertFalse(allowed_host('verifier.example'))
            with patch.dict(os.environ, {'CI_VERCEL_PROTECTED_PREVIEW':'0'}): self.assertFalse(allowed_host('verifier.example'))

class VercelUploadTests(VercelHTTPTests):
    # Reuse harness, but avoid rerunning inherited tests in this class below.
    def upload(self, name, content):
        import base64
        return self.request('/api/spreadsheets/import', {'filename':name,'content_base64':base64.b64encode(content).decode()})
    def test_csv_upload_scope_and_original_persistence(self):
        content = b'id,inspected,accepted,rejected\nA,10,8,3\n'
        code, raw, _ = self.upload('counts.csv', content)
        self.assertEqual(code,200)
        item=json.loads(raw)['item']
        self.assertEqual(item['verification_scope'],'internal_spreadsheet_checks_only')
        self.assertEqual(item['source_authenticity'],'not_supplied')
        self.assertEqual(item['release_status'],'held')
        self.assertEqual(item['version'],1)
        code, raw, _ = self.request('/api/spreadsheet?id='+item['file_hash'])
        self.assertEqual(code,200); self.assertEqual(json.loads(raw)['filename'],'counts.csv')
        with self.server.workspace_store.session() as root:
            self.assertEqual((root/item['original_path']).read_bytes(),content)
        self.assertEqual(json.loads(self.request('/api/spreadsheets')[1])['count'],1)
    def test_same_file_idempotent_changed_file_versioned(self):
        first=self.upload('counts.csv', b'id,count\nA,2\n')
        second=self.upload('counts.csv', b'id,count\nA,2\n')
        third=self.upload('counts.csv', b'id,count\nA,3\n')
        self.assertTrue(json.loads(second[1])['item']['idempotent'])
        self.assertEqual(json.loads(third[1])['item']['version'],2)
        self.assertEqual(json.loads(self.request('/api/spreadsheets')[1])['count'],2)
    def test_invalid_upload_rollback(self):
        self.assertEqual(self.upload('evil.exe',b'x')[0],409)
        self.assertEqual(self.upload('../counts.csv',b'a,b\n1,2')[0],409)
        self.assertEqual(self.request('/api/spreadsheets/import', {'filename':'x.csv','content_base64':'###'})[0],409)
        self.assertEqual(json.loads(self.request('/api/spreadsheets')[1])['count'],0)
    def test_xlsx_upload(self):
        from openpyxl import Workbook
        workbook=Workbook(); workbook.active.append(['ID','Count']); workbook.active.append(['A',2])
        output=io.BytesIO(); workbook.save(output); workbook.close()
        code, raw, _ = self.upload('example.xlsx',output.getvalue())
        self.assertEqual(code,200); self.assertEqual(json.loads(raw)['item']['release_status'],'held')
    def test_upload_failure_does_not_commit(self):
        self.database.fail_commit=True
        self.assertEqual(self.upload('counts.csv',b'id,count\nA,1\n')[0],503)
        self.assertIsNone(self.database.state)
    def test_response_limit_rejects_before_commit(self):
        with patch('api.index.MAX_RESPONSE_BYTES',100):
            self.assertEqual(self.request('/api/reports/import',self.sample)[0],409)
        self.assertIsNone(self.database.state)
    def test_large_finding_response_regression(self):
        sample=copy.deepcopy(self.sample)
        sample['report']['records']=[{'id':str(i).zfill(60),'inspected':10,'accepted':8,'rejected':2} for i in range(3000)]
        sample['source_snapshot']['records']=[{'id':str(i).zfill(60),'inspected':20,'accepted':15,'rejected':5} for i in range(3000)]
        sample['report']['totals']={'inspected':30000,'accepted':24000,'rejected':6000}
        self.assertLess(len(json.dumps(sample).encode()),1024*1024)
        self.assertEqual(self.request('/api/reports/import',sample)[0],409)
        self.assertIsNone(self.database.state)

# Inherited test methods are exercised once by VercelHTTPTests.
for name in list(VercelHTTPTests.__dict__):
    if name.startswith('test_') and name not in VercelUploadTests.__dict__:
        setattr(VercelUploadTests,name,None)

class VercelWorkflowTests(VercelHTTPTests):
    def workflow_fixture(self):
        from test_report_workflow import fixture
        m=fixture();m['report']['records'][0]['values']['certified']=8;m['report']['totals']['certified']=8
        return m
    def workflow_import(self):
        code,raw,_=self.request('/api/workflow/import',self.workflow_fixture())
        self.assertEqual(code,200);return json.loads(raw)['item']
    def test_workflow_sample_and_assets(self):
        self.assertEqual(self.request('/workflow-ui.js')[0],200)
        code,raw,_=self.request('/api/workflow/sample');self.assertEqual(code,200);self.assertTrue(json.loads(raw)['synthetic'])
    def test_workflow_import_list_detail(self):
        item=self.workflow_import()
        code,raw,_=self.request('/api/workflow');self.assertEqual(code,200)
        data=json.loads(raw);self.assertEqual(len(data['reports']),1);self.assertFalse(data['reviewer_identity_available'])
        code,raw,_=self.request('/api/workflow/version?hash='+item['manifest_hash']);self.assertEqual(code,200)
        self.assertEqual(json.loads(raw)['report_hash'],item['report_hash'])
    def test_workflow_apply_explicit_batch(self):
        item=self.workflow_import();payload={'manifest_hash':item['manifest_hash'],'proposal_ids':[p['id'] for p in item['proposals']],'new_version':'2'}
        code,raw,_=self.request('/api/workflow/apply',payload);self.assertEqual(code,200)
        self.assertEqual(json.loads(raw)['item']['finding_count'],0)
        self.assertEqual(self.request('/api/workflow/apply',dict(payload,new_version='3'))[0],409)
    def test_workflow_client_auth_claim_cannot_approve(self):
        from test_report_workflow import fixture
        code,raw,_=self.request('/api/workflow/import',fixture());self.assertEqual(code,200)
        item=json.loads(raw)['item'];payload={'manifest_hash':item['manifest_hash'],'reviewed':True,'authenticated':True,'identity':{'subject':'x','can_approve':True}}
        code,raw,_=self.request('/api/workflow/approve',payload);self.assertEqual(code,409)
        self.assertIn(b'Authenticated reviewer integration',raw)
    def test_workflow_bulk_failure_atomic(self):
        item=self.workflow_import()
        self.assertEqual(self.request('/api/workflow/apply',{'manifest_hash':item['manifest_hash'],'proposal_ids':[item['proposals'][0]['id'],'0'*64],'new_version':'2'})[0],409)
        self.assertEqual(len(json.loads(self.request('/api/workflow')[1])['reports']),1)
    def test_workflow_private_envelope_convert(self):
        from test_source_adapter import envelope
        code,raw,_=self.request('/api/workflow/import',envelope());self.assertEqual(code,200)
        self.assertEqual(json.loads(raw)['item']['source_authenticity'],'not_authenticated')
    def test_workflow_cross_origin_blocked(self):
        self.assertEqual(self.request('/api/workflow/import',self.workflow_fixture(),{'Origin':'https://untrusted.example'})[0],403)
        self.assertIsNone(self.database.state)
    def test_workflow_missing_values_remain_blocked(self):
        m=self.workflow_fixture()
        for side in ('report','source_snapshot'):m[side]['records'][0]['values']['rejected']=None
        m['report']['totals']['rejected']=None
        code,raw,_=self.request('/api/workflow/import',m);self.assertEqual(code,200)
        item=json.loads(raw)['item'];self.assertEqual(item['status'],'blocked');self.assertFalse(item['proposals']);self.assertIsNone(item['report_totals']['rejected'])
    def test_workflow_response_limit_before_commit(self):
        with patch('api.index.MAX_RESPONSE_BYTES',100):self.assertEqual(self.request('/api/workflow/import',self.workflow_fixture())[0],409)
        self.assertIsNone(self.database.state)
    def test_workflow_bad_cursor_and_hash(self):
        self.assertEqual(self.request('/api/workflow?before=invalid')[0],409)
        self.assertEqual(self.request('/api/workflow/version?hash=invalid')[0],409)

for name in list(VercelHTTPTests.__dict__):
    if name.startswith('test_') and name not in VercelWorkflowTests.__dict__:setattr(VercelWorkflowTests,name,None)

class VercelBatchTests(VercelHTTPTests):
    def test_workflow_batch_http(self):
        from test_report_workflow import fixture
        one=fixture();two=fixture();two['scope']['shift_id']='2';two['source_snapshot']['scope']=copy.deepcopy(two['scope'])
        code,raw,_=self.request('/api/workflow/batch',{'manifests':[one,two]})
        self.assertEqual(code,200);self.assertEqual(len(json.loads(raw)['items']),2)
    def test_workflow_batch_conflict_atomic(self):
        from test_report_workflow import fixture
        one=fixture();two=fixture();two['report']['totals']['inspected']=5
        code,raw,_=self.request('/api/workflow/batch',{'manifests':[one,two]})
        self.assertEqual(code,409);self.assertIsNone(self.database.state)

for name in list(VercelHTTPTests.__dict__):
    if name.startswith('test_') and name not in VercelBatchTests.__dict__:setattr(VercelBatchTests,name,None)

class VercelBrandedTests(VercelHTTPTests):
    def upload_branded(self):
        import base64
        from test_branded_reports import raw
        code,body,_=self.request('/api/spreadsheets/import',{'filename':'synthetic.xlsx','content_base64':base64.b64encode(raw()).decode()})
        self.assertEqual(code,200);return json.loads(body)['item']
    def test_branded_mapping_http(self):
        item=self.upload_branded();self.assertEqual(item['numeric_cells_checked'],2);self.assertEqual(item['record_count'],1)
    def test_source_reconciliation_http_persistence(self):
        from test_branded_reports import source
        item=self.upload_branded();code,body,_=self.request('/api/spreadsheets/reconcile',{'file_hash':item['file_hash'],'source':source()});self.assertEqual(code,200)
        r=json.loads(body)['item'];self.assertEqual(r['numeric_values_compared'],2);self.assertEqual(r['release_status'],'held');self.assertFalse(r['approval_effective'])
        self.assertEqual(json.loads(self.request('/api/spreadsheet?id='+item['file_hash'])[1])['check_hash'],r['check_hash'])
    def test_source_reconciliation_cross_origin_rejected(self):
        from test_branded_reports import source
        item=self.upload_branded();self.assertEqual(self.request('/api/spreadsheets/reconcile',{'file_hash':item['file_hash'],'source':source()},{'Origin':'https://other.invalid'})[0],403)
    def test_source_hash_mismatch_rollback(self):
        from test_branded_reports import source
        item=self.upload_branded();s=source();s['records'][0]['values']['inspected']=1
        self.assertEqual(self.request('/api/spreadsheets/reconcile',{'file_hash':item['file_hash'],'source':s})[0],409)
        self.assertEqual(json.loads(self.request('/api/spreadsheet?id='+item['file_hash'])[1])['check_hash'],item['check_hash'])
    def test_source_response_overflow_before_commit(self):
        from test_branded_reports import source
        item=self.upload_branded()
        with patch('api.index.MAX_RESPONSE_BYTES',100):self.assertEqual(self.request('/api/spreadsheets/reconcile',{'file_hash':item['file_hash'],'source':source()})[0],409)
        self.assertEqual(json.loads(self.request('/api/spreadsheet?id='+item['file_hash'])[1])['check_hash'],item['check_hash'])

for name in list(VercelHTTPTests.__dict__):
    if name.startswith('test_') and name not in VercelBrandedTests.__dict__:setattr(VercelBrandedTests,name,None)
