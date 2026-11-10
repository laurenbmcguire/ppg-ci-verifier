"""Actual-handler, isolated desktop/phone all-pages selection. Fabricated findings only."""
import hashlib, json, os, socket, subprocess, sys, threading
from pathlib import Path
from http.server import ThreadingHTTPServer
ROOT=Path(__file__).resolve().parents[1]
WIDTH=int(sys.argv[1]) if len(sys.argv)>1 else 1200
assert WIDTH in (1200,390)
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
for key in list(os.environ):
    if key.startswith(('MS_','ANTHROPIC_','OPENAI_','VERCEL','DATABASE_','PG','CI_PG_')): os.environ.pop(key,None)
os.environ.update(CI_LOCAL_DEV='1',VERCEL='0',CI_REVIEW_PAGE='300')
original_connect=socket.socket.connect
external=[]
def local_connect(self,address):
    if isinstance(address,tuple) and address[0] not in ('127.0.0.1','localhost','::1'):
        external.append(str(address));raise OSError('Synthetic selection test forbids external network')
    return original_connect(self,address)
socket.socket.connect=local_connect
from api.index import handler
import api.index as api_module
from hosting import ai_proposals
api_module.ai_review_configured=lambda: True
provider_calls=[]
def fake_provider(rows): provider_calls.append(rows); return {'fixes':[]}
ai_proposals.ask=fake_provider
from legacy_storage_fake import PostgresWorkspace
from test_vercel import FakeDatabase
from eos_fixture import build
from hosting.spreadsheet_inbox import SpreadsheetInbox
from hosting.review_summary import summarize
store=PostgresWorkspace('postgresql://test.invalid/synthetic-selection',FakeDatabase().connect)
raw=build();original_hash=hashlib.sha256(raw).hexdigest()
with store.session() as directory:
    box=SpreadsheetInbox(directory);box.ingest('2230-SYNTHETIC - EOS - 10.07.26.xlsx',raw)
    assert not box.fix_proposals(original_hash)['proposals']
    findings=[dict(code='synthetic_review',sheet='1st Shift',cell='I17',row=17,message=f'Synthetic item {i+1}',observed=[{'type':'str','value':'Holes / Glue'}]) for i in range(1301)]
    box._update(original_hash,lambda r:r.update(findings=findings,finding_counts={'synthetic_review':1301},finding_count=1301,findings_truncated=False))
    before=box.get(original_hash);expected_keys={i['key'] for i in summarize(before)['issues']}
    assert len(expected_keys)==1301
requests=[]
class LocalHandler(handler):
    def log_message(self,*args): pass
    def do_POST(self):
        requests.append(self.path)
        allowed=('/api/spreadsheets/fixes','/api/spreadsheets/sheet','/api/spreadsheets/cells','/api/spreadsheets/findings','/api/clients','/api/spreadsheets/ai-proposals')
        if self.path not in allowed: return self.fail(409,'Synthetic selection test forbids mutations.')
        return super().do_POST()
server=ThreadingHTTPServer(('127.0.0.1',0),LocalHandler);server.workspace_store=store
threading.Thread(target=server.serve_forever,daemon=True).start()
try:
    result=subprocess.run(['node',str(ROOT/'tests/browser_complete_selection.js'),f'http://127.0.0.1:{server.server_port}',original_hash,str(WIDTH)],text=True,capture_output=True,
        env={k:os.environ[k] for k in ('HOME','PATH','LANG','PLAYWRIGHT_MODULE','CHROMIUM_EXECUTABLE','QA_SCREENSHOT_DIR') if k in os.environ},timeout=120)
    print(result.stdout);print(result.stderr,file=sys.stderr);assert result.returncode==0,result.returncode
    observation=json.loads(result.stdout.strip().splitlines()[-1])
    assert set(observation['requested_keys'])==expected_keys-{observation['excluded']}
    with store.session() as directory:
        box=SpreadsheetInbox(directory);assert box.get(original_hash)==before;assert box.download(original_hash)[1]==raw;assert box.list()['count']==1
    assert not provider_calls and not external
    print(json.dumps(dict(passed=True,width=WIDTH,actual_handler=True,all_1301_inspectable=True,final_page_exclusion_preserved=True,all_1300_selected_ids_in_bounded_requests=True,no_writes=True,no_provider_traffic=True)))
finally:
    server.shutdown();server.server_close()
