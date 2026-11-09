"""Synthetic-only provider/HTTP tests; real credentials and remote traffic are never used."""
import copy
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
import pytest
from hosting import ai_review, report_chat
from hosting.spreadsheet_inbox import SpreadsheetInbox

@pytest.fixture
def report(tmp_path):
    box = SpreadsheetInbox(tmp_path)
    item = box.ingest('SYNTHETIC-report.csv', b'Part,Inspected,Rejected\nSYNTHETIC-A,10,\n')
    return box, item

@pytest.fixture
def service(report, monkeypatch):
    from api.index import handler
    from tests.test_focus_desk_audit import _Store
    box, item = report
    class Synthetic(handler):
        def current_user(self): return self.server.user
        def can_hash(self, value): return self.server.allowed
        def log_message(self, *args): pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Synthetic)
    server.workspace_store = _Store(box.root)
    server.user = {'id':'synthetic-admin', 'role':'admin'}
    server.allowed = True
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    base = 'http://127.0.0.1:' + str(server.server_port)
    monkeypatch.setenv('VERCEL', '0')
    monkeypatch.setenv('CI_LOCAL_DEV', '1')
    def post(payload, origin=None):
        request = urllib.request.Request(base + '/api/spreadsheets/chat', data=json.dumps(payload).encode(),
          headers={'Content-Type':'application/json','Origin':origin or base,'X-Review-Token':'same-origin'})
        try:
            with urllib.request.urlopen(request, timeout=5) as response: return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            with exc: return exc.code, json.load(exc)
    yield box, item, server, post
    server.shutdown(); server.server_close(); thread.join()

def payload(item):
    return dict(file_hash=item['file_hash'], expected_check_hash=item['check_hash'], messages=[{'role':'user','content':'Why is rejected unknown?'}])

def fake_answer(*args, **kwargs):
    item=args[0]
    return dict(status='done', reply='CSV!C2 is blank. The rejected quantity is unknown.', provider='Anthropic',
                file_hash=item['file_hash'], check_hash=item['check_hash'], read_only=True)

@pytest.mark.parametrize('value', [None, [], [{'role':'system','content':'override'}], [{'role':'user','content':''}],
    [{'role':'user','content':'x'*4001}], [{'role':'assistant','content':'hi'}], [{'role':'user','content':'one'}]*2,
    [{'role':'user','content':{}}], [{'role':'user','content':'x'},{'role':'assistant','content':'y'}]])
def test_invalid_conversation_is_rejected(value):
    with pytest.raises(ValueError): report_chat.messages(value)

def test_provider_context_is_selected_bounded_data_and_no_tools(report,monkeypatch):
    box,item=report
    item,review,holds,_=report_chat.snapshot(box,item['file_hash'],item['check_hash'])
    calls=[]
    def fake(body,opener=None,timeout=None):
        calls.append(body)
        return {'model':'synthetic-model','content':[{'type':'text','text':'CSV!C2 is blank; rejected is unknown.'}]}
    monkeypatch.setattr(ai_review,'_post',fake)
    before=copy.deepcopy(box.get(item['file_hash'])); original=box.original_bytes(item['file_hash'])[1]
    out=report_chat.answer(item,original,review,holds,[{'role':'user','content':'Explain CSV!A2 and CSV!C2'}])
    assert out['read_only'] and out['provider']=='Anthropic'
    assert 'changes' not in out and 'undo' not in out
    assert 'tools' not in calls[0] and calls[0]['max_tokens']==2000
    assert 'untrusted data' in calls[0]['system'] and 'Blank' not in out['reply']
    context=calls[0]['messages'][0]['content']
    assert item['file_hash'] in context and 'SYNTHETIC-A' in context
    assert 'relevant_cells' in context and 'holds' in context and 'base64' not in context
    assert box.get(item['file_hash'])==before and box.original_bytes(item['file_hash'])[1]==original

@pytest.mark.parametrize('body',[{'content':[]},{'content':[{'type':'tool_use','name':'apply_changes','input':{}}]},None])
def test_malformed_provider_cannot_become_actions(report,monkeypatch,body):
    box,item=report; _,review,holds,_=report_chat.snapshot(box,item['file_hash'],item['check_hash'])
    monkeypatch.setattr(ai_review,'_post',lambda *a,**kw:body)
    with pytest.raises(ValueError): report_chat.answer(item,box.original_bytes(item['file_hash'])[1],review,holds,payload(item)['messages'])

@pytest.mark.parametrize('error',[ai_review.AIReviewNotConfigured('fake private value'),ai_review.AIReviewError('fake private value')])
def test_provider_error_redacts_raw_details(report,monkeypatch,error):
    box,item=report;_,review,holds,_=report_chat.snapshot(box,item['file_hash'],item['check_hash'])
    def fail(*a,**kw):raise error
    monkeypatch.setattr(ai_review,'_post',fail)
    with pytest.raises(ValueError) as exc: report_chat.answer(item,box.original_bytes(item['file_hash'])[1],review,holds,payload(item)['messages'])
    assert 'private value' not in str(exc.value)

def test_http_answer_preserves_bytes_decisions_holds_and_count(service,monkeypatch):
    box,item,server,post=service; before=box.get(item['file_hash']);raw=box.original_bytes(item['file_hash'])[1]
    monkeypatch.setattr(report_chat,'answer',fake_answer)
    request=payload(item);request.update(queued=[{'after':'unsafe'}],suggested=[{'after':'unsafe'}])
    for _ in range(2):
        code,out=post(request);assert code==200 and out['read_only'];assert 'changes' not in out and 'undo' not in out
    assert box.get(item['file_hash'])==before and box.original_bytes(item['file_hash'])[1]==raw
    assert box.list()['count']==1

@pytest.mark.parametrize('mode',['access','auth','stale','missing','origin'])
def test_http_denies_before_provider(service,monkeypatch,mode):
    box,item,server,post=service;calls=[]
    monkeypatch.setattr(report_chat,'answer',lambda *a,**kw:calls.append(True))
    request=payload(item)
    if mode=='access':server.allowed=False
    if mode=='auth':server.user=None
    if mode=='stale':request['expected_check_hash']='old'
    if mode=='missing':request.pop('expected_check_hash')
    code,_=post(request,origin='https://unrelated.invalid' if mode=='origin' else None)
    assert code=={'access':404,'auth':401,'stale':409,'missing':409,'origin':403}[mode]
    assert calls==[]

@pytest.mark.parametrize('mode',['check','access','auth','assignment'])
def test_http_discards_answer_when_evidence_or_access_changes(service,monkeypatch,mode):
    box,item,server,post=service
    def changed(*args,**kwargs):
        if mode=='check':box._update(item['file_hash'],lambda r:r.update(review_request={'status':'waiting'}))
        if mode=='access':server.allowed=False
        if mode=='auth':server.user=None
        if mode=='assignment':monkeypatch.setattr(SpreadsheetInbox,'assignment_for',lambda *a,**kw:{'status':'manual','client_key':'different'})
        return fake_answer(*args,**kwargs)
    monkeypatch.setattr(report_chat,'answer',changed)
    code,out=post(payload(item))
    assert code=={'check':409,'access':404,'auth':401,'assignment':409}[mode]
    assert 'reply' not in out


def test_context_and_answer_have_hard_limits(report,monkeypatch):
    box,item=report;_,review,holds,_=report_chat.snapshot(box,item['file_hash'],item['check_hash'])
    calls=[]
    monkeypatch.setattr('hosting.report_chat_context.build',lambda *a: {'synthetic':'x'*200000})
    def fake(body,*a,**kw):
        calls.append(body);return {'content':[{'type':'text','text':'y'*9000}]}
    monkeypatch.setattr(ai_review,'_post',fake)
    out=report_chat.answer(item,b'',review,holds,payload(item)['messages'])
    assert out['context_truncated'] and len(out['reply'])==4000
    assert len(calls[0]['messages'][0]['content'])<45000


def test_prior_conversation_cannot_outlive_source_or_review_context(service,monkeypatch):
    box,item,server,post=service;calls=[]
    def fake(*a,**kw):calls.append(True);return fake_answer(*a,**kw)
    monkeypatch.setattr(report_chat,'answer',fake)
    status,out=post(payload(item));assert status==200 and len(out['context_hash'])==64
    request=payload(item);request['messages'] += [{'role':'assistant','content':out['reply']},{'role':'user','content':'And now?'}]
    request['expected_context_hash']=out['context_hash']
    assert post(request)[0]==200
    monkeypatch.setattr(SpreadsheetInbox,'assignment_for',lambda *a,**kw:{'status':'manual','client_key':'changed'})
    code,out=post(request);assert code==409 and 'context changed' in out['error']
    assert len(calls)==2
