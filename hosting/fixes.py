"""Proposed cell fixes, and a corrected copy of the workbook with only the approved ones applied.

Proposals come from the report's own checks (look-alike characters, stray/hidden characters,
inconsistent spellings, numbers typed as text, Certified ≠ Inspected − Rejected), from the SharePoint
comparison, and from AI suggestions that name a cell. Nothing changes until a person approves a fix;
the original file is never modified. Formula cells are never overwritten (fix their inputs instead).
"""
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
import csv
import hashlib
import io
import re
import zipfile

from hosting.adaptive_reports import scan, norm

LOOKALIKE = str.maketrans({'O': '0', 'o': '0', 'Q': '0', 'D': '0', 'I': '1', 'l': '1', 'i': '1', '|': '1', 'S': '5', 's': '5', 'B': '8', 'Z': '2', 'z': '2', 'G': '6'})
HIDDEN = re.compile('[ ​‌‍⁠﻿\x00-\x08\x0b\x0c\x0e-\x1f]')
MAX_PROPOSALS = 400
TEXT_LABELS = ('identifier', 'part', 'defect', 'condition', 'auditor', 'area', 'comment', 'shift')


class FixError(ValueError):
    pass


def col_letter(index0):
    letters, n = '', index0 + 1
    while n:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def col_index(letters):
    n = 0
    for ch in letters.upper():
        n = n * 26 + ord(ch) - 64
    return n - 1


def _addr(cell):
    m = re.fullmatch(r'([A-Z]{1,3})([1-9][0-9]{0,6})', (cell or '').upper().strip())
    if not m:
        raise FixError(f'"{cell}" is not a cell address like G17.')
    return m.group(1), int(m.group(2))


def _sources(filename, raw):
    if filename.lower().endswith('.xlsx'):
        from hosting.spreadsheets import _xlsx_rows, _cached_rows
        return [(t, list(r)) for t, r in _xlsx_rows(raw, [])], _cached_rows(raw) or {}
    return [('CSV', list(csv.reader(io.StringIO(raw.decode('utf-8-sig'), newline=''))))], {}


def _cell_value(sources, sheet, cell):
    letters, row = _addr(cell)
    rows = dict(sources).get(sheet)
    if rows is None:
        raise FixError(f'Sheet "{sheet}" is not in this workbook.')
    c = col_index(letters)
    if row - 1 >= len(rows) or c >= len(rows[row - 1]):
        return None
    return rows[row - 1][c]


def _show(v):
    if v is None:
        return ''
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _pid(sheet, cell, after):
    return hashlib.sha256(f'{sheet}|{cell}|{after}'.encode()).hexdigest()[:16]


def _num(text):
    try:
        n = Decimal(str(text).replace(',', '').strip())
        return n if n.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def proposals(result, filename, raw, sharepoint_rows=None):
    """List of proposed fixes: {id, sheet, cell, before, after, kind, reason, source, confidence}."""
    sources, cached = _sources(filename, raw)
    sheets, records, _ = scan(sources, cached)
    detail = [r for r in records if r['table_kind'] == 'detail' and not r['is_total']]
    out, seen = [], set()

    def add(sheet, cell, after, reason, source, confidence='high'):
        if not sheet or not cell or len(out) >= MAX_PROPOSALS:
            return
        try:
            before = _cell_value(sources, sheet, cell)
        except FixError:
            return
        if isinstance(before, str) and before.startswith('='):
            return  # never overwrite a formula
        kind = 'number' if isinstance(after, (int, float, Decimal)) and not isinstance(after, bool) else 'text'
        if (_show(before) == _show(after) and isinstance(before, str) == (kind == 'text')) or (sheet, cell) in seen:
            return
        seen.add((sheet, cell))
        after_text = (str(Decimal(after).normalize()) if Decimal(after) != Decimal(after).to_integral_value() else str(int(Decimal(after)))) if kind == 'number' else str(after)
        out.append(dict(id=_pid(sheet, cell, after_text), sheet=sheet, cell=cell, before=_show(before), after=after_text,
                        kind=kind, reason=reason, source=source, confidence=confidence))

    def text_cell(r, kind):
        letter = r['label_columns'].get(kind)
        return f'{letter}{r["row"]}' if letter else None

    def qty_cell(r, field):
        col = r['columns'].get(field)
        return f'{col_letter(col - 1)}{r["row"]}' if col else None

    # 1. SharePoint differences: the export is the record of what was scanned.
    sp = result.get('sharepoint') or {}
    for f in sp.get('findings') or []:
        if f.get('code') in ('differs_from_sharepoint', 'identifier_typo_vs_sharepoint') and f.get('cell') and f.get('sharepoint_value') not in (None, ''):
            value = f['sharepoint_value']
            if f.get('field') in ('inspected', 'rejected', 'certified'):
                value = _num(value)
                if value is None:
                    continue
            if f.get('field') in ('day', 'shift'):
                continue
            add(f.get('sheet'), f['cell'], value, f'SharePoint shows {f["sharepoint_value"]} for this scan ({f.get("message", "")})', 'SharePoint')

    # 2. Text clean-up: hidden characters, stray / double spaces.
    for r in detail:
        for kind in TEXT_LABELS:
            cell = text_cell(r, kind)
            raw_value = _cell_value(sources, r['sheet'], cell) if cell else None
            if not isinstance(raw_value, str) or raw_value.startswith('='):
                continue
            cleaned = re.sub(r'[ \t]{2,}', ' ', HIDDEN.sub(lambda m: ' ' if m.group(0) == ' ' else '', raw_value)).strip()
            if cleaned and cleaned != raw_value:
                add(r['sheet'], cell, cleaned, 'Removes hidden characters or extra spaces so matching and totals work.', 'Clean-up')

    # 3. Look-alike characters in identifiers / part numbers that are otherwise all digits.
    for kind in ('identifier', 'part'):
        values = [r['labels'].get(kind) for r in detail if r['labels'].get(kind)]
        digit_share = sum(1 for v in values if v.strip().isdigit()) / len(values) if values else 0
        if digit_share < 0.7:
            continue
        for r in detail:
            v = (r['labels'].get(kind) or '').strip()
            if v and not v.isdigit() and len(re.sub(r'\d', '', v)) <= 2:
                fixed = v.translate(LOOKALIKE)
                if fixed.isdigit():
                    add(r['sheet'], text_cell(r, kind), fixed, f'"{v}" has letter(s) that look like digits; the other values in this column are all digits.', 'Typo')

    # 4. Same value spelled different ways → the most common spelling.
    for kind in ('defect', 'condition', 'auditor', 'area'):
        groups = defaultdict(Counter)
        for r in detail:
            v = (r['labels'].get(kind) or '').strip()
            if v:
                groups[re.sub(r'[^A-Z0-9]', '', v.upper())][v] += 1
        for r in detail:
            v = (r['labels'].get(kind) or '').strip()
            if not v:
                continue
            spellings = groups[re.sub(r'[^A-Z0-9]', '', v.upper())]
            common, n = spellings.most_common(1)[0]
            if v != common and n > spellings[v]:
                add(r['sheet'], text_cell(r, kind), common, f'Uses the spelling "{common}" found on {n} other row(s).', 'Spelling')

    # 4b. Part numbers that differ only in upper/lower case → the most common form (uppercase on a tie).
    groups = defaultdict(Counter)
    for r in detail:
        v = (r['labels'].get('part') or '').strip()
        if v:
            groups[v.upper()][v] += 1
    for r in detail:
        v = (r['labels'].get('part') or '').strip()
        spellings = groups.get(v.upper()) if v else None
        if not spellings or len(spellings) < 2:
            continue
        best = max(spellings.items(), key=lambda kv: (kv[1], kv[0] == kv[0].upper()))[0]
        if v != best:
            add(r['sheet'], text_cell(r, 'part'), best, f'Part number written as "{best}" on {spellings[best]} other row(s); same part, different capitals.', 'Spelling')

    # 5. Numbers typed as text in quantity columns.
    for r in detail:
        for field in ('inspected', 'certified', 'rejected'):
            cell = qty_cell(r, field)
            v = _cell_value(sources, r['sheet'], cell) if cell else None
            if isinstance(v, str) and not v.startswith('=') and _num(v) is not None and v.strip():
                add(r['sheet'], cell, _num(v), f'"{v}" is typed as text; stored as a number it will add into the totals.', 'Number as text')

    # 6. Certified ≠ Inspected − Rejected (assumes Inspected and Rejected are right).
    for r in detail:
        ins, ok, nok = (r['values'].get(k) for k in ('inspected', 'certified', 'rejected'))
        if None not in (ins, ok, nok) and ins - nok != ok and ins - nok >= 0:
            add(r['sheet'], qty_cell(r, 'certified'), ins - nok,
                f'Certified {ok} does not equal Inspected {ins} − Rejected {nok}. This assumes Inspected and Rejected are right.', 'Math', 'medium')

    # 7. AI suggestions that name a cell and a value.
    ai = result.get('ai_review') or {}
    for issue in ai.get('issues') or [] if ai.get('status') == 'done' else []:
        if issue.get('cell') and issue.get('suggested_value') not in (None, '') and issue.get('sheet'):
            try:
                _addr(issue['cell'])
            except FixError:
                continue
            value = issue['suggested_value']
            add(issue['sheet'], issue['cell'].upper(), _num(value) if _num(value) is not None else value,
                f'AI suggestion: {issue.get("issue", "")}', 'AI suggestion', 'medium')
    return out


# ---------------------------------------------------------------- applying approved fixes

def _xml_text(value):
    return value.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def _new_cell(attrs, address, value, kind):
    style = re.search(r'\bs="(\d+)"', attrs)
    s = f' s="{style.group(1)}"' if style else ''
    if kind == 'number':
        return f'<c r="{address}"{s}><v>{value}</v></c>'
    return f'<c r="{address}"{s} t="inlineStr"><is><t xml:space="preserve">{_xml_text(value)}</t></is></c>'


def _insert_cell(xml, address, value, kind):
    """Add a cell to an existing (or new) row, keeping cells in column order."""
    letters, row = _addr(address)
    cell = _new_cell('', address, value, kind)
    xml = re.sub(rf'(<row\b[^>]*\br="{row}"[^>]*?)\s+spans="[^"]*"', r'\1', xml, count=1)  # size hint no longer exact
    m = re.search(rf'<row\b[^>]*\br="{row}"[^>]*?(/>|>(.*?)</row>)', xml, re.S)
    if m:
        if m.group(1) == '/>':
            open_tag = m.group(0)[:-2] + '>'
            return xml[:m.start()] + open_tag + cell + '</row>' + xml[m.end():]
        body, body_start = m.group(2), m.start(2)
        for cm in re.finditer(r'<c\b[^>]*\br="([A-Z]+)\d+"', body):
            if col_index(cm.group(1)) > col_index(letters):
                pos = body_start + cm.start()
                return xml[:pos] + cell + xml[pos:]
        pos = m.end(2)
        return xml[:pos] + cell + xml[pos:]
    rows = list(re.finditer(r'<row\b[^>]*\br="(\d+)"', xml))
    later = next((r for r in rows if int(r.group(1)) > row), None)
    new_row = f'<row r="{row}">{cell}</row>'
    if later:
        return xml[:later.start()] + new_row + xml[later.start():]
    if '<sheetData/>' in xml:
        return xml.replace('<sheetData/>', f'<sheetData>{new_row}</sheetData>')
    return xml.replace('</sheetData>', new_row + '</sheetData>')


def apply_fixes(filename, raw, approved):
    """Bytes of a corrected copy with exactly the approved changes; raises FixError if any can't be applied."""
    if not approved:
        raise FixError('Approve at least one change first.')
    sources, _ = _sources(filename, raw)
    edits = []
    for change in approved:
        sheet, cell = change.get('sheet'), (change.get('cell') or '').upper()
        _addr(cell)
        current = _cell_value(sources, sheet, cell)
        if isinstance(current, str) and current.startswith('='):
            raise FixError(f'{sheet}!{cell} is a formula; change the cells it uses instead.')
        if 'before' in change and _show(current) != str(change['before']):
            raise FixError(f'{sheet}!{cell} now holds "{_show(current)}", not "{change["before"]}". Re-open the report and review again.')
        after = str(change.get('after', ''))
        kind = change.get('kind') if change.get('kind') in ('number', 'text') else ('number' if _num(after) is not None and isinstance(current, (int, float)) and not isinstance(current, bool) else 'text')
        if kind == 'number':
            n = _num(after)
            if n is None:
                raise FixError(f'"{after}" is not a number (for {sheet}!{cell}).')
            after = str(n.normalize()) if n != n.to_integral_value() else str(int(n))
        if len(after) > 32767:
            raise FixError('A cell value is too long.')
        edits.append((sheet, cell, after, kind))
    if len({(s, c) for s, c, _, _ in edits}) != len(edits):
        raise FixError('The same cell was changed twice; keep one change per cell.')
    if not filename.lower().endswith('.xlsx'):
        return _apply_csv(raw, edits)
    return _apply_xlsx(raw, edits)


def _apply_csv(raw, edits):
    text = raw.decode('utf-8-sig')
    newline = '\r\n' if '\r\n' in text else '\n'
    rows = list(csv.reader(io.StringIO(text, newline='')))
    for _, cell, after, _ in edits:
        letters, row = _addr(cell)
        c = col_index(letters)
        while len(rows) < row:
            rows.append([])
        while len(rows[row - 1]) <= c:
            rows[row - 1].append('')
        rows[row - 1][c] = after
    out = io.StringIO(newline='')
    csv.writer(out, lineterminator=newline).writerows(rows)
    return (b'\xef\xbb\xbf' if raw.startswith(b'\xef\xbb\xbf') else b'') + out.getvalue().encode('utf-8')


def _apply_xlsx(raw, edits):
    from hosting.spreadsheet_demo import sheet_paths
    paths = sheet_paths(raw)
    by_path = defaultdict(dict)
    for sheet, cell, after, kind in edits:
        if sheet not in paths:
            raise FixError(f'Sheet "{sheet}" is not in this workbook.')
        by_path[paths[sheet]][cell] = (after, kind)
    out, done = io.BytesIO(), set()
    with zipfile.ZipFile(io.BytesIO(raw)) as src, zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            data = src.read(info.filename)
            if info.filename in by_path:
                xml = data.decode('utf-8')
                for cell, (after, kind) in by_path[info.filename].items():
                    pattern = re.compile(rf'<c\b(?=[^>]*\br="{cell}")([^>]*?)(?:/>|>(.*?)</c>)', re.S)
                    m = pattern.search(xml)
                    if m:
                        if m.group(2) and re.search(r'<f(?:\s|>|/)', m.group(2)):
                            raise FixError(f'{cell} is a formula; nothing was changed.')
                        xml = xml[:m.start()] + _new_cell(m.group(1), cell, after, kind) + xml[m.end():]
                    else:
                        xml = _insert_cell(xml, cell, after, kind)
                    done.add((info.filename, cell))
                data = xml.encode('utf-8')
            elif info.filename == 'xl/workbook.xml':
                xml = data.decode('utf-8')
                calc = '<calcPr calcId="0" fullCalcOnLoad="1"/>'
                xml = re.sub(r'<calcPr\b[^>]*/>', calc, xml, count=1) if '<calcPr' in xml else xml.replace('</workbook>', calc + '</workbook>')
                data = xml.encode('utf-8')
            dst.writestr(info, data)
    if len(done) != len(edits):
        raise FixError('Not every approved change could be applied; nothing was changed.')
    return out.getvalue()


def fixed_filename(filename):
    stem, dot, ext = filename.rpartition('.')
    stem = re.sub(r'\s*\(fixed( \d+)?\)$', '', stem if dot else filename)
    return f'{stem} (fixed).{ext}' if dot else f'{filename} (fixed)'


def row_cells(filename, raw, sheet, row):
    """Current values of one row, with the column headers above it, so a person can edit cells by hand.
    [{cell, header, value, formula}] — formula cells show their saved result and can't be edited here."""
    try:
        row = int(row)
    except (TypeError, ValueError):
        raise FixError('Pick a row number.') from None
    if not 1 <= row <= 1048576:
        raise FixError('Pick a row number.')
    sources, cached = _sources(filename, raw)
    names = [t for t, _ in sources]
    if sheet not in names:
        sheet = names[0] if len(names) == 1 else sheet
    rows = dict(sources).get(sheet)
    if rows is None:
        raise FixError(f'Sheet "{sheet}" is not in this workbook.')
    sheets, _, _ = scan(sources, cached)
    header_row = None
    for s in sheets:
        if s['name'] == sheet:
            for t in s['tables']:
                if t['header_row'] < row and (header_row is None or t['header_row'] > header_row):
                    header_row = t['header_row']
    heads = list(rows[header_row - 1]) if header_row else []
    values = list(rows[row - 1]) if row - 1 < len(rows) else []
    saved = list(cached.get(sheet, [])[row - 1]) if row - 1 < len(cached.get(sheet, [])) else []
    width = max([i + 1 for i, v in enumerate(values) if _show(v).strip()] + [i + 1 for i, v in enumerate(heads) if _show(v).strip()] + [0])
    cells = []
    for i in range(min(width, 200)):
        v = values[i] if i < len(values) else None
        formula = isinstance(v, str) and v.startswith('=')
        shown = saved[i] if formula and i < len(saved) else v
        cells.append(dict(cell=f'{col_letter(i)}{row}', header=_show(heads[i]).strip() if i < len(heads) else '',
                          value=_show(shown), before=_show(v), formula=formula))
    return dict(sheet=sheet, row=row, header_row=header_row, sheets=names, cells=cells)


MAX_GRID_ROWS = 1500
MAX_GRID_COLS = 80


def _layout(raw, sheet):
    """Merged ranges and column widths of one worksheet (xlsx only)."""
    from hosting.spreadsheet_demo import sheet_paths
    try:
        path = sheet_paths(raw).get(sheet)
        if not path:
            return [], {}, set()
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            xml = z.read(path).decode('utf-8', 'replace')
    except Exception:
        return [], {}, set()
    merges = re.findall(r'<mergeCell\b[^>]*\bref="([A-Z]+[0-9]+:[A-Z]+[0-9]+)"', xml)
    widths = {}
    for m in re.finditer(r'<col\b([^>]*)/?>', xml):
        attrs = dict(re.findall(r'(\w+)="([^"]*)"', m.group(1)))
        try:
            lo, hi, w = int(attrs['min']), int(attrs['max']), float(attrs.get('width', 0))
        except (KeyError, ValueError):
            continue
        hidden = attrs.get('hidden') in ('1', 'true')
        for c in range(lo, min(hi, MAX_GRID_COLS) + 1):
            widths[c - 1] = 0 if hidden else w
    hidden_rows = {int(r) for r in re.findall(r'<row\b[^>]*\br="(\d+)"[^>]*\bhidden="(?:1|true)"', xml)}
    return merges, widths, hidden_rows


def sheet_grid(filename, raw, sheet=None):
    """The sheet as shown in Excel: values (formulas as their saved results), merged cells, column widths."""
    sources, cached = _sources(filename, raw)
    names = [t for t, _ in sources]
    if sheet not in names:
        sheet = names[0] if names else None
    if sheet is None:
        raise FixError('This file has no sheets.')
    rows = dict(sources)[sheet]
    saved = cached.get(sheet, [])
    height = max([i + 1 for i, r in enumerate(rows) if any(_show(v).strip() for v in r)] + [0])
    width = max([i + 1 for r in rows[:height] for i, v in enumerate(r) if _show(v).strip()] + [0])
    height, truncated = min(height, MAX_GRID_ROWS), height > MAX_GRID_ROWS or width > MAX_GRID_COLS
    width = min(width, MAX_GRID_COLS)
    values, formulas = [], []
    for r in range(height):
        row, srow = rows[r] if r < len(rows) else [], saved[r] if r < len(saved) else []
        line = []
        for c in range(width):
            v = row[c] if c < len(row) else None
            if isinstance(v, str) and v.startswith('='):
                formulas.append(f'{col_letter(c)}{r + 1}')
                v = srow[c] if c < len(srow) else ''
            line.append(_show(v))
        values.append(line)
    merges, widths, hidden_rows = _layout(raw, sheet) if filename.lower().endswith('.xlsx') else ([], {}, set())
    return dict(sheet=sheet, sheets=names, rows=values, width=width, height=height, formulas=formulas, merges=merges,
                widths={str(k): v for k, v in widths.items() if k < width}, hidden_rows=sorted(r for r in hidden_rows if r <= height),
                truncated=truncated)
