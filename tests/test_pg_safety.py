"""The Postgres tests refuse anything that could be a live database (tests/pg_safety.py)."""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
from pg_safety import NotDisposable, assert_disposable, check_url  # noqa: E402

URL = os.environ.get('CI_PG_TEST_URL')


class RefusesHostedAndRemote(unittest.TestCase):

    def test_hosted_providers_are_refused_even_with_the_override(self):
        with patch.dict(os.environ, CI_PG_TEST_ALLOW_REMOTE='1'):
            for url in ('postgresql://postgres.abc:x@aws-0-us-east-1.pooler.supabase.com:6543/postgres',
                        'postgresql://postgres:x@db.pzveyaatkfsruhtxondj.supabase.co:5432/postgres',
                        'postgresql://u:x@ep-1.us-east-2.aws.neon.tech/db'):
                with self.assertRaises(NotDisposable, msg=url):
                    check_url(url)

    def test_other_remote_hosts_need_an_explicit_override(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('CI_PG_TEST_ALLOW_REMOTE', None)
            with self.assertRaises(NotDisposable):
                check_url('postgresql://u:x@10.0.0.5:5432/db')

    def test_local_socket_and_localhost_pass_the_address_check(self):
        for url in ('postgresql://ci_verifier:test@/postgres?host=/var/tmp/pgtest&port=55432',
                    'postgresql://u:x@localhost:5432/db', 'postgresql://u:x@127.0.0.1/db'):
            check_url(url)

    @unittest.skipUnless(URL, 'set CI_PG_TEST_URL to run against the disposable Postgres')
    def test_a_local_database_without_the_marker_is_refused(self):
        import psycopg
        admin = URL.replace('ci_verifier:test@', 'postgres@')
        with psycopg.connect(admin, autocommit=True) as c:
            c.execute('DROP DATABASE IF EXISTS unmarked_check'); c.execute('CREATE DATABASE unmarked_check')
        try:
            with self.assertRaises(NotDisposable):
                assert_disposable(admin.replace('/postgres?', '/unmarked_check?'))
            self.assertEqual(assert_disposable(URL)[0], 'postgres')
        finally:
            with psycopg.connect(admin, autocommit=True) as c:
                c.execute('DROP DATABASE unmarked_check')


if __name__ == '__main__':
    unittest.main()
