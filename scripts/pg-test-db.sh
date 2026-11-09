#!/usr/bin/env bash
# A throw-away PostgreSQL for the two native-table tests (and the decision tests that need real Postgres).
#
#   bash scripts/pg-test-db.sh start    # new empty cluster in /var/tmp/pgtest, Unix socket only, port 55432
#   bash scripts/pg-test-db.sh env      # prints the export lines for the tests
#   bash scripts/pg-test-db.sh stop     # stops it and deletes the whole folder
#
# Nothing here connects to Supabase, Vercel or any other server: the cluster listens on no TCP address at all
# (listen_addresses=''), only on a socket file inside /var/tmp/pgtest, and it is marked as disposable with a table
# the tests insist on before they write anything (see tests/pg_safety.py).
set -euo pipefail
DIR=${PGTEST_DIR:-/var/tmp/pgtest}
PORT=${PGTEST_PORT:-55432}
BIN=$(ls -d /usr/lib/postgresql/*/bin 2>/dev/null | sort -V | tail -1)
[[ -x "$BIN/initdb" ]] || BIN=$(dirname "$(command -v initdb)")
REPO=$(cd "$(dirname "$0")/.." && pwd)
as_pg() { if [[ $(id -u) == 0 ]]; then su postgres -s /bin/bash -c "$*"; else bash -c "$*"; fi; }   # initdb refuses to run as root

case "${1:-}" in
  start)
    [[ -e "$DIR" ]] && { echo "$DIR already exists. Run: $0 stop"; exit 1; }
    mkdir -p "$DIR"; [[ $(id -u) == 0 ]] && chown postgres "$DIR"
    as_pg "'$BIN/initdb' -D '$DIR/data' -U postgres --auth=trust >/dev/null"
    as_pg "'$BIN/pg_ctl' -D '$DIR/data' -l '$DIR/log' -o \"-k $DIR -p $PORT -c listen_addresses=''\" start >/dev/null"
    PSQL="env PGOPTIONS=-cclient_min_messages=warning $BIN/psql -h $DIR -p $PORT -U postgres -d postgres -v ON_ERROR_STOP=1 -q"
    $PSQL -c "create role ci_verifier login password 'test'" -c "create schema ci_verifier authorization ci_verifier" \
          -c "alter role ci_verifier set search_path = ci_verifier" -c "alter role postgres set search_path = ci_verifier"
    { echo 'set search_path = ci_verifier;'; cat "$REPO/hosting/schema.sql" "$REPO/supabase/ci_verifier_native_tables.sql" "$REPO/supabase/ci_verifier_access.sql"; } | $PSQL -f -
    $PSQL -c "grant all on all tables in schema ci_verifier to ci_verifier" -c "grant all on all sequences in schema ci_verifier to ci_verifier" \
          -c "create table ci_verifier.disposable_test_database (created_at timestamptz not null default now(), note text)" \
          -c "insert into ci_verifier.disposable_test_database (note) values ('Created by scripts/pg-test-db.sh; safe to wipe.')" \
          -c "grant select on ci_verifier.disposable_test_database to ci_verifier"
    echo "Disposable Postgres is running on socket $DIR, port $PORT (no TCP)."
    "$0" env ;;
  env)
    echo "export CI_PG_TEST_URL='postgresql://ci_verifier:test@/postgres?host=$DIR&port=$PORT'" ;;
  stop)
    [[ -d "$DIR/data" ]] && as_pg "'$BIN/pg_ctl' -D '$DIR/data' stop -m fast >/dev/null" || true
    rm -rf "$DIR"; echo "Stopped and deleted $DIR." ;;
  *) echo "Usage: $0 start|env|stop"; exit 2 ;;
esac
