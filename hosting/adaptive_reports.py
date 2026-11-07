"""Format-agnostic checks for inspection/sort reports in any layout.

Used whenever a workbook is not an exact match for a known PPG/Pratt template (or a CSV
has no first-row quantity headers). Header rows are found by meaning, anywhere in each
sheet, with any number of tables per sheet. Formulas are never executed; when Excel saved
a result for a formula, that saved result is used and labeled as such.

Checks: quantity rules, duplicates, likely typos, data validity, totals blocks and
Summary rows against the detail rows. Internal checks only: no source authenticity,
approval or release.
"""
from collections import Counter, defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import re

TEMPLATE_ID = 'adaptive-columns/1'
SCAN_ROWS = 400
MAX_FINDINGS = 300
MAX_RECORDS_RETURNED = 500

_FILLER = {'total', 'totals', 'qty', 'quantity', 'quantities', 'pcs', 'pc', 'pieces', 'piece', 'parts', 'part',
           'count', 'counts', 'no', 'number', 'of', 'num', 'units', 'unit', 'amount', 'daily', 'sum', 'ea',
           'recorded', 'reported', 'logged', 'entered'}   # "Recorded rejects", "Reported NG" are the reject column
FIELDS = {
    'inspected': {'inspected', 'sorted', 'checked', 'inspect', 'audited', 'reviewed', 'sort', 'actual', 'counted', 'actual count'},
    'rejected': {'rejected', 'rejects', 'reject', 'ng', 'nok', 'no good', 'defects', 'defective', 'nonconforming',
                 'non conforming', 'scrap', 'scrapped', 'failed', 'fail', 'bad', 'suspect'},
    'certified': {'certified', 'accepted', 'accept', 'good', 'passed', 'pass', 'ok', 'conforming', 'released',
                  'approved', 'clean', 'cleared', 'acceptable'},
    'reworked': {'reworked', 'rework', 'repaired'},
}
QTY = ('inspected', 'certified', 'rejected', 'reworked')
TOTAL_WORDS = ('total', 'grand total', 'totals', 'subtotal', 'sub total', 'shift total', 'daily total', 'day total', 'sum')
LABELS = {
    'identifier': {'identifier', 'identifier lot', 'id', 'lot', 'lot number', 'lot no', 'serial', 'serial number', 'serial no',
                   'tag', 'label', 'container', 'container id', 'box', 'skid', 'batch', 'barcode', 'scan', 'scan id',
                   'vin', 'vin number', 'vin no', 'vin inspected', 'vehicle vin', 'vin scanned',
                   'delivery note', 'delivery note number', 'delivery note no', 'packing slip', 'packing slip number', 'asn', 'shipper'},
    'part': {'part number', 'part', 'part no', 'part num', 'p n', 'pn', 'part name', 'item', 'item number', 'sku', 'part id',
             'vehicle', 'model', 'vehicle model'},
    'date': {'date', 'inspection date', 'scan date', 'report date'},
    'shift': {'shift', 'shift no', 'shift number'},
    'defect': {'defect', 'defects', 'defect type', 'reject reason', 'reason', 'defect reason', 'nok reason', 'failure mode', 'issue', 'suspect reason',
               'reason for reject', 'reject reason s'},
    'condition': {'condition', 'status', 'result', 'ok nok', 'ok nok status', 'disposition', 'audit result', 'audit status',
                  'pass fail', 'inspection result', 'audit outcome'},
    'auditor': {'auditor', 'auditor lookup id', 'inspector', 'inspected by', 'operator', 'sorter', 'technician'},
    'area': {'area', 'line', 'location', 'station', 'cell'},
    'expected': {'order', 'ordered', 'order qty', 'order quantity', 'expected', 'expected qty', 'expected quantity', 'planned'},
    'comment': {'comment', 'comments', 'note', 'notes', 'remark', 'remarks', 'observation', 'observations'},
}
CONFUSABLE = str.maketrans({'O': '0', 'o': '0', 'I': '1', 'l': '1', 'i': '1', 'S': '5', 's': '5', 'B': '8', 'Z': '2', 'z': '2'})


def norm(value):
    return re.sub(r'\s+', ' ', re.sub(r'[^a-z0-9]+', ' ', str(value).lower())).strip()


def header_text(value):
    """Readable form of a header: SharePoint internal names like 'OrderQty_x002e_' or 'PartNumber' -> 'Order Qty.' / 'Part Number'."""
    if not isinstance(value, str):
        return value
    text = re.sub(r'_x([0-9a-fA-F]{4})_', lambda m: chr(int(m.group(1), 16)), value)
    text = re.sub(r'(?<=[a-z0-9])(?=[A-Z])', ' ', text)          # camelCase -> camel Case
    text = re.sub(r'(?<=[A-Z])(?=[A-Z][a-z])', ' ', text)         # IDNumber -> ID Number
    return text.replace('_', ' ')


def field_for_header(value):
    """Map one header cell to a quantity field, or None. 'Inspected for' and a bare 'Qty' stay unmapped."""
    if not isinstance(value, str):
        return None
    words = norm(header_text(value)).split()
    if not words or len(words) > 12:
        return None
    has_quantity_word = any(w in ('qty', 'quantity', 'quantities', 'count', 'pcs', 'total') for w in words)
    while words and words[0] in _FILLER:
        words.pop(0)
    while words and words[-1] in _FILLER:
        words.pop()
    core = ' '.join(words)
    if core in ('defects', 'defect') and not has_quantity_word:
        return None  # 'Defects' is usually the defect description
    for field, names in FIELDS.items():
        if core in names and len(words) <= 6:
            return field
    # Descriptive headers: "Quantity Inspected 8 Wheel Weights per Vehicle", "Suspect Wheel Position Quantity".
    # Only when the header names a quantity, so "Inspected For" and "Reject Reason" stay labels.
    if has_quantity_word and words and (len(words) == 1 or words[1] not in ('for', 'by', 'reason', 'reasons', 'rate', 'date', 'time', 'percent', 'type')):
        for field, names in FIELDS.items():
            if words[0] in names:
                return field
    return None


def label_for_header(value):
    if not isinstance(value, str):
        return None
    key = norm(header_text(value))
    for label, names in LABELS.items():
        if key in names:
            return label
    if key.endswith(' condition') and len(key.split()) <= 3:
        return 'condition'  # LF Condition, Left Condition ...
    return None


def _header_map(row):
    mapping, labels = {}, {}
    for index, value in enumerate(row):
        field = field_for_header(value)
        if field and field not in mapping:
            mapping[field] = index
            continue
        label = label_for_header(value)
        if label and label not in labels:
            labels[label] = index
    return mapping, labels


def _text_cells(row):
    return sum(1 for v in row if isinstance(v, str) and v.strip() and not v.startswith('='))


def column_letter(index):
    letters, n = '', index + 1
    while n:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def number_state(raw, cached=None):
    """(Decimal|None, state). States: number, number_as_text, formula_result, formula, blank, invalid."""
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
        if isinstance(cached, (int, float)) and not isinstance(cached, bool):
            n = Decimal(str(cached))
            return (n, 'formula_result') if n.is_finite() else (None, 'formula')
        return None, 'formula'
    cleaned = text.replace(',', '')
    if re.fullmatch(r'-?\d+(\.\d+)?', cleaned):
        return Decimal(cleaned), 'number_as_text'
    if text.lower() in ('-', 'n/a', 'na', 'none', 'nil', '—', '–'):
        return None, 'blank'
    return None, 'invalid'


KNOWN = ('number', 'number_as_text', 'formula_result')


def label_text(raw, cached=None):
    if raw is None:
        return None
    if isinstance(raw, str) and raw.strip().startswith('='):
        raw = cached
        if raw is None:
            return None
    if isinstance(raw, float) and raw.is_integer():
        raw = int(raw)
    text = raw.isoformat() if isinstance(raw, (datetime, date)) else str(raw)
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] == '"':
        text = text[1:-1].strip()  # exported text like "2026-11-04T06:00:00Z"
    return text or None


def as_day(raw, cached=None):
    value = cached if isinstance(raw, str) and raw.strip().startswith('=') else raw
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        text = value.strip().strip('"').strip()
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}T[\d:.]+(Z|[+-]\d{2}:?\d{2})?', text):
            text = text[:10]  # ISO timestamp from a SharePoint export
        for fmt in ('%Y-%m-%d', '%m/%d/%Y', '%m/%d/%y', '%m-%d-%Y', '%m.%d.%y', '%m.%d.%Y', '%B %d, %Y', '%b %d, %Y'):
            try:
                return datetime.strptime(text, fmt).date()
            except ValueError:
                pass
    return None


def shift_number(text):
    if text is None:
        return None
    t = norm(text)
    m = re.fullmatch(r'(?:shift )?([123])(?:st|nd|rd)?(?: shift)?', t)
    if m:
        return m.group(1)
    return {'first': '1', 'second': '2', 'third': '3', 'day': None, 'night': None}.get(t.replace(' shift', ''), None)


def edit_distance_le1(a, b):
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        diffs = [i for i, (x, y) in enumerate(zip(a, b)) if x != y]
        if len(diffs) == 2 and diffs[1] == diffs[0] + 1 and a[diffs[0]] == b[diffs[1]] and a[diffs[1]] == b[diffs[0]]:
            return True  # two neighbouring characters swapped
        return len(diffs) == 1
    if len(a) > len(b):
        a, b = b, a
    i = j = diff = 0
    while i < len(a) and j < len(b):
        if a[i] != b[j]:
            diff += 1
            if diff > 1:
                return False
            j += 1
        else:
            i += 1; j += 1
    return True


def near_miss(a, b, one_char=True):
    """Two distinct codes that look like a typo of each other: a look-alike letter/digit, an inconsistent
    S prefix, or (when one_char) a single character off. Sequential serials differ by one digit, so callers
    comparing codes inside one report pass one_char=False."""
    if not a or not b or a == b:
        return False
    if a.translate(CONFUSABLE) == b.translate(CONFUSABLE):
        return True
    if a.lstrip('Ss') == b.lstrip('Ss') and a.lstrip('Ss'):
        return True
    return one_char and len(a) >= 5 and edit_distance_le1(a, b)


def shape(code):
    return re.sub(r'[A-Za-z]', 'A', re.sub(r'[0-9]', '9', code))


_COLUMN_RANGE = re.compile(r'\b(SUM|SUBTOTAL|COUNTA|COUNT|COUNTIF|COUNTIFS|SUMIF|SUMIFS)\s*\(\s*\$?([A-Z]{1,3})\$?(\d+)\s*:\s*\$?([A-Z]{1,3})\$?(\d+)', re.I)


def _is_total_row(row, mapping):
    used = set(mapping.values())
    for index in used:  # =SUM(M16:M76) style quantity cells: an unlabeled totals row
        value = row[index] if index < len(row) else None
        if isinstance(value, str) and value.startswith('='):
            m = _COLUMN_RANGE.search(value)
            if m and m.group(3) != m.group(5):
                return True
    for index, value in enumerate(row):
        if index in used:
            continue
        if isinstance(value, str) and value.strip() and not value.startswith('='):
            n = norm(value)
            return n in TOTAL_WORDS or n.endswith(' total') or n.startswith('total ')
    return False


def _row_label(row, mapping, labels):
    used = set(mapping.values()) | set(labels.values())
    for index, value in enumerate(row):
        if index not in used and isinstance(value, str) and value.strip() and not value.startswith('='):
            return value.strip()
    return None


class Checker:
    def __init__(self, result):
        self.result = result
        self.counts = Counter()

    def __call__(self, code, sheet, row=None, column=None, message=None):
        self.counts[code] += 1
        if len(self.result['findings']) < MAX_FINDINGS:
            item = dict(code=code, sheet=sheet, row=row, column=column)
            if message:
                item['message'] = message
            self.result['findings'].append(item)
        else:
            self.result['findings_truncated'] = True


def scan(sources, cached=None, layout_specs=None):
    """Find tables and rows. Returns (sheets, records, stats).
    layout_specs (from hosting.ai_layout) name the tables directly; sheets without one use header keywords."""
    cached = cached or {}
    sheets, records = [], []
    stats = Counter()
    for title, rows in sources:
        rows = list(rows)
        crow = list(cached.get(title, []))
        sheet = dict(name=title, row_count=len(rows), tables=[], data_row_count=0, mapping_status='no_quantity_columns')
        specs = sorted((t for t in (layout_specs or []) if t['sheet'] == title), key=lambda t: t['header_row'])
        spec_at = {t['header_row']: t for t in specs}
        best = (0, None, {}, {})
        if specs:
            t0 = specs[0]
            best = (1, t0['header_row'], *_spec_maps(t0))
        else:
            for r, row in enumerate(rows[:SCAN_ROWS], 1):
                m, lab = _header_map(row)
                if m and _text_cells(row) >= 2 and len(m) > best[0]:
                    best = (len(m), r, m, lab)
        if best[1] is None:
            sheets.append(sheet)
            continue
        sheet['mapping_status'] = 'adaptive'
        if specs:
            sheet['layout_source'] = 'ai'
        table = None
        active = None

        def start(r, m, lab):
            nonlocal table, active
            heads = rows[r - 1]
            active = spec_at.get(r)
            is_totals_block = active['kind'] == 'totals' if active else all(norm(heads[i]).startswith('total') for i in m.values())
            table = dict(index=len(sheet['tables']), header_row=r, kind='totals' if is_totals_block else 'detail',
                         columns={f: dict(column=column_letter(i), header=str(heads[i]).strip()) for f, i in m.items()},
                         labels={k: dict(column=column_letter(i), header=str(heads[i]).strip()) for k, i in lab.items()},
                         mapping=m, label_map=lab, data_row_count=0, total_rows=[],
                         condition_cols=([lab['condition']] if active and 'condition' in lab else
                                         [i for i, h in enumerate(heads) if label_for_header(h) == 'condition']))
            sheet['tables'].append(table)

        start(best[1], best[2], best[3])
        for r in range(best[1] + 1, len(rows) + 1):
            row = list(rows[r - 1])
            crw = list(crow[r - 1]) if r - 1 < len(crow) else []
            cget = (lambda i: crw[i] if i < len(crw) else None)
            stats['rows'] += 1
            stats['cells'] += len(row)
            if specs:
                if r in spec_at:
                    start(r, *_spec_maps(spec_at[r]))
                    continue
                if active is None or r < active['first_data_row'] or r > active['last_data_row']:
                    continue
                m, lab = {}, {}
            else:
                m, lab = _header_map(row)
            ident_col = table['label_map'].get('identifier')
            looks_like_data = (any(i in table['condition_cols'] and len(norm(row[i]).split()) <= 2 for i in m.values())  # 'OK' / 'SUSPECT' values, not headers
                               or (ident_col is not None and ident_col < len(row) and isinstance(row[ident_col], str)
                                   and row[ident_col].strip() and label_for_header(row[ident_col]) is None
                                   and field_for_header(row[ident_col]) is None and 'total' not in norm(row[ident_col]).split()))
            if m and not looks_like_data and _text_cells(row) >= 2 and len(m) >= max(1, len(table['mapping'])) \
                    and not any(number_state(row[i], cget(i))[1] in KNOWN and not isinstance(row[i], str) for i in m.values() if i < len(row)):
                start(r, m, lab)
                continue
            mapping, labmap = table['mapping'], table['label_map']
            cells = {f: number_state(row[i], cget(i)) if i < len(row) else (None, 'blank') for f, i in mapping.items()}
            if all(state == 'blank' for _, state in cells.values()):
                continue
            labels = {k: label_text(row[i], cget(i)) if i < len(row) else None for k, i in labmap.items()}
            typed = any(state in ('number', 'number_as_text') for _, state in cells.values())
            if table['kind'] != 'totals' and not typed and not any(labels.values()) and not _row_label(row, mapping, labmap):
                stats['blank_template_rows'] += 1
                continue  # an unused template row: only formulas, nothing entered
            day = as_day(row[labmap['date']], cget(labmap['date'])) if 'date' in labmap and labmap['date'] < len(row) else None
            for _, state in cells.values():
                stats[state] += 1
            record = dict(sheet=title, row=r, table=table['index'], table_kind=table['kind'],
                          values={f: n for f, (n, s) in cells.items()}, states={f: s for f, (n, s) in cells.items()},
                          labels=labels, day=day, row_label=_row_label(row, mapping, labmap),
                          is_total=_is_total_row(row, mapping) or (not (labels.get('identifier') or labels.get('part')) and any(
                              isinstance(v, str) and not v.startswith('=') and 'total' in norm(v).split() for v in row)),
                          columns={f: mapping[f] + 1 for f in mapping},
                          label_columns={k: column_letter(i) for k, i in labmap.items()},
                          conditions=[label_text(row[i], cget(i)) if i < len(row) else None for i in table['condition_cols']],
                          condition_headers=[str(rows[table['header_row'] - 1][i]).strip() for i in table['condition_cols']],
                          date_is_text=(isinstance(row[labmap['date']], str) and bool(row[labmap['date']].strip()) and not row[labmap['date']].startswith('='))
                          if 'date' in labmap and labmap['date'] < len(row) and row[labmap['date']] not in (None, '') else None)
            if record['is_total']:
                table['total_rows'].append(record)
            else:
                table['data_row_count'] += 1
                sheet['data_row_count'] += 1
            records.append(record)
        sheets.append(sheet)
    classify(sheets, records)
    return sheets, records, stats


def _spec_maps(spec):
    cols = spec['columns']
    return ({f: i for f, i in cols.items() if f in QTY}, {k: i for k, i in cols.items() if k in LABELS})


SUMMARY_SHEETS = {'summary', 'dashboard', 'overview', 'totals', 'day summary', 'daily summary', 'recap'}


def classify(sheets, records):
    """detail = row-level data with an Inspected column; supporting = reject logs etc. (no Inspected column);
    summary = summary sheets, or tables without identifier/part columns when other tables have them."""
    has_ids = any(t['kind'] == 'detail' and ('identifier' in t['labels'] or 'part' in t['labels'])
                  for s in sheets for t in s['tables'])
    kinds = {}
    for sheet in sheets:
        for t in sheet['tables']:
            if t['kind'] != 'totals':
                if norm(sheet['name']) in SUMMARY_SHEETS or (has_ids and not ('identifier' in t['labels'] or 'part' in t['labels'])):
                    t['kind'] = 'summary'
                elif 'inspected' not in t['columns']:
                    t['kind'] = 'supporting'
            kinds[(sheet['name'], t['index'])] = t['kind']
    for r in records:
        r['table_kind'] = kinds.get((r['sheet'], r['table']), r['table_kind'])
    for sheet in sheets:
        sheet['data_row_count'] = sum(1 for r in records if r['sheet'] == sheet['name'] and r['table_kind'] == 'detail' and not r['is_total'])


def _sum(rows, field):
    values = [r['values'].get(field) for r in rows if field in r['values']]
    if not values:
        return None, False
    known = all(v is not None for v in values)
    return sum((v for v in values if v is not None), Decimal(0)), known


def inspect_adaptive(sources, base, cached=None, report_day=None, hidden=None):
    """Return an adaptive result if any quantity column is found in any sheet, else None.
    When AI layout detection is set up, Claude maps the tables on every sheet each time; its reading is used
    when it finds more inspection rows than the header-keyword detection."""
    sources = [(title, list(rows)) for title, rows in sources]
    sheets, records, stats = scan(sources, cached)
    layout = None
    from hosting import ai_layout
    found = ai_layout.detect(sources, cached)
    if found:
        unit_sources, specs = ai_layout.apply_units(sources, found)
        a_sheets, a_records, a_stats = scan(unit_sources, cached, layout_specs=specs)
        if _detail_count(a_records) > _detail_count(records):
            sheets, records, stats, layout = a_sheets, a_records, a_stats, dict(found, tables=specs)
    if not any(s['mapping_status'] == 'adaptive' for s in sheets):
        return None
    result = dict(base, template_id=TEMPLATE_ID, mapping_mode='adaptive', template_mapping_status='matched',
                  verification_scope='adaptive_column_internal_checks_only', findings=[],
                  source_authenticity='not_supplied', source_reconciliation_status='not_performed',
                  approval_effective=False, release_status='held')
    result['limitations'] = list(base.get('limitations', [])) + [
        'Quantity columns were detected automatically from header text; review the detected columns.',
        'Formulas are never executed. Where Excel saved a result for a formula, that saved result was checked.']
    if layout:
        result['layout_source'] = 'ai'
        result['ai_layout'] = dict(model=layout.get('model'), notes=layout.get('notes'), tables=[
            dict(sheet=t['sheet'], kind=t['kind'], header_row=t['header_row'], first_data_row=t['first_data_row'],
                 last_data_row=t['last_data_row'], one_row_per_unit=t['one_row_per_unit'], why=t['why'],
                 columns={f: column_letter(i) for f, i in t['columns'].items()}) for t in layout['tables']])
        result['limitations'].insert(0, 'Claude found the inspection tables in this file (header keywords alone did not find them all); '
                                        'review the detected tables and columns.')
        if any(t['one_row_per_unit'] for t in layout['tables']):
            result['limitations'].insert(1, 'Scan-list tables count each row as one inspected part; rejected = rows whose condition is a reject value.')
    else:
        result['layout_source'] = 'keywords'
    finding = Checker(result)
    from hosting import sheet_roles
    result['dataset_selection'] = sheet_roles.select(sources, sheets, records, finding)

    for sheet in sheets:
        if sheet.get('role') == 'photos':
            continue
        if sheet['mapping_status'] != 'adaptive':
            finding('no_quantity_columns_detected', sheet['name'], message='No Inspected / Certified / Rejected style headers on this sheet, so it was not quantity-checked.')
        elif sheet['data_row_count'] == 0 and any(t['kind'] == 'detail' for t in sheet['tables']):
            finding('no_data_rows', sheet['name'], message='Quantity headers were found but no rows with quantities follow them.')

    sheet_names = {norm(s['name']): s['name'] for s in sheets if s['data_row_count'] or any(t['kind'] in ('detail', 'totals') for t in s['tables'])}
    for r in records:
        texts = [r['row_label']] + list(r['labels'].values())
        target = next((sheet_names[norm(t)] for t in texts if t and norm(t) in sheet_names and sheet_names[norm(t)] != r['sheet']), None)
        r['rollup_of'] = target
    detail = [r for r in records if r['table_kind'] == 'detail' and not r['is_total'] and not r['rollup_of']]
    by_table = defaultdict(list)
    for r in detail:
        by_table[(r['sheet'], r['table'])].append(r)

    # ---- per-row quantity and validity rules
    day_counts = Counter(r['day'] for r in detail if r['day'])
    expected_day = report_day or (day_counts.most_common(1)[0][0] if day_counts else None)
    for r in detail:
        v, s, c = r['values'], r['states'], r['columns']
        for f, n in v.items():
            if s[f] == 'number_as_text':
                finding('number_stored_as_text', r['sheet'], r['row'], c[f])
            elif s[f] == 'invalid':
                finding('invalid_numeric_value', r['sheet'], r['row'], c[f])
            if n is not None:
                if n < 0:
                    finding('negative_count_or_quantity', r['sheet'], r['row'], c[f])
                if n != n.to_integral_value():
                    finding('fractional_count', r['sheet'], r['row'], c[f])
        if any(st in KNOWN for st in s.values()):
            for f in v:
                if s[f] == 'blank':
                    finding('missing_cell', r['sheet'], r['row'], c[f])
        ins, ok, nok, rw = v.get('inspected'), v.get('certified'), v.get('rejected'), v.get('reworked')
        if ins is not None and ok is not None and nok is not None:
            if ins != ok + nok + (rw or 0):
                finding('inspected_count_mismatch', r['sheet'], r['row'],
                        message=f'Inspected {ins} ≠ certified {ok} + rejected {nok}' + (f' + reworked {rw}' if rw else '') + '.')
        if ins is not None and nok is not None and nok > ins:
            finding('rejected_exceeds_inspected', r['sheet'], r['row'], message=f'Rejected {nok} is more than inspected {ins}.')
        lab = r['labels']
        if 'part' in lab and not lab['part']:
            finding('missing_part_number', r['sheet'], r['row'])
        if 'identifier' in lab and not lab['identifier']:
            finding('missing_identifier', r['sheet'], r['row'])
        if nok is not None and nok > 0 and 'defect' in lab and not lab['defect']:
            finding('missing_defect_reason', r['sheet'], r['row'], message=f'{nok} rejected but no defect / reject reason.')
        cond = norm(lab.get('condition') or '')
        if cond and nok is not None:
            if cond in ('ok', 'pass', 'good', 'accepted') and nok > 0 and len(r.get('conditions') or []) < 2:
                finding('condition_conflicts_with_quantity', r['sheet'], r['row'], message=f'Condition says {lab["condition"]} but {nok} were rejected.')
            if cond in ('nok', 'ng', 'fail', 'reject', 'rejected', 'suspect') and nok == 0 and len(r.get('conditions') or []) < 2:
                finding('condition_conflicts_with_quantity', r['sheet'], r['row'], message=f'Condition says {lab["condition"]} but 0 were rejected.')
        if 'date' in lab:
            if lab['date'] and r['day'] is None:
                finding('invalid_date', r['sheet'], r['row'], message=f'"{lab["date"]}" is not a recognizable date.')
            elif r['day'] and expected_day and r['day'] != expected_day:
                finding('date_differs_from_report', r['sheet'], r['row'], message=f'{r["day"].isoformat()} differs from the report date {expected_day.isoformat()}.')
            if r['day'] and r['day'] > date.today():
                finding('future_date', r['sheet'], r['row'])
        sheet_shift = shift_number(r['sheet'])
        row_shift = shift_number(lab.get('shift'))
        if lab.get('shift') and row_shift is None:
            finding('invalid_shift', r['sheet'], r['row'], message=f'"{lab["shift"]}" is not a recognizable shift.')
        elif sheet_shift and row_shift and sheet_shift != row_shift:
            finding('shift_differs_from_sheet', r['sheet'], r['row'], message=f'Row says shift {row_shift} on the {r["sheet"]} sheet.')

    # ---- duplicates
    seen_exact, seen_id = {}, defaultdict(list)
    for r in detail:
        key = (r['sheet'], r['table'], tuple(sorted((k, v) for k, v in r['labels'].items() if v)), tuple(sorted((k, str(v)) for k, v in r['values'].items())), str(r.get('source')) if r.get('source') else None)
        if key in seen_exact and any(r['labels'].values()):
            first = seen_exact[key]
            finding('duplicate_row', r['sheet'], r['row'], message=f'Same as row {first["row"]} (every field identical).')
        else:
            seen_exact[key] = r
        ident = r['labels'].get('identifier')
        if ident:
            seen_id[ident.strip().upper()].append(r)
    for ident, rows in seen_id.items():
        items = [(x.get('source') or {}).get('item') for x in rows]
        if all(items) and len(set(items)) == len(items):
            continue   # separate SharePoint records that share a label (a supplier name, a lot): not duplicates
        if len(rows) > 1:
            places = ', '.join(f'{x["sheet"]} row {x["row"]}' for x in rows)
            where = 'across shifts' if len({x['sheet'] for x in rows}) > 1 else 'on the same sheet'
            for x in rows[1:]:
                finding('duplicate_identifier', x['sheet'], x['row'], message=f'Identifier {ident} appears {len(rows)} times {where}: {places}.')

    # ---- likely typos (within the report)
    ids = sorted(seen_id)
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            if near_miss(a, b, one_char=False):
                r = seen_id[b][0]
                finding('possible_identifier_typo', r['sheet'], r['row'], message=f'Identifier {b} looks like {a} with a look-alike character or prefix difference.')
    for kind in ('identifier', 'part'):
        codes = [(r, (r['labels'].get(kind) or '').strip()) for r in detail if r['labels'].get(kind)]
        shapes = Counter(shape(c) for _, c in codes)
        if len(codes) >= 4:
            common, n = shapes.most_common(1)[0]
            if n >= 0.75 * len(codes):
                for r, c in codes:
                    if shape(c) != common:
                        finding(f'{kind}_format_differs', r['sheet'], r['row'],
                                message=f'{"Identifier" if kind == "identifier" else "Part number"} {c} has a different format from the other {n} (expected like {common.replace("9", "#")}).')
    parts = Counter((r['labels'].get('part') or '').strip().upper() for r in detail if r['labels'].get('part'))
    # With a big parts catalog, part numbers one digit apart are usually different real parts (ST1946-115 vs ST1946-015).
    # Then only look-alike characters (O for 0, I/l for 1 ...) count as a likely typo.
    big_catalog = len(parts) > 25
    for rare, n in parts.items():
        if n > 2:
            continue
        for common, m in parts.items():
            if m >= 3 * n and m >= 3 and (rare.translate(CONFUSABLE) == common.translate(CONFUSABLE) if big_catalog else near_miss(rare, common)):
                for r in detail:
                    if (r['labels'].get('part') or '').strip().upper() == rare:
                        finding('possible_part_number_typo', r['sheet'], r['row'], message=f'Part {rare} appears {n}× but {common} appears {m}×; likely a typo.')
                break
    for kind in ('defect', 'condition', 'auditor'):
        words = Counter((r['labels'].get(kind) or '').strip() for r in detail if r['labels'].get(kind))
        for rare, n in words.items():
            if n > 1:
                continue
            for common, m in words.items():
                if m >= 3 and norm(common) != norm(rare) and len(norm(rare)) >= 4 and edit_distance_le1(norm(rare), norm(common)):
                    r = next(x for x in detail if (x['labels'].get(kind) or '').strip() == rare)
                    finding(f'possible_{kind}_typo', r['sheet'], r['row'], message=f'"{rare}" looks like a misspelling of "{common}" (used {m}×).')
                    break
        for value in words:
            if value != value.strip() or '  ' in value:
                finding('extra_spaces', 'Workbook', message=f'{kind.title()} "{value}" has extra spaces.')

    # ---- totals: stated total rows and totals blocks vs the rows they summarize
    sheet_sums = {}
    for sheet in sheets:
        for table in sheet['tables']:
            rows_ = by_table.get((sheet['name'], table['index']), [])
            sums = {f: _sum(rows_, f) for f in table['mapping']}
            table['sums'] = {f: (str(s) if known else None) for f, (s, known) in sums.items()}
            for tr in table['total_rows']:
                for f, n in tr['values'].items():
                    s, known = sums.get(f, (None, False))
                    if n is not None and known and rows_ and n != s:
                        finding('total_mismatch', sheet['name'], tr['row'], tr['columns'].get(f),
                                message=f'Stated {f} total {n} ≠ sum of the {len(rows_)} rows above ({s}).')
        detail_rows = [r for r in detail if r['sheet'] == sheet['name']]
        if detail_rows:
            sheet_sums[sheet['name']] = {f: _sum(detail_rows, f) for f in QTY}
        for table in sheet['tables']:
            if table['kind'] != 'totals':
                continue
            block_rows = [r for r in records if r['sheet'] == sheet['name'] and r['table'] == table['index']]
            if not block_rows or sheet['name'] not in sheet_sums:
                continue
            for f, n in block_rows[0]['values'].items():
                s, known = sheet_sums[sheet['name']].get(f, (None, False))
                if n is not None and s is not None and known and n != s:
                    finding('totals_block_mismatch', sheet['name'], block_rows[0]['row'], block_rows[0]['columns'].get(f),
                            message=f'"{table["columns"][f]["header"]}" says {n} but the {sheet["name"]} rows add up to {s}.')

    # ---- Summary rows labeled with another sheet's name must match that sheet's rows
    grand = {f: (sum((sheet_sums[n][f][0] for n in sheet_sums if sheet_sums[n][f][0] is not None), Decimal(0)),
                 all(sheet_sums[n][f][1] for n in sheet_sums)) for f in QTY}
    for r in records:
        if r['table_kind'] not in ('detail', 'summary'):
            continue
        target = r.get('rollup_of')
        if target and target in sheet_sums:
            for f, n in r['values'].items():
                s, known = sheet_sums[target].get(f, (None, False))
                if n is not None and known and n != s:
                    finding('summary_mismatch', r['sheet'], r['row'], r['columns'].get(f),
                            message=f'{target} {f} shows {n} on {r["sheet"]} but the {target} sheet adds up to {s}.')
        elif r['is_total'] and r['sheet'] not in sheet_sums and len(sheet_sums) >= 1:
            for f, n in r['values'].items():
                s, known = grand.get(f, (None, False))
                if n is not None and known and n != s:
                    finding('summary_mismatch', r['sheet'], r['row'], r['columns'].get(f),
                            message=f'Day total {f} shows {n} but all shift sheets add up to {s}.')

    detail_by_id = {(r['labels'].get('identifier') or '').strip().upper(): r for r in detail if r['labels'].get('identifier')}
    support = [r for r in records if r['table_kind'] == 'supporting' and not r['is_total']]
    if support:
        if any(r['labels'].get('identifier') for r in support):
            for r in support:
                ident = (r['labels'].get('identifier') or '').strip().upper()
                d = detail_by_id.get(ident)
                if ident and d is None:
                    finding('reject_log_row_not_in_detail', r['sheet'], r['row'], message=f'{ident} is on {r["sheet"]} but in no shift detail rows.')
                elif d is not None and r['values'].get('rejected') is not None and d['values'].get('rejected') is not None \
                        and r['values']['rejected'] != d['values']['rejected']:
                    finding('reject_log_mismatch', r['sheet'], r['row'],
                            message=f'{ident}: {r["sheet"]} shows {r["values"]["rejected"]} rejected but {d["sheet"]} row {d["row"]} shows {d["values"]["rejected"]}.')
        else:
            log_total, log_known = _sum(support, 'rejected')
            det_total, det_known = _sum(detail, 'rejected')
            if log_known and det_known and log_total != det_total and log_total is not None:
                finding('reject_log_total_mismatch', support[0]['sheet'],
                        message=f'{support[0]["sheet"]} lists {log_total} rejected in total but the shift rows add up to {det_total}.')

    # ---- common data-entry problems (researched list; see hosting/common_issues.py)
    from hosting.common_issues import run as common_issue_checks
    common_issue_checks(sources, cached, sheets, detail, finding, hidden)

    if stats['formula']:
        finding('quantity_formulas_not_evaluated', 'Workbook',
                message=f'{stats["formula"]} quantity cell(s) are formulas with no saved result, so their values are unknown.')
    if stats['formula_result']:
        finding('formula_results_used', 'Workbook',
                message=f'{stats["formula_result"]} quantity cell(s) are formulas; the result Excel last saved was checked. Recalculate in Excel if the file was edited elsewhere.')

    for sheet in sheets:
        for table in sheet['tables']:
            table.pop('mapping'); table.pop('label_map')
            table['total_rows'] = [tr['row'] for tr in table['total_rows']]
    numeric = stats['number'] + stats['number_as_text'] + stats['formula_result']
    info = {'no_quantity_columns_detected', 'no_data_rows', 'quantity_formulas_not_evaluated', 'formula_results_used', 'known_template_layout_differs', 'labeled_total_unit_differs'}
    issues = {c for c in finding.counts if c not in info}
    mapped = {f for s_ in sheets for t in s_['tables'] if t['kind'] == 'detail' for f in t['columns']}
    # a column with nothing written in it is "not recorded" too (a blank reject quantity is not zero rejects)
    mapped = {f for f in mapped if any(r['values'].get(f) is not None for r in detail)}
    # a quantity the report has no column for is unknown, never 0
    totals = {f: dict(total=str(grand[f][0]) if grand[f][1] and f in mapped else None, known_sum=str(grand[f][0]) if f in mapped else None)
              for f in ('inspected', 'certified', 'rejected')} if sheet_sums else {}
    # Pass/Fail reports (packing audits) have no reject quantity column: count the failed rows instead.
    has_qty = any(f in t['columns'] for s in sheets for t in s['tables'] if t['kind'] == 'detail' for f in ('rejected', 'certified'))
    results = [norm(r['labels'].get('condition') or '') for r in detail if r['labels'].get('condition')]
    if detail and not has_qty and len(results) >= 0.8 * len(detail):
        fails = sum(1 for v in results if v in ('fail', 'failed', 'nok', 'ng', 'reject', 'rejected', 'no good', 'not ok'))
        passes = sum(1 for v in results if v in ('pass', 'passed', 'ok', 'good', 'accept', 'accepted'))
        if fails + passes >= 0.8 * len(results):
            totals = dict(totals or {})
            totals['rejected'] = dict(total=str(fails), known_sum=str(fails), unit='rows')
            totals['certified'] = dict(total=str(passes), known_sum=str(passes), unit='rows')
    result.update(
        sheets=sheets, record_count=len(detail), numeric_cells_checked=numeric,
        formula_count=stats['formula'] + stats['formula_result'], formula_results_used=stats['formula_result'],
        records=[export_record(r) for r in detail[:MAX_RECORDS_RETURNED]], records_truncated=len(detail) > MAX_RECORDS_RETURNED,
        totals=totals, report_day=expected_day.isoformat() if expected_day else None,
        finding_counts=dict(finding.counts), finding_count=sum(finding.counts.values()),
        findings_truncated=sum(finding.counts.values()) > MAX_FINDINGS, row_count=stats['rows'], cell_count=stats['cells'],
        internal_check_status='unverified' if not numeric else ('findings' if issues else 'no_findings_in_checks_performed'))
    result['_detail_records'] = detail  # internal, removed before saving
    result['_export_records'] = [r for r in records if r['table_kind'] == 'source_copy' and not r['is_total']
                                 and next((s_['role'] for s_ in sheets if s_['name'] == r['sheet']), None) == 'source_export']
    return result


def _detail_count(records):
    return sum(1 for r in records if r['table_kind'] == 'detail' and not r['is_total']
               and any(st in KNOWN for st in r['states'].values()))


def export_record(r):
    part = r['labels'].get('part')
    return dict(sheet=r['sheet'], row=r['row'], part=part, identifier=r['labels'].get('identifier'), source=r.get('source'),
                day=r['day'].isoformat() if r['day'] else None, shift=r['labels'].get('shift'),
                values={f: (str(v) if v is not None else None) for f, v in r['values'].items()})
