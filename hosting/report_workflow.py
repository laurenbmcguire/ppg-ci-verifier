"""Versioned private report review. No network, source writeback, or delivery code.

Report/source scope and exact hashes bind proposals, drafts, and approval. The
runtime must supply a trusted reviewer identity; request JSON cannot create one.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3

MAX_BYTES = 2 * 1024 * 1024
MAX_ROWS = 10000
MAX_FINDINGS = 200
MAX_PROPOSALS = 500
SCOPE_FIELDS = ('site_id', 'list_id', 'client_id', 'job_id', 'shift_id', 'report_date')
DAILY_SCOPE_FIELDS = ('site_id', 'list_id', 'client_id', 'job_id', 'report_date', 'report_granularity')
NOTICE = 'Private draft review only. Supplied source evidence is not independently authenticated. Production source changes, customer release, and message delivery are disabled.'

class WorkflowError(ValueError): pass
class WorkflowConflict(WorkflowError): pass

@dataclass(frozen=True)
class ReviewerIdentity:
    subject: str
    display_name: str
    provider: str
    can_approve: bool = False


def canonical(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, RecursionError, OverflowError) as exc:
        raise WorkflowError('Only finite ordinary JSON is supported.') from exc


def fingerprint(value): return hashlib.sha256(canonical(value).encode()).hexdigest()
def now(): return datetime.now(timezone.utc).isoformat()


def pairs(items):
    result = {}
    for key, value in items:
        if key in result: raise WorkflowError('Duplicate JSON keys are not permitted.')
        result[key] = value
    return result


def parse(value):
    if isinstance(value, (str, bytes)):
        if len(value) > MAX_BYTES: raise WorkflowError('Manifest exceeds 2 MiB.')
        try:
            value = json.loads(value, object_pairs_hook=pairs,
                parse_constant=lambda x: (_ for _ in ()).throw(WorkflowError('Non-finite JSON number.')))
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise WorkflowError('Malformed report manifest.') from exc
    encoded = canonical(value)
    if len(encoded.encode()) > MAX_BYTES: raise WorkflowError('Manifest exceeds 2 MiB.')
    if not isinstance(value, dict): raise WorkflowError('Manifest must be an object.')
    return json.loads(encoded)


def text(value, label, maximum=200):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise WorkflowError(label + ' must be nonempty bounded text without control characters.')
    return value


def scope(value):
    if not isinstance(value, dict) or set(value) not in (set(SCOPE_FIELDS),set(DAILY_SCOPE_FIELDS)):
        raise WorkflowError('Scope must identify exact site, list, client, job, date, and a physical shift or explicit daily granularity.')
    if 'report_granularity' in value and value['report_granularity'] != 'daily':
        raise WorkflowError('Only explicit daily report granularity is supported.')
    for key in value:
        if key == 'client_id' and value[key] is None: continue
        text(value[key], key)
    try:
        if date.fromisoformat(value['report_date']).isoformat() != value['report_date']: raise ValueError()
    except ValueError as exc: raise WorkflowError('report_date must be an ISO date.') from exc
    return value


def field_definitions(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 30: raise WorkflowError('Supply 1 to 30 explicit field definitions.')
    fields = {}
    for field in value:
        if not isinstance(field, dict) or set(field) != {'name','type','required','total','nonnegative'}:
            raise WorkflowError('Each field needs name, type, required, total, and nonnegative.')
        name = field['name']
        if not isinstance(name, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,63}', name) or name in fields:
            raise WorkflowError('Field names must be unique lowercase identifiers.')
        if field['type'] not in ('integer','number','text') or any(type(field[key]) is not bool for key in ('required','total','nonnegative')):
            raise WorkflowError('Unsupported field type or nonboolean field flag.')
        if field['type'] == 'text' and (field['total'] or field['nonnegative']): raise WorkflowError('Text fields cannot have numeric rules.')
        fields[name] = field
    return fields


def valid(value, definition):
    if definition['type'] == 'text': return isinstance(value, str) and 0 < len(value.strip()) <= 500 and not any(ord(c)<32 for c in value)
    if type(value) not in (int,float) or abs(value) > 10**15: return False
    if type(value) is float and not math.isfinite(value): return False
    if definition['type'] == 'integer' and type(value) is not int: return False
    number = Decimal(str(value))
    if number.as_tuple().exponent < -6: return False
    return not definition['nonnegative'] or number >= 0


def numeric(value):
    if value == value.to_integral_value(): return int(value)
    result = float(value)
    if Decimal(str(result)) != value: raise WorkflowError('Aggregate cannot be represented exactly as a JSON number.')
    return result


def stamp(value):
    try:
        result = datetime.fromisoformat(value.replace('Z','+00:00'))
        if result.tzinfo is None or result.utcoffset() is None: raise ValueError()
        return result.astimezone(timezone.utc)
    except (TypeError, ValueError, AttributeError) as exc: raise WorkflowError('Timestamp must include a timezone.') from exc


def rows(value, fields, side):
    if not isinstance(value, list) or len(value) > MAX_ROWS: raise WorkflowError(side + ' records must be a bounded array.')
    found = {}
    for row in value:
        if not isinstance(row, dict) or set(row) - {'id','values','field_presence','source_locator','shift_id'}: raise WorkflowError('Unsupported record shape.')
        rid = text(row.get('id'), 'record id')
        if rid in found: raise WorkflowError('Duplicate record IDs are ambiguous; no correction is generated.')
        values = row.get('values')
        if not isinstance(values, dict) or set(values) - fields.keys(): raise WorkflowError('Record values contain an unknown field.')
        if 'field_presence' in row:
            presence = row['field_presence']
            if not isinstance(presence,dict) or set(presence)-fields.keys(): raise WorkflowError('Invalid field presence map.')
            for name, state in presence.items():
                expected = 'absent' if name not in values else ('null' if values[name] is None else 'present')
                if state != expected: raise WorkflowError('Field presence contradicts record values.')
        if 'source_locator' in row: text(row['source_locator'], 'source locator', 1000)
        if 'shift_id' in row: text(row['shift_id'], 'record shift id')
        found[rid] = row
    return found


def is_daily(value): return value.get('report_granularity') == 'daily'


def daily_scope(value):
    return {**{key:value[key] for key in SCOPE_FIELDS if key!='shift_id'},'report_granularity':'daily'}


def group_totals(records, fields, shifts):
    """Recompute shift totals without treating missing or invalid values as zero."""
    result={}
    for shift in shifts:
        members=[r for r in records.values() if r.get('shift_id')==shift]
        totals={}
        for name,definition in fields.items():
            if not definition['total']:continue
            values=[row['values'].get(name) for row in members]
            total=sum((Decimal(str(v)) for v in values),Decimal(0)) if members and all(valid(v,definition) for v in values) else None
            try:totals[name]=numeric(total) if total is not None and abs(total)<=10**15 else None
            except WorkflowError:totals[name]=None
        result[shift]={'record_count':len(members),'totals':totals}
    return result


def daily_structure(m, fields):
    """Validate exact membership independently of source completeness claims."""
    if not is_daily(m['scope']):
        if 'shift_breakdown' in m['report'] or any(k in (m.get('source_snapshot') or {}) for k in ('member_shifts','shift_coverage_complete')):
            raise WorkflowError('Daily evidence requires an explicit daily scope.')
        for side in ('report','source_snapshot'):
            for row in (m.get(side) or {}).get('records',[]):
                if 'shift_id' in row and row['shift_id'] != m[side].get('scope',m['scope']).get('shift_id'):
                    raise WorkflowError('Record shift does not match its physical shift scope.')
        return
    breakdown=m['report'].get('shift_breakdown')
    if not isinstance(breakdown,dict) or not 1<=len(breakdown)<=100:raise WorkflowError('Daily reports require a bounded per-shift breakdown.')
    total_fields={name for name,definition in fields.items() if definition['total']}
    for shift,item in breakdown.items():
        text(shift,'breakdown shift id')
        if not isinstance(item,dict) or set(item)!={'record_count','totals'} or type(item['record_count']) is not int or not 0<=item['record_count']<=MAX_ROWS:
            raise WorkflowError('Each shift breakdown requires a nonnegative record count and totals.')
        if not isinstance(item['totals'],dict) or set(item['totals'])!=total_fields:raise WorkflowError('Each shift breakdown requires exact total field names.')
    for side in ('report','source_snapshot'):
        for row in (m.get(side) or {}).get('records',[]):text(row.get('shift_id'),'daily record shift id')
    if any(row['shift_id'] not in breakdown for row in m['report']['records']):
        raise WorkflowError('Daily report row refers to a shift absent from its breakdown.')
    source=m.get('source_snapshot')
    if source is None:return
    if type(source.get('shift_coverage_complete')) is not bool:raise WorkflowError('Daily shift coverage must be explicitly known or unconfirmed.')
    members=source.get('member_shifts')
    if not isinstance(members,list) or not 1<=len(members)<=100:raise WorkflowError('Daily source requires explicit member shifts.')
    membership={};seen_shifts=set()
    required={'scope','source_revision','captured_at','complete','consistency','shift_complete','timezone_status','record_ids'}
    for member in members:
        if not isinstance(member,dict) or set(member)!=required:raise WorkflowError('Unsupported member shift evidence shape.')
        member_scope=scope(member['scope'])
        if is_daily(member_scope) or daily_scope(member_scope)!=m['scope']:
            raise WorkflowError('Daily member shifts must identify the same exact site, list, client, job and date.')
        shift=member_scope['shift_id']
        if shift in seen_shifts:raise WorkflowError('Daily member shifts must be unique.')
        seen_shifts.add(shift)
        if not isinstance(member['source_revision'],str) or not re.fullmatch('[0-9a-f]{64}',member['source_revision']):raise WorkflowError('Member source revision must be a SHA-256 hash.')
        if type(member['complete']) is not bool or member['consistency'] not in ('confirmed','unknown') or member['timezone_status'] not in ('confirmed','unknown'):
            raise WorkflowError('Member completeness, consistency and timezone evidence must be explicit.')
        if type(member['shift_complete']) not in (bool,type(None)):raise WorkflowError('Member shift completion must be boolean or unknown.')
        ids=member['record_ids']
        if not isinstance(ids,list) or len(ids)>MAX_ROWS:raise WorkflowError('Member record IDs must be bounded.')
        for rid in ids:
            text(rid,'member record id')
            if rid in membership:raise WorkflowError('Daily source records may belong to exactly one member shift.')
            membership[rid]=shift
    if set(breakdown)!=seen_shifts:raise WorkflowError('Daily breakdown and member shifts must identify the same shifts.')
    supplied={row['id']:row for row in source['records']}
    if set(membership)!=set(supplied) or any(supplied[rid]['shift_id']!=shift for rid,shift in membership.items()):
        raise WorkflowError('Daily member record IDs must partition the exact supplied source records and match their shift IDs.')


def validate_manifest(value):
    value = parse(value)
    allowed = {'schema_version','report_id','version','scope','fields','report','source_snapshot','generated_at','parent_manifest_hash','recipient_mapping_version','synthetic'}
    if set(value)-allowed: raise WorkflowError('Unsupported manifest field.')
    if type(value.get('schema_version')) is not int or value['schema_version'] != 2: raise WorkflowError('Report workflow requires schema_version 2.')
    for key in ('report_id','version'): text(value.get(key),key)
    scope(value.get('scope'))
    fields = field_definitions(value.get('fields'))
    report = value.get('report')
    if not isinstance(report,dict) or set(report) != ({'records','totals','shift_breakdown'} if is_daily(value['scope']) else {'records','totals'}): raise WorkflowError('report requires records, totals and, for daily reports, shift_breakdown.')
    rows(report['records'], fields, 'report')
    totals = report['totals']
    if not isinstance(totals,dict) or set(totals)-{name for name,d in fields.items() if d['total']}: raise WorkflowError('Unexpected report total field.')
    if 'synthetic' in value and type(value['synthetic']) is not bool: raise WorkflowError('synthetic must be a boolean.')
    if 'recipient_mapping_version' in value: text(value['recipient_mapping_version'],'recipient mapping version')
    if 'generated_at' in value: stamp(value['generated_at'])
    if 'parent_manifest_hash' in value and not re.fullmatch('[0-9a-f]{64}',str(value['parent_manifest_hash'])): raise WorkflowError('Invalid parent hash.')
    source = value.get('source_snapshot')
    if source is not None:
        if not isinstance(source,dict) or set(source)-{'source_reference','captured_at','complete','consistency','shift_complete','scope','records','field_definitions_hash','timezone_status','source_revision','envelope_hash','evidence_blockers','member_shifts','shift_coverage_complete'}:
            raise WorkflowError('Unsupported source snapshot shape.')
        text(source.get('source_reference'),'source reference',1000)
        if 'envelope_hash' in source and not re.fullmatch('[0-9a-f]{64}',str(source['envelope_hash'])): raise WorkflowError('Invalid envelope hash.')
        blockers=source.get('evidence_blockers',[])
        if not isinstance(blockers,list) or len(blockers)>50: raise WorkflowError('Source evidence blockers must be a bounded list.')
        for blocker in blockers: text(blocker,'source evidence blocker')
        scope(source.get('scope'))
        rows(source.get('records'), fields, 'source')
        if type(source.get('complete')) is not bool: raise WorkflowError('Source completeness must be explicit.')
        if source.get('consistency') not in ('confirmed','unknown'): raise WorkflowError('Snapshot consistency must be confirmed or unknown.')
        if source.get('shift_complete') not in (True,False,None) or type(source.get('shift_complete')) not in (bool,type(None)): raise WorkflowError('Shift completion must be boolean or unknown.')
        # Missing or malformed provenance is retained as a blocking finding.
    daily_structure(value,fields)
    return value


def evaluate(manifest):
    m = validate_manifest(manifest)
    fields = field_definitions(m['fields'])
    reported = rows(m['report']['records'],fields,'report')
    base_hash = fingerprint(m)
    findings, proposals, counts = [], [], {}
    proposal_count = 0
    def finding(code,message,side=None,record_id=None,field=None):
        counts[code] = counts.get(code,0)+1
        if len(findings)<MAX_FINDINGS: findings.append(dict(code=code,severity='blocking',message=message,side=side,record_id=record_id,field=field))
    def proposal(operation,rid,field,before,after):
        nonlocal proposal_count
        body=dict(base_manifest_hash=base_hash,operation=operation,record_id=rid,field=field,before=before,after=after)
        proposal_count += 1
        if len(proposals)<MAX_PROPOSALS: proposals.append(dict(body,id=fingerprint(body),status='proposed',source_write=False))
    def state(record,name):
        return {'present':name in record['values'], 'value':record['values'].get(name)}
    def inspect(indexed,side):
        sums={name:Decimal(0) for name,d in fields.items() if d['total']}
        for rid, row in indexed.items():
            for name,definition in fields.items():
                present = name in row['values']; value=row['values'].get(name)
                missing = not present or value is None or (isinstance(value,str) and not value.strip())
                if missing:
                    if definition['required']: finding('missing_value','Required value is absent, null, or blank; it was not treated as zero.',side,rid,name)
                    if name in sums: sums[name]=None
                elif not valid(value,definition):
                    finding('invalid_value','Value does not satisfy the explicitly supplied field rule.',side,rid,name)
                    if name in sums:sums[name]=None
                elif name in sums and sums[name] is not None:
                    sums[name]+=Decimal(str(value))
                    if abs(sums[name])>10**15:
                        finding('aggregate_limit','Aggregate exceeds the supported numeric range.',side,None,name);sums[name]=None
        converted={}
        for name,value in sums.items():
            try: converted[name]=numeric(value) if value is not None else None
            except WorkflowError:
                finding('aggregate_precision','Exact aggregate cannot be represented safely; it was not rounded.',side,None,name)
                converted[name]=None
        return converted
    if m['scope']['client_id'] is None: finding('client_identity_missing','Client master identity is unresolved; no client mapping was invented.')
    report_totals=inspect(reported,'report')
    for name, total in report_totals.items():
        claimed=m['report']['totals'].get(name)
        if total is None:
            finding('total_unknown','Total remains unknown because one or more underlying values are missing or invalid.','report',None,name)
        elif not valid(claimed,fields[name]) or Decimal(str(claimed))!=Decimal(str(total)):
            finding('total_mismatch','Claimed total does not match the sum of the supplied report rows.','report',None,name)
    daily=is_daily(m['scope']);report_breakdown=None;source_breakdown=None
    if daily:
        report_breakdown=group_totals(reported,fields,m['report']['shift_breakdown'])
        for shift,item in report_breakdown.items():
            claimed=m['report']['shift_breakdown'][shift]
            if item['record_count']!=claimed['record_count']:finding('shift_record_count','Shift record count does not match the supplied report rows.','report',shift)
            for name,total in item['totals'].items():
                if total is None:finding('shift_total_unknown','Shift total remains unknown because its underlying evidence is empty, missing or invalid.','report',shift,name)
                elif not valid(claimed['totals'][name],fields[name]) or Decimal(str(claimed['totals'][name]))!=Decimal(str(total)):
                    finding('shift_total_mismatch','Shift total does not match its supplied report rows.','report',shift,name)
    source=m.get('source_snapshot'); source_usable=m['scope']['client_id'] is not None; supplied={}; source_totals=None
    if source is None:
        finding('source_missing','No source snapshot was supplied.');source_usable=False
    else:
        supplied=rows(source['records'],fields,'source')
        for blocker in source.get('evidence_blockers',[]):
            finding('source_envelope_blocker','Source intake requires review: '+blocker);source_usable=False
        before_source_findings=sum(counts.values())
        source_totals=inspect(supplied,'source')
        if sum(counts.values())>before_source_findings: source_usable=False
        if source['scope']!=m['scope']:
            finding('source_scope','Source and report do not identify the same exact site, list, client, job, date and report granularity.');source_usable=False
        if not source['complete']:
            finding('source_incomplete','Source traversal is incomplete. No correction proposals are actionable.');source_usable=False
        try:
            if stamp(source.get('captured_at'))>datetime.now(timezone.utc)+timedelta(minutes=5): raise WorkflowError('Future source.')
        except WorkflowError:
            finding('source_timestamp','Source capture time is missing, invalid, or in the future.');source_usable=False
        if source.get('field_definitions_hash')!=fingerprint(m['fields']):
            finding('source_field_mapping','Source field definitions are not bound to this exact mapping.');source_usable=False
        if source['consistency']!='confirmed':
            finding('source_consistency_unconfirmed','Pagination completion does not establish a transactionally consistent snapshot.');source_usable=False
        if source.get('shift_complete') is not True:
            finding('shift_completion_unconfirmed','The shift is not confirmed complete; this report remains a draft.');source_usable=False
        if source.get('timezone_status')!='confirmed':
            finding('source_timezone_unconfirmed','Source business-date/timezone interpretation has not been confirmed.');source_usable=False
        if daily:
            source_breakdown=group_totals(supplied,fields,m['report']['shift_breakdown'])
            if not source['shift_coverage_complete']:
                finding('daily_shift_coverage_unconfirmed','All expected job/day shifts have not been confirmed accounted for; observed shifts alone do not establish daily completeness.');source_usable=False
            for member in source['member_shifts']:
                shift=member['scope']['shift_id']
                if not member['complete'] or member['consistency']!='confirmed' or member['shift_complete'] is not True or member['timezone_status']!='confirmed':
                    finding('daily_member_unconfirmed','Member shift evidence or completion is unconfirmed; another finished shift cannot clear it.','source',shift);source_usable=False
                if not member['record_ids']:
                    finding('daily_member_no_data','An empty member shift cannot establish completed zero activity; explicit zero-activity proof is not supported.','source',shift);source_usable=False
                try:
                    captured=stamp(member['captured_at'])
                    if captured>datetime.now(timezone.utc)+timedelta(minutes=5) or captured>stamp(source.get('captured_at')):raise WorkflowError('Invalid member time.')
                except WorkflowError:
                    finding('daily_member_timestamp','Member shift capture time is invalid, future, or later than the daily capture time.','source',shift);source_usable=False
            for rid in supplied.keys()&reported.keys():
                if supplied[rid]['shift_id']!=reported[rid]['shift_id']:
                    finding('daily_record_shift','A report row has moved between shifts relative to its source. Resolve membership explicitly; it cannot be fixed as a quantity edit.','report',rid);source_usable=False
        if not reported and not supplied: finding('no_data','Empty data cannot establish a completed zero-activity report.')
        for rid in sorted(supplied.keys()-reported.keys()):
            finding('missing_record','A source record is absent from the report.','report',rid)
            if source_usable and all(not d['required'] or valid(supplied[rid]['values'].get(n),d) for n,d in fields.items()):
                proposal('add_record',rid,None,{'present':False,'value':None},{'present':True,'value':supplied[rid]})
        for rid in sorted(reported.keys()-supplied.keys()):
            finding('extra_record','A report record is absent from the supplied source.','report',rid)
            if source_usable: proposal('remove_record',rid,None,{'present':True,'value':reported[rid]},{'present':False,'value':None})
        for rid in sorted(supplied.keys()&reported.keys()):
            for name,definition in fields.items():
                before, after=state(reported[rid],name),state(supplied[rid],name)
                if canonical(before)!=canonical(after):
                    finding('source_difference','Report and supplied source values differ.','report',rid,name)
                    if source_usable and after['present'] and valid(after['value'],definition): proposal('set_value',rid,name,before,after)
        if source_usable:
            for name,total in (source_totals or {}).items():
                before={'present':name in m['report']['totals'],'value':m['report']['totals'].get(name)}
                if total is not None and canonical(before)!=canonical({'present':True,'value':total}):
                    proposal('set_total',None,name,before,{'present':True,'value':total})
            for shift,item in (source_breakdown or {}).items():
                claimed=m['report']['shift_breakdown'][shift]
                if claimed['record_count']!=item['record_count']:
                    proposal('set_shift_record_count',shift,None,{'present':True,'value':claimed['record_count']},{'present':True,'value':item['record_count']})
                for name,total in item['totals'].items():
                    before={'present':True,'value':claimed['totals'][name]};after={'present':True,'value':total}
                    if total is not None and canonical(before)!=canonical(after):proposal('set_shift_total',shift,name,before,after)
    total_findings=sum(counts.values())
    return dict(manifest_hash=base_hash,report_hash=fingerprint(m['report']),source_hash=fingerprint(source) if source is not None else None,
        scope=m['scope'],report_id=m['report_id'],version=m['version'],findings=findings,finding_counts=counts,finding_count=total_findings,
        findings_truncated=total_findings>MAX_FINDINGS,proposals=proposals,proposal_count=proposal_count,proposals_truncated=proposal_count>MAX_PROPOSALS,
        report_totals=report_totals,source_totals=source_totals,report_shift_breakdown=report_breakdown,source_shift_breakdown=source_breakdown,
        member_shifts=source.get('member_shifts') if source else None,shift_coverage_complete=source.get('shift_coverage_complete') if source else None,verification_scope='comparison_to_supplied_snapshot',
        source_authenticity='not_authenticated',status='blocked' if total_findings else 'matched_supplied_snapshot',
        release_status='held',source_write_enabled=False,send_enabled=False,notice=NOTICE)


class ReportWorkflow:
    def __init__(self, root, development_demo=False):
        self.development_demo = development_demo is True
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True);self.db_path=self.root/'report_workflow.sqlite3'
        with self.connect() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS versions (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, version_key TEXT UNIQUE NOT NULL,
                report_key TEXT NOT NULL, scope_key TEXT NOT NULL, manifest_hash TEXT UNIQUE NOT NULL,
                manifest_json TEXT NOT NULL, received_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS report_heads (report_key TEXT PRIMARY KEY, manifest_hash TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS source_heads (scope_key TEXT PRIMARY KEY, source_hash TEXT NOT NULL, captured_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS member_evidence_heads (scope_key TEXT PRIMARY KEY, daily_scope_key TEXT NOT NULL, captured_at TEXT NOT NULL, evidence_hash TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS member_evidence_day ON member_evidence_heads(daily_scope_key);
                CREATE TABLE IF NOT EXISTS workflow_migrations (name TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS demo_decisions (manifest_hash TEXT NOT NULL, proposal_id TEXT NOT NULL, decision_json TEXT NOT NULL, PRIMARY KEY(manifest_hash, proposal_id));
                CREATE TABLE IF NOT EXISTS approvals (manifest_hash TEXT PRIMARY KEY, approval_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT, manifest_hash TEXT NOT NULL, at TEXT NOT NULL, event TEXT NOT NULL, detail_json TEXT NOT NULL);''')
            # Existing physical-shift imports must participate in daily freshness.
            # A transaction and marker make upgrades and retries deterministic.
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM workflow_migrations WHERE name=?',('member-evidence-v1',)).fetchone():
                for row in db.execute('SELECT manifest_hash FROM versions ORDER BY seq').fetchall():
                    manifest,_=self.manifest(db,row['manifest_hash'])
                    self.remember_members(db,manifest)
                db.execute('INSERT INTO workflow_migrations(name) VALUES(?)',('member-evidence-v1',))
    @contextmanager
    def connect(self):
        db=sqlite3.connect(str(self.db_path),timeout=15);db.row_factory=sqlite3.Row;db.execute('PRAGMA synchronous=FULL')
        try:
            with db: yield db
        finally: db.close()
    def log(self,db,mh,event,detail): db.execute('INSERT INTO events(manifest_hash,at,event,detail_json) VALUES(?,?,?,?)',(mh,now(),event,canonical(detail)))
    def keys(self,m):
        identity={'scope':m['scope']} if is_daily(m['scope']) else {'scope':m['scope'],'report_id':m['report_id']}
        return fingerprint(dict(identity,version=m['version'])),fingerprint(identity),fingerprint(m['scope'])
    def member_evidence(self,m):
        """Bind member claims to actual rows, mapping, flags, and capture time.

        Logical row order and redundant presence maps are normalized. Quantities,
        source locators, unknown values, and explicit evidence remain hash-bound.
        A supplied revision string alone is never used to establish freshness.
        """
        source=m.get('source_snapshot')
        if not source or source['scope']!=m['scope']:return []
        members=source['member_shifts'] if is_daily(m['scope']) else [source]
        indexed={row['id']:row for row in source['records']};result=[]
        for member in members:
            try:captured=stamp(member.get('captured_at'))
            except WorkflowError:continue
            member_scope=member['scope'];shift=member_scope['shift_id']
            ids=member['record_ids'] if is_daily(m['scope']) else list(indexed)
            records=[]
            for rid in sorted(ids):
                row=copy.deepcopy(indexed[rid]);row['shift_id']=shift;row.pop('field_presence',None);records.append(row)
            evidence={key:member.get(key) for key in ('scope','source_revision','complete','consistency','shift_complete','timezone_status')}
            evidence.update(captured_at=captured.isoformat(),records=records,field_definitions_hash=source.get('field_definitions_hash'),
                evidence_blockers=sorted(set(source.get('evidence_blockers',[]))))
            result.append(dict(scope_key=fingerprint(member_scope),daily_scope_key=fingerprint(daily_scope(member_scope)),
                captured_at=captured.isoformat(),evidence_hash=fingerprint(evidence)))
        return result
    def remember_members(self,db,m):
        for member in self.member_evidence(m):
            captured=stamp(member['captured_at'])
            if captured>datetime.now(timezone.utc)+timedelta(minutes=5):continue
            current=db.execute('SELECT * FROM member_evidence_heads WHERE scope_key=?',(member['scope_key'],)).fetchone()
            if current and captured==stamp(current['captured_at']) and member['evidence_hash']!=current['evidence_hash']:
                raise WorkflowConflict('Different member source content or evidence share a capture time; resolve provenance before importing.')
            if not current or captured>stamp(current['captured_at']):
                db.execute('INSERT INTO member_evidence_heads(scope_key,daily_scope_key,captured_at,evidence_hash) VALUES(?,?,?,?) ON CONFLICT(scope_key) DO UPDATE SET daily_scope_key=excluded.daily_scope_key,captured_at=excluded.captured_at,evidence_hash=excluded.evidence_hash',
                    (member['scope_key'],member['daily_scope_key'],member['captured_at'],member['evidence_hash']))
    def insert(self,db,m):
        result=evaluate(m);mh=result['manifest_hash'];vk,rk,sk=self.keys(m)
        old=db.execute('SELECT manifest_hash FROM versions WHERE version_key=?',(vk,)).fetchone()
        if old:
            if old['manifest_hash']!=mh: raise WorkflowConflict('This report version already has different content; original preserved.')
            return mh,True
        self.remember_members(db,m)
        source=m.get('source_snapshot')
        if source and source['scope']==m['scope']:
            try: captured=stamp(source.get('captured_at'))
            except WorkflowError: captured=None
            if captured and captured<=datetime.now(timezone.utc)+timedelta(minutes=5):
                current=db.execute('SELECT * FROM source_heads WHERE scope_key=?',(sk,)).fetchone()
                if current and captured==stamp(current['captured_at']) and result['source_hash']!=current['source_hash']:
                    raise WorkflowConflict('Two different source snapshots have the same capture time; resolve provenance before importing.')
                if not current or captured>stamp(current['captured_at']):
                    db.execute('INSERT INTO source_heads(scope_key,source_hash,captured_at) VALUES(?,?,?) ON CONFLICT(scope_key) DO UPDATE SET source_hash=excluded.source_hash,captured_at=excluded.captured_at',(sk,result['source_hash'],captured.isoformat()))
        db.execute('INSERT INTO versions(version_key,report_key,scope_key,manifest_hash,manifest_json,received_at) VALUES(?,?,?,?,?,?)',(vk,rk,sk,mh,canonical(m),now()))
        db.execute('INSERT INTO report_heads(report_key,manifest_hash) VALUES(?,?) ON CONFLICT(report_key) DO UPDATE SET manifest_hash=excluded.manifest_hash',(rk,mh))
        self.log(db,mh,'version_imported',{'report_hash':result['report_hash'],'source_hash':result['source_hash'],'source_written':False})
        return mh,False
    def ingest(self,manifest):
        m=validate_manifest(manifest)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE');mh,idempotent=self.insert(db,m)
            return dict(self.read(db,mh),idempotent=idempotent)
    def ingest_batch(self,manifests):
        if not isinstance(manifests,list) or not 1<=len(manifests)<=100:raise WorkflowError('A batch must contain 1 to 100 explicit manifests.')
        prepared=[validate_manifest(m) for m in manifests]
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE');items=[]
            for manifest in prepared:
                mh,idempotent=self.insert(db,manifest);result=self.read(db,mh)
                keep=('manifest_hash','report_hash','source_hash','scope','report_id','version','status','finding_count','proposal_count','release_status')
                items.append(dict({key:result[key] for key in keep},idempotent=idempotent))
            return dict(items=items,source_write_enabled=False,send_enabled=False)
    def manifest(self,db,mh):
        if not isinstance(mh,str) or not re.fullmatch('[0-9a-f]{64}',mh): raise WorkflowError('Exact manifest hash required.')
        row=db.execute('SELECT * FROM versions WHERE manifest_hash=?',(mh,)).fetchone()
        if not row: raise WorkflowError('Report version not found.')
        m=validate_manifest(row['manifest_json'])
        if fingerprint(m)!=mh: raise WorkflowConflict('Stored report integrity mismatch.')
        vk,rk,sk=self.keys(m)
        if (vk,rk,sk)!=(row['version_key'],row['report_key'],row['scope_key']): raise WorkflowConflict('Stored scope integrity mismatch.')
        return m,row
    def read(self,db,mh):
        m,row=self.manifest(db,mh);result=evaluate(m)
        head=db.execute('SELECT manifest_hash FROM report_heads WHERE report_key=?',(row['report_key'],)).fetchone()
        source_head=db.execute('SELECT source_hash FROM source_heads WHERE scope_key=?',(row['scope_key'],)).fetchone()
        current=head is not None and head['manifest_hash']==mh
        source_current=source_head is not None and source_head['source_hash']==result['source_hash']
        members_current=True;members=self.member_evidence(m)
        for member in members:
            current_member=db.execute('SELECT * FROM member_evidence_heads WHERE scope_key=?',(member['scope_key'],)).fetchone()
            if current_member is None or current_member['captured_at']!=member['captured_at'] or current_member['evidence_hash']!=member['evidence_hash']:
                members_current=False
        known_coverage=True
        if is_daily(m['scope']):
            known={row['scope_key'] for row in db.execute('SELECT scope_key FROM member_evidence_heads WHERE daily_scope_key=?',(fingerprint(m['scope']),))}
            known_coverage=known.issubset({member['scope_key'] for member in members})
            if not known_coverage:
                result['finding_counts']['daily_known_shift_missing']=1;result['finding_count']+=1;result['status']='blocked'
                if len(result['findings'])<MAX_FINDINGS:
                    result['findings'].append(dict(code='daily_known_shift_missing',severity='blocking',message='A previously imported shift for this exact job/day is absent from the daily member evidence.',side='source',record_id=None,field=None))
                result['findings_truncated']=result['finding_count']>MAX_FINDINGS
        members_current=members_current and known_coverage
        source_current=source_current and members_current
        approval=db.execute('SELECT approval_json FROM approvals WHERE manifest_hash=?',(mh,)).fetchone()
        approval=json.loads(approval[0]) if approval else None
        effective=bool(approval and current and source_current and result['finding_count']==0 and approval['manifest_hash']==mh and approval['report_hash']==result['report_hash'] and approval['source_hash']==result['source_hash'])
        return dict(result,development_demo=self.demo_enabled(),demo_eligible=self.demo_enabled() and m.get('synthetic') is True,
            demo_decisions=[json.loads(d[0]) for d in db.execute('SELECT decision_json FROM demo_decisions WHERE manifest_hash=? ORDER BY proposal_id',(mh,))] if self.demo_enabled() else [],current_version=current,current_source=source_current,current_member_sources=members_current,known_shift_coverage_complete=known_coverage,approval=approval,approval_effective=effective,
            approval_scope='private_draft_only',human_approval_required=True,received_at=row['received_at'],
            recipient_mapping_version=m.get('recipient_mapping_version'),
            events=[dict(at=e['at'],event=e['event'],detail=json.loads(e['detail_json'])) for e in db.execute('SELECT * FROM events WHERE manifest_hash=? ORDER BY seq DESC LIMIT 100',(mh,))])
    def get(self,mh):
        with self.connect() as db:return self.read(db,mh)
    def list(self,limit=50,before=None):
        if type(limit) is not int or not 1<=limit<=100: raise WorkflowError('List limit must be 1 to 100.')
        if before is not None and (type(before) is not int or before<1):raise WorkflowError('Invalid pagination cursor.')
        with self.connect() as db:
            sql='SELECT * FROM versions'+(' WHERE seq<?' if before else '')+' ORDER BY seq DESC LIMIT ?'
            results=db.execute(sql,(before,limit+1) if before else (limit+1,)).fetchall();items=[]
            for row in results[:limit]:
                result=self.read(db,row['manifest_hash'])
                keep=('manifest_hash','report_hash','source_hash','scope','report_id','version','status','finding_count','current_version','current_source','approval_effective','release_status','received_at')
                items.append({key:result[key] for key in keep})
            return dict(reports=items,next_cursor=results[limit-1]['seq'] if len(results)>limit else None,notice=NOTICE)
    def demo_enabled(self):
        from hosting.sqlite_storage import local_environment
        return self.development_demo and os.environ.get('CI_LOCAL_DEV') == '1' and os.environ.get('CI_DEVELOPMENT_DEMO') == '1' and local_environment()
    def demo_check(self,db,mh,report_hash,source_hash):
        if not self.demo_enabled():raise WorkflowError('Development simulation is disabled.')
        m,_=self.manifest(db,mh);state=self.read(db,mh)
        if m.get('synthetic') is not True:raise WorkflowError('Development simulation requires an explicitly synthetic manifest.')
        if not state['current_version'] or not state['current_source']:raise WorkflowConflict('Report or source changed; demo decisions are stale.')
        if report_hash != state['report_hash'] or source_hash != state['source_hash']:raise WorkflowConflict('Exact report and source hashes are required for this simulation.')
        return state
    def demo_decide(self,mh,proposal_id,decision,report_hash,source_hash):
        return self.demo_decide_batch(mh,[proposal_id],decision,report_hash,source_hash)
    def demo_decide_batch(self,mh,proposal_ids,decision,report_hash,source_hash):
        if decision not in ('approve','deny','hold'):raise WorkflowError('Choose approve, deny, or hold for this simulation.')
        if not isinstance(proposal_ids,list) or not 1<=len(proposal_ids)<=MAX_PROPOSALS or not all(isinstance(p,str) for p in proposal_ids) or len(set(proposal_ids))!=len(proposal_ids):
            raise WorkflowError('Select 1 to 500 distinct explicit proposal IDs.')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE');state=self.demo_check(db,mh,report_hash,source_hash)
            available={p['id']:p for p in state['proposals']}
            if any(pid not in available for pid in proposal_ids):raise WorkflowConflict('Unknown or stale proposal; no demo decisions recorded.')
            for proposal_id in proposal_ids:
                detail=dict(manifest_hash=mh,report_hash=report_hash,source_hash=source_hash,proposal_id=proposal_id,
                    proposal_hash=fingerprint(available[proposal_id]),decision=decision,actor='development-demo-simulator',
                    identity_authenticated=False,simulation_only=True,at=now(),production_release=False)
                db.execute('INSERT OR REPLACE INTO demo_decisions(manifest_hash,proposal_id,decision_json) VALUES(?,?,?)',(mh,proposal_id,canonical(detail)))
                self.log(db,mh,'demo_proposal_decision',detail)
            return self.read(db,mh)
    def demo_apply(self,mh,proposal_ids,new_version,report_hash,source_hash):
        return self._apply(mh,proposal_ids,new_version,demo_binding=(report_hash,source_hash))
    def apply(self,mh,proposal_ids,new_version):
        if self.demo_enabled():raise WorkflowError('Use the development simulation endpoint with explicitly demo-approved proposals.')
        return self._apply(mh,proposal_ids,new_version)
    def _apply(self,mh,proposal_ids,new_version,demo_binding=None):
        text(new_version,'new version')
        if not isinstance(proposal_ids,list) or not 1<=len(proposal_ids)<=MAX_PROPOSALS or not all(isinstance(p,str) for p in proposal_ids) or len(set(proposal_ids))!=len(proposal_ids):
            raise WorkflowError('Select 1 to 500 distinct explicit proposal IDs.')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE');m,_=self.manifest(db,mh);state=self.read(db,mh)
            if not state['current_version'] or not state['current_source']:raise WorkflowConflict('Report or source changed; reload before applying proposals.')
            available={p['id']:p for p in state['proposals']}
            if any(pid not in available for pid in proposal_ids):raise WorkflowConflict('Unknown or stale proposal selection; nothing changed.')
            if demo_binding is not None:
                self.demo_check(db,mh,*demo_binding)
                for pid in proposal_ids:
                    record=db.execute('SELECT decision_json FROM demo_decisions WHERE manifest_hash=? AND proposal_id=?',(mh,pid)).fetchone()
                    decision=json.loads(record[0]) if record else {}
                    expected=dict(manifest_hash=mh,report_hash=state['report_hash'],source_hash=state['source_hash'],proposal_id=pid,
                        proposal_hash=fingerprint(available[pid]),decision='approve',actor='development-demo-simulator',identity_authenticated=False,simulation_only=True,production_release=False)
                    if any(decision.get(k)!=v for k,v in expected.items()):raise WorkflowConflict('Every selected proposal must have a current demo approval; denied, held, or unreviewed proposals cannot apply.')
            draft=copy.deepcopy(m);indexed={r['id']:r for r in draft['report']['records']}
            for pid in sorted(proposal_ids):
                p=available[pid];rid=p['record_id'];name=p['field']
                if p['operation']=='set_value':
                    indexed[rid]['values'][name]=p['after']['value']
                    if 'field_presence' in indexed[rid]:indexed[rid]['field_presence'][name]='present'
                elif p['operation']=='set_total':draft['report']['totals'][name]=p['after']['value']
                elif p['operation']=='set_shift_total':draft['report']['shift_breakdown'][rid]['totals'][name]=p['after']['value']
                elif p['operation']=='set_shift_record_count':draft['report']['shift_breakdown'][rid]['record_count']=p['after']['value']
                elif p['operation']=='add_record':indexed[rid]=copy.deepcopy(p['after']['value'])
                elif p['operation']=='remove_record':del indexed[rid]
            draft['report']['records']=list(indexed.values());draft['version']=new_version;draft['parent_manifest_hash']=mh
            new_hash,_=self.insert(db,draft)
            self.log(db,new_hash,'demo_proposals_applied_to_draft' if demo_binding is not None else 'proposals_applied_to_draft',{'parent_manifest_hash':mh,'proposal_ids':proposal_ids,'source_written':False,'approval_carried_forward':False,**({'simulation_only':True,'actor':'development-demo-simulator','identity_authenticated':False,'production_release':False} if demo_binding is not None else {})})
            return self.read(db,new_hash)
    def approve(self,mh,reviewed,identity=None):
        if reviewed is not True:raise WorkflowError('Explicit review confirmation is required.')
        if not isinstance(identity,ReviewerIdentity) or identity.can_approve is not True or not all(isinstance(v,str) and v.strip() for v in (identity.subject,identity.display_name,identity.provider)):
            raise WorkflowError('Authenticated reviewer integration is not configured. Human approval remains required.')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE');state=self.read(db,mh)
            if not state['current_version'] or not state['current_source']:raise WorkflowConflict('Report or source changed; approval cannot be reused.')
            if state['finding_count']:raise WorkflowConflict('Resolve blocking findings before approving this exact private draft.')
            if state['approval_effective']:return state
            approval=dict(manifest_hash=mh,report_hash=state['report_hash'],source_hash=state['source_hash'],
                reviewer_subject=identity.subject,reviewer_name=identity.display_name,identity_provider=identity.provider,
                recipient_mapping_version=state['recipient_mapping_version'],approved_at=now(),scope='private_draft_only',production_release=False)
            db.execute('INSERT OR REPLACE INTO approvals(manifest_hash,approval_json) VALUES(?,?)',(mh,canonical(approval)))
            self.log(db,mh,'private_draft_approved',approval)
            return self.read(db,mh)
