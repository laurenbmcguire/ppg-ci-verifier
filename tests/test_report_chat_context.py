"""Relevant evidence only: unrelated cells/sheets must never reach the question provider."""
import io
import json
from openpyxl import Workbook
from hosting.report_chat_context import build

def fixture():
    wb=Workbook();ws=wb.active;ws.title='Review'
    ws.append(['Part','Inspected','Rejected','Private note'])
    ws.append(['SYNTHETIC-1',10,None,'UNRELATED-ADJACENT-SENTINEL'])
    ws.append(['UNRELATED-ROW-SENTINEL',999,888,'not relevant'])
    other=wb.create_sheet('Unrelated');other['A1']='UNRELATED-SHEET-SENTINEL'
    raw=io.BytesIO();wb.save(raw)
    item={'filename':'SYNTHETIC.xlsx','file_hash':'a'*64,'check_hash':'b'*64,'version':3,'revision':None,'sheets':[{'name':'Review','tables':[{'header_row':1}]}]}
    review={'issues':[{'sheet':'Review','row':2,'cell':'C2','text':'Rejected is blank','severity':'high'},
                      {'sheet':'Unrelated','row':1,'cell':'A1','text':'UNRELATED-FINDING-SENTINEL'}]}
    return item,raw.getvalue(),review

def test_reference_and_header_present_unrelated_values_absent():
    item,raw,review=fixture();out=build(item,raw,review,[],'Why is Review!C2 blank?')
    text=json.dumps(out)
    assert 'UNRELATED-' not in text
    assert out['relevant_cells']==[{'sheet':'Review','cell':'C2','value':None,'blank':True,'purpose':'referenced or relevant finding cell'},
                                  {'sheet':'Review','cell':'C1','value':'Rejected','blank':False,'purpose':'column header'}]
    assert out['report']['file_version']==3 and out['report']['revision'] is None

def test_unknown_sheet_or_other_report_does_not_resolve_context():
    item,raw,_=fixture();out=build(item,raw,{'issues':[]},[],'Show AnotherReport!A1 and https://other.invalid/report')
    assert out['relevant_cells']==[]
    assert 'UNRELATED-' not in json.dumps(out)

def test_reference_count_is_bounded_and_ranges_are_not_expanded():
    item,raw,_=fixture();out=build(item,raw,{'issues':[]},[], ' '.join('Review!B'+str(n) for n in range(1,1000)))
    assert len(out['relevant_cells'])<=24 and out['selection_limited']
    assert len(json.dumps(out).encode())<12000
    out=build(item,raw,{'issues':[]},[],'Review!B2:Z999999')
    assert len(out['relevant_cells'])<=2

def test_question_relevant_finding_does_not_share_unrelated_sheet():
    item,raw,review=fixture();out=build(item,raw,review,[],'Why is rejected blank?')
    assert out['findings_shown']==1
    assert 'UNRELATED-' not in json.dumps(out)

def test_unknown_qualified_reference_does_not_select_single_sheet_cell():
    item={'filename':'SYNTHETIC.csv','file_hash':'a'*64,'check_hash':'b'*64}
    out=build(item,b'Private\nUNRELATED-SECRET-SENTINEL\n',{'issues':[]},[], 'Read Unknown!A2 and Unknown! A2')
    assert out['relevant_cells']==[] and 'UNRELATED-' not in json.dumps(out)

def test_source_assignment_or_mapping_holds_hide_current_source_difference_context(monkeypatch):
    item,raw,review=fixture()
    monkeypatch.setattr('hosting.report_chat_context.reconcile.current',lambda i:{'status':'verified','rows':[{'sheet':'Review','row':2,'report':'STALE-SOURCE-SENTINEL'}]})
    for code in ('source_mapping_changed','source_assignment_changed','source_assignment_unavailable'):
        out=build(item,raw,review,[{'code':code,'text':'Resolve source binding'}],'Review!C2')
        assert out['source_comparison']['status']=='unverified'
        assert 'historical evidence only' in out['source_comparison']['scope']
        assert 'STALE-SOURCE-SENTINEL' not in json.dumps(out)

def test_quantities_preserve_partial_unknown_and_unit_metadata():
    item,raw,review=fixture()
    review.update(inspected=90,rejected=2,rejected_partial=True,rejected_unit='rows')
    out=build(item,raw,review,[],'Review!C2')['report']['summary']
    assert out['quantities']['rejected']=={'value':'2','partial':True,'complete':None,'unit':'rows'}
    assert out['quantities']['inspected']['complete'] is None
    assert 'rejected' not in out and 'inspected' not in out
    review['quantities']={'rejected':{'value':2,'partial':True,'complete':False,'unit':'rows'},
                          'inspected':{'value':None,'partial':False,'complete':False,'unit':'parts'}}
    out=build(item,raw,review,[],'Review!C2')['report']['summary']
    assert out['quantities']['rejected']['partial'] and out['quantities']['rejected']['complete'] is False
    assert out['quantities']['inspected']['value'] is None and out['quantities']['inspected']['complete'] is False
