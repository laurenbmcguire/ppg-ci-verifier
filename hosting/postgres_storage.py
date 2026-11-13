"""Hosted Vercel preview storage: one locked Postgres row holds the bounded workspace snapshot.

Every request restores a request-local workspace inside one transaction, runs the
retained feature logic, and commits the snapshot. Success is reported only after
the commit. Local-only SQLite storage is unchanged and never used on Vercel.
"""
from contextlib import contextmanager
import os
from pathlib import Path
import tempfile

import psycopg

from hosting.storage import StorageUnavailable, restore, NAMESPACE
from hosting.sqlite_storage import checked_pack, immutable_files
from hosting.files import PostgresFiles, activate

DB_ERRORS = (psycopg.Error, OSError)
UNAVAILABLE = 'Durable storage is unavailable. No success was confirmed; reload before retrying.'


_BUSY = ('too many connections', 'max clients reached', 'remaining connection slots')


def psycopg_connect(database_url, attempts=5, wait=0.5, sleep=None):
    """Connect, waiting briefly when the database only says it is full right now.

    A page with many pictures opens several requests at once and the pooler allows a fixed number of connections.
    "Too many connections" clears within moments, so a short wait and another try turns a failed save into a
    slightly slower one. Any other failure is reported at once, exactly as before.
    """
    import time
    sleep = sleep or time.sleep
    for attempt in range(attempts):
        try:
            return psycopg.connect(database_url, autocommit=False, connect_timeout=5)
        except psycopg.OperationalError as exc:
            if attempt == attempts - 1 or not any(text in str(exc).lower() for text in _BUSY):
                raise
            sleep(wait * (attempt + 1))


class PostgresWorkspace:
    storage_label = 'vercel-postgres'

    def __init__(self, database_url=None, connect=None):
        self.database_url = database_url
        self.connect = connect or psycopg_connect

    @contextmanager
    def session(self):
        if not self.database_url:
            raise StorageUnavailable('Durable storage is not connected. No changes were saved.')
        try:
            conn = self.connect(self.database_url)
        except DB_ERRORS as exc:
            raise StorageUnavailable(UNAVAILABLE) from exc
        if isinstance(conn, psycopg.Connection) and os.environ.get('CI_NATIVE_TABLES', '1') != '0':
            with self._native(conn) as root:
                yield root
            return
        committed = False
        try:
            with conn.cursor() as cur:
                # Bounded waits; one row lock serializes concurrent requests.
                cur.execute("SET LOCAL lock_timeout = '5s'")
                cur.execute("SET LOCAL statement_timeout = '120s'")
                cur.execute("SET LOCAL idle_in_transaction_session_timeout = '120s'")  # a stalled request never holds the workspace lock for long
                cur.execute('INSERT INTO ci_verifier_state(namespace) VALUES (%s) ON CONFLICT DO NOTHING', (NAMESPACE,))
                cur.execute('SELECT payload, revision FROM ci_verifier_state WHERE namespace=%s FOR UPDATE', (NAMESPACE,))
                row = cur.fetchone()
                if row is None:
                    raise StorageUnavailable('Durable workspace is unavailable.')
                old_payload, revision = row
                old = bytes(old_payload) if old_payload is not None else None
                with tempfile.TemporaryDirectory(prefix='ci-verifier-pg-') as directory, activate(PostgresFiles(cur)):
                    root = Path(directory)
                    restore(root, old)
                    originals = immutable_files(root)
                    yield root
                    new = checked_pack(root)
                    after = immutable_files(root)
                    if any(after.get(name) != digest for name, digest in originals.items()):
                        raise StorageUnavailable('Original uploads are immutable; nothing was saved.')
                    if old != new and (old is not None or any(p.is_file() for p in root.rglob('*'))):   # an empty workspace is not a change
                        cur.execute('UPDATE ci_verifier_state SET payload=%s, revision=%s, updated_at=now() WHERE namespace=%s',
                                    (new, revision + 1, NAMESPACE))
            conn.commit()
            committed = True
        except StorageUnavailable:
            raise
        except DB_ERRORS as exc:
            # Never report success for a failed or unknown commit; caller's own errors pass through.
            raise StorageUnavailable(UNAVAILABLE) from exc
        finally:
            if not committed:
                try:
                    conn.rollback()
                except DB_ERRORS:
                    pass
            try:
                conn.close()
            except DB_ERRORS:
                pass

    @contextmanager
    def _native(self, conn):
        """Reports live in real tables (see pg_tables); only the older review features still use the packed snapshot,
        which is now saved optimistically instead of being locked for the whole request."""
        from hosting import pg_tables
        committed = False
        token = None
        try:
            with conn.cursor() as cur:
                cur.execute("SET lock_timeout = '5s'")
                cur.execute("SET statement_timeout = '120s'")
                cur.execute("SET idle_in_transaction_session_timeout = '120s'")
                cur.execute('INSERT INTO ci_verifier_state(namespace) VALUES (%s) ON CONFLICT DO NOTHING', (NAMESPACE,))
                cur.execute('SELECT payload, revision FROM ci_verifier_state WHERE namespace=%s', (NAMESPACE,))
                row = cur.fetchone()
                if row is None:
                    raise StorageUnavailable('Durable workspace is unavailable.')
                old = bytes(row[0]) if row[0] is not None else None
                revision = row[1]
            conn.commit()
            with tempfile.TemporaryDirectory(prefix='ci-verifier-pg-') as directory:
                root = Path(directory)
                restore(root, old)
                legacy = root / 'spreadsheet_inbox.sqlite3'
                if legacy.exists():   # first run on the tables: copy the reports over (the old copy stays frozen as a fallback)
                    pg_tables.migrate_snapshot(conn, legacy)
                originals = immutable_files(root)
                token = pg_tables.current.set(pg_tables.PgSession(conn))
                with conn.cursor() as cur, activate(PostgresFiles(cur)):
                    yield root
                    new = checked_pack(root)
                    after = immutable_files(root)
                    if any(after.get(name) != digest for name, digest in originals.items()):
                        raise StorageUnavailable('Original uploads are immutable; nothing was saved.')
                    if old != new:
                        cur.execute('UPDATE ci_verifier_state SET payload=%s, revision=%s, updated_at=now() WHERE namespace=%s AND revision=%s',
                                    (new, revision + 1, NAMESPACE, revision))
                        if cur.rowcount != 1:
                            raise StorageUnavailable('Someone else saved a change at the same moment. Reload and try again.')
                conn.commit()
                committed = True
        except StorageUnavailable:
            raise
        except DB_ERRORS as exc:
            raise StorageUnavailable(UNAVAILABLE) from exc
        finally:
            if token is not None:
                pg_tables.current.reset(token)
            if not committed:
                try:
                    conn.rollback()
                except DB_ERRORS:
                    pass
            try:
                conn.close()
            except DB_ERRORS:
                pass
