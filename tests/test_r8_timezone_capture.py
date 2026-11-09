"""Real capture/selection paths with synthetic Graph replies; no live services."""
import copy
from unittest.mock import patch
import pytest
from hosting import reconcile, sharepoint
from tests.test_reconcile import ALL, COLS, snap, run, sl_daily
from tests import test_sharepoint_list as list_fixture


@pytest.mark.parametrize('stamp',['2026-11-06T06:00:00Z','2026-07-06T05:00:00Z','2026-11-07T00:00:00Z','2026-11-06T00:00:00-06:00','2026-11-06T00:00:00'])
@pytest.mark.parametrize('fmt',['dateOnly','dateTime',None])
def test_capture_timestamp_never_infers_source_timezone(stamp,fmt):
    items=copy.deepcopy(ALL);items[0]['fields']['Date']=stamp;before=copy.deepcopy(items)
    columns=copy.deepcopy(COLS);columns[0]['dateTime']={'format':fmt} if fmt else {}
    captured=snap([items],columns=columns)
    assert items==before
    assert captured['items'][0]['raw']['Date']==stamp
    assert captured['items'][0]['original']==items[0]
    assert captured['items'][0]['day'] is None
    assert captured['items'][0]['day_basis']=='timezone_unconfirmed'
    assert captured['coverage']['items']==5
    assert captured['coverage']['undated']==1
    assert captured['coverage']['timezone_unconfirmed']==1
    result=run(sl_daily(),captured)
    assert not result['verified']
    assert result['count_reconciliation']['status']=='held'
    assert not result['count_reconciliation']['exclusions']
    assert any('timezone contract' in reason for reason in result['not_verified_because'])


@pytest.mark.parametrize('fmt',['dateOnly','dateTime',None])
def test_plain_dates_allow_verified_capture_and_only_evidenced_exclusions(fmt):
    items=copy.deepcopy(ALL)
    extra=copy.deepcopy(items[0]);extra['id']='999';extra['fields']['Date']='2026-11-05';items.append(extra)
    columns=copy.deepcopy(COLS);columns[0]['dateTime']={'format':fmt} if fmt else {}
    captured=snap([items],columns=columns);result=run(sl_daily(),captured)
    assert captured['coverage']['undated']==0
    assert result['verified']
    assert [e['item'] for e in result['count_reconciliation']['exclusions']]==['999']


@pytest.mark.parametrize('method',['list_day','list_history'])
def test_list_selection_refuses_mixed_unknown_timezone_without_losing_raw(method):
    items=copy.deepcopy(list_fixture.ITEMS);items[0]['fields']['Date']='2026-10-02T05:00:00Z';before=copy.deepcopy(items)
    with patch.object(sharepoint,'configured',return_value=True),patch.object(sharepoint,'_token_value',return_value='synthetic'),patch.object(list_fixture,'ITEMS',items):
        with pytest.raises(sharepoint.SharePointError,match='timezone contract'):
            getattr(sharepoint,method)(list_fixture.JOB,'2026-10-05',opener=list_fixture.FakeGraph(),site_id='site-Hyundai_Montgomery',list_id='L1')
    assert items==before


def test_local_midnight_helper_is_compatibility_not_production_verification():
    assert reconcile.business_day('2026-11-06T06:00:00Z',True)=='2026-11-06'
    assert reconcile.item_day({'Date':'2026-11-06T06:00:00Z'},{'Date':{'dateTime':{'format':'dateOnly'}}})==(None,'timezone_unconfirmed')
    assert reconcile.item_day({'Date':'2026-11-06','Date0':'2026-11-07T07:00:00Z'}, {})==('2026-11-06','business_date')
