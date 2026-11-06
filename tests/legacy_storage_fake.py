"""Legacy fake transaction protocol for inherited tests only. No network connector."""
from contextlib import contextmanager
from pathlib import Path
import tempfile
from hosting.storage import StorageUnavailable, pack, restore, NAMESPACE

class PostgresWorkspace:
    def __init__(self, database_url=None, connect=None):
        self.database_url = database_url
        self.connector = connect

    @contextmanager
    def session(self):
        if not self.database_url:
            raise StorageUnavailable('Durable storage is not connected. No changes were saved.')
        if self.connector is None:
            raise StorageUnavailable('Legacy test adapter requires an explicit in-memory connector.')
        connector, kwargs = self.connector, {}
        # One locked row serializes every worker. The lock and snapshot commit share
        # one transaction, so parallel requests cannot silently overwrite each other.
        with connector(self.database_url, **kwargs) as db:
            db.execute("SET LOCAL lock_timeout = '5s'")
            db.execute("SET LOCAL statement_timeout = '10s'")
            db.execute('INSERT INTO ci_verifier_state(namespace) VALUES (%s) ON CONFLICT DO NOTHING', (NAMESPACE,))
            row = db.execute('SELECT payload, revision FROM ci_verifier_state WHERE namespace=%s FOR UPDATE', (NAMESPACE,)).fetchone()
            if row is None:
                raise StorageUnavailable('Durable workspace is unavailable.')
            old_payload, revision = row
            with tempfile.TemporaryDirectory(prefix='ci-verifier-') as directory:
                restore(directory, old_payload)
                yield Path(directory)
                new_payload = pack(directory)
                changed = any(p.is_file() for p in Path(directory).rglob('*')) if old_payload is None else bytes(old_payload) != new_payload
                if changed:   # as in production: an empty new workspace is not a change
                    db.execute('UPDATE ci_verifier_state SET payload=%s, revision=%s, updated_at=now() WHERE namespace=%s',
                               (new_payload, revision + 1, NAMESPACE))
        # Context-manager exit commits, before the caller can send success.
