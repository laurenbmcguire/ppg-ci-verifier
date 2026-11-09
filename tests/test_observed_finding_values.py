"""Full local workbook regressions for generic finding semantic fingerprints."""
import copy
import io
from openpyxl import load_workbook
from hosting.review_summary import summarize
from hosting.spreadsheet_inbox import SpreadsheetInbox
from tests.sl_daily_fixture import sl_daily
from tests.review_request_fixture import decide_displayed


def workbook(value):
    wb = load_workbook(io.BytesIO(sl_daily(nok_blank=False)))
    wb['Inspection Detail']['L5'] = value
    out = io.BytesIO()
    wb.save(out)
    wb.close()
    return out.getvalue()


def issue(item):
    rv = summarize(item)
    return next(x for x in rv['issues'] + rv['dismissed']
                if x.get('code') == 'number_stored_as_text' and x.get('cell') == 'L5')


def test_changed_numeric_text_never_inherits_rejection(tmp_path):
    box = SpreadsheetInbox(tmp_path)
    first = box.ingest('synthetic.xlsx', workbook('072'))
    old = issue(first)
    assert old['observed'] == [{'type': 'str', 'value': '072'}]
    saved, _ = decide_displayed(box, first['file_hash'], ids=[old['key']], action='reject', reason='Synthetic review', request_id='numeric-text')
    second = box.ingest('synthetic.xlsx', workbook('075'))
    changed = issue(second)
    assert changed['text'] == old['text']
    assert changed['key'] == old['key']
    assert changed['fp'] != old['fp']
    carried = {k: dict(v, carried_from=first['file_hash']) for k, v in saved['finding_decisions'].items()}
    second = box._update(second['file_hash'], lambda r: r.update(finding_decisions=copy.deepcopy(carried)))
    rv = summarize(second)
    current = next(x for x in rv['issues'] if x['key'] == old['key'])
    assert current['previous_decision']['action'] == 'reject'
    assert not current.get('decision')
    assert all(x['key'] != old['key'] for x in rv['dismissed'])


def test_unchanged_typed_cell_can_keep_verified_record_decision(tmp_path):
    box = SpreadsheetInbox(tmp_path)
    first = box.ingest('synthetic.xlsx', workbook('072'))
    old = issue(first)
    saved, _ = decide_displayed(box, first['file_hash'], ids=[old['key']], action='reject', reason='Synthetic review', request_id='unchanged')
    wb = load_workbook(io.BytesIO(workbook('072')))
    wb['Inspection Detail']['A1'] = 'Synthetic later export'
    out = io.BytesIO(); wb.save(out); wb.close()
    second = box.ingest('synthetic.xlsx', out.getvalue())
    assert issue(second)['fp'] == old['fp']
    carried = {k: dict(v, carried_from=first['file_hash']) for k, v in saved['finding_decisions'].items()}
    second = box._update(second['file_hash'], lambda r: r.update(finding_decisions=carried))
    assert any(x['key'] == old['key'] for x in summarize(second)['dismissed'])


def test_undo_requires_the_displayed_run_even_without_selected_ids(tmp_path):
    import pytest
    from hosting.review_decisions import DecisionConflict
    from hosting.review_summary import check_run
    box = SpreadsheetInbox(tmp_path)
    first = box.ingest('synthetic.xlsx', workbook('072'))
    old = issue(first)
    saved, entry = decide_displayed(box, first['file_hash'], ids=[old['key']], action='reject', reason='Synthetic review', request_id='before-undo')
    before = box.get(first['file_hash'])
    for run in (None, dict(check_run(saved), source_check='changed')):
        with pytest.raises(DecisionConflict):
            box.decide_findings(first['file_hash'], action='undo', undo_of=entry['decision_id'], request_id='undo-stale',
                                expected_rev=saved['finding_decisions_rev'], expected_run=run)
        assert box.get(first['file_hash']) == before
    undone, _ = decide_displayed(box, first['file_hash'], action='undo', undo_of=entry['decision_id'], request_id='undo-current')
    assert not undone.get('finding_decisions')
