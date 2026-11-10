"""Actual HTTP handler, synthetic lineage and fake storage; no provider/live data."""
import hashlib,json,os,socket,subprocess,sys,threading
from pathlib import Path
from http.server import ThreadingHTTPServer
ROOT=Path(__file__).resolve().parents[1];WIDTH=int(sys.argv[1]) if len(sys.argv)>1 else 1200
assert WIDTH in (1200,390)
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
for key in list(os.environ):
    if key.startswith(('MS_','ANTHROPIC_','OPENAI_','VERCEL','DATABASE_','PG','CI_PG_')):os.environ.pop(key,None)
os.environ.update(CI_LOCAL_DEV='1',VERCEL='0',AI_LAYOUT='0')
connect=socket.socket.connect;external=[]
def local(self,address):
    if isinstance(address,tuple) and address[0] not in ('127.0.0.1','localhost','::1'):
        external.append(str(address));raise OSError('Synthetic test forbids external network')
    return connect(self,address)
socket.socket.connect=local
from api.index import handler
from hosting.spreadsheet_inbox import SpreadsheetInbox
from legacy_storage_fake import PostgresWorkspace
from test_vercel import FakeDatabase
from test_version_reuse import RAW,NAME,save
from test_working_copy import parallel
store=PostgresWorkspace('postgresql://test.invalid/synthetic',FakeDatabase().connect)
with store.session() as directory:
    box=SpreadsheetInbox(directory)
    a=box.ingest(NAME,RAW)['file_hash'];b=save(box,a,'original','second')['file_hash'];c=save(box,b,'second','third')['file_hash']
    root=box.ingest('parallel.csv',RAW.replace(b'original',b'branch root'))['file_hash']
    left=save(box,root,'branch root','left branch')['file_hash'];right=parallel(box,root,'branch root','right branch')
    broken=box.ingest('inconsistent.csv',RAW.replace(b'original',b'inconsistent'))['file_hash']
    box._update(broken,lambda r:r.update(fixed_from={'file_hash':broken},fixes_applied=[{'fixed_file_hash':broken}]))
    locked={}
    for reason in ('approved','sent','sending'):
        key=box.ingest(reason+'.csv',RAW.replace(b'original',reason.encode()))['file_hash'];locked[reason]=key
        box._update(key,lambda r,reason=reason:r.update(**({'approval':{'by':'Synthetic reviewer'}} if reason=='approved' else {'sent':[{'sent':True}]} if reason=='sent' else {'sending':{'request_id':'synthetic'}})))
    dirty_raw=b'Date,Shift,Area,Part number,Identifier / lot,Inspected for,Quantity Inspected,Condition,Reject reason,Reject Quantity,Quantity Certified,Comments\n2026-11-09,1,A,Part-1,,,10,NOK,,2,8,\n'
    dirty=box.ingest('synthetic-dirty.csv',dirty_raw)['file_hash']
    config=dict(dirty=dirty,a=a,b=b,c=c,root=root,left=left,right=right,broken=broken,locked=locked)
    original={r['file_hash']:box.download(r['file_hash'])[1] for r in box.list()['spreadsheets']}
    config['sha']={k:hashlib.sha256(v).hexdigest() for k,v in original.items()}
requests=[]
class LocalHandler(handler):
    def log_message(self,*args):pass
    def do_POST(self):
        requests.append(self.path)
        if self.path not in ('/api/clients','/api/spreadsheets/sheet','/api/spreadsheets/cells','/api/spreadsheets/fixes','/api/spreadsheets/findings'):
            return self.fail(409,'Read-only synthetic browser test forbids mutations.')
        return super().do_POST()
server=ThreadingHTTPServer(('127.0.0.1',0),LocalHandler);server.workspace_store=store
threading.Thread(target=server.serve_forever,daemon=True).start()
try:
    result=subprocess.run(['node',str(ROOT/'tests/browser_latest_working_copy.js'),f'http://127.0.0.1:{server.server_port}',json.dumps(config),str(WIDTH)],text=True,capture_output=True,env={k:os.environ[k] for k in ('HOME','PATH','LANG','PLAYWRIGHT_MODULE','CHROMIUM_EXECUTABLE') if k in os.environ},timeout=120)
    print(result.stdout);print(result.stderr,file=sys.stderr);assert result.returncode==0
    with store.session() as directory:
        box=SpreadsheetInbox(directory)
        assert all(box.download(k)[1]==v for k,v in original.items())
    assert not external;print(json.dumps(dict(actual_handler=True,original_bytes_preserved=True,no_external_network=True,width=WIDTH)))
finally:server.shutdown();server.server_close()
