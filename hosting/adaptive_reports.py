"""Format-agnostic quantity checks for inspection/sort reports in any layout.

Used whenever a workbook is not an exact match for a known PPG/Pratt template (or a
CSV has no first-row quantity headers). Header rows are found by meaning, anywhere in
the first rows of each sheet and again further down (multiple tables per sheet).
Formulas are never evaluated: a formula in a quantity cell is an unknown value, not zero.
Results are internal checks only; no source authenticity, approval or release.
"""
from collections import Counter
from decimal import Decimal, InvalidOperation
import re

TEMPLATE_ID = 'adaptive-columns/1'
SCAN_ROWS = 120          # rows searched for the first header row on each sheet
MAX_FINDINGS = 200
MAX_RECORDS_RETURNED = 500

# Words that may surround a field word in a header: "Total Parts Inspected (pcs)", "Qty Rejected", "# Good".
_FILLER = {'total', 'totals', 'qty', 'quantity', 'quantities', 'pcs', 'pc', 'pieces', 'piece', 'parts', 'part',
           'count', 'counts', 'no', 'number', 'of', 'num', 'units', 'unit', 'amount', 'daily', 'shift', 'sum', 'ea'}
FIELDS = {
    'inspected': {'inspected', 'sorted', 'checked', 'inspect', 'inspection', 'audited', 'reviewed', 'sort'},
    'rejected': {'rejected', 'rejects', 'reject', 'ng', 'nok', 'no good', 'defects', 'defect', 'defective', 'nonconforming',
                 'non conforming', 'nc', 'scrap', 'scrapped', 'failed', 'fail', 'bad', 'suspect', 'held', 'on hold'},
    'certified': {'certified', 'accepted', 'accept', 'good', 'passed', 'pass', 'ok', 'conforming', 'released',
                  'approved', 'clean', 'cleared', 'acceptable'},
}
TOTAL_WORDS = ('total', 'grand total', 'totals', 'subtotal', 'sub total', 'shift total', 'daily total', 'sum')
LABEL_WORDS = {'part number': 'part', 'part': 'part', 'part no': 'part', 'part num': 'part', 'p n': 'part', 'pn': 'part',
               'part name': 'part', 'item': 'part', 'item number': 'part', 'sku': 'part', 'shift': 'shift', 'date': 'date',
               'area': 'area', 'line': 'area', 'location': 'area', 'lot': 'lot', 'identifier lot': 'lot', 'lot number': 'lot',
               'batch': 'lot', 'serial': 'lot'}


def _norm(value):
    return re.sub(r'\s+', ' ', re.sub(r'[^a-z0-9]+', ' ', str(value).lower())).strip()


def field_for_header(value):
    """Map one header cell to inspected/rejected/certified, or None. 'Inspected for' stays unmapped."""
    if not isinstance(value, str):
        return None
    words = _norm(value).split()
    if not words or len(words) > 6:
        return None
    while words and words[0] in _FILLER:
        words.pop(0)
    while words and words[-1] in _FILLER:
        words.pop()
    core = ' '.join(words)
    for field, names in FIELDS.items():
        if core in names:
            return field
    return None


def label_for_header(value):
    return LABEL_WORDS.get(_norm(value)) if isinstance(value, str) else None


def _header_map(row):
    mapping, labels = {}, {}
    for index, value in enumerate(row):
        field = field_for_header(value)
        if field and field not in mapping:
            mapping[field] = index
        label = label_for_header(value)
        if label and label not in labels:
            labels[label] = index
    return mapping, labels


def _text_cells(row):
    return sum(1 for v in row if isinstance(v, str) and v.strip() and not v.startswith('='))


def _column(index):
    letters, n = '', index + 1
    while n:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _value(raw):
    """Return (number|None, state). States: number, number_as_text, formula, blank, invalid."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None, 'blank'
    if isinstance(raw, bool):
        return None, 'invalid'
    if isinstance(raw, (int, float)):
        try:
            n = Decimal(str(raw))
        except InvalidOperation:
            return None, 'invalid'
        return (n, 'number') if n.is_finite() else (None, 'invalid')
    text = str(raw).strip()
    if text.startswith('='):
        return None, 'formula'
    cleaned = text.replace(',', '')
    if re.fullmatch(r'-?\d+(\.\d+)?', cleaned):
        return Decimal(cleaned), 'number_as_text'
    if text.lower() in ('-', 'n/a', 'na', 'none', 'nil'):
        return None, 'blank'
    return None, 'invalid'


def _is_total_row(row, mapping):
    used = set(mapping.values())
    for index, value in enumerate(row):
        if index in used:
            continue
        if isinstance(value, str) and value.strip():
            return _norm(value) in TOTAL_WORDS or _norm(value).endswith(' total')
    return False


def inspect_adaptive(sources, base):
    """Return an adaptive result if any quantity column is found in any sheet, else None."""
    result = dict(base, template_id=TEMPLATE_ID, mapping_mode='adaptive', template_mapping_status='matched',
                  verification_scope='adaptive_column_internal_checks_only', sheets=[], findings=[], records=[],
                  source_authenticity='not_supplied', source_reconciliation_status='not_performed',
                  approval_effective=False, release_status='held')
    result['limitations'] = list(base.get('limitations', [])) + [
        'Quantity columns were detected automatically from header text; review the detected columns below.',
        'Formulas in quantity cells are unknown values, never evaluated and never treated as zero.']
    counts = Counter()

    def finding(code, sheet, row=None, column=None, message=None):
        counts[code] += 1
        if len(result['findings']) < MAX_FINDINGS:
            result['findings'].append(dict(code=code, sheet=sheet, row=row, column=column, **({'message': message} if message else {})))
        else:
            result['findings_truncated'] = True

    grand = {f: Decimal(0) for f in FIELDS}
    grand_known = {f: True for f in FIELDS}
    record_count = numeric_cells = formula_cells = total_rows = total_cells = 0
    any_mapped = False

    for title, rows in sources:
        rows = list(rows)
        sheet = dict(name=title, row_count=len(rows), tables=[], data_row_count=0, mapping_status='no_quantity_columns')
        mapping, labels, header_row = {}, {}, None
        best = (0, None, {}, {})
        for r, row in enumerate(rows[:SCAN_ROWS], 1):
            m, lab = _header_map(row)
            if m and _text_cells(row) >= 2 and len(m) > best[0]:
                best = (len(m), r, m, lab)
        if best[1] is None:
            result['sheets'].append(sheet)
            finding('no_quantity_columns_detected', title, message='No inspected/rejected/certified header was found. This sheet was not quantity-checked.')
            continue
        any_mapped = True
        sheet['mapping_status'] = 'adaptive'
        segment = {f: Decimal(0) for f in FIELDS}
        segment_known = {f: True for f in FIELDS}
        segment_rows = 0
        table = None

        def start_table(r, m, lab):
            nonlocal mapping, labels, header_row, segment, segment_known, segment_rows, table
            mapping, labels, header_row = m, lab, r
            segment = {f: Decimal(0) for f in FIELDS}
            segment_known = {f: True for f in FIELDS}
            segment_rows = 0
            table = dict(header_row=r, columns={f: dict(column=_column(i), header=str(rows[r - 1][i]).strip()) for f, i in m.items()},
                         data_row_count=0, totals={})
            sheet['tables'].append(table)

        start_table(best[1], best[2], best[3])
        for r in range(best[1] + 1, len(rows) + 1):
            row = list(rows[r - 1])
            total_rows += 1
            total_cells += len(row)
            m, lab = _header_map(row)
            if m and _text_cells(row) >= 2 and len(m) >= max(1, len(mapping)) and not any(_value(row[i])[1] in ('number', 'number_as_text') for i in m.values() if i < len(row)):
                start_table(r, m, lab)
                continue
            cells = {f: _value(row[i]) if i < len(row) else (None, 'blank') for f, i in mapping.items()}
            if all(state == 'blank' for _, state in cells.values()):
                continue
            if _is_total_row(row, mapping):
                for f, (n, state) in cells.items():
                    if state in ('number', 'number_as_text'):
                        if segment_rows and segment_known[f] and n != segment[f]:
                            finding('total_mismatch', title, r, mapping[f] + 1,
                                    f'Stated {f} total does not equal the sum of the {segment_rows} rows above it.')
                        table['totals'][f] = dict(stated=str(n), sum_of_rows=str(segment[f]) if segment_known[f] else None)
                    elif state == 'formula':
                        formula_cells += 1
                segment = {f: Decimal(0) for f in FIELDS}
                segment_known = {f: True for f in FIELDS}
                segment_rows = 0
                continue
            values = {}
            for f, (n, state) in cells.items():
                col = mapping[f] + 1
                if state in ('number', 'number_as_text'):
                    numeric_cells += 1
                    if state == 'number_as_text':
                        finding('number_stored_as_text', title, r, col)
                    if n < 0:
                        finding('negative_count_or_quantity', title, r, col)
                    if n != n.to_integral_value():
                        finding('fractional_count', title, r, col)
                    values[f] = n
                    segment[f] += n
                    grand[f] += n
                else:
                    values[f] = None
                    segment_known[f] = False
                    grand_known[f] = False
                    if state == 'formula':
                        formula_cells += 1
                    elif state == 'invalid':
                        finding('invalid_numeric_value', title, r, col)
                    elif state == 'blank' and any(v[1] in ('number', 'number_as_text') for v in cells.values()):
                        finding('missing_cell', title, r, col)
            if all(values.get(f) is not None for f in ('inspected', 'rejected', 'certified')):
                if values['inspected'] != values['certified'] + values['rejected']:
                    finding('inspected_count_mismatch', title, r,
                            message='Inspected does not equal certified/accepted plus rejected in this row.')
            elif values.get('inspected') is not None and values.get('rejected') is not None and values['rejected'] > values['inspected']:
                finding('rejected_exceeds_inspected', title, r)
            segment_rows += 1
            table['data_row_count'] += 1
            sheet['data_row_count'] += 1
            record_count += 1
            if len(result['records']) < MAX_RECORDS_RETURNED:
                part = row[labels['part']] if 'part' in labels and labels['part'] < len(row) else None
                part = None if part is None or (isinstance(part, str) and part.startswith('=')) else part
                result['records'].append(dict(sheet=title, row=r, part=str(part) if part is not None else None,
                                              values={f: (str(v) if v is not None else None) for f, v in values.items()}))
        if sheet['data_row_count'] == 0:
            finding('no_data_rows', title, header_row, message='Quantity headers were found but no rows with quantities follow them.')
        result['sheets'].append(sheet)

    if not any_mapped:
        return None
    if formula_cells:
        finding('quantity_formulas_not_evaluated', 'Workbook',
                message=f'{formula_cells} quantity cell(s) contain formulas. They were not evaluated, so those values are unknown.')
    result.update(
        record_count=record_count, numeric_cells_checked=numeric_cells, formula_count=formula_cells,
        records_truncated=record_count > MAX_RECORDS_RETURNED,
        totals={f: dict(total=str(grand[f]) if grand_known[f] else None, known_sum=str(grand[f])) for f in FIELDS},
        finding_counts=dict(counts), finding_count=sum(counts.values()),
        findings_truncated=sum(counts.values()) > MAX_FINDINGS, row_count=total_rows, cell_count=total_cells)
    issues = {c for c in counts if c not in ('no_quantity_columns_detected', 'no_data_rows', 'quantity_formulas_not_evaluated')}
    result['internal_check_status'] = ('unverified' if not numeric_cells else 'findings' if issues else 'no_findings_in_checks_performed')
    return result
