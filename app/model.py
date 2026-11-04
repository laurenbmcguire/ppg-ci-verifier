"""Local-only review state machine. No outbound integrations."""
import csv, hashlib, io, json, os, re, sqlite3
from datetime import datetime, timezone
from pathlib import Path

HEADERS = ['Client', 'Period', 'Topic', 'Prior volume', 'Volume']
FIXTURE = [['SYNTHETIC DEMO — not a real client', '2026-10-12', 'M0tor launch', '100', '168'],
           ['SYNTHETIC DEMO — not a real client', '2026-10-12', 'Dealer network', '80', '82'],
           ['SYNTHETIC DEMO — not a real client', '2026-10-12', 'Service parts', '0', '24']]
def digest(b): return hashlib.sha256(b).hexdigest()
def now(): return datetime.now(timezone.utc).isoformat()
def canonical(x): return json.dumps(x, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
def csv_bytes(rows, headers=HEADERS):
    f = io.StringIO(newline=''); w = csv.writer(f); w.writerow(headers); w.writerows(rows)
    return f.getvalue().encode('utf-8-sig')
def checks(rows):
    result = []
    for i, row in enumerate(rows, 2):
        for j, value in enumerate(row):
            cell = f'{chr(65+j)}{i}'
            if re.search(r'[A-Za-z]0|0[A-Za-z]', value):
                result.append(dict(cell=cell, kind='judgment', message='Possible zero / letter O ambiguity. Check source; no automatic replacement.'))
            if value.lstrip().startswith(('=', '+', '-', '@')):
                result.append(dict(cell=cell, kind='blocking', message='Formula-like text is not supported by this CSV prototype.'))
        try:
            date = datetime.strptime(row[1], '%Y-%m-%d').date()
            if date.isoformat() >= '2026-10-19': raise ValueError()
        except ValueError:
            result.append(dict(cell=f'B{i}',kind='blocking', message='Use a valid historical date before October 19, 2026.'))
        try:
            prior, volume = int(row[3]), int(row[4])
            if prior < 0 or volume < 0: raise ValueError()
            if (prior == 0 and volume > 0) or (prior > 0 and abs(volume-prior)/prior >= .30):
                result.append(dict(cell=f'E{i}',kind='judgment',message=('New volume from zero baseline' if prior == 0 else f'{(volume-prior)/prior:+.0%} versus prior volume') + '; human judgment requested. Explanation optional.'))
        except ValueError:
            result.append(dict(cell=f'E{i}',kind='blocking',message='Volume and prior volume must be whole, nonnegative numbers. Ambiguous input is preserved.'))
    return result

class Conflict(Exception): pass

class Review:
    def __init__(self, root, mode="synthetic"):
        self.mode=mode
        self.config=json.loads((Path(__file__).resolve().parents[1]/"fixtures/historical.json").read_text()) if mode=="historical" else None
        self.headers=self.config["headers"] if self.config else HEADERS
        self.label=self.config["label"] if self.config else "SYNTHETIC FIXTURE — NOT A REAL CLIENT REPORT"
        self.prefix="HISTORICAL-DEMO" if self.config else "SYNTHETIC-DEMO"
        self.date=self.config["date"] if self.config else "2026-10-12"
        self.source_path="original/HISTORICAL_September29_original.xlsx" if self.config else "original/SYNTHETIC_original.csv"
        self.root=Path(root); self.root.mkdir(parents=True, exist_ok=True)
        self.db=self.root/'review.sqlite3'
        with self.connect() as c:
            c.executescript('CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY, data TEXT); CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY, at TEXT, event TEXT, detail TEXT);')
            if not c.execute('SELECT 1 FROM state').fetchone():
                rows=self.config['rows'] if self.config else FIXTURE
                original=(Path(__file__).resolve().parents[1]/self.config['source_file']).read_bytes() if self.config else self.encode(rows)
                if self.config and digest(original)!=self.config['source_hash']: raise Conflict('Historical source differs from verified manifest.')
                p=self.root/'original'; p.mkdir(exist_ok=True)
                path=self.root/self.source_path
                if path.exists() and path.read_bytes()!=original: raise Conflict('Existing original differs; recovery required.')
                if not path.exists(): path.write_bytes(original); path.chmod(0o444)
                s=dict(mode=self.mode, label=self.label, headers=self.headers, coordinates=self.config['coordinates'] if self.config else [[f'{chr(65+j)}{i+2}' for j in range(5)] for i in range(3)], metadata=self.config, source_path=self.source_path, grid_source_hash=digest(self.encode(rows)), rows=rows, original=rows, source_hash=digest(original), revision=1, status='unreviewed', validated=None, approval=None, request=None, simulated_report=None)
                s['hash']=digest(self.encode(s['rows'])); self.save(c,s); self.log(c,'initialized',{'mode':self.mode,'source_hash':s['source_hash']})
    def encode(self, rows):
        if not self.config: return csv_bytes(rows)
        metadata_headers=['Demonstration scope','Reporting date','Shift','Worksite','Source row']
        enriched=[[self.label,self.date,self.config['shift'],self.config['site'],str(16+i)]+r for i,r in enumerate(rows)]
        return csv_bytes(enriched,metadata_headers+self.headers)
    def checks(self, rows):
        if not self.config: return checks(rows)
        result=[dict(cell='K14',kind='judgment',message='Source header reads Rework Quanity. Possible typo: Quantity. Original header is retained.'),dict(cell='L50',kind='judgment',message='Unlabelled source L50 computes inspected minus rejected. Its original result (3598) equals the reported Rework Quanity total, but the workbook does not label it accepted. Human confirmation is required before classifying acceptance; no reclassification is made.'),dict(cell='G16:G18',kind='judgment',message='No comparable prior-period volume baseline supplied. No trend or volume-change conclusion is made.')]
        for i,row in enumerate(rows):
            coords=self.config['coordinates'][i]
            if sum(r[0]==row[0] for r in rows)>1: result.append(dict(cell=coords[0],kind='judgment',message='Duplicate source identifier retained. Physical source row is the stable record identity; no deduplication or renumbering.'))
            for j,v in enumerate(row):
                if re.search(r'[A-Za-z]0|0[A-Za-z]',v):result.append(dict(cell=coords[j],kind='judgment',message='Possible zero / letter O ambiguity. Human review only.'))
                if v.lstrip().startswith(('=','+','-','@')):result.append(dict(cell=coords[j],kind='blocking',message='Formula-like text is not supported in the review extraction.'))
            try:
                inspected,rejected,reworked=[int(row[j]) for j in (3,6,7)]
                if min(inspected,rejected,reworked)<0 or rejected>inspected or reworked>inspected:raise ValueError()
                if inspected!=rejected+reworked:result.append(dict(cell=coords[3],kind='judgment',message='Quantity Inspected differs from Reject Quantity plus source-labeled Rework Quanity. This is a comparison, not an asserted business identity; verify meanings and counts without automatic correction.'))
                if rejected and inspected and rejected/inspected>=.10:result.append(dict(cell=coords[6],kind='judgment',message='Reject share exceeds illustrative 10% flag. Human judgment; explanation optional.'))
            except ValueError:result.append(dict(cell=coords[3],kind='blocking',message='Quantities must be nonnegative whole numbers; rejected/reworked cannot exceed inspected.'))
        return result
    def report(self, rows, approval):
        if not self.config:return dict(label='SYNTHETIC SIMULATION — NOT A CLIENT REPORT — UNPUBLISHED',approved_input=approval,total_volume=sum(int(r[4]) for r in rows),rows=len(rows))
        values=[r[5:] for r in rows]
        inspected=sum(int(r[3]) for r in values); rejected=sum(int(r[6]) for r in values)
        return dict(label=self.label+' — SIMULATED REPORT — UNPUBLISHED',approved_input=approval,date=self.date,shift=self.config['shift'],site=self.config['site'],source_sheet=self.config['sheet'],source_library_id=self.config['library_file_id'],identifiers=[r[0] for r in values],inspected=inspected,rejected=rejected,source_rework_quantity=sum(int(r[7]) for r in values),source_quantity_labels={'inspected':'Quantity Inspected','rejected':'Reject Quantity','source_rework_quantity':'Rework Quanity'},unclassified_inspected_minus_rejected=inspected-rejected,acceptance_classification='UNCONFIRMED: source L50 has no acceptance label; equality with Rework Quanity does not establish accepted status.',rows=len(values))
    def connect(self):
        c=sqlite3.connect(self.db, timeout=10); c.execute('PRAGMA synchronous=FULL'); return c
    def log(self,c,event,detail): c.execute('INSERT INTO audit(at,event,detail) VALUES(?,?,?)',(now(),event,canonical(detail)))
    def save(self,c,s):
        s['working_path']=self.artifact('working',f"IP_{self.prefix}_v{s['revision']}_{s['hash'][:12]}.csv",self.encode(s['rows']))
        c.execute('INSERT OR REPLACE INTO state VALUES(1,?)',(canonical(s),))
    def load(self,c):
        s=json.loads(c.execute('SELECT data FROM state WHERE id=1').fetchone()[0])
        if self.config and s.get('metadata')!=self.config: raise Conflict('Historical review metadata changed. Start a new isolated session and revalidate; prior approval cannot be reused.')
        if digest((self.root/self.source_path).read_bytes())!=s['source_hash'] or digest(self.encode(s['original']))!=s.get('grid_source_hash',s['source_hash']):
            raise Conflict('Original source integrity mismatch. Fail closed; preserve files for recovery.')
        if s.get('mode','synthetic')!=self.mode: raise Conflict('Dataset mode mismatch. Use a separate session directory.')
        if digest(self.encode(s['rows']))!=s['hash']: raise Conflict('Working version integrity mismatch. Fail closed.')
        if s.get('working_path') and digest((self.root/s['working_path']).read_bytes())!=s['hash']: raise Conflict('Working spreadsheet snapshot integrity mismatch. Fail closed.')
        if s['approval'] and (s['approval']['hash']!=s['hash'] or s['approval']['revision']!=s['revision']): raise Conflict('Approval binding mismatch. Fail closed.')
        if s['approval'] and digest((self.root/s['approval']['path']).read_bytes())!=s['hash']: raise Conflict('Approved artifact integrity mismatch. Fail closed.')
        return s
    def view(self):
        with self.connect() as c:
            s=self.load(c); s['checks']=self.checks(s['rows']); s['audit']=[dict(id=r[0],at=r[1],event=r[2],detail=json.loads(r[3])) for r in c.execute('SELECT * FROM audit ORDER BY id')]
            return s
    def artifact(self, subdir, name, data):
        directory=self.root/subdir; directory.mkdir(exist_ok=True); path=directory/name
        if path.exists():
            if path.read_bytes()!=data: raise Conflict('Existing artifact differs. Fail closed; do not overwrite.')
        else:
            temp=directory/(name+'.tmp'); temp.write_bytes(data)
            with temp.open('rb') as f: os.fsync(f.fileno())
            os.replace(temp,path); path.chmod(0o444)
        return str(path.relative_to(self.root))
    def act(self, action, expected, payload=None):
        payload=payload or {}
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE'); s=self.load(c)
            if expected!=s['revision']: raise Conflict('Stale version. Reload and review the current version.')
            if action=='edit':
                row,col=payload.get('row'),payload.get('col'); value=payload.get('value')
                if type(row)!=int or type(col)!=int or not 0<=row<len(s['rows']) or not 0<=col<len(self.headers) or not isinstance(value,str) or len(value)>500: raise Conflict('Invalid cell edit.')
                old=s['rows'][row][col]
                if old==value: return s
                s['rows'][row][col]=value; s['revision']+=1; s['hash']=digest(self.encode(s['rows'])); s.update(status='in_progress',validated=None,approval=None,simulated_report=None,request=None)
                self.log(c,'cell_edited',dict(cell=s.get('coordinates',[[f'{chr(65+j)}{i+2}' for j in range(5)] for i in range(3)])[row][col],before=old,after=value,revision=s['revision'],hash=s['hash'],approval_invalidated=True))
            elif action=='validate':
                s['validated']=s['hash']; self.log(c,'validated',dict(hash=s['hash'],revision=s['revision'],checks=self.checks(s['rows'])))
            elif action=='approve':
                if payload.get('role')!='reviewer': raise Conflict('Reviewer role required (local demo identity only).')
                reviewer=payload.get('reviewer','')
                if not isinstance(reviewer,str) or not reviewer.strip() or len(reviewer)>80: raise Conflict('Assign a simulated reviewer before approval.')
                if s['approval']: return s
                if s['validated']!=s['hash']: raise Conflict('Revalidate the exact working version first.')
                if any(x['kind']=='blocking' for x in self.checks(s['rows'])): raise Conflict('Resolve blocking structure/type checks; ambiguous values remain unchanged.')
                if payload.get('reviewed') is not True: raise Conflict('Confirm review of flagged cells. No explanation is required.')
                name=f"AP_{self.prefix}_{self.date}_v{s['revision']}_{s['hash'][:12]}.csv"
                path=self.artifact('approved',name,self.encode(s['rows']))
                aid=c.execute("SELECT count(*) FROM audit WHERE event='approved'").fetchone()[0]+1
                a=dict(id=aid,path=path,hash=s['hash'],revision=s['revision'],source_hash=s['source_hash'],reviewer=reviewer.strip()+' (SIMULATED identity, not authenticated)',at=now(),synthetic=self.config is None,demonstration=True,scope=self.label)
                if self.config:a['source_manifest']={k:self.config[k] for k in ['library_file_id','library_version','filename','sheet','date','shift','site','project','coordinates','quantity_semantics']}
                s.update(approval=a,status='approved'); self.log(c,'approved',a)
                self.artifact('approved',name+f'.approval-{aid}.audit.json',canonical(dict(approval=a,source=self.source_path,history=[dict(at=r[0],event=r[1],detail=json.loads(r[2])) for r in c.execute('SELECT at,event,detail FROM audit ORDER BY id')])).encode())
            elif action=='generate':
                a=s['approval']
                if not a: raise Conflict('Only a current approved artifact can feed the simulation.')
                data=(self.root/a['path']).read_bytes()
                if digest(data)!=a['hash']: raise Conflict('Approved artifact integrity mismatch. Fail closed.')
                if s['simulated_report']: return s
                # Deliberately read only the approved bytes, never the working grid.
                rows=list(csv.reader(io.StringIO(data.decode('utf-8-sig'))))[1:]
                report=self.report(rows,a)
                s['simulated_report']=self.artifact('simulated_reports',f"SIMULATED_{s['hash'][:12]}_approval-{a['id']}.json",canonical(report).encode()); self.log(c,'report_simulated',dict(path=s['simulated_report'],hash=a['hash'],published=False))
            elif action=='request':
                if not s['request']:
                    s['request']=dict(id=f"review-{s['revision']}-{s['hash'][:12]}",at=now(),status='unanswered',channel='SIMULATED private in-app inbox',sent=False)
                    self.log(c,'review_request_simulated',s['request'])
            elif action=='timeout':
                if not s['request']: raise Conflict('Create a simulated request first.')
                if s['request']['status']=='unanswered':
                    s['request']['status']='reminder_simulated'; self.log(c,'reminder_simulated',dict(sent=False,auto_approved=False))
            elif action=='leave':
                s.update(status='unreviewed',approval=None,validated=None,simulated_report=None)
                self.log(c,'left_unreviewed',dict(revision=s['revision'],published=False))
            elif action=='download':
                if any(v.lstrip().startswith(('=','+','-','@')) for row in s['rows'] for v in row): raise Conflict('Formula-like text cannot be safely downloaded as CSV. Review the flagged cell first; source is unchanged.')
                self.log(c,'unapproved_copy_downloaded',dict(revision=s['revision'],hash=s['hash'],published=False))
            else: raise Conflict('Unknown action.')
            self.save(c,s); return s
    def download(self, revision):
        s=self.act('download',revision)
        # Permanent watermark column keeps privately exported copies distinct from approved bytes.
        source=list(csv.reader(io.StringIO(self.encode(s['rows']).decode('utf-8-sig'))))
        f=io.StringIO(newline=''); w=csv.writer(f); w.writerow(['Status']+source[0])
        w.writerows([['UNAPPROVED — '+self.label]+r for r in source[1:]])
        return f.getvalue().encode('utf-8-sig')
