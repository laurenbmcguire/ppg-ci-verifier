#!/bin/sh
# Real-PostgreSQL proof for the last-scan runner, on a DISPOSABLE cluster that lives and dies inside this worktree.
# It creates its own data directory under .tmp/pg, listens ONLY on a unix socket in that folder (no TCP port), and is
# stopped and deleted on exit. It never touches the existing PostgreSQL service, its data, or any other database.
#   usage: PY=/path/to/python sh scripts/last-scan-pg-test.sh
set -eu
ROOT=$(cd "$(dirname "$0")/.." && pwd)
BIN=${PG_BIN:-/opt/homebrew/bin}
PY=${PY:-python3}
BASE="$ROOT/.tmp/pg"; DATA="$BASE/data"; SOCK="$BASE/sock"; PORT=54329
[ -e "$BASE" ] && { echo "refusing: $BASE already exists"; exit 2; }
mkdir -p "$SOCK"
cleanup() {
  "$BIN/pg_ctl" -D "$DATA" -m immediate stop >/dev/null 2>&1 || true
  rm -rf "$BASE"
}
trap cleanup EXIT INT TERM
"$BIN/initdb" -D "$DATA" -A trust -E UTF8 --no-sync >/dev/null
"$BIN/pg_ctl" -D "$DATA" -w -l "$BASE/server.log" \
  -o "-c listen_addresses='' -c unix_socket_directories='$SOCK' -c port=$PORT -c fsync=off -c max_connections=60" start >/dev/null
cd "$ROOT"
LAST_SCAN_TEST_PG_HOST="$SOCK" LAST_SCAN_TEST_PG_PORT=$PORT PYTHONPATH=.:tests TMPDIR="$ROOT/.tmp" PYTHONDONTWRITEBYTECODE=1 \
  "$PY" -m pytest -q -p no:cacheprovider -s -rs tests/test_last_scan_store_pg.py "$@"
