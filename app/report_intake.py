"""Synthetic, local-only report reconciliation. No fetching, approval, or SMS send."""
from contextlib import contextmanager
import hashlib
import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

FIELDS = ('inspected', 'accepted', 'rejected')
NOTICE = ('Synthetic demo only. Comparison is to a supplied snapshot, whose origin and '
          'completeness are not independently authenticated. Human approval and customer '
          'release are separate and not implemented. SMS entries are previews, never sent.')

class IntakeError(ValueError):
    pass

class IntakeConflict(IntakeError):
    pass

def canonical(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError, OverflowError, RecursionError) as exc:
        raise IntakeError('Input must be finite, ordinary JSON data.') from exc

def fingerprint(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()

def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise IntakeError('Duplicate JSON object key: ' + key)
        result[key] = value
    return result

def _text(obj, key):
    val = obj.get(key)
    if not isinstance(val, str) or not val.strip() or len(val) > 200:
        raise IntakeError(key + ' must be a nonempty string of at most 200 characters.')
    if any(ord(c) < 32 for c in val):
        raise IntakeError(key + ' must not contain control characters.')
    return val

def _period(value):
    if not isinstance(value, dict) or set(value) != {'start', 'end'}:
        raise IntakeError('period must contain start and end ISO dates.')
    try:
        start, end = date.fromisoformat(value['start']), date.fromisoformat(value['end'])
        if start.isoformat() != value['start'] or end.isoformat() != value['end'] or start > end:
            raise ValueError()
    except (ValueError, TypeError):
        raise IntakeError('period must be ordered ISO dates.')

def _number(value):
    return type(value) is int and 0 <= value <= 10**15

def evaluate(m):
    findings = []
    def add(code, message, **extra):
        findings.append(dict(code=code, severity='blocking', message=message, **extra))
    def records(rows, side):
        if not isinstance(rows, list) or len(rows) > 10000:
            raise IntakeError(side + '.records must be an array of at most 10000 records.')
        indexed, sums, all_valid = {}, dict.fromkeys(FIELDS, 0), True
        for position, row in enumerate(rows):
            if not isinstance(row, dict):
                raise IntakeError('Each record must be an object.')
            rid = _text(row, 'id')
            if rid in indexed:
                add('duplicate_id', 'Duplicate record ID; reconciliation is ambiguous.', side=side, record_id=rid)
            else:
                indexed[rid] = row
            valid = all(_number(row.get(key)) for key in FIELDS)
            if not valid:
                all_valid = False
                add('invalid_numeric', 'Counts must be nonnegative integers, not booleans, strings or fractions.', side=side, record_id=rid)
                continue
            for key in FIELDS:
                sums[key] += row[key]
            if row['inspected'] != row['accepted'] + row['rejected']:
                add('record_balance', 'Inspected must equal accepted plus rejected.', side=side, record_id=rid)
            for key in ('job_id', 'period'):
                if key in row and row[key] != m[key]:
                    add('record_scope', 'Record ' + key + ' differs from manifest.', side=side, record_id=rid)
        return indexed, sums if all_valid else None
    report = m.get('report')
    if not isinstance(report, dict):
        raise IntakeError('report must be an object.')
    reported, report_totals = records(report.get('records'), 'report')
    claimed = report.get('totals')
    if not isinstance(claimed, dict):
        raise IntakeError('report.totals must be an object.')
    for field in FIELDS:
        if not _number(claimed.get(field)):
            add('invalid_total', 'Claimed total must be a nonnegative integer.', field=field)
        elif report_totals is not None and claimed[field] != report_totals[field]:
            add('total_mismatch', 'Claimed total differs from sum of report records.', field=field,
                claimed=claimed[field], calculated=report_totals[field])
    source = m.get('source_snapshot')
    source_totals = None
    usable = True
    if source is None:
        add('source_missing', 'No source snapshot supplied. Completeness cannot be established.')
        usable = False
    elif not isinstance(source, dict):
        raise IntakeError('source_snapshot must be an object or null.')
    else:
        for key in ('source_reference', 'job_id', 'period'):
            if source.get(key) != m[key]:
                usable = False
                add('source_scope', 'Source ' + key + ' is missing or differs from manifest.', field=key)
        if source.get('complete') is not True:
            usable = False
            add('source_incomplete', 'Source is not explicitly declared complete.')
        try:
            fetched = datetime.fromisoformat(source.get('fetched_at', '').replace('Z', '+00:00'))
            if fetched.tzinfo is None or fetched.utcoffset() is None:
                raise ValueError()
            if fetched > datetime.now(timezone.utc) + timedelta(minutes=5):
                usable = False
                add('source_future_timestamp', 'Source fetched_at is more than five minutes in the future.')
        except (ValueError, TypeError, AttributeError):
            usable = False
            add('source_provenance', 'Source fetched_at must be a timezone-aware ISO timestamp.')
        supplied, source_totals = records(source.get('records'), 'source')
        if not reported and not supplied:
            usable = False
            add('no_data', 'Both report and source are empty; explicit zero-activity evidence is not supported.')
        for rid in sorted(supplied.keys() - reported.keys()):
            add('missing_id', 'Source record is missing from report.', record_id=rid)
        for rid in sorted(reported.keys() - supplied.keys()):
            add('extra_id', 'Report record is absent from supplied source.', record_id=rid)
        for rid in sorted(reported.keys() & supplied.keys()):
            for field in FIELDS:
                a, b = reported[rid].get(field), supplied[rid].get(field)
                if _number(a) and _number(b) and a != b:
                    add('record_mismatch', 'Report count differs from supplied source.', record_id=rid, field=field, reported=a, source=b)
        if any(f['code'] in ('duplicate_id', 'invalid_numeric', 'record_scope') and f.get('side') == 'source' for f in findings):
            usable = False
    status = 'unable_to_verify' if not usable else ('mismatch' if findings else 'matched_supplied_snapshot')
    return dict(status=status, findings=findings, calculated_report_totals=report_totals,
                calculated_source_totals=source_totals, claimed_totals=claimed)

class ReportInbox:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / 'report_inbox.sqlite3'
        with self._connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS reports (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    report_id TEXT NOT NULL, version TEXT NOT NULL,
                    manifest_hash TEXT NOT NULL, manifest_json TEXT NOT NULL,
                    result_json TEXT NOT NULL, UNIQUE(report_id, version));
                CREATE TABLE IF NOT EXISTS preview_outbox (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    report_id TEXT NOT NULL, version TEXT NOT NULL,
                    preview_json TEXT NOT NULL, UNIQUE(report_id, version));
            ''')
    @contextmanager
    def _connect(self):
        db = sqlite3.connect(str(self.db_path), timeout=15)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA synchronous=FULL')
        try:
            with db:
                yield db
        finally:
            db.close()
    def ingest(self, manifest):
        if isinstance(manifest, (str, bytes)):
            try:
                manifest = json.loads(manifest, object_pairs_hook=_pairs,
                    parse_constant=lambda value: (_ for _ in ()).throw(IntakeError('Non-finite JSON number.')))
            except (ValueError, UnicodeError, RecursionError) as exc:
                raise IntakeError('Malformed JSON: ' + str(exc)) from exc
        if not isinstance(manifest, dict):
            raise IntakeError('Manifest must be a JSON object.')
        encoded = canonical(manifest)
        if len(encoded.encode('utf-8')) > 2_000_000:
            raise IntakeError('Manifest exceeds 2 MB.')
        # Copy through strict JSON: only immutable serialized content is evaluated/stored.
        manifest = json.loads(encoded)
        if type(manifest.get('schema_version')) is not int or manifest['schema_version'] != 1:
            raise IntakeError('Only schema_version 1 is supported.')
        if manifest.get('synthetic') is not True:
            raise IntakeError('This local demo accepts only explicitly synthetic:true manifests.')
        for key in ('report_id', 'version', 'job_id', 'source_reference'):
            _text(manifest, key)
        _period(manifest.get('period'))
        rid, version = manifest['report_id'], manifest['version']
        mh = fingerprint(manifest)
        result = evaluate(manifest)
        result.update(report_id=rid, version=version, job_id=manifest['job_id'], period=manifest['period'],
            source_reference=manifest['source_reference'], schema_version=1, synthetic=True,
            manifest_fingerprint=mh, report_fingerprint=fingerprint(manifest['report']),
            source_fingerprint=fingerprint(manifest['source_snapshot']) if manifest.get('source_snapshot') is not None else None,
            received_at=datetime.now(timezone.utc).isoformat(), verification_scope='comparison_to_supplied_snapshot',
            source_authenticity='not_authenticated', release_status='held', notice=NOTICE)
        result.update(report_hash=result['report_fingerprint'], source_hash=result['source_fingerprint'], created_at=result['received_at'])
        for finding in result['findings']:
            finding.setdefault('record_id', None)
            finding.setdefault('expected', finding.get('source', finding.get('calculated')))
            finding.setdefault('observed', finding.get('reported', finding.get('claimed')))
        preview = dict(report_id=rid, version=version, status='preview_only', recipient=None,
            send_enabled=False, message=('SYNTHETIC PREVIEW: Report ' + rid + ' v' + version + ': ' +
                result['status'] + '; ' + str(len(result['findings'])) + ' finding(s). Supplied snapshot only; release held.'))
        if result['findings']:
            preview['message'] += ' Findings: ' + ', '.join(sorted({f['code'] for f in result['findings']})) + '.'
        preview['text'] = preview['message']
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT manifest_hash,result_json FROM reports WHERE report_id=? AND version=?', (rid, version)).fetchone()
            if existing:
                if existing['manifest_hash'] != mh:
                    raise IntakeConflict('Same report ID/version has different content. Submit a new version; original preserved.')
                return dict(json.loads(existing['result_json']), idempotent=True)
            db.execute('INSERT INTO reports(report_id,version,manifest_hash,manifest_json,result_json) VALUES(?,?,?,?,?)',
                (rid, version, mh, encoded, canonical(result)))
            db.execute('INSERT INTO preview_outbox(report_id,version,preview_json) VALUES(?,?,?)', (rid, version, canonical(preview)))
        return dict(result, idempotent=False)
    def get(self, report_id, version):
        with self._connect() as db:
            row = db.execute('SELECT result_json FROM reports WHERE report_id=? AND version=?', (report_id, version)).fetchone()
        if row is None:
            raise IntakeError('Report version not found.')
        return json.loads(row['result_json'])
    def list_reports(self):
        with self._connect() as db:
            return [json.loads(row[0]) for row in db.execute('SELECT result_json FROM reports ORDER BY seq DESC')]
    def list_outbox(self):
        with self._connect() as db:
            return [json.loads(row[0]) for row in db.execute('SELECT preview_json FROM preview_outbox ORDER BY seq DESC')]
    def state(self):
        return dict(reports=self.list_reports(), outbox=self.list_outbox(), notice=NOTICE, send_enabled=False)
