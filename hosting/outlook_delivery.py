"""Offline delivery-intent foundation. No provider, credentials, routes or retries.

Callbacks are trusted server code, never browser data. See OUTLOOK_DELIVERY_SETUP.md.
"""
from dataclasses import dataclass, asdict
from contextlib import contextmanager
import hashlib
import json
import re
import sqlite3
import time
import uuid

MAX_ATTACHMENT = 3_000_000
MAX_SNAPSHOT = 100_000
STATES = ('awaiting_confirmation', 'confirmed', 'dispatching', 'accepted',
          'rejected_before_send', 'unknown', 'cancelled_before_dispatch')
SCHEMA = """
CREATE TABLE IF NOT EXISTS outlook_delivery_intents (
 id TEXT PRIMARY KEY, scope TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN
 ('awaiting_confirmation','confirmed','dispatching','accepted','rejected_before_send','unknown','cancelled_before_dispatch')),
 snapshot TEXT NOT NULL, digest TEXT NOT NULL, attachment BLOB NOT NULL,
 draft TEXT NOT NULL, expires REAL NOT NULL, created REAL NOT NULL,
 confirmed_actor TEXT, confirmed_at REAL, dispatched_at REAL, provider_receipt TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS outlook_delivery_active_scope
 ON outlook_delivery_intents(scope) WHERE state IN
 ('awaiting_confirmation','confirmed','dispatching','unknown','accepted');
"""

class DeliveryError(ValueError):
    pass


@dataclass(frozen=True)
class Auth:
    actor: str
    workspace: str
    client: str
    method: str
    can_send: bool


@dataclass(frozen=True)
class Acceptance:
    """Transport may return this only for an unambiguous provider acceptance."""
    receipt: str


def canonical(value):
    try:
        raw = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                         allow_nan=False)
        if len(raw.encode('utf-8')) > MAX_SNAPSHOT:
            raise DeliveryError('Snapshot exceeds limit')
        return raw
    except (TypeError, ValueError, UnicodeError) as exc:
        raise DeliveryError('Invalid or oversized snapshot') from exc


def digest(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def _text(value, limit=1000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or '\x00' in value:
        raise DeliveryError('Missing or invalid text')
    return value


def _addresses(values):
    if not isinstance(values, list) or len(values) > 100:
        raise DeliveryError('Invalid recipients')
    result = []
    for value in values:
        value = _text(value, 254).strip().lower()
        if not re.fullmatch(r'[^\s<>@,;]+@[^\s<>@,;]+\.[^\s<>@,;]+', value):
            raise DeliveryError('Use explicit email addresses only')
        if value not in result:
            result.append(value)
    return sorted(result)


class DeliveryService:
    def __init__(self, path, *, authenticate, build_snapshot, transport=None, clock=time.time):
        self.path, self.authenticate, self.build_snapshot = path, authenticate, build_snapshot
        self.transport, self.clock = transport, clock
        with self._connect() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA synchronous=FULL')
        try:
            with db:
                yield db
        finally:
            db.close()

    def _commit(self, db, phase):
        """Fault-injection seam. A failed/uncertain claim commit NEVER invokes transport."""
        db.commit()

    def _auth(self, credential):
        # authenticate MUST verify server session, CSRF and current scoped capability.
        auth = self.authenticate(credential)
        if (type(auth) is not Auth or auth.method != 'authenticated_human_session'
                or auth.can_send is not True):
            raise DeliveryError('Authenticated human send capability required')
        for value in (auth.actor, auth.workspace, auth.client):
            _text(value)
        return auth

    def _snapshot(self, auth, report_id, draft, db):
        # Builder MUST authorize report ownership and use authoritative report/artifact data.
        data = dict(self.build_snapshot(auth, report_id, json.loads(canonical(draft)), db))
        attachment = data.pop('attachment_bytes', None)
        if type(attachment) is not bytes or not 0 < len(attachment) <= MAX_ATTACHMENT:
            raise DeliveryError('Invalid attachment bytes or size')
        required = ('report_hash', 'report_version', 'check_hash', 'check_run', 'checker_version',
                    'approval_identity', 'approval_time', 'approved_state_hash', 'source_revision', 'assignment',
                    'sender_account_id', 'attachment_name', 'attachment_mime')
        normalized = {key: _text(data.get(key)) for key in required}
        if data.get('approval_current') is not True or data.get('sender_verified') is not True:
            raise DeliveryError('Current approval and verified sender required')
        normalized.update(approval_current=True, sender_verified=True, report_id=_text(report_id),
                          auth=asdict(auth), sender=_addresses([data.get('sender')])[0],
                          to=_addresses(data.get('to')), cc=_addresses(data.get('cc')),
                          bcc=_addresses(data.get('bcc')), subject=_text(data.get('subject'), 998),
                          body=_text(data.get('body'), 50000), mode='new_message')
        if not normalized['to'] or any(c in normalized['subject'] for c in '\r\n'):
            raise DeliveryError('To and single-line subject required')
        if any(c in normalized['attachment_name'] for c in '\r\n/\\'):
            raise DeliveryError('Invalid attachment name')
        normalized.update(attachment_sha256=hashlib.sha256(attachment).hexdigest(),
                          attachment_size=len(attachment))
        canonical(normalized)
        return normalized, attachment

    def preview(self, credential, report_id, draft, *, ttl=300):
        auth = self._auth(credential)
        if type(ttl) is not int or not 1 <= ttl <= 300:
            raise DeliveryError('Invalid confirmation lifetime')
        if not isinstance(draft, dict) or set(draft) - {'to', 'cc', 'bcc', 'subject', 'body'}:
            raise DeliveryError('Unsupported draft input')
        intent_id, now = str(uuid.uuid4()), self.clock()
        try:
            with self._connect() as db:
                db.execute('BEGIN IMMEDIATE')
                snapshot, attachment = self._snapshot(auth, report_id, draft, db)
                scope = digest([auth.workspace, auth.client, report_id, snapshot['sender_account_id']])
                # Expiry releases only intents that have never begun dispatch.
                db.execute("UPDATE outlook_delivery_intents SET state='rejected_before_send' "
                           "WHERE scope=? AND expires<=? AND state IN ('awaiting_confirmation','confirmed')", (scope, now))
                db.execute('INSERT INTO outlook_delivery_intents '
                           '(id,scope,state,snapshot,digest,attachment,draft,expires,created) VALUES (?,?,?,?,?,?,?,?,?)',
                           (intent_id, scope, 'awaiting_confirmation', canonical(snapshot), digest(snapshot),
                            attachment, canonical(draft), now + ttl, now))
        except sqlite3.IntegrityError as exc:
            raise DeliveryError('Existing delivery blocks this report/sender; reconciliation required') from exc
        return dict(id=intent_id, digest=digest(snapshot), expires=now + ttl, snapshot=snapshot)

    def _row(self, db, intent_id, auth):
        row = db.execute('SELECT * FROM outlook_delivery_intents WHERE id=?', (intent_id,)).fetchone()
        if row is None or json.loads(row['snapshot'])['auth'] != asdict(auth):
            raise DeliveryError('Intent not available to this authenticated context')
        return row

    def _fresh(self, row, auth, db):
        old = json.loads(row['snapshot'])
        current, attachment = self._snapshot(auth, old['report_id'], json.loads(row['draft']), db)
        if digest(current) != row['digest'] or attachment != row['attachment']:
            raise DeliveryError('Snapshot changed; create a new preview and confirmation')
        return current, attachment

    def confirm(self, credential, intent_id, reviewed_digest):
        auth = self._auth(credential)
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            auth = self._auth(credential)
            row = self._row(db, intent_id, auth)
            if row['digest'] != reviewed_digest or row['expires'] <= self.clock():
                raise DeliveryError('Confirmation stale or expired')
            if row['state'] not in ('awaiting_confirmation', 'confirmed'):
                raise DeliveryError('Intent is no longer confirmable')
            self._fresh(row, auth, db)
            if row['state'] == 'awaiting_confirmation':
                db.execute("UPDATE outlook_delivery_intents SET state='confirmed', confirmed_actor=?, confirmed_at=? WHERE id=?",
                           (auth.actor, self.clock(), intent_id))
        return 'confirmed'

    def cancel(self, credential, intent_id):
        auth = self._auth(credential)
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            auth = self._auth(credential)
            row = self._row(db, intent_id, auth)
            if row['state'] not in ('awaiting_confirmation', 'confirmed'):
                raise DeliveryError('Dispatch may have occurred; cancellation unavailable')
            db.execute("UPDATE outlook_delivery_intents SET state='cancelled_before_dispatch' WHERE id=?", (intent_id,))
        return 'cancelled_before_dispatch'

    def status(self, credential, intent_id):
        with self._connect() as db:
            return self._row(db, intent_id, self._auth(credential))['state']

    def dispatch(self, credential, intent_id):
        if self.transport is None:
            raise DeliveryError('No transport configured; delivery is not enabled')
        auth = self._auth(credential)
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            auth = self._auth(credential)
            row = self._row(db, intent_id, auth)
            if row['state'] != 'confirmed':
                raise DeliveryError('Not dispatchable; no retry after possible dispatch')
            try:
                if row['expires'] <= self.clock():
                    raise DeliveryError('Confirmation expired')
                snapshot, attachment = self._fresh(row, auth, db)
                if row['expires'] <= self.clock():
                    raise DeliveryError('Confirmation expired during freshness check')
            except Exception:
                db.execute("UPDATE outlook_delivery_intents SET state='rejected_before_send' WHERE id=?", (intent_id,))
                self._commit(db, 'reject')
                raise
            db.execute("UPDATE outlook_delivery_intents SET state='dispatching', dispatched_at=? WHERE id=?", (self.clock(), intent_id))
            self._commit(db, 'claim')  # MUST succeed before any provider call.
        try:
            result = self.transport(snapshot, attachment)
            if type(result) is not Acceptance:
                raise DeliveryError('Ambiguous provider response')
            receipt = _text(result.receipt)
            state = 'accepted'
        except Exception:
            receipt, state = None, 'unknown'
        # A crash or persistence error leaves durable dispatching, never retryable.
        with self._connect() as db:
            db.execute("UPDATE outlook_delivery_intents SET state=?,provider_receipt=? WHERE id=? AND state='dispatching'",
                       (state, receipt, intent_id))
            self._commit(db, 'result')
        return state


def assert_mutation_allowed(db, workspace, client, report_id):
    """Call UNDER SAME report-writer transaction, before any report/source mutation.

    Dispatching/unknown may already have reached the provider; never clear them.
    Existing application writers are NOT yet wired to this integration contract.
    """
    if not db.in_transaction:
        raise DeliveryError('Report writer transaction required')
    for row in db.execute("SELECT snapshot FROM outlook_delivery_intents WHERE state IN ('dispatching','unknown')"):
        snapshot = json.loads(row[0])
        if (snapshot['auth']['workspace'], snapshot['auth']['client'], snapshot['report_id']) == (workspace, client, report_id):
            raise DeliveryError('Delivery may already have occurred; reconcile before report mutation')
