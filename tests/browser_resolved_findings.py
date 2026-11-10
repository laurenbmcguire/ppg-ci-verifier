"""Actual handler, real checks and exact synthetic bytes; no external sources/provider."""
import io, json, os, socket, subprocess, sys, threading
from pathlib import Path
from http.server import ThreadingHTTPServer
ROOT = Path(__file__).resolve().parents[1]
WIDTH = int(sys.argv[1]) if len(sys.argv) > 1 else 1200
MODE = sys.argv[2] if len(sys.argv) > 2 else 'fixed'
assert WIDTH in (1200, 390) and MODE in ('baseline', 'fixed')
sys.path[:0] = [str(ROOT), str(ROOT/'tests')]
for key in list(os.environ):
    if key.startswith(('MS_', 'ANTHROPIC_', 'OPENAI_', 'VERCEL', 'DATABASE_', 'PG', 'CI_PG_')):
        os.environ.pop(key, None)
os.environ.update(CI_LOCAL_DEV='1', VERCEL='0', AI_LAYOUT='0', CI_REVIEW_PAGE='2')
connect = socket.socket.connect
external = []
def local(self, address):
    if isinstance(address, tuple) and address[0] not in ('127.0.0.1', 'localhost', '::1'):
        external.append(str(address)); raise OSError('External network forbidden')
    return connect(self, address)
socket.socket.connect = local
from api.index import handler
import api.index as api_module
from hosting import ai_proposals
from hosting.spreadsheet_inbox import SpreadsheetInbox
from hosting.review_summary import summarize
from legacy_storage_fake import PostgresWorkspace
from test_vercel import FakeDatabase
from eos_fixture import build
api_module.ai_review_configured = lambda: True
ai_proposals.ask = lambda rows: (_ for _ in ()).throw(AssertionError('Provider must not be used'))
store = PostgresWorkspace('postgresql://test.invalid/synthetic', FakeDatabase().connect)
cases = []
with store.session() as directory:
    box = SpreadsheetInbox(directory)
    for name in ('partial', 'complete', 'standalone', 'manual', 'refresh_failure', 'navigation'):
        raw = build(overrides=({('1st Shift','I17'): 'Holes /  Glue ', ('1st Shift','I18'): 'Holes /  Glue ', ('1st Shift','L18'): 'Synthetic lifecycle standalone'} if name == 'standalone' else {('1st Shift','G17'): '-1' if name == 'partial' else -1 if name == 'manual' else '34', ('1st Shift','G18'): '34', ('1st Shift','L18'): 'Synthetic lifecycle ' + name}))
        item = box.ingest('2230-SYNTHETIC-' + name + '.xlsx', raw)
        # Keep a genuine outstanding source requirement. No guard is replaced.
        item = box._update(item['file_hash'], lambda r: r.update(source_reconciliation=dict(status='incomplete',verified=False,bound_to=dict(file_hash=r['file_hash'],checker_version=r['checker_version']),not_verified_because=['Synthetic source evidence missing.'])))
        issues = summarize(item)['issues']
        selected = [i for i in issues if i.get('cell') == 'G18' and i['code'] == 'number_stored_as_text' or i.get('cell') == 'G17' and i['code'] == ('negative_count_or_quantity' if name in ('partial','manual') else 'number_stored_as_text')]
        if name == 'standalone': selected = [dict(key='fix:' + p['id']) for p in box.fix_proposals(item['file_hash'])['proposals'] if p['source']=='Clean-up' and p['cell'] in ('I17','I18')]
        assert len(selected) == 2
        unselected_id = next((i['key'] for i in selected if i.get('cell') == 'G18'), None)
        if name == 'manual': selected = [i for i in selected if i.get('cell') == 'G17']
        cases.append(dict(name=name,hash=item['file_hash'],ids=[i['key'] for i in selected],unselected_id=unselected_id,raw=raw))
applies=[]
class LocalHandler(handler):
    def log_message(self,*args): pass
    def do_POST(self):
        if self.path not in ('/api/clients','/api/spreadsheets/sheet','/api/spreadsheets/cells','/api/spreadsheets/fixes','/api/spreadsheets/findings','/api/spreadsheets/ai-proposals','/api/spreadsheets/apply-fixes'):
            return self.fail(409,'Only isolated fixture actions allowed.')
        if self.path.endswith('/apply-fixes'):
            data=self.rfile.read(int(self.headers.get('Content-Length','0')));p=json.loads(data);self.rfile=io.BytesIO(data)
            case=next(c for c in cases if c['hash']==p['file_hash'])
            if case['name']=='manual':
                assert len(p['changes'])==1 and p['changes'][0]['cell']=='G17' and p['changes'][0]['after']=='34' and p['changes'][0]['source']=='Your edit'
            else: assert len(p['changes'])==2 and {c['cell'] for c in p['changes']} in ({'G17','G18'},{'I17','I18'})
            applies.append(p)
        return super().do_POST()
server=ThreadingHTTPServer(('127.0.0.1',0),LocalHandler);server.workspace_store=store
threading.Thread(target=server.serve_forever,daemon=True).start()
try:
    config=[{k:v for k,v in c.items() if k!='raw'} for c in cases]
    result=subprocess.run(['node',str(ROOT/'tests/browser_resolved_findings.js'),f'http://127.0.0.1:{server.server_port}',json.dumps(config),str(WIDTH),MODE],text=True,capture_output=True,env={k:os.environ[k] for k in ('HOME','PATH','LANG','PLAYWRIGHT_MODULE','CHROMIUM_EXECUTABLE','EVIDENCE_DIR') if k in os.environ},timeout=180)
    print(result.stdout);print(result.stderr,file=sys.stderr);assert result.returncode==0
    with store.session() as directory:
        box=SpreadsheetInbox(directory)
        for c in cases:
            parent=box.get(c['hash']);assert box.download(c['hash'])[1]==c['raw']
            if MODE=='baseline' and c['name']!='partial': continue
            assert len(parent['fixes_applied'])==1
            child=box.get(parent['fixes_applied'][0]['fixed_file_hash']);review=summarize(child)
            assert child['revision']==2 and len(child['fixed_from']['changes'])==(1 if c['name']=='manual' else 2)
            if c['name']=='standalone': assert not any(i['code']=='extra_spaces' for i in review['issues'])
            assert not any(i['code']=='number_stored_as_text' and i.get('cell') in (('G17',) if c['name']=='manual' else ('G17','G18')) for i in review['issues'])
            assert any(i['code']=='negative_count_or_quantity' and i.get('cell')=='G17' for i in review['issues']) == (c['name']=='partial')
            if c['name']=='manual':
                from openpyxl import load_workbook
                a=load_workbook(io.BytesIO(c['raw']));b=load_workbook(io.BytesIO(box.download(child['file_hash'])[1]))
                diffs=[(ws.title,cell.coordinate,cell.value,b[ws.title][cell.coordinate].value) for ws in a for row in ws for cell in row if cell.value!=b[ws.title][cell.coordinate].value]
                assert diffs==[('1st Shift','G17',-1,34)],diffs
                assert any(i['key']==c['unselected_id'] for i in review['issues'])
            assert any(h['hard'] and h['code']=='source_coverage_incomplete' for h in box.holds(child))
            assert not child.get('approval') and not child.get('sent')
    assert not external
    print(json.dumps(dict(passed=True,width=WIDTH,mode=MODE,actual_handler=True,real_checker=True,saves=len(applies),originals_unchanged=True,source_holds_preserved=True,external_requests=0)))
finally:server.shutdown();server.server_close()
