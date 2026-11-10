from copy import deepcopy
from hosting import source_first as sf

SCOPE={'site_id':'example.sharepoint.com,11111111-1111-1111-1111-111111111111,22222222-2222-2222-2222-222222222222','list_id':'33333333-3333-3333-3333-333333333333','job':'SYNTHETIC-1234','business_date':'2026-11-10'}
USER={'id':'reviewer-synthetic','role':'admin','name':'Synthetic reviewer'}
MAP={'fields':{f:f.title().replace('_','') for f in sf.FIELDS},'shifts':deepcopy(sf.DEFAULT_SHIFTS)}
MAP['fields']['date']='Date'


def capture(rows=None):
    rows=rows or [dict(id='1',version='"1,1"',modified='2026-11-10T12:00:00Z',day='2026-11-10',date_basis='business_date',raw={MAP['fields'][k]:v for k,v in dict(date='2026-11-10',shift='1',location='Factory',part='00001234',identifier='0000000123',inspected_for='Surface',inspected=10,condition='NOK',reject_reason='Scratch',rejected=2,certified=8,comments='Two scratched parts').items()}),dict(id='2',version='"2,1"',modified='2026-11-10T12:01:00Z',day='2026-11-10',date_basis='business_date',raw={MAP['fields'][k]:v for k,v in dict(date='2026-11-10',shift='2nd',location='Factory',part='00001234',identifier='0000000124',inspected_for='Surface',inspected=20,condition='NOK',reject_reason='Dent',rejected=2,certified=18,comments='Two dents').items()})]
    columns={name:{'name':name,'displayName':name} for name in MAP['fields'].values()};columns['Date'].update(id='44444444-4444-4444-4444-444444444444',dateTime={'format':'dateOnly'})
    return sf.seal(dict(version=sf.VERSION,scope=deepcopy(SCOPE),captured_at=sf.now(),date_contract=None,columns=columns,records=deepcopy(rows),photos=[],uncertain_dates=[],raw_all_records=[],coverage=dict(items_terminal=True,columns_terminal=True,remaining_cursor=None,pages=[dict(kind=k,status=200,page_hash=sf.digest(k),terminal=True,next_cursor_hash=None) for k in ('items','columns')],meaning='Complete observed traversal only.')))


def date_contract(site_id, list_id, columns):
    from datetime import datetime, timezone, timedelta
    from hosting.source_date_contracts import contract_for, EXPECTED_TIMEZONE
    policy=contract_for(site_id,list_id,columns)
    now=datetime.now(timezone.utc)
    return dict(policy, site_url='https://example.sharepoint.com/sites/A', observed_timezone=deepcopy(EXPECTED_TIMEZONE),
                captured_at=now.isoformat(), expires_at=(now+timedelta(seconds=600)).isoformat())
