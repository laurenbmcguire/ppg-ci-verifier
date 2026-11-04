"""Where uploaded originals and in-progress upload parts live.

Local SQLite copy: files stay inside the workspace snapshot (unchanged behaviour).
Hosted Postgres mode: originals live in their own write-once table (ci_verifier.files)
and chunked-upload parts in ci_verifier.upload_parts, both inside the request's
transaction, so the per-request workspace snapshot stays small whatever the file size.
Database privileges for the app role allow INSERT but never UPDATE/DELETE on files.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
from pathlib import Path, PurePosixPath
import re

from hosting.storage import StorageUnavailable

PART_ID = re.compile(r'[0-9a-f]{32}')
_current = ContextVar('ci_verifier_files', default=None)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def files_for(root):
    """The active file store for this request, or the local filesystem under root."""
    active = _current.get()
    return active if active is not None else LocalFiles(root)


@contextmanager
def activate(store):
    token = _current.set(store)
    try:
        yield store
    finally:
        _current.reset(token)


def _check_path(relative):
    p = PurePosixPath(relative)
    if p.is_absolute() or len(p.parts) != 2 or p.parts[0] != 'uploads' or not re.fullmatch(r'[A-Za-z0-9_.-]+', p.parts[1]):
        raise ValueError('Invalid stored file path.')
    return relative


class LocalFiles:
    label = 'workspace'

    def __init__(self, root):
        self.root = Path(root)

    def exists(self, relative):
        return (self.root / _check_path(relative)).exists()

    def get(self, relative):
        path = self.root / _check_path(relative)
        if not path.exists():
            raise ValueError('Stored original not found.')
        return path.read_bytes()

    def put(self, relative, data):
        path = self.root / _check_path(relative)
        path.parent.mkdir(exist_ok=True)
        if path.exists():
            if path.read_bytes() != data:
                raise ValueError('Stored original differs; upload was not saved.')
            return
        path.write_bytes(data)
        path.chmod(0o400)

    # Chunked upload parts (local: inside the snapshot, so bounded by the snapshot cap).
    def _parts_dir(self, upload_id):
        if not PART_ID.fullmatch(upload_id):
            raise ValueError('Invalid upload id.')
        return self.root / 'upload_parts'

    def put_part(self, upload_id, index, data):
        directory = self._parts_dir(upload_id)
        directory.mkdir(exist_ok=True)
        (directory / f'{upload_id}-{int(index):06d}').write_bytes(data)

    def take_parts(self, upload_id):
        directory = self._parts_dir(upload_id)
        paths = sorted(directory.glob(f'{upload_id}-*')) if directory.exists() else []
        chunks = []
        for path in paths:
            chunks.append(path.read_bytes())
            path.unlink()
        return chunks


class PostgresFiles:
    label = 'postgres'

    def __init__(self, cursor):
        self.cur = cursor

    def exists(self, relative):
        self.cur.execute('SELECT 1 FROM ci_verifier_files WHERE path=%s', (_check_path(relative),))
        return self.cur.fetchone() is not None

    def get(self, relative):
        self.cur.execute('SELECT content, sha256 FROM ci_verifier_files WHERE path=%s', (_check_path(relative),))
        row = self.cur.fetchone()
        if row is None:
            raise ValueError('Stored original not found.')
        data = bytes(row[0])
        if digest(data) != row[1]:
            raise StorageUnavailable('Stored original integrity check failed.')
        return data

    def put(self, relative, data):
        relative = _check_path(relative)
        sha = digest(data)
        self.cur.execute('INSERT INTO ci_verifier_files(path, sha256, size, content) VALUES (%s, %s, %s, %s) ON CONFLICT (path) DO NOTHING',
                         (relative, sha, len(data), data))
        self.cur.execute('SELECT sha256 FROM ci_verifier_files WHERE path=%s', (relative,))
        row = self.cur.fetchone()
        if row is None or row[0] != sha:
            raise ValueError('Stored original differs; upload was not saved.')

    def put_part(self, upload_id, index, data):
        if not PART_ID.fullmatch(upload_id):
            raise ValueError('Invalid upload id.')
        self.cur.execute('INSERT INTO ci_verifier_upload_parts(upload_id, part_index, content) VALUES (%s, %s, %s) '
                         'ON CONFLICT (upload_id, part_index) DO UPDATE SET content=EXCLUDED.content', (upload_id, int(index), data))

    def take_parts(self, upload_id):
        if not PART_ID.fullmatch(upload_id):
            raise ValueError('Invalid upload id.')
        self.cur.execute('SELECT content FROM ci_verifier_upload_parts WHERE upload_id=%s ORDER BY part_index', (upload_id,))
        chunks = [bytes(row[0]) for row in self.cur.fetchall()]
        self.cur.execute('DELETE FROM ci_verifier_upload_parts WHERE upload_id=%s', (upload_id,))
        return chunks
