"""Source comparisons cannot verify only the surviving subset of an older layout."""
import copy
import pytest
from hosting.spreadsheet_inbox import SpreadsheetInbox
from hosting.row_scope import saved_detail_count, require_complete_detail
from hosting.spreadsheets import report_and_export_records
from tests.eos_fixture import build, ROWS, source_csv
from tests.test_reconcile import sl_daily, snap, ALL

@pytest.fixture
def box(tmp_path, monkeypatch):
    monkeypatch.setenv('CI_LOCAL_DEV','1');monkeypatch.setenv('VERCEL','0')
    monkeypatch.setenv('ANTHROPIC_API_KEY','synthetic-unused');monkeypatch.delenv('AI_LAYOUT',raising=False)
    def no_provider(*args,**kwargs):raise AssertionError('No provider permitted')
    monkeypatch.setattr('hosting.ai_review._post',no_provider)
    return SpreadsheetInbox(tmp_path)

@pytest.mark.parametrize('kind',['branded','adaptive','multi_sheet'])
def test_actual_authoritative_detail_scope_excludes_summary_and_export_duplicates(box,kind):
    if kind=='branded':name,raw='SYNTHETIC.xlsx',build(rows=list(ROWS)[:2])
    elif kind=='adaptive':name,raw='SYNTHETIC.csv',b'Synthetic offset report\nPart,Inspected,Rejected\nSYN-A,10,0\nSYN-B,20,3\nTotal,30,3\n'
    else:name,raw='SYNTHETIC.xlsx',sl_daily()
    item=box.ingest(name,raw);records,exports=report_and_export_records(name,raw)
    assert saved_detail_count(item)==len(records)
    require_complete_detail(item,records)
    if kind=='multi_sheet':assert len(records)==5 and len(exports)==5
    if kind=='adaptive':assert len(records)==2
    before=copy.deepcopy(box.get(item['file_hash']))
    if kind=='multi_sheet':
        out=box.source_reconcile(item['file_hash'],snap([ALL]),'2026-11-06',expected_check_hash=before['check_hash'])
        assert out['source_reconciliation']['verified']
    assert box.original_bytes(item['file_hash'])[1]==raw

@pytest.mark.parametrize('mode',['partial','missing','unknown'])
@pytest.mark.parametrize('operation',['snapshot','uploaded'])
def test_old_ai_layout_cannot_save_matching_partial_source_or_upload(box,monkeypatch,mode,operation):
    raw=sl_daily();item=box.ingest('SYNTHETIC.xlsx',raw);h=item['file_hash']
    box._update(h,lambda r:r.update(layout_source='ai',ai_layout={'model':'historical-synthetic'},record_count=None if mode=='unknown' else 5))
    records,exports=report_and_export_records('SYNTHETIC.xlsx',raw)
    surviving=records[:1] if mode!='missing' else []
    monkeypatch.setattr('hosting.spreadsheets.report_and_export_records',lambda *a,**kw:(surviving,exports))
    monkeypatch.setattr('hosting.spreadsheets.report_records',lambda *a,**kw:surviving)
    def forbidden(*a,**kw):raise AssertionError('Incomplete scope must fail before comparison')
    monkeypatch.setattr('hosting.reconcile.compare',forbidden);monkeypatch.setattr('hosting.source_compare.compare',forbidden)
    before=copy.deepcopy(box.get(h));files_before=sorted(str(p) for p in box.root.glob('uploads/**/*'))
    with pytest.raises(ValueError,match='Report layout needs review'):
        if operation=='snapshot':box.source_reconcile(h,snap([ALL[:1]]),'2026-11-06',expected_check_hash=before['check_hash'])
        else:box.compare_source(h,'SYNTHETIC-source.csv',source_csv(list(ROWS)[:1]))
    assert box.get(h)==before and box.original_bytes(h)[1]==raw
    assert sorted(str(p) for p in box.root.glob('uploads/**/*'))==files_before

@pytest.mark.parametrize('item',[
    {'record_count':20,'sheets':[{'data_row_count':20}]}, # generic includes totals/blanks
    {'template_id':'pratt-kn-daily-audit/1','template_mapping_status':'matched','record_count':20},
    {'template_id':'pratt-kn-daily-audit-union/2','template_mapping_status':'matched','record_count':20,'completed_record_count':20},
])
def test_incomparable_count_is_not_a_blanket_layout_invalidation(item):
    assert saved_detail_count(item) is None
    require_complete_detail(item,[{'sheet':'Detail','row':2}])

@pytest.mark.parametrize('count',[None,'5',True,-1])
def test_unknown_or_invalid_authoritative_counts_fail_closed(count):
    with pytest.raises(ValueError,match='count is unknown'):
        require_complete_detail({'mapping_mode':'adaptive','record_count':count},[{'row':2}])

@pytest.mark.parametrize('count',[1,None,True])
def test_historical_partial_receipt_is_held_in_current_pdf_and_apply(box,monkeypatch,count):
    from hosting import reconcile, change_summary
    from hosting.fixes import FixError
    item=box.ingest('SYNTHETIC.xlsx',sl_daily());h=item['file_hash']
    out=box.source_reconcile(h,snap([ALL]),'2026-11-06',expected_check_hash=box.get(h)['check_hash'])
    original=box.original_bytes(h)[1]
    # The saved check says five rows, but a historical receipt claims a different
    # scope, or that saved scope was unknown. Neither may authorize source use.
    def mutate(r):
        r.update(layout_source='ai',ai_layout={'model':'historical-synthetic'})
        if count is None:r['record_count']=None
        else:r['source_reconciliation']['report_rows']=count
    box._update(h,mutate);saved=copy.deepcopy(box.get(h));rec=saved['source_reconciliation']
    assignment={'status':'source','site_id':rec['site_id'],'list_id':rec['list_id'],'needs_person':False}
    monkeypatch.setattr(SpreadsheetInbox,'assignment_for',lambda *a,**kw:assignment)
    assert reconcile.current(saved) is None
    assert any(x['code']=='source_evidence_incomplete' and x['hard'] for x in box.holds(saved))
    receipt=change_summary.source_receipt(saved,'2026-11-06',assignment,rec['mapping_binding'])
    assert receipt['status']=='held' and any('authoritative report scope' in r for r in receipt['reasons'])
    proposal={'sheet':'First shift','cell':'A2','snapshot_id':rec['snapshot_id'],'item':'432','field':'inspected','source':'SharePoint','after':80}
    with pytest.raises(FixError):box._check_changes(saved,[proposal],False,original)
    assert box.get(h)==saved and box.original_bytes(h)[1]==original


def test_complete_historical_receipt_remains_historical_without_new_parse_or_provider(box,monkeypatch):
    from hosting import reconcile, change_summary
    item=box.ingest('SYNTHETIC.xlsx',sl_daily());h=item['file_hash']
    box.source_reconcile(h,snap([ALL]),'2026-11-06',expected_check_hash=box.get(h)['check_hash'])
    box._update(h,lambda r:r.update(layout_source='ai',ai_layout={'model':'historical-synthetic','tables':[]}))
    saved=copy.deepcopy(box.get(h));rec=saved['source_reconciliation']
    assignment={'status':'source','site_id':rec['site_id'],'list_id':rec['list_id'],'needs_person':False}
    monkeypatch.setattr(SpreadsheetInbox,'assignment_for',lambda *a,**kw:assignment)
    def no_parse(*a,**kw):raise AssertionError('Displaying saved evidence must not reparse or call AI')
    monkeypatch.setattr('hosting.spreadsheets.report_records',no_parse)
    assert reconcile.current(saved)==rec and reconcile.evidence_verified(rec)
    assert not [x for x in box.holds(saved) if x['code'].startswith('source_')]
    assert change_summary.source_receipt(saved,'2026-11-06',assignment,rec['mapping_binding'])['status']=='verified'
    assert box.get(h)==saved
