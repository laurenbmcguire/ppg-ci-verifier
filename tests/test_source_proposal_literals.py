"""Synthetic source receipt -> real proposal -> guarded save; no external source/provider."""
import copy
import hashlib
import socket
from decimal import Decimal
import pytest
from hosting.fixes import proposals, _pid, FixError
from hosting.spreadsheet_inbox import SpreadsheetInbox

RAW=b'Date,Identifier,Qty Inspected,Qty OK,Qty NOK\r\n2026-11-10,LOT-1,10,9,1\r\n'

@pytest.fixture
def case(tmp_path,monkeypatch):
    monkeypatch.setattr(socket.socket,'connect',lambda *a:pytest.fail('External traffic forbidden'))
    box=SpreadsheetInbox(tmp_path)
    item=box.ingest('synthetic.csv',RAW)
    mapping={'revision':'mapping-1','legacy_requires_confirmation':False}
    monkeypatch.setattr(box,'source_mapping',lambda *a:mapping)
    def prepare(value,field='rejected',column=5,before='1'):
        rec={'status':'discrepancies','snapshot_id':'synthetic','site_id':'site','list_id':'list','report_rows':1,
             'mapping_binding':mapping,'bound_to':{'file_hash':item['file_hash'],'checker_version':item['checker_version']},
             'rows':[{'status':'value_differs','sheet':'CSV','row':2,'item':'1','field':field,'report':before,'sharepoint':value}],
             'proposed_corrections':[{'identity_verified':True,'sheet':'CSV','row':2,'column':column,'item':'1','field':field,'before':before,'after':value}]}
        updated=box._update(item['file_hash'],lambda r:r.update(source_reconciliation=rec))
        props=[p for p in proposals(updated,'synthetic.csv',RAW) if p['source']=='SharePoint']
        return updated,props
    return box,item,prepare

@pytest.mark.parametrize('literal',['2.50','2.5','2500.00','2,500.00','0','0.00'])
def test_exact_source_literal_survives_proposal_and_atomic_save(case,literal):
    box,item,prepare=case;current,props=prepare(literal)
    assert len(props)==1
    p=props[0];assert p['after']==literal and p['source_value']==literal
    assert p['id']==_pid('CSV','E2',literal) and p['kind']=='number'
    box._check_changes(current,props,False,raw=RAW)
    child=box.apply_fixes(item['file_hash'],props,expected_check_hash=current['check_hash'])
    assert child['file_hash']!=item['file_hash']
    assert child['fixed_from']['changes'][0]['after']==literal
    assert box.download(item['file_hash'])[1]==RAW
    assert hashlib.sha256(RAW).hexdigest()==item['file_hash']
    result=box.download(child['file_hash'])[1].decode().splitlines()[1].split(',')
    assert Decimal(result[-1])==Decimal(literal.replace(',',''))
    assert result[:-1]==['2026-11-10','LOT-1','10','9']
    assert len(box.get(item['file_hash'])['fixes_applied'])==1
    assert not child.get('approval') and not child.get('sent')

@pytest.mark.parametrize('literal',[None,'','blank','NaN','Infinity','-Infinity',True,False,'abc'])
def test_non_numeric_source_is_not_offered_as_quantity(case,literal):
    box,item,prepare=case;_,props=prepare(literal)
    assert not props
    assert box.download(item['file_hash'])[1]==RAW and box.list()['count']==1


def test_identifier_keeps_leading_zeros_and_text_type(case):
    box,item,prepare=case;current,props=prepare('00124','identifier',2,'LOT-1')
    assert props[0]['after']=='00124' and props[0]['kind']=='text'
    child=box.apply_fixes(item['file_hash'],props,expected_check_hash=current['check_hash'])
    assert box.download(child['file_hash'])[1].decode().splitlines()[1].split(',')[1]=='00124'
    assert box.download(item['file_hash'])[1]==RAW

@pytest.mark.parametrize('tamper',['after','snapshot_id','item','field'])
def test_source_guard_still_refuses_changed_provenance_or_quantity(case,tamper):
    box,item,prepare=case;current,props=prepare('2.50');bad=copy.deepcopy(props)
    bad[0][tamper]='7' if tamper=='after' else 'other'
    with pytest.raises(ValueError):box.apply_fixes(item['file_hash'],bad,expected_check_hash=current['check_hash'])
    assert box.list()['count']==1 and box.download(item['file_hash'])[1]==RAW
    assert not box.get(item['file_hash']).get('fixes_applied')


def test_source_atomic_metadata_roundtrip_and_incomplete_set_rejection(monkeypatch):
    """Synthetic existing atomic-source contract: dropped metadata fails, exact full set saves."""
    from tests.test_correction_workflow import ChangeSetsAreWhole
    from tests.review_request_fixture import apply_displayed
    monkeypatch.setattr(socket.socket,'connect',lambda *a:pytest.fail('External traffic forbidden'))
    case=ChangeSetsAreWhole();case.setUp()
    source=[dict(c,source='SharePoint export',snapshot_id='export:'+case.h,item='433',field='field'+str(i)) for i,c in enumerate(case.change_set())]
    source[2]['after']='Damaged'
    monkeypatch.setattr('hosting.fixes.proposals',lambda *a,**k:copy.deepcopy(source))
    stripped=[{k:v for k,v in c.items() if k not in ('change_set','change_set_size')} for c in source]
    for changes in (stripped,source[:3]):
        with pytest.raises(ValueError):apply_displayed(case.box,case.h,changes)
        assert case.box.list()['count']==1 and case.box.download(case.h)[1]==case.raw
    child=apply_displayed(case.box,case.h,source)
    assert len(child['fixed_from']['changes'])==4
    assert all(c['change_set']=='set-5' and c['change_set_size']==4 for c in child['fixed_from']['changes'])
    assert case.box.download(case.h)[1]==case.raw and case.box.list()['count']==2
