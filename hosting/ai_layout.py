"""AI layout detection: Claude reads every sheet and says where the inspection tables are.

Runs on every upload when ANTHROPIC_API_KEY is set (turn off with AI_LAYOUT=0). The answer is only
a map of tables and columns; the automatic checks still read the cells themselves. Any failure
(no key, network, bad answer) returns None and the keyword detection is used as before.

Configuration (Vercel environment variables):
  ANTHROPIC_API_KEY   required (Sensitive)
  AI_LAYOUT           optional, '0' turns AI layout detection off
  AI_LAYOUT_MODEL     optional, defaults to AI_REVIEW_MODEL, then claude-sonnet-5-5
"""
import hashlib
import json
import os
import re
from collections import OrderedDict

MAX_CHARS = 70_000          # sheet text sent across all sheets
HEAD_ROWS = 150             # rows sent from the top of a long sheet
TAIL_ROWS = 25              # rows sent from the bottom of a long sheet
CELL_CHARS = 60
TIMEOUT = 90
QTY_FIELDS = ('inspected', 'certified', 'rejected', 'reworked')
LABEL_FIELDS = ('identifier', 'part', 'date', 'shift', 'defect', 'condition', 'auditor', 'area', 'comment')
_CACHE = OrderedDict()
_CACHE_SIZE = 16

SYSTEM = """You map the layout of PPG inspection / sort / end-of-shift reports (automotive and aerospace quality containment).
Files come in any layout: several sheets, several tables per sheet, titles and logos above headers, blank spacer rows,
merged headers, totals blocks, summary tabs, and VIN or serial scan lists where each row is one inspected part.

Look at EVERY sheet and EVERY table. Return each table that holds inspection data:
- kind "detail": one row per part number, lot, container, VIN or scan (the rows to check).
- kind "totals": a block of shift/day totals or a summary of the detail rows.
Ignore title blocks, sign-off boxes, instructions, contact lists and lookup tables.

For each table give the sheet name exactly as shown, the 1-based header row, the first and last data rows, and the
column LETTERS for every field you can find:
  inspected (sorted/checked/qty inspected), certified (good/OK/accepted/passed), rejected (NG/NOK/defects/suspect/scrap),
  reworked, identifier (lot/serial/VIN/tag/container/scan id), part (part number/name/model), date, shift, defect
  (reject reason/defect type), condition (OK/NOK/status/result/disposition), auditor (inspector/operator), area, comment.
If a detail table has no count columns because each row is one inspected unit (e.g. a VIN scan list with an OK/NOK
status), set one_row_per_unit true, give the condition column, and list the exact cell values that mean rejected in
reject_values (e.g. "NOK", "NG", "Suspect", "Reject", "Fail").
Only use column letters that really hold that field. Never invent sheets or rows."""

TOOL = dict(name='report_layout', description='Where the inspection tables are in this workbook.', input_schema={
    'type': 'object',
    'properties': {
        'tables': {'type': 'array', 'items': {'type': 'object', 'properties': {
            'sheet': {'type': 'string'},
            'kind': {'type': 'string', 'enum': ['detail', 'totals']},
            'header_row': {'type': 'integer'},
            'first_data_row': {'type': 'integer'},
            'last_data_row': {'type': ['integer', 'null']},
            'columns': {'type': 'object', 'properties': {f: {'type': ['string', 'null']} for f in QTY_FIELDS + LABEL_FIELDS}},
            'one_row_per_unit': {'type': 'boolean'},
            'reject_values': {'type': 'array', 'items': {'type': 'string'}},
            'why': {'type': 'string'}},
            'required': ['sheet', 'kind', 'header_row', 'first_data_row', 'columns']}},
        'notes': {'type': 'string'}},
    'required': ['tables']})


def enabled():
    return bool(os.environ.get('ANTHROPIC_API_KEY')) and os.environ.get('AI_LAYOUT', '1') != '0'


def _col(index):
    name, index = '', index + 1
    while index:
        index, rem = divmod(index - 1, 26)
        name = chr(65 + rem) + name
    return name


def col_index(letter):
    if not isinstance(letter, str) or not re.fullmatch(r'[A-Za-z]{1,3}', letter.strip()):
        return None
    n = 0
    for ch in letter.strip().upper():
        n = n * 26 + ord(ch) - 64
    return n - 1


def sheet_text(sources, cached=None):
    cached = cached or {}
    parts, used = [], 0
    for title, rows in sources:
        rows = list(rows)
        saved = list(cached.get(title) or [])
        lines = []
        for r, row in enumerate(rows, 1):
            cells = []
            for c, value in enumerate(row):
                text = '' if value is None else str(value).strip()
                if text.startswith('='):
                    sr = saved[r - 1] if r - 1 < len(saved) else ()
                    res = sr[c] if c < len(sr) else None
                    text = f'{res}' if res not in (None, '') else '(formula)'
                if text:
                    cells.append(f'{_col(c)}={text[:CELL_CHARS]}')
            if cells:
                lines.append(f'r{r}: ' + ' | '.join(cells))
        if len(lines) > HEAD_ROWS + TAIL_ROWS:
            lines = lines[:HEAD_ROWS] + [f'... {len(lines) - HEAD_ROWS - TAIL_ROWS} similar rows not shown ...'] + lines[-TAIL_ROWS:]
        block = f'=== Sheet "{title}" ({len(rows)} rows) ===\n' + ('\n'.join(lines) or '(empty)')
        if used + len(block) > MAX_CHARS:
            block = block[:max(0, MAX_CHARS - used)] + '\n... (cut for length)'
        parts.append(block)
        used += len(block)
        if used >= MAX_CHARS:
            break
    return '\n\n'.join(parts)


def _validate(answer, sources):
    widths = {title: (len(rows := list(rs)), max((len(r) for r in rows), default=0)) for title, rs in sources}
    by_norm = {t.strip().lower(): t for t in widths}
    out = []
    for t in (answer.get('tables') or [])[:40]:
        if not isinstance(t, dict):
            continue
        title = by_norm.get(str(t.get('sheet') or '').strip().lower())
        if not title:
            continue
        nrows, ncols = widths[title]
        try:
            header = int(t.get('header_row'))
            first = int(t.get('first_data_row') or header + 1)
            last = int(t['last_data_row']) if t.get('last_data_row') else nrows
        except (TypeError, ValueError):
            continue
        if not (1 <= header <= nrows and header < first <= max(first, nrows)):
            continue
        cols = {}
        for f, letter in (t.get('columns') or {}).items():
            i = col_index(letter)
            if f in QTY_FIELDS + LABEL_FIELDS and i is not None and i < ncols:
                cols[f] = i
        unit = bool(t.get('one_row_per_unit')) and not any(f in cols for f in QTY_FIELDS)
        if not unit and not any(f in cols for f in QTY_FIELDS):
            continue
        rejects = [str(v).strip().lower() for v in (t.get('reject_values') or []) if str(v).strip()][:30]
        out.append(dict(sheet=title, kind='totals' if t.get('kind') == 'totals' else 'detail', header_row=header,
                        first_data_row=first, last_data_row=min(max(last, first), nrows), columns=cols,
                        one_row_per_unit=unit, reject_values=rejects or ['nok', 'ng', 'reject', 'rejected', 'suspect', 'fail', 'failed', 'bad'],
                        why=str(t.get('why') or '')[:300]))
    return out


def detect(sources, cached=None, opener=None):
    """Return validated table specs, or None. Never raises."""
    if not enabled():
        return None
    try:
        sources = [(title, list(rows)) for title, rows in sources]
        text = sheet_text(sources, cached)
        key = hashlib.sha256(text.encode()).hexdigest()
        if key in _CACHE:
            _CACHE.move_to_end(key)
            return _CACHE[key]
        from hosting import ai_review
        model = os.environ.get('AI_LAYOUT_MODEL') or os.environ.get('AI_REVIEW_MODEL') or ai_review.DEFAULT_MODEL
        old_timeout, ai_review.TIMEOUT = ai_review.TIMEOUT, TIMEOUT
        try:
            data = ai_review._post(dict(model=model, max_tokens=4000, system=SYSTEM, tools=[TOOL],
                                        tool_choice={'type': 'tool', 'name': 'report_layout'},
                                        messages=[{'role': 'user', 'content': 'Map the inspection tables in this workbook.\n\n' + text}]), opener)
        finally:
            ai_review.TIMEOUT = old_timeout
        answer = ai_review._tool_input(data, 'a layout')
        tables = _validate(answer, sources) or None
        result = dict(tables=tables, notes=str(answer.get('notes') or '')[:500], model=data.get('model') or model) if tables else None
        _CACHE[key] = result
        while len(_CACHE) > _CACHE_SIZE:
            _CACHE.popitem(last=False)
        return result
    except Exception:
        return None


def apply_units(sources, layout):
    """For one-row-per-unit tables (scan lists), add Inspected/Rejected/Certified columns worked out from the
    condition column, so the normal checks can count them. Returns new sources and the specs updated with them."""
    if not layout:
        return sources, []
    specs = [dict(t, columns=dict(t['columns'])) for t in layout['tables']]
    out = []
    for title, rows in sources:
        rows = [list(r) for r in rows]
        for t in specs:
            if t['sheet'] != title or not t['one_row_per_unit']:
                continue
            width = max((len(r) for r in rows), default=0)
            ins, nok, ok = width, width + 1, width + 2
            cond = t['columns'].get('condition')
            ident = [t['columns'][k] for k in ('identifier', 'part') if k in t['columns']]
            for r in range(t['header_row'], t['last_data_row'] + 1):
                row = rows[r - 1]
                row.extend([None] * (ok + 1 - len(row)))
                if r == t['header_row']:
                    row[ins], row[nok], row[ok] = 'Inspected (1 per row)', 'Rejected (from condition)', 'Certified (from condition)'
                    continue
                if r < t['first_data_row']:
                    continue
                has_unit = any(i < len(row) and str(row[i] or '').strip() for i in ident) or (cond is not None and str(row[cond] or '').strip())
                if not has_unit:
                    continue
                bad = cond is not None and str(row[cond] or '').strip().lower() in t['reject_values']
                row[ins], row[nok], row[ok] = 1, int(bad), int(not bad)
            t['columns'].update(inspected=ins, rejected=nok, certified=ok)
        out.append((title, rows))
    return out, specs
