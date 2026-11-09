"""Actual chooser decisions/manual apply, isolated fabricated reports and fake storage only."""
import io,json,os,socket,subprocess,sys,threading
from pathlib import Path
from http.server import ThreadingHTTPServer
ROOT=Path(__file__).resolve().parents[1]
WIDTH=int(sys.argv[1]) if len(sys.argv)>1 else 1200
assert WIDTH in (1200,390)
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
for key in list(os.environ):
    if key.startswith(('MS_','ANTHROPIC_','OPENAI_','VERCEL','DATABASE_','PG','CI_PG_')):os.environ.pop(key,None)
os.environ.update(CI_LOCAL_DEV='1',VERCEL='0',AI_LAYOUT='0')
connect=socket.socket.connect;external=[]
def local(self,address):
    if isinstance(address,tuple) and address[0] not in ('127.0.0.1','localhost','::1'):
        external.append(str(address));raise OSError('Synthetic chooser test forbids provider traffic')
    return connect(self,address)
socket.socket.connect=local
from api.index import handler
from hosting.spreadsheet_inbox import SpreadsheetInbox
from hosting.review_summary import summarize
from legacy_storage_fake import PostgresWorkspace
from test_vercel import FakeDatabase
from eos_fixture import build,ROWS
from openpyxl import load_workbook
store=PostgresWorkspace('postgresql://test.invalid/synthetic',FakeDatabase().connect)
rows=[list(r) for r in ROWS];rows[1][4]='Holes /  Glue ';rows[2][4]='Pinch /  Gap '
raw=build([tuple(r) for r in rows]);rows[3][5]='Synthetic manual fixture';manual_raw=build([tuple(r) for r in rows]);rows[3][5]='Synthetic group fixture';group_raw=build([tuple(r) for r in rows])
with store.session() as directory:
    box=SpreadsheetInbox(directory);decision=box.ingest('2230-SYNTHETIC-decisions.xlsx',raw);manual=box.ingest('2230-SYNTHETIC-manual.xlsx',manual_raw);group=box.ingest('2230-SYNTHETIC-group.xlsx',group_raw)
    issues=lambda r:[i for i in summarize(r)['issues'] if i['code']=='extra_spaces']
    selected=issues(decision);manual_issues=issues(manual)
    assert len(selected)==2 and len(manual_issues)==2,(selected,manual_issues)
    assert box.holds(manual)==[],'Fixture must genuinely satisfy existing guards; never patch holds.'
    proposal=next(p for p in box.fix_proposals(manual['file_hash'])['proposals'] if p['source']=='Clean-up' and p['cell']=='I17')
    linked=next((i for i in summarize(manual)['issues'] if i.get('sheet')==proposal['sheet'] and str(i.get('row'))==''.join(c for c in proposal['cell'] if c.isdigit()) and str(proposal['before']).strip() in i.get('text','')),None)
    config=dict(group_hash=group['file_hash'],group_ids=[i['key'] for i in issues(group)],decision_hash=decision['file_hash'],ids=[i['key'] for i in selected],manual_hash=manual['file_hash'],manual_id=linked['key'] if linked else 'fix:'+proposal['id'],sheet=proposal['sheet'],cell=proposal['cell'],before=proposal['before'],after='00124')
requests=[];decisions=[];applies=[]
class LocalHandler(handler):
    def log_message(self,*args):pass
    def do_POST(self):
        requests.append(self.path)
        allowed=('/api/clients','/api/spreadsheets/sheet','/api/spreadsheets/cells','/api/spreadsheets/fixes','/api/spreadsheets/findings','/api/spreadsheets/decide','/api/spreadsheets/apply-fixes')
        if self.path not in allowed:return self.fail(409,'Only isolated synthetic chooser actions are allowed.')
        if self.path in ('/api/spreadsheets/decide','/api/spreadsheets/apply-fixes'):
            data=self.rfile.read(int(self.headers.get('Content-Length','0')));payload=json.loads(data);self.rfile=io.BytesIO(data)
            if self.path.endswith('/decide'):
                single=payload.get('file_hash')==config['decision_hash'] and payload.get('ids') in ([config['ids'][0]],[config['ids'][1]]) and payload.get('action') in ('reject','defer')
                batch=payload.get('file_hash')==config['group_hash'] and sorted(payload.get('ids',[]))==sorted(config['group_ids']) and payload.get('action')=='reject'
                if not(single or batch):
                    return self.fail(409,'Only the named synthetic decision scope is allowed.')
                decisions.append(payload)
            else:
                changes=payload.get('changes') or []
                if payload.get('file_hash')!=config['manual_hash'] or len(changes)!=1 or any(changes[0].get(k)!=config[k] for k in ('sheet','cell','after')):
                    return self.fail(409,'Only the named single synthetic manual edit is allowed.')
                applies.append(payload)
        return super().do_POST()
server=ThreadingHTTPServer(('127.0.0.1',0),LocalHandler);server.workspace_store=store
threading.Thread(target=server.serve_forever,daemon=True).start()
try:
    result=subprocess.run(['node',str(ROOT/'tests/browser_direct_review_actions.js'),f'http://127.0.0.1:{server.server_port}',json.dumps(config),str(WIDTH)],text=True,capture_output=True,
      env={k:os.environ[k] for k in ('HOME','PATH','LANG','PLAYWRIGHT_MODULE','CHROMIUM_EXECUTABLE') if k in os.environ},timeout=100)
    print(result.stdout);print(result.stderr,file=sys.stderr);assert result.returncode==0,result.returncode
    with store.session() as directory:
        box=SpreadsheetInbox(directory);saved=box.get(config['decision_hash']);old=box.get(config['manual_hash'])
        assert box.download(config['decision_hash'])[1]==raw and box.download(config['manual_hash'])[1]==manual_raw
        group=box.get(config['group_hash']);assert box.download(config['group_hash'])[1]==group_raw
        assert len(group['finding_decision_log'])==1 and set(group['finding_decisions'])==set(config['group_ids'])
        assert all(group['finding_decisions'][key]['action']=='reject' for key in config['group_ids'])
        assert len(saved['finding_decision_log'])==2
        assert saved['finding_decisions'][config['ids'][0]]['action']=='reject'
        assert saved['finding_decisions'][config['ids'][1]]['action']=='defer'
        assert any(h['code']=='awaiting_source_owner' for h in box.holds(saved))
        assert len(old['fixes_applied'])==1;new_hash=old['fixes_applied'][0]['fixed_file_hash'];new=box.get(new_hash)
        assert new['revision']==2 and len(new['fixed_from']['changes'])==1
        before=load_workbook(io.BytesIO(manual_raw));after=load_workbook(io.BytesIO(box.download(new_hash)[1]));diffs=[]
        for ws in before:
            for row in ws:
                for cell in row:
                    if cell.value!=after[ws.title][cell.coordinate].value:diffs.append((ws.title,cell.coordinate,cell.value,after[ws.title][cell.coordinate].value))
        assert diffs==[(config['sheet'],config['cell'],config['before'],config['after'])],diffs
        assert not new.get('sent') and not new.get('approval') and box.list()['count']==4
    assert len(decisions)==3 and len(applies)==1 and not external
    print(json.dumps(dict(passed=True,width=WIDTH,actual_handler=True,recorded_keep_and_defer=True,two_item_group_saved=True,unrelated_findings_unchanged=True,source_hold_preserved=True,manual_preview_cancel_preserved=True,one_explicit_cell_change=True,originals_unchanged=True,no_provider_traffic=True)))
finally:server.shutdown();server.server_close()
