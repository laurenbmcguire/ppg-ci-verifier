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


def merged_ranges(filename, raw):
    """{sheet: [(min_col, min_row, max_col, max_row, 'B4:D4'), ...]} from the workbook's own mergeCell list."""
    if not filename.lower().endswith('.xlsx'):
        return {}
    try:
        from zipfile import ZipFile
        from hosting.spreadsheet_demo import sheet_paths
        out = {}
        with ZipFile(io.BytesIO(raw)) as z:
            for name, path in sheet_paths(raw).items():
                xml = z.read(path).decode('utf-8', 'replace')
                for ref in re.findall(r'<mergeCell\b[^>]*\bref="([A-Z]+[0-9]+:[A-Z]+[0-9]+)"', xml):
                    a, b = ref.split(':')
                    (ca, ra), (cb, rb) = _addr(a), _addr(b)
                    out.setdefault(name, []).append((col_index(ca), ra, col_index(cb), rb, ref))
        return out
    except Exception:
        return {}


def merged_problem(merged, sheet, cell):
    """Why a cell must not be written: it is inside a merged range but is not the range's top-left cell."""
    letters, row = _addr(cell)
    col = col_index(letters)
    for c1, r1, c2, r2, ref in merged.get(sheet) or []:
        if c1 <= col <= c2 and r1 <= row <= r2 and (col, row) != (c1, r1):
            return f'{sheet}!{cell} is part of the merged cells {ref}; only {ref.split(":")[0]} holds a value there.'
    return None


def _is_merged_label(merged, sources, sheet, cell):
    letters, row = _addr(cell)
    col = col_index(letters)
    if not any((c1, r1) == (col, row) for c1, r1, *_ in merged.get(sheet) or []):
        return False
    v = _cell_value(sources, sheet, cell)
    return isinstance(v, str) and bool(v.strip()) and not re.search(r'\d', v)


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


SURE_SOURCES = ('SharePoint', 'Clean-up', 'Number as text', 'Known detail')
# values that came from the app or from Claude rather than being typed by a person: re-checked for type when applied
MACHINE_SOURCES = SURE_SOURCES + ('Suggested', 'AI fix', 'AI fix (asked)', 'Spelling', 'Typo', 'Claude (chat)', 'SharePoint export')
WRITTEN_ONLY = {   # fixes that only change how something is written, never what it means
    'Clean-up': 'Only extra spaces or invisible characters. The value is right; nothing else is wrong.',
    'Number as text': 'Only the way the number is stored. The number is right; nothing else is wrong.',
    'Spelling': 'Only the spelling is different from the other rows. Same value; nothing else is wrong.',
    'Typo': 'Looks like the same value typed with a letter for a digit (like O for 0). If that is all, nothing else is wrong.',
}


_DATE_LABEL = re.compile(r'(?i)\b(date|day)\b')
_NUM_LABEL = re.compile(r'(?i)\b(qty|quantity|inspected|rejected|rejects?|nok|ok qty|okqty|count|total|scans?|units?|reworked|certified)\b')


def _label_for(sources, sheet, cell):
    """The words that say what a cell holds: the text just left of it (a 'Label | value' layout) or the nearest
    text above it in the same column (a table header)."""
    letters, row = _addr(cell)
    rows = dict(sources).get(sheet) or []
    c = col_index(letters)
    if c > 0 and row - 1 < len(rows) and c - 1 < len(rows[row - 1]):
        left = rows[row - 1][c - 1]
        if isinstance(left, str) and left.strip() and not re.search(r'\d{3,}', left):
            return left.strip()
    for r in range(row - 2, max(-1, row - 40), -1):
        v = rows[r][c] if r < len(rows) and c < len(rows[r]) else None
        if isinstance(v, str) and v.strip() and not re.fullmatch(r'[\d\s.,:/-]+', v.strip()):
            return v.strip()
    return ''


def type_problem(sources, sheet, cell, after, before=None):
    """Why a proposed value cannot go into this cell, or None. A label or header word is never a value, a date
    field takes only a date and a quantity field only a number: a suggestion that fails is dropped, never offered."""
    text = str(after if after is not None else '').strip()
    if not text:
        return None
    norm = lambda v: re.sub(r'[\s\u00a0\u200b-\u200f\ufeff]+', ' ', str(v if v is not None else '')).strip().lower()
    if before is not None and norm(before) == norm(text):
        return None   # only spacing, hidden characters or capitals change: the value itself stays what it was
    from hosting.suggest import HEADER_WORDS
    if text.lower() in HEADER_WORDS or text.lower() in ('date', 'shift', 'client', 'job', 'part', 'description'):
        return f'"{text}" is a column or label name, not a value.'
    label = _label_for(sources, sheet, cell)
    if _DATE_LABEL.search(label) and not re.search(r'(?i)shift|time', label):
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4}|\d{1,2}-[A-Za-z]{3}-\d{2,4}|[A-Za-z]{3,9}\.? \d{1,2},? \d{4}', text):
            return f'"{label}" holds a date; "{text}" is not a date.'
    elif _NUM_LABEL.search(label) and not re.search(r'(?i)part|number|id\b|lot|serial|name', label):
        if not re.fullmatch(r'-?[\d,]*\.?\d+', text):
            return f'"{label}" holds a number; "{text}" is not a number.'
    return None


def proposals(result, filename, raw, sharepoint_rows=None, context=None):
    """List of proposed fixes: {id, sheet, cell, before, after, kind, reason, source, confidence}."""
    _merged = merged_ranges(filename, raw)
    sources, cached = _sources(filename, raw)
    sheets, records, _ = scan(sources, cached)
    detail = [r for r in records if r['table_kind'] == 'detail' and not r['is_total']]
    out, seen = [], set()

    def add(sheet, cell, after, reason, source, confidence='high', sure=None):
        if not sheet or not cell or len(out) >= MAX_PROPOSALS:
            return
        if merged_problem(_merged, sheet, cell):
            return   # never write into the covered part of a merged header or date
        try:
            before = _cell_value(sources, sheet, cell)
        except FixError:
            return
        if isinstance(before, str) and before.startswith('='):
            return  # never overwrite a formula
        if type_problem(sources, sheet, cell, after, before):
            return  # wrong type for the field, or a header word: never a source-backed value
        kind = 'number' if isinstance(after, (int, float, Decimal)) and not isinstance(after, bool) else 'text'
        if (_show(before) == _show(after) and isinstance(before, str) == (kind == 'text')) or (sheet, cell) in seen:
            return
        seen.add((sheet, cell))
        after_text = (str(Decimal(after).normalize()) if Decimal(after) != Decimal(after).to_integral_value() else str(int(Decimal(after)))) if kind == 'number' else str(after)
        if sure is None:   # owner's rule: only certain, mechanical corrections are "sure"; guesses always go to a person
            sure = confidence == 'high' and source in SURE_SOURCES
        only = WRITTEN_ONLY.get(source)
        if source == 'Spelling' and 'different capitals' in reason:
            only = 'Only the capital letters are different. Same part; nothing else is wrong.'
        out.append(dict(id=_pid(sheet, cell, after_text), sheet=sheet, cell=cell, before=_show(before), after=after_text,
                        kind=kind, reason=reason, source=source, confidence=confidence, sure=bool(sure), only=only))

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
            n = len(out)
            add(f.get('sheet'), f['cell'], value, f'SharePoint shows {f["sharepoint_value"]} for this scan ({f.get("message", "")})', 'SharePoint')
            if len(out) > n:   # bound to the classic comparison it came from: a re-run with other values makes it stale
                from hosting.review_summary import source_check_id
                out[-1].update(snapshot_id='classic:' + str(source_check_id(result)), item=f.get('item') or f.get('row_id') or f'{f.get("sheet")}!{f["cell"]}',
                               field=f.get('field') or 'value', source_value=f['sharepoint_value'])

    # 1a. The live SharePoint reconciliation: each value that differs takes the SharePoint value (SharePoint is the record).
    from hosting import reconcile as _rec
    rec = _rec.current(result) or {}
    if rec:
        for c in rec.get('proposed_corrections') or []:
            if c.get('identity_verified') is True and c.get('sheet') and c.get('column') and c.get('row') and c.get('after') not in (None, 'blank'):
                # Quantity cells are numeric; identifiers and other text retain their exact spelling.
                # Keep the source literal in the proposal/receipt so the exact source guard can verify it.
                v = _num(c['after']) if c.get('field') in _rec.QTY else c['after']
                if v is None or isinstance(v, bool):
                    continue
                n = len(out)
                add(c['sheet'], f'{col_letter(int(c["column"]) - 1)}{c["row"]}', v,
                    f'SharePoint item {c.get("item")} shows {c["after"]} for {c.get("field")}; the report shows {c.get("before")}.', 'SharePoint')
                if len(out) > n:   # bound to the SharePoint read it came from: a newer read makes it stale
                    source_after = str(c['after'])
                    out[-1].update(after=source_after, id=_pid(out[-1]['sheet'], out[-1]['cell'], source_after),
                                   snapshot_id=rec.get('snapshot_id'), captured_at=rec.get('captured_at'), item=c.get('item'), field=c.get('field'),
                                   source_value=c.get('after'))

    # 1c. Blanks on record rows that the report's own SharePoint export fills (matched by SharePoint item ID).
    try:
        from hosting.spreadsheets import report_and_export_records
        recs, exports = report_and_export_records(filename, raw)
        from hosting.sheet_roles import bound_source_key
        by_identity, duplicate_keys = {}, set()
        for e in exports:
            key = bound_source_key(e.get('source'))
            if key is None:
                continue
            if key in by_identity:
                duplicate_keys.add(key)
            by_identity[key] = e
        for r in recs:
            key = bound_source_key(r.get('source'))
            e = by_identity.get(key) if key is not None and key not in duplicate_keys else None
            if not e:
                continue
            for field in ('inspected', 'certified', 'rejected', 'reworked'):
                if field in r['values'] and r['values'][field] is None and e['values'].get(field) is not None:
                    n = len(out)
                    add(r['sheet'], qty_cell(r, field), e['values'][field],
                        f'{field.title()} is blank here; SharePoint item {e["source"]["item"]} has {e["values"][field]}.', 'SharePoint')
                    if len(out) > n:   # from the export inside this very file: it cannot change for this version
                        out[-1].update(snapshot_id='export:' + str(result.get('file_hash')), item=e['source']['item'], field=field, source_value=e['values'][field])
            for kind in ('identifier', 'part', 'defect', 'condition', 'area', 'shift'):
                if kind in r['labels'] and not r['labels'][kind] and e['labels'].get(kind):
                    n = len(out)
                    add(r['sheet'], text_cell(r, kind), e['labels'][kind],
                        f'{kind.title()} is blank here; SharePoint item {e["source"]["item"]} has "{e["labels"][kind]}".', 'SharePoint')
                    if len(out) > n:
                        out[-1].update(snapshot_id='export:' + str(result.get('file_hash')), item=e['source']['item'], field=kind, source_value=e['labels'][kind])
    except Exception:
        pass   # filling from the export must never break the other fixes

    # 1b. Blank details (paying client, worksite, date, shift, area...): fill what is known; suggest the rest.
    from hosting import suggest
    try:
        suggest.blank_details(sources, context or {}, add, col_letter)
        suggest.blank_columns(detail, context or {}, add, text_cell)
    except Exception:
        pass   # a suggestion must never break the real fixes

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

    # 2b. The same clean-up on every other text cell of a record row (part names, areas, auditors, comments...).
    by_sheet = dict(sources)
    for r in detail:
        row_vals = (by_sheet.get(r['sheet']) or [])[r['row'] - 1] if r['row'] - 1 < len(by_sheet.get(r['sheet']) or []) else []
        for c_i, raw_value in enumerate(row_vals or []):
            if not isinstance(raw_value, str) or raw_value.startswith('='):
                continue
            cleaned = re.sub(r'[ \t]{2,}', ' ', HIDDEN.sub(lambda m: ' ' if m.group(0) == ' ' else '', raw_value)).strip()
            if cleaned and cleaned != raw_value:
                add(r['sheet'], f'{col_letter(c_i)}{r["row"]}', cleaned, 'Removes hidden characters or extra spaces so matching and totals work.', 'Clean-up')

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
            add(r['sheet'], text_cell(r, 'part'), best, f'Part number written as "{best}" on {spellings[best]} other row(s); same part, different capitals.', 'Spelling', sure=True)   # only the capitals differ

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
    if kind == 'blank' or value == '':
        return f'<c r="{address}"{s}/>'
    if kind == 'formula':
        return f'<c r="{address}"{s}><f>{_xml_text(value.lstrip("="))}</f></c>'
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
    merged = merged_ranges(filename, raw)
    edits = []
    for change in approved:
        sheet, cell = change.get('sheet'), (change.get('cell') or '').upper()
        _addr(cell)
        problem = merged_problem(merged, sheet, cell)
        if problem:
            raise FixError(problem + ' Nothing was applied.')
        if change.get('source') in MACHINE_SOURCES and _is_merged_label(merged, sources, sheet, cell):
            raise FixError(f'{sheet}!{cell} is a merged heading or label, not a value; a suggestion never replaces it. Nothing was applied.')
        current = _cell_value(sources, sheet, cell)
        if isinstance(current, str) and current.startswith('='):
            raise FixError(f'{sheet}!{cell} is a formula; change the cells it uses instead.')
        if 'before' in change and _show(current) != str(change['before']):
            raise FixError(f'{sheet}!{cell} now holds "{_show(current)}", not "{change["before"]}". Re-open the report and review again.')
        after = str(change.get('after', ''))
        if change.get('kind') != 'formula' and change.get('source') in MACHINE_SOURCES:   # a person's own typed value is theirs; every suggestion is re-checked here
            problem = type_problem(sources, sheet, cell, after, current)
            if problem:
                raise FixError(f'{sheet}!{cell}: {problem} That suggestion cannot be applied.')
        kind = 'blank' if after == '' else change.get('kind') if change.get('kind') in ('number', 'text', 'formula') else ('number' if _num(after) is not None and isinstance(current, (int, float)) and not isinstance(current, bool) else 'text')
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
    from hosting.recalc import recalc
    return recalc(out.getvalue())  # saved formula results match the new values, as if Excel had saved it


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


def relocate_rows(filename, raw, moves):
    """Cell changes that put report rows on the right shift tab.
    moves: [(sheet, row, target_sheet or None)]. A row moves to the end of the target tab's table, or is taken
    out when target_sheet is None (it belongs in another shift's report). Rows below a removed row move up so the
    table has no gaps; formula cells (Date / Shift / Area columns, totals) are never touched."""
    from hosting.spreadsheets import report_records
    sources, _ = _sources(filename, raw)
    grid = dict(sources)
    recs = [r for r in report_records(filename, raw) if r.get('table_kind') == 'detail']
    if not recs:
        raise FixError('No inspection rows were found to move.')
    by_sheet = defaultdict(list)
    for r in recs:
        by_sheet[r['sheet']].append(r)
    span = {}
    for sheet, rows in by_sheet.items():
        cols = set()
        for r in rows:
            cols.update(c - 1 for c in r['columns'].values())
            cols.update(col_index(l) for l in (r.get('label_columns') or {}).values())
        span[sheet] = range(min(cols), max(cols) + 1)
    shift_col = {sheet: col_index(rows[0]['label_columns']['shift']) for sheet, rows in by_sheet.items()
                 if (rows[0].get('label_columns') or {}).get('shift')}
    value = lambda sheet, row, c: (grid[sheet][row - 1][c] if row - 1 < len(grid[sheet]) and c < len(grid[sheet][row - 1]) else '')
    is_formula = lambda v: isinstance(v, str) and v.startswith('=')
    def payload(sheet, row):
        out = {c: value(sheet, row, c) for c in span[sheet] if not is_formula(value(sheet, row, c))}
        out['_formulas'] = {c: (value(sheet, row, c), row) for c in span[sheet] if is_formula(value(sheet, row, c))}
        return out
    order = {sheet: [r['row'] for r in sorted(rows, key=lambda r: r['row'])] for sheet, rows in by_sheet.items()}
    content = {sheet: [(row, payload(sheet, row)) for row in rows] for sheet, rows in order.items()}
    moving = {}
    for sheet, row, target in moves:
        if sheet not in order or row not in order[sheet]:
            raise FixError(f'{sheet} row {row} is not an inspection row any more. Re-open the report and try again.')
        if target is not None and target not in order and target not in grid:
            raise FixError(f'There is no "{target}" tab in this workbook.')
        moving[(sheet, row)] = target
    for (sheet, row), target in sorted(moving.items(), key=lambda m: (m[0][0], m[0][1])):
        data = dict(content[sheet])[row]
        content[sheet] = [(r, d) for r, d in content[sheet] if r != row]
        if target:
            moved = {c: data.get(c, '') for c in span.get(target, span[sheet])}
            moved['_formulas'] = data.get('_formulas') or {}
            col = shift_col.get(sheet)
            if col is not None and col in moved and moved[col] not in ('', None):
                moved[col] = _shift_value(moved[col], target, grid, order, col, value)
            content.setdefault(target, []).append((None, moved))
    content = {sheet: [d for _, d in rows] for sheet, rows in content.items()}
    changes = []
    for sheet in set(order) | {t for t in moving.values() if t}:
        rows = list(order.get(sheet, []))
        cols = span.get(sheet) or next(iter(span.values()))
        need = len(content.get(sheet, []))
        nxt = rows[-1] + 1 if rows else _start_row(grid, order, sheet, cols, value)
        while len(rows) < need:  # extend into the blank rows below the table
            if any(value(sheet, nxt, c) not in ('', None) and not is_formula(value(sheet, nxt, c)) for c in cols):
                raise FixError(f'The "{sheet}" tab has no empty row below its table for {need - len(rows)} more car(s). Add rows in Excel first.')
            rows.append(nxt); nxt += 1
        for i, row in enumerate(rows):
            new = content[sheet][i] if i < need else {}
            for c, (formula, src_row) in (new.get('_formulas') or {}).items():
                if value(sheet, row, c) in ('', None):  # the row's own formula (e.g. Certified = Inspected - Rejected) comes with it
                    from hosting.recalc import _shift
                    changes.append(dict(sheet=sheet, cell=f'{col_letter(c)}{row}', before='', after='=' + _shift(formula.lstrip('='), row - src_row, 0),
                                        kind='formula', reason='Put the row on the right shift (by scan time)', source='Shift times'))
            for c in cols:
                old = value(sheet, row, c)
                if is_formula(old) or (c in (new.get('_formulas') or {}) and old in ('', None)):
                    continue
                after = new.get(c, '')
                after = '' if after is None else after
                if _show(old) != _show(after):
                    changes.append(dict(sheet=sheet, cell=f'{col_letter(c)}{row}', before=_show(old), after=_show(after),
                                        kind='blank' if after == '' else ('number' if isinstance(after, (int, float)) and not isinstance(after, bool) else 'text'),
                                        reason='Put the row on the right shift (by scan time)', source='Shift times'))
    return changes


def _start_row(grid, order, sheet, cols, value):
    """First data row on a shift tab that has no rows yet: below the header that matches another tab's header,
    with the same gap between header and data as that tab."""
    norm = lambda v: re.sub(r'\s+', ' ', str(v or '')).strip().lower()
    for src, rows in order.items():
        if not rows or src == sheet:
            continue
        for hs in range(rows[0] - 1, 0, -1):
            head = [norm(value(src, hs, c)) for c in cols]
            if sum(1 for x in head if x and not x.startswith('=')) < 3:
                continue
            for ht in range(1, len(grid[sheet]) + 1):
                if [norm(value(sheet, ht, c)) for c in cols] == head:
                    return ht + (rows[0] - hs)
            break
    raise FixError(f'Could not find the table header on the "{sheet}" tab, so rows were not moved there.')


def _shift_value(old, target, grid, order, col, value):
    """The Shift cell for a row moved to another tab, written the way that tab (or the old value) writes it."""
    from hosting.shifts import sheet_shift
    rows = order.get(target) or []
    if rows and value(target, rows[0], col) not in ('', None):
        return value(target, rows[0], col)
    n = sheet_shift(target)
    if not n:
        return old
    text = str(old).strip()
    if re.fullmatch(r'\d', text) or isinstance(old, (int, float)):
        return int(n) if isinstance(old, (int, float)) else n
    if re.fullmatch(r'\d(st|nd|rd|th)', text, re.I):
        return n + {'1': 'st', '2': 'nd', '3': 'rd'}.get(n, 'th')
    return re.sub(r'\b\d(st|nd|rd|th)?\b', lambda m: n + ({'1': 'st', '2': 'nd', '3': 'rd'}.get(n, 'th') if m.group(1) else ''), text, count=1)
