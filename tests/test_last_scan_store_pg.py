"""The same durability scenarios on a REAL PostgreSQL server, never a fake.

Runs only against a disposable cluster started by scripts/last-scan-pg-test.sh, which sets LAST_SCAN_TEST_PG_HOST
(a unix-socket directory inside the worktree) and LAST_SCAN_TEST_PG_PORT. Without them every test here is SKIPPED,
and a skip means hosted concurrency is UNVERIFIED, not passed. It never connects to any existing database.
"""
import os
import threading
import unittest
from uuid import uuid4
import last_scan_scenarios as sc
from hosting.last_scan_store import PostgresStore

HOST, PORT = os.environ.get('LAST_SCAN_TEST_PG_HOST'), os.environ.get('LAST_SCAN_TEST_PG_PORT')


@unittest.skipUnless(HOST and PORT and HOST.startswith('/') and '/.tmp/' in HOST,
                     'No disposable PostgreSQL cluster: hosted concurrency UNVERIFIED (run scripts/last-scan-pg-test.sh).')
class RealPostgres(unittest.TestCase):
    def connect(self):
        import psycopg
        return psycopg.connect(host=HOST, port=int(PORT), dbname='postgres', autocommit=False, connect_timeout=5)

    def fresh(self, **kw):
        schema = 't_' + uuid4().hex[:16]
        conn = self.connect(); conn.execute('CREATE SCHEMA ' + schema); conn.commit(); conn.close()
        store = PostgresStore(self.connect, schema=schema, **kw)
        store.install(); store.install()              # explicit and idempotent
        return store

    def test_server_is_the_disposable_one(self):
        conn = self.connect()
        version, directory = conn.execute("SELECT current_setting('server_version'), current_setting('data_directory')").fetchone()
        conn.close()
        self.assertIn('/.tmp/pg/', directory)
        print('\n[real PostgreSQL %s at %s]' % (version, directory))

    def test_shared_scenarios(self):
        for scenario in sc.ALL:
            with self.subTest(scenario.__name__):
                scenario(self.fresh())

    def test_nothing_is_created_without_install(self):
        import psycopg
        schema = 't_' + uuid4().hex[:16]
        conn = self.connect(); conn.execute('CREATE SCHEMA ' + schema); conn.commit(); conn.close()
        with self.assertRaises(psycopg.errors.UndefinedTable):
            with PostgresStore(self.connect, schema=schema).transaction('k') as r:
                r['x'] = 1
        with self.assertRaises(ValueError):
            PostgresStore(self.connect, schema='bad; drop')

    def test_row_lock_held_by_another_session_times_out_then_recovers(self):
        import psycopg
        store = self.fresh(lock_timeout='400ms')
        with store.transaction('k') as r:
            r['n'] = 1
        holder = self.connect()
        holder.execute('SELECT value FROM %sscan_draft_state WHERE key=%%s FOR UPDATE' % store.prefix, ('k',))
        with self.assertRaises(psycopg.errors.LockNotAvailable):
            with store.transaction('k') as r:
                r['n'] = 2
        holder.close()                                # the holder dies without committing: its lock goes with it
        with store.transaction('k') as r:
            self.assertEqual(r['n'], 1); r['n'] = 3
        with store.transaction('k') as r:
            self.assertEqual(r['n'], 3)

    def test_second_writer_waits_and_sees_the_first_commit(self):
        store = self.fresh(); order = []; inside = threading.Event(); go = threading.Event()
        def first():
            with store.atomic(['lease:x', 'state']) as r:
                inside.set(); go.wait(5)
                r['state']['owner'] = 'first'; order.append('first commits')
        def second():
            inside.wait(5)
            with store.atomic(['lease:x', 'state']) as r:
                order.append('second sees ' + str(r['state'].get('owner')))
        a, b = threading.Thread(target=first), threading.Thread(target=second)
        a.start(); b.start(); inside.wait(5)
        import time; time.sleep(0.3); go.set(); a.join(10); b.join(10)
        self.assertEqual(order, ['first commits', 'second sees first'])

    def test_killed_backend_commits_nothing(self):
        import psycopg
        store = self.fresh()
        with store.atomic(['a', 'b']) as r:
            r['a']['n'] = 1; r['b']['n'] = 1
        admin = self.connect(); admin.autocommit = True
        with self.assertRaises(psycopg.Error):
            with store.atomic(['a', 'b']) as r:
                r['a']['n'] = 2; r['b']['n'] = 2
                admin.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE pid <> pg_backend_pid() AND state = 'idle in transaction'")
        admin.close()
        with store.atomic(['a', 'b']) as r:
            self.assertEqual((r['a'], r['b']), ({'n': 1}, {'n': 1}))


import test_last_scan_durable as durable


class _OnPostgres:
    """Run a SQLite test class unchanged against the disposable PostgreSQL server."""
    def make_store(self):
        import psycopg
        connect = lambda: psycopg.connect(host=HOST, port=int(PORT), dbname='postgres', autocommit=False, connect_timeout=5)
        if not getattr(self, 'schema', None):
            self.schema = 't_' + uuid4().hex[:16]
            conn = connect(); conn.execute('CREATE SCHEMA ' + self.schema); conn.commit(); conn.close()
            PostgresStore(connect, schema=self.schema).install()
        return PostgresStore(connect, schema=self.schema)


SKIP = unittest.skipUnless(HOST and PORT and HOST.startswith('/') and '/.tmp/' in HOST,
                           'No disposable PostgreSQL cluster: hosted concurrency UNVERIFIED (run scripts/last-scan-pg-test.sh).')


@SKIP
class AttemptFencingOnPostgres(_OnPostgres, durable.AttemptFencing):
    pass


@SKIP
class ReadinessGateOnPostgres(_OnPostgres, durable.ReadinessGate):
    pass


@SKIP
class CrashRestartOnPostgres(_OnPostgres, durable.CrashRestart):
    pass


@SKIP
class StoreReadiness(RealPostgres):
    def test_ready_is_read_only_and_reports_missing_tables(self):
        schema = 't_' + uuid4().hex[:16]
        conn = self.connect(); conn.execute('CREATE SCHEMA ' + schema); conn.commit(); conn.close()
        store = PostgresStore(self.connect, schema=schema)
        self.assertFalse(store.ready())
        conn = self.connect()
        tables = conn.execute('SELECT count(*) FROM information_schema.tables WHERE table_schema=%s', (schema,)).fetchone()[0]
        conn.close()
        self.assertEqual(tables, 0)                     # checking created nothing
        store.install(); self.assertTrue(store.ready())

    test_server_is_the_disposable_one = None; test_shared_scenarios = None; test_nothing_is_created_without_install = None
    test_row_lock_held_by_another_session_times_out_then_recovers = None; test_second_writer_waits_and_sees_the_first_commit = None
    test_killed_backend_commits_nothing = None


if __name__ == '__main__':
    unittest.main()
