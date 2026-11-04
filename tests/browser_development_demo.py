"""Real Chromium regression QA; isolated temporary SQLite, synthetic data only.

Run from project root:
  PYTHONPATH=/path/to/vercel_deps:. python tests/browser_development_demo.py
Outputs verification/development-browser-results.json. Exit 2 means environment
blocked, not a passing test. No screenshots are published or saved to Library.
The local server and browser intentionally run in the same process/network scope.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import traceback
from http.server import ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
os.environ['CI_LOCAL_DEV'] = '1'
os.environ['CI_DEVELOPMENT_DEMO'] = '1'
from api.index import handler
from hosting.sqlite_storage import SQLiteWorkspace
from playwright.sync_api import sync_playwright, expect


def fixtures(directory):
    import hashlib
    directory = ROOT / 'samples/development_examples'
    index = json.loads((directory / 'index.json').read_text())
    assert index['synthetic'] is True
    result = []
    for example in index['examples']:
        if example['source'] is None:
            continue
        assert example['synthetic'] is True
        for field in ('artifact', 'source'):
            path = directory / example[field]['path']
            assert hashlib.sha256(path.read_bytes()).hexdigest() == example[field]['sha256']
        result.append((example['id'], directory / example['artifact']['path'], directory / example['source']['path'], example['expected']))
    assert len(result) >= 3
    return result


def run_viewport(browser, width, height, fixture_paths, result):
    context = browser.new_context(viewport={'width': width, 'height': height}, accept_downloads=True)
    page = context.new_page()
    errors, network = [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('console', lambda message: errors.append(message.text) if message.type == 'error' and 'favicon' not in message.text else None)
    page.on('requestfailed', lambda request: network.append({'url': request.url, 'failure': request.failure}))
    page.on('response', lambda response: network.append({'url': response.url, 'status': response.status}) if response.status >= 400 and not response.url.endswith('/favicon.ico') else None)
    checks = result['checks']
    def ok(name, detail=None):
        checks.append({'name': name, 'passed': True, 'detail': detail})
    def no_overflow(stage):
        dimensions = page.evaluate('({viewport:innerWidth,page:document.documentElement.scrollWidth})')
        assert dimensions['page'] <= dimensions['viewport'], (stage, dimensions)
        ok('no_horizontal_overflow_' + stage, dimensions)
    with tempfile.TemporaryDirectory(prefix='verifier-browser-db-') as db:
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        server.workspace_store = SQLiteWorkspace(db)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            page.goto(base, wait_until='networkidle')
            expect(page.locator('#spreadsheet-file')).to_be_enabled()
            expect(page.locator('#workflow-sample')).to_be_enabled()
            ok('page_loaded')
            no_overflow('loaded')
            # File chooser event exercises the browser's native input selection.
            with page.expect_file_chooser() as chooser:
                page.locator('#spreadsheet-file').click()
            chooser.value.set_files(str(ROOT / 'samples/sqlite-upload-demo.csv'))
            expect(page.locator('#spreadsheet-detail')).to_contain_text('sqlite-upload-demo.csv')
            expect(page.locator('#spreadsheet-file')).to_be_enabled()
            ok('csv_native_file_chooser')
            for name, workbook, source, expected in fixture_paths:
                with page.expect_response(lambda r: '/api/spreadsheets/import' in r.url and r.request.method == 'POST') as upload:
                    page.locator('#spreadsheet-file').set_input_files(str(workbook))
                data = upload.value.json()['item']
                expect(page.locator('#spreadsheet-detail')).to_contain_text('numeric cells checked')
                assert data['numeric_cells_checked'] > 0 and data['release_status'] == 'held'
                expect(page.locator('#spreadsheet-detail input[type=file]')).to_be_enabled()
                with page.expect_response(lambda r: '/api/spreadsheets/reconcile' in r.url) as reconciliation:
                    page.locator('#spreadsheet-detail input[type=file]').set_input_files(str(source))
                compared = reconciliation.value.json()['item']
                assert compared['numeric_values_compared'] > 0
                for key, value in expected.items():
                    assert compared.get(key) == value, (name, key, compared.get(key), value)
                assert compared['approval_effective'] is False and compared['release_status'] == 'held'
                expect(page.locator('#spreadsheet-file')).to_be_enabled()
                no_overflow(name)
                ok(name + '_xlsx_json_source', {key: compared.get(key) for key in ('numeric_cells_checked','numeric_values_compared','finding_counts','source_difference_count')})
            # Actual uploaded XLSX bridge, separate from the JSON workflow demo.
            import hashlib
            mismatch = next(item for item in fixture_paths if item[0] == 'synthetic_ppg_source_mismatch')
            original_bytes = mismatch[1].read_bytes()
            original_hash = hashlib.sha256(original_bytes).hexdigest()
            with page.expect_response(lambda r: '/api/spreadsheets/demo/review?id=' + original_hash in r.url) as reviewed:
                page.locator('#spreadsheet-list button[data-id="' + original_hash + '"]').click()
            spreadsheet_review = reviewed.value.json()
            assert spreadsheet_review['eligible'] is True
            assert spreadsheet_review['current_version'] is True
            assert len(spreadsheet_review['proposals']) > 0
            panel = page.locator('.spreadsheet-demo-review')
            changes = panel.locator('.report-finding')
            proposal = spreadsheet_review['proposals'][0]
            for label, decision in (('Accept', 'approve'), ('Reject', 'deny')):
                with page.expect_response(lambda r: r.url.endswith('/api/spreadsheets/demo/decisions')) as decision_saved:
                    changes.first.get_by_role('button', name='Demo ' + label + ' change', exact=True).click()
                assert decision_saved.value.status == 200
                payload = decision_saved.value.request.post_data_json
                assert payload['proposal_ids'] == [proposal['id']]
                assert payload['decision'] == decision
                expect(changes.first).to_contain_text('Demo decision: ' + decision)
            ok('xlsx_individual_accept_reject')
            changes.first.locator('input[type=checkbox]').check()
            panel.get_by_role('button', name='Create Excel draft from selected accepted changes', exact=True).click()
            expect(page.locator('#spreadsheet-message')).to_contain_text('must be demo-accepted first')
            expect(page.locator('#spreadsheet-detail h3').first).to_contain_text(mismatch[1].name)
            ok('xlsx_rejected_change_cannot_apply')
            with page.expect_response(lambda r: r.url.endswith('/api/spreadsheets/demo/decisions')) as accepted:
                panel.get_by_role('button', name='Demo Accept selected changes', exact=True).click()
            assert accepted.value.request.post_data_json['proposal_ids'] == [proposal['id']]
            expect(changes.first).to_contain_text('Demo decision: approve')
            expect(changes.first.locator('input[type=checkbox]')).not_to_be_checked()
            ok('xlsx_batch_selection_explicit_and_cleared')
            changes.first.locator('input[type=checkbox]').check()
            with page.expect_response(lambda r: r.url.endswith('/api/spreadsheets/demo/apply')) as excel_applied:
                panel.get_by_role('button', name='Create Excel draft from selected accepted changes', exact=True).click()
            assert excel_applied.value.status == 200
            assert excel_applied.value.request.post_data_json['proposal_ids'] == [proposal['id']]
            excel_draft = excel_applied.value.json()['item']
            assert excel_draft['file_hash'] != original_hash
            assert excel_draft['release_status'] == 'held'
            assert excel_draft['approval_effective'] is False
            expect(panel.get_by_role('link', name='Download unapproved synthetic Excel draft')).to_be_visible()
            with page.expect_download() as download_event:
                panel.get_by_role('link', name='Download unapproved synthetic Excel draft').click()
            download = download_event.value
            assert download.failure() is None
            from openpyxl import load_workbook
            import io
            exported = load_workbook(io.BytesIO(Path(download.path()).read_bytes()), data_only=False)
            assert exported[proposal['sheet']][proposal['cell']].value == proposal['after']
            assert hashlib.sha256(mismatch[1].read_bytes()).hexdigest() == original_hash
            exported.close()
            ok('xlsx_accepted_change_download', {'original_hash': original_hash, 'draft_hash': excel_draft['file_hash'], 'changed_cell': proposal['sheet'] + '!' + proposal['cell']})
            page.reload(wait_until='networkidle')
            with page.expect_response(lambda r: '/api/spreadsheets/demo/review?id=' + original_hash in r.url):
                page.locator('#spreadsheet-list button[data-id="' + original_hash + '"]').click()
            expect(panel).to_contain_text('corrected draft supersedes this version')
            expect(panel.get_by_role('button', name='Demo Accept change', exact=True)).to_have_count(0)
            expect(panel.locator('input[type=checkbox]')).to_have_count(0)
            expect(panel.get_by_role('button', name='Create Excel draft from selected accepted changes', exact=True)).to_have_count(0)
            expect(panel.get_by_role('button', name='Open latest corrected draft', exact=True)).to_be_visible()
            no_overflow('xlsx_historical_review')
            ok('xlsx_superseded_version_has_no_decision_controls_after_reload')
            with page.expect_response(lambda r: '/api/workflow/import' in r.url) as imported:
                page.locator('#workflow-file').set_input_files(str(ROOT / 'samples/workflow-report.json'))
            original = imported.value.json()['item']
            expect(page.get_by_role('button', name='Demo Accept', exact=True).first).to_be_enabled()
            assert len(original['proposals']) >= 3, 'Fixture needs three proposals'
            proposals = page.locator('#workflow-detail .report-finding').filter(has=page.get_by_role('button', name='Demo Accept', exact=True))
            for position, (label, decision) in enumerate((('Accept', 'approve'), ('Reject', 'deny'), ('Hold', 'hold'))):
                with page.expect_response(lambda r: '/api/workflow/demo/decision' in r.url):
                    proposals.nth(position).get_by_role('button', name='Demo ' + label, exact=True).click()
                expect(proposals.nth(position)).to_contain_text('Demo decision: ' + decision)
            expect(proposals.nth(0).locator('input[type=checkbox]')).to_be_enabled()
            expect(proposals.nth(1).get_by_role('button', name='Apply demo-approved proposal to new draft', exact=True)).to_be_disabled()
            expect(proposals.nth(2).get_by_role('button', name='Apply demo-approved proposal to new draft', exact=True)).to_be_disabled()
            ok('accept_reject_hold_eligibility')
            for label, decision in (('Accept', 'approve'), ('Reject', 'deny'), ('Hold', 'hold')):
                proposals.nth(1).locator('input[type=checkbox]').check()
                proposals.nth(2).locator('input[type=checkbox]').check()
                with page.expect_response(lambda r: r.url.endswith('/api/workflow/demo/decisions')) as batch:
                    page.get_by_role('button', name='Demo ' + label + ' selected', exact=True).click()
                assert batch.value.status == 200
                payload = batch.value.request.post_data_json
                assert payload['decision'] == decision
                assert set(payload['proposal_ids']) == {original['proposals'][1]['id'], original['proposals'][2]['id']}
                saved = {d['proposal_id']: d['decision'] for d in batch.value.json()['item']['demo_decisions']}
                for position in (1, 2):
                    assert saved[original['proposals'][position]['id']] == decision
                    expect(proposals.nth(position)).to_contain_text('Demo decision: ' + decision)
                    expect(proposals.nth(position).locator('input[type=checkbox]')).not_to_be_checked()
                assert saved[original['proposals'][0]['id']] == 'approve'
                ok('atomic_bulk_' + label.lower() + '_selected')
            with page.expect_response(lambda r: r.url.endswith('/api/workflow/demo/decision')):
                proposals.nth(1).get_by_role('button', name='Demo Reject', exact=True).click()
            expect(proposals.nth(1)).to_contain_text('Demo decision: deny')
            page.reload(wait_until='networkidle')
            page.locator('#workflow-list button').first.click()
            expect(proposals.nth(0)).to_contain_text('Demo decision: approve')
            expect(proposals.nth(1)).to_contain_text('Demo decision: deny')
            expect(proposals.nth(2)).to_contain_text('Demo decision: hold')
            ok('decisions_survive_reload')
            expect(page.get_by_role('button', name='Human approval unavailable until reviewer identity is connected')).to_be_disabled()
            proposals.nth(1).locator('input[type=checkbox]').check()
            page.get_by_role('button', name='Apply selected demo-approved proposals to new draft', exact=True).click()
            expect(page.locator('#workflow-message')).to_contain_text('demo-accepted', ignore_case=True)
            expect(page.locator('#workflow-detail h3').first).to_contain_text('version ' + original['version'])
            proposals.nth(1).locator('input[type=checkbox]').uncheck()
            ok('rejected_selection_cannot_apply')
            proposals.nth(0).locator('input[type=checkbox]').check()
            with page.expect_response(lambda r: '/api/workflow/demo/apply' in r.url) as applied:
                page.get_by_role('button', name='Apply selected demo-approved proposals to new draft', exact=True).click()
            draft = applied.value.json()['item']
            assert draft['manifest_hash'] != original['manifest_hash']
            assert draft['approval_effective'] is False
            assert draft['proposal_count'] == original['proposal_count'] - 1
            ok('only_approved_proposal_in_new_draft', {'original_proposals': original['proposal_count'], 'draft_proposals': draft['proposal_count']})
            page.locator('#workflow-list button').last.click()
            expect(page.locator('#workflow-detail')).to_contain_text('historical version cannot be corrected or approved')
            for button in page.get_by_role('button', name='Demo Accept', exact=True).all():
                expect(button).to_be_disabled()
            ok('historical_version_locked')
            # Synchronous overlapping clicks must not launch competing mutations.
            page.evaluate("document.querySelector('#workflow-refresh').click();document.querySelector('#workflow-sample').click();document.querySelector('#workflow-refresh').click()")
            expect(page.locator('#workflow-refresh')).to_be_enabled()
            expect(page.locator('#workflow-detail')).to_contain_text('historical version cannot be corrected or approved')
            no_overflow('workflow')
            ok('overlapping_navigation_serialized')
            assert not errors, errors
            assert not network, network
            ok('no_console_page_or_network_errors')
            result['status'] = 'passed'
        finally:
            result['console_errors'] = errors
            result['network_errors'] = network
            context.close()
            server.shutdown(); server.server_close(); thread.join()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', default=str(ROOT / 'verification/development-browser-results.json'))
    parser.add_argument('--prepare-only', action='store_true', help='Validate fixture preparation without launching browser')
    args = parser.parse_args()
    result = {'engine': 'real Chromium via Playwright', 'synthetic_only': True, 'status': 'running', 'viewports': []}
    exit_code = 0
    try:
        with tempfile.TemporaryDirectory(prefix='verifier-browser-fixtures-') as directory:
            files = fixtures(Path(directory))
            if args.prepare_only:
                result.update(status='not_run', fixture_preparation='passed', browser_assertions_executed=False, reason='Chromium local IPC socket() Operation not permitted under default and escalated execution; launch attempts stopped.')
                Path(args.output).parent.mkdir(parents=True, exist_ok=True)
                Path(args.output).write_text(json.dumps(result, indent=2))
                print(json.dumps(result, indent=2))
                return 2
            with sync_playwright() as playwright:
                try:
                    browser = playwright.chromium.launch(executable_path='/usr/bin/chromium', headless=True, args=['--no-sandbox'])
                except Exception as error:
                    result.update(status='environment_blocked', error=str(error), browser_assertions_executed=False)
                    exit_code = 2
                else:
                    try:
                        for width, height in ((1440, 1000), (390, 844)):
                            item = {'width': width, 'height': height, 'checks': [], 'status': 'running'}
                            result['viewports'].append(item)
                            try:
                                run_viewport(browser, width, height, files, item)
                            except Exception:
                                item.update(status='failed', error=traceback.format_exc())
                                exit_code = 1
                        result['status'] = 'passed' if exit_code == 0 else 'failed'
                    finally:
                        browser.close()
    except Exception:
        result.update(status='failed', error=traceback.format_exc())
        exit_code = 1
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))
    return exit_code

if __name__ == '__main__':
    raise SystemExit(main())
