"""Full app, synthetic inventory, fake storage, no provider or live report access."""
import hashlib, json, os, socket, subprocess, sys, threading
from pathlib import Path
from http.server import ThreadingHTTPServer
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
for key in list(os.environ):
    if key.startswith(('MS_', 'ANTHROPIC_', 'OPENAI_', 'VERCEL', 'DATABASE_', 'PG', 'CI_PG_')):
        os.environ.pop(key, None)
os.environ.update(CI_LOCAL_DEV='1', VERCEL='0')
original_connect = socket.socket.connect
external = []
def connect(self, address):
    if isinstance(address, tuple) and address[0] not in ('127.0.0.1', 'localhost', '::1'):
        external.append(str(address)); raise OSError('Synthetic inventory test forbids provider traffic')
    return original_connect(self, address)
socket.socket.connect = connect
from api.index import handler
from legacy_storage_fake import PostgresWorkspace
from test_vercel import FakeDatabase
from eos_fixture import build, ROWS
from hosting.spreadsheet_inbox import SpreadsheetInbox
store = PostgresWorkspace('postgresql://test.invalid/synthetic', FakeDatabase().connect)
rows = [list(r) for r in ROWS]
rows[2][4] = 'Holes /  Glue '
raw = build([tuple(r) for r in rows])
with store.session() as directory:
    box = SpreadsheetInbox(directory)
    report = box.ingest('SYNTHETIC_2230_Long_Report_Name_For_Assembly_Quality_Review_2026-10-07_With_Clear_Version_Context.xlsx', raw)
    before = box.get(report['file_hash'])
requests = []
class LocalHandler(handler):
    def log_message(self, *args): pass
    def do_POST(self):
        requests.append(self.path)
        if self.path not in ('/api/spreadsheets/fixes', '/api/spreadsheets/sheet', '/api/spreadsheets/cells', '/api/spreadsheets/findings', '/api/clients', '/api/vins/search'):
            return self.fail(409, 'Synthetic inventory test forbids mutation/provider actions.')
        return super().do_POST()
server = ThreadingHTTPServer(('127.0.0.1', 0), LocalHandler); server.workspace_store = store
threading.Thread(target=server.serve_forever, daemon=True).start()
try:
    result = subprocess.run(['node', str(ROOT / 'tests/browser_report_list_context.js'), f'http://127.0.0.1:{server.server_port}', report['file_hash']], text=True, capture_output=True,
        env={k:os.environ[k] for k in ('HOME','PATH','LANG','PLAYWRIGHT_MODULE','CHROMIUM_EXECUTABLE') if k in os.environ}, timeout=110)
    print(result.stdout); print(result.stderr, file=sys.stderr)
    with store.session() as directory:
        box = SpreadsheetInbox(directory)
        assert box.get(report['file_hash']) == before
        assert box.download(report['file_hash'])[1] == raw
    allowed = ('/api/spreadsheets/fixes', '/api/spreadsheets/sheet', '/api/spreadsheets/cells', '/api/spreadsheets/findings', '/api/clients', '/api/vins/search')
    assert not [p for p in requests if p not in allowed], requests
    assert not external, external
    assert result.returncode == 0, result.returncode
    print(json.dumps(dict(passed=True, full_app=True, fake_storage=True, original_unchanged=True, no_provider_traffic=True, requests=requests)))
finally:
    server.shutdown(); server.server_close()
