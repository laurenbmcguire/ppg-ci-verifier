"""Refuse to run the Postgres tests against anything but a throw-away database.

The native-table tests empty every ci_verifier table before they start. They only do it when the target:
  1. is reached through a local Unix socket, localhost, 127.0.0.1 or ::1 (never a hosted provider such as
     Supabase, unless CI_PG_TEST_ALLOW_REMOTE=1 is set for a separate, disposable hosted database), and
  2. contains the table ci_verifier.disposable_test_database with at least one row, which scripts/pg-test-db.sh
     creates and which no live verifier database has.
"""
import os
from urllib.parse import parse_qs, urlsplit

HOSTED = ('supabase.co', 'supabase.com', 'pooler.', 'neon.tech', 'amazonaws.com', 'azure.com', 'render.com', 'vercel')


class NotDisposable(AssertionError):
    pass


def check_url(url):
    parts = urlsplit(url)
    host = (parts.hostname or '') or (parse_qs(parts.query).get('host', [''])[0])
    if any(h in (parts.netloc + parts.query).lower() for h in HOSTED):
        raise NotDisposable(f'Refusing: {host} looks like a hosted database. Use scripts/pg-test-db.sh.')
    local = host.startswith('/') or host in ('localhost', '127.0.0.1', '::1')
    if not local and os.environ.get('CI_PG_TEST_ALLOW_REMOTE') != '1':
        raise NotDisposable(f'Refusing: {host or "(no host)"} is not a local socket or localhost.')
    return host


def assert_disposable(url):
    """Call before any write. Raises NotDisposable unless the database is marked as a throw-away test database."""
    import psycopg
    check_url(url)
    with psycopg.connect(url, connect_timeout=5) as c:
        marked = c.execute("SELECT to_regclass('ci_verifier.disposable_test_database') IS NOT NULL").fetchone()[0]
        if not marked or not c.execute('SELECT count(*) FROM ci_verifier.disposable_test_database').fetchone()[0]:
            raise NotDisposable('Refusing: ci_verifier.disposable_test_database is missing or empty, so this may be a real database.')
        return c.execute('SELECT current_database(), inet_server_addr(), current_setting(\'port\')').fetchone()
