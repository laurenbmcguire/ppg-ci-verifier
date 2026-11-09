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
raw=b'Date,Shift,Area,Part number,Identifier / lot,Inspected for,Quantity Inspected,Condition,Reject reason,Reject Quantity,Quantity Certified,Comments\n2026-11-09,1,A,Part-1,,,10,NOK,,2,8,\n'
format_name=sys.argv[2] if len(sys.argv)>2 else 'csv'
assert format_name in ('csv','xlsx')
if format_name=='xlsx':
    import csv
    from openpyxl import Workbook
    workbook=Workbook();workbook.active.title='Synthetic'
    for values in csv.reader(io.StringIO(raw.decode())):workbook.active.append(values)
    workbook.create_sheet('Other synthetic sheet')['A1']='Read only example'
    output=io.BytesIO();workbook.save(output);raw=output.getvalue()
with store.session() as directory:
    box=SpreadsheetInbox(directory); report=box.ingest('synthetic-row-context.'+format_name,raw)
    issues=summarize(report)['issues']; targets=[i for i in issues if i['code'] in ('missing_identifier','missing_defect_reason')]
    assert len(targets)==2 and all(not i.get('cell') and i.get('row')==2 for i in targets)
    config=dict(sheet=targets[0]['sheet'], file_hash=report['file_hash'], ids=[i['key'] for i in targets])
requests=[];applies=[]
class LocalHandler(handler):
    def log_message(self,*args):pass
    def do_POST(self):
        requests.append(self.path)
        if self.path not in ('/api/clients','/api/spreadsheets/sheet','/api/spreadsheets/cells','/api/spreadsheets/fixes','/api/spreadsheets/findings','/api/spreadsheets/apply-fixes'):
            return self.fail(409,'Synthetic row-context test is read only.')
        if self.path=='/api/spreadsheets/apply-fixes':
            body=self.rfile.read(int(self.headers.get('Content-Length','0')));payload=json.loads(body);self.rfile=io.BytesIO(body)
            changes=payload.get('changes') or []
            if payload.get('file_hash')!=config['file_hash'] or len(changes)!=1 or any(changes[0].get(k)!=v for k,v in dict(sheet=config['sheet'],cell='I2',before='',after='Synthetic source-confirmed reason',source='Your edit').items()):
                return self.fail(409,'Only the named synthetic blank defect cell can change.')
            applies.append(payload)
        return super().do_POST()
server=ThreadingHTTPServer(('127.0.0.1',0),LocalHandler);server.workspace_store=store
threading.Thread(target=server.serve_forever,daemon=True).start()
try:
    result=subprocess.run(['node',str(ROOT/'tests/browser_grid_manual.js'),f'http://127.0.0.1:{server.server_port}',json.dumps(config),str(WIDTH)],text=True,capture_output=True,
      env={k:os.environ[k] for k in ('HOME','PATH','LANG','PLAYWRIGHT_MODULE','CHROMIUM_EXECUTABLE') if k in os.environ},timeout=100)
    print(result.stdout);print(result.stderr,file=sys.stderr);assert result.returncode==0,result.returncode
    with store.session() as directory:
        box=SpreadsheetInbox(directory);assert box.download(config['file_hash'])[1]==raw
        old=box.get(config['file_hash']);assert len(old['fixes_applied'])==1 and len(applies)==1
        new=box.get(old['fixes_applied'][0]['fixed_file_hash']);assert new['revision']==2
        corrected=box.download(new['file_hash'])[1]
        from hosting.fixes import _sources
        before_rows=_sources('synthetic.'+format_name,raw)[0];after_rows=_sources('synthetic.'+format_name,corrected)[0]
        diffs=[]
        for (sheet,rows_before),(_,rows_after) in zip(before_rows,after_rows):
            for row_index,(before,after) in enumerate(zip(rows_before,rows_after),1):
                for col_index,(a,b) in enumerate(zip(before,after),1):
                    if a!=b:diffs.append((sheet,row_index,col_index,a,b))
        assert diffs==[(config['sheet'],2,9,None if format_name=='xlsx' else '', 'Synthetic source-confirmed reason')],diffs
        codes=[i['code'] for i in summarize(new)['issues']];assert 'missing_defect_reason' not in codes and 'missing_identifier' in codes
        assert not new.get('approval') and not new.get('sent')
    assert not external and '/api/spreadsheets/cells' in requests
    print(json.dumps(dict(passed=True,width=WIDTH,actual_handler=True,row_only_exact_headers=True,one_explicit_cell_applied=True,original_unchanged=True,no_provider_traffic=True)))
finally:server.shutdown();server.server_close()
