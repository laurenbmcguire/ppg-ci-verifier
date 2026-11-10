"""Synthetic exact-source DAILY pilot; real outer SQLite store transactions."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime,timezone,timedelta
from io import BytesIO
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parent))
from source_first_fixture import capture,MAP,SCOPE,USER,date_contract
from hosting import source_first as sf,source_first_service as svc,source_first_workbook as wb,source_first_capture as source
from hosting.sqlite_storage import SQLiteWorkspace
from hosting.spreadsheet_inbox import SpreadsheetInbox
from hosting.storage import StorageUnavailable
from hosting.files import files_for


class Model(unittest.TestCase):
    def test_missing_null_blank_and_zero_are_distinct(self):
        self.assertEqual([sf.typed({'n':None,'b':'','z':0},x)['type'] for x in ('x','n','b','z')],['absent','null','text','number'])
        self.assertEqual(sf.quantity(sf.typed({'z':0},'z')),0)
        for x in ({'type':'absent'},{'type':'null'},{'type':'text','value':''},{'type':'boolean','value':True}):self.assertIsNone(sf.quantity(x))
    def test_strings_and_leading_zeros_survive(self):
        rows,_=sf.reviewed_rows(capture(),MAP,[])
        self.assertEqual(rows[0]['resolved']['identifier'],'0000000123')
        self.assertEqual(rows[0]['values']['shift'],{'type':'text','value':'1'})
    def test_invalid_quantities_block(self):
        for value in (None,'','1,000',True,-1,1.5):
            c=capture();c['records'][0]['raw']['Inspected']=value;c=sf.seal({k:v for k,v in c.items() if k not in ('snapshot_hash','envelope_hash')})
            _,issues=sf.reviewed_rows(c,MAP,[]);self.assertTrue(any(i['blocking'] for i in issues))
    def test_duplicate_rows_not_deduplicated(self):
        c=capture();r=deepcopy(c['records'][0]);r['id']='3';c=capture(c['records']+[r]);rows,issues=sf.reviewed_rows(c,MAP,[])
        self.assertEqual(len(rows),3);self.assertTrue(any(i['code']=='duplicate_records' for i in issues))
    def test_overlay_is_typed_hash_bound_and_raw_immutable(self):
        c=capture();before=deepcopy(c);o=sf.proposal(c,'1','comments',{'type':'text','value':'Reviewed note'},'Checked original inspection record.',USER);o['original']=sf.typed(c['records'][0]['raw'],'Comments')
        rows,_=sf.reviewed_rows(c,MAP,[o]);self.assertEqual(rows[0]['resolved']['comments'],'Reviewed note');self.assertEqual(c,before)
        c['records'][0]['version']='"1,2"';c=sf.seal({k:v for k,v in c.items() if k not in ('snapshot_hash','envelope_hash')})
        with self.assertRaises(sf.ReviewError):sf.reviewed_rows(c,MAP,[o])
    def test_virtual_identity_cannot_approve(self):
        with self.assertRaises(PermissionError):sf.actor(dict(USER,virtual=True))
    def test_unknown_dates_do_not_disappear(self):
        c=capture();c['uncertain_dates']=['300'];c=sf.seal({k:v for k,v in c.items() if k not in ('snapshot_hash','envelope_hash')})
        self.assertTrue(any(i['code']=='uncertain_coverage' for i in sf.reviewed_rows(c,MAP,[])[1]))
    def test_tampered_capture_is_rejected(self):
        c=capture();c['records'][0]['raw']['Inspected']=999
        with self.assertRaises(sf.ReviewError):sf.verify_seal(c)
    def test_terminal_claim_requires_receipts(self):
        c=capture();c['coverage']['pages'][-1]['terminal']=False
        with self.assertRaises(sf.ReviewError):sf.seal(c)
    def test_freshness_is_bounded(self):
        c=capture();r={'snapshot_hash':c['snapshot_hash'],'match':True,'compared_at':sf.now()}
        self.assertTrue(sf.fresh(r,c['snapshot_hash']));self.assertFalse(sf.fresh(r,c['snapshot_hash'],datetime.now(timezone.utc)+timedelta(seconds=601)))

    def test_native_depth_guard_defers_inner_commits_and_restores(self):
        from types import SimpleNamespace
        pg=SimpleNamespace(depth=0);box=SimpleNamespace(pg=pg)
        with svc.outer_commit_only(box):
            self.assertEqual(pg.depth,1)
            with svc.outer_commit_only(box):self.assertEqual(pg.depth,2)
            self.assertEqual(pg.depth,1)
        self.assertEqual(pg.depth,0)
        with self.assertRaises(RuntimeError):
            with svc.outer_commit_only(box):raise RuntimeError('outer abort')
        self.assertEqual(pg.depth,0)


class Capture(unittest.TestCase):
    def test_pages_retain_terminal_and_hash_evidence(self):
        calls=[]
        def get(url):
            calls.append(url);return 200,{'value':[{'id':str(len(calls))}],**({'@odata.nextLink':'/sites/a/lists/b/items?skip=2'} if len(calls)==1 else {})}
        pages=[];out=source.collection('/sites/a/lists/b/items?top=1','items',pages,get)
        self.assertEqual(len(out),2);self.assertFalse(pages[0]['terminal']);self.assertTrue(pages[1]['terminal']);self.assertTrue(pages[0]['page_hash'])
    def test_host_change_cursor_repeat_denial_and_cap_fail_closed(self):
        for payload,status in [({'value':[],'@odata.nextLink':'https://evil.invalid/v1.0/sites/a/lists/b/items'},200),({'value':[],'@odata.nextLink':'/sites/a/lists/b/items'},200),({},403),({'value':[],'@odata.nextLink':''},200)]:
            with self.subTest(payload=payload),self.assertRaises((sf.ReviewError,RuntimeError)):
                source.collection('/sites/a/lists/b/items','items',[],lambda u:(status,payload))
    def test_capture_preserves_raw_other_days_privately(self):
        c=capture();original=[dict(id=r['id'],eTag=r['version'],fields=r['raw'],lastModifiedDateTime=r['modified']) for r in c['records']]
        old=deepcopy(original[0]);old['id']='99';old['fields']['Date']='2026-11-09';original.append(old)
        def get(u):
            if '/columns?' in u:return 200,{'value':list(c['columns'].values())}
            if '/items?' in u:return 200,{'value':original}
            return 200,{'id':SCOPE['list_id'],'webUrl':'https://example.sharepoint.com/sites/A/Lists/B'}
        with patch('hosting.photo_embed._host',return_value='example.sharepoint.com'):
            out,blobs=source.capture(SCOPE,fetch=get,date_capture=date_contract)
        self.assertEqual(len(out['raw_all_records']),3);self.assertEqual(len(out['records']),2);self.assertEqual(out['raw_all_records'],original)

    def test_missing_expired_or_mismatched_date_contract_blocks_even_literals(self):
        c=capture()
        def get(u):
            if '/columns?' in u:return 200,{'value':list(c['columns'].values())}
            if '/items?' in u:self.fail('Do not read items without current exact date metadata')
            return 200,{'id':SCOPE['list_id']}
        with patch('hosting.photo_embed._host',return_value='example.sharepoint.com'):
            good=date_contract(SCOPE['site_id'],SCOPE['list_id'],c['columns'])
            for receipt in (None,dict(good,list_id='other'),dict(good,expires_at='2020-01-01T00:00:00+00:00')):
                with self.subTest(receipt=receipt),self.assertRaisesRegex(source.CaptureFailure,'regional-timezone evidence'):
                    source.capture(SCOPE,fetch=get,date_capture=lambda *a:receipt)


class Workbook(unittest.TestCase):
    def prepared(self,c=None,header=None):
        c=c or capture();rows,issues=sf.reviewed_rows(c,MAP,[]);h=wb.header(header or {},c);co=wb.contract();bind=sf.binding(c,MAP,[],h,co['clean_sha256'],sf.digest(co),wb.generator_hash());return c,rows,h,bind
    def test_saved_template_rows_dates_totals_ties_and_missing_labor(self):
        from openpyxl import load_workbook
        c,rows,h,b=self.prepared();raw,v=wb.build(rows,h,c,b,{})
        book=load_workbook(BytesIO(raw),data_only=True)
        self.assertEqual(book['Summary']['G24'].value,30);self.assertEqual(book['Summary']['N24'].value,'Unavailable')
        self.assertEqual(book['Summary']['C49'].value,'0000000123');self.assertEqual(book['Summary']['C50'].value,'0000000124')
        self.assertEqual(book['1st Shift']['E16'].value,'0000000123');self.assertEqual(book['2nd Shift']['B16'].value,'2nd')
        self.assertEqual(book['Summary']['D14'].value.date().isoformat(),'2026-11-10')
        self.assertEqual(v['record_count'],2);self.assertFalse(v['current_sharepoint_claim'])
    def test_true_zero_is_not_blank(self):
        from openpyxl import load_workbook
        c=capture();c['records'][0]['raw'].update(Rejected=0,Certified=10);c=capture(c['records']);c,r,h,b=self.prepared(c);raw,_=wb.build(r,h,c,b,{})
        self.assertEqual(load_workbook(BytesIO(raw),data_only=True)['1st Shift']['J16'].value,0)
    def test_scope_rows_expand_without_truncation(self):
        c=capture();rows=deepcopy(c['records'])
        for i in range(3,17):r=deepcopy(rows[0]);r['id']=str(i);r['raw']['Identifier']=str(i);rows.append(r)
        c,r,h,b=self.prepared(capture(rows));self.assertFalse(any(x['blocking'] for x in wb.scope_findings(r,h,c,wb.contract())))
        raw,verification=wb.build(r,h,c,b,{})
        self.assertEqual(verification['record_count'],len(rows))
    def test_original_photo_hash_and_exact_record_binding(self):
        from PIL import Image
        out=BytesIO();Image.new('RGB',(3,2),(180,0,30)).save(out,format='PNG');blob=out.getvalue();sha=sf.digest(blob)
        c=capture();c['photos']=[dict(record_id='2',record_hash=sf.digest(c['records'][1]),field='DefectPhoto',status='supported',sha256=sha,title='Defect',bytes=len(blob))];c=sf.seal({k:v for k,v in c.items() if k not in ('snapshot_hash','envelope_hash')})
        c,r,h,b=self.prepared(c,{'photo_choices':{'2':sha}});raw,v=wb.build(r,h,c,b,{sha:blob})
        self.assertEqual(v['photo_provenance'][0]['record_id'],'2');self.assertEqual(v['photo_provenance'][0]['sha256'],sha)
        with self.assertRaises(sf.ReviewError):wb.build(r,h,c,b,{sha:b'wrong'})
    def test_formula_text_input_is_not_executable(self):
        from openpyxl import load_workbook
        c=capture();c['records'][0]['raw']['Comments']='=HYPERLINK("https://evil.invalid","x")';c,r,h,b=self.prepared(capture(c['records']));raw,_=wb.build(r,h,c,b,{})
        self.assertEqual(load_workbook(BytesIO(raw),data_only=False)['1st Shift']['L16'].data_type,'s')


class FakeHandler:
    def __init__(self,store):self._store=store;self.user=deepcopy(USER)
    def store(self):return self._store
    def current_user(self):return self.user
    def refresh_session_principal(self,inbox):pass


class Durable(unittest.TestCase):
    def setUp(self):
        self.env=patch.dict(os.environ,{},clear=True);self.env.start();self.tmp=tempfile.TemporaryDirectory();self.store=SQLiteWorkspace(self.tmp.name);self.h=FakeHandler(self.store);self.counter=0
        self.bind=patch('hosting.public_api._build_binding',return_value={k:SCOPE[k] for k in ('site_id','list_id')});self.bind.start()
        with self.store.session() as directory:
            SpreadsheetInbox(directory).set_setting('job_registry',[dict(SCOPE,verified=True,client_key='Synthetic',client='Synthetic Client',site='SyntheticSite')])
    def tearDown(self):self.bind.stop();self.tmp.cleanup();self.env.stop()
    def rid(self):self.counter+=1;return 'test-request-'+str(self.counter).zfill(8)
    def new(self):return svc.capture(self.h,dict(scope=SCOPE,request_id=self.rid()),capture_fn=lambda s:(capture(),{}))
    def action(self,s,name,**extra):return svc.mutate(self.h,s['id'],name,dict(request_id=self.rid(),snapshot_hash=s['snapshot_hash'],binding_hash=s['binding_hash'],**extra))
    def approved(self):
        s=self.new();s=self.action(s,'mapping',mapping=MAP);s=self.action(s,'header',header={});return self.action(s,'approve',reviewed=True)
    def test_real_outer_store_restart_and_idempotent_generated_bytes(self):
        s=self.approved();payload=dict(request_id=self.rid(),binding_hash=s['binding_hash'])
        out=svc.generate(self.h,s['id'],payload);self.assertTrue(out['report']['verification']['matches_reviewed_snapshot'])
        new_h=FakeHandler(SQLiteWorkspace(self.tmp.name));again=svc.generate(new_h,s['id'],payload)
        self.assertEqual(again['report'],out['report'])
        with new_h.store().session() as directory:
            box=SpreadsheetInbox(directory);self.assertEqual(box.list()['count'],0)
            with box.connect() as db:saved=svc._get(db,out['id'])['report']
            self.assertEqual(sf.digest(files_for(directory).get(saved['artifact_path'])),saved['file_hash'])
    def test_pending_capture_not_repeated_after_uncertain_transport(self):
        payload=dict(scope=SCOPE,request_id=self.rid());calls=[]
        def fail(s):calls.append(s);raise OSError('uncertain provider')
        with self.assertRaises(OSError):svc.capture(self.h,payload,capture_fn=fail)
        out=svc.capture(self.h,payload,capture_fn=fail);self.assertEqual(len(calls),1);self.assertEqual(out['status'],'capture_pending')
    def test_unchanged_refresh_keeps_approval_changed_version_reopens(self):
        s=self.approved();approval=s['approval']
        def fresh(s):return capture(),{}
        s=svc.refresh(self.h,s['id'],dict(request_id=self.rid(),snapshot_hash=s['snapshot_hash']),capture_fn=fresh)
        self.assertEqual(s['approval'],approval);self.assertTrue(s['approved'])
        def changed(s):c=capture();c['records'][0]['version']='"1,2"';return capture(c['records']),{}
        s=svc.refresh(self.h,s['id'],dict(request_id=self.rid(),snapshot_hash=s['snapshot_hash']),capture_fn=changed)
        self.assertFalse(s['approved']);self.assertEqual(s['changes']['changed'],['1']);self.assertEqual(s['overlays'],[])
    def test_missing_values_cannot_be_acknowledged_clear(self):
        s=self.new();s=self.action(s,'mapping',mapping=MAP);s=self.action(s,'header',header={});s=self.action(s,'correct',record_id='1',field='rejected',replacement={'type':'null'},evidence='Original source value is unknown.')
        with self.assertRaises(sf.ReviewError):self.action(s,'approve',reviewed=True)
        self.assertTrue(any(i['blocking'] for i in s['findings']))
    def test_late_added_and_deleted_records_reopen(self):
        s=self.approved();c=capture();c['records']=c['records'][1:];new=deepcopy(c['records'][0]);new['id']='3';new['raw']['Identifier']='0003';c=capture(c['records']+[new])
        s=svc.refresh(self.h,s['id'],dict(request_id=self.rid(),snapshot_hash=s['snapshot_hash']),capture_fn=lambda _:(c,{}))
        self.assertEqual(s['changes']['deleted'],['1']);self.assertEqual(s['changes']['added'],['3']);self.assertFalse(s['approved'])
    def test_stale_binding_and_reused_request_id_fail(self):
        s=self.approved();req=self.rid();p=dict(request_id=req,snapshot_hash=s['snapshot_hash'],binding_hash=s['binding_hash'],header={})
        svc.mutate(self.h,s['id'],'header',p)
        with self.assertRaises(sf.ReviewError):svc.mutate(self.h,s['id'],'header',dict(p,header={'area':'Different'}))
    def test_outer_store_failure_never_reports_success(self):
        s=self.approved();real=self.h._store
        class Failing:
            @contextmanager
            def session(self):
                with real.session() as directory:
                    yield directory
                    raise StorageUnavailable('synthetic outer rollback')
        self.h._store=Failing()
        with self.assertRaises(StorageUnavailable):svc.generate(self.h,s['id'],dict(request_id=self.rid(),binding_hash=s['binding_hash']))
        self.h._store=real
        self.assertIsNone(svc.view(self.h,s['id'])['report'])
        with real.session() as d:self.assertEqual(SpreadsheetInbox(d).list()['count'],0)
    def test_view_never_returns_other_day_raw_records(self):
        s=self.new();self.assertNotIn('raw_all_records',json.dumps(s));self.assertNotIn('snapshot_path',s)



class HTTP(unittest.TestCase):
    def setUp(self):
        from http.server import ThreadingHTTPServer
        import threading
        from api.index import handler
        from hosting import access
        self.env=patch.dict(os.environ,{'CI_LOCAL_DEV':'1'},clear=True);self.env.start()
        self.tmp=tempfile.TemporaryDirectory();self.store=SQLiteWorkspace(self.tmp.name)
        with self.store.session() as d:
            box=SpreadsheetInbox(d);u=access.save_user(box,dict(email='reviewer@example.invalid',role='admin'))
            self.cookie='ppg_session='+access.start_session(box,u)
            box.set_setting('job_registry',[dict(SCOPE,verified=True,client_key='Synthetic',client='Synthetic Client',site='SyntheticSite')])
        self.server=ThreadingHTTPServer(('127.0.0.1',0),handler);self.server.workspace_store=self.store
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start();self.base='http://127.0.0.1:'+str(self.server.server_port)
        self.source=patch('hosting.source_first_capture.capture',return_value=(capture(),{}));self.source.start()
    def tearDown(self):self.server.shutdown();self.server.server_close();self.source.stop();self.tmp.cleanup();self.env.stop()
    def call(self,path,payload=None,cookie=True,csrf=True):
        import urllib.request,urllib.error
        headers={'Origin':self.base,'Content-Type':'application/json'}
        if cookie:headers['Cookie']=self.cookie
        if csrf:headers['X-Review-Token']='same-origin'
        req=urllib.request.Request(self.base+path,data=json.dumps(payload).encode() if payload is not None else None,headers=headers)
        try:r=urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req,timeout=15)
        except urllib.error.HTTPError as ex:r=ex
        with r:
            raw=r.read()
            return r.status,json.loads(raw) if r.headers.get('Content-Type','').startswith('application/json') else raw
    def test_auth_csrf_real_capture_and_route_static(self):
        self.assertEqual(self.call('/source-review')[0],200)
        self.assertIn(self.call('/api/source-review',cookie=False)[0],(401,403))
        p=dict(scope=SCOPE,request_id='http-capture-request-001')
        self.assertEqual(self.call('/api/source-review/capture',p,csrf=False)[0],403)
        status,s=self.call('/api/source-review/capture',p);self.assertEqual(status,200,s);self.assertEqual(s['status'],'source_review')
        self.assertEqual(self.call('/api/source-review?id='+s['id'])[0],200)
    def test_revoked_cookie_and_api_key_cannot_approve(self):
        from hosting import access
        with self.store.session() as d:
            box=SpreadsheetInbox(d)
            with box.connect() as db:db.execute('DELETE FROM sessions')
        self.assertIn(self.call('/api/source-review')[0],(401,403))

class AdditionalBoundaries(unittest.TestCase):
    def test_exact_fresh_boundary_future_and_missing_version(self):
        c=capture();t=datetime.now(timezone.utc);r=dict(snapshot_hash=c['snapshot_hash'],match=True,compared_at=t.isoformat())
        self.assertFalse(sf.fresh(r,c['snapshot_hash'],t+timedelta(seconds=600)));self.assertFalse(sf.fresh(r,c['snapshot_hash'],t-timedelta(seconds=1)))
        c['records'][0]['version']=None;c=capture(c['records']);self.assertTrue(any(i['code']=='missing_version' for i in sf.reviewed_rows(c,MAP,[])[1]))
    def test_optional_structured_value_has_clear_blocker(self):
        c=capture();c['records'][0]['raw']['Comments']={'unexpected':'value'};c=capture(c['records']);rows,issues=sf.reviewed_rows(c,MAP,[])
        self.assertTrue(any(i['code']=='unsupported_value_type' for i in issues));self.assertIsNone(rows[0]['resolved']['comments'])
    def test_photo_transport_metadata_is_not_source_change(self):
        base={'id':'1','eTag':'1','fields':{'Photo':'a'},'createdDateTime':'x'}
        self.assertEqual(source.substantive(base),source.substantive(dict(base,**{'@odata.context':'single item response metadata'})))
    def test_retry_after_is_not_clipped_and_failed_pages_remain_private(self):
        before=datetime.now(timezone.utc);hold=source.cooldown({'Retry-After':'3600'});due=datetime.fromisoformat(hold['not_before'])
        self.assertGreater((due-before).total_seconds(),3599)
        pages=[]
        with self.assertRaises(source.CaptureFailure) as cm:source.collection('/sites/a/lists/b/items','items',pages,lambda u:(429,{}, {'Retry-After':'3600'}))
        self.assertEqual(cm.exception.evidence['raw_pages'][0]['status'],429);self.assertFalse(cm.exception.evidence['items_terminal'])


class ConcurrentDurable(Durable):
    def test_competing_edits_cannot_silently_accept_a_stale_binding(self):
        from concurrent.futures import ThreadPoolExecutor
        s=self.approved();requests=[dict(request_id='parallel-correction-'+str(i),snapshot_hash=s['snapshot_hash'],binding_hash=s['binding_hash'],record_id='1',field='comments',replacement={'type':'text','value':'Reviewed '+str(i)},evidence='Checked source evidence.') for i in range(2)]
        def work(p):
            try:return svc.mutate(FakeHandler(SQLiteWorkspace(self.tmp.name)),s['id'],'correct',p)
            except sf.ReviewError as exc:return str(exc)
        with ThreadPoolExecutor(2) as pool:out=list(pool.map(work,requests))
        self.assertEqual(sum(isinstance(x,dict) for x in out),1)
        latest=svc.view(self.h,s['id']);self.assertEqual(len(latest['overlays']),1);self.assertFalse(latest['approved'])
    def test_failed_capture_receipt_persists_and_blocks_early_retry(self):
        due=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()
        def fail(s):raise source.CaptureFailure('Cooldown',dict(status=429,remaining_cursor='/exact/list/items?next=2',pages=[],raw_pages=[{'private':'source values'}],cooldown={'not_before':due,'known':True,'retry_after':'3600'}))
        s=svc.capture(self.h,dict(scope=SCOPE,request_id=self.rid()),capture_fn=fail)
        self.assertEqual(s['status'],'capture_failed');self.assertEqual(s['failure_receipt']['status'],429);self.assertNotIn('private_evidence_path',s['failure_receipt'])
        with self.assertRaisesRegex(sf.ReviewError,'cooldown'):self.new()

class PhotoBudget(unittest.TestCase):
    def run_capture(self, count=13, status=200, cooldown_call=None):
        from PIL import Image
        b=BytesIO();Image.new('RGB',(2,2),(40,50,60)).save(b,format='PNG');blob=b.getvalue()
        c=capture();raw=[]
        for i in range(1,count+1):
            fields=deepcopy(c['records'][0]['raw']);fields['Photo']=json.dumps({'fileName':'same.png','type':'thumbnail'});fields['Identifier']=str(i)
            raw.append(dict(id=str(i),eTag='v1',fields=fields))
        c['columns']['Photo']={'name':'Photo','thumbnail':{}}
        reads=[];photos=[]
        def get(u):
            if '/columns?' in u:return 200,{'value':list(c['columns'].values())}
            if '/items?' in u:return 200,{'value':raw}
            if '/items/' in u:
                reads.append(u)
                if len(reads)==cooldown_call:return 429,{}, {'Retry-After':'7200'}
                ident=u.split('/items/')[1].split('?')[0]
                return 200,dict(next(r for r in raw if r['id']==ident),**{'@odata.context':'transport only'})
            return 200,{'id':SCOPE['list_id'],'webUrl':'https://example.sharepoint.com/sites/A/Lists/B'}
        def photo(u):photos.append(u);return status,blob if status==200 else b''
        with patch('hosting.photo_embed._host',return_value='example.sharepoint.com'):
            out,blobs=source.capture(SCOPE,fetch=get,photo_fetch=photo,date_capture=date_contract)
        return out,blobs,reads,photos
    def test_duplicate_photo_bytes_consume_each_attempt(self):
        out,blobs,reads,photos=self.run_capture()
        self.assertEqual(len(photos),12);self.assertEqual(len(reads),24);self.assertEqual(len(blobs),1)
        self.assertEqual(out['coverage']['photo_attempts'],12);self.assertIn('not requested',out['photos'][-1]['reason'])
    def test_unavailable_photos_also_consume_attempts(self):
        out,blobs,reads,photos=self.run_capture(status=404)
        self.assertEqual(len(photos),12);self.assertFalse(blobs);self.assertEqual(len(reads),24)
    def test_photo_before_and_after_item_cooldown_keeps_headers(self):
        for call in (1,2):
            with self.subTest(call=call),self.assertRaises(source.CaptureFailure) as cm:self.run_capture(count=1,cooldown_call=call)
            evidence=cm.exception.evidence
            self.assertEqual(evidence['status'],429);self.assertEqual(evidence['cooldown']['retry_after'],'7200')
            self.assertIn('/items/1?',evidence['remaining_cursor']);self.assertTrue(evidence['raw_pages'])

class PrivateArtifact(Durable):
    def test_pending_checkpoint_commits_before_any_provider_read(self):
        req=self.rid();key=sf.digest({'capture_request_id':req})
        def observe(scope):
            with self.store.session() as d:
                with SpreadsheetInbox(d).connect() as db:pending=svc._get(db,key)
            self.assertEqual(pending['status'],'capture_pending')
            self.assertEqual(pending['scope'],SCOPE)
            return capture(),{}
        with (patch('hosting.public_api._build_binding',side_effect=AssertionError('No provider-capable legacy binding')),
              patch('hosting.sharepoint.assignment_identity',side_effect=AssertionError('No external site-name resolution'))):
            s=svc.capture(self.h,dict(scope=SCOPE,request_id=req),capture_fn=observe)
        self.assertEqual(s['status'],'source_review')
    def test_report_current_requires_comparison_after_saved_workbook(self):
        s=self.approved();s=svc.generate(self.h,s['id'],dict(request_id=self.rid(),binding_hash=s['binding_hash']))
        self.assertTrue(s['current_sharepoint']);self.assertFalse(s['report']['current_sharepoint'])
        self.assertEqual(s['verification_state'],'matches_reviewed_snapshot')
        s=svc.refresh(self.h,s['id'],dict(request_id=self.rid(),snapshot_hash=s['snapshot_hash']),capture_fn=lambda _:(capture(),{}))
        self.assertTrue(s['report']['current_sharepoint']);self.assertEqual(s['verification_state'],'matches_current_sharepoint')
    def test_refresh_started_before_generation_cannot_be_final_comparison(self):
        s=self.approved()
        def during(_):
            observed=capture()
            svc.generate(self.h,s['id'],dict(request_id=self.rid(),binding_hash=s['binding_hash']))
            observed['captured_at']=sf.now()
            return sf.seal({k:v for k,v in observed.items() if k not in ('snapshot_hash','envelope_hash')}),{}
        s=svc.refresh(self.h,s['id'],dict(request_id=self.rid(),snapshot_hash=s['snapshot_hash']),capture_fn=during)
        self.assertFalse(s['report']['current_sharepoint']);self.assertEqual(s['verification_state'],'matches_reviewed_snapshot')
    def test_changed_source_resets_header_photo_review(self):
        s=self.approved();c=capture();c['records'][0]['version']='v2';c=capture(c['records'])
        s=svc.refresh(self.h,s['id'],dict(request_id=self.rid(),snapshot_hash=s['snapshot_hash']),capture_fn=lambda _:(c,{}))
        self.assertEqual(s['header'],{});self.assertTrue(any(f['code']=='header_review_required' for f in s['findings']))
    def test_private_report_never_enters_ordinary_inbox_or_aggregates(self):
        from datetime import date
        s=self.approved();s=svc.generate(self.h,s['id'],dict(request_id=self.rid(),binding_hash=s['binding_hash']))
        self.assertEqual(s['report']['assignment']['client_key'],'Synthetic');self.assertNotIn('artifact_path',s['report'])
        with self.store.session() as d:
            box=SpreadsheetInbox(d)
            self.assertEqual(box.list()['count'],0);self.assertEqual(box.all_results(),[]);self.assertEqual(box.jobs(),[])
            self.assertEqual(box.reports_for(SCOPE['job'],date(2026,11,1),date(2026,11,30)),{})
            self.assertEqual(box.find_vin('0000000123')['results'],[])
            with self.assertRaises(ValueError):box.get(s['report']['file_hash'])
    def test_registry_choices_use_exact_persisted_identity_only(self):
        with self.store.session() as d:
            box=SpreadsheetInbox(d);choices=svc.source_choices(box)
            self.assertEqual(len(choices),1);self.assertEqual(choices[0]['site_id'],SCOPE['site_id'])
            box.set_setting('job_registry',[])
            self.assertEqual(svc.source_choices(box),[])

class PrivateHTTP(HTTP):
    def build(self):
        status,s=self.call('/api/source-review/capture',dict(scope=SCOPE,request_id='private-http-capture-001'));self.assertEqual(status,200,s)
        for action,extra in [('mapping',{'mapping':MAP}),('header',{'header':{}}),('approve',{'reviewed':True}),('generate',{})]:
            status,s=self.call('/api/source-review/'+action,dict(id=s['id'],snapshot_hash=s['snapshot_hash'],binding_hash=s['binding_hash'],request_id='private-http-'+action+'-001',**extra));self.assertEqual(status,200,s)
        return s
    def test_dedicated_admin_download_only_and_no_ordinary_access(self):
        from hosting import access
        s=self.build();h=s['report']['file_hash'];url='/api/source-review/download?id='+s['id']+'&sha256='+h
        code,raw=self.call(url);self.assertEqual(code,200);self.assertEqual(sf.digest(raw),h)
        self.assertEqual(self.call('/api/spreadsheets')[1]['count'],0)
        for path in ('/api/spreadsheet?id=','/api/spreadsheets/download?id='):
            self.assertNotEqual(self.call(path+h)[0],200)
        with self.store.session() as d:
            box=SpreadsheetInbox(d);u=access.save_user(box,dict(email='external@example.invalid',role='member',clients=['Synthetic']))
            self.cookie='ppg_session='+access.start_session(box,u)
        self.assertEqual(self.call(url)[0],403)
        self.assertEqual(self.call('/api/source-review?id='+s['id'])[0],403)
        self.assertEqual(self.call('/api/spreadsheets')[1]['count'],0)
        for path in ('/api/spreadsheet?id=','/api/spreadsheets/download?id='):
            self.assertNotEqual(self.call(path+h)[0],200)
    def test_saved_bytes_tamper_is_blocked_and_identity_not_reused(self):
        s=self.build();url='/api/source-review/download?id='+s['id']+'&sha256='+s['report']['file_hash']
        with self.store.session() as d:
            with SpreadsheetInbox(d).connect() as db:state=svc._get(db,s['id'])
            with self.assertRaisesRegex(ValueError,'differs'):files_for(d).put(state['report']['artifact_path'],b'changed bytes')
        with patch('hosting.files.LocalFiles.get',return_value=b'changed bytes'):
            self.assertEqual(self.call(url)[0],409)

if __name__=='__main__':unittest.main()
