"""Today and period activity must use captured exact-source date evidence."""
from copy import deepcopy
from datetime import date,datetime,timezone,timedelta
import pytest
from hosting import periods,sharepoint,source_date_contracts as contracts
SITE=contracts.SITE;LIST=contracts.LIST
COL={'Date':{'id':'00000000-0000-0000-0000-000000000001','name':'Date','dateTime':{'format':'dateOnly'}}}

def fixture(monkeypatch,values):
    data=('Synthetic',{'id':LIST,'displayName':'Synthetic','_capture':{'site_id':SITE,'list_id':LIST,'columns':list(COL.values())}},
          {'Date':'Date','Quantity':'Quantity Inspected','OKQty':'Quantity Certified','NOKQty':'Reject Quantity'},
          [{'id':str(n),'fields':{'Date':v,'Date0':'2026-11-10T17:00:00Z','Quantity':10,'OKQty':10,'NOKQty':0}} for n,v in enumerate(values,1)])
    monkeypatch.setattr(sharepoint,'configured',lambda:True)
    monkeypatch.setattr(sharepoint,'_list_items',lambda *a,**k:data)
    monkeypatch.setattr('hosting.photo_embed.certificate_ready',lambda:True)
    monkeypatch.setattr(contracts,'_site_url',lambda *a,**k:contracts.SITE_URL)
    monkeypatch.setattr('hosting.photo_embed._get',lambda *a,**k:(200,deepcopy(contracts.EXPECTED_TIMEZONE)))
    return data

@pytest.mark.parametrize('day,values',[
 ('2026-11-10',['2026-11-10T06:00:00Z','2026-11-09T06:00:00Z']),
 ('2026-07-10',['2026-07-10T05:00:00Z','2026-07-09T05:00:00Z']),
 ('2026-11-10',['2026-11-10','2026-11-09']),
])
def test_today_and_period_refresh_exact_metadata_without_editing(monkeypatch,day,values):
    data=fixture(monkeypatch,values);before=deepcopy(data);calls=[];real=contracts.capture_contract
    def capture(site,list_id,columns):
        calls.append((site,list_id,deepcopy(columns)));return real(site,list_id,columns)
    monkeypatch.setattr(contracts,'capture_contract',capture)
    out=periods.day_scans('synthetic',day)
    assert out['today']==1 and out['yesterday']==1 and not out.get('error')
    days,note=periods.sharepoint_days('synthetic',date.fromisoformat(day),date.fromisoformat(day))
    assert days[day]['scans']==1 and days[day]['inspected']==10
    assert calls==[(SITE,LIST,COL),(SITE,LIST,COL)] and data==before

@pytest.mark.parametrize('kind',['denied','stale','wrong_list','wrong_column','inconsistent_time','missing_date','missing_metadata'])
def test_unknown_evidence_never_becomes_zero(monkeypatch,kind):
    data=fixture(monkeypatch,['2026-11-10T06:00:00Z'])
    real=contracts.capture_contract
    contract=real(SITE,LIST,COL)
    if kind=='denied':contract=None
    elif kind=='stale':
        contract['captured_at']=(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat()
        contract['expires_at']=(datetime.now(timezone.utc)-timedelta(minutes=50)).isoformat()
    elif kind=='wrong_list':contract['list_id']='00000000-0000-0000-0000-000000000002'
    elif kind=='wrong_column':contract['column_id']='00000000-0000-0000-0000-000000000002'
    elif kind=='inconsistent_time':data[3][0]['fields']['Date']='2026-11-10T00:00:00Z'
    elif kind=='missing_date':data[3][0]['fields'].pop('Date')
    elif kind=='missing_metadata':data[1].pop('_capture')
    monkeypatch.setattr(contracts,'capture_contract',lambda *a,**k:deepcopy(contract))
    out=periods.day_scans('synthetic','2026-11-10')
    assert out['today'] is None and out['yesterday'] is None and out['complete'] is False
    days,note=periods.sharepoint_days('synthetic',date(2026,11,9),date(2026,11,10))
    assert days=={} and 'unknown' in note

@pytest.mark.parametrize('kind',['wrong_list','duplicate_columns'])
def test_invalid_capture_identity_stays_blocked(monkeypatch,kind):
    data=fixture(monkeypatch,['2026-11-10T06:00:00Z'])
    if kind=='wrong_list':data[1]['id']='different'
    else:data[1]['_capture']['columns']*=2
    with pytest.raises(sharepoint.SharePointError):periods.day_scans('synthetic','2026-11-10')
    days,note=periods.sharepoint_days('synthetic',date(2026,11,9),date(2026,11,10))
    assert days=={} and 'unknown' in note

def test_ambiguous_job_stays_blocked_without_selecting_a_list(monkeypatch):
    fixture(monkeypatch,[])
    def ambiguous(*a,**k):raise sharepoint.SharePointError('Ambiguous SharePoint job: select exact source.')
    monkeypatch.setattr(sharepoint,'_list_items',ambiguous)
    with pytest.raises(sharepoint.SharePointError,match='Ambiguous'):periods.day_scans('2207','2026-11-10')
