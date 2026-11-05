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
            db.execute('CREATE TABLE IF NOT EXISTS client_contacts (job TEXT PRIMARY KEY, contacts_json TEXT NOT NULL)')
            db.execute('''CREATE TABLE IF NOT EXISTS api_keys (id TEXT PRIMARY KEY, name TEXT NOT NULL, key_hash TEXT NOT NULL UNIQUE,
                prefix TEXT NOT NULL, can_send INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, last_used TEXT, revoked_at TEXT)''')
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
    def ai_fix_inputs(self, file_hash, include_dismissed=False):
        from hosting.review_summary import summarize
        item = self.get(file_hash)
        _, raw = self.original_bytes(file_hash)
        out = summarize(item, use_verdicts=not include_dismissed)
        return item, raw, [i for i in out['issues'] if i['severity'] != 'info']
    def _update(self, file_hash, change):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            original, _ = self._original(db, file_hash)
            result = dict(self.latest(db, file_hash, original))
            result.pop('check_hash', None); result.pop('checked_at', None)
            change(result)
            return self.save_check(db, result)
    def approve(self, file_hash, approver, note='', override=False, withdraw=False):
        """Record that a reviewer approved this exact file version as final (or withdraw it). Sending requires it."""
        from hosting.review_summary import summarize
        item = self.get(file_hash)
        if withdraw:
            if any(x.get('sent') for x in item.get('sent') or []):
                raise ValueError('This report was already sent to the client, so its approval stays on record.')
            return self._update(file_hash, lambda r: r.pop('approval', None))
        approver = ' '.join(str(approver or '').split())[:80]
        if not approver:
            raise ValueError('Type your name to approve the report.')
        rv = summarize(item, with_issues=False)
        must = (rv.get('counts') or {}).get('high', 0)
        if must and not override:
            raise ValueError(f'{must} must-fix issue{"s are" if must != 1 else " is"} still open. Fix them first, or tick "Approve anyway".')
        newer = (item.get('fixes_applied') or [])
        if newer and not override:
            raise ValueError(f'A corrected copy of this report exists ("{newer[-1]["fixed_filename"]}"). Approve that version instead, or tick "Approve anyway".')
        record = dict(by=approver, note=' '.join(str(note or '').split())[:500], at=datetime.now(timezone.utc).isoformat(),
                      status=rv['status'], open_must_fix=must, open_to_check=(rv['counts'].get('medium', 0) + rv['counts'].get('low', 0)),
                      overridden=bool(must and override), filename=item['filename'], version=item.get('version'))
        return self._update(file_hash, lambda r: r.__setitem__('approval', record))
    def record_send(self, file_hash, record):
        return self._update(file_hash, lambda r: r.__setitem__('sent', (r.get('sent') or []) + [record]))
    def contacts_for(self, job):
        if not job:
            return {}
        with self.connect() as db:
            row = db.execute('SELECT contacts_json FROM client_contacts WHERE job=?', (job,)).fetchone()
        return json.loads(row['contacts_json']) if row else {}
    def save_contacts(self, job, to, cc):
        if not job:
            return
        with self.connect() as db:
            db.execute('INSERT INTO client_contacts (job, contacts_json) VALUES (?, ?) ON CONFLICT(job) DO UPDATE SET contacts_json=excluded.contacts_json',
                       (job, json.dumps(dict(to=to, cc=cc, saved_at=datetime.now(timezone.utc).isoformat()))))
    # ---------------- API keys (only a SHA-256 fingerprint is stored; the key is shown once)
    def create_key(self, name, can_send=False):
        import secrets
        name = ' '.join(str(name or '').split())[:60]
        if not name:
            raise ValueError('Name the key (for example: Power Automate, Nexus sync).')
        token = 'ppgv_' + secrets.token_urlsafe(32)
        key_id = 'key_' + secrets.token_hex(6)
        with self.connect() as db:
            if db.execute('SELECT COUNT(*) FROM api_keys WHERE revoked_at IS NULL').fetchone()[0] >= 25:
                raise ValueError('There are already 25 active keys. Revoke one first.')
            db.execute('INSERT INTO api_keys (id, name, key_hash, prefix, can_send, created_at) VALUES (?,?,?,?,?,?)',
                       (key_id, name, hashlib.sha256(token.encode()).hexdigest(), token[:12], 1 if can_send else 0, datetime.now(timezone.utc).isoformat()))
        return dict(self._key_row(key_id), key=token)
    def _key_row(self, key_id):
        with self.connect() as db:
            r = db.execute('SELECT * FROM api_keys WHERE id=?', (key_id,)).fetchone()
        return dict(id=r['id'], name=r['name'], prefix=r['prefix'], can_send=bool(r['can_send']), created_at=r['created_at'],
                    last_used=r['last_used'], revoked_at=r['revoked_at']) if r else None
    def list_keys(self):
        with self.connect() as db:
            ids = [r['id'] for r in db.execute('SELECT id FROM api_keys ORDER BY created_at DESC').fetchall()]
        return [self._key_row(i) for i in ids]
    def revoke_key(self, key_id):
        with self.connect() as db:
            if not db.execute("UPDATE api_keys SET revoked_at=? WHERE id=? AND revoked_at IS NULL", (datetime.now(timezone.utc).isoformat(), str(key_id))).rowcount:
                raise ValueError('That key was not found or is already revoked.')
        return self._key_row(key_id)
    def check_key(self, token):
        """The key's record when the token is valid and not revoked, else None. last_used is refreshed at most hourly."""
        if not isinstance(token, str) or not token.startswith('ppgv_') or len(token) > 100:
            return None
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.connect() as db:
            r = db.execute('SELECT id, last_used FROM api_keys WHERE key_hash=? AND revoked_at IS NULL', (digest,)).fetchone()
            if not r:
                return None
            now = datetime.now(timezone.utc)
            if not r['last_used'] or (now - datetime.fromisoformat(r['last_used'])).total_seconds() > 3600:
                db.execute('UPDATE api_keys SET last_used=? WHERE id=?', (now.isoformat(), r['id']))
        return self._key_row(r['id'])
    def record_ai_verify(self, file_hash, record):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            original, _ = self._original(db, file_hash)
            result = dict(self.latest(db, file_hash, original))
            result.pop('check_hash', None); result.pop('checked_at', None)
            result['ai_verify'] = record
            return self.save_check(db, result)
    def record_ai_fixes(self, file_hash, record):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            original, _ = self._original(db, file_hash)
            result = dict(self.latest(db, file_hash, original))
            result.pop('check_hash', None); result.pop('checked_at', None)
            result['ai_fixes'] = record
            self.save_check(db, result)
        return record
    def ai_fixes(self, file_hash, opener=None):
        """Ask Claude to write cell changes for the open issues; keep only changes that fit the file exactly."""
        item, raw, issues = self.ai_fix_inputs(file_hash)
        record = make_ai_fixes(item, raw, issues, opener)
        return self.record_ai_fixes(file_hash, record) if record['status'] == 'done' else record
    def history_for(self, job, day, exclude_hash=None):
        """Day totals of earlier saved reports for the same job (latest version per file, one entry per day)."""
        from hosting.source_compare import identify
        from hosting.baseline import stats_from_result
        if not job:
            return []
        out = []
        with self.connect() as db:
            hashes = [r[0] for r in db.execute('SELECT DISTINCT file_hash FROM spreadsheets').fetchall()]
            for file_hash in hashes:
                if file_hash == exclude_hash:
                    continue
                row = db.execute('SELECT result_json FROM spreadsheets WHERE file_hash=? ORDER BY seq DESC LIMIT 1', (file_hash,)).fetchone()
                result = self.latest(db, file_hash, json.loads(row[0]))
                j, d = identify(result.get('filename'), result)
                if not d or d == day or (j or '').upper() != job.upper() or not result.get('record_count'):
                    continue
                out.append(dict(stats_from_result(result), day=d, source='saved report', received_at=result.get('received_at', '')))
        out.sort(key=lambda h: (h['day'], h['received_at']), reverse=True)
        return [{k: v for k, v in h.items() if k != 'received_at'} for h in out]
    def record_baseline(self, file_hash, baseline):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            original, _ = self._original(db, file_hash)
            result = dict(self.latest(db, file_hash, original))
            result.pop('check_hash', None); result.pop('checked_at', None)
            result['baseline'] = baseline
            return self.save_check(db, result)
    def fix_proposals(self, file_hash):
        from hosting.fixes import proposals
        item = self.get(file_hash)
        _, raw = self.original_bytes(file_hash)
        props = proposals(item, item['filename'], raw)
        taken = {(p['sheet'], p['cell']) for p in props}
        ai = item.get('ai_fixes') or {}
        for f in ai.get('fixes') or []:
            if (f['sheet'], f['cell']) in taken:
                continue
            props.append(dict(id=f['id'], sheet=f['sheet'], cell=f['cell'], before=f['before'], after=f['after'], kind='auto',
                              reason=f['reason'], source='AI fix', confidence=f.get('confidence') or 'medium'))
        return dict(file_hash=file_hash, filename=item['filename'], proposals=props, applied=item.get('fixes_applied') or [],
                    ai=dict(status=ai.get('status') or 'not_run', made_at=ai.get('made_at'), count=len(ai.get('fixes') or []),
                            skipped=ai.get('skipped') or [], dropped=ai.get('dropped') or 0, message=ai.get('message')))
    def sheet_grid(self, file_hash, sheet=None):
        from hosting.fixes import sheet_grid
        item = self.get(file_hash)
        _, raw = self.original_bytes(file_hash)
        return sheet_grid(item['filename'], raw, sheet)
    def row_cells(self, file_hash, sheet, row):
        from hosting.fixes import row_cells
        item = self.get(file_hash)
        _, raw = self.original_bytes(file_hash)
        return row_cells(item['filename'], raw, sheet, row)
    def apply_fixes(self, file_hash, approved):
        """Write a corrected copy with the approved changes as a new upload; the original is untouched."""
        from hosting.fixes import apply_fixes, fixed_filename
        if not isinstance(approved, list) or not 1 <= len(approved) <= 500 or not all(isinstance(c, dict) for c in approved):
            raise ValueError('Send 1 to 500 approved changes.')
        item = self.get(file_hash)
        _, raw = self.original_bytes(file_hash)
        fixed = apply_fixes(item['filename'], raw, approved)
        new_item = self.ingest(fixed_filename(item['filename']), fixed)
        record = dict(fixed_file_hash=new_item['file_hash'], fixed_filename=new_item['filename'],
                      applied_at=datetime.now(timezone.utc).isoformat(),
                      changes=[{k: c.get(k) for k in ('sheet', 'cell', 'before', 'after', 'reason', 'source')} for c in approved])
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            original, _ = self._original(db, file_hash)
            result = dict(self.latest(db, file_hash, original))
            result.pop('check_hash', None); result.pop('checked_at', None)
            result['fixes_applied'] = (result.get('fixes_applied') or []) + [record]
            self.save_check(db, result)
            fixed_original, _ = self._original(db, new_item['file_hash'])
            fixed_result = dict(self.latest(db, new_item['file_hash'], fixed_original))
            fixed_result.pop('check_hash', None); fixed_result.pop('checked_at', None)
            fixed_result['fixed_from'] = dict(file_hash=file_hash, filename=item['filename'], version=item.get('version'), changes=record['changes'])
            new_item = self.save_check(db, fixed_result)
        return new_item
    def download(self, file_hash):
        with self.connect() as db:
            original, raw = self._original(db, file_hash)
        return original['filename'], raw
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
        from hosting.review_summary import summarize
        def row(result):
            item = {key: result[key] for key in fields if key in result}
            try:
                item['review'] = summarize(result, with_issues=False)
            except Exception:
                item['review'] = None
            return item
        return {'spreadsheets': [row(result) for result in results],
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
            keep={k:result[k] for k in ('approval','sent','fixes_applied','fixed_from') if result.get(k)}
            fresh=self.ingest(result['filename'],raw)
            if keep:
                fresh=self._update(file_hash,lambda r:r.update(keep))
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


def make_ai_fixes(item, raw, issues, opener=None):
    """Network call + validation (no database). {status, fixes, skipped, dropped, ...}"""
    from hosting import ai_review
    from hosting.fixes import _sources, _cell_value, _show, _addr, _pid, FixError
    if not issues:
        return dict(status='done', made_at=datetime.now(timezone.utc).isoformat(), fixes=[], skipped=[], dropped=0, message='No issues to fix.')
    try:
        answer = ai_review.fix(item, raw, issues, opener=opener)
    except ai_review.AIReviewNotConfigured as exc:
        return dict(status='not_configured', message=str(exc))
    except ai_review.AIReviewError as exc:
        return dict(status='error', message=str(exc))
    sources, _ = _sources(item['filename'], raw)
    kept, seen, dropped = [], set(), 0
    for f in answer['fixes']:
        try:
            _addr(f['cell'])
            current = _cell_value(sources, f['sheet'], f['cell'])
        except FixError:
            dropped += 1
            continue
        shown = _show(current)
        if (isinstance(current, str) and current.startswith('=')) or (f['sheet'], f['cell']) in seen or f['after'] == shown \
                or f['before'].strip() != shown.strip():
            dropped += 1
            continue
        seen.add((f['sheet'], f['cell']))
        kept.append(dict(f, before=shown, id=_pid(f['sheet'], f['cell'], f['after'])))
    return dict(status='done', model=answer['model'], made_at=datetime.now(timezone.utc).isoformat(),
                fixes=kept, skipped=answer['skipped'], dropped=dropped)


def validate_changes(item, raw, changes):
    """Keep only changes that fit the file exactly (current value matches, not a formula, one per cell)."""
    from hosting.fixes import _sources, _cell_value, _show, _addr, FixError
    sources, _ = _sources(item['filename'], raw)
    kept, seen, dropped = [], set(), []
    for f in changes:
        try:
            _addr(f['cell'])
            current = _cell_value(sources, f['sheet'], f['cell'])
        except FixError as exc:
            dropped.append(dict(f, why=str(exc)))
            continue
        shown = _show(current)
        if isinstance(current, str) and current.startswith('='):
            dropped.append(dict(f, why='formula cell')); continue
        if (f['sheet'], f['cell']) in seen:
            dropped.append(dict(f, why='same cell twice')); continue
        if f['after'] == shown:
            dropped.append(dict(f, why='already that value')); continue
        if f['before'].strip() != shown.strip():
            dropped.append(dict(f, why=f'cell holds "{shown}"')); continue
        seen.add((f['sheet'], f['cell']))
        kept.append(dict(f, before=shown))
    return kept, dropped


def chat_turn(item, raw, issues, messages, queued, opener=None, suggested=()):
    from hosting import ai_review
    try:
        answer = ai_review.chat(item, raw, issues, messages, queued, opener=opener, suggested=suggested)
    except ai_review.AIReviewNotConfigured as exc:
        return dict(status='not_configured', message=str(exc))
    except ai_review.AIReviewError as exc:
        return dict(status='error', message=str(exc))
    kept, dropped = validate_changes(item, raw, answer['changes'])
    return dict(status='done', reply=answer['reply'], changes=kept, undo=answer['undo'],
                dropped=[dict(sheet=d['sheet'], cell=d['cell'], why=d.get('why')) for d in dropped])


def make_ai_verify(item, raw, issues, opener=None):
    from hosting import ai_review
    if not issues:
        return dict(status='done', made_at=datetime.now(timezone.utc).isoformat(), verdicts={}, checker_version=item.get('checker_version'))
    try:
        out = ai_review.verify(item, raw, issues, opener=opener)
    except ai_review.AIReviewNotConfigured as exc:
        return dict(status='not_configured', message=str(exc))
    except ai_review.AIReviewError as exc:
        return dict(status='error', message=str(exc))
    return dict(status='done', model=out['model'], made_at=datetime.now(timezone.utc).isoformat(), verdicts=out['verdicts'],
                checker_version=item.get('checker_version'))
