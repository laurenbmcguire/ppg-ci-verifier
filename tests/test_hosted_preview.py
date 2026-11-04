"""Protected Vercel preview: host gate and Postgres commit semantics (fake connection, no network)."""
import copy
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from api.index import allowed_host, hosted_preview
from hosting.postgres_storage import PostgresWorkspace
from hosting.storage import StorageUnavailable, pack
from hosting.storage import NAMESPACE

PREVIEW = {'VERCEL': '1', 'VERCEL_ENV': 'preview', 'CI_VERCEL_PROTECTED_PREVIEW': '1',
           'VERCEL_URL': 'ppg-ci-verifier-abc.vercel.app', 'VERCEL_BRANCH_URL': 'ppg-ci-verifier-git-main.vercel.app'}


class FakeDB:
    def __init__(self, fail_commit=False):
        self.state = {}
        self.fail_commit = fail_commit
        self.commits = 0

    def connect(self, url):
        return FakeConn(self)


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.row = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        if sql.startswith('INSERT INTO ci_verifier_state'):
            self.conn.working.setdefault(params[0], (None, 0))
        elif sql.startswith('SELECT payload'):
            self.row = self.conn.working.get(params[0])
        elif sql.startswith('UPDATE'):
            payload, revision, namespace = params
            self.conn.working[namespace] = (payload, revision)
        return self

    def fetchone(self):
        if self.row is None:
            return None
        return (self.row[0], self.row[1])


class FakeConn:
    def __init__(self, db):
        self.db = db
        self.working = copy.deepcopy(db.state)
        self.closed = False

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        if self.db.fail_commit:
            raise OSError('simulated commit failure')
        self.db.state = self.working
        self.db.commits += 1

    def rollback(self):
        self.working = copy.deepcopy(self.db.state)

    def close(self):
        self.closed = True


class HostedGateTests(unittest.TestCase):
    def test_hosted_only_with_preview_flag(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(hosted_preview())
        with patch.dict(os.environ, {**PREVIEW, 'VERCEL_ENV': 'production'}):
            self.assertFalse(hosted_preview())  # production only forwards to the preview app
        with patch.dict(os.environ, {**PREVIEW, 'VERCEL_ENV': 'development'}):
            self.assertFalse(hosted_preview())
        with patch.dict(os.environ, PREVIEW):
            self.assertTrue(hosted_preview())

    def test_only_deployment_urls_allowed(self):
        with patch.dict(os.environ, PREVIEW):
            self.assertTrue(allowed_host('ppg-ci-verifier-abc.vercel.app'))
            self.assertTrue(allowed_host('PPG-CI-VERIFIER-GIT-MAIN.vercel.app'))
            self.assertFalse(allowed_host('evil.example'))
            self.assertFalse(allowed_host('ppg-ci-verifier-abc.vercel.app.evil.example'))
            self.assertFalse(allowed_host('127.0.0.1:8766'))

    def test_local_loopback_unchanged_when_not_hosted(self):
        with patch.dict(os.environ, {'CI_LOCAL_DEV': '1'}, clear=True):
            self.assertTrue(allowed_host('127.0.0.1:8766'))
            self.assertFalse(allowed_host('ppg-ci-verifier-abc.vercel.app'))


class PostgresCommitTests(unittest.TestCase):
    def test_write_commits_and_reads_back(self):
        db = FakeDB()
        store = PostgresWorkspace('postgresql://fake', connect=db.connect)
        with store.session() as root:
            (root / 'review.sqlite3').write_bytes(b'durable')
        self.assertEqual(db.commits, 1)
        with store.session() as root:
            self.assertEqual((root / 'review.sqlite3').read_bytes(), b'durable')

    def test_exception_in_body_rolls_back_and_passes_through(self):
        db = FakeDB()
        store = PostgresWorkspace('postgresql://fake', connect=db.connect)
        with store.session() as root:
            (root / 'review.sqlite3').write_bytes(b'original')
        before = copy.deepcopy(db.state)
        with self.assertRaises(ValueError):
            with store.session() as root:
                (root / 'review.sqlite3').write_bytes(b'changed')
                raise ValueError('caller error must not become a storage error')
        self.assertEqual(db.state, before)

    def test_commit_failure_is_never_success(self):
        db = FakeDB(fail_commit=True)
        store = PostgresWorkspace('postgresql://fake', connect=db.connect)
        with self.assertRaises(StorageUnavailable):
            with store.session() as root:
                (root / 'review.sqlite3').write_bytes(b'unsaved')
        self.assertEqual(db.state, {})

    def test_missing_database_url_fails_closed(self):
        with self.assertRaises(StorageUnavailable):
            with PostgresWorkspace(None).session():
                pass


if __name__ == '__main__':
    unittest.main()


class ProductionForwardTests(unittest.TestCase):
    def test_production_forwards_every_get_to_the_preview_app(self):
        import threading, urllib.request, urllib.error
        from http.server import ThreadingHTTPServer
        from api.index import handler, APP_HOST
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k): return None
        opener = urllib.request.build_opener(NoRedirect)
        try:
            with patch.dict(os.environ, {'VERCEL': '1', 'VERCEL_ENV': 'production'}):
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    opener.open(f'http://127.0.0.1:{server.server_port}/api/spreadsheets?x=1', timeout=10)
            self.assertEqual(ctx.exception.code, 307)
            self.assertEqual(ctx.exception.headers['Location'], f'https://{APP_HOST}/api/spreadsheets?x=1')
        finally:
            server.shutdown(); server.server_close()
