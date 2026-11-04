"""Local-only atomic workspace snapshots in a durable SQLite database.

Feature databases are materialized in disposable scratch space for one transaction.
Only the committed BLOB is authoritative; SQLite holds its process-wide write lock
through restore, application work, validation, and commit. No network DB is used.
"""
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import sqlite3
import tempfile

from hosting.storage import StorageUnavailable, pack, restore, MAX_SNAPSHOT_BYTES, MAX_FILES

APPLICATION_ID = 0x43495653  # CIVS: dedicated local verifier snapshot database

SERVERLESS_MARKERS = ('VERCEL', 'VERCEL_ENV', 'AWS_LAMBDA_FUNCTION_NAME',
                      'AWS_EXECUTION_ENV', 'K_SERVICE', 'FUNCTIONS_WORKER_RUNTIME',
                      'NETLIFY', 'NETLIFY_DEV', 'FUNCTION_TARGET')


def local_environment():
    return not any(os.environ.get(key, '').lower() not in ('', '0', 'false')
                   for key in SERVERLESS_MARKERS)


def checked_pack(root):
    # Bound reads before serialization, and reject directory symlinks too.
    count = size = 0
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(directory) / name
            if path.is_symlink():
                raise StorageUnavailable('Workspace symlinks are not permitted.')
        for name in files:
            path = Path(directory) / name
            if not path.is_file():
                raise StorageUnavailable('Workspace special files are not permitted.')
            count += 1
            size += path.stat().st_size
            if count > MAX_FILES or size > MAX_SNAPSHOT_BYTES:
                raise StorageUnavailable('Local SQLite prototype storage limit reached; nothing was saved.')
    return pack(root)


def immutable_files(root):
    result = {}
    for name in ('original', 'uploads'):
        directory = root / name
        if directory.exists():
            for path in directory.rglob('*'):
                if path.is_file():
                    result[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).digest()
    return result


def verify_database_owner(db):
    marker = db.execute('PRAGMA application_id').fetchone()[0]
    tables = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    if marker == 0 and not tables:
        return  # Only an empty database may be initialized.
    if marker != APPLICATION_ID or tables != [('workspace',)]:
        raise StorageUnavailable('This is not a LOCAL SQLITE COPY database. Choose a new empty data directory; the existing database was not changed.')
    columns = [row[1] for row in db.execute('PRAGMA table_info(workspace)')]
    if columns != ['id', 'revision', 'payload', 'sha256', 'updated_at']:
        raise StorageUnavailable('Unrecognized verifier database schema; the existing database was not changed.')


class SQLiteWorkspace:
    storage_label = 'local-sqlite'

    def __init__(self, directory, timeout=5.0):
        if not local_environment():
            raise StorageUnavailable('LOCAL SQLITE COPY cannot run on Vercel or a serverless host.')
        raw = Path(directory).expanduser().absolute()
        if any(p.is_symlink() for p in (raw, *raw.parents)):
            raise StorageUnavailable('Choose a local data directory without symbolic links.')
        self.directory = raw.resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.database = self.directory / 'workspace.sqlite3'
        self.timeout = float(timeout)
        if not 0 <= self.timeout <= 60:
            raise ValueError('SQLite lock timeout must be between 0 and 60 seconds.')

    def _connect(self):
        if not local_environment():
            raise StorageUnavailable('Local SQLite storage is disabled on serverless hosts.')
        for suffix in ('', '-wal', '-shm'):
            if Path(str(self.database) + suffix).is_symlink():
                raise StorageUnavailable('SQLite database symbolic links are not permitted.')
        # SQLite creates journals using the database file permissions.
        fd = os.open(self.database, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        os.close(fd)
        # Check ownership read-only BEFORE WAL mode or other persistent pragmas.
        # Existing unrelated databases must never receive schema/pragma changes.
        probe = sqlite3.connect(self.database.as_uri() + '?mode=ro', uri=True, timeout=self.timeout)
        try:
            verify_database_owner(probe)
        finally:
            probe.close()
        connection = sqlite3.connect(str(self.database), timeout=self.timeout, isolation_level=None)
        try:
            connection.execute(f'PRAGMA busy_timeout={int(self.timeout * 1000)}')
            if connection.execute('PRAGMA journal_mode=WAL').fetchone()[0].lower() != 'wal':
                raise StorageUnavailable('SQLite WAL could not be enabled.')
            connection.execute('PRAGMA synchronous=FULL')
            connection.execute('PRAGMA foreign_keys=ON')
            return connection
        except BaseException:
            connection.close()
            raise

    @contextmanager
    def session(self):
        db = None
        try:
            db = self._connect()
            db.execute('BEGIN IMMEDIATE')
            verify_database_owner(db)
            db.execute(f'PRAGMA application_id={APPLICATION_ID}')
            db.execute('''CREATE TABLE IF NOT EXISTS workspace (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                revision INTEGER NOT NULL CHECK (revision >= 0),
                payload BLOB NOT NULL,
                sha256 TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
            shape = db.execute('SELECT typeof(payload), length(payload) FROM workspace WHERE id=1').fetchone()
            if shape and (shape[0] != 'blob' or shape[1] > MAX_SNAPSHOT_BYTES):
                raise StorageUnavailable('Stored workspace exceeds the local SQLite limit or is invalid.')
            row = db.execute('SELECT payload, revision, sha256 FROM workspace WHERE id=1').fetchone()
            old, revision = (row[0], row[1]) if row else (None, 0)
            if row and hashlib.sha256(old).hexdigest() != row[2]:
                raise StorageUnavailable('SQLite workspace integrity check failed; nothing was saved.')
            with tempfile.TemporaryDirectory(prefix='ci-verifier-sqlite-') as directory:
                root = Path(directory)
                restore(root, old)
                originals = immutable_files(root)
                yield root
                new = checked_pack(root)
                after = immutable_files(root)
                if any(after.get(name) != digest for name, digest in originals.items()):
                    raise StorageUnavailable('Original uploads are immutable; nothing was saved.')
                if old != new:
                    db.execute('''INSERT INTO workspace(id, revision, payload, sha256) VALUES(1, ?, ?, ?)
                        ON CONFLICT(id) DO UPDATE SET revision=excluded.revision, payload=excluded.payload,
                        sha256=excluded.sha256, updated_at=CURRENT_TIMESTAMP''',
                        (revision + 1, new, hashlib.sha256(new).hexdigest()))
            # No success can be returned from the context manager before commit.
            db.commit()
        except (sqlite3.Error, OSError) as exc:
            raise StorageUnavailable('Local SQLite storage is unavailable or busy. No success was confirmed; reload before retrying.') from exc
        finally:
            if db is not None:
                if db.in_transaction:
                    db.rollback()
                db.close()
