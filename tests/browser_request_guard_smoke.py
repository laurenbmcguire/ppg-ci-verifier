"""Real Chromium, synthetic in-memory workspace, loopback only; no dependencies installed.

Use an already installed playwright-core Node module and its default browser, or set
PLAYWRIGHT_MODULE to the installed module path/name and CHROMIUM_EXECUTABLE to an
existing Chromium executable. Run this script with the project's test Python.
"""
import json, os, subprocess, sys, threading
from pathlib import Path
from http.server import ThreadingHTTPServer
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
# Erase inherited service credentials before loading application code.
for key in list(os.environ):
    if key.startswith(('MS_', 'ANTHROPIC_', 'OPENAI_', 'VERCEL', 'DATABASE_', 'PG')):
        os.environ.pop(key, None)
os.environ.update(CI_LOCAL_DEV='1', VERCEL='0')
from api.index import handler
from legacy_storage_fake import PostgresWorkspace
from test_vercel import FakeDatabase
from eos_fixture import build, ROWS
from hosting.spreadsheet_inbox import SpreadsheetInbox
store = PostgresWorkspace('postgresql://test.invalid/synthetic', FakeDatabase().connect)
rows = [list(r) for r in ROWS]; rows[2][4] = 'Holes /  Glue '
with store.session() as directory:
    box = SpreadsheetInbox(directory)
    report = box.ingest('2230-OWENS-BASCO - EOS - 10.07.26.xlsx', build([tuple(r) for r in rows]))
    before = box.get(report['file_hash'])
requests = []
class LocalHandler(handler):
    def do_POST(self):
        requests.append(self.path)
        if self.path not in ('/api/spreadsheets/fixes', '/api/spreadsheets/sheet', '/api/spreadsheets/cells', '/api/spreadsheets/findings', '/api/clients'):
            return self.fail(409, 'Synthetic smoke forbids mutation or provider actions.')
        return super().do_POST()
server = ThreadingHTTPServer(('127.0.0.1', 0), LocalHandler); server.workspace_store = store
threading.Thread(target=server.serve_forever, daemon=True).start()
try:
    result = subprocess.run(['node', str(ROOT / 'tests/browser_request_guard_smoke.js'),
        f'http://127.0.0.1:{server.server_port}', report['file_hash']], text=True, capture_output=True,
        env={k: os.environ[k] for k in ('HOME', 'PATH', 'LANG', 'PLAYWRIGHT_MODULE', 'CHROMIUM_EXECUTABLE') if k in os.environ}, timeout=70)
    print(result.stdout); print(result.stderr, file=sys.stderr)
    with store.session() as directory:
        after = SpreadsheetInbox(directory).get(report['file_hash'])
    assert before == after, 'Opening or navigation mutated report'
    forbidden = [p for p in requests if p not in ('/api/spreadsheets/fixes', '/api/spreadsheets/sheet', '/api/spreadsheets/cells', '/api/spreadsheets/findings', '/api/clients')]
    assert not forbidden, forbidden
    assert result.returncode == 0, result.returncode
    print(json.dumps({'passed': True, 'report_unchanged': True, 'post_requests': requests}))
finally:
    server.shutdown(); server.server_close()
