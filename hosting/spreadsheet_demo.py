"""Synthetic-only XLSX review simulation. Call within a SQLiteWorkspace session.

The outer durable workspace transaction covers audit, inbox DB and new file bytes.
No authenticated approval, formula execution, source writes or delivery exist here.
"""
from contextlib import contextmanager
from decimal import Decimal
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import xml.etree.ElementTree as ET
from zipfile import ZipFile

from hosting.branded_reports import QUANTITIES, SHIFT_SHEETS, quantity
from hosting.report_workflow import canonical, fingerprint, now, WorkflowError, WorkflowConflict
from hosting.spreadsheet_inbox import SpreadsheetInbox
from hosting.sqlite_storage import local_environment

EXAMPLES = Path(__file__).resolve().parents[1] / 'samples' / 'development_examples'
NS = {'m':'http://schemas.openxmlformats.org/spreadsheetml/2006/main', 'r':'http://schemas.openxmlformats.org/officeDocument/2006/relationships'}
NOTICE = ('SYNTHETIC DEVELOPMENT SIMULATION ONLY. Accepted changes create a new workbook; originals and source stay unchanged. '
          'Formula expressions are preserved but cached results are cleared; Excel must recalculate. Unsupported totals and missing evidence remain held. '
          'No authenticated human approval or customer release.')


def digest(raw):return hashlib.sha256(raw).hexdigest()
def selection(ids):
    if not isinstance(ids,list) or not 1<=len(ids)<=500 or not all(isinstance(i,str) for i in ids) or len(set(ids))!=len(ids):
        raise WorkflowError('Select 1 to 500 distinct explicit proposal IDs.')


def sheet_paths(raw):
    with ZipFile(BytesIO(raw)) as z:
        if len(z.namelist())!=len(set(z.namelist())):raise WorkflowError('Duplicate workbook package entries are unsupported.')
        rels={r.attrib['Id']:r.attrib['Target'] for r in ET.fromstring(z.read('xl/_rels/workbook.xml.rels'))}
        result={}
        for sheet in ET.fromstring(z.read('xl/workbook.xml')).findall('m:sheets/m:sheet',NS):
            target=rels[sheet.attrib['{'+NS['r']+'}id']]
            path=target.lstrip('/') if target.startswith('/') else 'xl/'+target
            if '..' in PurePosixPath(path).parts or path not in z.namelist():raise WorkflowError('Unsupported worksheet relationship.')
            result[sheet.attrib['name']]=path
        return result


def numeric_cell(raw,path,cell):
    with ZipFile(BytesIO(raw)) as z:root=ET.fromstring(z.read(path))
    matches=[c for c in root.findall('.//m:sheetData/m:row/m:c',NS) if c.get('r')==cell]
    if len(matches)!=1:raise WorkflowError('An exact existing numeric cell is required.')
    c=matches[0];v=c.find('m:v',NS)
    if c.find('m:f',NS) is not None or c.get('t','n')!='n' or v is None or v.text is None:raise WorkflowError('Formula, blank, and text cells cannot be changed by this simulation.')
    try:n=Decimal(v.text)
    except Exception as exc:raise WorkflowError('Unsupported numeric cell.') from exc
    if not n.is_finite() or n<0 or n>10**15 or n!=n.to_integral_value():raise WorkflowError('Only known nonnegative integer quantities can be changed.')
    return int(n)


def corrected_xlsx(raw,proposals):
    paths=sheet_paths(raw);edits={}
    for p in proposals:
        path=paths.get(p['sheet'])
        if not path or numeric_cell(raw,path,p['cell'])!=p['before']:raise WorkflowConflict('Workbook cell differs from reviewed proposal.')
        edits.setdefault(path,{})[p['cell']]=p['after']
    out=BytesIO();changed=set()
    with ZipFile(BytesIO(raw)) as src, ZipFile(out,'w') as dst:
        for info in src.infolist():
            data=src.read(info.filename)
            if info.filename in paths.values():
                xml=data.decode('utf-8')
                def cell_edit(match):
                    block=match.group(0);address=re.search(r'\br="([A-Z]+[1-9][0-9]*)"',block)
                    if not address:return block
                    address=address.group(1)
                    if address in edits.get(info.filename,{}):
                        block,count=re.subn(r'<v>[^<]*</v>', '<v>'+str(edits[info.filename][address])+'</v>',block)
                        if count!=1:raise WorkflowError('Numeric cell XML is not supported; nothing changed.')
                        changed.add((info.filename,address))
                    if re.search(r'<f(?:\s|>)',block):block=re.sub(r'<v(?:\s[^>]*)?>[^<]*</v>|<v\s*/>','',block)
                    return block
                data=re.sub(r'<c\b[^>]*>.*?</c>',cell_edit,xml,flags=re.S).encode('utf-8')
            elif info.filename=='xl/workbook.xml':
                xml=data.decode('utf-8');calc='<calcPr calcId="0" fullCalcOnLoad="1" forceFullCalc="1" calcMode="auto"/>'
                if re.search(r'<calcPr\b',xml):xml,count=re.subn(r'<calcPr\b[^>]*/>',calc,xml)
                else:xml=xml.replace('</workbook>',calc+'</workbook>');count=1
                if count!=1:raise WorkflowError('Workbook calculation settings are unsupported.')
                data=xml.encode('utf-8')
            dst.writestr(info,data)
    if len(changed)!=len(proposals):raise WorkflowError('Not every selected cell could be updated; nothing changed.')
    return out.getvalue()


class SpreadsheetDemo:
    def __init__(self,root,enabled=False):
        if enabled is not True or os.environ.get('CI_LOCAL_DEV')!='1' or os.environ.get('CI_DEVELOPMENT_DEMO')!='1' or not local_environment():
            raise WorkflowError('Synthetic workbook simulation is disabled.')
        self.root=Path(root);self.inbox=SpreadsheetInbox(root);self.audit_root=self.root/'simulated_reports';self.audit_root.mkdir(exist_ok=True);self.database=self.audit_root/'spreadsheet_demo.sqlite3'
        with self.connect() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS decisions (binding TEXT NOT NULL, proposal_id TEXT NOT NULL, detail TEXT NOT NULL, PRIMARY KEY(binding,proposal_id));
            CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT,file_hash TEXT NOT NULL,detail TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS drafts (file_hash TEXT PRIMARY KEY,parent_hash TEXT NOT NULL,fixture_hash TEXT NOT NULL,source_hash TEXT NOT NULL,filename TEXT NOT NULL);''')
    @contextmanager
    def connect(self):
        db=sqlite3.connect(str(self.database),timeout=15);db.row_factory=sqlite3.Row
        try:
            with db:yield db
        finally:db.close()
    def log(self,db,artifact_hash,event,**values):
        detail=dict(event=event,at=now(),actor='development-demo-simulator',simulation_only=True,identity_authenticated=False,release_status='held',**values)
        db.execute('INSERT INTO events(file_hash,detail) VALUES(?,?)',(artifact_hash,canonical(detail)))
    def fixture(self,file_hash,db):
        index=json.loads((EXAMPLES/'index.json').read_text())
        if index.get('synthetic') is not True:raise WorkflowError('Synthetic example index required.')
        draft=db.execute('SELECT * FROM drafts WHERE file_hash=?',(file_hash,)).fetchone()
        fixture_hash=draft['fixture_hash'] if draft else file_hash
        example=next((e for e in index['examples'] if e.get('synthetic') is True and e['artifact']['sha256']==fixture_hash and e.get('source')),None)
        if not example:raise WorkflowError('Only exact packaged synthetic workbooks and their audited drafts can use demo corrections.')
        for key in ('artifact','source'):
            name=example[key]['path']
            if Path(name).name!=name:raise WorkflowError('Invalid packaged fixture path.')
            if digest((EXAMPLES/name).read_bytes())!=example[key]['sha256']:raise WorkflowError('Packaged synthetic fixture integrity mismatch.')
        source=json.loads((EXAMPLES/example['source']['path']).read_text())
        if source.get('source_sha256')!=fingerprint({k:v for k,v in source.items() if k!='source_sha256'}):raise WorkflowError('Synthetic source integrity mismatch.')
        if draft and draft['source_hash']!=source['source_sha256']:raise WorkflowConflict('Draft source no longer matches its packaged fixture.')
        return example,source
    def state(self,db,file_hash):
        if not isinstance(file_hash,str) or not re.fullmatch('[0-9a-f]{64}',file_hash):raise WorkflowError('Exact workbook hash required.')
        example,source=self.fixture(file_hash,db);item=self.inbox.get(file_hash)
        if item.get('format')!='xlsx' or item.get('source_sha256')!=source['source_sha256']:raise WorkflowConflict('Reconcile this exact synthetic workbook with its packaged source before reviewing changes.')
        with self.inbox.connect() as inbox_db:
            stored=inbox_db.execute('SELECT source_json FROM spreadsheet_sources WHERE source_hash=?',(source['source_sha256'],)).fetchone()
        if not stored or stored[0]!=canonical(source):raise WorkflowConflict('Stored comparison source differs from the packaged synthetic source.')
        raw=(self.root/item['original_path']).read_bytes()
        if digest(raw)!=file_hash:raise WorkflowConflict('Workbook integrity mismatch.')
        binding={k:item[k] for k in ('file_hash','check_hash','source_sha256')};binding_hash=fingerprint(binding);proposals=[];paths=sheet_paths(raw)
        # Deliberately narrow: only explicit PPG quantity mappings. No arbitrary cell edits.
        if str(item.get('template_id','')).startswith('ppg-daily-'):
            for d in item.get('source_differences',[]):
                if d.get('code')!='source_value_difference' or d.get('sheet') not in SHIFT_SHEETS or d.get('field') not in QUANTITIES:continue
                before,after=d.get('before'),d.get('source_value')
                if quantity(before) is None or quantity(after) is None or type(d.get('row')) is not int:continue
                column=QUANTITIES[d['field']]+1;cell=chr(64+column)+str(d['row'])
                if not any(f.get('code')=='source_value_difference' and f.get('sheet')==d['sheet'] and f.get('row')==d['row'] and f.get('column')==column for f in item.get('findings',[])):continue
                try:actual=numeric_cell(raw,paths[d['sheet']],cell)
                except WorkflowError:continue
                if actual!=before:continue
                p=dict(**binding,sheet=d['sheet'],row=d['row'],column=column,cell=cell,field=d['field'],before=before,after=after)
                proposals.append(dict(p,id=fingerprint(p),simulation_only=True))
        if len({(p['sheet'],p['cell']) for p in proposals})!=len(proposals):raise WorkflowError('Ambiguous proposals cannot be edited.')
        child=db.execute('SELECT file_hash FROM drafts WHERE parent_hash=? ORDER BY rowid DESC LIMIT 1',(file_hash,)).fetchone()
        return dict(**binding,current_version=child is None,superseded_by=child['file_hash'] if child else None,
            superseded_download_url=('/api/spreadsheets/demo/download?id='+child['file_hash']) if child else None,eligible=True,simulation_only=True,approval_effective=False,release_status='held',source_write_enabled=False,send_enabled=False,
            proposals=proposals,decisions=[json.loads(r[0]) for r in db.execute('SELECT detail FROM decisions WHERE binding=?',(binding_hash,))],
            download_url=('/api/spreadsheets/demo/download?id='+file_hash) if db.execute('SELECT 1 FROM drafts WHERE file_hash=?',(file_hash,)).fetchone() else None,
            events=[json.loads(r[0]) for r in db.execute('SELECT detail FROM events WHERE file_hash=? ORDER BY seq DESC LIMIT 100',(file_hash,))],notice=NOTICE),item,raw,example,source
    def get(self,file_hash):
        try:
            with self.connect() as db:return self.state(db,file_hash)[0]
        except (WorkflowError,WorkflowConflict) as exc:
            return dict(file_hash=file_hash,eligible=False,simulation_only=True,proposals=[],decisions=[],events=[],release_status='held',approval_effective=False,notice=str(exc)+' '+NOTICE)
    def checked(self,db,payload):
        state,item,raw,example,source=self.state(db,payload.get('file_hash'))
        if not state['current_version']:raise WorkflowConflict('A corrected draft already superseded this workbook; open the latest draft before reviewing or applying again.')
        if any(payload.get(k)!=state[k] for k in ('check_hash','source_sha256')):raise WorkflowConflict('Workbook comparison or source changed; reload before deciding or applying.')
        ids=payload.get('proposal_ids');selection(ids);available={p['id']:p for p in state['proposals']}
        if any(i not in available for i in ids):raise WorkflowConflict('Unknown or stale proposal selection; nothing changed.')
        return state,item,raw,example,source,[available[i] for i in ids]
    def decide(self,payload):
        if set(payload)!={'file_hash','check_hash','source_sha256','proposal_ids','decision'} or payload.get('decision') not in ('approve','deny','hold'):raise WorkflowError('Exact bindings, explicit proposal IDs and approve/deny/hold required; identity fields are not accepted.')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE');state,_,_,_,_,proposals=self.checked(db,payload)
            binding={k:state[k] for k in ('file_hash','check_hash','source_sha256')}
            for p in proposals:
                d=dict(**binding,proposal_id=p['id'],decision=payload['decision'],actor='development-demo-simulator',simulation_only=True,identity_authenticated=False,at=now())
                db.execute('INSERT OR REPLACE INTO decisions(binding,proposal_id,detail) VALUES(?,?,?)',(fingerprint(binding),p['id'],canonical(d)))
                self.log(db,state['file_hash'],'spreadsheet_demo_decision',**binding,proposal_id=p['id'],decision=payload['decision'])
            return self.state(db,state['file_hash'])[0]
    def apply(self,payload):
        if set(payload)!={'file_hash','check_hash','source_sha256','proposal_ids'}:raise WorkflowError('Exact bindings and explicit accepted proposal IDs required.')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE');state,item,raw,example,source,proposals=self.checked(db,payload)
            accepted={d['proposal_id'] for d in state['decisions'] if d['decision']=='approve' and d.get('simulation_only') is True and d.get('identity_authenticated') is False}
            if any(p['id'] not in accepted for p in proposals):raise WorkflowConflict('Every selected proposal must be demo-accepted; rejected, held and undecided proposals cannot apply.')
            new_raw=corrected_xlsx(raw,proposals);new_hash=digest(new_raw)
            filename='DEMO_UNAPPROVED_'+new_hash[:12]+'.xlsx'
            draft=self.inbox.ingest(filename,new_raw);draft=self.inbox.reconcile(new_hash,source)
            if draft.get('formula_sha256')!=item.get('formula_sha256'):raise WorkflowConflict('Formula integrity changed; draft cannot be saved.')
            db.execute('INSERT OR IGNORE INTO drafts(file_hash,parent_hash,fixture_hash,source_hash,filename) VALUES(?,?,?,?,?)',(new_hash,state['file_hash'],example['artifact']['sha256'],source['source_sha256'],filename))
            if digest((self.root/item['original_path']).read_bytes())!=state['file_hash']:raise WorkflowConflict('Original workbook integrity changed; draft cannot be saved.')
            self.log(db,new_hash,'spreadsheet_demo_draft_created',parent_file_hash=state['file_hash'],check_hash=state['check_hash'],source_sha256=source['source_sha256'],proposal_ids=payload['proposal_ids'],formula_caches_cleared=True,original_unchanged=digest((self.root/item['original_path']).read_bytes())==state['file_hash'])
            return dict(item=draft,review=self.state(db,new_hash)[0],download_url='/api/spreadsheets/demo/download?id='+new_hash)
    def download(self,file_hash):
        with self.connect() as db:
            record=db.execute('SELECT filename FROM drafts WHERE file_hash=?',(file_hash,)).fetchone()
            if not record:raise WorkflowError('Only audited synthetic corrected drafts can be downloaded here.')
            _,_,raw,_,_=self.state(db,file_hash)
            return raw,record['filename']
