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


def psycopg_connect(database_url):
    return psycopg.connect(database_url, autocommit=False, connect_timeout=5)


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
        committed = False
        try:
            with conn.cursor() as cur:
                # Bounded waits; one row lock serializes concurrent requests.
                cur.execute("SET LOCAL lock_timeout = '5s'")
                cur.execute("SET LOCAL statement_timeout = '120s'")
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
                    if old != new:
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
