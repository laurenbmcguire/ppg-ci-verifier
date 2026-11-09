"""Offline saved-workbook reproductions of bounded static-review findings."""
import shutil
from unittest.mock import patch
import pytest
from hosting import reconcile
from hosting.spreadsheet_inbox import SpreadsheetInbox
from tests.test_correction_workflow import reviewed
from tests.review_request_fixture import apply_displayed
from tests.test_r8_mapping_guards import MappingGuards
from tests.test_r8_count_contract import fixture, DAY
from tests.test_static_review_regressions import stored, finding
from hosting.review_summary import summarize


def test_manual_edit_cannot_retain_source_provenance():
    box,h,raw,props=reviewed()
    try:
        edit=dict(props[0],source='Your edit',after='999')
        original_edit=dict(edit)
        child=apply_displayed(box,h,[edit])
        assert edit==original_edit
        saved=box.get(h)['fixes_applied'][-1]['changes'][0]
        assert saved['source']=='Your edit'
        assert not any(saved.get(k) for k in ('snapshot_id','item','field','change_set','change_set_size'))
        assert child['file_hash']!=h
        assert box.original_bytes(h)[1]==raw
        assert not summarize(child)['sharepoint']['source_verified']
    finally:shutil.rmtree(box.root,ignore_errors=True)


def test_old_capture_without_pre_capture_hash_cannot_replace_newer():
    case=MappingGuards();case.setUp()
    try:
        old_hash=case.box.get(case.h)['check_hash']
        fresh=case.capture();before=case.box.get(case.h)
        records,older=fixture();older['snapshot_id']='older-capture'
        with patch('hosting.spreadsheets.report_and_export_records',return_value=(records,[])):
            with pytest.raises(ValueError,match='displayed check hash'):
                case.box.source_reconcile(case.h,older,DAY)
            with pytest.raises(ValueError,match='checks changed'):
                case.box.source_reconcile(case.h,older,DAY,expected_check_hash=old_hash)
        assert case.box.get(case.h)==before
    finally:case.doCleanups()


@pytest.mark.parametrize('mode',['stale','old_checker','other_file'])
def test_column_confirmation_refuses_noncurrent_receipt(mode):
    case=MappingGuards();case.setUp()
    try:
        result=case.capture()
        def corrupt(r):
            rec=r['source_reconciliation']
            if mode=='stale':rec['stale']=True
            elif mode=='old_checker':r['checker_version']=rec['bound_to']['checker_version']='historical-checker'
            else:rec['bound_to']['file_hash']='other-file'
        current=case.box._update(case.h,corrupt);rec=current['source_reconciliation']
        with patch.object(SpreadsheetInbox,'assignment_for',return_value=case.a):
            with pytest.raises(ValueError,match='comparison changed'):
                case.box.confirm_columns('lid',{'rejected':'R'},'reviewer',site_id='sid',expected_mapping_revision=None,file_hash=case.h,expected_check_hash=current['check_hash'],expected_snapshot_id=rec['snapshot_id'])
        assert case.box.source_mapping('sid','lid')['revision'] is None
    finally:case.doCleanups()


def test_ambiguous_date_only_timestamp_is_not_silently_previous_day():
    assert reconcile.business_day('2026-11-07T00:00:00Z',True) is None
    assert reconcile.business_day('2026-11-07T06:00:00Z',True)=='2026-11-07'
    assert reconcile.business_day('2026-07-07T05:00:00Z',True)=='2026-07-07'
    assert reconcile.business_day('2026-11-07',True)=='2026-11-07'
    assert reconcile.business_day('2026-11-07T00:00:00Z',False)=='2026-11-06'


def test_historical_review_decision_does_not_hide_changed_child_value(stored):
    box,item,raw=stored;issue=finding(item);h=item['file_hash']
    saved=box._update(h,lambda r:r.update(review_decisions={issue['key']:{'action':'keep','note':'Historical context'}}))
    child=box.apply_fixes(h,[dict(sheet='Inspection Detail',cell='L5',before='072',after='075',kind='text',source='Your edit')],expected_check_hash=saved['check_hash'])
    changed=finding(child)
    assert changed['fp']!=issue['fp']
    assert changed['key'] in {x['key'] for x in summarize(child)['issues']}
    assert child['review_decisions']==saved['review_decisions']
