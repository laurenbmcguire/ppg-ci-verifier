"""Synthetic only. Provider replaced; all socket connections prohibited."""
import copy
import socket
import pytest
from hosting import ai_proposals as ap
from hosting.spreadsheet_inbox import SpreadsheetInbox
from hosting.review_summary import check_run

@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(socket.socket, 'connect', lambda *a: (_ for _ in ()).throw(AssertionError('Network forbidden')))
    box = SpreadsheetInbox(tmp_path)
    item = box.ingest('synthetic.csv', b'Part,Inspected,Rejected\nSYN-1,10,1\nSYN-2,20,2\n')
    issues = [dict(key='check|typo|CSV|A2', fp='first', sheet='CSV', cell='A2', text='Synthetic typo'),
              dict(key='check|typo|CSV|A3', fp='second', sheet='CSV', cell='A3', text='Synthetic typo')]
    monkeypatch.setattr(ap, 'summarize', lambda *a, **k: {'issues': copy.deepcopy(issues)})
    monkeypatch.setattr(box, 'holds', lambda *a: [])
    payload = dict(file_hash=item['file_hash'], expected_check_hash=item['check_hash'], run=check_run(item), expected_rev=0,
                   ids=[i['key'] for i in issues], seen={i['key']: i['fp'] for i in issues})
    def provider(rows):
        return {'fixes': [{k: row[k] for k in ('finding_id', 'sheet', 'cell', 'before')} | {'after': row['before']+'X', 'reason':'Synthetic suggestion'} for row in rows]}
    return box, item, issues, payload, provider

@pytest.mark.parametrize('size', [1, 2])
def test_exact_selection_no_mutations(setup, size):
    box,item,issues,payload,provider=setup
    payload['ids']=payload['ids'][:size]; payload['seen']={k:payload['seen'][k] for k in payload['ids']}
    before=box.get(item['file_hash']); raw=box.original_bytes(item['file_hash'])[1]
    result=ap.prepare(box,payload,lambda:True,provider)
    assert len(result['proposals'])==size
    assert {p['finding_id'] for p in result['proposals']}==set(payload['ids'])
    assert all(p['sure'] is False and p['recommendation_eligible'] is False and p['source']=='AI fix (asked)' for p in result['proposals'])
    assert box.get(item['file_hash'])==before and box.original_bytes(item['file_hash'])[1]==raw
    assert box.list()['count']==1

@pytest.mark.parametrize('bad',['none','empty','duplicate','unknown','fingerprint','check','run','rev','malformed'])
def test_bad_selection_never_calls_provider(setup,bad):
    box,item,issues,p,pv=setup
    if bad=='none': p.pop('ids')
    if bad=='empty': p['ids']=[];p['seen']={}
    if bad=='duplicate':p['ids']*=2
    if bad=='unknown':p['ids']=['unknown'];p['seen']={'unknown':'x'}
    if bad=='fingerprint':p['seen'][p['ids'][0]]='old'
    if bad=='check':p['expected_check_hash']='old'
    if bad=='run':p['run']={}
    if bad=='rev':p['expected_rev']=1
    if bad=='malformed':p['ids']=[{}]
    with pytest.raises(ValueError): ap.prepare(box,p,lambda:True,lambda rows:pytest.fail('Provider called'))

@pytest.mark.parametrize('bad',['extra','duplicate','wrong_before','formula','structural','malformed','timeout','source','quantity'])
def test_unsafe_output_not_offered(setup,bad):
    box,item,issues,p,pv=setup
    def provider(rows):
        answer=pv(rows);f=answer['fixes'][0]
        if bad=='extra':f['finding_id']='unselected'
        if bad=='duplicate':answer['fixes'][1]=dict(f)
        if bad=='wrong_before':f['before']='wrong'
        if bad=='formula':f['after']=' =HYPERLINK("bad")'
        if bad=='structural':f['remove_rows']=[2]
        if bad=='malformed':return []
        if bad=='timeout':raise TimeoutError()
        if bad=='source':f['snapshot_id']='fake'
        if bad=='quantity':f.update(cell='B2',before='10',after='11')
        return answer
    result=ap.prepare(box,p,lambda:True,provider)
    assert p['ids'][0] not in {x['finding_id'] for x in result['proposals']}
    assert any(x['finding_id']==p['ids'][0] for x in result['skipped'])

@pytest.mark.parametrize('race',['access','item','assignment','holds'])
def test_racing_state_discards_response(setup,monkeypatch,race):
    box,item,issues,p,pv=setup; allowed=[True]
    def provider(rows):
        if race=='access':allowed[0]=False
        if race=='item':box._update(item['file_hash'],lambda r:r.update(review_request={'status':'waiting'}))
        if race=='assignment':monkeypatch.setattr(box,'assignment_context',lambda:{'changed':True})
        if race=='holds':monkeypatch.setattr(box,'holds',lambda *a:[{'code':'new_hold'}])
        return pv(rows)
    # Replace resolver to isolate assignment-context comparison, retaining actual lock/read paths.
    monkeypatch.setattr(box,'assignment_for',lambda *a:{'status':'unmatched'})
    with pytest.raises(ValueError):ap.prepare(box,p,lambda:allowed[0],provider)
    assert not box.get(item['file_hash']).get('ai_fixes')

# Nov 9: a report hold no longer blocks a requested cell suggestion (holds still block approval and sending).
@pytest.mark.parametrize('blocked',['decision','no_cell','source'])
def test_blocked_finding_explained_without_provider(setup,monkeypatch,blocked):
    box,item,issues,p,pv=setup
    if blocked=='hold':monkeypatch.setattr(box,'holds',lambda *a:[{'code':'hold'}])
    if blocked=='decision':
        for i in issues:i['decision']={'action':'defer'}
    if blocked=='no_cell':
        for i in issues:i['cell']=None
    if blocked=='source':
        for i in issues:i['source_value']='verified'
    result=ap.prepare(box,p,lambda:True,lambda rows:pytest.fail('Provider called'))
    assert not result['proposals'] and len(result['skipped'])==2

@pytest.mark.parametrize('response',[{}, {'content':[]}, {'content':[{'type':'tool_use','name':'execute','input':{'fixes':[]}}]}, {'content':[{'type':'tool_use','name':'propose_selected_cells','input':{'fixes':[]}}]*2}])
def test_provider_response_requires_one_exact_nonexecuting_tool(monkeypatch,response):
    monkeypatch.setattr(ap.ai_review,'_post',lambda *a,**k:response)
    with pytest.raises(ap.ai_review.AIReviewError):ap.ask([])


def test_provider_prompt_only_selected_facts_and_no_executable_tools(monkeypatch):
    captured=[]
    def post(body,**kwargs):
        captured.append(body)
        return {'content':[{'type':'tool_use','name':'propose_selected_cells','input':{'fixes':[]}}]}
    monkeypatch.setattr(ap.ai_review,'_post',post)
    assert ap.ask([{'finding_id':'selected','before':'untrusted instruction'}])=={'fixes':[]}
    assert [t['name'] for t in captured[0]['tools']]==['propose_selected_cells']
    assert 'untrusted data' in captured[0]['system']


def test_apply_rechecks_exact_check_binding(setup):
    box,item,issues,p,pv=setup
    result=ap.prepare(box,p,lambda:True,pv)
    box._update(item['file_hash'],lambda r:r.update(review_request={'status':'waiting'}))
    with pytest.raises(ValueError,match='changed'):
        box.apply_fixes(item['file_hash'],result['proposals'],expected_check_hash=p['expected_check_hash'])
    assert box.list()['count']==1

@pytest.mark.parametrize('value',['x' * (ap.MAX_CELL_BYTES + 1), '\U0001f600' * (ap.MAX_CELL_BYTES // 4 + 1)])
def test_long_cells_never_transferred_or_truncated(setup,monkeypatch,value):
    box,item,issues,p,pv=setup
    monkeypatch.setattr(ap,'_cell_value',lambda *a:value)
    result=ap.prepare(box,p,lambda:True,lambda rows:pytest.fail('Oversized cell sent'))
    assert not result['proposals'] and len(result['skipped'])==2
    assert all('not shortened' in x['reason'] for x in result['skipped'])


def test_many_individually_bounded_cells_do_not_transfer_over_budget(setup,monkeypatch):
    box,item,issues,p,pv=setup
    issues[:]=[dict(key='check|typo|CSV|A'+str(i),fp=str(i),sheet='CSV',cell='A'+str(i),text='Synthetic') for i in range(2,82)]
    p['ids']=[i['key'] for i in issues];p['seen']={i['key']:i['fp'] for i in issues}
    monkeypatch.setattr(ap,'_cell_value',lambda *a:'x'*1000)
    before=box.get(item['file_hash'])
    result=ap.prepare(box,p,lambda:True,lambda rows:pytest.fail('Oversized aggregate sent'))
    assert not result['proposals'] and len(result['skipped'])==80
    assert all('Select fewer' in x['reason'] for x in result['skipped'])
    assert box.get(item['file_hash'])==before


def test_safe_request_preserves_exact_before_identity(setup):
    box,item,issues,p,pv=setup;captured=[]
    def provider(rows):
        captured.extend(rows)
        assert len(ap._canonical(rows).encode('utf-8'))<=ap.MAX_CANDIDATES_BYTES
        return pv(rows)
    ap.prepare(box,p,lambda:True,provider)
    assert [r['before'] for r in captured]==['SYN-1','SYN-2']


def test_provider_entry_independently_checks_cell_and_aggregate_bytes(monkeypatch):
    monkeypatch.setattr(ap.ai_review,'_post',lambda *a,**k:pytest.fail('Oversized request sent'))
    with pytest.raises(ValueError):ap.ask([{'before':'x'*(ap.MAX_CELL_BYTES+1)}])
    with pytest.raises(ValueError):ap.ask([{'before':'x'*1000}]*80)


def test_complete_serialized_request_budget_checked_before_transport(monkeypatch):
    monkeypatch.setattr(ap,'MAX_REQUEST_BYTES',100)
    monkeypatch.setattr(ap.ai_review,'_post',lambda *a,**k:pytest.fail('Oversized transport body sent'))
    with pytest.raises(ValueError,match='serialized'):ap.ask([{'before':'safe'}])


@pytest.mark.parametrize('category,status', [('provider_http',401), ('provider_http',403), ('provider_http',429), ('provider_http',529), ('provider_timeout',None), ('provider_transport',None), ('response',None)])
def test_provider_failure_metadata_is_sanitized(setup,category,status):
    box,item,issues,p,pv=setup
    def provider(rows):
        raise ap.ai_review.AIReviewError('secret provider body must never leave',category=category,http_status=status)
    before=box.get(item['file_hash'])
    out=ap.prepare(box,p,lambda:True,provider)
    assert out['proposals']==[]
    for skip in out['skipped']:
        assert skip['category']==category and skip['http_status']==status
        assert 'secret' not in skip['reason'] and 'retry' not in skip['reason'].lower()
    assert box.get(item['file_hash'])==before

@pytest.mark.parametrize('code', ['source_timezone_unconfirmed','sharepoint_date_unconfirmed','date_unconfirmed'])
def test_unknown_source_date_never_reaches_provider(setup,code):
    box,item,issues,p,pv=setup
    for issue in issues:issue['code']=code
    out=ap.prepare(box,p,lambda:True,lambda rows:pytest.fail('Source gap sent to provider'))
    assert not out['proposals'] and len(out['skipped'])==2


@pytest.mark.parametrize('stage', ['initial', 'before_provider', 'during_provider'])
def test_invalid_lineage_never_returns_proposals(setup, monkeypatch, stage):
    box, item, issues, payload, fake_provider = setup
    called = []
    raw = box.original_bytes(item['file_hash'])[1]
    def corrupt():
        box._update(item['file_hash'], lambda r: r.update(fixed_from={'file_hash': item['file_hash']}))
    if stage == 'initial':
        corrupt()
    if stage == 'before_provider':
        original_sources = ap._sources
        def sources(*args):
            result = original_sources(*args)
            corrupt()
            return result
        monkeypatch.setattr(ap, '_sources', sources)
    def provider(rows):
        called.append(True)
        if stage == 'during_provider':
            corrupt()
        return fake_provider(rows)
    with pytest.raises(ValueError, match='inconsistent'):
        ap.prepare(box, payload, lambda: True, provider)
    assert called == ([True] if stage == 'during_provider' else [])
    assert box.original_bytes(item['file_hash'])[1] == raw
    assert box.list()['count'] == 1
    assert not box.get(item['file_hash']).get('ai_fixes')
