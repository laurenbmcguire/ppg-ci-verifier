"""Offline regression tests using invented data, with no external services."""
import copy
import sqlite3
import pytest
from hosting import review_decisions as decisions
from hosting.review_summary import check_run, summarize
from hosting.spreadsheet_inbox import SpreadsheetInbox


def current():
    result = {'file_hash': 'synthetic', 'checker_version': 'test'}
    issues = decisions.assign_ids([
        dict(source='Checks', code='missing_cell', sheet='CSV', cell='A2', text='Missing value', severity='medium'),
        dict(source='Checks', code='missing_cell', sheet='CSV', cell='B2', text='Missing value', severity='medium')])
    mapping = {x['key']: x for x in issues}
    kwargs = dict(ids=list(mapping), action='reject', reason='Invented fixture', request_id='r1', expected_rev=0,
                  expected_fps={k: x['fp'] for k,x in mapping.items()}, expected_run=check_run(result))
    return result, mapping, kwargs


@pytest.mark.parametrize('bad', ['missing_fps', 'stale_fp', 'mixed', 'missing_rev', 'stale_run'])
def test_stale_batch_never_mutates(bad):
    r, m, kw = current()
    if bad == 'missing_fps': kw.pop('expected_fps')
    if bad in ('stale_fp', 'mixed'): kw['expected_fps'][next(iter(m))] = 'old'
    if bad == 'missing_rev': kw.pop('expected_rev')
    if bad == 'stale_run': kw['expected_run'] = dict(kw['expected_run'], checker_version='older')
    before = copy.deepcopy(r)
    with pytest.raises(decisions.DecisionConflict): decisions.decide(r, m, **kw)
    assert r == before


def test_original_request_replays_before_stale_checks():
    r, m, kw = current()
    first = decisions.decide(r, m, **kw)
    r['checker_version'] = 'changed-after-the-original-request'
    r['finding_decisions_rev'] = 9
    replay = decisions.decide(r, {}, **kw)
    assert replay['decision_id'] == first['decision_id'] and replay['replayed']
    assert r['finding_decisions_rev'] == 9


@pytest.fixture
def box(tmp_path):
    b = SpreadsheetInbox(tmp_path)
    item = b.ingest('synthetic.csv', b'Part,Inspected,Rejected\nTEST-001,10,1\n')
    return b, item


def approve(b, i, **kw):
    return b.approve(i['file_hash'], 'Synthetic reviewer', note='Synthetic review', override=True,
                     expected_check_hash=i['check_hash'], expected_run=check_run(i), **kw)


def test_approval_validates_locked_revision(box, monkeypatch):
    b,i = box
    original = b._update
    def interleave(h, callback):
        original(h, lambda r:r.update(finding_decisions_rev=1))
        return original(h, callback)
    monkeypatch.setattr(b, '_update', interleave)
    with pytest.raises(ValueError, match='changed'): approve(b,i)
    assert not b.get(i['file_hash']).get('approval')


def test_approval_replay_and_withdrawal(box):
    b,i = box
    approved = approve(b,i)
    retry = approve(b,i)
    assert retry['check_hash'] == approved['check_hash']
    withdrawn = b.approve(i['file_hash'], 'Synthetic reviewer', withdraw=True)
    assert not withdrawn.get('approval')
    assert withdrawn['approval_withdrawn']


def test_withdrawal_validates_locked_sent_state(box, monkeypatch):
    b,i = box
    approve(b,i)
    original = b._update
    def interleave(h, callback):
        original(h, lambda r:r.update(sent=[{'sent':True}]))
        return original(h, callback)
    monkeypatch.setattr(b,'_update', interleave)
    with pytest.raises(ValueError, match='sent'): b.approve(i['file_hash'], 'Reviewer', withdraw=True)
    assert b.get(i['file_hash']).get('approval')


def test_decision_validates_locked_approval(box, monkeypatch):
    b,i=box
    rv=summarize(i); issue={'key':'synthetic-issue','fp':'displayed'}
    original=b._update
    def interleave(h, callback):
        original(h,lambda r:r.update(approval={'by':'Other reviewer'}))
        return original(h,callback)
    monkeypatch.setattr(b,'_update',interleave)
    with pytest.raises(decisions.DecisionConflict,match='approved'):
        b.decide_findings(i['file_hash'], ids=[issue['key']],action='confirm',request_id='race',expected_rev=0,
                         expected_fps={issue['key']:issue['fp']},expected_run=rv['run'])
    assert not b.get(i['file_hash']).get('finding_decisions')


def change(**extra):
    return dict(sheet='CSV',cell='A2',before='TEST-001',after='TEST-002',reason='Synthetic correction',**extra)


def rows(b):
    with b.connect() as db:
        return db.execute('SELECT count(*) FROM spreadsheets').fetchone()[0]


@pytest.mark.parametrize('state', ['approval','sent','defer','reject','stale_snapshot','incomplete_group','missing_snapshot','stale_check'])
def test_correction_rejected_before_ingest(box, monkeypatch, state):
    if state=='incomplete_group':
        monkeypatch.setattr('hosting.reconcile.current', lambda item: {'snapshot_id':'snap'})
    b,i=box; changes=[change()]; kwargs={}
    if state=='approval': b._update(i['file_hash'],lambda r:r.update(approval={'by':'Reviewer'}))
    if state=='sent': b._update(i['file_hash'],lambda r:r.update(sent=[{'sent':True}]))
    if state in ('defer','reject'):
        b._update(i['file_hash'],lambda r:r.update(finding_decisions={'x':dict(action=state,sheet='CSV',cell='A2')}))
    if state=='stale_snapshot': changes=[change(snapshot_id='old')]
    if state=='incomplete_group': changes=[change(snapshot_id='snap',change_set='set',change_set_size=2)]
    if state=='missing_snapshot': changes=[change(source='SharePoint')]
    if state=='stale_check': kwargs={'expected_check_hash':'old'}
    before=b.get(i['file_hash'])
    kwargs.setdefault('expected_check_hash',before['check_hash'])
    called=[]
    monkeypatch.setattr(b,'ingest',lambda *a,**kw:called.append(a))
    with pytest.raises(ValueError): b.apply_fixes(i['file_hash'],changes,**kwargs)
    assert not called and rows(b)==1 and b.get(i['file_hash'])==before


def test_manual_correction_atomic_and_original_immutable(box):
    b,i=box; raw=b.original_bytes(i['file_hash'])[1]
    fixed=b.apply_fixes(i['file_hash'],[change()],expected_check_hash=i['check_hash'])
    assert rows(b)==2 and fixed['fixed_from']['file_hash']==i['file_hash']
    assert b.original_bytes(i['file_hash'])[1]==raw
    assert b'TEST-002' in b.original_bytes(fixed['file_hash'])[1]
    assert b.apply_fixes(i['file_hash'],[change()],expected_check_hash=b.get(i['file_hash'])['check_hash'])['already_applied']


def test_correction_rolls_back_database_on_lineage_failure(box, monkeypatch):
    b,i=box; original=b.save_check
    def fail(db,r):
        if r.get('fixed_from'): raise ValueError('Injected transaction failure')
        return original(db,r)
    monkeypatch.setattr(b,'save_check',fail)
    with pytest.raises(ValueError,match='Injected'): b.apply_fixes(i['file_hash'],[change()],expected_check_hash=b.get(i['file_hash'])['check_hash'])
    assert rows(b)==1 and not b.get(i['file_hash']).get('fixes_applied')


@pytest.mark.parametrize('which',['auto','ai','legacy','revert_cells','revert_fine'])
def test_unscoped_paths_fail_closed_without_external_calls(box, which):
    from hosting import autofix, ai_fix
    b,i=box
    before=b.get(i['file_hash'])
    with pytest.raises(ValueError):
        if which=='auto': autofix.step(object(),i['file_hash'],'sure')
        elif which=='ai': ai_fix.run(object(),i['file_hash'])
        elif which=='revert_cells': autofix.revert(object(),i['file_hash'],cells=[{'sheet':'CSV','cell':'A2'}])
        elif which=='revert_fine': autofix.revert(object(),i['file_hash'],fine=['Synthetic finding'])
        else: b.record_decisions(i['file_hash'],['x'],'reject','Reason','Reviewer')
    assert rows(b)==1 and b.get(i['file_hash'])==before


def test_duplicate_cell_cannot_fake_complete_change_set(box, monkeypatch):
    b,i=box
    monkeypatch.setattr('hosting.reconcile.current',lambda item:{'snapshot_id':'snap'})
    c=change(snapshot_id='snap',change_set='group',change_set_size=2)
    with pytest.raises(ValueError,match='same cell twice'): b.apply_fixes(i['file_hash'],[c,c],expected_check_hash=i['check_hash'])
    assert rows(b)==1


@pytest.mark.parametrize('protected',['decisions','hold','revision'])
def test_internal_automatic_guard_runs_before_ingest(box, monkeypatch, protected):
    b,i=box
    monkeypatch.setattr(b,'holds',lambda item: [{'text':'Unresolved source'}] if protected=='hold' else [])
    if protected=='decisions':
        b._update(i['file_hash'],lambda r:r.update(finding_decisions={'x':{'action':'confirm'}}))
    shown=b.get(i['file_hash'])
    expected='old' if protected=='revision' else shown['check_hash']
    with pytest.raises(ValueError): b.apply_fixes(i['file_hash'],[change()],automatic=True,expected_check_hash=expected)
    assert rows(b)==1 and b.get(i['file_hash'])==shown


def test_approval_replay_refuses_any_subsequent_review_revision(box):
    b,i=box
    approve(b,i)
    b._update(i['file_hash'],lambda r:r.update(source_reconciliation={'status':'incomplete','coverage':{'notes':['New hold']}}))
    before=b.get(i['file_hash'])
    with pytest.raises(ValueError,match='changed'): approve(b,i)
    assert b.get(i['file_hash'])==before


def test_manual_corrections_require_displayed_binding(box):
    b,i=box
    with pytest.raises(ValueError,match='changed'): b.apply_fixes(i['file_hash'],[change()])
    assert rows(b)==1


def test_approval_replay_rechecks_holds_from_shared_settings(box, monkeypatch):
    b,i=box
    approve(b,i)
    monkeypatch.setattr(b,'holds',lambda *args:[{'code':'client_not_settled','hard':True,'text':'Changed assignment'}])
    with pytest.raises(ValueError,match='holds changed'): approve(b,i)


def test_summary_marks_approval_stale_after_review_or_hold_change(box):
    b,i=box
    approved=approve(b,i)
    assert summarize(approved)['approval_current'] is True
    changed=b._update(i['file_hash'],lambda r:r.update(ai_review={'status':'done','summary':'New synthetic finding'}))
    assert summarize(changed)['approval_current'] is False
    assert changed['approval']==approved['approval']  # historical approval remains on record
