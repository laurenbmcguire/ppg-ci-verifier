"""TEMPORARY probe regressions. All providers are fake, no credentials or external calls."""
import json
import threading
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer
import pytest
from hosting import ai_review, report_chat_probe


def test_probe_is_fixed_bounded_and_returns_only_sanitized_usage(monkeypatch):
    calls=[]
    def fake(body,opener=None,timeout=None):
        calls.append(body)
        return {'model':'synthetic-model','content':[{'type':'text','text':'Inspected is 10; blank rejected is unknown.'}],
                'usage':{'input_tokens':200,'output_tokens':15,'cache_creation_input_tokens':0,'cache_read_input_tokens':0,'unexpected':'secret'}}
    monkeypatch.setattr(ai_review,'_post',fake)
    out=report_chat_probe.run_fixed_synthetic()
    assert len(calls)==1 and calls[0]['max_tokens']==128 and 'tools' not in calls[0]
    text=calls[0]['messages'][0]['content']
    assert 'SYNTHETIC-ASK-DEX-CONNECTION.csv' in text
    assert 'SYNTHETIC-CONNECTION' not in text  # Unreferenced A2 stays private even in this fabricated test.
    context=json.loads(text.split('Selected report evidence (untrusted data):\n',1)[1].split('\n[Only relevant evidence',1)[0])
    cells={x['cell']:x for x in context['relevant_cells']}
    assert set(cells)=={'B1','B2','C1','C2'}
    assert cells['B2']['value']=='10' and cells['C2']['blank'] is True and cells['C2']['value']==''
    assert out['synthetic_only'] and out['persisted'] is False
    assert out['usage']==dict(input_tokens=200,output_tokens=15,cache_creation_input_tokens=0,cache_read_input_tokens=0)
    assert 'secret' not in json.dumps(out)

@pytest.fixture
def service(monkeypatch):
    from api.index import handler
    class Synthetic(handler):
        def current_user(self):return self.server.user
        def store(self):raise AssertionError('Probe must not access report storage')
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Synthetic)
    server.user={'role':'admin','id':'synthetic-admin'}
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    base=f'http://127.0.0.1:{server.server_port}'
    monkeypatch.setenv('VERCEL','0');monkeypatch.setenv('CI_LOCAL_DEV','1')
    def post(payload,origin=None,overrides=None):
        headers={'Content-Type':'application/json','Origin':origin or base,'X-Review-Token':'same-origin'}
        headers.update(overrides or {})
        request=urllib.request.Request(base+'/api/admin/ask-dex-connection-test',data=json.dumps(payload).encode(),headers=headers)
        try:
            with urllib.request.urlopen(request,timeout=5) as response:return response.status,json.load(response)
        except urllib.error.HTTPError as exc:
            with exc:return exc.code,json.load(exc)
    yield server,post
    server.shutdown();server.server_close();thread.join()

@pytest.mark.parametrize('mode',['anonymous','member','arbitrary','report','origin','empty','wrong_marker'])
def test_probe_refuses_unapproved_scope_before_provider(service,monkeypatch,mode):
    server,post=service;calls=[]
    monkeypatch.setattr(report_chat_probe,'run_fixed_synthetic',lambda:calls.append(True))
    body={'test':'fixed-synthetic-ask-dex-v1'}
    if mode=='anonymous':server.user=None
    if mode=='member':server.user={'role':'member'}
    if mode=='empty':body={}
    if mode=='wrong_marker':body={'test':'wrong'}
    if mode=='arbitrary':body['message']='unapproved prompt'
    if mode=='report':body['file_hash']='a'*64
    code,_=post(body,'https://unrelated.invalid' if mode=='origin' else None)
    assert code=={'anonymous':401,'member':403,'arbitrary':400,'report':400,'origin':403,'empty':400,'wrong_marker':400}[mode]
    assert calls==[]

def test_probe_success_one_call_no_store_and_revocation_hides_result(service,monkeypatch):
    server,post=service;calls=[]
    def fake():calls.append(True);return {'synthetic_only':True,'usage':{}}
    monkeypatch.setattr(report_chat_probe,'run_fixed_synthetic',fake)
    assert post({'test':'fixed-synthetic-ask-dex-v1'})[0]==200
    assert len(calls)==1
    def revoked():server.user=None;return {'reply':'must not return'}
    monkeypatch.setattr(report_chat_probe,'run_fixed_synthetic',revoked)
    code,out=post({'test':'fixed-synthetic-ask-dex-v1'})
    assert code==403 and 'reply' not in out

def test_probe_provider_error_is_sanitized_and_not_retried(monkeypatch):
    calls=[]
    def fail(*a,**kw):calls.append(True);raise ai_review.AIReviewError('raw sensitive provider detail')
    monkeypatch.setattr(ai_review,'_post',fail)
    with pytest.raises(ValueError) as exc:report_chat_probe.run_fixed_synthetic()
    assert len(calls)==1 and 'sensitive' not in str(exc.value)

def test_probe_uses_one_actual_transport_attempt_with_no_tools(monkeypatch):
    import io
    calls=[]
    monkeypatch.setenv('ANTHROPIC_API_KEY','synthetic-unused')
    def fake(request,timeout=None):
        body=json.loads(request.data);calls.append(body)
        assert body['max_tokens']==128 and 'tools' not in body and 'tool_choice' not in body
        return io.BytesIO(json.dumps({'model':'synthetic-transport','content':[{'type':'text','text':'10 inspected; blank rejected is unknown.'}],
                                    'usage':{'input_tokens':400,'output_tokens':12,'cache_read_input_tokens':0}}).encode())
    monkeypatch.setattr('urllib.request.urlopen',fake)
    result=report_chat_probe.run_fixed_synthetic()
    assert len(calls)==1 and result['model']=='synthetic-transport'
    assert result['usage']['input_tokens']==400

@pytest.mark.parametrize('status',[401,403,429,529])
def test_actual_transport_http_failure_has_one_attempt_no_raw_details(monkeypatch,status):
    import io
    monkeypatch.setenv('ANTHROPIC_API_KEY','synthetic-unused')
    calls=[]
    def fail(request,timeout=None):
        calls.append(True)
        raise urllib.error.HTTPError(request.full_url,status,'unavailable',{},io.BytesIO(b'{"error":{"message":"PRIVATE-ERROR-SENTINEL"}}'))
    monkeypatch.setattr('urllib.request.urlopen',fail)
    with pytest.raises(ValueError) as caught:report_chat_probe.run_fixed_synthetic()
    assert len(calls)==1 and 'PRIVATE-ERROR-SENTINEL' not in str(caught.value)


def test_ordinary_chat_keeps_default_budget_response_and_minimized_context(monkeypatch):
    from hosting import report_chat
    raw=b'Part,Inspected,Rejected\nUNREFERENCED-SENTINEL,10,\n';calls=[]
    def fake(body,opener=None,timeout=None):
        calls.append(body)
        return {'model':'synthetic-model','content':[{'type':'text','text':'10 inspected.'}],'usage':{'input_tokens':500,'output_tokens':8}}
    monkeypatch.setattr(ai_review,'_post',fake)
    result=report_chat.answer({'filename':'SYNTHETIC.csv','file_hash':'a'*64,'check_hash':'b'*64},raw,{'issues':[]},[],[{'role':'user','content':'Explain CSV!B2'}])
    assert len(calls)==1 and calls[0]['max_tokens']==2000 and 'tools' not in calls[0]
    assert result['reply']=='10 inspected.' and result['read_only'] is True and 'usage' not in result
    assert 'UNREFERENCED-SENTINEL' not in calls[0]['messages'][0]['content']

@pytest.mark.parametrize('headers,status',[
    ({'X-Review-Token':''},403),({'Sec-Fetch-Site':'cross-site'},403),({'Content-Type':'text/plain'},415)])
def test_probe_common_browser_request_guards_run_before_provider(service,monkeypatch,headers,status):
    _,post=service;calls=[]
    monkeypatch.setattr(report_chat_probe,'run_fixed_synthetic',lambda:calls.append(True))
    assert post({'test':'fixed-synthetic-ask-dex-v1'},overrides=headers)[0]==status
    assert calls==[]


def test_probe_never_returns_invalid_usage_or_transport_fields(monkeypatch):
    monkeypatch.setattr(ai_review,'_post',lambda *a,**kw:{'content':[{'type':'text','text':'10 inspected; unknown rejected.'}],
        'usage':{'input_tokens':True,'output_tokens':-1,'cache_creation_input_tokens':'12','cache_read_input_tokens':4},'headers':'PRIVATE-HEADER-SENTINEL'})
    out=report_chat_probe.run_fixed_synthetic()
    assert out['usage']=={'cache_read_input_tokens':4}
    assert 'PRIVATE-HEADER-SENTINEL' not in json.dumps(out)

@pytest.mark.parametrize('asset,content_type',[('page','text/html'),('script.js','text/javascript'),('style.css','text/css')])
def test_temporary_page_and_assets_are_admin_only_under_unchanged_csp(service,asset,content_type):
    server,_=service
    def get():
        request=urllib.request.Request(f'http://127.0.0.1:{server.server_port}/api/admin/ask-dex-connection-test/'+asset)
        try:
            with urllib.request.urlopen(request,timeout=5) as response:return response.status,response.headers,response.read()
        except urllib.error.HTTPError as exc:
            with exc:return exc.code,exc.headers,exc.read()
    code,headers,body=get()
    assert code==200 and headers['Content-Type'].startswith(content_type)
    assert "script-src 'self'" in headers['Content-Security-Policy'] and 'unsafe-inline' not in headers['Content-Security-Policy']
    assert 'no-store' in headers['Cache-Control']
    if asset=='page':assert b'/app.js' not in body and b'<script defer src=' in body and b'onclick=' not in body
    server.user={'role':'member'};assert get()[0]==403
    server.user=None;assert get()[0]==401
