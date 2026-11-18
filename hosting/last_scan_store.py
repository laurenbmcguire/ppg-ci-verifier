"""Durable state for the last-scan draft runner: one interface over SQLite and PostgreSQL.

Every change goes through ``atomic(keys)``: all named records are read under lock and written in ONE database
transaction. The lease is a record like any other, so a lease check, a current-operation check, a state update and
an outbox insert can be committed (or all refused) together. Nothing is retried here.

Photo bytes and captured snapshots are content-addressed blobs kept apart from the state rows, so a state row stays
small however many photos a shift has. PostgreSQL tables are created only by an explicit ``install()``.
"""
import hashlib
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from hosting import source_first as sf

STATE, BLOB = 'scan_draft_state', 'scan_draft_blob'
MAX_BLOB = 64 * 1024 * 1024


class LeaseLost(RuntimeError):
    """The caller no longer holds the scope: nothing it prepared may be committed."""


def _instant(value):
    d = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if d.tzinfo is None:
        raise ValueError('Lease times must carry an offset.')
    return d.astimezone(timezone.utc)


class _Store:
    @contextmanager
    def atomic(self, keys):
        """Yield {key: record}. Every record is locked, and all changes commit together or not at all."""
        keys = sorted({str(k) for k in keys})
        if not keys:
            raise ValueError('Name at least one record.')
        with self._tx() as db:
            records = {k: self._read(db, k) for k in keys}       # sorted order: two writers never deadlock on each other
            before = {k: sf.canonical(v) for k, v in records.items()}
            yield records
            for k in keys:
                body = sf.canonical(records[k])
                if body != before[k]:
                    self._write(db, k, body.decode())

    @contextmanager
    def transaction(self, key):
        with self.atomic([key]) as records:
            yield records[key]

    def put_blob(self, body):
        body = bytes(body)
        if len(body) > MAX_BLOB:
            raise ValueError('Blob exceeds the private storage limit.')
        sha = hashlib.sha256(body).hexdigest()
        with self._tx() as db:
            self._put_blob(db, sha, body)
        return sha

    def get_blob(self, sha):
        with self._tx() as db:
            body = self._get_blob(db, sha)
        if body is None:
            raise KeyError('Blob not found: ' + str(sha))
        body = bytes(body)
        if hashlib.sha256(body).hexdigest() != sha:
            raise ValueError('Stored blob does not match its address.')
        return body


class SqliteStore(_Store):
    """Local and test store. One writer at a time for the whole file (BEGIN IMMEDIATE)."""
    def __init__(self, path, timeout=30):
        self.path, self.timeout = str(path), timeout
        self.install()

    def install(self):
        with closing(sqlite3.connect(self.path, timeout=self.timeout)) as db, db:
            db.execute(f'CREATE TABLE IF NOT EXISTS {STATE} (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            db.execute(f'CREATE TABLE IF NOT EXISTS {BLOB} (sha256 TEXT PRIMARY KEY, body BLOB NOT NULL)')

    @contextmanager
    def _tx(self):
        with closing(sqlite3.connect(self.path, timeout=self.timeout, isolation_level=None)) as db:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            try:
                yield db
            except BaseException:
                db.execute('ROLLBACK')
                raise
            db.execute('COMMIT')

    def _read(self, db, key):
        import json
        row = db.execute(f'SELECT value FROM {STATE} WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else {}

    def _write(self, db, key, value):
        db.execute(f'INSERT INTO {STATE}(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, value))

    def _put_blob(self, db, sha, body):
        db.execute(f'INSERT INTO {BLOB}(sha256,body) VALUES (?,?) ON CONFLICT(sha256) DO NOTHING', (sha, body))

    def _get_blob(self, db, sha):
        row = db.execute(f'SELECT body FROM {BLOB} WHERE sha256=?', (sha,)).fetchone()
        return row[0] if row else None


class PostgresStore(_Store):
    """Hosted store. ``connect()`` returns a new psycopg connection (autocommit off); this class never sees a DSN.

    Each record is a row locked with SELECT ... FOR UPDATE inside the transaction, so two instances contending for
    the same scope serialize on its rows and the loser reads the winner's committed lease.
    """
    def __init__(self, connect, *, schema=None, lock_timeout='10s', statement_timeout='60s'):
        import re
        if schema is not None and not re.fullmatch(r'[a-z_][a-z0-9_]{0,62}', schema):
            raise ValueError('Invalid schema name.')
        self.connect, self.prefix = connect, (schema + '.') if schema else ''
        self.lock_timeout, self.statement_timeout = lock_timeout, statement_timeout

    def install(self):
        """Explicit, idempotent table creation. Never called implicitly: running it is a deployment decision."""
        with self._tx() as cur:
            cur.execute(f'CREATE TABLE IF NOT EXISTS {self.prefix}{STATE} (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            cur.execute(f'CREATE TABLE IF NOT EXISTS {self.prefix}{BLOB} (sha256 TEXT PRIMARY KEY, body BYTEA NOT NULL)')

    @contextmanager
    def _tx(self):
        conn = self.connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT set_config('lock_timeout', %s, true), set_config('statement_timeout', %s, true)",
                            (self.lock_timeout, self.statement_timeout))
                yield cur
            conn.commit()
        except BaseException:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _read(self, cur, key):
        import json
        cur.execute(f"INSERT INTO {self.prefix}{STATE}(key,value) VALUES (%s,'{{}}') ON CONFLICT (key) DO NOTHING", (key,))
        cur.execute(f'SELECT value FROM {self.prefix}{STATE} WHERE key=%s FOR UPDATE', (key,))
        return json.loads(cur.fetchone()[0])

    def _write(self, cur, key, value):
        cur.execute(f'UPDATE {self.prefix}{STATE} SET value=%s WHERE key=%s', (value, key))

    def _put_blob(self, cur, sha, body):
        cur.execute(f'INSERT INTO {self.prefix}{BLOB}(sha256,body) VALUES (%s,%s) ON CONFLICT (sha256) DO NOTHING', (sha, body))

    def _get_blob(self, cur, sha):
        cur.execute(f'SELECT body FROM {self.prefix}{BLOB} WHERE sha256=%s', (sha,))
        row = cur.fetchone()
        return row[0] if row else None


# ---------------------------------------------------------------- leases with fencing
def lease_key(scope_digest):
    return 'lease:' + scope_digest


def claim(store, scope_digest, now, seconds, owner=None):
    """Take the scope if nobody holds an unexpired lease. Returns the held lease, or None when busy.

    ``fence`` grows by one with every claim: a worker that stalled past its expiry and was replaced holds an older
    fence and is refused at commit time, whatever its own clock says."""
    key = lease_key(scope_digest)
    with store.atomic([key]) as records:
        lease = records[key]
        if lease.get('expires') and _instant(lease['expires']) > _instant(now):
            return None
        held = dict(fence=int(lease.get('fence') or 0) + 1, token=str(uuid4()), owner=owner,
                    expires=(_instant(now) + timedelta(seconds=seconds)).isoformat(), claimed_at=now)
        lease.clear()
        lease.update(held)
        return dict(held)


def verify(lease, held, now):
    if lease.get('token') != held['token'] or int(lease.get('fence') or 0) != held['fence']:
        raise LeaseLost('Another worker took this scope; nothing was committed.')
    if not lease.get('expires') or _instant(lease['expires']) <= _instant(now):
        raise LeaseLost('The lease expired before the commit; nothing was committed.')


@contextmanager
def fenced(store, scope_digest, held, now, keys):
    """``atomic(keys)`` that also holds the lease row and refuses to commit unless ``held`` is still the lease."""
    key = lease_key(scope_digest)
    with store.atomic(list(keys) + [key]) as records:
        verify(records[key], held, now() if callable(now) else now)
        yield records


def renew(store, scope_digest, held, now, seconds):
    with fenced(store, scope_digest, held, now, []) as records:
        records[lease_key(scope_digest)]['expires'] = (_instant(now) + timedelta(seconds=seconds)).isoformat()


def release(store, scope_digest, held, now):
    """Give the scope back early. A lease that is no longer ours is left exactly as it is."""
    key = lease_key(scope_digest)
    with store.atomic([key]) as records:
        lease = records[key]
        if lease.get('token') == held['token'] and int(lease.get('fence') or 0) == held['fence']:
            lease['expires'] = now
            return True
        return False
