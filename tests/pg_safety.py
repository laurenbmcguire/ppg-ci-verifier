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


APP_TABLES = ('ci_verifier_state', 'ci_verifier_files', 'ci_verifier_upload_parts', 'spreadsheets', 'spreadsheet_checks',
              'spreadsheet_sources', 'client_contacts', 'settings', 'vin_index', 'vin_indexed', 'dashboards', 'api_keys')


def admin_url(url):
    """The throw-away cluster's own administrator, on the same local socket. Only for emptying it between tests."""
    if 'ci_verifier:test@' not in url:
        raise NotDisposable('Refusing: this is not the URL scripts/pg-test-db.sh prints, so no administrator is assumed.')
    return url.replace('ci_verifier:test@', 'postgres@')


def wipe(url):
    """Empty the application tables of the disposable database and restart their counters.

    Restarting a counter needs ownership of its sequence. The application role (ci_verifier) deliberately does not
    own the tables or sequences, as in a real deployment, so the wipe is done by the throw-away cluster's
    administrator and the code under test keeps running as the limited role. Nothing is granted to that role."""
    import psycopg
    assert_disposable(url)
    admin = admin_url(url)
    assert_disposable(admin)
    with psycopg.connect(admin, autocommit=True, connect_timeout=5) as c:
        c.execute('TRUNCATE ' + ', '.join('ci_verifier.' + t for t in APP_TABLES) + ' RESTART IDENTITY')
