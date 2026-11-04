"""Immutable originals and versioned internal-check results, inside the durable workspace."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from hosting.spreadsheets import inspect_spreadsheet,CHECKER_VERSION
from hosting.files import files_for
from hosting.branded_reports import fingerprint
from hosting.reconcile_spreadsheet import reconcile_result
from hosting.report_workflow import stamp,canonical

class SpreadsheetInbox:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.database = self.root / 'spreadsheet_inbox.sqlite3'
        with self.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS spreadsheets (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                file_hash TEXT NOT NULL UNIQUE,
                filename TEXT NOT NULL,
                version INTEGER NOT NULL,
                result_json TEXT NOT NULL
            )''')
            db.execute('''CREATE TABLE IF NOT EXISTS spreadsheet_checks (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, file_hash TEXT NOT NULL,
                check_hash TEXT NOT NULL UNIQUE, result_json TEXT NOT NULL)''')
            db.execute('CREATE TABLE IF NOT EXISTS spreadsheet_sources (source_hash TEXT PRIMARY KEY, source_json TEXT NOT NULL)')
    @contextmanager
    def connect(self):
        db = sqlite3.connect(str(self.database), timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA synchronous=FULL')
        try:
            with db: yield db
        finally: db.close()
    def ingest(self, filename, raw):
        if not isinstance(filename, str) or any(c in filename for c in '/\\') or any(ord(c) < 32 for c in filename):
            raise ValueError('Use a plain filename without folders or control characters.')
        result = inspect_spreadsheet(filename, raw)
        file_hash = hashlib.sha256(raw).hexdigest()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            found = db.execute('SELECT result_json FROM spreadsheets WHERE file_hash=?', (file_hash,)).fetchone()
            if found:
                old=self.latest(db,file_hash,json.loads(found['result_json']))
                if old.get('checker_version')==CHECKER_VERSION:return dict(old,idempotent=True)
                result.update({k:old[k] for k in ('file_hash','filename','version','original_path','received_at')})
                result.update(finding_count=sum(result.get('finding_counts',{}).values()),sheet_count=len(result['sheets']))
                return dict(self.save_check(db,result),idempotent=True,rechecked=True)
            version = db.execute('SELECT COALESCE(MAX(version),0)+1 FROM spreadsheets WHERE filename=?', (filename,)).fetchone()[0]
            suffix = '.xlsx' if filename.lower().endswith('.xlsx') else '.csv'
            relative = 'uploads/' + file_hash + suffix
            files_for(self.root).put(relative, raw)  # write-once; a differing existing file raises
            result.update(file_hash=file_hash, filename=filename, version=version,
                finding_count=sum(result.get('finding_counts', {}).values()), sheet_count=len(result.get('sheets', [])),
                original_path=relative, received_at=datetime.now(timezone.utc).isoformat())
            db.execute('INSERT INTO spreadsheets(file_hash,filename,version,result_json) VALUES(?,?,?,?)',
                       (file_hash, filename, version, json.dumps(result, allow_nan=False)))
            result=self.save_check(db,result)
        return dict(result, idempotent=False)
    def store_part(self, upload_id, index, raw):
        if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < 100000:
            raise ValueError('Upload part index must be an integer from 0 to 99999.')
        if not raw:
            raise ValueError('Empty upload part.')
        files_for(self.root).put_part(upload_id, index, raw)
        return {'upload_id': upload_id, 'part_index': index, 'bytes': len(raw)}
    def finish_upload(self, upload_id, filename, part_count, sha256):
        if not isinstance(part_count, int) or isinstance(part_count, bool) or not 0 < part_count <= 100000:
            raise ValueError('Upload part count is invalid.')
        if not isinstance(sha256, str) or not re.fullmatch(r'[0-9a-f]{64}', sha256):
            raise ValueError('A SHA-256 of the complete file is required.')
        chunks = files_for(self.root).take_parts(upload_id)
        if len(chunks) != part_count:
            raise ValueError(f'Upload incomplete: {len(chunks)} of {part_count} parts arrived. Nothing was saved; upload the file again.')
        raw = b''.join(chunks)
        if hashlib.sha256(raw).hexdigest() != sha256:
            raise ValueError('Uploaded bytes do not match the file hash computed in the browser. Nothing was saved; upload the file again.')
        return self.ingest(filename, raw)
    def _original(self, db, file_hash):
        row = db.execute('SELECT result_json FROM spreadsheets WHERE file_hash=?', (file_hash,)).fetchone()
        if not row:
            raise ValueError('Spreadsheet version not found.')
        original = json.loads(row['result_json'])
        raw = files_for(self.root).get(original['original_path'])
        if hashlib.sha256(raw).hexdigest() != file_hash:
            raise ValueError('Stored original integrity mismatch.')
        return original, raw
    def compare_source(self, file_hash, source_name, source_raw, meta=None):
        """Compare the report with a SharePoint export (fetched or uploaded); saved as a new review revision."""
        from hosting.source_compare import parse_export, compare, digest
        from hosting.spreadsheets import report_records
        if not source_raw:
            raise ValueError('The SharePoint export is empty.')
        source_rows = parse_export(source_name, source_raw)
        self.get(file_hash)  # make sure the report's own checks are current before layering the comparison
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            original, raw = self._original(db, file_hash)
            rows = report_records(original['filename'], raw)
            if not rows:
                raise ValueError('No report rows with Inspected quantities were found to compare.')
            sha = digest(source_raw)
            suffix = '.xlsx' if source_name.lower().endswith('.xlsx') else '.csv'
            files_for(self.root).put('uploads/' + sha + suffix, source_raw)
            comparison = compare(rows, source_rows, dict(meta or {}, source_filename=source_name, source_sha256=sha))
            result = dict(self.latest(db, file_hash, original))
            result.pop('check_hash', None); result.pop('checked_at', None)
            result['sharepoint'] = comparison
            result['source_reconciliation_status'] = 'match' if comparison['status'] == 'match' else 'mismatch'
            if comparison['status'] != 'match':
                result['internal_check_status'] = 'findings'
            return self.save_check(db, result)
    def record_lookup(self, file_hash, status, message, job=None, day=None):
        self.get(file_hash)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            original, _ = self._original(db, file_hash)
            result = dict(self.latest(db, file_hash, original))
            if (result.get('sharepoint') or {}).get('status') in ('match', 'differences'):
                return result  # keep the last real comparison
            result.pop('check_hash', None); result.pop('checked_at', None)
            result['sharepoint'] = dict(status=status, message=message, job=job, day=day)
            return self.save_check(db, result)
    def original_bytes(self, file_hash):
        with self.connect() as db:
            original, raw = self._original(db, file_hash)
        return original, raw
    def record_ai_review(self, file_hash, ai_review):
        """Save Claude's suggestions with the current check. They never change findings or status."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            original, _ = self._original(db, file_hash)
            result = dict(self.latest(db, file_hash, original))
            if ai_review.get('status') != 'done' and (result.get('ai_review') or {}).get('status') == 'done':
                return result  # keep the last real review when a retry fails
            result.pop('check_hash', None); result.pop('checked_at', None)
            result['ai_review'] = ai_review
            return self.save_check(db, result)
    def list(self):
        with self.connect() as db:
            rows = db.execute('SELECT result_json FROM spreadsheets ORDER BY seq DESC LIMIT 100').fetchall()
            count = db.execute('SELECT COUNT(*) FROM spreadsheets').fetchone()[0]
            results = [self.latest(db,json.loads(row['result_json'])['file_hash'],json.loads(row['result_json'])) for row in rows]
        for result in results:
            if result.get('checker_version')!=CHECKER_VERSION:
                result['internal_check_status']='outdated_checks_require_recheck'
        # List endpoint is bounded; detailed results remain available by hash.
        fields = ('file_hash','filename','version','received_at','format','status','internal_check_status','verification_scope','release_status','finding_count','findings_truncated','sheet_count','row_count')
        return {'spreadsheets': [{key: result[key] for key in fields if key in result} for result in results],
                'count': count, 'limit':100, 'has_more': count > 100,
                'notice':'Mapped checks and optional separately supplied source comparisons. No source authentication, business approval or release.'}
    def get(self, file_hash):
        with self.connect() as db:
            found = db.execute('SELECT result_json FROM spreadsheets WHERE file_hash=?', (file_hash,)).fetchone()
            if not found: raise ValueError('Spreadsheet version not found.')
            result=self.latest(db,file_hash,json.loads(found['result_json']))
        if result.get('checker_version')!=CHECKER_VERSION:
            suffix='.xlsx' if result['filename'].lower().endswith('.xlsx') else '.csv'
            raw=files_for(self.root).get('uploads/'+file_hash+suffix)
            if hashlib.sha256(raw).hexdigest()!=file_hash:raise ValueError('Stored original integrity mismatch.')
            previous=result.get('sharepoint') or {}
            fresh=self.ingest(result['filename'],raw)
            if previous.get('status') in ('match','differences') and previous.get('source_sha256'):
                # Re-run the last SharePoint comparison with the stored export so it reflects the new checks.
                name=previous.get('source_filename') or 'export.csv'
                stored=files_for(self.root).get('uploads/'+previous['source_sha256']+('.xlsx' if name.lower().endswith('.xlsx') else '.csv'))
                meta={k:previous[k] for k in ('origin','source_label','site','path','web_url','modified','job','day') if k in previous}
                return self.compare_source(file_hash,name,stored,meta)
            return fresh
        return result
    def latest(self,db,file_hash,fallback):
        row=db.execute('SELECT result_json FROM spreadsheet_checks WHERE file_hash=? ORDER BY seq DESC LIMIT 1',(file_hash,)).fetchone()
        return json.loads(row['result_json']) if row else fallback
    def save_check(self,db,result):
        check_hash=fingerprint(result)
        old=db.execute('SELECT result_json FROM spreadsheet_checks WHERE check_hash=?',(check_hash,)).fetchone()
        if old:
            latest=self.latest(db,result['file_hash'],{})
            if latest.get('check_hash')!=check_hash:raise ValueError('This historical check is not the current review revision. Reload current evidence.')
            return dict(json.loads(old['result_json']),idempotent=True)
        result=dict(result,check_hash=check_hash,checked_at=datetime.now(timezone.utc).isoformat())
        db.execute('INSERT INTO spreadsheet_checks(file_hash,check_hash,result_json) VALUES(?,?,?)',(result['file_hash'],check_hash,json.dumps(result,allow_nan=False)))
        return dict(result,idempotent=False)
    def reconcile(self,file_hash,source):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT result_json FROM spreadsheets WHERE file_hash=?',(file_hash,)).fetchone()
            if not row:raise ValueError('Spreadsheet version not found.')
            original=json.loads(row['result_json']);raw=files_for(self.root).get(original['original_path'])
            if hashlib.sha256(raw).hexdigest()!=file_hash:raise ValueError('Stored original integrity mismatch.')
            result=reconcile_result(inspect_spreadsheet(original['filename'],raw),source)
            latest=self.latest(db,file_hash,original)
            if latest.get('source_sha256') and latest['source_sha256']!=result['source_sha256']:
                if not result.get('source_captured_at') or not latest.get('source_captured_at'):
                    raise ValueError('Different source revisions cannot be ordered without established capture times. Current evidence was not replaced.')
                if stamp(result['source_captured_at'])<=stamp(latest['source_captured_at']):raise ValueError('Newer or conflicting source evidence is already recorded. Do not replace current review with an old snapshot.')
            from hosting.report_workflow import parse
            source_json=canonical(parse(source))
            stored=db.execute('SELECT source_json FROM spreadsheet_sources WHERE source_hash=?',(result['source_sha256'],)).fetchone()
            if stored and stored['source_json']!=source_json:raise ValueError('Stored source evidence integrity conflict.')
            if not stored:db.execute('INSERT INTO spreadsheet_sources(source_hash,source_json) VALUES(?,?)',(result['source_sha256'],source_json))
            result.update({k:original[k] for k in ('file_hash','filename','version','original_path','received_at')})
            result['sheet_count']=len(result['sheets'])
            return self.save_check(db,result)
