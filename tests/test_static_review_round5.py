"""Synthetic audit-provenance and whole-batch regressions."""
from unittest.mock import patch
import pytest
from tests.test_correction_workflow import ChangeSetsAreWhole
from tests.review_request_fixture import apply_displayed

@pytest.fixture
def case():
    c=ChangeSetsAreWhole();c.setUp()
    yield c
    c.doCleanups()

@pytest.mark.parametrize('label',['Manual edit','Your edit ','your edit'])
def test_manual_labels_cannot_keep_partial_source_provenance(case,label):
    change=dict(case.change_set()[0],source=label,item='forged',field='inspected')
    change.pop('change_set');change.pop('change_set_size')
    apply_displayed(case.box,case.h,[change])
    saved=case.box.get(case.h)['fixes_applied'][-1]['changes'][0]
    assert not saved.get('item') and not saved.get('field')


def test_forged_revert_is_refused_without_creating_version(case):
    change=dict(case.change_set()[0],source='Reverted by reviewer',snapshot_id='export:'+case.h,item='forged',field='inspected')
    change.pop('change_set');change.pop('change_set_size')
    before=case.box.get(case.h)
    with pytest.raises(ValueError,match='Revert'):
        apply_displayed(case.box,case.h,[change])
    assert case.box.get(case.h)==before
    assert case.box.original_bytes(case.h)[1]==case.raw


def test_manual_override_keeps_atomic_membership_without_source_claim(case):
    proposals=[dict(c,source='SharePoint export',snapshot_id='export:'+case.h,item='433',field='field'+str(i)) for i,c in enumerate(case.change_set())]
    proposals[2]['after']='Damaged'  # a synthetic source value, not the reserved NOK column label
    selected=[dict(p) for p in proposals];selected[0]['source']='Your edit';selected[0]['after']='78'
    before=[dict(p) for p in selected]
    with patch('hosting.fixes.proposals',return_value=proposals):
        child=apply_displayed(case.box,case.h,selected)
    assert selected==before
    saved=case.box.get(case.h)['fixes_applied'][-1]['changes']
    assert len(saved)==4 and all(c['change_set']=='set-5' and c['change_set_size']==4 for c in saved)
    assert not saved[0].get('snapshot_id') and not saved[0].get('item') and not saved[0].get('field')
    assert all(c['snapshot_id']=='export:'+case.h for c in saved[1:])
    assert child['file_hash']!=case.h


def test_missing_atomic_size_refused_before_workbook_generation(case):
    changes=case.change_set()
    for c in changes:c.pop('change_set_size')
    with patch('hosting.fixes.apply_fixes',side_effect=AssertionError('must reject before generating')):
        with pytest.raises(ValueError,match='size'):
            apply_displayed(case.box,case.h,changes)


@pytest.mark.parametrize('identifier',[[],{},['set'],{'set':1},True,42,'','   ','x'*301])
def test_malformed_atomic_identifier_refused_before_generation(case,identifier):
    changes=case.change_set()
    for c in changes:c['change_set']=identifier
    before=case.box.get(case.h)
    with patch('hosting.fixes.apply_fixes',side_effect=AssertionError('must reject before generating')):
        with pytest.raises(ValueError,match='identifier'):
            apply_displayed(case.box,case.h,changes)
    assert case.box.get(case.h)==before

@pytest.mark.parametrize('size',[None,0,-1,True,'4',4.0,[],{}])
def test_invalid_atomic_size_refused_before_generation(case,size):
    changes=case.change_set()
    for c in changes:c['change_set_size']=size
    with patch('hosting.fixes.apply_fixes',side_effect=AssertionError('must reject before generating')):
        with pytest.raises(ValueError,match='size'):
            apply_displayed(case.box,case.h,changes)
