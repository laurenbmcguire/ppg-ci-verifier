"""Local end-to-end draft harness. NOT the hosted PostgreSQL/cron deployment.

Uses exact configured captures and the pinned native template. No source writes,
client communication, automatic evidence correction, or approval.
"""
import base64
from copy import deepcopy
import json
from pathlib import Path
from hosting import source_first as sf, source_first_workbook as wb
from hosting.last_scan_store import SqliteStore
from hosting.last_scan_service import RendererRouter, binding as exact_binding

class StateStore(SqliteStore):
    """Local SQLite store. Same interface as the hosted store (hosting.last_scan_store): ``transaction(key)`` for one
    record, ``atomic(keys)`` for several records committed together, and content-addressed blobs."""

class NativeDraftPublisher:
    """One mapping + header + pinned template. With ``binding`` it serves exactly that client/site/list/job and
    refuses any other scope before writing anything."""
    def __init__(self, output, mapping, header, binding=None):
        self.output=Path(output);self.mapping=deepcopy(mapping);self.header=deepcopy(header)
        self.binding=exact_binding(binding) if binding is not None else None
    def render_version(self):
        return sf.digest(dict(mapping=self.mapping,header=self.header,contract=wb.contract(),generator=wb.generator_hash(),binding=self.binding))
    def _own(self,scope):
        if self.binding is not None and exact_binding(scope or {})!=self.binding:
            raise ValueError('This publisher is bound to another client/site/list/job.')
    # On this single local filesystem an absent operation folder means nothing was written, so the same operation
    # may be rendered again. A folder without its receipt is an interrupted write: that stays for a person.
    lookup_is_authoritative=True
    FILES=(('draft.xlsx','workbook_sha256'),('checklist.json','checklist_sha256'))
    def lookup(self,op):
        path=self.output/op/'receipt.json'
        if not path.is_file():
            if path.parent.exists():raise ValueError('Interrupted artifact write; reconcile by hand, never overwrite.')
            return None
        receipt=json.loads(path.read_text())
        if receipt.get('binding')!=self.binding:raise ValueError('Published draft belongs to another client/site/list/job.')
        for filename,key in self.FILES:
            if sf.digest((path.parent/filename).read_bytes())!=receipt[key]:raise ValueError('Published bytes changed.')
        return receipt
    def build(self,snapshot,op):
        """Pure: (files, receipt) for this operation. Writes nothing."""
        self._own(snapshot.get('scope'))
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
        checklist=dict(operation_id=op,scope=snapshot['scope'],binding=self.binding,render_version=self.render_version(),source_snapshot_hash=cap['snapshot_hash'],
                       scoped_snapshot_hash=view['snapshot_hash'],captured_at=cap['captured_at'],
                       workbook_sha256=sf.digest(raw),source_ids=sorted(selected),findings=findings,
                       layout_findings=wb.scope_findings(rows,head,view,contract),verification=verification,
                       day_completion='not_established',client_release='held',status='DRAFT',
                       timing_note='One-hour quiet-window draft; not proof of final scan or complete shift.',
                       photo_evidence=view['photos'])
        body=sf.canonical(checklist)
        notes=['PPG DRAFT — not approved for client release','Workbook SHA-256: '+sf.digest(raw),
               'Shift/day completion is not established.','Source IDs: '+', '.join(sorted(selected)),
               'Verification issues (%d). Nothing below was corrected, filled in or hidden:'%len(findings+checklist['layout_findings'])]
        notes+=[('Record '+str(f['record_id'])+': ' if f.get('record_id') else '')+f.get('text','')+' ['+str(f.get('code'))+']' for f in findings+checklist['layout_findings']]
        notes+=['Photos named at the source but not captured:']+[f"Record {p['record_id']}: {p.get('reason')}" for p in view['photos'] if p.get('status')!='supported'] if any(p.get('status')!='supported' for p in view['photos']) else []
        receipt=dict(operation_id=op,workbook_sha256=sf.digest(raw),checklist_sha256=sf.digest(body),
                     render_version=self.render_version(),binding=self.binding,status='draft_ready',client_release='held')
        return {'draft.xlsx':raw,'checklist.json':body,'checklist.txt':'\n'.join(notes).encode()},receipt
    def __call__(self,snapshot,op):
        self._own(snapshot.get('scope'))
        found=self.lookup(op)
        if found:
            if found.get('render_version')!=self.render_version():
                raise ValueError('Render configuration changed; new operation required.')
            return found
        files,receipt=self.build(snapshot,op)
        target=self.output/op
        target.mkdir(parents=True,exist_ok=False) # uncertain partial write requires reconcile; never overwrite
        for name,body in files.items():(target/name).write_bytes(body)
        receipt=dict(receipt,path=str(target))
        (target/'receipt.json').write_bytes(sf.canonical(receipt))
        return receipt


class ArtifactStore:
    """Where finished drafts are kept. A hosted deployment needs private, durable storage behind this interface."""
    def lookup(self,key):raise NotImplementedError          # -> receipt dict, or None if nothing was committed
    def commit(self,key,files,receipt):raise NotImplementedError   # all files + receipt, or nothing
    def read(self,key,name):raise NotImplementedError       # -> bytes of one stored file


class BlobArtifacts(ArtifactStore):
    """Drafts kept in the runner's own durable store: files as content-addressed private blobs, and one record per
    draft written in a single transaction. No record means nothing was committed, so its lookup is authoritative."""
    def __init__(self,store):self.store=store
    def lookup(self,key):
        with self.store.transaction('artifact:'+key) as record:
            return deepcopy(record.get('receipt'))
    def commit(self,key,files,receipt):
        refs={name:self.store.put_blob(body) for name,body in files.items()}
        with self.store.transaction('artifact:'+key) as record:
            if record.get('receipt'):return deepcopy(record['receipt'])      # first writer wins; never overwritten
            record.update(receipt=receipt,files=refs)
            return deepcopy(receipt)
    def read(self,key,name):
        with self.store.transaction('artifact:'+key) as record:
            ref=(record.get('files') or {})[name]
        return self.store.get_blob(ref)


class StoredDraftPublisher(NativeDraftPublisher):
    """Same native draft, kept in an ArtifactStore instead of a local folder. Always bound to one exact scope."""
    def __init__(self,artifacts,mapping,header,binding):
        super().__init__('.',mapping,header,binding=binding)
        if self.binding is None:raise ValueError('A stored publisher must be bound to one client/site/list/job.')
        self.artifacts=artifacts;self.output=None
    def key(self,op):return sf.digest(self.binding)[:16]+':'+op
    def lookup(self,op):
        receipt=self.artifacts.lookup(self.key(op))
        if receipt is None:return None
        if receipt.get('binding')!=self.binding:raise ValueError('Published draft belongs to another client/site/list/job.')
        for filename,field in self.FILES:
            if sf.digest(self.artifacts.read(self.key(op),filename))!=receipt[field]:raise ValueError('Published bytes changed.')
        return receipt
    def read(self,op,name):
        """Draft access for Lauren, whatever happened to delivery."""
        return self.artifacts.read(self.key(op),name)
    def __call__(self,snapshot,op):
        self._own(snapshot.get('scope'))
        found=self.lookup(op)
        if found:
            if found.get('render_version')!=self.render_version():
                raise ValueError('Render configuration changed; new operation required.')
            return found
        files,receipt=self.build(snapshot,op)
        return self.artifacts.commit(self.key(op),files,dict(receipt,stored='private_blob'))


class NativeDraftRouter(RendererRouter):
    """One bound NativeDraftPublisher per verified assignment, built from that assignment's OWN ``mapping`` and
    ``header``. An assignment without both gets no publisher (and so cannot run); nothing is shared or defaulted.
    Each binding writes to its own folder."""
    def __init__(self,output,assignments):
        publishers=[]
        for a in assignments or []:
            if not isinstance(a.get('mapping'),dict) or not a['mapping'].get('fields') or not isinstance(a.get('header'),dict):
                continue
            try:bound=exact_binding(a)
            except ValueError:continue
            publishers.append(self.publisher(output,a,bound))
        super().__init__(publishers)
    @staticmethod
    def publisher(output,a,bound):
        return NativeDraftPublisher(Path(output)/sf.digest(bound)[:16],a['mapping'],a['header'],binding=bound)


class StoredDraftRouter(NativeDraftRouter):
    """As NativeDraftRouter, but every draft goes to the given ArtifactStore."""
    @staticmethod
    def publisher(artifacts,a,bound):
        return StoredDraftPublisher(artifacts,a['mapping'],a['header'],bound)
