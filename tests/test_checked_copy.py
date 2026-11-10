"""Synthetic, read-only actual completion predicates and actual handler branches."""
import ast
import json
import copy
import contextlib
from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
import re
import pytest
from hosting.checked_copy import completion
from hosting.spreadsheets import CHECKER_VERSION

HASH = 'a' * 64
CHECK = 'b' * 64


def clean():
    now = datetime.now(timezone.utc)
    value = dict(total=10, known_rows=1, unknown_rows=0)
    pair = dict(status='matched', report=value, source=value)
    rec = dict(status='verified', verified=True, captured_at=now.isoformat(), snapshot_id='synthetic',
               bound_to=dict(file_hash=HASH, checker_version=CHECKER_VERSION),
               coverage=dict(complete=True), report_rows=1, sharepoint_items_for_day=1, rows=[], fields_checked=['inspected', 'rejected'],
               count_reconciliation=dict(status='verified', record_counts=dict(report_rows=1, source_rows=1, matched_unique_items=1, duplicate_report_rows=0, missing_source_items=0, unmatched_report_rows=0), quantities=dict(inspected=pair, rejected=pair), rejected_records=pair))
    item = dict(file_hash=HASH, check_hash=CHECK, checked_at=now.isoformat(), checker_version=CHECKER_VERSION,
                internal_check_status='no_findings_in_checks_performed', source_reconciliation=rec)
    review = dict(issues=[], dismissed=[], coverage=dict(findings_total=0, findings_listed=0, not_listed=0, all_findings=0, truncated=False),
                  sharepoint=dict(source_verified=True), quantities={k: dict(value=10, unit='parts', complete=True, partial=False) for k in ('inspected', 'certified', 'rejected')})
    working = dict(status='current', requested_hash=HASH, working_hash=HASH, read_only_reason=None)
    return item, review, [], working


def test_complete_current_zero_is_checked_and_readonly():
    args = clean(); before = copy.deepcopy(args)
    result = completion(*args)
    assert result['ready'] and result['remaining_detected_errors'] == 0
    assert result['label'] == '0 remaining detected errors'
    assert result['download'] == f'/api/spreadsheets/download?id={HASH}&checked=1&check={CHECK}'
    assert args == before


@pytest.mark.parametrize('kind', ['open', 'reject', 'defer', 'confirm', 'person', 'person_legacy', 'claude_verify', 'claude_fix', 'corrected'])
def test_set_aside_is_not_fixed_on_this_version(kind):
    item, review, holds, work = clean()
    issue = dict(key='synthetic', severity='medium', code='synthetic', decision=dict(action=kind))
    if kind in ('person', 'person_legacy', 'claude_verify', 'claude_fix', 'corrected'):
        issue['dismissed_by'] = kind; review['dismissed'] = [issue]
    else: review['issues'] = [issue]
    review['coverage'].update(findings_total=1, findings_listed=1, all_findings=1)
    out = completion(item, review, holds, work)
    assert not out['ready'] and out['remaining_detected_errors'] == 1


@pytest.mark.parametrize('bad', ['truncated', 'not_listed', 'unknown', 'duplicate', 'unknown_severity', 'old_checker', 'unverified', 'no_check', 'wrong_bound_hash', 'stale_source', 'unknown_rejects', 'partial_quantity', 'source_gap', 'source_unverified', 'hold', 'expired', 'future', 'no_capture', 'date_contract', 'superseded', 'forked', 'wrong_working_hash', 'readonly', 'approval', 'sent'])
def test_fail_closed(bad):
    i, r, h, w = clean(); rec = i['source_reconciliation']
    if bad == 'truncated': r['coverage']['truncated'] = True
    elif bad == 'not_listed': r['coverage']['not_listed'] = 1
    elif bad == 'unknown': r['coverage'].pop('all_findings')
    elif bad == 'duplicate': r['issues'] = [dict(key='same', severity='info')] * 2; r['coverage'].update(all_findings=2, findings_total=2, findings_listed=2)
    elif bad == 'unknown_severity': r['issues'] = [dict(key='unknown')]; r['coverage'].update(all_findings=1, findings_total=1, findings_listed=1)
    elif bad == 'old_checker': i['checker_version'] = 'old'
    elif bad == 'unverified': i['internal_check_status'] = 'unverified'
    elif bad == 'no_check': i.pop('check_hash')
    elif bad == 'wrong_bound_hash': rec['bound_to']['file_hash'] = 'c' * 64
    elif bad == 'stale_source': rec['stale'] = True
    elif bad == 'unknown_rejects': rec['count_reconciliation']['quantities']['rejected']['report']['unknown_rows'] = 29
    elif bad == 'partial_quantity': r['quantities']['rejected']['complete'] = False
    elif bad == 'source_gap': rec['rows'] = [dict(status='source_day_unconfirmed')]
    elif bad == 'source_unverified': r['sharepoint']['source_verified'] = False
    elif bad == 'hold': h.append(dict(code='source_assignment_changed'))
    elif bad == 'expired': rec['captured_at'] = (datetime.now(timezone.utc) - timedelta(seconds=601)).isoformat()
    elif bad == 'future': rec['captured_at'] = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
    elif bad == 'no_capture': rec.pop('captured_at')
    elif bad == 'date_contract': rec['date_contract'] = dict(expires_at='2000-01-01T00:00:00+00:00')
    elif bad == 'superseded': w['status'] = 'superseded'
    elif bad == 'forked': w['status'] = 'choice'
    elif bad == 'wrong_working_hash': w['working_hash'] = 'c' * 64
    elif bad == 'readonly': w['read_only_reason'] = 'approved'
    elif bad == 'approval': i['approval'] = dict(by='Synthetic')
    elif bad == 'sent': i['sent'] = [dict(sent=True)]
    out = completion(i, r, h, w)
    assert not out['ready'] and out['download'] is None and out['reasons']


def test_informational_and_prior_version_fixes_not_current_errors():
    i, r, h, w = clean()
    i['fixed_from'] = dict(file_hash='c'*64, changes=[dict(cell='A2')])
    r['issues'] = [dict(key='info', severity='info', code='external_hyperlink')]
    r['coverage'].update(all_findings=1, findings_total=1, findings_listed=1)
    assert completion(i, r, h, w)['ready']


# Execute the production download route block directly, without auth/settings/env loading.
source = ast.parse((Path(__file__).parents[1] / 'api/index.py').read_text())
route = next(n for n in ast.walk(source) if isinstance(n, ast.If) and ast.unparse(n.test) == "route == '/api/spreadsheets/download'")
fn = ast.FunctionDef(name='download', args=ast.arguments(posonlyargs=[], args=[ast.arg(arg='self')], kwonlyargs=[], kw_defaults=[], defaults=[]), body=route.body, decorator_list=[])
ns = dict(json=json, parse_qs=parse_qs, urlsplit=urlsplit, re=re, Conflict=ValueError)
exec(compile(ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[])), 'actual_download_route', 'exec'), ns)

class Inbox:
    def __init__(self, directory): pass
    def get(self, hash): return dict(file_hash=hash)
    def download(self, hash):
        assert hash == HASH
        return 'Synthetic checked.xlsx', b'PK\x03\x04SYNTHETIC-EXACT-BYTES'

class Handler:
    download = ns['download']
    allowed = True
    proof = None
    def __init__(self, suffix=''):
        self.path = f'/api/spreadsheets/download?id={HASH}' + suffix
    def store(self): return SimpleNamespace(session=lambda: contextlib.nullcontext('synthetic'))
    def refresh_session_principal(self, inbox): pass
    def can_stored_report(self, inbox, item): return self.allowed
    def report_view(self, inbox, hash):
        assert hash == HASH
        return dict(checked_copy=self.proof or completion(*clean()))
    def fail(self, code, text): return code, text
    def send(self, *args): return args

ns['SpreadsheetInbox'] = Inbox

def test_actual_guarded_download_exact_filename_type_bytes_and_original():
    for suffix in ('', f'&checked=1&check={CHECK}'):
        out = Handler(suffix).download()
        assert out[:3] == (200, b'PK\x03\x04SYNTHETIC-EXACT-BYTES', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        assert out[3]['Content-Disposition'] == 'attachment; filename="Synthetic checked.xlsx"'
        if suffix:
            receipt = json.loads(out[3]['X-PPG-Checked-Copy'])
            assert receipt['protocol'] == 'ppg-checked-copy-v1' and receipt['ready'] is True
            assert receipt['file_hash'] == HASH and receipt['check_hash'] == CHECK
            assert datetime.fromisoformat(receipt['expires_at']) > datetime.now(timezone.utc)
            assert out[3]['Cache-Control'] == 'private, no-store'
        else:
            assert 'X-PPG-Checked-Copy' not in out[3]


def test_actual_guarded_download_denied_opaque():
    h = Handler(f'&checked=1&check={CHECK}'); h.allowed = False
    assert h.download() == (404, 'Report not found.')

@pytest.mark.parametrize('change', ['check', 'file', 'expired', 'fork', 'superseded'])
def test_actual_guarded_download_rejects_state_change_without_bytes(change):
    h = Handler(f'&checked=1&check={CHECK}')
    i, r, holds, w = clean()
    if change == 'check': i['check_hash'] = 'd'*64
    elif change == 'file': i['file_hash'] = 'd'*64
    elif change == 'expired': i['source_reconciliation']['captured_at'] = '2000-01-01T00:00:00+00:00'
    else: w['status'] = 'choice' if change == 'fork' else 'superseded'
    h.proof = completion(i, r, holds, w)
    with pytest.raises(ValueError, match='no longer verified'): h.download()
    assert Handler().download()[0] == 200


def test_canonical_fork_requires_choice_but_explicit_valid_leaf_qualifies():
    from hosting.working_copy import WorkingCopies
    parent, sibling = 'c'*64, 'd'*64
    i, r, h, w = clean()
    bases = {parent:dict(file_hash=parent), HASH:dict(i, fixed_from=dict(file_hash=parent)), sibling:dict(file_hash=sibling, fixed_from=dict(file_hash=parent))}
    histories = {key:[copy.deepcopy(value)] for key,value in bases.items()}
    histories[parent][0]['fixes_applied'] = [dict(fixed_file_hash=HASH), dict(fixed_file_hash=sibling)]
    graph = WorkingCopies(bases, histories)
    assert graph.resolve(parent)['status'] == 'choice'
    assert not completion(i, r, h, graph.resolve(parent))['ready']
    assert graph.resolve(HASH)['status'] == 'current'
    assert completion(i, r, h, graph.resolve(HASH))['ready']


def test_actual_report_view_computes_full_inventory_before_pagination(monkeypatch):
    from hosting import review_summary, registry, sharepoint_jobs
    i, r, holds, w = clean()
    rec = i['source_reconciliation']; rec.update(site_id='synthetic-site', list_id='synthetic-list')
    r['sharepoint'].update(source_evidence_complete=True, status='match')
    r['issues'] = [dict(key=f'finding-{n}', severity='medium') for n in range(1201)]
    r['coverage'].update(findings_total=1201, findings_listed=1201, all_findings=1201)
    monkeypatch.setattr(review_summary, 'summarize', lambda _:copy.deepcopy(r))
    monkeypatch.setattr(registry, 'history', lambda *a:[])
    monkeypatch.setattr(sharepoint_jobs, 'lookup', lambda *a:None)
    method = next(n for n in ast.walk(source) if isinstance(n,ast.FunctionDef) and n.name=='report_view')
    scope = dict(debug_log=lambda:None, review_page_size=lambda:20, findings_binding=lambda *a:{})
    exec(compile(ast.fix_missing_locations(ast.Module(body=[method],type_ignores=[])), 'actual_report_view', 'exec'),scope)
    inbox = SimpleNamespace(get=lambda _:i, repeats_elsewhere=lambda *a,**kw:[], assignment_for=lambda *a:dict(status='verified',site_id='synthetic-site',list_id='synthetic-list'), holds=lambda *a:holds, working_copy=lambda *a:w, setting=lambda *a:None)
    handler = SimpleNamespace(can_stored_report=lambda *a:True)
    out = scope['report_view'](handler,inbox,HASH)
    assert len(out['review']['issues']) == 20
    assert out['checked_copy']['remaining_detected_errors'] == 1201
    assert out['checked_copy']['coverage_complete'] and not out['checked_copy']['ready']


def test_actual_download_rechecks_after_bytes_read(monkeypatch):
    h = Handler(f'&checked=1&check={CHECK}')
    original = Inbox.download
    def change_during_read(inbox, hash):
        data = original(inbox, hash)
        h.proof = dict(completion(*clean()), ready=False)
        return data
    monkeypatch.setattr(Inbox, 'download', change_during_read)
    with pytest.raises(ValueError, match='while reading'): h.download()

@pytest.mark.parametrize('gap', ['verification_gap', 'quantity_formulas_not_evaluated', 'no_data_rows', 'no_quantity_columns_detected'])
def test_info_gaps_are_not_errors_but_prevent_checked_claim(gap):
    i,r,h,w=clean(); r['issues']=[dict(key='info', severity='info', code=gap, verification_gap=gap=='verification_gap')]
    r['coverage'].update(all_findings=1, findings_total=1, findings_listed=1)
    out=completion(i,r,h,w)
    assert not out['ready'] and out['remaining_detected_errors']==0 and out['verification_gaps']==1

@pytest.mark.parametrize('stamp', ['garbage', '2026-11-10T00:00:00', '2099-01-01T00:00:00+00:00'])
def test_invalid_or_future_saved_check_time_refuses(stamp):
    i,r,h,w=clean();i['checked_at']=stamp
    assert not completion(i,r,h,w)['ready']


def test_real_saved_xlsx_bytes_and_history_are_unchanged_by_checked_download(tmp_path, monkeypatch):
    """Real local storage + parser + server functions; exact source receipt is synthetic."""
    import hashlib
    import io
    import json
    import socket
    from openpyxl import Workbook, load_workbook
    from hosting.spreadsheet_inbox import SpreadsheetInbox
    from hosting import registry, sharepoint_jobs
    monkeypatch.setattr(socket.socket, 'connect', lambda *a:pytest.fail('No network allowed'))
    wb=Workbook(); wb.active.title='Synthetic'; wb.active.append(['Part','Inspected','Certified','Rejected']); wb.active.append(['P1',10,10,0])
    buffer=io.BytesIO(); wb.save(buffer); raw=buffer.getvalue()
    box=SpreadsheetInbox(tmp_path)
    saved=box.ingest('Synthetic original.xlsx',raw); fh=saved['file_hash']
    assert hashlib.sha256(raw).hexdigest()==fh
    item=box.get(fh); rec=clean()[0]['source_reconciliation']
    rec.update(site_id='synthetic-site',list_id='synthetic-list');rec['bound_to']['file_hash']=fh
    # Fixture evidence only, no retrieval or customer source. Set the same concrete quantities as workbook.
    pair=rec['count_reconciliation']['quantities']['rejected']
    pair['report']['total']=pair['source']['total']=0
    item['source_reconciliation']=rec
    item['totals']={k:dict(total=v,known_sum=v,unit='parts',missing_count=0) for k,v in [('inspected',10),('certified',10),('rejected',0)]}
    with box.connect() as db: item=box.save_check(db,item)
    monkeypatch.setattr(box,'assignment_for',lambda *a,**kw:dict(status='verified',site_id='synthetic-site',list_id='synthetic-list'))
    monkeypatch.setattr(box,'source_mapping',lambda *a:None)
    monkeypatch.setattr(box,'assignment_context',lambda:dict(snapshot=None))
    monkeypatch.setattr(registry,'history',lambda *a:[])
    monkeypatch.setattr(sharepoint_jobs,'lookup',lambda *a:None)
    method=next(n for n in ast.walk(source) if isinstance(n,ast.FunctionDef) and n.name=='report_view')
    scope=dict(debug_log=lambda:None,review_page_size=lambda:20,findings_binding=lambda *a:{})
    exec(compile(ast.fix_missing_locations(ast.Module(body=[method],type_ignores=[])),'actual_view_storage','exec'),scope)
    class RealHandler(Handler):
        report_view=scope['report_view']
        def can_stored_report(self,*a):return True
    h=RealHandler();h.path=f'/api/spreadsheets/download?id={fh}&checked=1&check={item["check_hash"]}'
    def snapshot():
        with box.connect() as db:
            return [tuple(row) for row in db.execute('SELECT file_hash,check_hash,result_json FROM spreadsheet_checks ORDER BY seq').fetchall()]
    before=snapshot();ns['SpreadsheetInbox']=lambda _:box
    try:
        view=h.report_view(box,fh)
        assert view['checked_copy']['ready'],view['checked_copy']
        response=h.download()
        assert response[0]==200 and response[1]==raw and response[2]=='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        assert load_workbook(io.BytesIO(response[1])).active['B2'].value==10
        assert response[3]['Content-Disposition']=='attachment; filename="Synthetic original.xlsx"'
        assert snapshot()==before and box.download(fh)[1]==raw
    finally:ns['SpreadsheetInbox']=Inbox


@pytest.mark.parametrize("stamp", [None, 123, "garbage", "2026-11-10T00:00:00"])
def test_malformed_source_timestamp_fails_closed(stamp):
    i,r,h,w=clean();i["source_reconciliation"]["captured_at"]=stamp
    assert not completion(i,r,h,w)["ready"]
