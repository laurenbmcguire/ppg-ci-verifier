"""An isolated copy of the review state of the November 6 SL2200 report (version 6), built from synthetic rows.

No real report is read or changed. The finding layout mirrors what the live report had when the reviewer tested it
(read from its saved check, counts only): 101 blank rework cells in column L (rows 4-104), 95 blank NOK cells in
column M, Claude's earlier double-check that settled L4:L80 and most of column M, a broken-clip blocker on row 19,
five other things to review, four informational notes, and 25 part-number findings a person had already marked
fine (with the older record that has no cell). Before any decision the screen shows 1 blocker + 53 to review +
4 informational = 58.
"""
from hosting.review_summary import check_run
import io

from openpyxl import load_workbook

from tests.eos_fixture import add_cached_values
from tests.sl_daily_fixture import records, sl_daily

SHEET = 'Inspection Detail'
L, M = 12, 13
NOK_RECORDED_ROWS = (20, 33, 47, 60, 88, 97)          # the six rows that do have a NOK value (95 of 101 are blank)
M_UNVERDICTED_EXTRA = 80                              # one blank NOK cell Claude's double-check did not reach
BROKEN_CLIP_ROW = 19


def findings():
    out = [dict(code='missing_cell', sheet=SHEET, row=r, column=L) for r in range(4, 105)]
    out += [dict(code='missing_cell', sheet=SHEET, row=r, column=M) for r in range(4, 105) if r not in NOK_RECORDED_ROWS]
    out += [dict(code='part_format_differs', sheet=SHEET, row=r, message=f'Part number 00323798-RH has a different format from the other 76 (expected like ######## AA).')
            for r in (14, 17, 21, 26, 29, 32, 34, 37, 44, 48, 52, 55, 59, 63, 65, 68, 72, 77, 80, 81, 85, 91, 92, 93, 94)]
    out += [dict(code='outlier', sheet=SHEET, row=16, message='Quantity inspected 29 for 00356685 RH. Every other record for this part is 30.'),
            dict(code='missing_info', sheet=SHEET, row=40, message='Supplier is blank on this row.'),
            dict(code='unusual_auditor_id', sheet=SHEET, row=40, message='Auditor lookup ID 98113 is much higher than the others.'),
            dict(code='inconsistent', sheet=SHEET, row=96, message='Rows 96-104 (shift 3) have recorded times on 2026-11-07 01:04 to 05:37 UTC.'),
            dict(code='identifier_repeats', sheet=SHEET, row=50, message='Identifier 090726 also appears on source item 6230.')]
    out += [dict(code='hyperlink_formula', sheet=SHEET, row=r, column=17) for r in (4, 5, 6, 7)]
    return out


def photo_links():
    """The 109 photo hyperlinks the live report counted but never stored (107 on Inspection Detail, 2 on Defect Photos)."""
    return ([dict(code='external_hyperlink', sheet=SHEET, row=r, column=18) for r in range(4, 111)]
            + [dict(code='external_hyperlink', sheet='Defect Photos', row=r, column=2) for r in (4, 22)])


def legacy_key(f, i):
    return f'check:{f.get("code")}:{f.get("sheet")}:{f.get("row")}:{f.get("column")}:{i}'


def verdicts(fs):
    out = {}
    for i, f in enumerate(fs):
        if f['code'] != 'missing_cell':
            continue
        if f['column'] == L and f['row'] <= 80:
            out[legacy_key(f, i)] = dict(verdict='not_an_issue', reason='Rework is not recorded on this report.')
        if f['column'] == M:
            if f['row'] == BROKEN_CLIP_ROW:
                out[legacy_key(f, i)] = dict(verdict='valid', reason='The comment says 1 broken clip while NOK is blank.')
            elif f['row'] < M_UNVERDICTED_EXTRA:
                out[legacy_key(f, i)] = dict(verdict='not_an_issue', reason='Blank NOK, nothing recorded.')
    return out


def build(box, name='SL Alabama - 2200 - 2026-11-06.xlsx', complete=False, regenerated=False):
    """Upload a synthetic SL daily workbook into ``box`` and give its check the SL2200 review state.
    complete=False is the live state: 339 findings counted, 230 stored (the 109 photo hyperlinks were counted but not
    stored). complete=True is the same report re-checked with every finding stored."""
    wb = load_workbook(io.BytesIO(sl_daily(recs=records(101, inspected=47), job='2200')))
    d = wb[SHEET]
    for r in NOK_RECORDED_ROWS:   # 11 NOK recorded in all: OK is reduced to match, so the row adds up
        nok = 6 if r == 20 else 1
        d[f'M{r}'] = nok; d[f'K{r}'] = 47 - nok
    d[f'O{BROKEN_CLIP_ROW}'] = '1 broken clip'      # 47 inspected, 47 OK, NOK blank, defect blank, Condition OK
    if regenerated:                                   # the same day exported again: same records, different file
        d['A1'] = 'Inspection Detail (exported again)'
    buf = io.BytesIO(); wb.save(buf)
    item = box.ingest(name, add_cached_values(buf.getvalue(), wb.sheetnames, {}))   # same bytes every run
    fs = findings() + (photo_links() if complete else [])
    fine = [dict(code='part_format_differs', sheet=SHEET, row=f['row'], text=f['message'], reason='Marked fine as is by Lauren.', by_person=True)
            for f in fs if f['code'] == 'part_format_differs']
    def state(r):
        counts = {'missing_cell': 196, 'part_format_differs': 25, 'external_hyperlink': 109, 'outlier': 1, 'missing_info': 1,
                  'unusual_auditor_id': 1, 'inconsistent': 1, 'identifier_repeats': 1, 'hyperlink_formula': 4}
        r.update(findings=fs, finding_counts=counts, finding_count=sum(counts.values()), findings_truncated=not complete, accepted_issues=fine)
        from hosting.spreadsheets import _attach_observed_values
        _attach_observed_values(r, name, buf.getvalue())
        # This fixture models exact reviewed historical acceptances, not unsafe unbound records.
        from hosting.review_summary import summarize
        shown = summarize(dict(r, accepted_issues=[]))['issues']
        for a in fine:
            x = next(x for x in shown if x.get('code') == a['code'] and x.get('row') == a['row'])
            a.update(fp=x['fp'], run=check_run(r))
        r['ai_verify'] = dict(status='done', verdicts=verdicts(fs), run=check_run(r))
    box._update(item['file_hash'], state)
    box.record_ai_review(item['file_hash'], dict(status='done', summary='', issues=[dict(
        issue='Row 19 comment says "1 broken clip" but NOK is blank, Recorded defect is blank and Condition is OK. The captured '
              'source has the same conflict: confirm with the source owner.', severity='high', sheet=SHEET, cell='O19', category='source_conflict')]))
    return box.get(item['file_hash'])


def l_ids(rv, rows=range(81, 105)):
    want = {f'L{r}' for r in rows}
    return [x['key'] for x in rv['issues'] if x.get('code') == 'missing_cell' and str(x.get('cell')) in want]


def m_open(rv):
    return sorted(int(x['cell'][1:]) for x in rv['issues'] if x.get('code') == 'missing_cell' and str(x.get('cell', '')).startswith('M'))


def shown(review, ids, extra=()):
    """What a reviewer's screen sends with a decision: the check run it shows and the exact reading (fingerprint) of
    each chosen finding. ``review`` is the review part of a report view; ``extra`` are more loaded findings (pages)."""
    fps = {x['key']: x.get('fp') for x in list(review.get('issues') or []) + list(review.get('dismissed') or []) + list(extra)}
    return dict(run=review.get('run'), seen={k: fps[k] for k in ids if k in fps})
