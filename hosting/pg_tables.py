"""The spreadsheet inbox on real Supabase (Postgres) tables.

SpreadsheetInbox was written for SQLite. On Vercel it now runs the same statements against the tables in the
ci_verifier schema: `?` placeholders become `%s`, SQLite-only statements (PRAGMA, BEGIN IMMEDIATE, CREATE ... IF NOT
EXISTS, which the Supabase migration already did) are skipped, and INSERT OR IGNORE becomes ON CONFLICT DO NOTHING.
Each `with inbox.connect()` block is its own transaction (nested blocks use savepoints), as with SQLite, so there is
no workspace-wide lock: several people can work at once.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import json
import re
import sqlite3

current = ContextVar('ci_verifier_pg', default=None)
SKIP = re.compile(r'^\s*(PRAGMA|BEGIN|CREATE\s+(TABLE|INDEX|UNIQUE\s+INDEX)\s+IF\s+NOT\s+EXISTS)\b', re.I)
TABLES = ('spreadsheets', 'spreadsheet_checks', 'spreadsheet_sources', 'client_contacts', 'settings',
          'vin_index', 'vin_indexed', 'dashboards', 'api_keys')
MIGRATED = '_migrated_from_snapshot'


def translate(sql):
    if SKIP.match(sql):
        return None
    out = sql.replace('%', '%%')
    ignore = re.match(r'^\s*INSERT\s+OR\s+IGNORE\s+INTO\b', out, re.I)
    if ignore:
        out = re.sub(r'^\s*INSERT\s+OR\s+IGNORE\s+INTO\b', 'INSERT INTO', out, flags=re.I) + ' ON CONFLICT DO NOTHING'
    return out.replace('?', '%s')


class Row(tuple):
    """Like sqlite3.Row: row[0], row['name'], dict(row)."""
    def __new__(cls, values, names):
        r = super().__new__(cls, values)
        r._names = names
        return r

    def __getitem__(self, key):
        if isinstance(key, str):
            return tuple.__getitem__(self, self._names.index(key))
        return tuple.__getitem__(self, key)

    def keys(self):
        return list(self._names)


class Result:
    def __init__(self, cur=None):
        self.cur = cur
        self.rowcount = cur.rowcount if cur is not None else 0

    def _row(self, values):
        names = [d.name for d in self.cur.description]
        return Row([bytes(v) if isinstance(v, memoryview) else v for v in values], names)

    def fetchone(self):
        if self.cur is None or self.cur.description is None:
            return None
        v = self.cur.fetchone()
        return None if v is None else self._row(v)

    def fetchall(self):
        if self.cur is None or self.cur.description is None:
            return []
        return [self._row(v) for v in self.cur.fetchall()]

    def __iter__(self):
        return iter(self.fetchall())


class PgDB:
    """What `with inbox.connect() as db` hands the inbox code in Postgres mode."""
    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, params=()):
        q = translate(sql)
        if q is None:
            return Result()
        cur = self.conn.cursor()
        cur.execute(q, tuple(params))
        return Result(cur)

    def executemany(self, sql, seq):
        q = translate(sql)
        if q is None:
            return Result()
        cur = self.conn.cursor()
        cur.executemany(q, [tuple(p) for p in seq])
        return Result(cur)


class PgSession:
    def __init__(self, conn):
        self.conn = conn
        self.depth = 0
        self.db = PgDB(conn)

    @contextmanager
    def transaction(self):
        """Outermost block commits (or rolls back); nested blocks are savepoints."""
        if self.depth == 0:
            self.depth = 1
            try:
                yield self.db
                self.conn.commit()
            except BaseException:
                self.conn.rollback()
                raise
            finally:
                self.depth = 0
            return
        name = f'sp{self.depth}'
        self.depth += 1
        self.conn.execute(f'SAVEPOINT {name}')
        try:
            yield self.db
            self.conn.execute(f'RELEASE SAVEPOINT {name}')
        except BaseException:
            self.conn.execute(f'ROLLBACK TO SAVEPOINT {name}')
            raise
        finally:
            self.depth -= 1


def migrate_snapshot(conn, sqlite_path):
    """Copy the inbox out of the old packed snapshot into the tables, once. Safe if two requests race."""
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(hashtext('ci_verifier_snapshot_migration'))")
        cur.execute('SELECT 1 FROM settings WHERE key=%s', (MIGRATED,))
        if cur.fetchone():
            conn.commit()
            return False
        src = sqlite3.connect(str(sqlite_path))
        try:
            have = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            copied = {}
            for table in TABLES:
                if table not in have:
                    continue
                cols = [r[1] for r in src.execute(f'PRAGMA table_info({table})')]
                rows = src.execute(f'SELECT {", ".join(cols)} FROM {table}' + (' ORDER BY seq' if 'seq' in cols else '')).fetchall()
                if not rows:
                    continue
                names = ', '.join('"row"' if c == 'row' else c for c in cols)
                cur.executemany(f'INSERT INTO {table} ({names}) VALUES ({", ".join(["%s"] * len(cols))}) ON CONFLICT DO NOTHING', rows)
                copied[table] = len(rows)
            for table in ('spreadsheets', 'spreadsheet_checks'):   # keep the sequences ahead of the copied seq numbers
                cur.execute(f"SELECT setval(pg_get_serial_sequence('ci_verifier.{table}', 'seq'), GREATEST((SELECT COALESCE(MAX(seq), 0) FROM {table}), 1))")
        finally:
            src.close()
        cur.execute('INSERT INTO settings (key, value_json) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING', (MIGRATED, json.dumps(copied)))
    conn.commit()
    return True
