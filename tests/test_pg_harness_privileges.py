"""The disposable test database keeps a realistic, limited application role.

Tests that need a clean database empty it through tests/pg_safety.wipe (the throw-away cluster's administrator).
The application role itself must stay unable to do that, so a test can never pass only because it was over-privileged.
Runs only against the database started by scripts/pg-test-db.sh; skipped (and reported as skipped) otherwise.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from pg_safety import APP_TABLES, NotDisposable, admin_url, assert_disposable, wipe  # noqa: E402

URL = os.environ.get('CI_PG_TEST_URL')


class AdminUrl(unittest.TestCase):
    def test_only_the_script_printed_url_has_an_assumed_administrator(self):
        self.assertEqual(admin_url('postgresql://ci_verifier:test@/postgres?host=/var/tmp/pgtest&port=55432'),
                         'postgresql://postgres@/postgres?host=/var/tmp/pgtest&port=55432')
        for other in ('postgresql://someone:secret@/postgres?host=/var/tmp/pgtest', 'postgresql://ci_verifier:other@localhost/postgres'):
            with self.assertRaises(NotDisposable):
                admin_url(other)

    def test_wipe_refuses_hosted_and_remote_targets_before_connecting(self):
        for url in ('postgresql://ci_verifier:test@db.example.supabase.co:5432/postgres', 'postgresql://ci_verifier:test@10.0.0.5:5432/db'):
            with self.assertRaises(NotDisposable):
                wipe(url)


@unittest.skipUnless(URL, 'set CI_PG_TEST_URL to the disposable Postgres from scripts/pg-test-db.sh')
class ApplicationRoleStaysLimited(unittest.TestCase):
    def setUp(self):
        import psycopg
        self.psycopg = psycopg
        wipe(URL)
        self.app = psycopg.connect(URL, autocommit=True, connect_timeout=5); self.addCleanup(self.app.close)

    def one(self, sql, *args):
        return self.app.execute(sql, args).fetchone()[0]

    def refused(self, sql):
        with self.assertRaises(self.psycopg.errors.InsufficientPrivilege, msg=sql):
            self.app.execute(sql)

    def test_role_is_not_superuser_and_owns_no_application_table_or_sequence(self):
        self.assertEqual(self.one('SELECT current_user'), 'ci_verifier')
        flags = self.app.execute('SELECT rolsuper, rolcreaterole, rolcreatedb, rolbypassrls, rolreplication FROM pg_roles WHERE rolname = current_user').fetchone()
        self.assertEqual(flags, (False, False, False, False, False))
        owned = self.one("SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                         "WHERE n.nspname = 'ci_verifier' AND c.relkind IN ('r', 'S') AND pg_get_userbyid(c.relowner) = current_user")
        self.assertEqual(owned, 0)
        self.assertEqual(self.one("SELECT pg_get_userbyid(relowner) FROM pg_class WHERE relname = 'spreadsheets_seq_seq'"), 'postgres')

    def test_role_cannot_restart_counters_or_change_structure(self):
        self.refused('TRUNCATE ci_verifier.spreadsheets RESTART IDENTITY')        # the exact operation the PDF tests used to attempt
        self.refused('ALTER SEQUENCE ci_verifier.spreadsheets_seq_seq RESTART')
        self.refused('ALTER TABLE ci_verifier.spreadsheets ADD COLUMN x int')
        self.refused('CREATE ROLE someone')
        self.refused("ALTER ROLE ci_verifier SUPERUSER")
        self.refused('DELETE FROM ci_verifier.disposable_test_database')         # the safety marker is read-only to it

    def test_known_gap_the_role_owns_the_schema_in_this_harness(self):
        # scripts/pg-test-db.sh creates the schema "authorization ci_verifier". A schema owner may drop objects in
        # its schema even when it does not own them, so in THIS harness the role is not fully least-privilege:
        # DROP TABLE is stopped only by a dependent view, not by a permission check. Recorded, not hidden.
        # Whether the hosted database is set up the same way must be confirmed by its owner.
        owner = self.one("SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = 'ci_verifier'")
        self.assertEqual(owner, 'ci_verifier')
        with self.assertRaises(self.psycopg.errors.DependentObjectsStillExist):
            self.app.execute('DROP TABLE ci_verifier.spreadsheets')
        self.assertEqual(self.one("SELECT count(*) FROM pg_class WHERE relname = 'spreadsheets' AND relkind = 'r'"), 1)

    def test_role_can_still_do_ordinary_application_work(self):
        self.assertTrue(self.one("SELECT has_table_privilege(current_user, 'ci_verifier.spreadsheets', 'INSERT, SELECT, UPDATE, DELETE')"))
        self.assertTrue(self.one("SELECT has_sequence_privilege(current_user, 'ci_verifier.spreadsheets_seq_seq', 'USAGE')"))
        self.assertGreaterEqual(self.one("SELECT nextval('ci_verifier.spreadsheets_seq_seq')"), 1)

    def test_wipe_empties_tables_restarts_counters_and_keeps_the_safety_marker(self):
        self.assertGreaterEqual(self.one("SELECT nextval('ci_verifier.spreadsheets_seq_seq')"), 1)
        self.one("SELECT nextval('ci_verifier.spreadsheets_seq_seq')")
        wipe(URL)
        self.assertEqual(self.one("SELECT nextval('ci_verifier.spreadsheets_seq_seq')"), 1)
        for table in APP_TABLES:
            self.assertEqual(self.one('SELECT count(*) FROM ci_verifier.' + table), 0, table)
        self.assertGreaterEqual(self.one('SELECT count(*) FROM ci_verifier.disposable_test_database'), 1)
        assert_disposable(URL)

    def test_wipe_granted_nothing_to_the_application_role(self):
        before = self.app.execute("SELECT table_name, privilege_type FROM information_schema.role_table_grants WHERE grantee = 'ci_verifier' ORDER BY 1, 2").fetchall()
        wipe(URL); wipe(URL)
        after = self.app.execute("SELECT table_name, privilege_type FROM information_schema.role_table_grants WHERE grantee = 'ci_verifier' ORDER BY 1, 2").fetchall()
        self.assertEqual(before, after)
        self.refused('TRUNCATE ci_verifier.spreadsheets RESTART IDENTITY')


if __name__ == '__main__':
    unittest.main()
