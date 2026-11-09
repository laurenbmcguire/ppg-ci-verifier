"""Synthetic actual HTTP/browser Q&A; no user files, real provider, grants or remote traffic."""
import json, os, socket, subprocess, sys, threading, time
from pathlib import Path
from http.server import ThreadingHTTPServer
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
for key in list(os.environ):
    if key.startswith(('MS_','ANTHROPIC_','OPENAI_','VERCEL','DATABASE_','PG','CI_PG_')):os.environ.pop(key,None)
os.environ.update(CI_LOCAL_DEV='1',VERCEL='0')
os.environ.pop('AI_LAYOUT',None)
original_connect=socket.socket.connect
external=[]
def local_connect(self,address):
    if isinstance(address,tuple) and address[0] not in ('127.0.0.1','localhost','::1'):
        external.append(str(address));raise OSError('Synthetic chat test forbids external network')
    return original_connect(self,address)
socket.socket.connect=local_connect
from api.index import handler
from hosting import ai_review
from hosting.spreadsheet_inbox import SpreadsheetInbox
from legacy_storage_fake import PostgresWorkspace
from test_vercel import FakeDatabase
store=PostgresWorkspace('postgresql://test.invalid/synthetic',FakeDatabase().connect)
provider_calls=[]
def fake_post(body,opener=None,timeout=None):
    provider_calls.append(body)
    assert 'tools' not in body
    if 'retry this failure' in body['messages'][-1]['content']:raise ai_review.AIReviewError('synthetic private provider detail')
    if 'slow reply' in body['messages'][-1]['content']:time.sleep(0.8)
    return {'model':'synthetic-provider','content':[{'type':'text','text':'CSV!C2 is blank: rejected quantity is unknown. <img src=x onerror="window.dexUnsafe=true"> No report was changed.'}]}
with store.session() as directory:
    box=SpreadsheetInbox(directory)
    first=box.ingest('SYNTHETIC-one.csv',b'Part,Inspected,Rejected\nSYN-A,10,\n')
    second=box.ingest('SYNTHETIC-two.csv',b'Part,Inspected,Rejected\nSYN-B,20,2\n')
    originals={i['file_hash']:box.original_bytes(i['file_hash'])[1] for i in (first,second)}
    before={i['file_hash']:box.get(i['file_hash']) for i in (first,second)}
ai_review._post=fake_post
os.environ['ANTHROPIC_API_KEY']='synthetic-unused'
requests=[]
class LocalHandler(handler):
    def log_message(self,*args):pass
    def do_POST(self):
        requests.append(self.path)
        if self.path not in ('/api/spreadsheets/fixes','/api/spreadsheets/sheet','/api/spreadsheets/cells','/api/spreadsheets/findings','/api/clients','/api/spreadsheets/chat'):
            return self.fail(409,'Synthetic chat test forbids mutations.')
        return super().do_POST()
server=ThreadingHTTPServer(('127.0.0.1',0),LocalHandler);server.workspace_store=store
threading.Thread(target=server.serve_forever,daemon=True).start()
try:
    result=subprocess.run(['node',str(ROOT/'tests/browser_report_chat.js'),f'http://127.0.0.1:{server.server_port}',first['file_hash'],second['file_hash']],text=True,capture_output=True,
      env={k:os.environ[k] for k in ('HOME','PATH','LANG','PLAYWRIGHT_MODULE','CHROMIUM_EXECUTABLE') if k in os.environ},timeout=120)
    print(result.stdout);print(result.stderr,file=sys.stderr);assert result.returncode==0
    with store.session() as directory:
        box=SpreadsheetInbox(directory)
        for hash_,raw in originals.items():
            assert box.original_bytes(hash_)[1]==raw and box.get(hash_)==before[hash_]
        assert box.list()['count']==2
    assert not external
    assert len(provider_calls)==8, len(provider_calls)
    assert not any(p.endswith(('apply-fixes','ai-fix','send','approve')) for p in requests)
    print(json.dumps(dict(passed=True,synthetic_only=True,provider='fake',provider_calls=len(provider_calls),no_external_traffic=True,unchanged_reports=True)))
finally:server.shutdown();server.server_close()
