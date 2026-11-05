"""One review summary per report: Hold / Check / Clear, counts, and a single issue list.

Combines the automatic checks, the SharePoint comparison, the usual-day comparison and the AI review
so every screen shows the same severities. Read-only: computed from the saved result.
"""
import re

INFO = {'hyperlink_formula', 'external_hyperlink', 'known_template_layout_differs', 'formula_results_used',
        'quantity_formulas_not_evaluated', 'no_data_rows', 'no_quantity_columns_detected'}
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
    return 'high' if HIGH.search(code) else 'medium'


def _where(f):
    cell = f.get('cell') or (f'{f["column"]}{f["row"]}' if isinstance(f.get('column'), str) and f.get('row') else None)
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


def summarize(result, with_issues=True):
    from hosting.source_compare import identify
    issues = []
    for i, f in enumerate(result.get('findings') or []):
        issues.append(dict(key=f'check:{f.get("code")}:{f.get("sheet")}:{f.get("row")}:{f.get("column")}:{i}', source='Checks',
                           severity=severity(f.get('code')), code=f.get('code'), where=_where(f), sheet=f.get('sheet'), row=f.get('row'),
                           text=_plain(f)))
    sp = result.get('sharepoint') or {}
    for i, f in enumerate(sp.get('findings') or []):
        issues.append(dict(key=f'sp:{f.get("code")}:{f.get("row")}:{f.get("field")}:{i}', source='SharePoint', severity='high',
                           code=f.get('code'), where=_where(f), sheet=f.get('sheet'), row=f.get('row'), text=f.get('message')))
    bl = result.get('baseline') or {}
    for i, f in enumerate(bl.get('findings') or []):
        issues.append(dict(key=f'usual:{f.get("code")}:{i}', source='Usual day', severity=f.get('severity') or 'medium',
                           code=f.get('code'), where='Whole report', text=f.get('message')))
    ai = result.get('ai_review') or {}
    for i, f in enumerate(ai.get('issues') or [] if ai.get('status') == 'done' else []):
        issues.append(dict(key=f'ai:{i}:{f.get("sheet")}:{f.get("cell") or f.get("row")}', source='AI review',
                           severity=f.get('severity') or 'medium', code=f.get('category'),
                           where=' · '.join(p for p in (f.get('sheet'), f.get('cell') or (f'row {f["row"]}' if f.get('row') else None)) if p),
                           sheet=f.get('sheet'), row=f.get('row'), text=f.get('issue'), why=f.get('why'), check=f.get('check')))
    if not result.get('record_count'):
        issues.append(dict(key='none:rows', source='Checks', severity='high', code='no_rows_found', where='Whole file',
                           text='No inspection rows were found in this file, so nothing could be checked. Check that this is the end-of-shift report.'))
    issues.sort(key=lambda x: RANK.get(x['severity'], 1))
    counts = {k: sum(1 for x in issues if x['severity'] == k) for k in RANK}
    status = 'hold' if counts['high'] else 'check' if counts['medium'] or counts['low'] else 'clear'
    totals = result.get('totals') or {}
    job, day = identify(result.get('filename'), result)
    out = dict(status=status, counts=counts, job=job, day=day,
               scans=result.get('record_count'), inspected=_num(totals.get('inspected')), rejected=_num(totals.get('rejected')),
               certified=_num(totals.get('certified')),
               sharepoint=dict(status=sp.get('status') or 'not_checked', message=sp.get('message'), matched=sp.get('matched_rows'),
                               rows=sp.get('sharepoint_rows'), web_url=sp.get('web_url'), source=sp.get('path')),
               usual=dict(status=bl.get('status') or 'not_checked', message=bl.get('message')),
               ai=dict(status=ai.get('status') or 'not_run', summary=ai.get('summary'), message=ai.get('message')),
               fixed_from=(result.get('fixed_from') or {}).get('filename'), fixes_applied=len(result.get('fixes_applied') or []))
    if with_issues:
        out['issues'] = issues
    return out
