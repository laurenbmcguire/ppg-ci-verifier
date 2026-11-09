"""Synthetic regressions for source provenance, dependency observations and retry identity."""
import copy
import hashlib
import io
import zipfile
import pytest
from openpyxl import Workbook
from hosting import review_decisions
from hosting.review_summary import check_run, summarize
from hosting.spreadsheets import _attach_observed_values, CHECKER_VERSION
from hosting.spreadsheet_inbox import SpreadsheetInbox
from tests.test_review_races import sp_report


def test_source_label_cannot_authorize_a_value_or_metadata_the_source_did_not_supply(tmp_path):
    box = SpreadsheetInbox(tmp_path)
    item = sp_report(box)
    proposal = next(p for p in box.fix_proposals(item['file_hash'])['proposals'] if p.get('snapshot_id'))
    for key, value in [('after', '999'), ('item', 'different-item'), ('field', 'different-field'), ('change_set', 'made-up')]:
        before = box.get(item['file_hash'])
        with pytest.raises(ValueError):
            box.apply_fixes(item['file_hash'], [dict(proposal, **{key: value})], expected_check_hash=before['check_hash'])
        assert box.get(item['file_hash']) == before
        assert len(box.list()['spreadsheets']) == 1
    manual = dict(proposal, source='Your edit', after='999', reason='Explicit synthetic manual correction')
    corrected = box.apply_fixes(item['file_hash'], [manual], expected_check_hash=item['check_hash'])
    change = corrected['fixed_from']['changes'][0]
    assert change['source'] == 'Your edit' and change['after'] == '999'


def decision_fixture():
    result = {'file_hash':'synthetic', 'checker_version':CHECKER_VERSION}
    current = {x['key']:x for x in review_decisions.assign_ids([dict(source='Checks',code='missing_cell',sheet='S',cell='A1',text='Missing',severity='medium')])}
    kwargs = dict(ids=list(current), action='reject',reason='Synthetic review',by='Reviewer',owner='',evidence=[],request_id='one-click',expected_rev=0,
                  expected_fps={k:x['fp'] for k,x in current.items()},expected_run=check_run(result))
    return result, current, kwargs


@pytest.mark.parametrize('field,value', [('action','confirm'),('ids',['other']),('reason','Different decision'),('by','Different reviewer'),('owner','Another owner'),('evidence',['new evidence']),('expected_rev',9),('expected_fps',{}),('expected_run',{}),('undo_of','other')])
def test_request_id_reuse_with_different_normalized_payload_never_claims_success(field,value):
    result, current, request = decision_fixture()
    first = review_decisions.decide(result,current,**request)
    before = copy.deepcopy(result)
    with pytest.raises(review_decisions.DecisionConflict):
        review_decisions.decide(result,current,**dict(request,**{field:value}))
    assert result == before
    retry = review_decisions.decide(result,{},**request)
    assert retry['replayed'] and retry['decision_id'] == first['decision_id']
    assert result == before


def workbook(formula=False, link=None, other='first', cached=None):
    wb = Workbook(); ws=wb.active; ws.title='S'
    ws['A1'] = '=SUM(B1:B2)' if formula else 'same label'
    ws['B1'] = other
    if link: ws['A1'].hyperlink=link
    stream=io.BytesIO();wb.save(stream);wb.close()
    raw=stream.getvalue()
    if cached is not None:
        out=io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(raw)) as src, zipfile.ZipFile(out,'w') as dst:
            for part in src.infolist():
                data=src.read(part.filename)
                if part.filename=='xl/worksheets/sheet1.xml': data=data.replace(b'<v></v>',f'<v>{cached}</v>'.encode())
                dst.writestr(part,data)
        raw=out.getvalue()
    return raw


def bound(raw, code):
    digest=hashlib.sha256(raw).hexdigest()
    result=dict(file_hash=digest,file_sha256=digest,checker_version=CHECKER_VERSION,findings=[dict(code=code,sheet='S',row=1,column=1)])
    _attach_observed_values(result,'synthetic.xlsx',raw)
    return next(x for x in summarize(result)['issues'] if x.get('code') == code)


@pytest.mark.parametrize('code,left,right', [
    ('number_stored_as_text',workbook(formula=True,cached=10),workbook(formula=True,cached=12)),
    ('external_hyperlink',workbook(link='https://example.invalid/first'),workbook(link='https://example.invalid/second')),
    ('total_mismatch',workbook(other='first'),workbook(other='second')),
    ('duplicate_count_block',workbook(other='first'),workbook(other='second')),
], ids=['cached-formula', 'hyperlink-target', 'aggregate', 'duplicate-group'])
def test_nonlocal_dependencies_invalidate_generic_finding_fingerprint(code,left,right):
    old,new=bound(left,code),bound(right,code)
    assert old['key']==new['key']
    assert old['fp']!=new['fp']
    assert old['observed']=={'file_sha256':hashlib.sha256(left).hexdigest()}
