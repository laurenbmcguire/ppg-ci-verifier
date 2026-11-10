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
    raw=b'Date,Shift,Area,Part number,Identifier / lot,Inspected for,Quantity Inspected,Condition,Reject reason,Reject Quantity,Quantity Certified,Comments\n2026-11-09,1,A,Part-1,,,10,NOK,,2,8,\n'
    a=box.ingest('synthetic-reused.csv',raw)['file_hash']
    def correction(key,before,after):
        return box.apply_fixes(key,[dict(sheet='CSV',cell='I2',before=before,after=after,kind='auto',source='Your edit',reason='Synthetic reviewed edit')],expected_check_hash=box.get(key)['check_hash'])['file_hash']
    b=correction(a,'','first reason');c=correction(b,'first reason','second reason')
    config=dict(a=a,b=b,c=c)
    original={r['file_hash']:box.download(r['file_hash'])[1] for r in box.list()['spreadsheets']}
requests=[]
class LocalHandler(handler):
    def log_message(self,*args):pass
    def do_POST(self):
        requests.append(self.path)
        if self.path not in ('/api/clients','/api/spreadsheets/sheet','/api/spreadsheets/cells','/api/spreadsheets/fixes','/api/spreadsheets/findings','/api/spreadsheets/apply-fixes'):
            return self.fail(409,'Read-only synthetic browser test forbids mutations.')
        if self.path=='/api/spreadsheets/apply-fixes':
            import io
            raw_body=self.rfile.read(int(self.headers.get('Content-Length','0')));body=json.loads(raw_body);self.rfile=io.BytesIO(raw_body)
            if body.get('file_hash')!=c or len(body.get('changes',[]))!=1 or any(body['changes'][0].get(k)!=v for k,v in dict(sheet='CSV',cell='I2',before='second reason',after='first reason',kind='auto',source='Your edit').items()):
                return self.fail(409,'Only the exact synthetic ancestor reuse is allowed.')
        return super().do_POST()
server=ThreadingHTTPServer(('127.0.0.1',0),LocalHandler);server.workspace_store=store
threading.Thread(target=server.serve_forever,daemon=True).start()
try:
    result=subprocess.run(['node',str(ROOT/'tests/browser_latest_receipt.js'),f'http://127.0.0.1:{server.server_port}',json.dumps(config),str(WIDTH)],text=True,capture_output=True,env={k:os.environ[k] for k in ('HOME','PATH','LANG','PLAYWRIGHT_MODULE','CHROMIUM_EXECUTABLE') if k in os.environ},timeout=120)
    print(result.stdout);print(result.stderr,file=sys.stderr);assert result.returncode==0
    with store.session() as directory:
        box=SpreadsheetInbox(directory)
        assert all(box.download(k)[1]==v for k,v in original.items())
        assert box.list()['count']==3
    assert requests.count('/api/spreadsheets/apply-fixes')==1
    assert not external;print(json.dumps(dict(actual_handler=True,original_bytes_preserved=True,no_external_network=True,width=WIDTH)))
finally:server.shutdown();server.server_close()
