"""Actual-handler/browser recommendation apply, fabricated data, isolated fake-only storage.
Existing read-only browser smoke and its guards remain unchanged. No provider or live DB.
"""
import hashlib, io, json, os, socket, subprocess, sys, threading
from pathlib import Path
from http.server import ThreadingHTTPServer
ROOT=Path(__file__).resolve().parents[1]
WIDTH=int(sys.argv[1]) if len(sys.argv)>1 else 1200
assert WIDTH in (1200,390), 'Only the two isolated test viewports are supported.'
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
for key in list(os.environ):
    if key.startswith(('MS_','ANTHROPIC_','OPENAI_','VERCEL','DATABASE_','PG','CI_PG_')):
        os.environ.pop(key,None)
os.environ.update(CI_LOCAL_DEV='1',VERCEL='0')
original_connect=socket.socket.connect
external=[]
def local_connect(self,address):
    if isinstance(address,tuple) and address[0] not in ('127.0.0.1','localhost','::1'):
        external.append(str(address));raise OSError('Synthetic apply test forbids external network')
    return original_connect(self,address)
socket.socket.connect=local_connect
from api.index import handler
from legacy_storage_fake import PostgresWorkspace
from test_vercel import FakeDatabase
from eos_fixture import build,ROWS
from hosting.spreadsheet_inbox import SpreadsheetInbox
from openpyxl import load_workbook
store=PostgresWorkspace('postgresql://test.invalid/synthetic',FakeDatabase().connect)
rows=[list(row) for row in ROWS];rows[1][4]='Holes /  Glue ';rows[2][4]='Holes /  Glue '
raw=build([tuple(row) for row in rows]);original_hash=hashlib.sha256(raw).hexdigest()
with store.session() as directory:
    box=SpreadsheetInbox(directory);item=box.ingest('2230-SYNTHETIC - EOS - 10.07.26.xlsx',raw);before=box.get(original_hash)
    assert box.holds(before)==[], 'Stop: safe fixture must genuinely satisfy existing eligibility, never patch holds.'
    eligible=[p for p in box.fix_proposals(original_hash)['proposals'] if p['recommendation_eligible']]
    assert len(eligible)==2 and all(p['source']=='Clean-up' for p in eligible),eligible
requests=[];apply_requests=[]
class LocalHandler(handler):
    def log_message(self,*args):pass
    def do_POST(self):
        requests.append(self.path)
        allowed=('/api/spreadsheets/fixes','/api/spreadsheets/sheet','/api/spreadsheets/cells','/api/spreadsheets/findings','/api/clients','/api/spreadsheets/apply-fixes')
        if self.path not in allowed:return self.fail(409,'Synthetic apply test forbids this action.')
        if self.path=='/api/spreadsheets/apply-fixes':
            data=self.rfile.read(int(self.headers.get('Content-Length','0')));payload=json.loads(data);self.rfile=io.BytesIO(data)
            if payload.get('file_hash')!=original_hash or payload.get('recommended_only') is not True:
                return self.fail(409,'Only the fabricated reviewed recommendation scope is allowed.')
            if not payload.get('changes') or any(not any(c.get('id')==p['id'] and c.get('after')==p['after'] for p in eligible) for c in payload['changes']):
                return self.fail(409,'Only generated synthetic recommendations are allowed.')
            apply_requests.append(payload)
        return super().do_POST()
server=ThreadingHTTPServer(('127.0.0.1',0),LocalHandler);server.workspace_store=store
threading.Thread(target=server.serve_forever,daemon=True).start()
try:
    result=subprocess.run(['node',str(ROOT/'tests/browser_recommended_apply.js'),f'http://127.0.0.1:{server.server_port}',original_hash,str(WIDTH)],text=True,capture_output=True,
        env={k:os.environ[k] for k in ('HOME','PATH','LANG','PLAYWRIGHT_MODULE','CHROMIUM_EXECUTABLE') if k in os.environ},timeout=80)
    print(result.stdout);print(result.stderr,file=sys.stderr);assert result.returncode==0,result.returncode
    with store.session() as directory:
        box=SpreadsheetInbox(directory);parent=box.get(original_hash);assert box.download(original_hash)[1]==raw
        assert len(parent.get('fixes_applied') or [])==1;record=parent['fixes_applied'][0];assert len(record['changes'])==1
        new_hash=record['fixed_file_hash'];new=box.get(new_hash);assert new['revision']==2
        assert len(new['fixed_from']['changes'])==1 and new['fixed_from']['file_hash']==original_hash
        original_wb=load_workbook(io.BytesIO(raw));new_wb=load_workbook(io.BytesIO(box.download(new_hash)[1]))
        diffs=[]
        for ws in original_wb:
            for row in ws:
                for cell in row:
                    if cell.value!=new_wb[ws.title][cell.coordinate].value:diffs.append((ws.title,cell.coordinate,cell.value,new_wb[ws.title][cell.coordinate].value))
        chosen=apply_requests[0]['changes'][0];assert diffs==[(chosen['sheet'],chosen['cell'],chosen['before'],chosen['after'])],diffs
        assert not new.get('approval') and not new.get('sent');assert len(apply_requests)==2 # one success and one unchanged replay rejection
        assert not external,external
        print(json.dumps(dict(passed=True,width=WIDTH,actual_handler=True,original_byte_identical=True,one_corrected_version=True,revision=2,recorded_changes=1,exact_cell_differences=diffs,replay_created_no_version=True,no_provider_traffic=True,synthetic_only=True)))
finally:
    server.shutdown();server.server_close()
