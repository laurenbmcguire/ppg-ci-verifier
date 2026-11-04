"""Protected HTTP dispatch and immutable review tests with synthetic Pratt rows."""
import base64
import io
import json
import test_vercel
from test_pratt_reports import workbook,union_workbook,source,rehash

class PrattHTTPTests(test_vercel.VercelHTTPTests):
    def upload_pratt(self, union=False):
        stream=io.BytesIO();(union_workbook() if union else workbook()).save(stream)
        code,body,_=self.request('/api/spreadsheets/import',{'filename':'synthetic-pratt.xlsx','content_base64':base64.b64encode(stream.getvalue()).decode()})
        self.assertEqual(code,200);return json.loads(body)['item']
    def test_original_template_dispatched_after_preflight(self):
        r=self.upload_pratt();self.assertEqual(r['template_id'],'pratt-kn-daily-audit/1');self.assertEqual(r['numeric_cells_checked'],173);self.assertEqual(r['release_status'],'held')
    def test_union_comparison_persists_without_approval(self):
        r=self.upload_pratt(True);s=source(r)
        code,body,_=self.request('/api/spreadsheets/reconcile',{'file_hash':r['file_hash'],'source':s});self.assertEqual(code,200)
        c=json.loads(body)['item'];self.assertEqual(c['numeric_values_compared'],195);self.assertEqual(c['source_difference_count'],0);self.assertFalse(c['approval_effective']);self.assertEqual(c['release_status'],'held')
        self.assertEqual(json.loads(self.request('/api/spreadsheet?id='+r['file_hash'])[1])['check_hash'],c['check_hash'])
        code,body,_=self.request('/api/spreadsheets/reconcile',{'file_hash':r['file_hash'],'source':s});self.assertEqual(code,200);self.assertTrue(json.loads(body)['item']['idempotent'])
    def test_union_source_hash_tampering_rolls_back(self):
        r=self.upload_pratt(True);s=source(r);s['source_sha256']='0'*64
        self.assertEqual(self.request('/api/spreadsheets/reconcile',{'file_hash':r['file_hash'],'source':s})[0],409)
        self.assertEqual(json.loads(self.request('/api/spreadsheet?id='+r['file_hash'])[1])['check_hash'],r['check_hash'])
    def test_unknown_capture_is_honest_and_idempotent(self):
        r=self.upload_pratt(True);s=source(r);s['captured_at']=None;s['capture_time_status']='unknown';rehash(s)
        code,body,_=self.request('/api/spreadsheets/reconcile',{'file_hash':r['file_hash'],'source':s});self.assertEqual(code,200)
        c=json.loads(body)['item'];self.assertIsNone(c['source_captured_at']);self.assertIn('source_capture_time_unverified',c['finding_counts'])
        code,body,_=self.request('/api/spreadsheets/reconcile',{'file_hash':r['file_hash'],'source':s});self.assertEqual(code,200);self.assertTrue(json.loads(body)['item']['idempotent'])
    def test_unknown_capture_cannot_be_automatically_replaced(self):
        r=self.upload_pratt(True);known=source(r);unknown=dict(known,captured_at=None,capture_time_status='unknown');rehash(unknown)
        code,body,_=self.request('/api/spreadsheets/reconcile',{'file_hash':r['file_hash'],'source':unknown});self.assertEqual(code,200);saved=json.loads(body)['item']['check_hash']
        self.assertEqual(self.request('/api/spreadsheets/reconcile',{'file_hash':r['file_hash'],'source':known})[0],409)
        self.assertEqual(json.loads(self.request('/api/spreadsheet?id='+r['file_hash'])[1])['check_hash'],saved)
    def test_known_capture_cannot_be_replaced_by_unknown(self):
        r=self.upload_pratt(True);known=source(r);unknown=dict(known,captured_at=None,capture_time_status='unknown');rehash(unknown)
        self.assertEqual(self.request('/api/spreadsheets/reconcile',{'file_hash':r['file_hash'],'source':known})[0],200)
        self.assertEqual(self.request('/api/spreadsheets/reconcile',{'file_hash':r['file_hash'],'source':unknown})[0],409)
    def test_cross_schema_does_not_fall_back(self):
        from test_branded_reports import source as ppg_source
        r=self.upload_pratt(True)
        self.assertEqual(self.request('/api/spreadsheets/reconcile',{'file_hash':r['file_hash'],'source':ppg_source()})[0],409)
    def test_cross_origin_reconcile_rejected(self):
        r=self.upload_pratt(True)
        self.assertEqual(self.request('/api/spreadsheets/reconcile',{'file_hash':r['file_hash'],'source':source(r)},{'Origin':'https://untrusted.invalid'})[0],403)

for name in list(test_vercel.VercelHTTPTests.__dict__):
    if name.startswith('test_') and name not in PrattHTTPTests.__dict__:setattr(PrattHTTPTests,name,None)
