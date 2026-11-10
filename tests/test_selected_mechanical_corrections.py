"""Synthetic selected corrections: actual bytes, checks and guarded saves; no provider/network."""
import copy
import socket
import pytest
from hosting import ai_proposals as ap, selected_corrections as sc
from hosting.spreadsheet_inbox import SpreadsheetInbox
from hosting.review_summary import summarize, check_run
from hosting.fixes import FixError
from tests.eos_fixture import build, ROWS

@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(socket.socket, 'connect', lambda *a: pytest.fail('Network forbidden'))
    box = SpreadsheetInbox(tmp_path)
    rows = [list(r) for r in ROWS]
    rows[1][4] = 'Holes /  Glue '
    rows[2][4] = 'Holes /  Glue '
    raw = build([tuple(r) for r in rows])
    item = box.ingest('2230-SYNTHETIC - EOS - 10.07.26.xlsx', raw)
    fixes = [f for f in box.fix_proposals(item['file_hash'])['proposals'] if f['source'] == 'Clean-up']
    assert len(fixes) == 2
    findings = [dict(code='synthetic_spacing', sheet=p['sheet'], cell=p['cell'], row=int(p['cell'][1:]),
        message='Synthetic whitespace review', observed=[dict(type='str', value=p['before'])]) for p in fixes]
    item = box._update(item['file_hash'], lambda r: r.update(findings=findings,
        finding_counts={'synthetic_spacing': 2}, finding_count=2, findings_truncated=False))
    issues = [i for i in summarize(item)['issues'] if i.get('code') == 'synthetic_spacing']
    assert len(issues) == 2
    payload = dict(file_hash=item['file_hash'], expected_check_hash=item['check_hash'], run=check_run(item),
        expected_rev=0, ids=[i['key'] for i in issues], seen={i['key']: i['fp'] for i in issues})
    return box, item, raw, payload, issues


def prepared(setup):
    box, item, raw, payload, issues = setup
    return ap.prepare(box, payload, lambda: True, lambda rows: pytest.fail('Mechanical cells must not reach provider'))['proposals']


def test_real_multiple_mechanical_corrections_preserve_original(setup):
    box, item, raw, payload, issues = setup
    before = box.get(item['file_hash'])
    fixes = prepared(setup)
    assert len(fixes) == 2
    assert all(p['source'] == 'Clean-up' and p['sure'] is True and p['recommendation_eligible'] is False for p in fixes)
    assert box.get(item['file_hash']) == before
    saved = box.apply_fixes(item['file_hash'], fixes, expected_check_hash=item['check_hash'])
    assert saved['file_hash'] != item['file_hash'] and saved['revision'] == 2
    assert len(saved['fixed_from']['changes']) == 2
    assert box.original_bytes(item['file_hash'])[1] == raw
    assert not saved.get('approval') and not saved.get('sent')

@pytest.mark.parametrize('field,value', [('selected_finding_id','forged'), ('selected_finding_fp','stale'),
    ('id','fake'), ('sheet','Summary'), ('cell','A1'), ('before','wrong'), ('after','invented'),
    ('kind','number'), ('source','AI fix (asked)'), ('snapshot_id','invented'),
    ('item','invented'), ('field','invented'), ('change_set','atomic'), ('change_set_size',2)])
def test_forged_selected_correction_refused(setup, field, value):
    box, item, raw, payload, issues = setup
    fixes = prepared(setup)
    fixes[0][field] = value
    with pytest.raises((ValueError, FixError)):
        box.apply_fixes(item['file_hash'], fixes, expected_check_hash=item['check_hash'])
    assert box.original_bytes(item['file_hash'])[1] == raw and box.list()['count'] == 1

@pytest.mark.parametrize('action', ['reject','defer','confirm'])
def test_decision_added_after_preparation_blocks_selected_path(setup, action):
    box, item, raw, payload, issues = setup
    fixes = prepared(setup)
    issue = issues[0]
    box._update(item['file_hash'], lambda r: r.update(review_decisions={issue['key']: {'action': action}}))
    current = box.get(item['file_hash'])
    with pytest.raises((ValueError, FixError)):
        box.apply_fixes(item['file_hash'], fixes, expected_check_hash=current['check_hash'])
    assert box.list()['count'] == 1


def test_stale_check_and_conflicting_batch_refused(setup):
    box, item, raw, payload, issues = setup
    fixes = prepared(setup)
    with pytest.raises((ValueError, FixError)):
        box.apply_fixes(item['file_hash'], fixes, expected_check_hash='stale')
    with pytest.raises((ValueError, FixError)):
        box.apply_fixes(item['file_hash'], [fixes[0],dict(fixes[0],after='conflict')], expected_check_hash=item['check_hash'])
    assert box.list()['count'] == 1


def test_mechanical_source_owned_cell_not_allowlisted(setup, monkeypatch):
    box, item, raw, payload, issues = setup
    original = sc.summarize
    def owned(*args, **kwargs):
        review = original(*args, **kwargs)
        for issue in review['issues']:
            issue['source_value'] = 'source value'
        return review
    monkeypatch.setattr(sc, 'summarize', owned)
    assert sc.candidates(item, raw) == {}


def test_ambiguous_or_guessed_proposals_not_allowlisted(setup, monkeypatch):
    box, item, raw, payload, issues = setup
    original = sc.proposals
    existing = original(item, item['filename'], raw)
    monkeypatch.setattr(sc, 'proposals', lambda *a: existing + copy.deepcopy(existing))
    assert sc.candidates(item, raw) == {}
    monkeypatch.setattr(sc, 'proposals', lambda *a: [dict(p, source='Suggested', sure=False) for p in existing])
    assert sc.candidates(item, raw) == {}


def test_atomic_proposal_not_allowlisted(setup, monkeypatch):
    box, item, raw, payload, issues = setup
    existing = sc.proposals(item, item['filename'], raw)
    monkeypatch.setattr(sc, 'proposals', lambda *a: [dict(p, change_set='whole',change_set_size=2) for p in existing])
    assert sc.candidates(item, raw) == {}

@pytest.mark.parametrize('mode', ['success', 'timeout', 'omitted'])
def test_mixed_selection_keeps_supported_corrections(setup, mode):
    box, item, raw, payload, issues = setup
    findings = copy.deepcopy(item['findings'])
    findings.append(dict(code='synthetic_review', sheet='1st Shift', cell='L18', row=18,
        message='Synthetic explicit text review', observed=[dict(type='str', value=ROWS[2][5])]))
    item = box._update(item['file_hash'], lambda r: r.update(findings=findings,
        finding_counts={'synthetic_spacing':2,'synthetic_review':1}, finding_count=3))
    issues = [i for i in summarize(item)['issues'] if i.get('code') in ('synthetic_spacing','synthetic_review')]
    payload.update(expected_check_hash=item['check_hash'], run=check_run(item), ids=[i['key'] for i in issues], seen={i['key']:i['fp'] for i in issues})
    calls=[]
    def provider(rows):
        calls.append(rows)
        assert len(rows)==1 and rows[0]['cell']=='L18'
        if mode=='timeout': raise TimeoutError()
        if mode=='omitted': return {'fixes':[]}
        return {'fixes':[dict((k,rows[0][k]) for k in ('finding_id','sheet','cell','before')) | dict(after='Synthetic verified text',reason='Synthetic provider response')]}
    out=ap.prepare(box,payload,lambda:True,provider)
    assert len(calls)==1
    assert len(out['proposals'])==(3 if mode=='success' else 2)
    assert sum(p['source']=='Clean-up' for p in out['proposals'])==2
    saved=box.apply_fixes(item['file_hash'],out['proposals'],expected_check_hash=item['check_hash'])
    assert len(saved['fixed_from']['changes'])==len(out['proposals'])
    assert box.original_bytes(item['file_hash'])[1]==raw


def test_mechanical_binding_change_during_other_provider_discards_all(setup):
    box,item,raw,payload,issues=setup
    original_snapshot=ap.snapshot
    count=[0]
    # Keep the real reads and guards; fabricate only an intervening stored report change.
    def snapshot(*args,**kwargs):
        count[0]+=1
        if count[0]==2:
            box._update(item['file_hash'],lambda r:r.update(review_request={'status':'waiting'}))
        return original_snapshot(*args,**kwargs)
    from unittest.mock import patch
    with patch.object(ap,'snapshot',snapshot):
        with pytest.raises(ValueError):
            prepared(setup)
    assert box.list()['count']==1 and box.original_bytes(item['file_hash'])[1]==raw


def test_saved_correction_preserves_source_requirement_as_stale(setup):
    from hosting import reconcile
    box,item,raw,payload,issues=setup
    rec=dict(status='incomplete',verified=False,coverage={'complete':False},
        not_verified_because=['Synthetic missing source evidence'],
        bound_to={'file_hash':item['file_hash'],'checker_version':item['checker_version']})
    item=box._update(item['file_hash'],lambda r:r.update(source_reconciliation=rec))
    payload.update(expected_check_hash=item['check_hash'],run=check_run(item))
    out=ap.prepare(box,payload,lambda:True,lambda rows:pytest.fail('No provider'))
    assert len(out['proposals'])==2 and box.holds(item)
    saved=box.apply_fixes(item['file_hash'],out['proposals'],expected_check_hash=item['check_hash'])
    assert box.holds(saved)
    assert saved['source_reconciliation']['stale'] is True
    assert saved['source_reconciliation']['verified'] is False
    assert saved['source_reconciliation']['bound_to']==rec['bound_to']
    assert reconcile.current(saved) is None
    assert box.original_bytes(item['file_hash'])[1]==raw
    assert not saved.get('approval')
    # Every source consumer must still refuse to treat this saved copy as verified.
    from hosting import change_summary
    review=summarize(saved)
    assert review['sharepoint']['source_verified'] is False
    assert review['sharepoint']['status']=='stale'
    assert change_summary.source_receipt(saved,'2026-10-07')['status']=='held'
    assert not any(p.get('source','').startswith('SharePoint') for p in box.fix_proposals(saved['file_hash'])['proposals'])
    with pytest.raises(ValueError):
        box.approve(saved['file_hash'],'Synthetic reviewer',override=True,
                    expected_check_hash=saved['check_hash'],expected_run=check_run(saved))
    failed=box.record_lookup(saved['file_hash'],'error','Synthetic source check failed')
    assert failed['source_reconciliation']['stale'] is True
    assert failed['source_reconciliation']['bound_to']==rec['bound_to']
    assert box.holds(failed) and reconcile.current(failed) is None
    assert change_summary.source_receipt(failed,'2026-10-07')['status']=='held'

    # Cross-feature contract: the actual saved child has no checked download until
    # its own source comparison qualifies. A later failed lookup cannot clear this.
    from hosting.checked_copy import completion
    for child in (saved, failed):
        proof = completion(child, summarize(child), box.holds(child), box.working_copy(child['file_hash']))
        assert proof['ready'] is False and proof['download'] is None
        assert proof['label'] == 'Verification not complete'
        assert 'A complete current exact-source comparison is required.' in proof['reasons']
        assert 'Report or source verification holds remain. Review the listed holds.' in proof['reasons']
        assert child['source_reconciliation']['bound_to']['file_hash'] != child['file_hash']
