"""Actual handlers and UI with a synthetic zero-findings, source-unverified report."""
from pathlib import Path
import os, sys, json, tempfile, threading, socket
from http.server import ThreadingHTTPServer
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from playwright.sync_api import sync_playwright, expect
from hosting.sqlite_storage import SQLiteWorkspace
from hosting.spreadsheet_inbox import SpreadsheetInbox
from hosting import access
from api.index import handler
from tests.test_reconcile import sl_daily, snap, ALL, SITE, LIST
OUT = Path(os.environ['EVIDENCE_DIR']); OUT.mkdir(parents=True, exist_ok=True)
CHROMIUM = os.environ['SOURCE_FIRST_CHROMIUM']
external = []; realconnect = socket.socket.connect

def local(self, address):
    if isinstance(address, tuple) and address[0] not in ('127.0.0.1', 'localhost', '::1'):
        external.append(str(address)); raise OSError('External network forbidden')
    return realconnect(self, address)
socket.socket.connect = local
assigned = dict(status='manual', site=SITE, site_id='site-1', list_id=LIST, needs_person=False, client='Synthetic', client_key='synthetic')
source = [dict(x, fields={k: v for k, v in x['fields'].items() if k != 'QtyNOK'}) for x in ALL]
raw = sl_daily(nok_blank=True)
with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'CI_LOCAL_DEV':'1', 'VERCEL':'0'}, clear=True), patch.object(SpreadsheetInbox, 'assignment_for', return_value=assigned):
    store = SQLiteWorkspace(temp)
    with store.session() as d:
        box = SpreadsheetInbox(d)
        user = access.save_user(box, dict(email='synthetic-label-admin@example.invalid', role='admin'))
        token = access.start_session(box, user)
        item = box.ingest('Synthetic held report 2214.xlsx', raw); key = item['file_hash']
        box.source_reconcile(key, snap([source]), '2026-11-06', expected_check_hash=item['check_hash'], expected_assignment=assigned)
        before = box.get(key)
        row = box.list()['spreadsheets'][0]
        assert row['review']['status'] == 'clear' and row['review']['counts']['high'] == 0
        assert not row['verification']['source_verified'] and row['verification']['holds']
    class Quiet(handler):
        def log_message(self, *args): pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Quiet); server.workspace_store = store
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = 'http://127.0.0.1:' + str(server.server_port)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, executable_path=CHROMIUM)
            for width in (1200, 390):
                context = browser.new_context(viewport={'width':width, 'height':900})
                context.add_cookies([dict(name='ppg_session', value=token, url=base)])
                context.route('**/*', lambda r: r.continue_() if r.request.url.startswith(base + '/') else (external.append(r.request.url), r.abort())[1])
                # Fix the synthetic test's business date without touching saved data or clock settings.
                context.route('**/api/today', lambda r: r.continue_(post_data=json.dumps(dict(day='2026-11-06'))))
                page = context.new_page(); errors = []; writes = []
                page.on('pageerror', lambda e: errors.append(str(e)))
                page.on('request', lambda r: writes.append(r.url) if r.method == 'POST' else None)
                page.goto(base + '/app', wait_until='networkidle')
                expect(page.locator('.queue-entry')).to_contain_text('Verification held')
                expect(page.locator('.queue-entry')).to_contain_text('Rejected: Unknown')
                assert page.locator('.tag-final').count() == 0
                page.get_by_role('button', name='Final reports', exact=True).click()
                expect(page.locator('.queue-empty')).to_contain_text('No final reports yet')
                page.get_by_role('button', name='To do: 1 saved versions needing attention', exact=True).click()
                expect(page.locator('.queue-entry')).to_contain_text('Verification held')
                page.goto(base + '/app#today', wait_until='networkidle')
                expect(page.locator('.pdays')).to_contain_text('Verification held')
                expect(page.locator('.pdays')).to_contain_text('Not sent')
                expect(page.locator('.numbers')).to_contain_text('Yesterday on hold')
                expect(page.locator('.numbers')).to_contain_text('Yesterday not sent')
                page.locator('.pdays').evaluate('(el) => { el.parentElement.scrollLeft = el.parentElement.scrollWidth; }')
                expect(page.locator('.wins-bar')).to_contain_text('1 saved report needs attention')
                assert 'Nice work' not in page.locator('.wins-bar').inner_text()
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
                page.screenshot(path=str(OUT / ('held-labels-today-' + str(width) + '.png')), full_page=True)
                assert not errors, errors
                assert not any(any(action in url for action in ('/approve', '/send', '/source-reconcile', '/apply', '/api/build', '/api/autopilot')) for url in writes), writes
                with store.session() as d:
                    box = SpreadsheetInbox(d)
                    assert box.get(key) == before
                    assert box.download(key)[1] == raw
                context.close()
                print(json.dumps(dict(passed=True, width=width, actual_handlers=True, zero_findings_with_real_hold=True, no_final_or_clear=True, today_and_waiting_consistent=True, original_unchanged=True)), flush=True)
            browser.close()
    finally:
        server.shutdown(); server.server_close()
assert not external, external
print('PASS held-report labels actual-handler desktop/mobile; no external requests')
