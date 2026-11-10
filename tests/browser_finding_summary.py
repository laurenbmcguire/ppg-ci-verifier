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
import api.index as api_module
from hosting import finding_summary
api_module.ai_review_configured=lambda: True
provider_calls=[]
def fake_provider(body):
    provider_calls.append(body)
    assert 'tools' not in body
    return ('Synthetic AI explanation: review the available finding groups below. Nothing has changed.\n' * 90)
finding_summary.ask=fake_provider
from legacy_storage_fake import PostgresWorkspace
from test_vercel import FakeDatabase
from eos_fixture import build,ROWS
from hosting.spreadsheet_inbox import SpreadsheetInbox
from openpyxl import load_workbook
store=PostgresWorkspace('postgresql://test.invalid/synthetic',FakeDatabase().connect)
rows=[list(row) for row in ROWS];rows[1][4]='Holes /  Glue ';rows[2][4]='Holes /  Glue '
raw=build([tuple(row) for row in rows]);original_hash=hashlib.sha256(raw).hexdigest()
with store.session() as directory:
    box=SpreadsheetInbox(directory);item=box.ingest('2230-SYNTHETIC-summary.xlsx',raw)
requests=[]
class LocalHandler(handler):
    def log_message(self,*args):pass
    def do_POST(self):
        requests.append(self.path)
        if self.path not in ('/api/spreadsheets/fixes','/api/spreadsheets/sheet','/api/spreadsheets/cells','/api/spreadsheets/findings','/api/clients','/api/spreadsheets/review-summary'):
            return self.fail(409,'Synthetic summary test forbids writes and other provider requests.')
        return super().do_POST()
server=ThreadingHTTPServer(('127.0.0.1',0),LocalHandler);server.workspace_store=store
threading.Thread(target=server.serve_forever,daemon=True).start()
try:
    result=subprocess.run(['node',str(ROOT/'tests/browser_finding_summary.js'),f'http://127.0.0.1:{server.server_port}',original_hash,str(WIDTH)],text=True,capture_output=True,
        env={k:os.environ[k] for k in ('HOME','TMPDIR','PATH','LANG','PLAYWRIGHT_MODULE','CHROMIUM_EXECUTABLE') if k in os.environ},timeout=80)
    print(result.stdout);print(result.stderr,file=sys.stderr);assert result.returncode==0,result.returncode
    with store.session() as directory:
        box=SpreadsheetInbox(directory);parent=box.get(original_hash);assert box.download(original_hash)[1]==raw
        assert not parent.get('fixes_applied') and not parent.get('finding_decision_log') and not parent.get('approval') and not parent.get('sent')
    assert len(provider_calls)==1 and not external   # Only the explicit review-window action requests a summary.
    context=json.loads(provider_calls[0]['messages'][0]['content'].split('\n',1)[1]);assert context['counts']['available']>0;assert all('ids' not in g for g in context['groups'])
    print(json.dumps(dict(passed=True,width=WIDTH,actual_handler=True,read_only=True,explicit_only=True,no_provider_traffic=True)))
finally:server.shutdown();server.server_close()
