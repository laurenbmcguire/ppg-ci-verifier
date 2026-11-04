"""Apply hosting/schema.sql to the Postgres database named by DATABASE_URL (run locally, once).

Not deployed to Vercel (.vercelignore). Use a least-privilege role; the URL is never printed.
"""
import os
from pathlib import Path
import sys


def main():
    url = os.environ.get('DATABASE_URL')
    if not url:
        raise SystemExit('Set DATABASE_URL in this shell (never paste it into chat or commit it).')
    import psycopg
    sql = (Path(__file__).resolve().parent / 'schema.sql').read_text()
    with psycopg.connect(url, connect_timeout=10) as conn:
        conn.execute(sql)
    print('Schema applied (idempotent).')


if __name__ == '__main__':
    sys.exit(main())
