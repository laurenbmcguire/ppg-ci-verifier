"""Immutable originals and versioned internal-check results, inside the durable workspace."""
from contextlib import contextmanager, nullcontext
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

def _known_details(filename, raw):
    """Job type and description from the report's own record rows (the value used on most rows of 'Service performed'
    / 'Inspected for' style columns), to fill those header blanks."""
    from collections import Counter
    try:
        from hosting.spreadsheets import _xlsx_rows
        out = {}
        for title, it in _xlsx_rows(raw, []):
            rows = list(it)
            for r_i, row in enumerate(rows[:15]):
                heads = {i: str(v).strip().lower() for i, v in enumerate(row) if isinstance(v, str)}
                for i, h in heads.items():
                    what = 'job_type' if h in ('service performed', 'service', 'job type', 'service type') else \
                           'description' if h in ('inspected for', 'inspection for', 'description') and 'description' not in out else None
                    if not what or what in out:
                        continue
                    vals = Counter(str(x[i]).strip() for x in rows[r_i + 1:] if i < len(x) and isinstance(x[i], str) and x[i].strip() and not x[i].startswith('='))
                    if vals:
                        v, n = vals.most_common(1)[0]
                        if n >= 0.6 * sum(vals.values()):
                            out[what] = v
        return out
    except Exception:
        return {}


class CorrectionAccessDenied(ValueError):
    """A correction destination is not accessible; no identifying metadata may escape."""


class _NoChange(Exception):
    pass


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
            self._begin(db, file_hash)
            result = self._store_new(db, filename, raw, result, file_hash)
        if not result.get('duplicate'):
            try:
                self.index_vins(file_hash)
            except Exception:
                pass  # search falls back to indexing later
        return result
    def _store_new(self, db, filename, raw, result, file_hash):
        """Store a checked upload inside the caller's transaction (which holds the lock on ``file_hash``)."""
        if True:
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
            self._begin(db, file_hash)
            original, raw = self._original(db, file_hash)
            rows = report_records(original['filename'], raw)
            from hosting.row_scope import require_complete_detail
            require_complete_detail(self.latest(db, file_hash, original), rows)
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
    def source_reconcile(self, file_hash, snap, day, *, expected_check_hash=None, expected_assignment=None):
        """Compare the stored original (record rows and its own captured export) with a SharePoint snapshot, item by
        item. Saved as a new check revision; the workbook is not changed and no correction is applied."""
        from hosting import reconcile
        from hosting.spreadsheets import report_and_export_records
        if not isinstance(expected_check_hash, str) or not expected_check_hash:
            raise ValueError('The displayed check hash from before source capture is required.')
        with self.connect() as db:
            original, raw = self._original(db, file_hash)
        records, exports = report_and_export_records(original['filename'], raw)
        mapping_state = self.source_mapping(snap.get('site_id'), snap.get('list_id'))
        confirmed = mapping_state['mapping']
        current = self.get(file_hash)
        from hosting.row_scope import require_complete_detail
        require_complete_detail(current, records)
        found = reconcile.compare(day, records, exports, snap, confirmed=confirmed,
                                  report=dict(file_hash=file_hash, checker_version=current.get('checker_version')))
        found['mapping_binding'] = mapping_state
        if mapping_state['legacy_requires_confirmation']:
            found.update(status='unverified', verified=False)
            found['not_verified_because'].append('A legacy list-only column mapping requires confirmation for this exact site and list.')
            for field, detail in (found.get('column_mapping') or {}).items():
                if detail.get('column') and detail.get('status') in ('single', 'confirmed'):
                    detail.update(status='needs_confirmation', evidence=f'Confirm {detail["column"]} as {field} for this exact site and list; the earlier mapping was list-only.')
        def persist(result):
            if self.source_mapping(snap.get('site_id'), snap.get('list_id')) != mapping_state:
                raise ValueError('The source column mapping changed during capture. Compare again using the current mapping.')
            if result.get('check_hash') != expected_check_hash or result.get('checker_version') != CHECKER_VERSION:
                raise ValueError('The report checks changed during source capture. Recheck the current report.')
            if expected_assignment is not None:
                from hosting.review_summary import summarize
                assigned = self.assignment_for(result, summarize(result, with_issues=False))
                keys = ('status', 'site', 'site_id', 'list_id', 'job', 'needs_person')
                if any(assigned.get(k) != expected_assignment.get(k) for k in keys):
                    raise ValueError('The source assignment changed during capture. Read the newly assigned source.')
            result['source_reconciliation'] = found
        return self._update(file_hash, persist, lock_settings=True)
    @staticmethod
    def source_mapping_key(site_id, list_id):
        if not isinstance(site_id, str) or not site_id.strip() or not isinstance(list_id, str) or not list_id.strip():
            raise ValueError('Exact site_id and list_id are required to confirm source columns.')
        if len(site_id) > 512 or len(list_id) > 128:
            raise ValueError('Source identity is too long.')
        return 'column_map_v2|' + hashlib.sha256(json.dumps([site_id, list_id]).encode()).hexdigest()
    def source_mapping(self, site_id, list_id):
        key = self.source_mapping_key(site_id, list_id)
        saved = self.setting(key, None)
        return dict(site_id=site_id, list_id=list_id, mapping=dict((saved or {}).get('mapping') or {}),
                    revision=(saved or {}).get('revision'),
                    legacy_requires_confirmation=bool(saved is None and self.setting(f'column_map|{list_id}', None)))
    def confirm_columns(self, list_id, mapping, by, *, site_id=None, expected_mapping_revision=Ellipsis, file_hash=None, expected_check_hash=None, expected_snapshot_id=None):
        """Confirm only an exact source pair. Old list-only records remain unchanged."""
        key = self.source_mapping_key(site_id, list_id)
        if expected_mapping_revision is Ellipsis:
            raise ValueError('The displayed mapping revision is required, including null for an unconfirmed source.')
        clean = {k: str(v)[:120] for k, v in (mapping or {}).items() if k in ('inspected', 'certified', 'rejected', 'reworked') and v}
        if not clean:
            raise ValueError('Choose the list and its columns.')
        from contextlib import nullcontext
        from copy import copy
        from uuid import uuid4
        with self.connect() as db:
            if file_hash:
                self._begin(db, file_hash)
            elif self.pg is None:
                db.execute('BEGIN IMMEDIATE')
            if self.pg is not None:
                db.execute('LOCK TABLE settings IN SHARE ROW EXCLUSIVE MODE')
            locked = copy(self); locked.connect = lambda: nullcontext(db)
            if file_hash:
                from hosting import registry, reconcile
                from hosting.review_summary import summarize
                current = locked.get(file_hash)
                rec = reconcile.current(current) or {}
                if current.get('checker_version') != CHECKER_VERSION or not rec:
                    raise ValueError('The displayed source comparison changed. Recheck and compare before confirming columns.')
                assigned = locked.assignment_for(current, summarize(current, with_issues=False))
                if not expected_check_hash or current.get('check_hash') != expected_check_hash or not expected_snapshot_id or rec.get('snapshot_id') != expected_snapshot_id:
                    raise ValueError('The displayed source comparison changed. Reload before confirming columns.')
                if any(rec.get(k) != value or assigned.get(k) != value for k, value in (('site_id',site_id),('list_id',list_id))) or assigned.get('status') not in registry.RESOLVED or assigned.get('needs_person'):
                    raise ValueError('The current assigned source differs from this confirmation.')
                if any(column not in ((rec.get('column_mapping') or {}).get(field) or {}).get('candidates', []) for field,column in clean.items()):
                    raise ValueError('Confirm only columns present in the displayed source capture.')
            previous = locked.setting(key, {}) or {}
            if previous.get('revision') != expected_mapping_revision:
                raise ValueError('The source column mapping changed after it was displayed. Reload before confirming.')
            saved = dict(previous.get('mapping') or {}, **clean)
            state = dict(site_id=site_id, list_id=list_id, mapping=saved, revision=uuid4().hex,
                         by=by, at=datetime.now(timezone.utc).isoformat())
            locked.set_setting(key, state)
            log = list(locked.setting('column_map_log', []) or [])
            log.append(dict(state, confirmed=clean))
            locked.set_setting('column_map_log', log[-500:])
        return saved
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
        if review is None or 'decisions' not in review:
            from hosting.review_summary import summarize
            review = summarize(item, with_issues=False)
        cov = review.get('coverage') or {}
        if cov.get('not_listed') or cov.get('truncated'):   # unlisted findings cannot be reviewed, so they cannot be approved past
            from hosting import limits as _limits   # stored count at the bound: the file itself is too big; below it: an older, shorter check
            fix = ('This file goes past the safety limit of stored findings, so a re-check stores no more: split the file or review '
                   'it outside the app before approving.' if (cov.get('findings_listed') or 0) >= _limits.MAX_FINDINGS
                   else 'Re-check the report (it stores every finding now) before approving.')
            out.append(dict(code='findings_incomplete', hard=True, text=f'The checks found {cov.get("findings_total")} things but only '
                            f'{cov.get("findings_listed")} were stored, so {cov.get("not_listed")} cannot be reviewed. ' + fix))
        waiting = (review.get('decisions') or {}).get('deferred') or 0
        if waiting:   # a question sent to the source owner keeps the report on hold until it is answered
            ds = [d for d in (item.get('finding_decisions') or {}).values() if d.get('action') == 'defer']
            d = ds[-1] if ds else {}
            out.append(dict(code='awaiting_source_owner', hard=True, text=f'{waiting} finding{"s are" if waiting != 1 else " is"} waiting for an answer'
                            + (f' from {d["owner"]}' if d.get('owner') else '') + (f': {d.get("reason")}' if d.get('reason') else '.')))
        rec = item.get('source_reconciliation') or {}
        if rec:
            from hosting import reconcile, registry
            current = reconcile.current(item)
            if rec.get('date_contract') is not None:
                from hosting.source_date_contracts import receipt_contract_current
                if not receipt_contract_current(rec['date_contract']):
                    out.append(dict(code='source_date_metadata_stale', hard=True, text='Source-date metadata expired or changed. Recheck SharePoint to refresh timezone and Date-field evidence; do not edit report rows to resolve this gap.'))
            counts = rec.get('count_reconciliation') or {}
            if not current or not reconcile.evidence_verified(rec):
                out.append(dict(code='source_coverage_incomplete' if rec.get('status') == 'incomplete' else 'source_evidence_incomplete', hard=True,
                                text='Source comparison is not fully verified: ' + ' '.join(rec.get('not_verified_because') or (rec.get('coverage') or {}).get('notes') or ['Current complete inspection and rejection evidence is required.'])))
            try:
                if rec.get('mapping_binding') != self.source_mapping(rec.get('site_id'), rec.get('list_id')):
                    out.append(dict(code='source_mapping_changed', hard=True, text='Source columns changed or need exact-source confirmation. Compare again with the current mapping.'))
            except ValueError:
                out.append(dict(code='source_mapping_changed', hard=True, text='Exact source identity is missing from the column mapping.'))
            try:
                assigned = self.assignment_for(item, review)
                if assigned.get('status') not in registry.RESOLVED or assigned.get('needs_person') or any(not assigned.get(k) or assigned.get(k) != rec.get(k) for k in ('site_id','list_id')):
                    out.append(dict(code='source_assignment_changed', hard=True, text='The current source assignment does not match this comparison. Reconcile the assigned source again.'))
            except Exception:
                out.append(dict(code='source_assignment_unavailable', hard=True, text='Current source assignment could not be verified.'))
        return out
    def record_lookup(self, file_hash, status, message, job=None, day=None):
        self.get(file_hash)
        with self.connect() as db:
            self._begin(db, file_hash)
            original, _ = self._original(db, file_hash)
            result = dict(self.latest(db, file_hash, original))
            if (result.get('sharepoint') or {}).get('status') in ('match', 'differences'):
                result['previous_sharepoint_comparison'] = result['sharepoint']
            if result.get('source_reconciliation'):
                result['source_reconciliation'] = dict(result['source_reconciliation'], stale=True, stale_reason='The latest source capture did not complete: ' + str(message))
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
            self._begin(db, file_hash)
            original, _ = self._original(db, file_hash)
            result = dict(self.latest(db, file_hash, original))
            if ai_review.get('status') != 'done' and (result.get('ai_review') or {}).get('status') == 'done':
                return result  # keep the last real review when a retry fails
            result.pop('check_hash', None); result.pop('checked_at', None)
            from hosting.review_summary import check_run
            result['ai_review'] = dict(ai_review, run=check_run(result))
            return self.save_check(db, result)
    def ai_fix_inputs(self, file_hash, include_dismissed=False):
        from hosting.review_summary import summarize
        item = self.get(file_hash)
        _, raw = self.original_bytes(file_hash)
        out = self.review_summary(item, use_verdicts=not include_dismissed)
        decided = item.get('review_decisions') or {}   # what a person already decided is never re-decided by Claude
        return item, raw, [i for i in out['issues'] if i['severity'] != 'info' and i['key'] not in decided and not i.get('decision') and not i.get('previous_decision')]
    def _begin(self, db, file_hash):
        """Serialize writers of one report. SQLite: BEGIN IMMEDIATE locks the file. Postgres: pg_tables skips BEGIN IMMEDIATE,
        so take a transaction-scoped advisory lock on this report's hash; it is released at commit or rollback."""
        if self.pg is not None:
            db.execute('SELECT pg_advisory_xact_lock(hashtext(?))', ('check:' + file_hash,))
        else:
            db.execute('BEGIN IMMEDIATE')
    def _update(self, file_hash, change, *, lock_settings=False):
        """Read the latest check of this report, let ``change`` modify it and save it, all under the report's lock.
        ``change`` sees the state it is about to overwrite, so every check it makes is made against what is saved.
        If the change alters any decision on an approved (unsent) version, the approval is withdrawn in the same save."""
        from hosting.review_summary import decisions_basis
        with self.connect() as db:
            self._begin(db, file_hash)
            if lock_settings and self.pg is not None:
                db.execute('LOCK TABLE settings IN SHARE ROW EXCLUSIVE MODE')
            original, _ = self._original(db, file_hash)
            result = dict(self.latest(db, file_hash, original))
            before = decisions_basis(result)
            change(result)
            if result.get('approval') and decisions_basis(result) != before and not any(x.get('sent') for x in result.get('sent') or []):
                was = result.pop('approval')
                result['approval_withdrawn'] = dict(at=datetime.now(timezone.utc).isoformat(), by=None, version=was.get('version'),
                                                    reason='The decisions on this report changed after it was approved, so the approval no longer applies. Approve it again.')
            result.pop('check_hash', None); result.pop('checked_at', None)
            return self.save_check(db, result)
    @staticmethod
    def _approval_state(result):
        from hosting.review_summary import approved_state_fingerprint
        return approved_state_fingerprint(result)
    def approve(self, file_hash, approver, note='', override=False, withdraw=False, *, expected_check_hash=None, expected_run=None):
        """Validate and approve the exact displayed revision under the report writer lock."""
        from hosting.review_summary import summarize, same_run
        approver = ' '.join(str(approver or '').split())[:80]
        def change(r):
            if any(x.get('sent') for x in r.get('sent') or []) or r.get('send_pending') or r.get('send_in_progress'):
                raise ValueError('This report was sent or is being sent; its approval stays on record.')
            if withdraw:
                was = r.pop('approval', None)
                r['approval_withdrawn'] = dict(at=datetime.now(timezone.utc).isoformat(), by=approver or None, version=(was or {}).get('version'))
                return
            if r.get('checker_version') != CHECKER_VERSION:
                raise ValueError('Re-check this report with the current checker before approving.')
            prior = r.get('approval') or {}
            if expected_check_hash and prior.get('reviewed_check_hash') == expected_check_hash and same_run(prior.get('run'), r) and prior.get('approved_state_hash') == self._approval_state(r):
                # Some holds derive from shared assignment settings, outside the report JSON.
                replay_holds = self.holds(r, summarize(r, with_issues=False))
                if any(h['hard'] or h['code'] not in (prior.get('holds_overridden') or []) for h in replay_holds):
                    raise ValueError('The approval holds changed. Reload and review before approving.')
                raise _NoChange()
            if not expected_check_hash or expected_check_hash != r.get('check_hash') or not same_run(expected_run, r):
                raise ValueError('This report changed after it was displayed. Reload and review it before approving.')
            if not approver:
                raise ValueError('Type your name to approve the report.')
            rv = self.review_summary(r, with_issues=False)
            must = (rv.get('counts') or {}).get('high', 0)
            holds = self.holds(r, rv)
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
            if must and override and not ' '.join(str(note or '').split()):
                raise ValueError('Say why you are approving with must-fix issues open; the reason is kept with the approval.')
            newer = (r.get('fixes_applied') or [])
            if newer and not override:
                raise ValueError(f'A corrected copy of this report exists ("{newer[-1]["fixed_filename"]}"). Approve that version instead, or tick "Approve anyway".')
            record = dict(by=approver, note=' '.join(str(note or '').split())[:500], at=datetime.now(timezone.utc).isoformat(),
                          status=rv['status'], open_must_fix=must, open_to_check=(rv['counts'].get('medium', 0) + rv['counts'].get('low', 0)),
                          overridden=bool((must or soft) and override), holds_overridden=[x['code'] for x in soft],
                          filename=r['filename'], version=r.get('version'), run=rv.get('run'))
            from hosting.review_summary import decisions_basis
            record.update(decisions_basis=decisions_basis(r), decisions_rev=int(r.get('finding_decisions_rev') or 0), assignment=self._assignment_key(r, rv))
            record['reviewed_check_hash'] = expected_check_hash
            r['approval'] = record
            if (r.get('review_request') or {}).get('status') in ('waiting', 'sent_back'):
                rr = dict(r['review_request'])
                rr.update(status='approved', reviewer=approver, answered_at=record['at'])
                rr['history'] = (rr.get('history') or []) + [dict(action='approve', by=approver, note=record['note'], at=record['at'])]
                r['review_request'] = rr
            record['approved_state_hash'] = self._approval_state(r)
        try:
            return self._update(file_hash, change)
        except _NoChange:
            return self.get(file_hash)
    def _assignment_key(self, r, rv):
        try:
            a = self.assignment_for(r, rv)
            return dict(status=a.get('status'), label=a.get('label'), job=a.get('job'), client=a.get('client'))
        except Exception:
            return None
    SEND_HOLD_SECONDS = 300
    def verify_for_delivery(self, file_hash, mode='send', again=False):
        """Immediately before a report goes to the client, under the report's lock, and reserve it for this send.
        It must carry an approval made on this check run and these exact decisions; the checker must be current; no
        hard hold, no more must-fix issues than were approved, no corrected copy made after the approval, the same
        client and job; and no other send in progress. Returns (the saved state that was checked, reservation token).
        Pair with record_send(token=...) or release_send(token) whatever happens to the message."""
        raise ValueError('Sending and draft creation are paused pending delivery safety checks.')
        import uuid
        from hosting.review_summary import summarize, approval_current
        out = {}
        def check(r):
            if not r.get('approval'):
                raise ValueError('Approve the report before sending it to the client.')
            if r.get('checker_version') != CHECKER_VERSION or not approval_current(r):
                raise ValueError('The checks or decisions changed after this report was approved (re-checked, compared with SharePoint again, a '
                                 'decision changed, or approved before approvals recorded their decisions). Approve it again before sending.')
            busy = r.get('send_in_progress') or {}
            if busy and (datetime.now(timezone.utc) - datetime.fromisoformat(busy['at'])).total_seconds() < self.SEND_HOLD_SECONDS:
                raise ValueError('This report is already being sent. Wait a moment and check its history before sending again.')
            done = [x for x in r.get('sent') or [] if x.get('sent')]
            if done and mode == 'send' and not again:
                raise ValueError(f'This report was already sent to the client on {done[-1].get("at", "")[:10]}. Tick "Send again" (again: true) to send it anyway.')
            appr = r['approval']
            later = [f for f in r.get('fixes_applied') or [] if str(f.get('applied_at') or '') > str(appr.get('at') or '')]
            if later:
                raise ValueError(f'A corrected copy ("{later[-1].get("fixed_filename")}") was made after this version was approved. Send that version instead.')
            rv = self.review_summary(r, with_issues=False)
            hard = [x for x in self.holds(r, rv) if x['hard']]
            if hard:
                raise ValueError('Not sent: ' + hard[0]['text'])
            must = (rv.get('counts') or {}).get('high', 0)
            if must > int(appr.get('open_must_fix') or 0):
                raise ValueError(f'{must} must-fix issue{"s are" if must != 1 else " is"} open now, more than when it was approved. Approve it again before sending.')
            if appr.get('assignment') and self._assignment_key(r, rv) != appr['assignment']:
                raise ValueError('The client or job this report belongs to changed after it was approved. Approve it again before sending.')
            out['token'] = uuid.uuid4().hex
            r['send_in_progress'] = dict(token=out['token'], at=datetime.now(timezone.utc).isoformat())
            out['item'] = dict(r)
        self._update(file_hash, check)
        return out['item'], out['token']
    def release_send(self, file_hash, token):
        """The message did not go (an error): end the reservation without recording a send."""
        def change(r):
            if (r.get('send_in_progress') or {}).get('token') == token:
                was = r.pop('send_in_progress')
                # the failed attempt is kept (it also keeps this revision distinct from the one before the reservation)
                r['send_attempts_failed'] = (r.get('send_attempts_failed') or [])[-19:] + [dict(started=was.get('at'), ended=datetime.now(timezone.utc).isoformat())]
            else:
                raise _NoChange()
        try:
            self._update(file_hash, change)
        except _NoChange:
            pass
    @staticmethod
    def _check_frozen(r):
        """Decisions never change on a version that was sent, or that is approved (withdraw the approval first)."""
        from hosting import review_decisions
        if any(x.get('sent') for x in r.get('sent') or []):
            raise review_decisions.DecisionConflict('This version was sent to the client; its decisions stay as they were.')
        if r.get('approval'):
            raise review_decisions.DecisionConflict('This version is approved; withdraw the approval before changing decisions.')
        if r.get('send_in_progress'):
            raise review_decisions.DecisionConflict('This version is being sent right now; its decisions stay as they are.')
    def _check_shown(self, r, keys, seen, run):
        """Under the report's lock: the request was made on this check run, and each finding it names still reads as it
        did on the reviewer's screen. Keys that are not findings (suggestion IDs) are bound by the run alone."""
        from hosting import review_decisions
        from hosting.review_summary import summarize, same_run, check_run
        if run is not None and not same_run(run, r):
            raise review_decisions.DecisionConflict('This report was checked again or SharePoint was read again after you opened it, so '
                                                    'what you saw may be out of date. Nothing was saved. Reload and decide again.',
                                                    current_run=check_run(r))
        if seen is None:
            return
        rv = self.review_summary(r)
        current = {x['key']: x for x in rv['issues'] + rv['dismissed']}
        changed = [k for k in keys if k in current and str((seen or {}).get(k)) != str(current[k].get('fp'))]
        if changed:
            raise review_decisions.DecisionConflict(f'{len(changed)} of these findings changed after you looked at them (for example a new '
                                                    'SharePoint value). Nothing was saved. Reload to see what they say now, then decide again.',
                                                    changed=[dict(id=k, now=current[k].get('text')) for k in changed])
    def mark_fine(self, file_hash, add, seen=None, run=None):
        """Older "fine as is" records, checked against what the reviewer saw under the report's lock."""
        raise ValueError('Use scoped finding decisions with current fingerprints.')
        def change(r):
            self._check_frozen(r)
            self._check_shown(r, [a.get('key') for a in add if a.get('key')], seen, run)
            r['accepted_issues'] = (r.get('accepted_issues') or []) + add
        return self._update(file_hash, change)
    def decide_findings(self, file_hash, *, ids=(), action, reason='', by='', owner='', evidence=(), request_id='', expected_rev=None, undo_of=None, expected_fps=None, expected_run=None, seen=None, run=None):
        """A reviewer's reject / defer / confirm / undo on findings of this exact version, in one transaction: all of the
        batch is saved, or none of it. Returns (item, log entry)."""
        from hosting import review_decisions
        from hosting.review_summary import summarize, check_run
        expected_fps = expected_fps if expected_fps is not None else seen
        expected_run = expected_run if expected_run is not None else run
        out = {}
        def change(r):
            # Replay the original successful click even when the report was subsequently locked.
            replay = review_decisions._clean(request_id, 80) in (r.get('finding_requests') or {})
            if not replay and (r.get('approval') or r.get('send_pending') or r.get('send_in_progress') or any(x.get('sent') for x in r.get('sent') or [])):
                raise review_decisions.DecisionConflict('This version is approved, sent, or being sent; its decisions cannot change.')
            if not replay and (not isinstance(expected_run, dict) or expected_run != check_run(r)):
                raise review_decisions.DecisionConflict('The check run changed. Reload and decide again.')
            rv = self.review_summary(r)
            current = {x['key']: x for x in rv['issues'] + rv['dismissed']}
            out['entry'] = review_decisions.decide(r, current, ids=ids, action=action, reason=reason, by=by, owner=owner, evidence=evidence,
                                                   request_id=request_id, expected_rev=expected_rev, undo_of=undo_of, expected_fps=expected_fps, expected_run=expected_run)
            if out['entry'].get('replayed'):
                raise _NoChange()
        try:
            new = self._update(file_hash, change)
        except _NoChange:
            new = self.get(file_hash)
        return new, out['entry']
    DECISIONS = ('keep', 'reject', 'note', 'reopen')
    def record_decision(self, file_hash, key, action, note, by):
        return self.record_decisions(file_hash, [key], action, note, by)
    def record_decisions(self, file_hash, keys, action, note, by, seen=None, run=None):
        """A reviewer's decision on issues or suggestions, kept with this version and logged (one save for a group). A
        decision never clears a blocker by itself: keeping a blank that has no source value keeps the report on hold."""
        raise ValueError('Use the finding decision controls with the displayed revision and fingerprints.')
        keys, note, by = [k.strip()[:300] for k in keys if k and k.strip()], ' '.join(note.split())[:1000], ' '.join(by.split())[:80]
        if not keys:
            raise ValueError('Which issue or suggestion?')
        if action not in self.DECISIONS:
            raise ValueError('Unknown decision.')
        if action == 'note' and not note:
            raise ValueError('Type the note first.')
        at = datetime.now(timezone.utc).isoformat()
        def change(r):
            self._check_frozen(r)
            self._check_shown(r, keys, seen, run)
            d = dict(r.get('review_decisions') or {})
            for key in keys:
                if action == 'reopen':
                    d.pop(key, None)
                else:
                    prev = d.get(key) or {}
                    d[key] = dict(action=prev.get('action') if action == 'note' and prev.get('action') in ('keep', 'reject') else action,
                                  note=note or prev.get('note') or '', by=by or None, at=at)
            r['review_decisions'] = d
            r['review_decision_log'] = ((r.get('review_decision_log') or []) + [dict(key=k, action=action, note=note, by=by or None, at=at) for k in keys])[-500:]
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
    def record_send(self, file_hash, record, token=None):
        raise ValueError('Sending and draft creation are paused pending delivery safety checks.')
        """Record a delivery and end its reservation. The message has already left, so it is always recorded; if the
        approval stopped being current while it was on its way, the record says so."""
        from hosting.review_summary import approval_current
        def change(r):
            rec = dict(record)
            if not approval_current(r):
                rec['approval_changed_during_send'] = True
            if token and (r.get('send_in_progress') or {}).get('token') == token:
                r.pop('send_in_progress', None)
            r['sent'] = (r.get('sent') or []) + [rec]
        return self._update(file_hash, change)
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
            rv = self.review_summary(result, with_issues=False)
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
    def review_summary(self, item, **kwargs):
        """Read-only classification using current assignment and source mapping, never saved success flags."""
        from hosting.review_summary import summarize
        base = summarize(item, **kwargs)
        rec = item.get('source_reconciliation') or {}
        if not rec.get('identifier_pattern_evidence'):
            return base
        try:
            from hosting import registry
            assigned = self.assignment_for(item, base)
            valid = (assigned.get('status') in registry.RESOLVED and not assigned.get('needs_person')
                     and all(assigned.get(k) and assigned.get(k) == rec.get(k) for k in ('site_id', 'list_id'))
                     and rec.get('mapping_binding') == self.source_mapping(rec.get('site_id'), rec.get('list_id'))
                     and not any(str(h.get('code', '')).startswith('source_') and any(word in h.get('code', '') for word in ('mapping', 'identity', 'coverage', 'stale', 'assignment')) for h in self.holds(item, base)))
            context = dict(valid=valid, site_id=assigned.get('site_id'), list_id=assigned.get('list_id'))
        except Exception:
            context = None
        return summarize(item, source_context=context, **kwargs)

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
            self._begin(db, file_hash)
            original, _ = self._original(db, file_hash)
            result = dict(self.latest(db, file_hash, original))
            result.pop('check_hash', None); result.pop('checked_at', None)
            from hosting.review_summary import check_run
            result['ai_verify'] = dict(record, run=check_run(result))
            return self.save_check(db, result)
    @staticmethod
    def _check_proposal_state(result, expected_check_hash):
        if result.get('checker_version') != CHECKER_VERSION:
            raise ValueError('Re-check this report before preparing suggestions.')
        if not expected_check_hash or expected_check_hash != result.get('check_hash'):
            raise ValueError('This report changed after review. Reload before preparing suggestions.')
        if result.get('approval') or result.get('send_pending') or result.get('send_in_progress') or result.get('fixes_applied') or any(x.get('sent') for x in result.get('sent') or []):
            if any(x.get('sent') for x in result.get('sent') or []):
                raise ValueError('This version was sent and is read-only. Open its working copy if one exists.')
            if result.get('send_pending') or result.get('send_in_progress'):
                raise ValueError('This version is being sent and is read-only. Wait for sending to finish.')
            if result.get('approval'):
                raise ValueError('This version is approved and is read-only. An authorized person must resolve its approval status before editing.')
            raise ValueError('This version is superseded. Open the working copy before preparing suggestions.')
    def proposal_inputs(self, file_hash, expected_check_hash):
        from hosting.review_summary import summarize
        with self.connect() as db:
            self._begin(db, file_hash)
            original, raw = self._original(db, file_hash)
            item = dict(self.latest(db, file_hash, original))
            self._require_working_parent(db, file_hash)
            self._check_proposal_state(item, expected_check_hash)
            review = self.review_summary(item)
            decided = item.get('review_decisions') or {}
            issues = [i for i in review['issues'] if i['severity'] != 'info' and i['key'] not in decided and not i.get('decision') and not i.get('previous_decision')]
            return item, raw, issues
    def record_ai_fixes(self, file_hash, record, *, expected_check_hash=None):
        with self.connect() as db:
            self._begin(db, file_hash)
            original, _ = self._original(db, file_hash)
            result = dict(self.latest(db, file_hash, original))
            self._require_working_parent(db, file_hash)
            self._check_proposal_state(result, expected_check_hash)
            result.pop('check_hash', None); result.pop('checked_at', None)
            from hosting.review_summary import check_run
            result['ai_fixes'] = record = dict(record, run=check_run(result))
            self.save_check(db, result)
        return record
    def ai_fixes(self, file_hash, opener=None, *, expected_check_hash=None):
        """Ask Claude to write cell changes for the open issues; keep only changes that fit the file exactly."""
        item, raw, issues = self.proposal_inputs(file_hash, expected_check_hash)
        record = make_ai_fixes(item, raw, issues, opener)
        return self.record_ai_fixes(file_hash, record, expected_check_hash=expected_check_hash) if record['status'] == 'done' else record
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
            self._begin(db, file_hash)
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
        from hosting.review_summary import summarize
        try:
            assignment = self.assignment_for(item, summarize(item, with_issues=False))
        except Exception:
            assignment = None
        ctx = suggest.context(job, day, self.setting(sharepoint_jobs.KEY), assignment=assignment, known=_known_details(item['filename'], raw))
        props = proposals(item, item['filename'], raw, context=ctx)
        taken = {(p['sheet'], p['cell']) for p in props}
        ai = item.get('ai_fixes') or {}
        from hosting.review_summary import same_run
        if ai.get('run') is not None and not same_run(ai.get('run'), item):
            ai = dict(ai, fixes=[], status='stale')   # drafted on an earlier check run: never offered as a fix
        elif ai.get('run') is None and ai.get('fixes'):
            ai = dict(ai, fixes=[], status='stale')
        for f in ai.get('fixes') or []:
            if (f['sheet'], f['cell']) in taken:
                continue
            props.append(dict(id=f['id'], sheet=f['sheet'], cell=f['cell'], before=f['before'], after=f['after'], kind='auto',
                              reason=f['reason'], source='AI fix', confidence=f.get('confidence') or 'medium', sure=False))
        self._mark_recommended_proposals(item, props, raw)
        return dict(file_hash=file_hash, filename=item['filename'], proposals=props, applied=item.get('fixes_applied') or [],
                    ai=dict(status=ai.get('status') or 'not_run', made_at=ai.get('made_at'), count=len(ai.get('fixes') or []),
                            skipped=ai.get('skipped') or [], dropped=ai.get('dropped') or 0, message=ai.get('message')))
    def _mark_recommended_proposals(self, item, props, raw):
        """Read-only eligibility for preview-all, never permission to apply.
        The normal apply endpoint repeats guards under lock after explicit review.
        Fail closed on holds, ambiguous cells, incomplete change sets and AI guesses.
        """
        from hosting.fixes import FixError
        from collections import Counter
        cells = Counter((p.get('sheet'), str(p.get('cell') or '').upper()) for p in props)
        holds = self.holds(item)
        global_reason = ('Review current holds before selecting recommendations.' if holds else
                         'Re-check this version first.' if item.get('checker_version') != CHECKER_VERSION else
                         'This version is locked or has a newer corrected version.' if
                         item.get('approval') or item.get('send_pending') or item.get('fixes_applied') or
                         any(x.get('sent') for x in item.get('sent') or []) else '')
        candidates = []
        for p in props:
            reason = global_reason
            if not reason and (p.get('sure') is not True or str(p.get('source') or '').startswith(('AI', 'Suggested', 'Known detail'))):
                reason = 'This suggestion needs an individual review.'
            if not reason and p.get('kind') == 'text' and p.get('before') == p.get('after'):
                reason = 'This cell is already unchanged.'
            if not reason and (p.get('change_set') or cells[(p.get('sheet'), str(p.get('cell') or '').upper())] != 1):
                reason = 'Review this coordinated or ambiguous change individually.'
            if not reason:
                for d in (item.get('finding_decisions') or {}).values():
                    if d.get('action') in ('reject', 'defer') and (not d.get('sheet') or d['sheet'] == p.get('sheet')) and (not d.get('cell') or str(d['cell']).upper() == str(p.get('cell') or '').upper()):
                        reason = 'Reopen the held finding before changing this cell.'
                        break
            p['recommendation_eligible'] = not bool(reason)
            p['recommendation_reason'] = reason or 'Current supported proposal; exact values and source are checked again when applied.'
            p['recommendation_check_hash'] = item.get('check_hash')
            if not reason:
                candidates.append(p)
        if candidates:
            try:
                self._validate_correction_batch(candidates)
                self._check_changes(item, candidates, False, raw=raw)
            except (ValueError, FixError):
                # The common all-valid case performs source and summary work once.
                # Isolate rejected members only when the batch check fails.
                for p in candidates:
                    try:
                        self._check_changes(item, [p], False, raw=raw)
                    except (ValueError, FixError) as exc:
                        p['recommendation_eligible'] = False
                        p['recommendation_reason'] = str(exc)

    def sheet_grid(self, file_hash, sheet=None):
        """The sheet as shown, with its clickable links and the pictures anchored on it, each labelled with its record,
        field, purpose and business date (a picture whose caption marks it as an earlier reference says so)."""
        from hosting.fixes import sheet_grid
        from hosting import photos
        item = self.get(file_hash)
        _, raw = self.original_bytes(file_hash)
        g = sheet_grid(item['filename'], raw, sheet)
        if not item['filename'].lower().endswith('.xlsx'):
            return g
        name = g['sheet']
        links = {x['cell']: dict(url=x['url'], safe=x['safe'], kind='hyperlink') for x in photos.native_links(raw) if x['sheet'] == name}
        for r_i, row in enumerate(g['rows']):   # web addresses typed into cells are links too
            for c_i, v in enumerate(row):
                ref = f'{photos._col(c_i)}{r_i + 1}'
                if ref not in links and isinstance(v, str) and re.match(r'(?i)^(https?://|mailto:|javascript:|data:|file:)', v.strip()):
                    links[ref] = dict(url=v.strip()[:2000], safe=photos.safe_url(v), kind='text')
        recs = {(r.get('sheet'), r.get('row')): r for r in item.get('records') or []}
        known = {(x.get('sheet'), x.get('cell')): x.get('item') for x in (item.get('photos') or {}).get('images') or []}
        by_item = {str((r.get('source') or {}).get('item')): r for r in item.get('records') or [] if (r.get('source') or {}).get('item')}
        day = item.get('report_day')
        header_row = next((i for i, row in enumerate(g['rows'][:15]) if sum(1 for v in row if str(v).strip()) >= 3), None)
        pics = []
        for p in photos.images(raw):
            if p['sheet'] != name:
                continue
            row = p.get('row') or 0
            col = sum((ord(ch) - 64) * 26 ** i for i, ch in enumerate(reversed(re.sub(r'\d', '', p.get('cell') or 'A')))) - 1
            line = g['rows'][row - 1] if 0 < row <= len(g['rows']) else []
            text = ' '.join(str(v) for v in line if str(v).strip())
            m = re.search(r'\b(20\d\d)-(\d\d)-(\d\d)\b', text)
            pic_day = m.group(0) if m else None
            p = dict(p, item=p.get('item') or known.get((name, p.get('cell'))))
            rec = recs.get((name, row)) or by_item.get(str(p.get('item'))) or {}
            field = g['rows'][header_row][col] if header_row is not None and col < len(g['rows'][header_row]) and header_row < row - 1 else None
            historical = bool(re.search(r'(?i)reference|historical|earlier|previous', text)) or bool(pic_day and day and pic_day != day)
            pics.append(dict(cell=p.get('cell'), row=row, media=p['media'], sha256=p['sha256'], bytes=p['bytes'], missing=p['missing'],
                             item=p.get('item') or (rec.get('source') or {}).get('item'), field=field or None,
                             purpose='defect' if re.search(r'(?i)defect|nok|broken', f'{name} {field or ""} {text}') else 'label' if re.search(r'(?i)label', f'{field or ""} {text}') else 'photo',
                             business_date=rec.get('day') or pic_day or day, historical=historical,
                             note=(f'Reference only ({pic_day or "earlier"}): it cannot show the outcome on {day}.' if historical else None)))
        g['links'] = links
        g['images'] = pics
        g['image_summary'] = dict(placements=len(pics), unique=len({p['sha256'] for p in pics if p['sha256']}), missing=sum(1 for p in pics if p['missing']))
        return g
    def row_cells(self, file_hash, sheet, row):
        from hosting.fixes import row_cells
        item = self.get(file_hash)
        _, raw = self.original_bytes(file_hash)
        return row_cells(item['filename'], raw, sheet, row)
    @staticmethod
    def _validate_correction_batch(approved):
        """Atomic membership is batch metadata, not source provenance."""
        addresses = [(c.get('sheet'), str(c.get('cell') or '').upper()) for c in approved]
        if len(set(addresses)) != len(addresses):
            raise ValueError('A correction batch cannot contain the same cell twice.')
        sets = {}
        for c in approved:
            name = c.get('change_set')
            if name is not None and (not isinstance(name, str) or not name.strip() or len(name) > 300):
                raise ValueError('Each atomic change set requires a nonempty text identifier of at most 300 characters.')
            if name is not None:
                size = c.get('change_set_size')
                if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
                    raise ValueError('Each atomic change set requires an explicit positive integer size. Nothing was applied.')
                sets.setdefault(c['change_set'], []).append(size)
        for name, sizes in sets.items():
            if any(size != len(sizes) for size in sizes):
                raise ValueError(f'Change set {name} is incomplete ({len(sizes)} of its cells were sent). Nothing was applied.')

    def apply_fixes(self, file_hash, approved, *, auto=False, automatic=False, expected_check_hash=None, recommended_only=False, authorize_destination=None):
        """Write a corrected copy with the approved changes as a new upload; the original is untouched.
        Source freshness, change sets and (for the automatic pass, ``auto=True``) a person's decisions are checked under
        the report's lock, and the new version is created in the same transaction: a SharePoint read or decision that
        lands in between refuses the whole set and no corrected version is made."""
        from hosting.fixes import apply_fixes, fixed_filename, FixError
        if not isinstance(approved, list) or not approved or not all(isinstance(c, dict) for c in approved):
            raise ValueError('Send at least one approved change.')
        self._validate_correction_batch(approved)
        # A typed value is the person's proposal, never a claim about source evidence.
        # Copy first so the immutable displayed request and caller-owned objects stay unchanged.
        approved = [dict(c) for c in approved]
        for c in approved:
            if ('selected_finding_id' in c or 'selected_finding_fp' in c) and any(c.get(k) is not None for k in ('snapshot_id', 'item', 'field', 'change_set', 'change_set_size')):
                raise FixError('A selected mechanical correction cannot assert source or coordinated-change authority. Nothing was applied.')
            label = str(c.get('source') or '').strip()
            if label.casefold().startswith('reverted'):
                raise ValueError('Revert workbook changes are disabled pending safety review.')
            if label.casefold() in ('your edit', 'manual edit'):
                c['source'] = 'Your edit'
                for key in ('snapshot_id', 'item', 'field'):
                    c.pop(key, None)
            elif not c.get('snapshot_id') and not label.startswith('SharePoint'):
                # Partial caller metadata cannot assert source authority. Real source
                # proposals retain their complete identity and are proved below.
                for key in ('item', 'field'):
                    c.pop(key, None)
        if auto or automatic:
            raise ValueError('Automatic workbook changes are disabled pending safety review.')
        item = self.get(file_hash)
        _, raw = self.original_bytes(file_hash)
        self._check_changes(item, approved, auto, raw=raw)  # repeated under the lock
        fixed = apply_fixes(item['filename'], raw, approved)  # the original file never changes, so this can be built first
        new_name = fixed_filename(item['filename'])
        new_hash = hashlib.sha256(fixed).hexdigest()
        new_check = inspect_spreadsheet(new_name, fixed)
        record = dict(fixed_file_hash=new_hash, fixed_filename=new_name, applied_at=datetime.now(timezone.utc).isoformat(),
                      changes=[{k: c.get(k) for k in ('sheet', 'cell', 'before', 'after', 'reason', 'source', 'snapshot_id', 'item', 'field', 'change_set', 'change_set_size') if c.get(k) is not None or k in ('before', 'after')} for c in approved])
        with self.connect() as db:
            if self.pg is not None:   # both reports, always in the same order: two corrections in opposite directions cannot deadlock
                for hh in sorted({file_hash, new_hash}):
                    self._begin(db, hh)
            else:
                self._begin(db, file_hash)
            if self.pg is not None:
                db.execute('LOCK TABLE settings IN SHARE ROW EXCLUSIVE MODE')
            original, _ = self._original(db, file_hash)
            result = dict(self.latest(db, file_hash, original))
            if any(x.get('sent') for x in result.get('sent') or []):
                raise ValueError('This version was sent and is read-only. No correction was saved.')
            if result.get('send_pending') or result.get('send_in_progress'):
                raise ValueError('This version is being sent and is read-only. No correction was saved.')
            if result.get('approval'):
                raise ValueError('This version is approved and is read-only. An authorized person must resolve its approval status before editing.')
            if not expected_check_hash or expected_check_hash != result.get('check_hash'):
                raise ValueError('This report changed after the corrections were reviewed. Nothing was applied.')
            if automatic:
                from hosting.autofix import eligible
                why = eligible(result)
                if why:
                    raise ValueError(why)
                if not expected_check_hash:
                    raise ValueError('Automatic corrections require the reviewed check revision.')
                if self.holds(result):
                    raise ValueError('This report has unresolved source or review holds. Nothing was applied.')
                if result.get('finding_decisions') or result.get('review_decisions') or result.get('accepted_issues'):
                    raise ValueError('A person has reviewed this report; automatic corrections are disabled.')
            # Never silently rewrite rejected or deferred data, even through a manual batch.
            for decision in (result.get('finding_decisions') or {}).values():
                if decision.get('action') not in ('reject', 'defer'):
                    continue
                for c in approved:
                    if (not decision.get('sheet') or decision.get('sheet') == c.get('sheet')) and (not decision.get('cell') or str(decision['cell']).upper() == str(c.get('cell') or '').upper()):
                        raise ValueError('Reopen the rejected or deferred finding before correcting its data.')
            self._validate_correction_batch(approved)
            if recommended_only:
                # Rebuild only deterministic proposals against this locked version. A bulk
                # preview is not authority to bypass a newly appearing hold or human decision.
                from hosting.fixes import proposals
                current = proposals(result, result['filename'], raw)
                self._mark_recommended_proposals(result, current, raw)
                fields = ('id', 'sheet', 'cell', 'before', 'after', 'kind', 'source', 'snapshot_id', 'item', 'field')
                eligible = [p for p in current if p.get('recommendation_eligible')]
                if any(not any(all(c.get(k) == p.get(k) for k in fields) for p in eligible) for c in approved):
                    raise ValueError('Recommendations changed or are now held. Reopen the current preview. Nothing was applied.')

            self._check_changes(result, approved, auto, raw=raw)  # against the state being saved
            if authorize_destination is not None:
                stored = db.execute('SELECT result_json FROM spreadsheets WHERE file_hash=?', (new_hash,)).fetchone()
                candidate = self.latest(db, new_hash, json.loads(stored['result_json'])) if stored else dict(new_check, file_hash=new_hash, filename=new_name)
                if not authorize_destination(candidate):
                    # Check before storing bytes, updating either version, or committing.
                    # The API uses the current destination's own assignment semantics.
                    raise CorrectionAccessDenied('Report not found.')
            # Existing exact-output reuse below is read-only. A new write must still
            # originate from the current leaf at transaction time, never a stale tab.
            existing = db.execute('SELECT result_json FROM spreadsheets WHERE file_hash=?', (new_hash,)).fetchone()
            if not existing:
                self._require_working_parent(db, file_hash)
            new_item = self._store_new(db, new_name, fixed, new_check, new_hash)
            if new_item.get('duplicate'):
                # the same changes give byte-identical output: that version already exists, so it is opened, never
                # recorded a second time (this is how one report once showed many "fixed" entries for a single file)
                if self._shared_correction_ancestry(db, file_hash, new_hash):
                    # This is a read-only outcome, not a new historical correction. The
                    # requested transformation is proved by its exact output hash above.
                    return dict(new_item, already_applied=True, save_outcome=dict(
                        type='existing_version', requested_from=file_hash,
                        expected_check_hash=expected_check_hash, changes=record['changes']))
                raise ValueError('The corrected content matches an unrelated upload. No version was changed; review the requested changes manually.')
            revision = int(result.get('revision') or 1) + 1
            record = dict(record, fixed_filename=new_item['filename'], revision=revision)
            result.pop('check_hash', None); result.pop('checked_at', None)
            result['fixes_applied'] = (result.get('fixes_applied') or []) + [record]
            self.save_check(db, result)
            fixed_original, _ = self._original(db, new_item['file_hash'])
            fixed_result = dict(self.latest(db, new_item['file_hash'], fixed_original))
            fixed_result.pop('check_hash', None); fixed_result.pop('checked_at', None)
            fixed_result['fixed_from'] = dict(file_hash=file_hash, filename=result['filename'], version=result.get('version'), revision=int(result.get('revision') or 1),
                                              changes=record['changes'], applied_at=record['applied_at'])
            # one lineage per report: v1 is the upload, each saved set of changes is the next revision with its own hash and checks
            fixed_result['revision'] = revision
            fixed_result['root_hash'] = result.get('root_hash') or file_hash
            if result.get('source_document'): fixed_result['source_document'] = result['source_document']   # still came from that PDF / Word file
            # Changed workbook bytes cannot inherit source verification, but must not
            # silently lose an outstanding source requirement either. Keep the prior
            # receipt explicitly historical, bound to the old bytes, until refreshed.
            if result.get('source_reconciliation'):
                fixed_result['source_reconciliation'] = dict(result['source_reconciliation'],
                    stale=True, verified=False, carried_from=file_hash,
                    stale_reason='Workbook corrections changed this version. Compare the assigned source again before verification.')
            # Unbound historical acceptances never follow changed workbook bytes.
            for k in ('auto_formatting', 'ai_fixed', 'review_decisions'):   # what was already fixed or decided stays on the report as it moves on
                if result.get(k) and k not in fixed_result: fixed_result[k] = result[k]
            # a person's finding decisions go along, marked as made on the earlier version; each one is only applied to a
            # finding that still says exactly the same thing (same cell, message and values), never to changed data
            if result.get('finding_decisions') and 'finding_decisions' not in fixed_result:
                fixed_result['finding_decisions'] = {k: dict(d, carried_from=d.get('carried_from') or file_hash) for k, d in result['finding_decisions'].items()
                                                     if d.get('action') in ('reject', 'defer')}
            new_item = self.save_check(db, fixed_result)
        try:
            self.index_vins(new_item['file_hash'])
        except Exception:
            pass  # search falls back to indexing later
        return new_item
    def _require_working_parent(self, db, file_hash):
        current = self._working_copies(db).resolve(file_hash)
        if current.get('unavailable_reason') == 'history_inconsistent':
            raise ValueError(current['message'])
        if current['status'] == 'unavailable':
            raise ValueError('The working copy could not be verified. Nothing was applied; reload before saving.')
        if current['status'] != 'current':
            raise ValueError('This version is superseded. Open the working copy and review your edits there. Nothing was applied.')

    def _history_inventory(self, db):
        bases = {}
        for row in db.execute('SELECT file_hash,result_json FROM spreadsheets').fetchall():
            bases[row['file_hash']] = json.loads(row['result_json'])
        histories = {}
        for row in db.execute('SELECT file_hash,result_json FROM spreadsheet_checks ORDER BY seq ASC').fetchall():
            histories.setdefault(row['file_hash'], []).append(json.loads(row['result_json']))
        return bases, histories

    def _working_copies(self, db):
        from hosting.working_copy import WorkingCopies
        return WorkingCopies(*self._history_inventory(db))

    def recover_self_cycle(self, file_hash, *, actor, expected_check_hash, recovery_token=None, apply=False):
        from hosting import history_recovery as recovery
        if not isinstance(actor, dict) or actor.get('role') != 'admin' or not actor.get('id'):
            raise ValueError('Only an authenticated admin can recover version history.')
        with recovery.outer_commit_only(self), self.connect() as db:
            self._begin(db, file_hash)
            if self.pg is not None:
                db.execute('LOCK TABLE spreadsheets, spreadsheet_checks IN SHARE ROW EXCLUSIVE MODE')
            original, raw = self._original(db, file_hash)
            bases, histories = self._history_inventory(db)
            current = self.latest(db, file_hash, original)
            if current.get('check_hash') != expected_check_hash:
                raise ValueError('The report changed. Review current history before recovery.')
            plan = recovery.proposal(file_hash, bases, histories)
            plan.update(file_hash=file_hash, expected_check_hash=expected_check_hash, original_sha256=hashlib.sha256(raw).hexdigest())
            if not apply:
                return plan
            if not recovery_token or recovery_token != plan['recovery_token']:
                raise ValueError('The saved inventory changed. Nothing was recovered; review it again.')
            result = recovery.cleared(current)
            result.pop('check_hash', None); result.pop('checked_at', None)
            result[recovery.FIELD] = dict(plan['receipt'], by={k: actor.get(k) for k in ('id', 'role', 'email', 'name')},
                                          at=datetime.now(timezone.utc).isoformat())
            saved = self.save_check(db, result)
            if self._working_copies(db).resolve(file_hash).get('status') != 'current':
                raise ValueError('Recovery did not establish a current review root. Nothing was saved.')
            return saved

    def working_copy(self, file_hash, authorize=None):
        with self.connect() as db:
            return self._working_copies(db).resolve(file_hash, authorize or (lambda item: True))

    def _shared_correction_ancestry(self, db, left, right):
        """Prove membership from append-only stored parent links, never filename/root hints.

        A correction initially has a check without a parent, followed in the same
        transaction by its permanent fixed_from record. All later parent records
        must agree with that first record. Missing, cyclic or conflicting chains
        fail closed; no metadata or history is rewritten when a copy is reused.
        """
        graph = self._working_copies(db)
        def chain(start):
            seen = set()
            current = start
            for _ in range(1000):
                if current in seen or current not in graph.items or current in graph.invalid:
                    return None
                seen.add(current)
                current = graph.parents.get(current)
                if current is None:
                    return seen
            return None
        a, b = chain(left), chain(right)
        return bool(a and b and a.intersection(b))

    @staticmethod
    def held_cells(item, auto=True):
        """(sheet, CELL) a person decided on (kept as recorded or deferred), plus, for automatic work, those whose
        earlier decision no longer matches. Automatic paths leave these out; apply_fixes refuses them in any case."""
        from hosting.review_summary import summarize
        rv = summarize(item)
        return {(x.get('sheet'), str(x.get('cell') or '').upper()) for x in rv['issues'] + rv['dismissed'] if x.get('cell') and (
            (x.get('decision') or {}).get('action') in ('reject', 'defer') or (auto and x.get('previous_decision')))}
    def _check_changes(self, result, approved, auto, raw=None):
        """Refuse (FixError) a change that would override a person or use stale or unidentified source data. Every path
        that makes a corrected version comes through here (review screen, automatic pass, Claude, clean-up, autopilot,
        public API), and it runs again under the report's lock.
        - A finding a person kept as recorded or deferred is never changed (reopen it first). Reverts of
          earlier automatic changes are also paused pending safety review.
        - auto=True (anything a person did not type or pick cell by cell): also not a finding whose earlier decision no
          longer matches, and not an approved, sent or already-corrected version.
        - A SharePoint value must name the read it came from (live reconciliation snapshot, classic comparison, or the
          export inside this file), and that read must still be the current one. The automatic paths may only write
          SharePoint's own value, for the same item and field."""
        from hosting import reconcile
        from hosting.fixes import FixError
        from hosting.review_summary import summarize, source_check_id
        snap = (reconcile.current(result) or {}).get('snapshot_id')
        if any(c.get('snapshot_id') and not str(c['snapshot_id']).startswith(('classic:', 'export:')) for c in approved):
            rec = reconcile.current(result) or {}
            try:
                mapping_current = self.source_mapping(rec.get('site_id'), rec.get('list_id'))
            except ValueError:
                raise FixError('The source column mapping has no exact identity. Compare the assigned source again.') from None
            if rec.get('mapping_binding') != mapping_current or mapping_current['legacy_requires_confirmation']:
                raise FixError('The source column mapping changed after these corrections were reviewed. Compare again before applying.')
        classic = 'classic:' + str(source_check_id(result))
        export = 'export:' + str(result.get('file_hash'))
        rv = summarize(result)
        everything = rv['issues'] + rv['dismissed']
        sp = {(x.get('sheet'), str(x.get('cell') or '').upper()): x for x in everything if x.get('source') == 'SharePoint' and x.get('cell')}
        if auto:
            if result.get('approval') or any(x.get('sent') for x in result.get('sent') or []):
                raise FixError('This version is approved or sent; it is left exactly as it is.')
            if result.get('fixes_applied'):
                raise FixError('A newer version of this report exists; it was not fixed automatically.')
        held = {}
        for x in everything:
            if not x.get('cell'):
                continue
            act = (x.get('decision') or {}).get('action')
            if act in ('reject', 'defer') or (auto and x.get('previous_decision')):
                held[(x.get('sheet'), str(x.get('cell') or '').upper())] = act or 'stale'
        for c in approved:
            if str(c.get('source') or '').strip().casefold().startswith('reverted'):
                raise FixError('Revert workbook changes are disabled pending safety review.')
            why = held.get((c.get('sheet'), str(c.get('cell') or '').upper()))
            if why:
                why = {'reject': 'a person decided to keep it as recorded', 'defer': 'it is waiting for the source owner'}.get(
                    why, 'an earlier decision on it no longer matches and needs a person')
                raise FixError(f'{c.get("sheet")}!{c.get("cell")} was not changed: {why}. Reopen it first. Nothing was applied.')
        from hosting.selected_corrections import validate as validate_selected_corrections
        validate_selected_corrections(result, raw, approved)
        source_proposals = None
        for c in approved:
            where = (c.get('sheet'), str(c.get('cell') or '').upper())
            src = str(c.get('source') or '')
            person = src == 'Your edit'
            backed = src.startswith('SharePoint') or c.get('snapshot_id') or (where in sp and not person)
            if not backed:
                continue
            sid = c.get('snapshot_id')
            if not (sid and c.get('item') and c.get('field')):
                raise FixError(f'The correction for {where[0]}!{where[1]} does not say which SharePoint read, item and field it came from, so it '
                               'cannot be checked against SharePoint. Nothing was applied. Review it again.')
            if sid not in (snap, classic, export):
                raise FixError('SharePoint was read again or its comparison changed after this correction was reviewed. Nothing was applied. Review it again.')
            if not person:
                if source_proposals is None:
                    from hosting.fixes import proposals
                    if raw is None:
                        _, raw = self.original_bytes(result['file_hash'])
                    source_proposals = proposals(result, result['filename'], raw)
                fields = ('sheet', 'cell', 'snapshot_id', 'item', 'field', 'before', 'after', 'change_set', 'change_set_size')
                if not any(all(str(c.get(k) if c.get(k) is not None else '') == str(p.get(k) if p.get(k) is not None else '') for k in fields) for p in source_proposals):
                    raise FixError('The source-backed change does not match the current source proposal. Review it again or make an explicit manual edit.')
            if str(sid).startswith('classic:'):
                if sid != classic:
                    raise FixError(f'The SharePoint comparison was run again after {where[0]}!{where[1]} was reviewed. Nothing was applied. '
                                   'Review the corrections against the current comparison.')
                continue
            if str(sid).startswith('export:'):
                if sid != export:
                    raise FixError(f'The correction for {where[0]}!{where[1]} came from another version of this report. Nothing was applied.')
                continue
            if sid != snap:
                raise FixError(f'SharePoint was read again after {where[0]}!{where[1]} was reviewed, so that correction may be out of date. '
                               'Nothing was applied. Review the corrections against the current SharePoint data.')
            x = sp.get(where)
            if not person:
                if not x or str(x.get('item')) != str(c['item']) or str(x.get('field')) != str(c['field']):
                    raise FixError(f'SharePoint no longer reports a difference for item {c["item"]} {c["field"]} at {where[0]}!{where[1]}. Nothing was applied.')
                if str(c.get('after')) != str(x.get('source_value')):
                    raise FixError(f'{where[0]}!{where[1]} may only be changed to SharePoint\'s value ({x.get("source_value")}). Nothing was applied.')
    def download(self, file_hash):
        with self.connect() as db:
            original, raw = self._original(db, file_hash)
        return original['filename'], raw
    def list(self, *, working=False, authorize=None):
        with self.connect() as db:
            if working:
                graph = self._working_copies(db)
                results = []
                for key, item in graph.items.items():
                    if authorize is not None and not authorize(item):
                        continue
                    resolution = graph.resolve(key, authorize or (lambda item: True))
                    if resolution["status"] in ("superseded", "choice"):
                        continue
                    results.append(dict(item, working_copy=resolution))
                results.sort(key=lambda item: item.get("received_at") or "", reverse=True)
                count = len(results)
                results = results[:100]
            else:
                results, count = self._listed_results(db)
        return self._list_payload(results, count, working=working)

    def _listed_results(self, db):
        rows = db.execute('SELECT result_json FROM spreadsheets ORDER BY seq DESC LIMIT 100').fetchall()
        count = db.execute('SELECT COUNT(*) FROM spreadsheets').fetchone()[0]
        results = []
        for stored in rows:   # one unreadable report never empties the whole list
            base = json.loads(stored['result_json'])
            try:
                results.append(self.latest(db, base['file_hash'], base))
            except Exception:
                results.append(dict(base, internal_check_status='could_not_load_latest_check'))
        return results, count

    def _list_payload(self, results, count, *, working=False):
        for result in results:
            if result.get('checker_version')!=CHECKER_VERSION:
                result['internal_check_status']='outdated_checks_require_recheck'
        # List endpoint is bounded; detailed results remain available by hash.
        fields = ('file_hash','filename','version','revision','root_hash','received_at','format','status','internal_check_status','verification_scope','release_status','finding_count','findings_truncated','sheet_count','row_count','working_copy')
        from hosting.review_summary import summarize
        try:
            ctx = self.assignment_context()
        except Exception:
            ctx = None
        def row(result):
            item = {key: result[key] for key in fields if key in result}
            try:
                item['review'] = self.review_summary(result, with_issues=False)
            except Exception:
                item['review'] = None
            if ctx is not None:
                try:
                    item['assignment'] = self.assignment_for(result, item['review'], ctx)
                except Exception:
                    item['assignment'] = None
            return item
        return {'spreadsheets': [row(result) for result in results],
                'count': count, 'limit':100, 'has_more': count > 100, 'working_copies': working,
                'notice':'Mapped checks and optional separately supplied source comparisons. No source authentication, business approval or release.'}
    def get(self, file_hash):
        """The stored result. A result made by an older checker is returned as it is, marked outdated: new checks
        are only shown as a proposal (propose_recheck) and saved when a person applies them (apply_recheck)."""
        with self.connect() as db:
            found = db.execute('SELECT result_json FROM spreadsheets WHERE file_hash=?', (file_hash,)).fetchone()
            if not found: raise ValueError('Spreadsheet version not found.')
            result=self.latest(db,file_hash,json.loads(found['result_json']))
        if result.get('checker_version')!=CHECKER_VERSION:
            # never reclassified behind anyone's back: the stored result is shown as it is, marked out of date, and the
            # new checks are saved only when a person presses Re-check (apply_recheck)
            result=dict(result, internal_check_status='outdated_checks_require_recheck',
                        recheck=dict(available=True, checked_with=result.get('checker_version'), current=CHECKER_VERSION))
        return result
    RECHECK_KEEP = ('history_recovery','approval','sent','send_pending','send_in_progress','fixes_applied','fixed_from','review_request','source_document','accepted_issues','auto_formatting','ai_fixed',
                    'source_reconciliation','approval_withdrawn','recheck_applied','review_decisions','review_decision_log','revision','root_hash',
                    'finding_decisions','finding_decision_log','finding_decisions_rev','finding_requests')
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
            rv = self.review_summary(r, with_issues=False)
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
            self._begin(db, file_hash)
            original, _ = self._original(db, file_hash)
            locked = self.latest(db, file_hash, original)
            if locked.get('check_hash') != current.get('check_hash'):
                raise ValueError('This report changed while the new checks were prepared. No checks were applied; reload and review again.')
            saved = self.save_check(db, fresh)
        previous = current.get('sharepoint') or {}
        if previous.get('status') in ('match','differences') and previous.get('source_sha256') and not current.get('source_reconciliation'):
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
            self._begin(db, file_hash)
            row=db.execute('SELECT result_json FROM spreadsheets WHERE file_hash=?',(file_hash,)).fetchone()
            if not row:raise ValueError('Spreadsheet version not found.')
            original=json.loads(row['result_json']);raw=files_for(self.root).get(original['original_path'])
            if hashlib.sha256(raw).hexdigest()!=file_hash:raise ValueError('Stored original integrity mismatch.')
            result=reconcile_result(inspect_spreadsheet(original['filename'],raw),source)
            latest=self.latest(db,file_hash,original)
            # Legacy source reconciliation rechecks the immutable workbook, not its
            # workflow. Never erase approvals, sends or correction ancestry when
            # appending new evidence to an exact historical version.
            result.update({k: latest[k] for k in self.RECHECK_KEEP if k in latest})
            if result.get('source_reconciliation'):
                result['source_reconciliation'] = dict(result['source_reconciliation'], stale=True,
                    stale_reason='Separate source evidence was applied; reconcile the exact assigned source again.')
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
