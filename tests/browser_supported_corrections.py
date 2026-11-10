"""Actual HTTP handler and real browser; synthetic workbook, fake storage/provider only."""
import hashlib, io, json, os, socket, subprocess, sys, threading
from pathlib import Path
from http.server import ThreadingHTTPServer
ROOT = Path(__file__).resolve().parents[1]
WIDTH = int(sys.argv[1]) if len(sys.argv) > 1 else 1200
assert WIDTH in (1200, 390)
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
for key in list(os.environ):
    if key.startswith(('MS_', 'ANTHROPIC_', 'OPENAI_', 'VERCEL', 'DATABASE_', 'PG', 'CI_PG_')):
        os.environ.pop(key, None)
os.environ.update(CI_LOCAL_DEV='1', VERCEL='0', AI_LAYOUT='0')
connect = socket.socket.connect
external = []
def local(self, address):
    if isinstance(address, tuple) and address[0] not in ('127.0.0.1', 'localhost', '::1'):
        external.append(str(address))
        raise OSError('Synthetic regression forbids external network')
    return connect(self, address)
socket.socket.connect = local
from api.index import handler
import api.index as api_module
from hosting import ai_proposals, reconcile
from hosting.spreadsheet_inbox import SpreadsheetInbox
from hosting.review_summary import summarize
from hosting.selected_corrections import candidates
from legacy_storage_fake import PostgresWorkspace
from test_vercel import FakeDatabase
from eos_fixture import build
from openpyxl import load_workbook
# Fake availability and provider only. No safety, holds, cell-type or authorization guards replaced.
api_module.ai_review_configured = lambda: True
provider_calls = []
def provider(rows):
    provider_calls.append(rows)
    assert len(rows) == 1 and rows[0]['cell'] == 'L18' and rows[0]['before'] == 'Synthetic mixed note', rows
    return {'fixes': [dict(finding_id=r['finding_id'], sheet=r['sheet'], cell=r['cell'], before=r['before'], after='Synthetic reviewed note', reason='Synthetic provider text suggestion') for r in rows]}
ai_proposals.ask = provider
store = PostgresWorkspace('postgresql://test.invalid/synthetic', FakeDatabase().connect)
cases = []
with store.session() as directory:
    box = SpreadsheetInbox(directory)
    for name in ('mechanical', 'mixed', 'unsupported', 'refresh_failure'):
        overrides = {('1st Shift', 'G17'): '34', ('1st Shift', 'I18'): 'Holes /  Glue ', ('1st Shift', 'L18'): 'Synthetic ' + name + ' note'}
        raw = build(overrides=overrides)
        item = box.ingest('2230-SYNTHETIC-' + name + '.xlsx', raw)
        cells = ['G17', 'I18'] if name in ('mechanical', 'refresh_failure') else ['G17', 'L18'] if name == 'mixed' else [None]
        findings = [dict(code='synthetic_selected_review', sheet='1st Shift', cell=cell, row=17 if cell == 'G17' else 18, message='Synthetic selected review', observed=[{'type':'str','value':str(overrides.get(('1st Shift', cell), 'row'))}]) for cell in cells]
        box._update(item['file_hash'], lambda r: r.update(findings=findings, finding_counts={'synthetic_selected_review':len(findings)}, finding_count=len(findings), findings_truncated=False, source_reconciliation=dict(status='incomplete', verified=False, bound_to=dict(file_hash=item['file_hash'], checker_version=item['checker_version']), not_verified_because=['Synthetic source evidence still missing.'])))
        item = box.get(item['file_hash'])
        selected = [i for i in summarize(item)['issues'] if i.get('code') == 'synthetic_selected_review']
        assert len(selected) == len(cells), selected
        assert any(h['code'] == 'source_coverage_incomplete' for h in box.holds(item))
        exact = candidates(item, raw)
        expected_mechanical = 2 if name in ('mechanical', 'refresh_failure') else 1 if name == 'mixed' else 0
        assert sum(i['key'] in exact for i in selected) == expected_mechanical, (name, selected, exact)
        cases.append(dict(name=name, hash=item['file_hash'], ids=[i['key'] for i in selected], fps={i['key']:i['fp'] for i in selected}, raw=raw, cells=cells, original_receipt=item['source_reconciliation']))
applies = []
read_failures = []
class LocalHandler(handler):
    def log_message(self, *args): pass
    def do_GET(self):
        failed_case = next(c for c in cases if c['name'] == 'refresh_failure')
        if any(p['file_hash'] == failed_case['hash'] for p in applies) and (self.path == '/api/spreadsheets' or self.path.startswith('/api/spreadsheet?id=')):
            read_failures.append(self.path)
            return self.fail(503, 'Synthetic post-save refresh failure. The committed version remains stored.')
        return super().do_GET()
    def do_POST(self):
        allowed = ('/api/clients', '/api/spreadsheets/sheet', '/api/spreadsheets/cells', '/api/spreadsheets/fixes', '/api/spreadsheets/findings', '/api/spreadsheets/ai-proposals', '/api/spreadsheets/apply-fixes')
        if self.path not in allowed: return self.fail(409, 'Only isolated synthetic actions allowed.')
        if self.path.endswith('/apply-fixes'):
            data = self.rfile.read(int(self.headers.get('Content-Length', '0')))
            payload = json.loads(data); self.rfile = io.BytesIO(data)
            case = next((c for c in cases if c['hash'] == payload.get('file_hash') and c['name'] != 'unsupported'), None)
            if not case or payload.get('recommended_only') is True or len(payload.get('changes', [])) != 2 or {c.get('cell') for c in payload['changes']} != set(case['cells']):
                return self.fail(409, 'Only the exact two synthetic selected cells allowed.')
            applies.append(payload)
        return super().do_POST()
server = ThreadingHTTPServer(('127.0.0.1', 0), LocalHandler); server.workspace_store = store
threading.Thread(target=server.serve_forever, daemon=True).start()
try:
    config = [{k:v for k,v in c.items() if k != 'raw'} for c in cases]
    result = subprocess.run(['node', str(ROOT/'tests/browser_supported_corrections.js'), f'http://127.0.0.1:{server.server_port}', json.dumps(config), str(WIDTH)], text=True, capture_output=True, env={k:os.environ[k] for k in ('HOME','PATH','LANG','PLAYWRIGHT_MODULE','CHROMIUM_EXECUTABLE','EVIDENCE_DIR') if k in os.environ}, timeout=130)
    print(result.stdout); print(result.stderr, file=sys.stderr)
    assert result.returncode == 0, result.returncode
    evidence = []
    with store.session() as directory:
        box = SpreadsheetInbox(directory)
        for c in cases:
            old = box.get(c['hash']); assert box.download(c['hash'])[1] == c['raw']
            assert old['source_reconciliation'] == c['original_receipt']
            records = old.get('fixes_applied') or []
            if c['name'] == 'unsupported':
                assert not records; continue
            assert len(records) == 1
            new = box.get(records[0]['fixed_file_hash'])
            assert new['revision'] == 2 and len(new['fixed_from']['changes']) == 2
            assert not new.get('approval') and not new.get('sent')
            assert any(h['code'] == 'source_coverage_incomplete' and h['hard'] for h in box.holds(new)), box.holds(new)
            receipt = new['source_reconciliation']
            assert receipt['stale'] is True and receipt['verified'] is False and receipt['carried_from'] == c['hash']
            assert receipt['bound_to'] == c['original_receipt']['bound_to'] and not reconcile.current(new)
            a = load_workbook(io.BytesIO(c['raw'])); b = load_workbook(io.BytesIO(box.download(new['file_hash'])[1]))
            diffs = [(ws.title, cell.coordinate, cell.value, b[ws.title][cell.coordinate].value) for ws in a for row in ws for cell in row if cell.value != b[ws.title][cell.coordinate].value]
            assert {d[1] for d in diffs} == set(c['cells']) and len(diffs) == 2, diffs
            assert type(b['1st Shift']['G17'].value) is int
            evidence.append(dict(case=c['name'], exact_cell_differences=diffs, original_byte_identical=True, source_hold_preserved=True, new_revision=2))
    assert len(applies) == 3 and len(provider_calls) == 1 and not external
    assert any(p == "/api/spreadsheets" for p in read_failures) and any(p.startswith("/api/spreadsheet?id=") for p in read_failures), read_failures
    print(json.dumps(dict(passed=True, width=WIDTH, actual_handler=True, saves=3, provider_calls=1, post_save_read_failures=len(read_failures), synthetic_only=True, evidence=evidence)))
finally:
    server.shutdown(); server.server_close()
