"""The inbox on real Postgres tables (as on Supabase), including the one-time copy out of the old snapshot.

Needs a Postgres with the ci_verifier schema (supabase/ci_verifier_native_tables.sql); set CI_PG_TEST_URL to run, e.g.
postgresql://ci_verifier:test@/postgres?host=/var/tmp/pgtest&port=55432
"""
import base64, json, os, sys, threading, unittest, urllib.error, urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
from eos_fixture import build, ROWS

URL = os.environ.get('CI_PG_TEST_URL')


@unittest.skipUnless(URL, 'set CI_PG_TEST_URL to run against a real Postgres')
class NativeTables(unittest.TestCase):
    def setUp(self):
        import psycopg
        admin = URL.replace('ci_verifier:test@', 'postgres@')
        with psycopg.connect(admin, autocommit=True) as c:
            c.execute('TRUNCATE ci_verifier.ci_verifier_state, ci_verifier.ci_verifier_files, ci_verifier.ci_verifier_upload_parts, '
                      'ci_verifier.spreadsheets, ci_verifier.spreadsheet_checks, ci_verifier.spreadsheet_sources, ci_verifier.client_contacts, '
                      'ci_verifier.settings, ci_verifier.vin_index, ci_verifier.vin_indexed, ci_verifier.dashboards, ci_verifier.api_keys RESTART IDENTITY')
        self.env = patch.dict(os.environ, {'CI_LOCAL_DEV': '1', 'VERCEL': '0'}); self.env.start()
        for k in ('MS_TENANT_ID', 'MS_CLIENT_ID', 'MS_CLIENT_SECRET', 'ANTHROPIC_API_KEY'):
            os.environ.pop(k, None)
        from api.index import handler
        from hosting.postgres_storage import PostgresWorkspace
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        self.server.workspace_store = PostgresWorkspace(URL)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.env.stop()

    def call(self, path, body=None):
        req = urllib.request.Request(self.base + path, data=None if body is None else json.dumps(body).encode(),
                                     headers={'Content-Type': 'application/json', 'Origin': self.base, 'X-Review-Token': 'same-origin'})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def upload(self, day, rows=ROWS, name=None):
        return self.call('/api/spreadsheets/import', {'filename': name or f'2230-OWENS-BASCO - EOS - {day}.xlsx',
                                                      'content_base64': base64.b64encode(build([tuple(r) for r in rows], day=day)).decode()})

    def test_copy_from_snapshot_then_tables(self):
        import psycopg
        with patch.dict(os.environ, {'CI_NATIVE_TABLES': '0'}):           # the old way: everything in the snapshot
            status, old = self.upload('2026-10-06')
            self.assertEqual(status, 200, old)
        with urllib.request.urlopen(self.base + '/api/spreadsheets', timeout=30) as r:   # first request on the tables copies it over
            listed = json.loads(r.read())['spreadsheets']
        self.assertEqual([x['file_hash'] for x in listed], [old['item']['file_hash']])
        with psycopg.connect(URL) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM spreadsheets').fetchone()[0], 1)
            self.assertTrue(c.execute("SELECT 1 FROM settings WHERE key='_migrated_from_snapshot'").fetchone())
        # new work lands in the tables
        status, new = self.upload('2026-10-07', [tuple(r) for r in ROWS[:3]] + [tuple(r) for r in ROWS[4:]])
        self.assertEqual(status, 200, new)
        h = new['item']['file_hash']
        with urllib.request.urlopen(self.base + '/api/spreadsheet?id=' + h, timeout=30) as r:
            item = json.loads(r.read())
        self.assertEqual(item['file_hash'], h)
        self.assertTrue(item['other_days'])                                   # VIN index works across the copied and new reports
        status, a = self.call('/api/spreadsheets/approve', {'file_hash': h, 'by': 'Lauren', 'note': ''})
        self.assertIn(status, (200, 409), a)
        self.assertEqual(self.call('/api/settings/report-header', {'job': '2230-OWENS-BASCO', 'header': {'customer': 'Owens'}})[1]['header']['customer'], 'Owens')
        status, hits = self.call('/api/vins/search', {'q': ROWS[0][0]})
        self.assertEqual(status, 200, hits)
        with psycopg.connect(URL) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM spreadsheets').fetchone()[0], 2)
            self.assertGreater(c.execute('SELECT count(*) FROM vin_index').fetchone()[0], 10)
            self.assertEqual(c.execute('SELECT count(*) FROM reports_overview').fetchone()[0], 2)

    def test_parallel_requests_do_not_block_each_other(self):
        status, first = self.upload('2026-10-07')
        self.assertEqual(status, 200, first)
        h = first['item']['file_hash']
        def get(_):
            with urllib.request.urlopen(self.base + '/api/spreadsheet?id=' + h, timeout=60) as r:
                return r.status
        with ThreadPoolExecutor(8) as pool:
            results = list(pool.map(get, range(16)))
            uploads = list(pool.map(lambda d: self.upload(d)[0], ['2026-10-0' + str(i) for i in range(1, 6)]))
        self.assertEqual(set(results), {200}); self.assertEqual(set(uploads), {200})


if __name__ == '__main__':
    unittest.main()
