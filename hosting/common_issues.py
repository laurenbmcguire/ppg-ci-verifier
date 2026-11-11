"""Common data-quality problems in hand-keyed inspection / sort reports.

Researched from data-entry and spreadsheet-cleaning guidance (transcription, transposition,
wrong-field, missing, duplicate, formatting/unit and misread-character errors; Excel error
values, hidden/garbled characters, numbers mangled into scientific notation, hidden rows and
sheets) plus PPG-specific rules (comment counts vs reject quantity). Each check only flags a
possible problem for a person to confirm; nothing is changed.
"""
from collections import Counter, defaultdict
from decimal import Decimal
from statistics import median
import io
import re
import zipfile

EXCEL_ERRORS = {'#REF!', '#N/A', '#VALUE!', '#DIV/0!', '#NAME?', '#NUM!', '#NULL!', '#SPILL!', '#CALC!', '#GETTING_DATA', '#FIELD!', '#BLOCKED!'}
HIDDEN_CHARS = {' ': 'non-breaking space', '​': 'zero-width space', '‌': 'zero-width character', '‍': 'zero-width character',
                '﻿': 'invisible byte-order mark', '�': 'unreadable character (�)', '⁠': 'invisible word joiner'}
MOJIBAKE = re.compile(r'Ã.|â€|Â[\s\xa0]')
PLACEHOLDERS = {'tbd', 'tba', 'tbc', 'xxx', 'xx', 'x', 'test', 'testing', 'dummy', 'sample', 'unknown', 'pending', 'temp',
                'asdf', 'abc', 'same', 'same as above', 'ditto', 'see above', 'see note', 'see notes', '?', '??', '???', '"', '〃', '..', '...'}
PLACEHOLDER_CODES = {'0', '00', '000', '0000', '123', '1234', '12345', '999', '9999', '99999', '111', '1111', '-'}
SCIENTIFIC = re.compile(r'^\d(?:\.\d+)?[eE]\+?\d+$')
COMMENT_LABELS = {'comment', 'comments', 'note', 'notes', 'remark', 'remarks', 'observation', 'observations'}
NOT_DEFECT_WORDS = {'am', 'pm', 'hr', 'hrs', 'hour', 'hours', 'min', 'mins', 'minute', 'minutes', 'mm', 'cm', 'in', 'inch', 'inches',
                    'of', 'out', 'st', 'nd', 'rd', 'th', 'shift', 'shifts', 'box', 'boxes', 'bin', 'bins', 'skid', 'skids', 'lot', 'lots',
                    'day', 'days', 'total', 'inspected', 'sorted', 'checked', 'good', 'ok', 'pass', 'passed', 'parts', 'pcs', 'pieces',
                    'per', 'rack', 'racks', 'tote', 'totes', 'layer', 'layers', 'row', 'rows', 'deg', 'degrees', 'percent', 'and', 'to'}


def hidden_layout(raw):
    """{sheet name: {'state': visible|hidden|veryHidden, 'rows': set(1-based), 'cols': set(0-based)}} for an XLSX."""
    from defusedxml import ElementTree as ET
    ns_main = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
    ns_rel = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'
    out = {}
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            rels = ET.fromstring(archive.read('xl/_rels/workbook.xml.rels'))
            targets = {r.get('Id'): r.get('Target', '') for r in rels}
            book = ET.fromstring(archive.read('xl/workbook.xml'))
            for sheet in book.iter(ns_main + 'sheet'):
                target = targets.get(sheet.get(ns_rel + 'id'), '')
                path = target.lstrip('/') if target.startswith('/') else 'xl/' + target
                info = {'state': sheet.get('state') or 'visible', 'rows': set(), 'cols': set()}
                try:
                    with archive.open(path) as handle:
                        for _, element in ET.iterparse(handle, events=('end',)):
                            tag = element.tag.rsplit('}', 1)[-1]
                            if tag == 'row':
                                if element.get('hidden') in ('1', 'true') and element.get('r', '').isdigit():
                                    info['rows'].add(int(element.get('r')))
                                element.clear()
                            elif tag == 'col' and element.get('hidden') in ('1', 'true'):
                                lo, hi = int(element.get('min', '1')), int(element.get('max', '1'))
                                info['cols'].update(range(lo - 1, min(hi, lo + 200)))
                except KeyError:
                    pass
                out[sheet.get('name')] = info
    except Exception:
        return {}
    return out


def _key(text):
    return re.sub(r'[^A-Z0-9]', '', str(text).upper())


def run(sources, cached, sheets, detail, finding, hidden=None):
    cached = cached or {}
    hidden = hidden or {}

    # 1. Excel error values and hidden / garbled characters, anywhere in the workbook.
    errors, odd = 0, 0
    for title, rows in sources:
        crows = cached.get(title) or []
        for r, row in enumerate(rows, 1):
            crow = crows[r - 1] if r - 1 < len(crows) else ()
            for c, value in enumerate(row):
                shown = value
                if isinstance(value, str) and value.startswith('=') and c < len(crow):
                    shown = crow[c]
                if isinstance(shown, str) and shown.strip().upper() in EXCEL_ERRORS and errors < 50:
                    errors += 1
                    finding('excel_error_value', title, r, _col(c),
                            message=f'Cell {_col(c)}{r} shows the Excel error {shown.strip()}' + (' from its formula' if shown is not value else '') + '; any total that uses it is wrong.')
                if isinstance(value, str) and not value.startswith('=') and odd < 40:
                    kinds = sorted({name for ch, name in HIDDEN_CHARS.items() if ch in value}
                                   | ({'control character'} if any(ord(ch) < 32 and ch not in '\t\n\r' for ch in value) else set())
                                   | ({'garbled text (encoding problem)'} if MOJIBAKE.search(value) else set()))
                    if kinds:
                        odd += 1
                        finding('hidden_characters', title, r, _col(c),
                                message=f'Cell {_col(c)}{r} ("{_clip(value)}") contains {", ".join(kinds)}. It can stop matching, look-ups and duplicate checks from working.')

    # 2. Hidden sheets, rows and columns that hold real data (unused template formulas don't count).
    def entered(title, r, c, value):
        if value in (None, ''):
            return False
        if isinstance(value, str) and value.startswith('='):
            crows = cached.get(title) or []
            saved = crows[r - 1][c] if r - 1 < len(crows) and c < len(crows[r - 1]) else None
            return saved not in (None, '', 0)
        return True
    for title, rows in sources:
        info = hidden.get(title)
        if not info:
            continue
        has_data = any(entered(title, r, c, v) for r, row in enumerate(rows, 1) for c, v in enumerate(row))
        if info['state'] != 'visible' and has_data:
            finding('hidden_sheet_with_data', title, message=f'Sheet "{title}" is hidden but contains data. Check it is meant to be part of (or left out of) the report.')
        data_rows = [r for r in sorted(info['rows']) if r - 1 < len(rows) and any(entered(title, r, c, v) for c, v in enumerate(rows[r - 1]))]
        if data_rows:
            listed = ', '.join(map(str, data_rows[:12])) + ('…' if len(data_rows) > 12 else '')
            finding('hidden_rows_with_data', title, data_rows[0], message=f'{len(data_rows)} hidden row(s) contain data (rows {listed}). Hidden rows are still counted in totals and can hide duplicates or errors.')
        data_cols = [c for c in sorted(info['cols']) if any(c < len(row) and entered(title, r, c, row[c]) for r, row in enumerate(rows, 1))]
        if data_cols:
            finding('hidden_columns_with_data', title, column=_col(data_cols[0]),
                    message=f'Hidden column(s) {", ".join(_col(c) for c in data_cols[:10])} contain data. Unhide and check them.')

    if not detail:
        return

    # 3. Same value written different ways (case, spaces, punctuation): 'Holes / Glue' vs 'holes/glue'.
    for kind, label in (('identifier', 'Identifier'), ('part', 'Part number'), ('defect', 'Defect reason'),
                        ('condition', 'Condition'), ('auditor', 'Auditor'), ('area', 'Area')):
        groups = defaultdict(Counter)
        where = defaultdict(list)
        for r in detail:
            value = (r['labels'].get(kind) or '').strip()
            if value and _key(value):
                groups[_key(value)][value] += 1
                where[value].append(r)
        for key, spellings in groups.items():
            if len(spellings) < 2:
                continue
            common, n = spellings.most_common(1)[0]
            for variant, m in spellings.items():
                if variant == common:
                    continue
                # Only the capital letters (or doubled spaces) differ: not an error. The clean-up makes them match by itself.
                if ' '.join(variant.split()).casefold() == ' '.join(common.split()).casefold():
                    continue
                first = where[variant][0]
                if kind == 'identifier':
                    finding('duplicate_identifier_variant', first['sheet'], first['row'],
                            message=f'Identifier "{variant}" is the same scan as "{common}" written differently (spaces/dashes/case). It is probably a duplicate.')
                else:
                    finding('inconsistent_spelling', first['sheet'], first['row'],
                            message=f'{label} "{variant}" ({m}×) is written differently from "{common}" ({n}×). Use one spelling so totals by {label.lower()} group correctly.')

    # 4. Placeholders and values that Excel mangled.
    for r in detail:
        for kind, label in (('identifier', 'Identifier'), ('part', 'Part number'), ('auditor', 'Auditor'), ('defect', 'Defect reason'), ('area', 'Area')):
            value = (r['labels'].get(kind) or '').strip()
            if not value:
                continue
            low = value.lower()
            if low in PLACEHOLDERS or (kind in ('identifier', 'part') and value in PLACEHOLDER_CODES):
                finding('placeholder_value', r['sheet'], r['row'], r['label_columns'].get(kind),
                        message=f'{label} is "{value}", which looks like a placeholder or copy-down note, not real data.')
            elif kind in ('identifier', 'part') and SCIENTIFIC.match(value.replace(',', '')):
                finding('scientific_notation_code', r['sheet'], r['row'], r['label_columns'].get(kind),
                        message=f'{label} "{value}" is in scientific notation. Excel has rounded it, so digits are lost. Re-enter it from the scan as text.')
        for f, n in r['values'].items():
            if n is not None and n in (Decimal(999), Decimal(9999), Decimal(99999)):
                finding('placeholder_value', r['sheet'], r['row'], r['columns'].get(f),
                        message=f'{f.title()} is {n}, a number often used as a placeholder.')

    # 5. Value typed into the wrong column.
    parts = {_key(r['labels']['part']) for r in detail if r['labels'].get('part')}
    idents = {_key(r['labels']['identifier']) for r in detail if r['labels'].get('identifier')}
    for r in detail:
        ident, part = r['labels'].get('identifier'), r['labels'].get('part')
        if ident and _key(ident) in parts and _key(ident) != _key(part or ''):
            finding('value_in_wrong_column', r['sheet'], r['row'], r['label_columns'].get('identifier'),
                    message=f'Identifier "{ident}" is a part number used elsewhere in this report; it may be in the wrong column.')
        if part and _key(part) in idents and _key(part) != _key(ident or ''):
            finding('value_in_wrong_column', r['sheet'], r['row'], r['label_columns'].get('part'),
                    message=f'Part number "{part}" matches an identifier used elsewhere in this report; it may be in the wrong column.')
    quantities = [n for r in detail for n in r['values'].values() if n is not None and n >= 0]
    typical = median(quantities) if quantities else None
    for r in detail:
        for f, n in r['values'].items():
            if n is None or typical is None:
                continue
            if n >= 1_000_000 and typical < 10_000:
                finding('value_in_wrong_column', r['sheet'], r['row'], r['columns'].get(f),
                        message=f'{f.title()} is {n}, which looks like an identifier or part number typed into a quantity column.')
            elif 40_000 <= n <= 60_000 and typical < 1_000 and n == n.to_integral_value():
                finding('value_in_wrong_column', r['sheet'], r['row'], r['columns'].get(f),
                        message=f'{f.title()} is {n}, which looks like an Excel date (serial number) in a quantity column.')

    # 6. Quantity outliers: a dropped or extra zero, or a figure far from the rest.
    for f in ('inspected', 'certified', 'rejected'):
        values = [(r, r['values'].get(f)) for r in detail if r['values'].get(f) is not None and r['values'].get(f) > 0]
        if len(values) < 6:
            continue
        mid = median(v for _, v in values)
        if mid < 5:
            continue
        for r, v in values:
            ratio = v / mid
            if ratio >= 5 and v - mid >= 20:
                hint = ' (possible extra zero)' if Decimal('0.5') <= (v / 10) / mid <= Decimal('1.5') else ''
                finding('quantity_outlier', r['sheet'], r['row'], r['columns'].get(f),
                        message=f'{f.title()} {v} is far above the usual {mid}{hint}. Confirm it was keyed correctly.')
            elif f == 'inspected' and ratio <= Decimal('0.2') and mid - v >= 10:
                hint = ' (possible missing digit)' if Decimal('0.5') <= (v * 10) / mid <= Decimal('1.5') else ''
                finding('quantity_outlier', r['sheet'], r['row'], r['columns'].get(f),
                        message=f'{f.title()} {v} is far below the usual {mid}{hint}. Confirm it was keyed correctly.')
    for r in detail:
        ins = r['values'].get('inspected')
        if ins is not None and ins == 0 and (r['labels'].get('identifier') or r['labels'].get('part')):
            finding('zero_inspected', r['sheet'], r['row'], r['columns'].get('inspected'),
                    message='0 inspected on a row with a part/identifier. Was the count left out?')

    # 7. Blank in a column that is filled on almost every other row.
    for kind, label in (('date', 'Date'), ('shift', 'Shift'), ('auditor', 'Auditor'), ('area', 'Area'), ('condition', 'Condition')):
        rows = [r for r in detail if kind in r['labels']]
        filled = [r for r in rows if r['labels'].get(kind)]
        if len(rows) >= 4 and len(filled) >= 0.75 * len(rows):
            for r in rows:
                if not r['labels'].get(kind):
                    finding('missing_value', r['sheet'], r['row'], r['label_columns'].get(kind),
                            message=f'{label} is blank here but filled in on {len(filled)} of {len(rows)} rows.')

    # 8. Dates typed as text in a column of real dates (they will not sort or filter as dates).
    dated = [r for r in detail if r.get('date_is_text') is not None]
    text_dates = [r for r in dated if r['date_is_text']]
    if len(dated) >= 4 and 0 < len(text_dates) <= 0.25 * len(dated):
        for r in text_dates:
            finding('date_stored_as_text', r['sheet'], r['row'], r['label_columns'].get('date'),
                    message=f'Date "{r["labels"].get("date")}" is typed as text while the other dates are real dates.')

    # 10. Several condition columns (LF/RF/LR/RR): the number marked SUSPECT/NOK should equal the suspect/reject quantity.
    bad_words = {'suspect', 'nok', 'ng', 'fail', 'failed', 'reject', 'rejected', 'bad', 'defect', 'loose', 'missing'}
    for r in detail:
        conds, nok = r.get('conditions') or [], r['values'].get('rejected')
        if len(conds) < 2 or nok is None or not any(conds):
            continue
        flagged = [h for h, v in zip(r.get('condition_headers') or [], conds) if v and v.strip().lower() in bad_words]
        if len(flagged) != nok:
            finding('condition_count_mismatch', r['sheet'], r['row'], r['columns'].get('rejected'),
                    message=f'{len(flagged)} condition column(s) marked suspect/NOK' + (f' ({", ".join(flagged)})' if flagged else '') + f', but the suspect/reject quantity is {nok}.')
        unknown = sorted({v for v in conds if v and v.strip().lower() not in bad_words | {'ok', 'good', 'pass', 'passed', 'n/a', 'na', '-'}})
        if unknown:
            finding('unexpected_condition_value', r['sheet'], r['row'],
                    message=f'Condition value(s) {", ".join(repr(u) for u in unknown)} are not OK or SUSPECT/NOK. Check for a typo.')

    # 11. "Total … Inspected / Suspect / OK" label cells with a number beside them must match the rows.
    sheet_rows = defaultdict(list)
    for r in detail:
        sheet_rows[r['sheet']].append(r)
    detail_cells = {(r['sheet'], r['row']) for r in detail}
    for title, rows in sources:
        mine = sheet_rows.get(title)
        if not mine:
            continue
        crows = cached.get(title) or []
        for rn, row in enumerate(rows, 1):
            if (title, rn) in detail_cells:
                continue
            for c, value in enumerate(row):
                if not isinstance(value, str) or value.startswith('='):
                    continue
                words = re.sub(r'[^a-z ]+', ' ', value.lower()).split()
                if 'total' not in words or any(w in words for w in ('rate', 'percent', 'hours', 'hour', 'cost', 'labor', 'productivity', 'per')) and 'inspected' not in words:
                    continue
                field = ('inspected' if 'inspected' in words or 'sorted' in words else
                         'rejected' if {'suspect', 'rejected', 'reject', 'rejects', 'nok', 'ng', 'defects', 'defective'} & set(words) else
                         'certified' if {'ok', 'good', 'certified', 'accepted', 'passed'} & set(words) else None)
                if not field or not any(field in x['values'] for x in mine):
                    continue
                stated = None
                for k in range(c + 1, min(len(row), c + 12)):
                    v = row[k]
                    if isinstance(v, str) and v.startswith('='):
                        v = crows[rn - 1][k] if rn - 1 < len(crows) and k < len(crows[rn - 1]) else None
                    if isinstance(v, (int, float)) and not isinstance(v, bool):
                        stated = Decimal(str(v)); break
                    if isinstance(v, str) and v.strip():
                        break
                vals = [x['values'].get(field) for x in mine]
                if stated is None or any(v is None for v in vals):
                    continue
                total = sum(vals, Decimal(0))
                if stated != total:
                    per = re.search(r'\((\d+)\s*per\s*(\w+)\)', value)
                    if per and stated == int(per.group(1)) * len(mine):
                        finding('labeled_total_unit_differs', title, rn, _col(c),
                                message=f'"{value.strip()}" counts {per.group(1)} per {per.group(2)} ({stated}), but the rows count {total} in total '
                                        f'(about {total / len(mine):g} per row). Both may be intended (e.g. weights vs positions), but confirm which one is billed.')
                    else:
                        finding('labeled_total_mismatch', title, rn, _col(c),
                                message=f'"{value.strip()}" shows {stated}, but the {len(mine)} rows add up to {total}.')

    # 9. Counts written in the comment ("7 holes 1 glue") should add up to the reject quantity.
    for r in detail:
        note, nok = r['labels'].get('comment'), r['values'].get('rejected')
        if not note or nok is None:
            continue
        counts = [int(n) for n, word in re.findall(r'(?<![\w#./-])(\d{1,5})\s*(?:x\s+|pcs?\s+|pieces?\s+)?([A-Za-z]{3,})', note)
                  if word.lower() not in NOT_DEFECT_WORDS]
        if counts and sum(counts) != nok:
            finding('comment_count_mismatch', r['sheet'], r['row'], r['columns'].get('rejected'),
                    message=f'Comment "{_clip(note)}" adds up to {sum(counts)}, but Rejected is {nok}.')

    # 12. Identifier format: VIN letters and check digit, lengths that differ from the rest, Excel fill-down series, lost leading zeros.
    idrows = [(r, _key(r['labels'].get('identifier') or '')) for r in detail if _key(r['labels'].get('identifier') or '')]
    lengths = Counter(len(k) for _, k in idrows)
    usual, n_usual = lengths.most_common(1)[0] if lengths else (0, 0)
    vins = [(r, k) for r, k in idrows if len(k) == 17]
    vin_report = len(vins) >= 3 and len(vins) >= 0.6 * len(idrows)
    if vin_report:
        passing = sum(1 for _, k in vins if vin_check_digit(k) == k[8])
        check_digits_used = passing >= max(3, 0.8 * len(vins))   # North American VINs carry a check digit; others may not
        flagged = 0
        for r, k in vins:
            if flagged >= 40:
                break
            bad = sorted(set(ch for ch in k if ch in 'IOQ'))
            if bad:
                flagged += 1
                finding('vin_has_i_o_q', r['sheet'], r['row'], r['label_columns'].get('identifier'),
                        message=f'VIN "{r["labels"]["identifier"]}" contains {", ".join(bad)}. Real VINs never use I, O or Q, so it is probably a misread 1 or 0.')
            elif check_digits_used and vin_check_digit(k) != k[8]:
                flagged += 1
                finding('vin_check_digit_wrong', r['sheet'], r['row'], r['label_columns'].get('identifier'),
                        message=f'VIN "{r["labels"]["identifier"]}" fails the VIN check digit (9th character should be {vin_check_digit(k)}), so a character was probably typed or scanned wrong.')
    if n_usual >= 5 and usual >= 8 and n_usual >= 0.8 * len(idrows):
        odd = [(r, k) for r, k in idrows if len(k) != usual][:20]
        for r, k in odd:
            finding('identifier_unusual_length', r['sheet'], r['row'], r['label_columns'].get('identifier'),
                    message=f'Identifier "{r["labels"]["identifier"]}" has {len(k)} characters; {n_usual} of the {len(idrows)} identifiers have {usual}. A character may be missing or extra.')
    tail = re.compile(r'^(.*?)(\d+)$')
    steps = []
    for (a, ka), (b, kb) in zip(idrows, idrows[1:]):
        ma, mb = tail.match(ka), tail.match(kb)
        steps.append(bool(a['sheet'] == b['sheet'] and b['row'] == a['row'] + 1 and ma and mb and ma.group(1) == mb.group(1)
                          and len(ka) >= 10 and int(mb.group(2)) == int(ma.group(2)) + 1))
    if steps and sum(steps) < 0.5 * len(steps):   # identifiers that are normally sequential (scan numbers) are fine
        run_start = None
        for i, step in enumerate(steps + [False]):
            if step and run_start is None:
                run_start = i
            elif not step and run_start is not None:
                if i - run_start >= 3:
                    r0, k0 = idrows[run_start]
                    finding('fill_series_identifiers', r0['sheet'], r0['row'], r0['label_columns'].get('identifier'),
                            message=f'{i - run_start + 1} identifiers in a row count up by one from "{r0["labels"]["identifier"]}". That is what Excel does when a cell is dragged down; check they were really scanned.')
                run_start = None
    by_stripped = defaultdict(set)
    for r in detail:
        part = (r['labels'].get('part') or '').strip()
        if part:
            by_stripped[_key(part).lstrip('0')].add(_key(part))
    for stripped, forms in by_stripped.items():
        if stripped and len(forms) > 1:
            longest, shortest = max(forms, key=len), min(forms, key=len)
            if longest != shortest:
                row = next(r for r in detail if _key(r['labels'].get('part') or '') == shortest)
                finding('leading_zeros_lost', row['sheet'], row['row'], row['label_columns'].get('part'),
                        message=f'Part number "{shortest}" is "{longest}" without its leading zero(s). Excel drops leading zeros from numbers; enter part numbers as text.')


    _copied_count_blocks(detail, finding)
    _blank_totals(sources, sheet_rows, detail_cells, finding)
    _defect_without_reject(detail, finding)


NO_DEFECT_WORDS = {'', 'none', 'n a', 'na', 'no', 'nil', 'nothing', 'no defect', 'no defects', 'no reject', 'no rejects', 'no issue',
                   'no issues', 'not applicable', 'ok', 'good', 'pass', 'passed', '0'}


def _copied_count_blocks(detail, finding):
    """13. Six or more rows in a row with exactly the same counts, inside a report whose other rows differ.
    That is what copying a row down looks like. A report where every row (or most rows) has the same count
    is a fixed lot size, and zero rejects on every row proves nothing, so neither is flagged."""
    tables = defaultdict(list)
    for r in detail:
        tables[(r['sheet'], r['table'])].append(r)
    for rows in tables.values():
        sig = [tuple(r['values'].get(f) for f in ('inspected', 'certified', 'rejected', 'reworked')) for r in rows]
        if len(rows) < 8 or len(set(sig)) < 4:
            continue
        i = 0
        while i < len(sig):
            j = i
            while j + 1 < len(sig) and sig[j + 1] == sig[i]:
                j += 1
            n, (ins, _, nok, _) = j - i + 1, sig[i]
            if n >= 6 and n <= 0.6 * len(rows) and ins is not None and nok is not None and nok >= 2:
                first, last = rows[i], rows[j]
                finding('duplicate_count_block', first['sheet'], first['row'], first['columns'].get('rejected'),
                        message=f'Rows {first["row"]} to {last["row"]} ({n} in a row) all show Inspected {ins} and Rejected {nok}. '
                                f'Check they were counted and not copied down.')
            i = j + 1


def _total_label_field(value):
    """Which quantity a 'Total ...' label cell is about (inspected / rejected / certified), or None."""
    if not isinstance(value, str) or value.startswith('='):
        return None
    words = re.sub(r'[^a-z ]+', ' ', value.lower()).split()
    if 'total' not in words or any(w in words for w in ('rate', 'percent', 'hours', 'hour', 'cost', 'labor', 'productivity', 'per')) and 'inspected' not in words:
        return None
    return ('inspected' if 'inspected' in words or 'sorted' in words else
            'rejected' if {'suspect', 'rejected', 'reject', 'rejects', 'nok', 'ng', 'defects', 'defective'} & set(words) else
            'certified' if {'ok', 'good', 'certified', 'accepted', 'passed'} & set(words) else None)


def _filled(value):
    """A number, a formula, or text that is a number."""
    if value is None or isinstance(value, bool):
        return False
    if isinstance(value, str):
        return value.strip().startswith('=') or bool(re.fullmatch(r'\s*-?[\d,]+(?:\.\d+)?\s*', value))
    return True


def _blank_totals(sources, sheet_rows, detail_cells, finding):
    """14. A 'Total ...' label on a sheet with inspection rows, but nothing beside it or in the two rows under it."""
    for title, rows in sources:
        mine = sheet_rows.get(title)
        if not mine:
            continue
        for rn, row in enumerate(rows, 1):
            if (title, rn) in detail_cells:
                continue
            for c, value in enumerate(row):
                field = _total_label_field(value)
                known = [x['values'][field] for x in mine if x['values'].get(field) is not None] if field else []
                if not known:
                    continue
                beside = False
                for k in range(c + 1, min(len(row), c + 12)):
                    if _filled(row[k]):
                        beside = True
                        break
                    if isinstance(row[k], str) and row[k].strip():
                        break   # the next label starts here
                below = any(rn + d < len(rows) and c < len(rows[rn + d]) and _filled(rows[rn + d][c]) for d in (0, 1))
                if not beside and not below:
                    finding('total_row_blank', title, rn, _col(c),
                            message=f'"{value.strip()}" in {_col(c)}{rn} has no number beside or under it, but the {len(known)} rows add up to {sum(known, Decimal(0))}.')


def _defect_without_reject(detail, finding):
    """15. A defect reason written on a row whose reject quantity is 0."""
    zero = [r for r in detail if 'defect' in r['labels'] and r['values'].get('rejected') == 0]
    given = [r for r in zero if (r['labels'].get('defect') or '').strip()
             and re.sub(r'[^a-z0-9]+', ' ', r['labels']['defect'].lower()).strip() not in NO_DEFECT_WORDS
             and r['labels']['defect'].strip().lower() not in PLACEHOLDERS]
    if len(zero) >= 4 and len(given) >= 0.9 * len(zero):
        return   # the column is filled on every row whatever the quantity, so it is not a reject reason
    for r in given[:40]:
        finding('defect_reject_mismatch', r['sheet'], r['row'], r['label_columns'].get('defect'),
                message=f'Defect reason "{_clip(r["labels"]["defect"])}" is given but Rejected is 0. Was a reject left out?')


def _col(index):
    letters, n = '', index + 1
    while n:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _clip(text, n=60):
    text = re.sub(r'[\x00-\x1f]', '·', str(text))
    return text if len(text) <= n else text[:n - 1] + '…'


VIN_VALUES = dict(zip('ABCDEFGHJKLMNPRSTUVWXYZ', [1, 2, 3, 4, 5, 6, 7, 8, 1, 2, 3, 4, 5, 7, 9, 2, 3, 4, 5, 6, 7, 8, 9]))
VIN_WEIGHTS = [8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2]


def vin_check_digit(vin):
    """The 9th character a 17-character North American VIN should have ('X' for 10); '?' if it has letters VINs never use."""
    total = 0
    for ch, w in zip(vin, VIN_WEIGHTS):
        v = int(ch) if ch.isdigit() else VIN_VALUES.get(ch)
        if v is None:
            return '?'
        total += v * w
    d = total % 11
    return 'X' if d == 10 else str(d)
