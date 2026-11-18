"""Local end-to-end draft harness. NOT the hosted PostgreSQL/cron deployment.

Uses exact configured captures and the pinned native template. No source writes,
client communication, automatic evidence correction, or approval.
"""
import base64
from contextlib import contextmanager, closing
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
from hosting import source_first as sf, source_first_workbook as wb

class StateStore:
    def __init__(self,path):
        self.path=str(path)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS scan_draft_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
    @contextmanager
    def transaction(self,key):
        with closing(sqlite3.connect(self.path,timeout=30)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            raw=db.execute('SELECT value FROM scan_draft_state WHERE key=?',(key,)).fetchone()
            state=json.loads(raw[0]) if raw else {}
            yield state
            db.execute('INSERT INTO scan_draft_state VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,sf.canonical(state).decode()))

class NativeDraftPublisher:
    def __init__(self, output, mapping, header):
        self.output=Path(output);self.mapping=deepcopy(mapping);self.header=deepcopy(header)
    def lookup(self,op):
        path=self.output/op/'receipt.json'
        if not path.is_file():return None
        receipt=json.loads(path.read_text())
        for filename,key in [('draft.xlsx','workbook_sha256'),('checklist.json','checklist_sha256')]:
            if sf.digest((path.parent/filename).read_bytes())!=receipt[key]:raise ValueError('Published bytes changed.')
        return receipt
    def __call__(self,snapshot,op):
        found=self.lookup(op)
        if found:return found
        cap=snapshot['source_capture'];sf.verify_seal(cap)
        selected={r['id'] for r in snapshot['rows']}
        # Derive a new sealed shift-scoped view while retaining original full-day
        # capture in checklist; never mutate the original source snapshot.
        view=deepcopy(cap);view['records']=[r for r in cap['records'] if r['id'] in selected]
        view['photos']=[p for p in cap['photos'] if p['record_id'] in selected]
        view=sf.seal({k:v for k,v in view.items() if k not in ('snapshot_hash','envelope_hash')})
        rows,findings=sf.reviewed_rows(view,self.mapping,[])
        head=wb.header(self.header,view);contract=wb.contract()
        bind=sf.binding(view,self.mapping,[],head,contract['clean_sha256'],sf.digest(contract),wb.generator_hash())
        blobs={k:base64.b64decode(v,validate=True) for k,v in snapshot.get('photo_blobs',{}).items()}
        raw,verification=wb.build(rows,head,view,bind,blobs)
        checklist=dict(operation_id=op,scope=snapshot['scope'],source_snapshot_hash=cap['snapshot_hash'],
                       scoped_snapshot_hash=view['snapshot_hash'],captured_at=cap['captured_at'],
                       workbook_sha256=sf.digest(raw),source_ids=sorted(selected),findings=findings,
                       layout_findings=wb.scope_findings(rows,head,view,contract),verification=verification,
                       day_completion='not_established',client_release='held',status='DRAFT',
                       timing_note='One-hour quiet-window draft; not proof of final scan or complete shift.',
                       photo_evidence=view['photos'])
        body=sf.canonical(checklist)
        target=self.output/op
        target.mkdir(parents=True,exist_ok=False) # uncertain partial write requires reconcile; never overwrite
        (target/'draft.xlsx').write_bytes(raw);(target/'checklist.json').write_bytes(body)
        notes=['PPG DRAFT — not approved for client release','Workbook SHA-256: '+sf.digest(raw),
               'Shift/day completion is not established.','Source IDs: '+', '.join(sorted(selected)),
               'Verification issues:']+[f.get('text','') for f in findings+checklist['layout_findings']]
        (target/'checklist.txt').write_text('\n'.join(notes))
        receipt=dict(operation_id=op,workbook_sha256=sf.digest(raw),checklist_sha256=sf.digest(body),
                     path=str(target),status='draft_ready',client_release='held')
        (target/'receipt.json').write_bytes(sf.canonical(receipt))
        return receipt
