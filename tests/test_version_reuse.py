"""Exact-content reuse is a read-only, authorized navigation outcome, not a new correction."""
import copy
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
from hosting.spreadsheet_inbox import SpreadsheetInbox
from hosting.fixes import apply_fixes
from api.index import handler
from legacy_storage_fake import PostgresWorkspace
from test_vercel import FakeDatabase

RAW = b'Date,Identifier,Qty Inspected,Qty OK,Qty NOK,Note\r\n2026-10-07,8010808540,34,30,4,original\r\n'
NAME = 'synthetic-versions.csv'
def change(before, after):
    return dict(sheet='CSV', cell='F2', before=before, after=after, kind='text', source='Your edit', reason='Synthetic approved edit')
def save(box, h, before, after):
    return box.apply_fixes(h, [change(before, after)], expected_check_hash=box.get(h)['check_hash'])

class VersionReuseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.box = SpreadsheetInbox(self.temp.name)
        self.a = self.box.ingest(NAME, RAW)['file_hash']
    def history(self):
        with self.box.connect() as db:
            return [tuple(row) for row in db.execute('SELECT * FROM spreadsheet_checks ORDER BY seq').fetchall()]
    def test_descendant_and_parallel_branch_reuse_preserves_all_history(self):
        b = save(self.box, self.a, 'original', 'second')['file_hash']
        c = save(self.box, b, 'second', 'third')['file_hash']
        # A separate child is chronologically newer, but must never substitute for C.
        d = save(self.box, self.a, 'original', 'parallel')['file_hash']
        before = self.history(); metadata = copy.deepcopy(self.box.get(c))
        for source, value in [(self.a, 'original'), (d, 'parallel')]:
            result = save(self.box, source, value, 'third')
            self.assertEqual(result['file_hash'], c)
            self.assertTrue(result['already_applied'])
            self.assertEqual(result['save_outcome']['type'], 'existing_version')
            self.assertEqual(result['save_outcome']['requested_from'], source)
            self.assertEqual(result['save_outcome']['changes'][0]['before'], value)
            self.assertEqual(result['fixed_from']['file_hash'], b)
            self.assertEqual(self.history(), before)
            self.assertEqual(self.box.get(c), metadata)
        self.assertEqual(self.box.download(self.a)[1], RAW)
        self.assertEqual(self.box.list()['count'], 4)
    def test_ancestor_and_noop_reuse_do_not_create_cycles_or_revision(self):
        b = save(self.box, self.a, 'original', 'second')['file_hash']
        before = self.history()
        ancestor = save(self.box, b, 'second', 'original')
        same = save(self.box, self.a, 'original', 'original')
        self.assertEqual(ancestor['file_hash'], self.a)
        self.assertEqual(same['file_hash'], self.a)
        self.assertNotIn('fixed_from', self.box.get(self.a))
        self.assertEqual(self.history(), before)
    def test_unrelated_same_bytes_and_forged_root_hint_are_not_reparented(self):
        raw = apply_fixes(NAME, RAW, [change('original', 'third')])
        other = self.box.ingest('other-client.csv', raw)['file_hash']
        self.box._update(other, lambda r: r.update(root_hash=self.a))
        before = self.history(); original = self.box.get(other)
        with self.assertRaisesRegex(ValueError, 'unrelated upload'):
            save(self.box, self.a, 'original', 'third')
        self.assertEqual(self.history(), before)
        self.assertEqual(self.box.get(other), original)
        self.assertNotIn('fixed_from', original)
    def test_conflicting_stored_parent_records_fail_closed(self):
        b = save(self.box, self.a, 'original', 'second')['file_hash']
        c = save(self.box, b, 'second', 'third')['file_hash']
        self.box._update(c, lambda r: r.update(fixed_from=dict(r['fixed_from'], file_hash=self.a)))
        before = self.history()
        with self.assertRaisesRegex(ValueError, 'unrelated upload'):
            save(self.box, self.a, 'original', 'third')
        self.assertEqual(self.history(), before)

    def test_missing_cyclic_malformed_or_removed_parent_link_fails_closed(self):
        for malformed in ({'file_hash': 'f'*64}, {'file_hash': self.a}, ['not a link']):
            with self.subTest(parent=malformed), tempfile.TemporaryDirectory() as root:
                box=SpreadsheetInbox(root); a=box.ingest(NAME,RAW)['file_hash']
                b=save(box,a,'original','second')['file_hash']
                box._update(a,lambda r:r.update(fixed_from=malformed))
                with self.assertRaisesRegex(ValueError,'unrelated upload'):
                    save(box,b,'second','original')
        b=save(self.box,self.a,'original','second')['file_hash']
        self.box._update(b,lambda r:r.pop('fixed_from',None))
        with self.assertRaisesRegex(ValueError,'unrelated upload'):
            save(self.box,self.a,'original','second')
    def test_missing_root_check_history_cannot_prove_ancestry(self):
        b=save(self.box,self.a,'original','second')['file_hash']
        with self.box.connect() as db:
            db.execute('DELETE FROM spreadsheet_checks WHERE file_hash=?',(self.a,))
        with self.assertRaisesRegex(ValueError,'unrelated upload'):
            save(self.box,b,'second','original')
    def test_denied_new_destination_never_stores_bytes_or_history(self):
        before=self.history(); files={str(p):p.read_bytes() for p in self.box.root.glob('uploads/*')}
        from hosting.spreadsheet_inbox import CorrectionAccessDenied
        with self.assertRaises(CorrectionAccessDenied):
            self.box.apply_fixes(self.a,[change('original','new')],expected_check_hash=self.box.get(self.a)['check_hash'],authorize_destination=lambda candidate:False)
        self.assertEqual(before,self.history());self.assertEqual(files,{str(p):p.read_bytes() for p in self.box.root.glob('uploads/*')})

class VersionReuseHTTPTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {'CI_LOCAL_DEV': '1', 'VERCEL': '0'}); self.env.start(); self.addCleanup(self.env.stop)
        self.store = PostgresWorkspace('postgresql://test.invalid/reuse', FakeDatabase().connect)
        with self.store.session() as directory:
            box = SpreadsheetInbox(directory); self.a = box.ingest(NAME, RAW)['file_hash']
            self.b = save(box, self.a, 'original', 'second')['file_hash']
            self.c = save(box, self.b, 'second', 'third')['file_hash']
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler); self.server.workspace_store = self.store
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'
    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()
    def request(self, path, body=None, extra_headers=None):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None,
            headers={'Content-Type': 'application/json', 'Origin': self.base, 'X-Review-Token': 'same-origin', **(extra_headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=15) as r: return r.status, json.loads(r.read())
        except urllib.error.HTTPError as r: return r.code, json.loads(r.read())
    def payload(self):
        with self.store.session() as directory:
            return dict(file_hash=self.a, changes=[change('original', 'third')], expected_check_hash=SpreadsheetInbox(directory).get(self.a)['check_hash'])
    def test_reuse_skips_mutation_capable_post_checks(self):
        with patch('api.index.sharepoint_configured', return_value=True), patch.object(handler, 'sharepoint_check') as source, patch.object(handler, 'baseline_check') as baseline:
            status, response = self.request('/api/spreadsheets/apply-fixes', self.payload())
        self.assertEqual(status, 200, response); self.assertEqual(response['item']['file_hash'], self.c)
        self.assertTrue(response['already_applied']); source.assert_not_called(); baseline.assert_not_called()
        self.assertEqual(response['item']['fixed_from']['file_hash'], self.b)
    def test_destination_is_authorized_before_metadata_exposure_or_checks(self):
        original = handler.can_hash
        def can_read(h, file_hash): return False if file_hash == self.c else original(h, file_hash)
        with patch.object(handler, 'can_hash', can_read), patch.object(handler, 'sharepoint_check') as source, patch.object(handler, 'baseline_check') as baseline:
            status, response = self.request('/api/spreadsheets/apply-fixes', self.payload())
        self.assertEqual(status, 404, response)
        self.assertEqual(response, {'error': 'Report not found.', 'fail_closed': True})
        source.assert_not_called(); baseline.assert_not_called()
    def test_page_binding_detects_same_count_recheck_and_revision_changes(self):
        status, report = self.request('/api/spreadsheet?id=' + self.a); self.assertEqual(status, 200)
        bound = report['review']['page']['binding']
        request = dict(file_hash=self.a, list='issues', offset=0, limit=25, expected_binding=bound)
        status, page = self.request('/api/spreadsheets/findings', request); self.assertEqual(status, 200)
        self.assertEqual(page['binding'], bound); self.assertEqual(page['rev'], bound['rev'])
        with self.store.session() as directory:
            SpreadsheetInbox(directory)._update(self.a, lambda r: r.update(synthetic_check_marker='new check, same findings'))
        status, rejected = self.request('/api/spreadsheets/findings', request)
        self.assertEqual(status, 409, rejected); self.assertIn('findings changed', rejected['error'])

    def test_fresh_allowed_child_keeps_existing_client_resolution(self):
        from eos_fixture import build, ROWS
        from test_access import CLIENTS
        with self.store.session() as directory:
            box=SpreadsheetInbox(directory); a=box.ingest('2230-OWENS-BASCO.xlsx',build())
        payload=dict(file_hash=a['file_hash'],expected_check_hash=a['check_hash'],changes=[dict(sheet='1st Shift',cell='L17',before=ROWS[1][5],after='Synthetic reviewer comment',source='Your edit',kind='text')])
        member=dict(role='member',clients=['Owens'])
        with patch.object(handler,'current_user',return_value=member),patch.object(handler,'clients_map',return_value=CLIENTS),patch.object(handler,'baseline_check',side_effect=RuntimeError('Synthetic no post-check')):
            status,result=self.request('/api/spreadsheets/apply-fixes',payload)
            self.assertEqual(status,200,result)
            status,opened=self.request('/api/spreadsheet?id='+result['item']['file_hash'])
            self.assertEqual(status,200,opened)
    def test_manual_parent_assignment_does_not_grant_unresolved_child_access(self):
        with self.store.session() as directory:
            box=SpreadsheetInbox(directory);box.set_setting('assignment|'+self.a,dict(client_key='Owens',client='Owens',job='unlisted',by='Synthetic'))
            before={h:box.get(h) for h in (self.a,self.b,self.c)}
        payload=self.payload();payload['changes']=[change('original','unseen child')]
        with patch.object(handler,'current_user',return_value=dict(role='member',clients=['Owens'])),patch.object(handler,'clients_map',return_value=[]):
            status,result=self.request('/api/spreadsheets/apply-fixes',payload)
        self.assertEqual(status,404,result);self.assertEqual(result,dict(error='Report not found.',fail_closed=True))
        with self.store.session() as directory:
            box=SpreadsheetInbox(directory);self.assertEqual(box.list()['count'],3);self.assertEqual(before,{h:box.get(h) for h in before})
    def test_revoked_destination_during_precommit_check_rolls_back(self):
        payload=self.payload();payload['changes']=[change('original','revoked child')]
        with patch.object(handler,'current_user',return_value=dict(role='member',clients=['Owens'])),patch.object(handler,'clients_map',return_value=[]),patch.object(handler,'can_report',side_effect=[True,False]):
            status,result=self.request('/api/spreadsheets/apply-fixes',payload)
        self.assertEqual(status,404,result)
        with self.store.session() as directory:self.assertEqual(SpreadsheetInbox(directory).list()['count'],3)
    def test_postcommit_access_loss_is_explicit_without_destination_leak(self):
        original=handler.can_hash
        def can_read(h,file_hash):return original(h,file_hash) if file_hash==self.a else False
        payload=self.payload();payload['changes']=[change('original','committed child')]
        with patch.object(handler,'can_hash',can_read):status,result=self.request('/api/spreadsheets/apply-fixes',payload)
        self.assertEqual(status,403,result);self.assertTrue(result['save_committed']);self.assertNotIn('item',result)
        self.assertIn('was saved',result['error'])
        with self.store.session() as directory:self.assertEqual(SpreadsheetInbox(directory).list()['count'],4)
    def test_public_api_and_mcp_reuse_are_read_only_and_honest(self):
        with self.store.session() as directory:
            box=SpreadsheetInbox(directory);key=box.create_key('Synthetic API reuse')['key'];before={h:box.get(h) for h in (self.a,self.b,self.c)}
        payload=self.payload();headers={'Authorization':'Bearer '+key}
        with patch('hosting.public_api._after_change') as after:
            status,result=self.request('/api/v1/reports/'+self.a+'/fixes',payload,headers)
            self.assertEqual(status,201,result);self.assertEqual(result['id'],self.c);self.assertEqual(result['applied'],0);self.assertEqual(result['requested_changes'],1);self.assertTrue(result['already_applied'])
            status,rpc=self.request('/mcp/'+key,dict(jsonrpc='2.0',id=1,method='tools/call',params=dict(name='apply_fixes',arguments=dict(id=self.a,changes=payload['changes'],expected_check_hash=payload['expected_check_hash']))))
            self.assertEqual(status,200,rpc);self.assertFalse(rpc['result']['isError'],rpc)
            data=json.loads(rpc['result']['content'][0]['text']);self.assertEqual(data['id'],self.c);self.assertEqual(data['applied'],0);self.assertEqual(data['save_outcome']['type'],'existing_version');after.assert_not_called()
        with self.store.session() as directory:
            box=SpreadsheetInbox(directory);self.assertEqual(before,{h:box.get(h) for h in before});self.assertEqual(box.list()['count'],3)

    def session_fixture(self):
        from hosting import access
        from eos_fixture import build, ROWS
        with self.store.session() as directory:
            box=SpreadsheetInbox(directory)
            admin=access.save_user(box,dict(email='admin@synthetic.invalid',role='admin'))
            member=access.save_user(box,dict(email='member@synthetic.invalid',role='member',clients=['Owens']))
            token=access.start_session(box,member);access.set_required(box,True,admin)
            a=box.ingest('2230-OWENS-BASCO.xlsx',build())
        payload=dict(file_hash=a['file_hash'],expected_check_hash=a['check_hash'],changes=[dict(sheet='1st Shift',cell='L17',before=ROWS[1][5],after='Synthetic session edit',source='Your edit',kind='text')])
        return member,{'Cookie':access.COOKIE+'='+token},payload
    def test_actual_session_client_revocation_before_commit_denies_without_report_write(self):
        from hosting import access
        from test_access import CLIENTS
        member,headers,payload=self.session_fixture();original=handler.can_hash;revoked=[]
        def revoke_after_parent(h,file_hash):
            allowed=original(h,file_hash)
            if allowed and not revoked:
                with h.store().session() as directory:access.save_user(SpreadsheetInbox(directory),dict(member,clients=[]))
                revoked.append(True)
            return allowed
        with self.store.session() as directory:
            box=SpreadsheetInbox(directory);before=box.get(payload['file_hash']);count=box.list()['count']
        with patch.object(handler,'clients_map',return_value=CLIENTS),patch.object(handler,'can_hash',revoke_after_parent):
            status,result=self.request('/api/spreadsheets/apply-fixes',payload,headers)
        self.assertEqual(status,404,result);self.assertNotIn('item',result)
        with self.store.session() as directory:
            box=SpreadsheetInbox(directory);self.assertEqual(before,box.get(payload['file_hash']));self.assertEqual(count,box.list()['count']);self.assertEqual(access.get_user(box,member['id'])['clients'],[])
    def test_actual_session_revocation_after_commit_is_saved_without_metadata(self):
        from hosting import access
        from test_access import CLIENTS
        member,headers,payload=self.session_fixture()
        def revoke_in_postcheck(h,file_hash):
            with h.store().session() as directory:
                box=SpreadsheetInbox(directory);access.save_user(box,dict(member,clients=[]));return box.get(file_hash)
        with patch.object(handler,'clients_map',return_value=CLIENTS),patch.object(handler,'baseline_check',revoke_in_postcheck):
            status,result=self.request('/api/spreadsheets/apply-fixes',payload,headers)
        self.assertEqual(status,403,result);self.assertTrue(result['save_committed']);self.assertNotIn('item',result)
        with self.store.session() as directory:self.assertEqual(len(SpreadsheetInbox(directory).get(payload['file_hash'])['fixes_applied']),1)
    def test_actual_session_revocation_before_reuse_disclosure_is_opaque(self):
        from hosting import access
        from test_access import CLIENTS
        member,headers,payload=self.session_fixture()
        with self.store.session() as directory:
            box=SpreadsheetInbox(directory);target=box.apply_fixes(payload['file_hash'],payload['changes'],expected_check_hash=payload['expected_check_hash']);payload['expected_check_hash']=box.get(payload['file_hash'])['check_hash'];before={h:box.get(h) for h in (payload['file_hash'],target['file_hash'])}
        original=SpreadsheetInbox.apply_fixes
        def revoke_after_reuse(box,*args,**kwargs):
            result=original(box,*args,**kwargs);self.assertTrue(result['already_applied']);access.save_user(box,dict(member,clients=[]));return result
        with patch.object(handler,'clients_map',return_value=CLIENTS),patch.object(SpreadsheetInbox,'apply_fixes',revoke_after_reuse),patch.object(handler,'baseline_check') as baseline:
            status,result=self.request('/api/spreadsheets/apply-fixes',payload,headers)
        self.assertEqual(status,404,result);self.assertEqual(result,dict(error='Report not found.',fail_closed=True));baseline.assert_not_called()
        with self.store.session() as directory:
            box=SpreadsheetInbox(directory);self.assertEqual(before,{h:box.get(h) for h in before})

    def test_parent_revocation_denies_even_when_existing_destination_remains_allowed(self):
        from hosting import access
        from test_access import CLIENTS
        member,headers,_=self.session_fixture()
        with self.store.session() as directory:
            box=SpreadsheetInbox(directory);member=access.save_user(box,dict(member,clients=['Owens','Lear']))
            box.set_setting('assignment|'+self.a,dict(client_key='Owens',job='source'))
            box.set_setting('assignment|'+self.c,dict(client_key='Lear',job='target'))
            before={h:box.get(h) for h in (self.a,self.b,self.c)}
        original=handler.can_hash;revoked=[]
        def revoke_parent_only(h,file_hash):
            allowed=original(h,file_hash)
            if allowed and not revoked:
                with h.store().session() as directory:access.save_user(SpreadsheetInbox(directory),dict(member,clients=['Lear']))
                revoked.append(True)
            return allowed
        with patch.object(handler,'clients_map',return_value=CLIENTS),patch.object(handler,'can_hash',revoke_parent_only):
            status,result=self.request('/api/spreadsheets/apply-fixes',self.payload(),headers)
        self.assertEqual(status,404,result)
        with patch.object(handler,'clients_map',return_value=CLIENTS):self.assertEqual(self.request('/api/spreadsheet?id='+self.c,extra_headers=headers)[0],200)
        with self.store.session() as directory:
            box=SpreadsheetInbox(directory);self.assertEqual(before,{h:box.get(h) for h in before})

if __name__ == '__main__': unittest.main()
