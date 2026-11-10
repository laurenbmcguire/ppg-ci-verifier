"""Synthetic source-date ambiguity never proves a missing or wrong-day record."""
from copy import deepcopy
from datetime import datetime, timezone
from hosting import source_date_contracts as contracts
import pytest
from hosting import reconcile
from tests.test_reconcile import ALL, sp_item, snap, run, sl_daily

@pytest.fixture(autouse=True)
def reviewed_time(monkeypatch):
    class Reviewed(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026,11,10,3,tzinfo=timezone.utc)
    monkeypatch.setattr(contracts,'datetime',Reviewed)
    monkeypatch.setattr('hosting.photo_embed.certificate_ready',lambda:True)
    monkeypatch.setattr(contracts,'_site_url',lambda *a,**k:contracts.SITE_URL)
    monkeypatch.setattr('hosting.photo_embed._get',lambda *a,**k:(200,deepcopy(contracts.EXPECTED_TIMEZONE)))

@pytest.mark.parametrize('value', ['2026-11-06T06:00:00Z', '2026-07-06T05:00:00Z', '2026-11-06T00:00:00Z', '2026-11-06T06:00:00', 'invalid', '2026-02-30'])
def test_unconfirmed_business_value_is_not_missing_or_wrong_day(value):
    items = deepcopy(ALL)
    items[0]['fields']['Date'] = value
    source = snap([items]); before = deepcopy(source)
    result = run(sl_daily(), source)
    row = next(r for r in result['rows'] if r['item'] == '432')
    assert row['status'] == 'source_day_unconfirmed'
    assert value in row['note'] and 'dated nothing' not in row['note']
    assert result['status'] == 'incomplete'
    assert not reconcile.evidence_verified(result)
    assert result['count_reconciliation']['quantities']['inspected']['status'] == 'unavailable'
    assert result['count_reconciliation']['record_counts']['unmatched_report_rows'] == 1
    assert result['count_reconciliation']['exclusions'] == []
    assert not result['proposed_corrections']
    assert source == before

@pytest.mark.parametrize('fields', [{}, {'Date0': '2026-11-06T06:00:00Z'}, {'Date': None}, {'Date': ''}])
def test_missing_business_date_is_unknown_not_absent_item(fields):
    items = deepcopy(ALL)
    for k in ('Date','Date0'): items[0]['fields'].pop(k, None)
    items[0]['fields'].update(fields)
    result = run(sl_daily(), snap([items]))
    assert result['rows'][0]['status'] == 'source_day_unconfirmed'
    assert 'No usable business-date value' in result['rows'][0]['note']
    assert not result['verified']

def test_confirmed_other_day_and_absent_item_remain_distinct():
    items = deepcopy(ALL[:4]); items[0]['fields']['Date'] = '2026-11-05'
    result = run(sl_daily(), snap([items]))
    statuses = {r['item']:r['status'] for r in result['rows']}
    assert statuses['432'] == 'not_on_sharepoint_for_day'
    assert statuses['436'] == 'not_on_sharepoint'
    assert [x['item'] for x in result['count_reconciliation']['exclusions']] == ['432']

def test_business_date_wins_and_calendar_date_can_match():
    assert reconcile.item_day({'Date':'2026-11-06', 'Date0':'2026-11-07T07:00:00Z'}, {}) == ('2026-11-06','business_date')
    assert reconcile.item_day({'Date':'2026-11-06T06:00:00Z', 'Date0':'2026-11-06'}, {}) == (None,'timezone_unconfirmed')
    assert run(sl_daily(), snap([ALL]))['verified']

from hosting.source_date_contracts import SITE, LIST, contract_for
from hosting import sharepoint
from unittest.mock import patch
COL = {'Date': {'id':'00000000-0000-0000-0000-000000000001','name':'Date', 'dateTime':{'format':'dateOnly'}}}

def resolved(value, **kwargs):
    return reconcile.item_day({'Date':value, 'Date0':'2026-11-07T07:01:18Z'}, COL, site_id=kwargs.get('site',SITE), list_id=kwargs.get('list',LIST), date_contract=contracts.capture_contract(kwargs.get('site',SITE),kwargs.get('list',LIST),COL))

@pytest.mark.parametrize('value,expected', [
    ('2026-11-07T06:00:00Z','2026-11-07'),
    ('2026-07-06T05:00:00Z','2026-07-06'),
    ('2026-03-08T06:00:00Z','2026-03-08'),
    ('2026-11-01T05:00:00Z','2026-11-01'),
    ('2026-11-07T00:00:00-06:00','2026-11-07'),
])
def test_exact_verified_contract_parses_local_midnight(value,expected):
    assert resolved(value) == (expected,'business_date')

@pytest.mark.parametrize('value', ['2026-11-07T00:00:00Z','2026-11-07T07:01:18Z','2026-11-07T06:00:00','2026-11-07T06:00:00.000001Z','2026-11-07T06:00:00.0000001Z','not-a-date'])
def test_contract_does_not_guess_inconsistent_timestamp(value):
    assert resolved(value) == (None,'timezone_unconfirmed')

@pytest.mark.parametrize('site,list_id,columns', [('other',LIST,COL),(SITE,'other',COL),(SITE,LIST,{}),(SITE,LIST,{'Date':{'name':'Date','dateTime':{'format':'dateTime'}}})])
def test_exact_identity_and_schema_required(site,list_id,columns):
    assert reconcile.item_day({'Date':'2026-11-07T06:00:00Z'},columns,site_id=site,list_id=list_id) == (None,'timezone_unconfirmed')

def contract_snapshot(value='2026-11-07T06:00:00Z'):
    raw_item = sp_item(123,date=value)
    def fetch(url,*args,**kwargs):
        if '?$select=' in url:return 200, {'id':LIST,'displayName':'synthetic','webUrl':'https://example.invalid/sites/test/Lists/test'}
        if '/columns?' in url:return 200, {'value':list(COL.values())+[{'name':'QuantityInspected','displayName':'Quantity Inspected'},{'name':'QtyNOK','displayName':'Qty NOK'}]}
        return 200, {'value':[raw_item]}
    with patch.object(reconcile,'_fetch',fetch):return reconcile.snapshot('synthetic',LIST,site_id=SITE)

def test_real_capture_compare_contract_binding_and_rollback():
    source=contract_snapshot(); before=deepcopy(source)
    record={'sheet':'Inspection Detail','row':4,'source':{'site_id':SITE,'list_id':LIST,'item':'123'},'values':{'inspected':80,'rejected':0},'columns':{'inspected':4,'rejected':5}}
    result=reconcile.compare('2026-11-07',[record],[],source, report={'file_hash':'synthetic','checker_version':'test'})
    assert result['verified'] and reconcile.evidence_verified(result)
    assert result['date_contract']['revision'] == contract_for(SITE,LIST,COL)['revision']
    assert source == before and source['items'][0]['raw']['Date']=='2026-11-07T06:00:00Z'
    changed=deepcopy(source); changed['date_contract']['revision']='next-review'
    assert reconcile.compare('2026-11-07',[record],[],changed)['snapshot_id'] != result['snapshot_id']
    with patch('hosting.source_date_contracts.contract_for',return_value=None):
        unavailable=contract_snapshot()
    held=reconcile.compare('2026-11-07',[record],[],unavailable)
    assert held['status']=='incomplete' and not held['verified']
    assert held['rows'][0]['status']=='source_day_unconfirmed'
    assert not held['proposed_corrections']
    assert not held['count_reconciliation']['exclusions']

def test_list_day_and_history_use_capture_identity_not_display_name():
    raw=sp_item(123,date='2026-11-07T06:00:00Z')
    capture={'site_id':SITE,'list_id':LIST,'columns':list(COL.values())}
    data=('arbitrary-name',{'id':LIST,'_capture':capture},{'Date':'Date'},[raw])
    with patch.object(sharepoint,'configured',return_value=True),patch.object(sharepoint,'_list_items',return_value=data):
        assert sharepoint.list_day('synthetic','2026-11-07')[2]['rows']==1
        assert sharepoint.list_history('synthetic','2026-11-08')[0][0]=='2026-11-07'
        capture['list_id']='other'
        with pytest.raises(sharepoint.SharePointError):sharepoint.list_day('synthetic','2026-11-07')

def test_old_receipt_not_rewritten_or_promoted_by_new_contract():
    old=run(sl_daily(),snap([[sp_item(i,date='2026-11-06T06:00:00Z') for i in range(432,437)]]))
    old['bound_to']={'file_hash':'synthetic','checker_version':'old'}
    before=deepcopy(old)
    result={'file_hash':'synthetic','checker_version':'old','source_reconciliation':old}
    assert reconcile.current(result)==before
    assert not reconcile.evidence_verified(old)
    assert old==before
    result['checker_version']='changed'
    assert reconcile.current(result) is None


def test_contract_expiry_revision_and_disabled_rollback_invalidate_only_new_receipts(monkeypatch):
    contract=contracts.capture_contract(SITE,LIST,COL)
    assert contracts.receipt_contract_current(contract)
    record={'file_hash':'synthetic','checker_version':'test','source_reconciliation':{'date_contract':contract,'bound_to':{'file_hash':'synthetic','checker_version':'test'}}}
    assert reconcile.current(record)
    changed=deepcopy(contract); changed['revision']='old'
    assert not contracts.receipt_contract_current(changed)
    with patch.object(contracts,'contract_for',return_value=None):
        assert reconcile.current(record) is None
    class Expired(datetime):
        @classmethod
        def now(cls,tz=None):return datetime(2026,11,11,tzinfo=timezone.utc)
    monkeypatch.setattr(contracts,'datetime',Expired)
    assert reconcile.current(record) is None
    assert reconcile.item_day({'Date':'2026-11-07T06:00:00Z'},COL,site_id=SITE,list_id=LIST,date_contract=contract)==(None,'timezone_unconfirmed')
    record['source_reconciliation'].pop('date_contract')
    assert reconcile.current(record)


from tests.test_source_proposal_literals import case, RAW

def test_expired_contract_refuses_real_source_apply_and_keeps_original(case,monkeypatch):
    from hosting.fixes import proposals, FixError
    from hosting.change_summary import source_receipt
    box,item,prepare=case
    current,props=prepare('2.50')
    contract=contracts.capture_contract(SITE,LIST,COL)
    current=box._update(item['file_hash'],lambda r:r['source_reconciliation'].update(date_contract=contract))
    assert reconcile.current(current)
    with patch.object(contracts,'contract_for',return_value=None):
        assert not [p for p in proposals(current,'synthetic.csv',RAW) if p['source']=='SharePoint']
        with pytest.raises(ValueError):box.apply_fixes(item['file_hash'],props,expected_check_hash=current['check_hash'])
        assert any(h['code'].startswith('source_') for h in box.holds(current))
        assert source_receipt(current,'2026-11-10')['status']=='held'
    assert box.list()['count']==1 and box.download(item['file_hash'])[1]==RAW


@pytest.mark.parametrize('failure',['certificate','host','401','500','missing','wrong_zone','wrong_bias','exception'])
def test_runtime_metadata_failure_never_uses_static_fallback(monkeypatch,failure):
    if failure=='certificate':monkeypatch.setattr('hosting.photo_embed.certificate_ready',lambda:False)
    elif failure=='host':monkeypatch.setattr('hosting.photo_embed._host',lambda:'other.invalid')
    elif failure=='exception':
        def fail(*a,**k):raise RuntimeError('synthetic unavailable')
        monkeypatch.setattr('hosting.photo_embed._get',fail)
    else:
        metadata=deepcopy(contracts.EXPECTED_TIMEZONE);status=200
        if failure in ('401','500'):status=int(failure)
        if failure=='missing':metadata={}
        if failure=='wrong_zone':metadata['Id']=12
        if failure=='wrong_bias':metadata['Information']['Bias']=300
        monkeypatch.setattr('hosting.photo_embed._get',lambda *a,**k:(status,metadata))
    assert contracts.capture_contract(SITE,LIST,COL) is None
    assert resolved('2026-11-07T06:00:00Z')==(None,'timezone_unconfirmed')

def test_runtime_request_is_one_exact_read_and_schema_identity_is_bound(monkeypatch):
    calls=[]
    def get(url,opener=None):calls.append(url);return 200,deepcopy(contracts.EXPECTED_TIMEZONE)
    monkeypatch.setattr('hosting.photo_embed._get',get)
    columns=deepcopy(COL);columns['Date']['id']='00000000-0000-0000-0000-000000000002'
    captured=contracts.capture_contract(SITE,LIST,columns)
    assert calls==[contracts.SITE_URL+'/_api/web/RegionalSettings/TimeZone']
    assert captured['column_id']=='00000000-0000-0000-0000-000000000002'
    assert contracts.contract_matches(captured,SITE,LIST,columns)
    columns['Date']['id']='00000000-0000-0000-0000-000000000003'
    assert not contracts.contract_matches(captured,SITE,LIST,columns)
    assert contracts.capture_contract('other',LIST,columns) is None
    assert len(calls)==1

@pytest.mark.parametrize('list_id',contracts.LISTS)
def test_both_verified_lists_and_item_shapes_resolve(list_id):
    contract=contracts.capture_contract(SITE,list_id,COL)
    for submitted in ('2026-11-07T07:01:18Z','2026-11-07T08:15:43Z','2026-11-07T06:32:03Z'):
        assert reconcile.item_day({'Date':'2026-11-07T06:00:00Z','Date0':submitted},COL,site_id=SITE,list_id=list_id,date_contract=contract)==('2026-11-07','business_date')


@pytest.mark.parametrize('item',['6276','440','437'])
def test_canonical_link_preserves_legacy_identity(item):
    from hosting.sheet_roles import source_url_identity
    base=contracts.SITE_URL+'/Lists/Data%202214SLALSLAL'
    result=reconcile.display_item_url('SLAlabama_AlexanderCity',SITE,LIST,{'id':LIST,'webUrl':base},item)
    assert result==base+'/DispForm.aspx?ID='+item
    old=source_url_identity(base+'/'+item+'_.000');new=source_url_identity(result)
    assert {k:v for k,v in old.items() if k!='url'}=={k:v for k,v in new.items() if k!='url'}

@pytest.mark.parametrize('url,item,site,lid',[
    ('https://evil.invalid/sites/SLAlabama_AlexanderCity/Lists/x','1','SLAlabama_AlexanderCity',LIST),
    (contracts.SITE_URL+'/Lists/x','1','OtherSite',LIST),
    (contracts.SITE_URL+'/Lists/x','1','SLAlabama_AlexanderCity','other'),
    (contracts.SITE_URL+'/Lists/x','1&ID=2','SLAlabama_AlexanderCity',LIST),
    (contracts.SITE_URL+'/Lists/x','１２','SLAlabama_AlexanderCity',LIST),
    (contracts.SITE_URL+'/Lists/x/../y','1','SLAlabama_AlexanderCity',LIST),
    (contracts.SITE_URL+'/Lists/x%2Fy','1','SLAlabama_AlexanderCity',LIST),
    (contracts.SITE_URL+'/Lists/x?foo=bar','1','SLAlabama_AlexanderCity',LIST),
    (contracts.SITE_URL+'/Lists/x/1_.000','1','SLAlabama_AlexanderCity',LIST),
])
def test_canonical_link_rejects_untrusted_identity_or_path(url,item,site,lid):
    assert reconcile.display_item_url(site,SITE,lid,{'id':LIST,'webUrl':url},item) is None

def test_canonical_link_encodes_spaces_and_special_list_characters():
    url=contracts.SITE_URL+'/Lists/Data%20%232214%20%26%20QA'
    assert reconcile.display_item_url('SLAlabama_AlexanderCity',SITE,LIST,{'id':LIST,'webUrl':url},'440')==url+'/DispForm.aspx?ID=440'


def test_incomplete_capture_does_not_claim_absent_item_or_export():
    source=snap([ALL[:3],ALL[3:]],fail={2:503})
    result=run(sl_daily(),source)
    assert result['status']=='incomplete'
    for row in result['rows']:
        assert row['status'] not in ('not_on_sharepoint','export_item_not_on_sharepoint')
    assert any(row['status']=='source_item_unconfirmed' for row in result['rows'])
    assert not result['verified']


@pytest.mark.parametrize('url',[None,'https://evil.invalid/sites/x','https://ppgquality.sharepoint.com/sites/x?token=x','https://ppgquality.sharepoint.com/sites/x#y','https://ppgquality.sharepoint.com/sites/a%2fb','https://ppgquality.sharepoint.com/sites/a%0ab','https://ppgquality.sharepoint.com/sites/..','https://ppgquality.sharepoint.com/sites/x/extra'])
def test_graph_site_url_rejects_malformed_or_cross_tenant_destination(url):
    assert contracts._validated_site_url(SITE,url) is None

def test_other_exact_configured_source_can_use_verified_runtime_zone(monkeypatch):
    site='ppgquality.sharepoint.com,00000000-0000-0000-0000-000000000005,00000000-0000-0000-0000-000000000006'
    list_id='00000000-0000-0000-0000-000000000007'
    url='https://ppgquality.sharepoint.com/sites/SyntheticPlant'
    monkeypatch.setattr(contracts,'_site_url',lambda actual,opener=None:url if actual==site else None)
    calls=[]
    def get(target,opener=None):calls.append(target);return 200,deepcopy(contracts.EXPECTED_TIMEZONE)
    monkeypatch.setattr('hosting.photo_embed._get',get)
    receipt=contracts.capture_contract(site,list_id,COL)
    assert receipt['site_id']==site and receipt['list_id']==list_id
    assert calls==[url+'/_api/web/RegionalSettings/TimeZone']
    assert reconcile.item_day({'Date':'2026-11-06T06:00:00Z'},COL,site_id=site,list_id=list_id,date_contract=receipt)==('2026-11-06','business_date')

def test_graph_site_metadata_requires_exact_returned_identity(monkeypatch):
    # Exercise the production helper rather than the fixture's network seam.
    import importlib.util
    spec=importlib.util.spec_from_file_location('isolated_contracts',contracts.__file__)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    calls=[]
    def fetch(url,opener=None,retries=2):
        calls.append((url,retries));return 200,{'id':SITE,'webUrl':contracts.SITE_URL}
    monkeypatch.setattr(reconcile,'_fetch',fetch)
    assert module._site_url(SITE)==contracts.SITE_URL
    assert len(calls)==1 and calls[0][1]==0
    monkeypatch.setattr(reconcile,'_fetch',lambda *a,**k:(200,{'id':'other','webUrl':contracts.SITE_URL}))
    assert module._site_url(SITE) is None

from tests.test_selected_ai_proposals import setup

def test_unknown_source_date_never_calls_ai_or_mutates_workbook(setup):
    from hosting import ai_proposals
    box,item,issues,payload,_=setup
    for issue in issues:
        issue.update(source='SharePoint',code='sharepoint_source_day_unconfirmed',text='Business day unconfirmed')
    before=box.original_bytes(item['file_hash'])[1]
    result=ai_proposals.prepare(box,payload,lambda:True,lambda rows:pytest.fail('Unknown source evidence must never reach provider'))
    assert result['proposals']==[] and len(result['skipped'])==len(issues)
    assert box.original_bytes(item['file_hash'])[1]==before and box.list()['count']==1

def test_verified_runtime_date_does_not_hide_real_wrong_day_or_quantity_error():
    source=contract_snapshot()
    record={'sheet':'Inspection Detail','row':4,'source':{'site_id':SITE,'list_id':LIST,'item':'123'},'values':{'inspected':72,'rejected':0},'columns':{'inspected':4,'rejected':5}}
    wrong_day=reconcile.compare('2026-11-06',[record],[],source)
    assert wrong_day['rows'][0]['status']=='not_on_sharepoint_for_day'
    assert wrong_day['count_reconciliation']['exclusions'][0]['day']=='2026-11-07'
    different=reconcile.compare('2026-11-07',[record],[],source)
    assert different['status']=='discrepancies'
    assert different['rows'][0]['status']=='value_differs'
    assert different['rows'][0]['report']=='72' and different['rows'][0]['sharepoint']=='80'

@pytest.mark.parametrize('destination',['https://evil.invalid/steal','https://ppgquality.sharepoint.com/sites/Other/_api/web/RegionalSettings/TimeZone'])
@pytest.mark.parametrize('kind',['regional','token'])
def test_metadata_transport_rejects_redirect_without_second_request(monkeypatch,destination,kind):
    import io, email.message, urllib.request, urllib.response, urllib.error
    calls=[]
    class FakeHTTPS(urllib.request.HTTPSHandler):
        def https_open(self,request):
            calls.append(request.full_url)
            headers=email.message.Message();headers['Location']=destination
            response=urllib.response.addinfourl(io.BytesIO(b''),headers,request.full_url,302)
            response.msg='Found'
            return response
    real_build=urllib.request.build_opener
    monkeypatch.setattr(urllib.request,'build_opener',lambda *handlers:real_build(*handlers,FakeHTTPS()))
    monkeypatch.setenv('MS_TENANT_ID','synthetic-tenant')
    regional=contracts.SITE_URL+'/_api/web/RegionalSettings/TimeZone'
    request=urllib.request.Request(regional,headers={'Authorization':'Bearer synthetic'}) if kind=='regional' else urllib.request.Request('https://login.microsoftonline.com/synthetic-tenant/oauth2/v2.0/token',data=b'synthetic')
    with pytest.raises(urllib.error.HTTPError) as error:contracts.metadata_opener(regional)(request,timeout=1)
    assert error.value.code==302 and calls==[request.full_url]

def test_source_gaps_are_separate_from_confirmed_differences():
    from hosting.review_summary import _source_status
    from hosting.spreadsheets import CHECKER_VERSION
    source=snap([[sp_item(i,date='2026-11-06T06:00:00Z') for i in range(432,437)]])
    result=run(sl_daily(),source)
    assert result['difference_count']==0 and result['verification_gap_count']==5
    result['bound_to']={'file_hash':'synthetic','checker_version':CHECKER_VERSION}
    summary=_source_status({'file_hash':'synthetic','checker_version':CHECKER_VERSION,'source_reconciliation':result},{})
    assert summary['differences']==0 and summary['verification_gaps']==5
    assert summary['status']=='incomplete' and not summary['source_verified']

def test_builder_and_list_selection_use_the_same_runtime_business_date(monkeypatch):
    from hosting import builder,shifts
    item=sp_item(123,date='2026-11-07T06:00:00Z');item['fields']['Shift']='1'
    capture={'site_id':SITE,'list_id':LIST,'columns':list(COL.values())}
    data=('Synthetic',{'id':LIST,'_capture':capture},{'Identifier':'Identifier','QuantityInspected':'Quantity Inspected','QtyNOK':'Qty NOK','Shift':'Shift'},[item])
    original=deepcopy(item)
    monkeypatch.setattr(sharepoint,'configured',lambda:True)
    monkeypatch.setattr(sharepoint,'_list_items',lambda *a,**k:data)
    rows,info=builder.day_rows('synthetic','2026-11-07',shifts.DEFAULT,site_id=SITE,list_id=LIST)
    assert len(rows)==1 and rows[0]['raw']['Date']=='2026-11-07T06:00:00Z'
    assert info['capture']['date_contract']['observed_timezone']['Id']==11
    assert sharepoint.list_day('synthetic','2026-11-07')[2]['capture']['date_contract']['zone']=='America/Chicago'
    assert item==original
    with patch.object(contracts,'capture_contract',return_value=None):
        with pytest.raises(builder.BuildError,match='unconfirmed'):builder.day_rows('synthetic','2026-11-07',shifts.DEFAULT,site_id=SITE,list_id=LIST)
        with pytest.raises(sharepoint.SharePointError):sharepoint.list_day('synthetic','2026-11-07')

def test_legacy_presentation_normalization_is_exact_and_does_not_edit_receipt():
    legacy={'status':'not_on_sharepoint_for_day','note':'On SharePoint the item is dated nothing (timezone unconfirmed).','item':'123'}
    original=deepcopy(legacy)
    assert reconcile.presentation_row(legacy)['status']=='source_day_unconfirmed'
    assert legacy==original
    real=dict(legacy,note='On SharePoint the item is dated 2026-11-05 (business date).')
    assert reconcile.presentation_row(real) is real
    other=dict(legacy,note='On SharePoint the item is dated nothing (no business date).')
    assert reconcile.presentation_row(other) is other

def test_activity_counts_unknown_dates_are_not_zero(monkeypatch):
    from hosting import periods
    from datetime import date
    data=('synthetic',{'displayName':'Synthetic'},{},[sp_item(123,date='2026-11-07T06:00:00Z')])
    monkeypatch.setattr(sharepoint,'configured',lambda:True)
    monkeypatch.setattr(sharepoint,'_list_items',lambda *a,**k:data)
    result=periods.day_scans('synthetic','2026-11-07')
    assert result['today'] is None and result['yesterday'] is None and result['complete'] is False
    days,note=periods.sharepoint_days('synthetic',date(2026,11,1),date(2026,11,7))
    assert days=={} and 'Totals are unknown' in note
