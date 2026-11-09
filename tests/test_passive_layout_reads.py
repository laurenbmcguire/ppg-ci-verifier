"""Configured AI never makes passive report reads contact a provider. Synthetic data only."""
import copy
import json
import threading
import urllib.request
from urllib.parse import urlsplit
from http.server import ThreadingHTTPServer
import pytest
from hosting import ai_layout, ai_review
from hosting.spreadsheet_inbox import SpreadsheetInbox
from hosting.spreadsheets import report_records, report_and_export_records

@pytest.fixture
def passive(tmp_path,monkeypatch):
    monkeypatch.delenv('ANTHROPIC_API_KEY',raising=False)
    monkeypatch.delenv('AI_LAYOUT',raising=False)
    monkeypatch.setenv('CI_LOCAL_DEV','1');monkeypatch.setenv('VERCEL','0')
    box=SpreadsheetInbox(tmp_path)
    raw=b'Part,Inspected,Rejected\nSYNTHETIC-A,10,\nSYNTHETIC-B,20,2\n'
    item=box.ingest('SYNTHETIC-passive.csv',raw)
    before=copy.deepcopy(box.get(item['file_hash']))
    calls=[];remote=[]
    def forbidden(*args,**kwargs):
        calls.append(True);raise AssertionError('Passive reads must not call the provider')
    monkeypatch.setenv('ANTHROPIC_API_KEY','synthetic-unused')
    ai_layout._CACHE.clear()
    monkeypatch.setattr(ai_review,'_post',forbidden)
    original=urllib.request.urlopen
    def local_only(request,*args,**kwargs):
        url=request.full_url if hasattr(request,'full_url') else request
        if urlsplit(url).hostname not in ('127.0.0.1','localhost','::1'):
            remote.append(urlsplit(url).hostname);raise AssertionError('External traffic is forbidden')
        return original(request,*args,**kwargs)
    monkeypatch.setattr(urllib.request,'urlopen',local_only)
    yield box,item,raw,before,calls,remote
    assert not remote

def test_record_extraction_deterministic_with_key_present_and_no_model_cache(passive):
    box,item,raw,before,calls,_=passive
    assert ai_layout.enabled() is True
    for _ in range(2):
        rows=report_records(item['filename'],raw)
        recs,exports=report_and_export_records(item['filename'],raw)
        assert len(rows)==len(recs)==2 and exports==[]
        assert rows[0]['values']['inspected']==10 and rows[0]['values']['rejected'] is None
    assert calls==[]
    assert box.get(item['file_hash'])==before and box.original_bytes(item['file_hash'])[1]==raw

def test_ambiguous_layout_stays_unverified_not_invented(passive):
    _,_,_,_,calls,_=passive
    raw=b'Unrecognized fields\nSYNTHETIC-X,maybe,a lot\n'
    assert report_records('SYNTHETIC-ambiguous.csv',raw)==[]
    assert report_and_export_records('SYNTHETIC-ambiguous.csv',raw)==([],[])
    assert calls==[]

def test_actual_handler_open_list_fixes_sheet_cells_make_zero_provider_calls(passive):
    from api.index import handler
    from tests.test_focus_desk_audit import _Store
    box,item,raw,before,calls,remote=passive
    class Synthetic(handler):
        def current_user(self):return {'role':'admin','id':'synthetic-admin'}
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Synthetic);server.workspace_store=_Store(box.root)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    base=f'http://127.0.0.1:{server.server_port}'
    def read(path,payload=None):
        request=urllib.request.Request(base+path,data=None if payload is None else json.dumps(payload).encode(),headers={'Content-Type':'application/json','Origin':base,'X-Review-Token':'same-origin'})
        with urllib.request.urlopen(request,timeout=5) as response:
            assert response.status==200;return json.load(response)
    try:
        for _ in range(2):
            assert read('/api/spreadsheets')['count']==1
            shown=read('/api/spreadsheet?id='+item['file_hash']);assert shown['check_hash']==item['check_hash']
            fixes=read('/api/spreadsheets/fixes',{'file_hash':item['file_hash']});assert fixes['file_hash']==item['file_hash']
            grid=read('/api/spreadsheets/sheet',{'file_hash':item['file_hash'],'sheet':'CSV'});assert grid['rows'][1][0]=='SYNTHETIC-A'
            read('/api/spreadsheets/cells',{'file_hash':item['file_hash'],'sheet':'CSV','row':2})
            read('/api/spreadsheets/recheck',{'file_hash':item['file_hash']})
        assert calls==[] and remote==[]
        assert box.get(item['file_hash'])==before and box.original_bytes(item['file_hash'])[1]==raw
        assert box.list()['count']==1
    finally:server.shutdown();server.server_close();thread.join()


def test_ingest_and_recheck_are_deterministic_with_ai_configured(passive):
    box,item,raw,before,calls,_=passive
    from tests.test_ai_layout import _xlsx,scan_list
    ambiguous=box.ingest('SYNTHETIC-unrecognized.xlsx',_xlsx(scan_list))
    assert ambiguous.get('layout_ai_requested') is False
    assert ambiguous['record_count']==0 and ambiguous['release_status']=='held'
    assert any('AI layout detection was not requested' in x for x in ambiguous['limitations'])
    proposal=box.propose_recheck(ambiguous['file_hash'])
    assert proposal is not None and calls==[]
    assert box.get(item['file_hash'])==before and box.original_bytes(item['file_hash'])[1]==raw


def test_historical_ai_layout_metadata_is_not_rewritten_on_reads(passive):
    box,item,raw,_,calls,_=passive
    box._update(item['file_hash'],lambda r:r.update(layout_source='ai',ai_layout={'model':'historical-synthetic','notes':'historical mapping'}))
    old=copy.deepcopy(box.get(item['file_hash']))
    box.fix_proposals(item['file_hash']);box.sheet_grid(item['file_hash'],'CSV')
    assert box.get(item['file_hash'])==old
    assert box.original_bytes(item['file_hash'])[1]==raw and calls==[]


def test_browser_upload_does_not_implicitly_prepare_ai(passive):
    import base64
    from api.index import handler
    from tests.test_focus_desk_audit import _Store
    box,item,raw,before,calls,_=passive
    class Synthetic(handler):
        def current_user(self):return {'role':'admin','id':'synthetic-admin'}
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Synthetic);server.workspace_store=_Store(box.root)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    base=f'http://127.0.0.1:{server.server_port}'
    try:
        upload={'filename':'SYNTHETIC-upload.csv','content_base64':base64.b64encode(b'Part,Inspected,Rejected\nSYN-NEW,3,0\n').decode()}
        request=urllib.request.Request(base+'/api/spreadsheets/import',data=json.dumps(upload).encode(),headers={'Content-Type':'application/json','Origin':base,'X-Review-Token':'same-origin'})
        with urllib.request.urlopen(request,timeout=5) as response:
            assert response.status==200;out=json.load(response)
        assert calls==[] and not out['item'].get('ai_review')
        assert box.get(item['file_hash'])==before and box.original_bytes(item['file_hash'])[1]==raw
    finally:server.shutdown();server.server_close();thread.join()

def test_public_api_upload_does_not_implicitly_prepare_ai(passive,monkeypatch):
    import base64
    from hosting import public_api
    from tests.test_focus_desk_audit import _Store
    box,item,raw,before,calls,_=passive
    class API:
        def store(self):return _Store(box.root)
        def sharepoint_check(self,h):return None
        def baseline_check(self,h):return None
        def ai_review(self,h):calls.append(True);raise AssertionError('Import must not request AI')
    monkeypatch.setattr('api.index.ai_review_configured',lambda:True)
    monkeypatch.setattr('api.index.sharepoint_configured',lambda:False)
    public_api.op_upload(API(),'SYNTHETIC-api-upload.csv',base64.b64encode(b'Part,Inspected,Rejected\nSYN-API,3,0\n').decode())
    checked=public_api.op_recheck(API(),item['file_hash'])
    assert checked['ran']==['baseline']
    assert calls==[] and box.get(item['file_hash'])==before
