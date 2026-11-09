"""Saved synthetic workbook regressions for independently checked static-review claims."""
import io

import pytest
from openpyxl import load_workbook

from hosting.review_summary import approval_current, check_run, summarize
from hosting.spreadsheet_inbox import SpreadsheetInbox
from hosting.spreadsheets import CHECKER_VERSION
from tests.sl_daily_fixture import sl_daily


@pytest.fixture
def stored(tmp_path):
    # Template bytes are generated locally. No source service, provider or model is used.
    workbook = load_workbook(io.BytesIO(sl_daily(source_records=False, photos=False, nok_blank=False, job='9001')))
    detail = workbook['Inspection Detail']
    detail['L5'] = '072'
    for sheet in workbook:
        for row in sheet:
            for cell in row:
                if isinstance(cell.value, str):
                    cell.value = cell.value.replace('ppg.sharepoint.com', 'source.example.invalid').replace('SL Alabama', 'Synthetic client')
    data = io.BytesIO()
    workbook.save(data)
    raw = data.getvalue()
    box = SpreadsheetInbox(tmp_path)
    item = box.ingest('Synthetic - 9001 - 2026-11-06.xlsx', raw)
    return box, item, raw


def finding(item, code='number_stored_as_text', cell='L5'):
    review = summarize(item)
    return next(x for x in review['issues'] + review['dismissed'] if x['code'] == code and x.get('cell') == cell)


def acceptance(item, issue, by_person=False, bound=True):
    value = {k: issue.get(k) for k in ('code', 'sheet', 'row', 'cell', 'text')}
    value.update(reason='Historical synthetic acceptance', by_person=by_person)
    if bound:
        value.update(fp=issue['fp'], run=check_run(item))
    return value


@pytest.mark.parametrize('by_person', [False, True])
def test_legacy_acceptance_cannot_hide_changed_numeric_text_in_child(stored, by_person):
    box, item, raw = stored
    issue = finding(item)
    h = item['file_hash']
    reviewed = box._update(h, lambda r: r.update(accepted_issues=[acceptance(item, issue, by_person)]))
    child = box.apply_fixes(h, [dict(sheet='Inspection Detail', cell='L5', before='072', after='075', kind='text',
                                    source='Your edit', reason='Synthetic explicit correction')],
                            expected_check_hash=reviewed['check_hash'])
    child_issue = finding(child)
    assert child_issue['fp'] != issue['fp'], 'Changed numeric text must have a distinct fingerprint.'
    assert child_issue['key'] in {x['key'] for x in summarize(child)['issues']}, 'A prior acceptance hid the new 075 finding.'
    assert box.original_bytes(h)[1] == raw
    assert load_workbook(io.BytesIO(box.original_bytes(child['file_hash'])[1]))['Inspection Detail']['L5'].value == '075'


@pytest.mark.parametrize('by_person', [False, True])
def test_unbound_legacy_acceptance_does_not_hide_a_current_finding(stored, by_person):
    box, item, _ = stored
    issue = finding(item)
    updated = box._update(item['file_hash'], lambda r: r.update(accepted_issues=[acceptance(item, issue, by_person, bound=False)]))
    assert issue['key'] in {x['key'] for x in summarize(updated)['issues']}, 'Unbound legacy acceptance silently dismissed a finding.'


def test_ai_acceptance_never_hides_high_severity_blockers(stored):
    box, item, _ = stored
    before = summarize(item)
    blockers = [x for x in before['issues'] if x['severity'] == 'high']
    assert blockers, 'Fixture must contain a real checker-generated blocker.'
    updated = box._update(item['file_hash'], lambda r: r.update(accepted_issues=[acceptance(item, x) for x in blockers]))
    after = summarize(updated)
    assert after['counts']['high'] == before['counts']['high'], 'AI accepted_issues reduced the blocker count.'
    assert {x['key'] for x in blockers} <= {x['key'] for x in after['issues']}


def test_outdated_checker_record_cannot_be_approved(stored):
    box, item, _ = stored
    old = box._update(item['file_hash'], lambda r: r.update(checker_version='synthetic-older-checker'))
    assert old['checker_version'] != CHECKER_VERSION
    with pytest.raises(ValueError):
        box.approve(item['file_hash'], 'Synthetic reviewer', note='Synthetic override', override=True,
                    expected_check_hash=old['check_hash'], expected_run=check_run(old))
    assert not box.get(item['file_hash']).get('approval')


def test_outdated_historical_approval_is_never_displayed_current(stored):
    box, item, _ = stored
    current = box.approve(item['file_hash'], 'Synthetic reviewer', note='Synthetic override', override=True,
                          expected_check_hash=item['check_hash'], expected_run=check_run(item))
    assert approval_current(current)
    def historical(r):
        r['checker_version'] = 'synthetic-older-checker'
        r['approval']['run'] = check_run(r)
        r['approval']['approved_state_hash'] = box._approval_state(r)
    older = box._update(item['file_hash'], historical)
    assert not approval_current(older), 'A coherently stored approval from an old checker was displayed as current.'
    assert summarize(older)['approval_current'] is False
