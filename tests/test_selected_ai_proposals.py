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

@pytest.mark.parametrize('blocked',['hold','decision','no_cell','source'])
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
