"""Chunked spreadsheet upload (no file-size limit) and the Postgres file store (fake cursor, no network)."""
import base64
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from api.index import handler
from hosting.files import PostgresFiles, LocalFiles, files_for, activate
from hosting.storage import StorageUnavailable
from legacy_storage_fake import PostgresWorkspace
from test_vercel import FakeDatabase


def sha(data):
    return hashlib.sha256(data).hexdigest()


class ChunkedUploadHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = patch.dict(os.environ, {'CI_LOCAL_DEV': '1', 'VERCEL': '0'})
        cls.env.start()

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()

    def setUp(self):
        self.database = FakeDatabase()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        self.server.workspace_store = PostgresWorkspace('postgresql://test.invalid/verifier', self.database.connect)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()

    def post(self, path, body):
        request = urllib.request.Request(self.base + path, data=json.dumps(body).encode(),
                                         headers={'Content-Type': 'application/json', 'Origin': self.base, 'X-Review-Token': 'same-origin'})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def upload(self, name, content, chunk=7, upload_id='a' * 32, sha256=None, part_count=None, skip=()):
        parts = [content[i:i + chunk] for i in range(0, len(content), chunk)]
        for index, part in enumerate(parts):
            if index in skip:
                continue
            code, result = self.post('/api/spreadsheets/upload/part', {'upload_id': upload_id, 'index': index, 'content_base64': base64.b64encode(part).decode()})
            self.assertEqual(code, 200, result)
            self.assertEqual(result['part']['part_index'], index)
        return self.post('/api/spreadsheets/upload/finish', {'upload_id': upload_id, 'filename': name,
                                                             'part_count': len(parts) if part_count is None else part_count,
                                                             'sha256': sha(content) if sha256 is None else sha256})

    def test_parts_assemble_verify_and_check(self):
        content = b'id,inspected,accepted,rejected\nA,10,8,2\nB,5,5,0\n'
        code, result = self.upload('counts.csv', content)
        self.assertEqual(code, 200, result)
        item = result['item']
        self.assertEqual(item['file_hash'], sha(content))
        self.assertEqual(item['version'], 1)
        self.assertFalse(item['idempotent'])
        # Original is retained byte-for-byte and parts are gone.
        with self.server.workspace_store.session() as root:
            self.assertEqual((root / item['original_path']).read_bytes(), content)
            self.assertEqual(list((root / 'upload_parts').glob('*')) if (root / 'upload_parts').exists() else [], [])
        # Same bytes again (new upload id) is idempotent.
        code, again = self.upload('counts.csv', content, upload_id='b' * 32)
        self.assertEqual(code, 200)
        self.assertTrue(again['item']['idempotent'])

    def test_missing_part_fails_closed(self):
        content = b'a,b\n1,2\n3,4\n5,6\n'
        code, result = self.upload('gap.csv', content, chunk=4, skip={1})
        self.assertEqual(code, 409)
        self.assertIn('incomplete', result['error'])
        self.assertEqual(json.loads(urllib.request.urlopen(self.base + '/api/spreadsheets').read())['count'], 0)

    def test_hash_mismatch_fails_closed(self):
        content = b'a,b\n1,2\n'
        code, result = self.upload('tampered.csv', content, sha256='0' * 64)
        self.assertEqual(code, 409)
        self.assertIn('do not match', result['error'])

    def test_bad_ids_and_indexes_rejected(self):
        for upload_id in ('short', 'G' * 32, '../x'):
            code, _ = self.post('/api/spreadsheets/upload/part', {'upload_id': upload_id, 'index': 0, 'content_base64': 'YQ=='})
            self.assertEqual(code, 409, upload_id)
        code, _ = self.post('/api/spreadsheets/upload/part', {'upload_id': 'c' * 32, 'index': -1, 'content_base64': 'YQ=='})
        self.assertEqual(code, 409)
        code, _ = self.post('/api/spreadsheets/upload/part', {'upload_id': 'c' * 32, 'index': 0, 'content_base64': ''})
        self.assertEqual(code, 409)

    def test_part_route_accepts_a_full_three_mib_chunk(self):
        chunk = b'x' * (3 * 1024 * 1024)
        code, result = self.post('/api/spreadsheets/upload/part', {'upload_id': 'd' * 32, 'index': 0, 'content_base64': base64.b64encode(chunk).decode()})
        self.assertEqual(code, 200, result)
        self.assertEqual(result['part']['bytes'], len(chunk))


class FakeCursor:
    """Minimal Postgres stand-in for the two file tables."""
    def __init__(self):
        self.files = {}
        self.parts = {}
        self.rows = []

    def execute(self, sql, params=()):
        self.rows = []
        if sql.startswith('SELECT 1 FROM ci_verifier_files'):
            self.rows = [(1,)] if params[0] in self.files else []
        elif sql.startswith('SELECT content, sha256 FROM ci_verifier_files'):
            row = self.files.get(params[0]); self.rows = [(row['content'], row['sha256'])] if row else []
        elif sql.startswith('INSERT INTO ci_verifier_files'):
            path, sha256, size, content = params
            self.files.setdefault(path, {'sha256': sha256, 'size': size, 'content': content})
        elif sql.startswith('SELECT sha256 FROM ci_verifier_files'):
            row = self.files.get(params[0]); self.rows = [(row['sha256'],)] if row else []
        elif sql.startswith('INSERT INTO ci_verifier_upload_parts'):
            self.parts[(params[0], params[1])] = params[2]
        elif sql.startswith('SELECT content FROM ci_verifier_upload_parts'):
            self.rows = [(v,) for (u, i), v in sorted(self.parts.items()) if u == params[0]]
        elif sql.startswith('DELETE FROM ci_verifier_upload_parts'):
            self.parts = {k: v for k, v in self.parts.items() if k[0] != params[0]}
        else:
            raise AssertionError(sql)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


class PostgresFilesTests(unittest.TestCase):
    def test_put_get_is_write_once(self):
        store = PostgresFiles(FakeCursor())
        store.put('uploads/abc.csv', b'one')
        store.put('uploads/abc.csv', b'one')  # idempotent
        self.assertEqual(store.get('uploads/abc.csv'), b'one')
        with self.assertRaises(ValueError):
            store.put('uploads/abc.csv', b'two')
        self.assertTrue(store.exists('uploads/abc.csv'))
        self.assertFalse(store.exists('uploads/nope.csv'))

    def test_integrity_and_paths(self):
        cur = FakeCursor(); store = PostgresFiles(cur)
        store.put('uploads/x.xlsx', b'data')
        cur.files['uploads/x.xlsx']['content'] = b'tampered'
        with self.assertRaises(StorageUnavailable):
            store.get('uploads/x.xlsx')
        for bad in ('../x', 'uploads/../x', 'other/x', 'uploads/a/b', '/uploads/x', 'uploads/sp ace'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                store.put(bad, b'x')

    def test_parts_round_trip_and_cleanup(self):
        store = PostgresFiles(FakeCursor())
        store.put_part('e' * 32, 1, b'B'); store.put_part('e' * 32, 0, b'A'); store.put_part('f' * 32, 0, b'other')
        self.assertEqual(store.take_parts('e' * 32), [b'A', b'B'])
        self.assertEqual(store.take_parts('e' * 32), [])
        self.assertEqual(store.take_parts('f' * 32), [b'other'])
        with self.assertRaises(ValueError):
            store.put_part('bad id', 0, b'x')

    def test_activate_switches_store_for_request(self):
        cur = FakeCursor()
        with activate(PostgresFiles(cur)):
            self.assertIsInstance(files_for('/nonexistent'), PostgresFiles)
        self.assertIsInstance(files_for('/nonexistent'), LocalFiles)


if __name__ == '__main__':
    unittest.main()
