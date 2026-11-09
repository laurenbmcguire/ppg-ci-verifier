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
    output=io.BytesIO();workbook.save(output);raw=output.getvalue()
with store.session() as directory:
    box=SpreadsheetInbox(directory); report=box.ingest('synthetic-row-context.'+format_name,raw)
    issues=summarize(report)['issues']; targets=[i for i in issues if i['code'] in ('missing_identifier','missing_defect_reason')]
    assert len(targets)==2 and all(not i.get('cell') and i.get('row')==2 for i in targets)
    config=dict(sheet=targets[0]['sheet'], file_hash=report['file_hash'], ids=[i['key'] for i in targets])
requests=[]
class LocalHandler(handler):
    def log_message(self,*args):pass
    def do_POST(self):
        requests.append(self.path)
        if self.path not in ('/api/clients','/api/spreadsheets/sheet','/api/spreadsheets/cells','/api/spreadsheets/fixes','/api/spreadsheets/findings'):
            return self.fail(409,'Synthetic row-context test is read only.')
        return super().do_POST()
server=ThreadingHTTPServer(('127.0.0.1',0),LocalHandler);server.workspace_store=store
threading.Thread(target=server.serve_forever,daemon=True).start()
try:
    result=subprocess.run(['node',str(ROOT/'tests/browser_manual_row_context.js'),f'http://127.0.0.1:{server.server_port}',json.dumps(config),str(WIDTH)],text=True,capture_output=True,
      env={k:os.environ[k] for k in ('HOME','PATH','LANG','PLAYWRIGHT_MODULE','CHROMIUM_EXECUTABLE') if k in os.environ},timeout=100)
    print(result.stdout);print(result.stderr,file=sys.stderr);assert result.returncode==0,result.returncode
    with store.session() as directory:
        box=SpreadsheetInbox(directory);assert box.download(config['file_hash'])[1]==raw;assert not box.get(config['file_hash']).get('fixes_applied')
    assert not external and '/api/spreadsheets/cells' in requests
    print(json.dumps(dict(passed=True,width=WIDTH,actual_handler=True,row_only_exact_headers=True,original_unchanged=True,no_provider_traffic=True)))
finally:server.shutdown();server.server_close()
