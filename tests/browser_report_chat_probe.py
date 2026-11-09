"""Temporary probe normal-UI acceptance: synthetic admin, fake provider, loopback only."""
import json,os,socket,subprocess,sys,threading,time
from pathlib import Path
from http.server import ThreadingHTTPServer
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
for key in list(os.environ):
    if key.startswith(('MS_','ANTHROPIC_','OPENAI_','VERCEL','DATABASE_','PG','CI_PG_')):os.environ.pop(key,None)
os.environ.update(CI_LOCAL_DEV='1',VERCEL='0')
connect=socket.socket.connect;external=[]
def loopback(self,address):
    if isinstance(address,tuple) and address[0] not in ('127.0.0.1','localhost','::1'):
        external.append(str(address));raise OSError('External network forbidden')
    return connect(self,address)
socket.socket.connect=loopback
from api.index import handler
from hosting import ai_review
calls=[];requests=[]
def fake(body,opener=None,timeout=None):
    calls.append(body);assert body['max_tokens']==128 and 'tools' not in body
    time.sleep(.35)
    return {'model':'synthetic-model','content':[{'type':'text','text':'10 inspected; blank rejected is unknown. <img src=x onerror="window.probeUnsafe=true">'}],
            'usage':{'input_tokens':300,'output_tokens':18,'cache_read_input_tokens':0}}
ai_review._post=fake
class Local(handler):
    def current_user(self):return {'id':'synthetic-admin','role':'admin'}
    def store(self):raise AssertionError('No report storage in probe UI')
    def do_GET(self):requests.append(('GET',self.path));return super().do_GET()
    def do_POST(self):requests.append(('POST',self.path));return super().do_POST()
server=ThreadingHTTPServer(('127.0.0.1',0),Local);threading.Thread(target=server.serve_forever,daemon=True).start()
try:
    result=subprocess.run(['node',str(ROOT/'tests/browser_report_chat_probe.js'),f'http://127.0.0.1:{server.server_port}'],capture_output=True,text=True,
      env={k:os.environ[k] for k in ('HOME','PATH','LANG','PLAYWRIGHT_MODULE','CHROMIUM_EXECUTABLE') if k in os.environ},timeout=90)
    print(result.stdout);print(result.stderr,file=sys.stderr);assert result.returncode==0
    assert len(calls)==2 and not external
    assert all(path.startswith('/api/admin/ask-dex-connection-test') or path=='/favicon.ico' for _,path in requests)
    print(json.dumps({'passed':True,'provider':'fake','provider_calls':2,'external_calls':0,'report_store_access':0,'automatic_retries':0}))
finally:server.shutdown();server.server_close()
