"""Local-only proposal preparation: no model/provider is contacted."""
import ast
import copy
from pathlib import Path
import pytest
from hosting.spreadsheet_inbox import SpreadsheetInbox
from hosting.review_summary import check_run

@pytest.fixture
def report(tmp_path):
    box=SpreadsheetInbox(tmp_path)
    item=box.ingest('synthetic.csv',b'Part,Inspected,Rejected\nSYN-1,10,1\n')
    return box,item

@pytest.mark.parametrize('condition',['missing','stale','old-checker','approved','sent','superseded'])
def test_preparation_refuses_invalid_state_before_model(report,monkeypatch,condition):
    box,item=report
    hash_=item['check_hash']
    if condition=='missing': hash_=None
    if condition=='stale': hash_='old'
    changes={'old-checker':{'checker_version':'old'},'approved':{'approval':{'by':'Synthetic'}},'sent':{'sent':[{'sent':True}]},'superseded':{'fixes_applied':[{'fixed_file_hash':'new'}]}}
    if condition in changes:
        item=box._update(item['file_hash'],lambda r:r.update(changes[condition]));hash_=item['check_hash']
    before=box.get(item['file_hash'])
    def forbidden(*a,**kw): raise AssertionError('Model must not be called')
    monkeypatch.setattr('hosting.spreadsheet_inbox.make_ai_fixes',forbidden)
    with pytest.raises(ValueError): box.ai_fixes(item['file_hash'],expected_check_hash=hash_)
    assert box.get(item['file_hash'])==before


def test_async_result_is_discarded_if_report_changes(report,monkeypatch):
    box,item=report
    intervening=[]
    def fake_model(*a,**kw):
        box._update(item['file_hash'],lambda r:r.update(review_request={'status':'waiting'}))
        intervening.append(box.get(item['file_hash']))
        return {'status':'done','fixes':[]}
    monkeypatch.setattr('hosting.spreadsheet_inbox.make_ai_fixes',fake_model)
    with pytest.raises(ValueError,match='changed'):
        box.ai_fixes(item['file_hash'],expected_check_hash=item['check_hash'])
    assert box.get(item['file_hash'])==intervening[0]
    assert not box.get(item['file_hash']).get('ai_fixes')


def test_prepared_suggestions_never_change_workbook_or_create_child(report,monkeypatch):
    box,item=report; original=box.original_bytes(item['file_hash'])[1]
    monkeypatch.setattr('hosting.spreadsheet_inbox.make_ai_fixes',lambda *a,**kw: {'status':'done','fixes':[{'id':'synthetic','sheet':'CSV','cell':'A2','before':'SYN-1','after':'SYN-2'}]})
    result=box.ai_fixes(item['file_hash'],expected_check_hash=item['check_hash'])
    assert result['run']==check_run(item)
    assert box.original_bytes(item['file_hash'])[1]==original
    assert box.list()['count']==1
    assert not box.get(item['file_hash']).get('fixes_applied')
    assert box.get(item['file_hash'])['check_hash']!=item['check_hash']


def test_http_route_checks_access_before_loading_or_preparing():
    source=Path('api/index.py').read_text()
    route=source[source.index("            if route == '/api/spreadsheets/ai-fixes':"):source.index("            if route in ('/api/settings'")]
    assert route.index('self.can_hash(file_hash)') < route.index('.proposal_inputs(') < route.index('record = make_ai_fixes(')
    assert 'expected_check_hash=expected_check_hash' in route
    assert "'report': view" in route


def test_http_cross_report_access_never_calls_model_or_saves(report,monkeypatch):
    import json, threading, urllib.request, urllib.error
    from http.server import ThreadingHTTPServer
    from api.index import handler
    from tests.test_focus_desk_audit import _Store
    box,item=report
    class Restricted(handler):
        def can_hash(self, file_hash): return False
        def log_message(self,*args): pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Restricted)
    server.workspace_store=_Store(box.root)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    base=f'http://127.0.0.1:{server.server_port}'
    called=[]
    monkeypatch.setattr('hosting.spreadsheet_inbox.make_ai_fixes',lambda *a,**kw: called.append(True))
    before=box.get(item['file_hash'])
    try:
        request=urllib.request.Request(base+'/api/spreadsheets/ai-fixes',data=json.dumps({'file_hash':item['file_hash'],'expected_check_hash':item['check_hash']}).encode(),headers={'Content-Type':'application/json','Origin':base,'X-Review-Token':'same-origin'})
        with pytest.raises(urllib.error.HTTPError) as denied: urllib.request.urlopen(request,timeout=5)
        assert denied.value.code==404
        denied.value.close()
        assert called==[] and box.get(item['file_hash'])==before
    finally:
        server.shutdown();server.server_close();thread.join()
