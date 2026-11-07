"""One review summary per report: Hold / Check / Clear, counts, and a single issue list.

Combines the automatic checks, the SharePoint comparison, the usual-day comparison and the AI review
so every screen shows the same severities. Read-only: computed from the saved result.
"""
import re

from hosting.explain import explain

INFO = {'hyperlink_formula', 'external_hyperlink', 'known_template_layout_differs', 'formula_results_used',
        'quantity_formulas_not_evaluated', 'no_data_rows', 'no_quantity_columns_detected'}
# New checks that are worth a look but should not stop a report on their own.
MEDIUM = {'duplicate_count_block', 'total_row_blank', 'defect_reject_mismatch'}
LOW = {'labeled_total_unit_differs', 'date_stored_as_text', 'zero_inspected', 'hidden_columns_with_data',
       'extra_spaces', 'number_stored_as_text'}
HIGH = re.compile(r'duplicate|total|differs|missing_from_report|not_on_sharepoint|typo|mismatch|exceeds|future|invalid|'
                  r'excel_error|wrong_column|scientific')
RANK = {'high': 0, 'medium': 1, 'low': 2, 'info': 3}


PLAIN = {
    'number_stored_as_text': 'A quantity is typed as text, so it will not add into the totals.',
    'extra_spaces': 'A value has extra spaces, which can stop it matching other rows.',
    'missing_part_number': 'Part number is blank on this row.',
    'missing_identifier': 'Identifier / scan number is blank on this row.',
    'missing_defect_reason': 'Rejected parts are recorded but no defect reason is given.',
    'rejected_exceeds_inspected': 'More parts are rejected than were inspected.',
    'quantity_math_mismatch': 'Inspected does not equal OK plus rejected.',
    'duplicate_row': 'This row is an exact copy of another row.',
    'formula_results_used': 'Formula cells were checked using the results Excel saved.',
}


def _plain(f):
    code = f.get('code') or ''
    text = f.get('message')
    if text and text.strip() != code:
        return text
    return PLAIN.get(code) or (code.replace('_', ' ').capitalize() + '.')


def severity(code):
    code = code or ''
    if code in INFO:
        return 'info'
    if code in LOW:
        return 'low'
    if code in MEDIUM:
        return 'medium'
    return 'high' if HIGH.search(code) else 'medium'


def _annotate(issue):
    """Add why / fix / sure from the explanation catalog. AI findings keep their own why and check, and are never sure."""
    e = explain(issue.get('code'))
    if issue.get('source') == 'AI review':
        issue['why'] = issue.get('why') or e['why']
        issue['fix'] = issue.get('check') or e['fix']
        issue['sure'] = False
    else:
        issue['why'], issue['fix'], issue['sure'] = e['why'], e['fix'], e['sure']
    issue['only'] = e.get('only')   # set when the only problem is how something is written
    return issue


def check_run(result):
    """The check run everything on the screen belongs to: this exact file, this checker, this SharePoint snapshot."""
    from hosting import reconcile
    rec = reconcile.current(result) or {}
    return dict(file_hash=result.get('file_hash'), checker_version=result.get('checker_version'), snapshot_id=rec.get('snapshot_id'))


def same_run(bound, result):
    """True only when something (an AI review, a verdict list, an approval) was made on this exact check run."""
    if not isinstance(bound, dict):
        return False
    cur = check_run(result)
    return all(bound.get(k) == cur[k] for k in ('file_hash', 'checker_version', 'snapshot_id'))


def _source_status(result, sp):
    """One source verdict for the whole screen."""
    from hosting import reconcile
    if result.get('source_reconciliation'):
        rec = reconcile.current(result)
        if not rec:
            return dict(status='stale', message='The SharePoint check was made for an earlier version of this report. Reconcile again.')
        word = {'verified': 'match', 'discrepancies': 'differences', 'incomplete': 'incomplete', 'unverified': 'unverified'}.get(rec['status'], rec['status'])
        return dict(status=word, message=' '.join(rec.get('not_verified_because') or (rec.get('coverage') or {}).get('notes') or []) or None,
                    matched=rec.get('report_rows'), rows=rec.get('sharepoint_items_for_day'), web_url=(rec.get('list') or {}).get('url'),
                    source=(rec.get('list') or {}).get('name'), captured_at=rec.get('captured_at'), snapshot_id=rec.get('snapshot_id'),
                    fields_checked=rec.get('fields_checked'), fields_not_checked=rec.get('fields_not_checked'), differences=len(rec.get('rows') or []))
    legacy = sp.get('status') or 'not_checked'
    if legacy in ('match', 'differences'):
        # the older daily-export comparison matches by identifier and clock-time shift windows, not by SharePoint item:
        # it can point at differences but it can never verify a report
        return dict(status='day_level', day_level=legacy, message='Compared with the daily SharePoint export by totals and clock-time shifts, '
                    'not record by record (the rows carry no SharePoint item links). Not verified.',
                    matched=sp.get('matched_rows'), rows=sp.get('sharepoint_rows'), web_url=sp.get('web_url'), source=sp.get('path'))
    return dict(status=legacy, message=sp.get('message'), matched=sp.get('matched_rows'),
                rows=sp.get('sharepoint_rows'), web_url=sp.get('web_url'), source=sp.get('path'))


def _col_letter(n):
    s = ''
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _cell(f):
    col = f.get('column')
    if isinstance(col, int) and not isinstance(col, bool) and col > 0 and f.get('row'):   # a 1-based column number: name the exact cell
        return f'{_col_letter(col)}{f["row"]}'
    return f.get('cell') or (f'{col}{f["row"]}' if isinstance(col, str) and re.fullmatch(r'[A-Z]{1,3}', col) and f.get('row') else None)


def _where(f):
    cell = _cell(f) or (f'{f["column"]}{f["row"]}' if isinstance(f.get('column'), str) and f.get('row') else None)
    parts = [f.get('sheet') if f.get('sheet') not in (None, 'SharePoint comparison', 'Compared with usual') else None,
             cell or (f'row {f["row"]}' if f.get('row') else None)]
    return ' · '.join(p for p in parts if p)


def _num(total):
    if not isinstance(total, dict):
        return None
    value = total.get('known_sum') if total.get('known_sum') is not None else total.get('total')
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def summarize(result, with_issues=True, use_verdicts=True):
    from hosting.source_compare import identify
    issues = []
    for i, f in enumerate(result.get('findings') or []):
        issues.append(dict(key=f'check:{f.get("code")}:{f.get("sheet")}:{f.get("row")}:{f.get("column")}:{i}', source='Checks',
                           severity=severity(f.get('code')), code=f.get('code'), where=_where(f), sheet=f.get('sheet'), row=f.get('row'), cell=_cell(f),
                           text=_plain(f)))
    from hosting import reconcile as _rec
    sp = result.get('sharepoint') or {}
    if result.get('source_reconciliation'):
        # the item-by-item reconciliation is the one source check; the older daily-export comparison (matched by
        # identifier and clock-time shifts) is superseded and must not add its own differences or a second verdict
        sp = {}
    for i, f in enumerate(sp.get('findings') or []):
        issues.append(dict(key=f'sp:{f.get("code")}:{f.get("row")}:{f.get("field")}:{i}', source='SharePoint (day totals)', severity='medium',
                           code=f.get('code'), where=_where(f), sheet=f.get('sheet'), row=f.get('row'), cell=_cell(f), day_level=True,
                           text=f'{f.get("message")} (Day-level comparison by clock-time shifts, not by SharePoint item: check before changing anything.)'))
    rec = _rec.current(result) or {}
    if rec:   # each difference from the live SharePoint list is its own issue, with the cell when there is one
        cells = {(c.get('item'), c.get('field')): f'{_col_letter(int(c["column"]))}{c["row"]}' for c in rec.get('proposed_corrections') or [] if c.get('column') and c.get('row')}
        words = {'value_differs': 'differs from SharePoint', 'missing_from_report': 'is on SharePoint but not in the report',
                 'not_on_sharepoint': 'is not on SharePoint', 'not_on_sharepoint_for_day': 'is on SharePoint for another day',
                 'item_listed_twice': 'is listed twice', 'report_row_without_item_id': 'has no SharePoint item ID',
                 'changed_since_export': 'was edited on SharePoint after the export', 'export_value_differs': 'differs between the export tab and SharePoint',
                 'export_item_not_on_sharepoint': 'is on the export tab but no longer on SharePoint',
                 'wrong_list': 'comes from a different SharePoint site or list', 'field_not_in_source': 'has a value SharePoint has no column for, so it was not checked',
                 'undated': 'has no business date on SharePoint, so its day could not be confirmed'}
        for i, d in enumerate(rec.get('rows') or []):
            what = f'SharePoint item {d["item"]}' if d.get('item') else 'This row'
            detail = f': {d["field"]} is {d.get("report")} in the report, {d.get("sharepoint")} on SharePoint' if d.get('field') and d['status'] in ('value_differs', 'export_value_differs') else ''
            issues.append(dict(key=f'rec:{d["status"]}:{d.get("item")}:{d.get("field")}:{i}', source='SharePoint', severity='high' if d['status'] in ('value_differs', 'missing_from_report', 'not_on_sharepoint', 'item_listed_twice', 'wrong_list') else 'medium',
                               code='sharepoint_' + d['status'], where=f'{d["sheet"]} row {d["row"]}' if d.get('sheet') else 'SharePoint',
                               sheet=d.get('sheet'), row=d.get('row'), cell=cells.get((d.get('item'), d.get('field'))),
                               text=(f'{what}: {d["field"]} is {d.get("report")} in the report, {d.get("sharepoint")} on SharePoint.' if detail and d['status'] == 'value_differs'
                                     else f'{what} {words.get(d["status"], d["status"])}{detail}.' + (f' {d["note"]}' if d.get('note') and not detail else '')), url=d.get('url'),
                               item=d.get('item'), field=d.get('field'), report_value=d.get('report'), source_value=d.get('sharepoint'),
                               snapshot_id=rec.get('snapshot_id'), captured_at=rec.get('captured_at')))
    bl = result.get('baseline') or {}
    for i, f in enumerate(bl.get('findings') or []):
        issues.append(dict(key=f'usual:{f.get("code")}:{i}', source='Usual day', severity=f.get('severity') or 'medium',
                           code=f.get('code'), where='Whole report', text=f.get('message')))
    ai = result.get('ai_review') or {}
    ai_current = ai.get('status') == 'done' and same_run(ai.get('run'), result)
    for i, f in enumerate(ai.get('issues') or [] if ai_current else []):
        issues.append(dict(key=f'ai:{i}:{f.get("sheet")}:{f.get("cell") or f.get("row")}', source='AI review',
                           severity=f.get('severity') or 'medium', code=f.get('category'),
                           where=' · '.join(p for p in (f.get('sheet'), f.get('cell') or (f'row {f["row"]}' if f.get('row') else None)) if p),
                           sheet=f.get('sheet'), row=f.get('row'), cell=f.get('cell'), text=f.get('issue'), why=f.get('why'), check=f.get('check')))
    if not result.get('record_count'):
        issues.append(dict(key='none:rows', source='Checks', severity='high', code='no_rows_found', where='Whole file',
                           text='No inspection rows were found in this file, so nothing could be checked. Check that this is the end-of-shift report.'))
    for x in issues:
        _annotate(x)
    # Claude's double-check: drop false alarms (kept in 'dismissed' with the reason), mark confirmed ones
    verify = result.get('ai_verify') or {}
    dismissed = []
    if use_verdicts and verify.get('status') == 'done' and same_run(verify.get('run'), result):
        verdicts, kept = verify.get('verdicts') or {}, []
        for x in issues:
            v = verdicts.get(x['key'])
            if v:
                x['verdict'], x['verdict_reason'], x['fixable'] = v.get('verdict'), v.get('reason'), v.get('fixable')
            (dismissed if v and v.get('verdict') == 'not_an_issue' else kept).append(x)
        issues = kept
    # issues Claude (asked by the reviewer) confirmed are fine: shown under dismissed with the reason
    accepted = result.get('accepted_issues') or []
    if accepted:
        keep = []
        for x in issues:
            def same(a):
                if a.get('code') != x.get('code'):
                    return False
                if a.get('by_person'):   # a person's "fine as is" is about that one row: the same message on another row is still open
                    return a.get('text') == x.get('text') and (a.get('row') is None or (a.get('sheet') == x.get('sheet') and str(a.get('row')) == str(x.get('row'))))
                if a.get('cell') and x.get('cell'):   # Claude's "fine" is about that one cell
                    return a.get('sheet') == x.get('sheet') and str(a['cell']).upper() == str(x['cell']).upper()
                if a.get('row') is not None:   # ... or that row: the same message on another row is still checked
                    return a.get('sheet') == x.get('sheet') and str(a.get('row')) == str(x.get('row')) and (a.get('text') == x.get('text') or not x.get('cell'))
                return a.get('text') == x.get('text')
            hit = next((a for a in accepted if same(a)), None)
            if hit:
                dismissed.append(dict(x, verdict='not_an_issue', verdict_reason=(str(hit.get('reason') or '') if hit.get('by_person') else 'Claude checked: ' + str(hit.get('reason') or ''))))
            else:
                keep.append(x)
        issues = keep
    issues.sort(key=lambda x: RANK.get(x['severity'], 1))
    counts = {k: sum(1 for x in issues if x['severity'] == k) for k in RANK}
    status = 'hold' if counts['high'] else 'check' if counts['medium'] or counts['low'] else 'clear'
    totals = result.get('totals') or {}
    job, day = identify(result.get('filename'), result)
    out = dict(status=status, counts=counts, job=job, day=day,
               scans=result.get('record_count'), inspected=_num(totals.get('inspected')), rejected=_num(totals.get('rejected')),
               certified=_num(totals.get('certified')), rejected_unit=(totals.get('rejected') or {}).get('unit'),
               rejected_partial=bool(isinstance(totals.get('rejected'), dict) and totals['rejected'].get('total') is None and totals['rejected'].get('known_sum') is not None),
               sharepoint=_source_status(result, sp),
               usual=dict(status=bl.get('status') or 'not_checked', message=bl.get('message')),
               ai=dict(status=('stale' if ai.get('status') == 'done' and not ai_current else ai.get('status') or 'not_run'),
                       summary=ai.get('summary') if ai_current else None,
                       message=ai.get('message') if ai.get('status') != 'done' or ai_current else 'Claude\'s read was made on an earlier check run of this report; it is not used. Ask again.'),
               fixed_from=(result.get('fixed_from') or {}).get('filename'), fixes_applied=len(result.get('fixes_applied') or []),
               auto_hidden=(result.get('auto_formatting') or {}).get('hidden') or [x for x in [(result.get('auto_formatting') or {}).get('from_hash')] if x],
               verify=dict(status=('stale' if verify.get('status') == 'done' and not same_run(verify.get('run'), result) else verify.get('status') or 'not_run'),
                           dismissed=len(dismissed), message=verify.get('message')),
               approved=(result.get('approval') or {}).get('by'),
               approval_current=bool(result.get('approval')) and same_run((result.get('approval') or {}).get('run'), result),
               run=check_run(result),
               review_state=(result.get('review_request') or {}).get('status'),
               sent_at=next((x.get('at') for x in reversed(result.get('sent') or []) if x.get('sent')), None))
    if with_issues:
        out['issues'] = issues
        out['dismissed'] = dismissed
    return out
