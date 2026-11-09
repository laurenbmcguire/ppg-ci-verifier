"""Synthetic-only exact scope, completeness, race and read-only summary tests."""
import copy
import json
import socket
from pathlib import Path
import pytest
from hosting import finding_summary as fs
from hosting import ai_review
from hosting.review_summary import check_run, summarize
from hosting.spreadsheet_inbox import SpreadsheetInbox


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(socket.socket, 'connect', lambda *a: (_ for _ in ()).throw(AssertionError('Network forbidden')))
    box = SpreadsheetInbox(tmp_path)
    item = box.ingest('SYNTHETIC.csv', b'Part,Inspected,Rejected\nPRIVATE-CELL-SENTINEL,10,\n')
    payload = dict(file_hash=item['file_hash'], expected_check_hash=item['check_hash'], run=check_run(item),
                   expected_rev=0, scope='available_findings')
    return box, item, payload


def provider(body):
    return 'Review the available missing quantity findings. Unknown values remain unknown.'


def fake_review(n=4):
    return dict(issues=[dict(key=f'finding-{i}', fp=str(i), source='Checks', code='missing_quantity', severity='high') for i in range(n)],
                dismissed=[], coverage=dict(findings_total=n, findings_listed=n, all_findings=n, not_listed=0, truncated=False), reconcile={})


def test_real_inventory_readonly_no_full_workbook_or_cell_values(setup):
    box, item, p = setup
    before = copy.deepcopy(box.get(item['file_hash']))
    raw = box.original_bytes(item['file_hash'])[1]
    calls = []
    def capture(body):
        calls.append(body)
        return provider(body)
    out = fs.prepare(box, p, lambda: True, capture)
    rv = summarize(item)
    assert out['counts']['available'] == len(rv['issues']) + len(rv['dismissed'])
    assert sum(g['count'] for g in out['groups']) == out['counts']['available']
    assert out['coverage']['complete_available_inventory']
    assert out['narrative_group_count'] == len(out['groups']) and out['read_only']
    assert out['check_hash'] == p['expected_check_hash'] and out['run'] == p['run']
    assert box.get(item['file_hash']) == before and box.original_bytes(item['file_hash'])[1] == raw
    assert box.list()['count'] == 1
    body = calls[0]
    assert body['max_tokens'] == 2000 and 'tools' not in body
    assert 'untrusted data' in body['system']
    serialized = json.dumps(body)
    for private in ('PRIVATE-CELL-SENTINEL', 'SYNTHETIC.csv', item['file_hash'], 'base64', 'cell_value'):
        assert private not in serialized
    assert not any(k in out for k in ('proposals', 'changes', 'decisions', 'history'))


@pytest.mark.parametrize('bad', ['scope', 'extra', 'hash', 'check', 'run', 'rev', 'large', 'access'])
def test_invalid_stale_access_never_calls_provider(setup, bad):
    box, item, p = setup
    if bad == 'scope': p['scope'] = 'all'
    if bad == 'extra': p['messages'] = ['untrusted request']
    if bad == 'hash': p['file_hash'] = '../unsafe'
    if bad == 'check': p['expected_check_hash'] = '0'*64
    if bad == 'run': p['run'] = {}
    if bad == 'rev': p['expected_rev'] = 1
    if bad == 'large': p['run']['extra'] = 'x'*9000
    with pytest.raises(ValueError):
        fs.prepare(box, p, lambda: bad != 'access', lambda body: pytest.fail('Provider called'))


@pytest.mark.parametrize('race', ['access', 'item', 'assignment', 'holds', 'inventory', 'run', 'rev', 'raw'])
def test_post_provider_races_discard_entire_result(setup, monkeypatch, race):
    box, item, p = setup
    allowed = [True]
    monkeypatch.setattr(box, 'assignment_for', lambda *a: {'status': 'unmatched'})
    def change(body):
        if race == 'access': allowed[0] = False
        if race == 'item': box._update(item['file_hash'], lambda r: r.update(review_request={'status': 'waiting'}))
        if race == 'assignment': monkeypatch.setattr(box, 'assignment_context', lambda: {'changed': True})
        if race == 'holds': monkeypatch.setattr(box, 'holds', lambda *a: [{'code': 'new_hold'}])
        if race == 'inventory': monkeypatch.setattr(fs, 'summarize', lambda *a: fake_review(7))
        if race == 'run': p['run'] = {}
        if race == 'rev': p['expected_rev'] = 1
        if race == 'raw':
            old = box._original
            monkeypatch.setattr(box, '_original', lambda *a: (old(*a)[0], b'different bytes'))
        return provider(body)
    with pytest.raises(ValueError): fs.prepare(box, p, lambda: allowed[0], change)
    assert not box.get(item['file_hash']).get('ai_fixes')


def test_changed_pre_provider_inventory_never_sent(setup, monkeypatch):
    box, item, p = setup
    calls = [0]
    original = fs.summarize
    def changing(item):
        calls[0] += 1
        return original(item) if calls[0] == 1 else fake_review(7)
    monkeypatch.setattr(fs, 'summarize', changing)
    with pytest.raises(ValueError): fs.prepare(box, p, lambda: True, lambda body: pytest.fail('Stale data sent'))


@pytest.mark.parametrize('kind', ['partial', 'missing', 'inconsistent'])
def test_incomplete_unknown_coverage_never_claims_all(setup, monkeypatch, kind):
    box, item, p = setup
    rv = fake_review()
    if kind == 'partial': rv['coverage'].update(findings_total=2000, not_listed=1996, truncated=True)
    if kind == 'missing': rv.pop('coverage')
    if kind == 'inconsistent': rv['coverage']['all_findings'] = 10
    monkeypatch.setattr(fs, 'summarize', lambda *a: copy.deepcopy(rv))
    monkeypatch.setattr(box, 'holds', lambda *a: [])
    out = fs.prepare(box, p, lambda: True, provider)
    assert not out['coverage']['complete_available_inventory']
    assert 'not all errors' in out['coverage']['label']
    assert sum(g['count'] for g in out['groups']) == 4


def test_zero_skips_provider_and_retains_holds(setup, monkeypatch):
    box, item, p = setup
    monkeypatch.setattr(fs, 'summarize', lambda *a: fake_review(0))
    monkeypatch.setattr(box, 'holds', lambda *a: [{'code': 'source_incomplete', 'hard': True}])
    out = fs.prepare(box, p, lambda: True, lambda body: pytest.fail('Empty inventory called provider'))
    assert out['narrative_status'] == 'empty' and out['counts']['available'] == 0 and out['holds']


def test_high_count_compact_groups_complete_no_slicing(setup, monkeypatch):
    box, item, p = setup
    rv = fake_review(3000)
    rv['dismissed'] = [dict(rv['issues'].pop(), dismissed_by='person')]
    monkeypatch.setattr(fs, 'summarize', lambda *a: copy.deepcopy(rv))
    monkeypatch.setattr(box, 'holds', lambda *a: [])
    captured = []
    out = fs.prepare(box, p, lambda: True, lambda body: captured.append(body) or provider(body))
    assert out['counts']['available'] == 3000 and out['counts']['dismissed'] == 1
    assert sum(g['count'] for g in out['groups']) == 3000
    assert {i for g in out['groups'] for i in g['ids']} == {f'finding-{i}' for i in range(3000)}
    assert len(out['groups']) == 2 and out['narrative_group_count'] == 2
    assert len(json.dumps(captured[0]).encode()) < fs.MAX_REQUEST_BYTES


@pytest.mark.parametrize('kind', ['groups', 'metadata', 'serialization'])
def test_oversized_preserves_all_deterministic_groups_without_provider(setup, monkeypatch, kind):
    box, item, p = setup
    rv = fake_review(201 if kind == 'groups' else 4)
    if kind == 'groups':
        for i, row in enumerate(rv['issues']): row['code'] = f'code-{i}'
    if kind == 'metadata': rv['issues'][0]['code'] = 'x'*fs.MAX_REQUEST_BYTES
    if kind == 'serialization': monkeypatch.setattr(fs, 'MAX_REQUEST_BYTES', 100)
    monkeypatch.setattr(fs, 'summarize', lambda *a: copy.deepcopy(rv))
    monkeypatch.setattr(box, 'holds', lambda *a: [])
    out = fs.prepare(box, p, lambda: True, lambda body: pytest.fail('Oversize data sent'))
    assert out['narrative_status'] == 'too_large' and not out['reply']
    assert sum(g['count'] for g in out['groups']) == len(rv['issues'])
    assert out['narrative_group_count'] == 0


@pytest.mark.parametrize('error', [ai_review.AIReviewNotConfigured('SECRET'), ai_review.AIReviewError('SECRET'), TimeoutError('SECRET')])
def test_provider_failure_retains_counts_redacts_error(setup, error):
    box, item, p = setup
    def fail(body): raise error
    out = fs.prepare(box, p, lambda: True, fail)
    assert out['narrative_status'] in ('not_configured', 'unavailable')
    assert 'SECRET' not in json.dumps(out) and out['counts']['available'] > 0 and not out['reply']


@pytest.mark.parametrize('data', [None, {}, {'content': []}, {'content': [{'type': 'tool_use', 'name': 'apply'}]},
    {'content': [{'type': 'text', 'text': 'x'*12001}]}, {'content': [{'type': 'text', 'text': 'partial'}], 'stop_reason': 'max_tokens'}])
def test_model_output_no_tools_truncation_or_oversize(monkeypatch, data):
    monkeypatch.setattr(ai_review, '_post', lambda *a, **k: data)
    with pytest.raises(ai_review.AIReviewError): fs.ask({})


def test_duplicate_inventory_fails_closed(setup, monkeypatch):
    box, item, p = setup
    rv = fake_review(); rv['issues'].append(dict(rv['issues'][0]))
    monkeypatch.setattr(fs, 'summarize', lambda *a: rv)
    with pytest.raises(ValueError, match='ambiguous'):
        fs.prepare(box, p, lambda: True, lambda body: pytest.fail('Ambiguous inventory sent'))


def test_route_access_and_explicit_only():
    source = Path('api/index.py').read_text()
    route = source.split("            if route == '/api/spreadsheets/review-summary':")[1].split("            if route ==")[0]
    assert route.index('validate_request') < route.index('self.can_hash') < route.index('finding_summary.run')
    assert source.count('finding_summary.run(') == 1


def test_provider_allowlist_excludes_future_review_fields():
    rv = fake_review()
    rv['coverage']['private_future_field'] = 'PRIVATE-COVERAGE-SENTINEL'
    rv['reconcile'] = {'private_future_field': 'PRIVATE-REVIEW-SENTINEL'}
    result = fs.inventory(rv, [{'code': 'hold', 'hard': True, 'text': 'PRIVATE-HOLD-SENTINEL'}])
    encoded = json.dumps(fs.request_body(result))
    assert 'PRIVATE-' not in encoded and 'private_future_field' not in encoded


def test_http_explicit_route_auth_scope_and_readonly(tmp_path, monkeypatch):
    import threading
    import urllib.request
    import urllib.error
    from http.server import ThreadingHTTPServer
    from api.index import handler
    from tests.test_focus_desk_audit import _Store
    box = SpreadsheetInbox(tmp_path)
    item = box.ingest('SYNTHETIC.csv', b'Part,Inspected,Rejected\nSYN-1,10,\n')
    p = dict(file_hash=item['file_hash'], expected_check_hash=item['check_hash'], run=check_run(item), expected_rev=0, scope='available_findings')
    class Synthetic(handler):
        def current_user(self): return self.server.user
        def can_hash(self, value): return self.server.allowed
        def log_message(self, *args): pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Synthetic)
    server.workspace_store = _Store(box.root)
    server.user = {'id': 'synthetic', 'role': 'admin'}
    server.allowed = True
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    monkeypatch.setenv('VERCEL', '0')
    monkeypatch.setenv('CI_LOCAL_DEV', '1')
    called = []
    monkeypatch.setattr(fs, 'ask', lambda body: called.append(body) or provider(body))
    base = f'http://127.0.0.1:{server.server_port}'
    def post(payload, origin=None):
        req = urllib.request.Request(base + '/api/spreadsheets/review-summary', data=json.dumps(payload).encode(),
            headers={'Content-Type': 'application/json', 'Origin': origin or base, 'X-Review-Token': 'same-origin'})
        try:
            with urllib.request.urlopen(req, timeout=5) as response: return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            with exc: return exc.code, json.load(exc)
    before = box.get(item['file_hash']); raw = box.original_bytes(item['file_hash'])[1]
    try:
        assert post(p, 'https://wrong.invalid')[0] == 403 and not called
        server.user = None
        assert post(p)[0] == 401 and not called
        server.user = {'id': 'synthetic', 'role': 'reviewer'}
        server.allowed = False
        assert post(p)[0] == 404 and not called
        server.allowed = True
        assert post(dict(p, scope='all'))[0] == 409 and not called
        code, out = post(p)
        assert code == 200 and out['read_only'] and out['narrative_status'] == 'done' and len(called) == 1
        assert box.get(item['file_hash']) == before and box.original_bytes(item['file_hash'])[1] == raw
        assert box.list()['count'] == 1
    finally:
        server.shutdown(); server.server_close(); thread.join()
