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
        from hosting import pg_tables
        self.pg = pg_tables.current.get()   # on Vercel: real Supabase tables (created by migration); locally: SQLite
        if self.pg is not None:
            return
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
            db.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value_json TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS vin_index (ident TEXT NOT NULL, shown TEXT NOT NULL, file_hash TEXT NOT NULL, sheet TEXT, row INTEGER)')
            db.execute('CREATE INDEX IF NOT EXISTS vin_index_ident ON vin_index (ident)')
            db.execute('CREATE TABLE IF NOT EXISTS vin_indexed (file_hash TEXT PRIMARY KEY)')
            db.execute('CREATE TABLE IF NOT EXISTS dashboards (file_hash TEXT PRIMARY KEY, versions_json TEXT NOT NULL, messages_json TEXT NOT NULL, updated_at TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE, name TEXT, role TEXT NOT NULL, clients_json TEXT NOT NULL, created_at TEXT NOT NULL, last_login TEXT, disabled_at TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS login_tokens (token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL, used_at TEXT)')
            db.execute('''CREATE TABLE IF NOT EXISTS api_keys (id TEXT PRIMARY KEY, name TEXT NOT NULL, key_hash TEXT NOT NULL UNIQUE,
                prefix TEXT NOT NULL, can_send INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, last_used TEXT, revoked_at TEXT)''')
    @contextmanager
    def connect(self):
        if self.pg is not None:
            with self.pg.transaction() as db:
                yield db
            return
        db = sqlite3.connect(str(self.database), timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA synchronous=FULL')
        try:
            with db: yield db
        finally: db.close()
    def ingest(self, filename, raw):
        if not isinstance(filename, str) or any(c in filename for c in '/\\') or any(ord(c) < 32 for c in filename):
            raise ValueError('Use a plain filename without folders or control characters.')
        from hosting import documents
        doc = None
        if documents.kind(filename):   # PDF or Word: its tables become a workbook and get the same checks
            raw, filename, doc = documents.to_workbook(filename, raw)
        result = inspect_spreadsheet(filename, raw)
        if doc:
            result['source_document'] = doc
        file_hash = hashlib.sha256(raw).hexdigest()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            found = db.execute('SELECT result_json FROM spreadsheets WHERE file_hash=?', (file_hash,)).fetchone()
            if found:
                old=self.latest(db,file_hash,json.loads(found['result_json']))
                # The identical file is already stored: never a second copy, and never silently re-checked.
                out=dict(old,idempotent=True,duplicate=True)
                if old.get('checker_version')!=CHECKER_VERSION:
                    out.update(internal_check_status='outdated_checks_require_recheck',
                               recheck=dict(available=True, checked_with=old.get('checker_version'), current=CHECKER_VERSION))
                return out
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
        try:
            self.index_vins(file_hash)
        except Exception:
            pass  # search falls back to indexing later
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
        if not chunks:   # a retried "finish" after the first one already saved the file: answer with what is stored
            with self.connect() as db:
                found = db.execute('SELECT result_json FROM spreadsheets WHERE file_hash=?', (sha256,)).fetchone()
                if found:
                    return dict(self.latest(db, sha256, json.loads(found['result_json'])), idempotent=True, duplicate=True)
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
    def source_reconcile(self, file_hash, snap, day):
        """Compare the stored original (record rows and its own captured export) with a SharePoint snapshot, item by
        item. Saved as a new check revision; the workbook is not changed and no correction is applied."""
        from hosting import reconcile
        from hosting.spreadsheets import report_and_export_records
        with self.connect() as db:
            original, raw = self._original(db, file_hash)
        records, exports = report_and_export_records(original['filename'], raw)
        found = reconcile.compare(day, records, exports, snap)
        return self._update(file_hash, lambda r: r.update(source_reconciliation=found))
    def holds(self, item, review=None):
        """Why this report must stay on hold, beyond its open issues. hard=True cannot be overridden."""
        out = []
        try:
            ctx = self.assignment_context()
            a = self.assignment_for(item, review, ctx)
            if a.get('status') in ('ambiguous', 'conflict'):   # more than one client fits: a person must choose
                out.append(dict(code='client_not_settled', hard=True, text='Choose the client and job first: ' + ' '.join(a.get('evidence') or [])))
            elif a.get('status') == 'unmatched' and ctx['snapshot']:   # without SharePoint's client lists there is nothing to match against
                out.append(dict(code='client_unmatched', hard=False, text='No client list matches this report. ' + ' '.join(a.get('evidence') or [])))
        except Exception:
            out.append(dict(code='client_unmatched', hard=False, text='The client and job could not be worked out.'))
        sel = item.get('dataset_selection') or {}
        if sel.get('status') == 'ambiguous':
            out.append(dict(code='record_set_ambiguous', hard=False, text='It is not certain which sheet holds the records. ' + ' '.join(sel.get('notes') or [])))
        rec = item.get('source_reconciliation') or {}
        if rec.get('status') == 'incomplete':
            out.append(dict(code='source_coverage_incomplete', hard=False, text='The SharePoint snapshot was incomplete: ' + ' '.join((rec.get('coverage') or {}).get('notes') or [])))
        elif rec.get('status') == 'discrepancies':
            out.append(dict(code='source_discrepancies', hard=False, text=f'{len(rec.get("rows") or [])} difference(s) from SharePoint are open.'))
        return out
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
            def undo(r):   # keep a record of the withdrawal (it also keeps this revision distinct from the pre-approval one)
                was = r.pop('approval', None)
                r['approval_withdrawn'] = dict(at=datetime.now(timezone.utc).isoformat(), by=' '.join(str(approver or '').split())[:80] or None,
                                               version=(was or {}).get('version'))
            return self._update(file_hash, undo)
        approver = ' '.join(str(approver or '').split())[:80]
        if not approver:
            raise ValueError('Type your name to approve the report.')
        rv = summarize(item, with_issues=False)
        must = (rv.get('counts') or {}).get('high', 0)
        holds = self.holds(item, rv)
        hard = [x for x in holds if x['hard']]
        if hard:
            raise ValueError(hard[0]['text'])
        soft = [x for x in holds if not x['hard']]
        if soft and not override:
            raise ValueError(soft[0]['text'] + ' Resolve it first, or tick "Approve anyway" and say why in the note.')
        if soft and override and not ' '.join(str(note or '').split()):
            raise ValueError('Say why you are approving anyway in the note.')
        if must and not override:
            raise ValueError(f'{must} must-fix issue{"s are" if must != 1 else " is"} still open. Fix them first, or tick "Approve anyway".')
        newer = (item.get('fixes_applied') or [])
        if newer and not override:
            raise ValueError(f'A corrected copy of this report exists ("{newer[-1]["fixed_filename"]}"). Approve that version instead, or tick "Approve anyway".')
        record = dict(by=approver, note=' '.join(str(note or '').split())[:500], at=datetime.now(timezone.utc).isoformat(),
                      status=rv['status'], open_must_fix=must, open_to_check=(rv['counts'].get('medium', 0) + rv['counts'].get('low', 0)),
                      overridden=bool((must or soft) and override), holds_overridden=[x['code'] for x in soft],
                      filename=item['filename'], version=item.get('version'))
        def change(r):
            r['approval'] = record
            if (r.get('review_request') or {}).get('status') in ('waiting', 'sent_back'):
                rr = dict(r['review_request'])
                rr.update(status='approved', reviewer=approver, answered_at=record['at'])
                rr['history'] = (rr.get('history') or []) + [dict(action='approve', by=approver, note=record['note'], at=record['at'])]
                r['review_request'] = rr
        return self._update(file_hash, change)
    def review_action(self, file_hash, action, by, note=''):
        """Ask a reviewer (usually the boss) to sign the report off, or send it back with a comment."""
        by = ' '.join(str(by or '').split())[:80]
        note = ' '.join(str(note or '').split())[:1000]
        if not by:
            raise ValueError('Type your name.')
        if action not in ('request', 'send_back'):
            raise ValueError('Unknown review action.')
        if action == 'send_back' and not note:
            raise ValueError('Say what needs changing, so it can be fixed.')
        now = datetime.now(timezone.utc).isoformat()
        def change(r):
            rr = dict(r.get('review_request') or {})
            hist = list(rr.get('history') or [])
            hist.append(dict(action=action, by=by, note=note, at=now))
            if action == 'request':
                rr.update(status='waiting', by=by, note=note, at=now)
            else:
                rr.update(status='sent_back', reviewer=by, comment=note, answered_at=now)
            rr['history'] = hist[-20:]
            r['review_request'] = rr
        return self._update(file_hash, change)
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
    # ---------------- dashboards (current page first, up to 10 earlier versions for undo, and the conversation)
    def dashboard(self, file_hash):
        with self.connect() as db:
            row = db.execute('SELECT * FROM dashboards WHERE file_hash=?', (file_hash,)).fetchone()
        if not row:
            return None
        versions = json.loads(row['versions_json'])
        return dict(html=versions[0], undo=len(versions) > 1, messages=json.loads(row['messages_json']), updated_at=row['updated_at'])
    def save_dashboard(self, file_hash, page, messages=(), reset=False):
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as db:
            row = db.execute('SELECT * FROM dashboards WHERE file_hash=?', (file_hash,)).fetchone()
            versions = [] if reset or not row else json.loads(row['versions_json'])
            talk = [] if reset or not row else json.loads(row['messages_json'])
            versions = ([page] + versions)[:11]
            talk = (talk + list(messages))[-40:]
            db.execute('INSERT INTO dashboards (file_hash, versions_json, messages_json, updated_at) VALUES (?,?,?,?) '
                       'ON CONFLICT(file_hash) DO UPDATE SET versions_json=excluded.versions_json, messages_json=excluded.messages_json, updated_at=excluded.updated_at',
                       (file_hash, json.dumps(versions), json.dumps(talk), now))
        return self.dashboard(file_hash)
    def undo_dashboard(self, file_hash):
        with self.connect() as db:
            row = db.execute('SELECT * FROM dashboards WHERE file_hash=?', (file_hash,)).fetchone()
            if not row or len(json.loads(row['versions_json'])) < 2:
                raise ValueError('There is no earlier version to go back to.')
            versions = json.loads(row['versions_json'])[1:]
            talk = json.loads(row['messages_json']) + [dict(role='assistant', text='Went back to the previous version.', at=datetime.now(timezone.utc).isoformat())]
            db.execute('UPDATE dashboards SET versions_json=?, messages_json=?, updated_at=? WHERE file_hash=?',
                       (json.dumps(versions), json.dumps(talk[-40:]), datetime.now(timezone.utc).isoformat(), file_hash))
        return self.dashboard(file_hash)
    def reports_for(self, job, start, end):
        """{day: summary} for the job's daily reports between start and end (the upload that counts for each day)."""
        from hosting.review_summary import summarize
        from hosting.source_compare import identify
        from hosting.periods import pick_daily
        with self.connect() as db:
            rows = db.execute('SELECT file_hash, result_json FROM spreadsheets').fetchall()
            found = []
            for r in rows:
                base = json.loads(r['result_json'])
                j, d = identify(base.get('filename'), base)
                if (j or '').upper() == job.upper() and d and start.isoformat() <= d <= end.isoformat():
                    found.append(self.latest(db, r['file_hash'], base))
        by_day = {}
        for result in found:
            rv = summarize(result, with_issues=False)
            by_day.setdefault(rv['day'], []).append(dict(file_hash=result['file_hash'], filename=result['filename'], version=result.get('version'),
                                                         received_at=result.get('received_at'), review=rv))
        out = {}
        for d, cands in by_day.items():
            c = pick_daily(cands)
            rv = c['review']
            out[d] = dict(file_hash=c['file_hash'], filename=c['filename'], version=c['version'], status=rv['status'], counts=rv['counts'],
                          scans=float(rv['scans']) if rv.get('scans') is not None else None, inspected=rv.get('inspected'), rejected=rv.get('rejected'),
                          approved=rv.get('approved'), sent_at=rv.get('sent_at'), uploads=len(cands),
                          fixed_from=rv.get('fixed_from'), fixes_applied=rv.get('fixes_applied') or 0)
        return out
    def all_results(self):
        with self.connect() as db:
            rows = db.execute('SELECT file_hash, result_json FROM spreadsheets').fetchall()
            return [self.latest(db, r['file_hash'], json.loads(r['result_json'])) for r in rows]
    def jobs(self):
        from hosting.source_compare import identify
        with self.connect() as db:
            rows = db.execute('SELECT result_json FROM spreadsheets').fetchall()
        return sorted({j.upper() for j in (identify(json.loads(r['result_json']).get('filename'), json.loads(r['result_json']))[0] for r in rows) if j})
    # ---------------- VIN / identifier index: which reports a VIN is in
    @staticmethod
    def vin_key(text):
        return re.sub(r'[^A-Z0-9]', '', str(text or '').upper())
    VIN_INDEX_MAX_BYTES = 3 * 1024 * 1024

    def index_vins(self, file_hash):
        from hosting.spreadsheets import report_records
        with self.connect() as db:
            if db.execute('SELECT 1 FROM vin_indexed WHERE file_hash=?', (file_hash,)).fetchone():
                return
            original, raw = self._original(db, file_hash)
        try:   # a file this big is a load test, not an end-of-shift report: reading it would stall every request
            recs = [] if len(raw or b'') > self.VIN_INDEX_MAX_BYTES else [r for r in report_records(original['filename'], raw) if r.get('table_kind') == 'detail']
        except Exception:
            recs = []
        rows = [(self.vin_key(r['labels'].get('identifier')), str(r['labels'].get('identifier')).strip(), file_hash, r['sheet'], r['row'])
                for r in recs if self.vin_key((r.get('labels') or {}).get('identifier'))]
        with self.connect() as db:
            if self.pg is not None:   # two requests indexing the same report at once: the second waits, then finds it done
                db.execute('SELECT pg_advisory_xact_lock(hashtext(?))', ('vin:' + file_hash,))
                if db.execute('SELECT 1 FROM vin_indexed WHERE file_hash=?', (file_hash,)).fetchone():
                    return
            db.execute('DELETE FROM vin_index WHERE file_hash=?', (file_hash,))
            db.executemany('INSERT INTO vin_index (ident, shown, file_hash, sheet, row) VALUES (?,?,?,?,?)', rows)
            db.execute('INSERT OR IGNORE INTO vin_indexed (file_hash) VALUES (?)', (file_hash,))
    def index_missing(self, budget=40, seconds=4.0):
        """Index reports uploaded before the index existed, a few at a time and never for more than `seconds`
        (so a request never runs long). Returns how many are left."""
        import time
        stop = time.monotonic() + seconds
        with self.connect() as db:
            todo = [r['file_hash'] for r in db.execute('SELECT file_hash FROM spreadsheets WHERE file_hash NOT IN (SELECT file_hash FROM vin_indexed) ORDER BY seq DESC').fetchall()]
        done = 0
        for h in todo[:budget]:
            if time.monotonic() >= stop:
                break
            self.index_vins(h)
            done += 1
        return len(todo) - done
    def find_vin(self, query, limit=50):
        from hosting.source_compare import identify
        q = self.vin_key(query)
        if len(q) < 4:
            raise ValueError('Type at least 4 letters or digits of the VIN or identifier.')
        left = self.index_missing()
        with self.connect() as db:
            hits = db.execute('SELECT ident, shown, file_hash, sheet, row FROM vin_index WHERE ident = ? OR ident LIKE ? ORDER BY ident = ? DESC LIMIT ?',
                              (q, f'%{q}%', q, limit)).fetchall()
            meta = {}
            for h in {x['file_hash'] for x in hits}:
                base = json.loads(db.execute('SELECT result_json FROM spreadsheets WHERE file_hash=?', (h,)).fetchone()['result_json'])
                result = self.latest(db, h, base)
                job, day = identify(result.get('filename'), result)
                meta[h] = dict(filename=result['filename'], version=result.get('version'), job=job, day=day,
                               corrected=bool(result.get('fixed_from')), sent=any(x.get('sent') for x in result.get('sent') or []))
        out = [dict(identifier=x['shown'], exact=x['ident'] == q, file_hash=x['file_hash'], sheet=x['sheet'], row=x['row'], **meta[x['file_hash']]) for x in hits]
        out.sort(key=lambda m: (not m['exact'], m['day'] or '', m['filename']), reverse=False)
        return dict(query=query, results=out, still_indexing=left)
    def repeats_elsewhere(self, file_hash, limit=20, index_old=True):
        """VINs in this report that are also in the same job's report for a different day: a re-sent old file, or a car counted twice."""
        from hosting.source_compare import identify
        item = self.get(file_hash)
        job, day = identify(item.get('filename'), item)
        if not job or not day:
            return []
        self.index_vins(file_hash)
        if index_old:
            self.index_missing(budget=3, seconds=2.0)   # older reports get indexed a few per view, never blocking for long
        with self.connect() as db:
            rows = db.execute('SELECT DISTINCT b.ident, b.shown, b.file_hash FROM vin_index a JOIN vin_index b ON a.ident = b.ident AND b.file_hash != a.file_hash '
                              'WHERE a.file_hash=? LIMIT 2000', (file_hash,)).fetchall()
            meta = {}
            for h in {r['file_hash'] for r in rows}:
                found = db.execute('SELECT result_json FROM spreadsheets WHERE file_hash=?', (h,)).fetchone()
                if found:
                    result = self.latest(db, h, json.loads(found['result_json']))
                    j, d = identify(result.get('filename'), result)
                    meta[h] = dict(job=(j or '').upper(), day=d, filename=result['filename'])
        out, seen = [], set()
        for r in rows:
            o = meta.get(r['file_hash'])
            if not o or o['job'] != job.upper() or not o['day'] or o['day'] == day or (r['ident'], o['day']) in seen:
                continue
            seen.add((r['ident'], o['day']))
            out.append(dict(vin=r['shown'], day=o['day'], filename=o['filename'], file_hash=r['file_hash']))
        out.sort(key=lambda x: (x['day'], x['vin']))
        return out[:limit] if limit else out
    def vins_in(self, file_hashes):
        """{file_hash: set of VIN keys}. Indexing old reports stops after a few seconds; the rest are indexed on later views."""
        import time
        stop = time.monotonic() + 6.0
        for h in file_hashes:
            if time.monotonic() >= stop:
                break
            self.index_vins(h)
        out = {h: set() for h in file_hashes}
        with self.connect() as db:
            for h in file_hashes:
                out[h] = {r['ident'] for r in db.execute('SELECT ident FROM vin_index WHERE file_hash=?', (h,)).fetchall()}
        return out
    # ---------------- settings (shift times, reviewer rules)
    def setting(self, key, default=None):
        with self.connect() as db:
            row = db.execute('SELECT value_json FROM settings WHERE key=?', (key,)).fetchone()
        return json.loads(row['value_json']) if row else default
    def settings_with_prefix(self, prefix):
        with self.connect() as db:
            rows = db.execute('SELECT key, value_json FROM settings WHERE key LIKE ?', (prefix.replace('%', '') + '%',)).fetchall()
        return {r['key'][len(prefix):]: json.loads(r['value_json']) for r in rows}
    def assignment_context(self):
        from hosting import registry
        return dict(registry=registry.entries(self.setting(registry.REGISTRY_KEY, [])), snapshot=self.setting(registry.SNAPSHOT_KEY, []) or [],
                    manual=self.settings_with_prefix('assignment|'))
    def assignment_for(self, result, review, ctx=None):
        from hosting import registry
        ctx = ctx or self.assignment_context()
        rv = review or {}
        a = registry.resolve(result, result.get('filename'), rv.get('job'), rv.get('day'), ctx['registry'], ctx['snapshot'],
                             ctx['manual'].get(result.get('file_hash')))
        a['label'] = registry.label(a)
        a['needs_person'] = a['status'] in registry.NEEDS_PERSON
        return a
    def set_setting(self, key, value):
        with self.connect() as db:
            db.execute('INSERT INTO settings (key, value_json) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json',
                       (key, json.dumps(value)))
        return value
    def rules_for(self, job):
        """The reviewer's "also look for" rules that apply to this job (all-jobs rules plus the job's own)."""
        stored = self.setting('ai_rules', []) or []
        return [r['text'] for r in stored if not r.get('job') or (job and r['job'].upper() == job.upper())]
    def shift_schedule(self, job):
        from hosting.shifts import for_job
        return for_job(self.setting('shifts', None), job)
    def shift_moves(self, file_hash):
        """[(sheet, row, target sheet or None)] for rows the SharePoint comparison found on the wrong shift."""
        from hosting.shifts import sheet_shift
        item = self.get(file_hash)
        tabs = [s.get('name') for s in item.get('sheets') or [] if s.get('name')]
        moves = []
        for f in (item.get('sharepoint') or {}).get('findings') or []:
            if f.get('code') != 'wrong_shift' or not f.get('row'):
                continue
            target = next((t for t in tabs if t != f.get('sheet') and sheet_shift(t) == str(f.get('target_shift'))), None)
            moves.append((f['sheet'], f['row'], target))
        return moves
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
        from hosting import suggest, sharepoint_jobs
        from hosting.source_compare import identify
        job, day = identify(item.get('filename'), item)
        ctx = suggest.context(job, day, self.setting(sharepoint_jobs.KEY))
        props = proposals(item, item['filename'], raw, context=ctx)
        taken = {(p['sheet'], p['cell']) for p in props}
        ai = item.get('ai_fixes') or {}
        for f in ai.get('fixes') or []:
            if (f['sheet'], f['cell']) in taken:
                continue
            props.append(dict(id=f['id'], sheet=f['sheet'], cell=f['cell'], before=f['before'], after=f['after'], kind='auto',
                              reason=f['reason'], source='AI fix', confidence=f.get('confidence') or 'medium', sure=False))
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
        if not isinstance(approved, list) or not approved or not all(isinstance(c, dict) for c in approved):
            raise ValueError('Send at least one approved change.')
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
            if result.get('source_document'): fixed_result['source_document'] = result['source_document']   # still came from that PDF / Word file
            if result.get('accepted_issues'): fixed_result['accepted_issues'] = result['accepted_issues']     # still confirmed fine
            for k in ('auto_formatting', 'ai_fixed'):   # what was already fixed stays on the report as it moves on
                if result.get(k) and k not in fixed_result: fixed_result[k] = result[k]
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
            results = []
            for stored in rows:   # one unreadable report never empties the whole list
                base = json.loads(stored['result_json'])
                try:
                    results.append(self.latest(db, base['file_hash'], base))
                except Exception:
                    results.append(dict(base, internal_check_status='could_not_load_latest_check'))
        for result in results:
            if result.get('checker_version')!=CHECKER_VERSION:
                result['internal_check_status']='outdated_checks_require_recheck'
        # List endpoint is bounded; detailed results remain available by hash.
        fields = ('file_hash','filename','version','received_at','format','status','internal_check_status','verification_scope','release_status','finding_count','findings_truncated','sheet_count','row_count')
        from hosting.review_summary import summarize
        try:
            ctx = self.assignment_context()
        except Exception:
            ctx = None
        def row(result):
            item = {key: result[key] for key in fields if key in result}
            try:
                item['review'] = summarize(result, with_issues=False)
            except Exception:
                item['review'] = None
            if ctx is not None:
                try:
                    item['assignment'] = self.assignment_for(result, item['review'], ctx)
                except Exception:
                    item['assignment'] = None
            return item
        return {'spreadsheets': [row(result) for result in results],
                'count': count, 'limit':100, 'has_more': count > 100,
                'notice':'Mapped checks and optional separately supplied source comparisons. No source authentication, business approval or release.'}
    def get(self, file_hash):
        """The stored result. A result made by an older checker is returned as it is, marked outdated: new checks
        are only shown as a proposal (propose_recheck) and saved when a person applies them (apply_recheck)."""
        with self.connect() as db:
            found = db.execute('SELECT result_json FROM spreadsheets WHERE file_hash=?', (file_hash,)).fetchone()
            if not found: raise ValueError('Spreadsheet version not found.')
            result=self.latest(db,file_hash,json.loads(found['result_json']))
        if result.get('checker_version')!=CHECKER_VERSION:
            result=dict(result, internal_check_status='outdated_checks_require_recheck',
                        recheck=dict(available=True, checked_with=result.get('checker_version'), current=CHECKER_VERSION))
        return result
    RECHECK_KEEP = ('approval','sent','fixes_applied','fixed_from','review_request','source_document','accepted_issues','auto_formatting','ai_fixed',
                    'source_reconciliation','approval_withdrawn','recheck_applied')
    def _fresh_checks(self, file_hash):
        with self.connect() as db:
            original, raw = self._original(db, file_hash)
            current = dict(self.latest(db, file_hash, original))
        fresh = inspect_spreadsheet(original['filename'], raw)
        fresh.update({k: original[k] for k in ('file_hash','filename','version','original_path','received_at')})
        fresh.update(finding_count=sum(fresh.get('finding_counts',{}).values()), sheet_count=len(fresh.get('sheets',[])))
        return current, fresh
    def propose_recheck(self, file_hash):
        """What the current checker would change on this report, without saving anything to the report."""
        from hosting.review_summary import summarize
        current, fresh = self._fresh_checks(file_hash)
        def facts(r):
            rv = summarize(r, with_issues=False)
            try:
                a = self.assignment_for(r, rv)
            except Exception:
                a = {}
            t = r.get('totals') or {}
            return dict(checker_version=r.get('checker_version'), record_count=r.get('record_count'),
                        inspected=(t.get('inspected') or {}).get('total'), certified=(t.get('certified') or {}).get('total'),
                        rejected=(t.get('rejected') or {}).get('total'), finding_count=r.get('finding_count'),
                        job=rv.get('job'), day=rv.get('day'), client=a.get('client'), assignment=a.get('label'), assignment_status=a.get('status'),
                        authoritative=(r.get('dataset_selection') or {}).get('authoritative'),
                        compared=(r.get('dataset_selection') or {}).get('compared'))
        before, after = facts(current), facts(fresh)
        changes = [dict(field=k, before=before[k], after=after[k]) for k in before if before[k] != after[k] and k != 'checker_version']
        proposal = dict(file_hash=file_hash, filename=current.get('filename'), made_at=datetime.now(timezone.utc).isoformat(),
                        before=before, after=after, changes=changes,
                        selection=fresh.get('dataset_selection'), applied=False,
                        note='Nothing on the report has changed. Apply only after reviewing these differences.')
        self.set_setting(f'recheck_proposal|{file_hash}', proposal)
        return proposal
    def apply_recheck(self, file_hash, by):
        """Save the current checker's results as a new revision of this report, keeping approvals, sends, fixes,
        accepted issues and the reconciliation. The original upload and earlier revisions stay as they were."""
        by = ' '.join(str(by or '').split())[:80]
        if not by:
            raise ValueError('Type your name to apply the new checks.')
        current, fresh = self._fresh_checks(file_hash)
        proposal = self.setting(f'recheck_proposal|{file_hash}', None)
        keep = {k: current[k] for k in self.RECHECK_KEEP if current.get(k)}
        if keep.get('source_reconciliation'):
            keep['source_reconciliation'] = dict(keep['source_reconciliation'], stale=True, stale_reason='Made with the earlier checks; reconcile again.')
        fresh.update(keep)
        fresh['recheck_applied'] = (current.get('recheck_applied') or []) + [dict(
            by=by, at=datetime.now(timezone.utc).isoformat(), from_version=current.get('checker_version'), to_version=fresh.get('checker_version'),
            from_check=current.get('check_hash'), changes=(proposal or {}).get('changes'))]
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            saved = self.save_check(db, fresh)
        previous = current.get('sharepoint') or {}
        if previous.get('status') in ('match','differences') and previous.get('source_sha256'):
            # Re-run the last SharePoint comparison with the stored export so it reflects the new checks.
            name=previous.get('source_filename') or 'export.csv'
            stored=files_for(self.root).get('uploads/'+previous['source_sha256']+('.xlsx' if name.lower().endswith('.xlsx') else '.csv'))
            meta={k:previous[k] for k in ('origin','source_label','site','path','web_url','modified','job','day') if k in previous}
            saved = self.compare_source(file_hash,name,stored,meta)
        if proposal:
            self.set_setting(f'recheck_proposal|{file_hash}', dict(proposal, applied=True, applied_by=by))
        return saved
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


def make_ai_verify(item, raw, issues, opener=None, rules=None):
    from hosting import ai_review
    if not issues:
        return dict(status='done', made_at=datetime.now(timezone.utc).isoformat(), verdicts={}, checker_version=item.get('checker_version'))
    try:
        out = ai_review.verify(item, raw, issues, opener=opener, rules=rules)
    except ai_review.AIReviewNotConfigured as exc:
        return dict(status='not_configured', message=str(exc))
    except ai_review.AIReviewError as exc:
        return dict(status='error', message=str(exc))
    return dict(status='done', model=out['model'], made_at=datetime.now(timezone.utc).isoformat(), verdicts=out['verdicts'],
                checker_version=item.get('checker_version'))
