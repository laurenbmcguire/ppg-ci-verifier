"""Bounded, local-only spreadsheet inspection. Never evaluates spreadsheet formulas."""
from __future__ import annotations

import csv
import hashlib
import io
import re
import zipfile
from collections import Counter
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import PurePosixPath

# No upload size limit. Budgets below are Excel's own sheet dimensions plus a per-workbook
# cell budget sized to what the checkers can hold in memory on the hosted function.
MAX_ROWS = 1048576
MAX_COLS = 16384
MAX_CELLS = 2000000
MAX_UNCOMPRESSED = 4 * 1024 * 1024 * 1024
MAX_MEMBERS = 4000
MAX_RATIO = 200
MAX_FIELD = 32767
MAX_FINDINGS = 200
CHECKER_VERSION = '2026-11-07-recorded-rejects-v10'


class SpreadsheetError(ValueError):
    """Unsafe, unsupported, malformed, or over-budget input (safe to show the message)."""


def _number(value):
    if isinstance(value, bool) or value is None:
        return None
    text = str(value).strip()
    if len(text) > 100 or text.startswith('='):
        return None
    try:
        n = Decimal(text)
        return n if n.is_finite() and -100 <= n.as_tuple().exponent <= 100 and n.adjusted() <= 100 else None
    except InvalidOperation:
        return None


def _add(left, right):
    with localcontext() as context:
        context.prec = 256
        return left + right


_STRING = re.compile(r'"(?:[^"]|"")*"')
_EXTERNAL_REF = re.compile(r"\[[1-9][0-9]*\]|\[[^\]\[]+\.(?:xls[xmb]?|xlam|csv)\]|https?://|\\\\\\\\", re.I)
_WEB_FN = re.compile(r'\b(?:WEBSERVICE|IMAGE|FILTERXML)\s*\(', re.I)
_HYPERLINK_FN = re.compile(r'\bHYPERLINK\s*\(', re.I)
_DDE = re.compile(r"\||\b(?:RTD|DDE|DDEAUTO|CALL|REGISTER(?:\.ID)?|EXEC(?:UTE)?|RUN|SQL\.REQUEST)\s*\(", re.I)

# Recorded as findings, never executed. The checker reads stored values only and no
# uploaded original is ever served back, so these are flagged for review instead of refused.
EXTERNAL_CONTENT_CODES = ('external_reference_formula', 'web_function_formula', 'hyperlink_formula',
                          'dde_or_macro_call_formula', 'external_hyperlink', 'external_workbook_link',
                          'external_data_connection', 'dde_link')


def formula_flags(formula):
    """Classify one formula. Table structured references like Table1[Col] or [@Col] are normal."""
    code = _STRING.sub('""', formula or '')
    flags = []
    if _EXTERNAL_REF.search(code):
        flags.append('external_reference_formula')
    if _WEB_FN.search(code):
        flags.append('web_function_formula')
    # HYPERLINK("#'Sheet'!A1", ...) only jumps inside the workbook; that is navigation, not an external link.
    if _HYPERLINK_FN.search(code) and not re.fullmatch(r'(?:[^"]|"")*?\bHYPERLINK\s*\(\s*"#[^"]*"[^()]*\)\s*', formula or '', re.I | re.S):
        flags.append('hyperlink_formula')
    if _DDE.search(code):
        flags.append('dde_or_macro_call_formula')
    return flags


def _part_label(name):
    return PurePosixPath(name).stem


def _xlsx_rows(raw, notes=None):
    notes = notes if notes is not None else []
    # defusedxml is mandatory: no entity expansion or external XML references.
    try:
        from defusedxml import ElementTree as ET
        from openpyxl import load_workbook
    except ImportError as exc:
        raise SpreadsheetError('XLSX support requires openpyxl and defusedxml on the server.') from exc

    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
        with archive:
            members = archive.infolist()
            if len(members) > MAX_MEMBERS:
                raise SpreadsheetError('Archive has too many members.')
            names = set()
            total = 0
            for member in members:
                name = member.filename
                path = PurePosixPath(name)
                if (name in names or '\\' in name or path.is_absolute() or '..' in path.parts
                        or ':' in name or '\x00' in name):
                    raise SpreadsheetError('Unsafe or duplicate archive path.')
                names.add(name)
                if member.flag_bits & 1:
                    raise SpreadsheetError('Encrypted spreadsheets are not supported.')
                total += member.file_size
                if total > MAX_UNCOMPRESSED or member.file_size > MAX_UNCOMPRESSED:
                    raise SpreadsheetError('Archive expansion exceeds the allowed size.')
                if member.file_size / max(member.compress_size, 1) > MAX_RATIO:
                    raise SpreadsheetError('Archive compression ratio exceeds the allowed limit.')
                low = name.lower()
                if (any(token in low for token in ('vbaproject', 'macrosheet', 'embeddings/', 'activex/', 'customui/'))
                        or (low.endswith('.bin') and not low.startswith('xl/printersettings/'))
                        or low.endswith(('.exe', '.vbs', '.js', '.dll', '.ps1'))):
                    raise SpreadsheetError('Macros or embedded active content (VBA, ActiveX, OLE objects) are not supported. Save a copy as .xlsx without macros or embedded objects.')
                if 'externallink' in low and low.endswith('.xml'):
                    notes.append(('external_workbook_link', _part_label(name)))
                if (low.endswith('connections.xml') or 'querytables/' in low) and low.endswith('.xml'):
                    notes.append(('external_data_connection', _part_label(name)))
            if not {'[Content_Types].xml', 'xl/workbook.xml'}.issubset(names):
                raise SpreadsheetError('File is not a supported XLSX workbook.')
            cells = 0
            for member in members:
                if not member.filename.lower().endswith(('.xml', '.rels')):
                    continue
                with archive.open(member) as stream:
                  for _event, element in ET.iterparse(stream, events=('end',), forbid_dtd=True):
                    tag = element.tag.rsplit('}', 1)[-1]
                    if tag == 'Override' and any(t in element.attrib.get('ContentType', '').lower()
                                                 for t in ('macroenabled', 'macrosheet', 'activex')):
                        raise SpreadsheetError('Macros or active content are not supported.')
                    if tag == 'Relationship' and element.attrib.get('TargetMode', '').lower() == 'external':
                        kind = element.attrib.get('Type', '').rsplit('/', 1)[-1].lower()
                        if kind in ('oleobject', 'package', 'control', 'activexcontrol', 'attachedtemplate'):
                            raise SpreadsheetError('Linked OLE/ActiveX objects are not supported. Save a copy without embedded or linked objects.')
                        notes.append(('external_hyperlink' if kind == 'hyperlink' else 'external_workbook_link', _part_label(member.filename)))
                    if tag in ('oleObject', 'control'):
                        raise SpreadsheetError('Embedded OLE objects or ActiveX controls are not supported. Save a copy without them.')
                    if tag in ('ddeLink', 'oleLink'):
                        notes.append(('dde_link', _part_label(member.filename)))
                    if member.filename.startswith('xl/worksheets/'):
                        if tag == 'row' and int(element.attrib.get('r', '1')) > MAX_ROWS:
                            raise SpreadsheetError('Spreadsheet exceeds the row limit.')
                        if tag == 'c':
                            cells += 1
                            coordinate = re.fullmatch(r'([A-Z]+)([1-9][0-9]*)', element.attrib.get('r', ''))
                            if not coordinate or len(coordinate[1]) > 3 or len(coordinate[2]) > 7:  # Excel: XFD1048576
                                raise SpreadsheetError('Invalid spreadsheet cell coordinate.')
                            col = 0
                            for letter in coordinate[1]:
                                col = col * 26 + ord(letter) - 64
                            if col > MAX_COLS or int(coordinate[2]) > MAX_ROWS or cells > MAX_CELLS:
                                raise SpreadsheetError('Spreadsheet exceeds the cell, row, or column limit.')
                    if tag in ('f', 'definedName'):
                        for flag in formula_flags(element.text or ''):
                            notes.append((flag, _part_label(member.filename) if tag == 'f' else 'workbook names'))
                    if tag in ('row', 'c', 'v', 'f', 'is', 't', 'si', 'r'):
                        element.clear()
        workbook = load_workbook(io.BytesIO(raw), read_only=True, data_only=False, keep_links=False)
        try:
            for sheet in workbook.worksheets:
                if len(sheet.title) > 128:
                    raise SpreadsheetError('Worksheet name exceeds the length limit.')
                # Ignore attacker-controlled dimension metadata, preflight bounded actual coordinates.
                sheet.reset_dimensions()
                yield sheet.title, sheet.iter_rows(values_only=True)
        finally:
            workbook.close()
    except SpreadsheetError:
        raise
    except Exception as exc:
        raise SpreadsheetError('This file could not be opened as an Excel workbook. It may be damaged or saved in another format. Open it in Excel, save it as .xlsx, and upload it again.') from exc


def _cached_rows(raw):
    """Values Excel last saved for each cell (formula results), read after the safety preflight passed."""
    try:
        from openpyxl import load_workbook
        workbook = load_workbook(io.BytesIO(raw), read_only=True, data_only=True, keep_links=False)
    except Exception:
        return {}
    try:
        out = {}
        for sheet in workbook.worksheets:
            sheet.reset_dimensions()
            out[sheet.title] = [tuple(row) for row in sheet.iter_rows(values_only=True)]
        return out
    except Exception:
        return {}
    finally:
        workbook.close()


def inspect_spreadsheet(filename: str, raw: bytes, keep_records: bool = False) -> dict:
    """Return internal checks only; rejects unsafe input with SpreadsheetError.

    A successful parse is not source authentication, external reconciliation, or
    approval for release. First row is treated as headers; no schema is guessed.
    """
    if not isinstance(raw, bytes) or not raw:
        raise SpreadsheetError('Spreadsheet is empty.')
    if not isinstance(filename, str) or not filename or len(filename) > 255:
        raise SpreadsheetError('A filename of at most 255 characters is required.')
    fmt = filename.rsplit('.', 1)[-1].lower()
    if fmt not in ('csv', 'xlsx'):
        raise SpreadsheetError('Only UTF-8 CSV and XLSX files are supported.')
    digest = hashlib.sha256(raw).hexdigest()
    result = dict(filename=filename, file_sha256=digest, spreadsheet_id=digest,checker_version=CHECKER_VERSION,
                  format=fmt, byte_count=len(raw), verification_scope='internal_spreadsheet_checks_only',
                  source_authenticity='not_supplied', release_status='held',
                  formulas_evaluated=False, sheets=[], findings=[], findings_truncated=False,
                  limitations=['First row is treated as headers.', 'Formulas are not evaluated.',
                               'No external source reconciliation or authenticity verification was performed.'])
    counts = Counter()
    pristine = dict(result, sheets=[], findings=[], limitations=list(result['limitations']))

    def finding(code, sheet, row=None, column=None):
        counts[code] += 1
        if len(result['findings']) < MAX_FINDINGS:
            result['findings'].append(dict(code=code, sheet=sheet, row=row, column=column))
        else:
            result['findings_truncated'] = True

    notes = []
    if fmt == 'csv':
        try:
            decoded = raw.decode('utf-8-sig')
        except UnicodeDecodeError as exc:
            raise SpreadsheetError('CSV must use UTF-8 encoding.') from exc
        if '\x00' in decoded:
            raise SpreadsheetError('CSV contains unsupported null characters.')
        sources = [('CSV', csv.reader(io.StringIO(decoded, newline=''), strict=True))]
    else:
        # Materialize only within the same row/cell limits after ZIP/XML preflight.
        sources=[];bounded_rows=bounded_cells=0
        for title, iterator in _xlsx_rows(raw, notes):
            sheet_rows=[]
            for row in iterator:
                bounded_rows+=1;bounded_cells+=len(row)
                if bounded_rows>MAX_ROWS or len(row)>MAX_COLS or bounded_cells>MAX_CELLS:
                    raise SpreadsheetError('Spreadsheet exceeds the cell, row, or column limit.')
                if any(isinstance(v,str) and len(v)>MAX_FIELD for v in row):raise SpreadsheetError('Spreadsheet field exceeds the length limit.')
                sheet_rows.append(row)
            sources.append((title,sheet_rows))
        from hosting.pratt_reports import inspect_pratt
        pratt=inspect_pratt(sources,result)
        if pratt is not None:return _finish(_attach_external(_exact_or_adaptive(pratt,sources,pristine,raw),notes),keep_records)
        from hosting.branded_reports import inspect_branded
        branded=inspect_branded(sources,result)
        if branded is not None:return _finish(_attach_external(_exact_or_adaptive(branded,sources,pristine,raw),notes),keep_records)
    total_cells = total_rows = 0
    try:
        for title, rows in sources:
            headers = []
            numeric = {}
            sums = {}
            formula_count = row_count = width = 0
            total_seen = False
            for row_count, original in enumerate(rows, 1):
                row = list(original)
                total_rows += 1
                total_cells += len(row)
                if row_count > MAX_ROWS or total_rows > MAX_ROWS or len(row) > MAX_COLS or total_cells > MAX_CELLS:
                    raise SpreadsheetError('Spreadsheet exceeds the cell, row, or column limit.')
                if any(isinstance(v, str) and len(v) > MAX_FIELD for v in row):
                    raise SpreadsheetError('Spreadsheet field exceeds the length limit.')
                for index, value in enumerate(row):
                    if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@')):
                        # Negative numeric literals are safe data, not CSV formula payloads.
                        if value.lstrip().startswith('-') and _number(value) is not None:
                            continue
                        formula_count += 1
                        finding('formula_not_evaluated' if fmt == 'xlsx' else 'csv_formula_injection_risk', title, row_count, index + 1)
                if row_count == 1:
                    width = len(row)
                    headers = [str(v).strip().casefold() if v is not None else '' for v in row]
                    for i, h in enumerate(headers):
                        if not h:
                            finding('blank_header', title, 1, i + 1)
                        if isinstance(row[i], str) and row[i] != row[i].strip():
                            finding('header_whitespace', title, 1, i + 1)
                        if h and headers.count(h) > 1:
                            finding('duplicate_header', title, 1, i + 1)
                    numeric = {h: i for i, h in enumerate(headers) if headers.count(h) == 1 and h in ('inspected', 'accepted', 'rejected', 'quantity')}
                    sums = {h: Decimal(0) for h in numeric}
                    continue
                if len(row) != width:
                    finding('inconsistent_row_width', title, row_count)
                for i in range(max(width, len(row))):
                    if i >= len(row) or row[i] is None or (isinstance(row[i], str) and not row[i].strip()):
                        finding('missing_cell', title, row_count, i + 1)
                values = {h: _number(row[i]) if i < len(row) else None for h, i in numeric.items()}
                # A single final Total row is compared only for exact conventional numeric headers.
                is_total = bool(row and isinstance(row[0], str) and row[0].strip().casefold() in ('total', 'grand total') and 0 not in numeric.values())
                if total_seen:
                    finding('rows_after_total_not_reconciled', title, row_count)
                for h, value in values.items():
                    if value is None:
                        finding('invalid_numeric_value', title, row_count, numeric[h] + 1)
                        sums[h] = None
                    else:
                        if value < 0:
                            finding('negative_count_or_quantity', title, row_count, numeric[h] + 1)
                        if h != 'quantity' and value != value.to_integral_value():
                            finding('fractional_count', title, row_count, numeric[h] + 1)
                        if is_total:
                            if not total_seen and sums[h] is not None and value != sums[h]:
                                finding('total_mismatch', title, row_count, numeric[h] + 1)
                        elif sums[h] is not None:
                            sums[h] = _add(sums[h], value)
                if all(h in values and values[h] is not None for h in ('inspected', 'accepted', 'rejected')):
                    if values['inspected'] != _add(values['accepted'], values['rejected']):
                        finding('inspected_count_mismatch', title, row_count)
                total_seen = total_seen or is_total
            if not row_count:
                finding('empty_sheet', title)
            elif row_count == 1:
                finding('no_data_rows', title)
            if row_count and not numeric: finding('no_quantity_mapping', title, None)
            result['sheets'].append(dict(name=title, row_count=row_count, data_row_count=max(0, row_count - 1),
                                         header_column_count=width, formula_or_injection_count=formula_count,
                                         numeric_columns_checked=list(numeric)))
    except csv.Error as exc:
        raise SpreadsheetError('Malformed CSV or field exceeds the parser limit.') from exc
    if not result['sheets']:
        raise SpreadsheetError('Workbook contains no worksheets.')
    result.update(row_count=total_rows, cell_count=total_cells, finding_counts=dict(counts),
                  numeric_columns_checked=sum(len(s['numeric_columns_checked']) for s in result['sheets']),
                  internal_check_status='unverified' if not any(s['numeric_columns_checked'] for s in result['sheets']) else ('findings' if counts else 'no_findings_in_checks_performed'))
    result['record_count'] = sum(s['data_row_count'] for s in result['sheets'] if s['numeric_columns_checked'])
    if result['internal_check_status'] == 'unverified' or not result['record_count']:
        # No first-row quantity headers (or no rows under them): look for the tables anywhere, in any layout.
        from hosting.adaptive_reports import inspect_adaptive
        rows = sources if fmt == 'xlsx' else [('CSV', list(csv.reader(io.StringIO(decoded, newline=''), strict=True)))]
        adaptive = inspect_adaptive(rows, pristine, cached=_cached_rows(raw) if fmt == 'xlsx' else None, hidden=_hidden(raw) if fmt == 'xlsx' else None)
        if adaptive is not None:
            ident = _good_identity(adaptive.get('report_identity'), rows)
            if ident:
                adaptive['report_identity'] = ident
            return _finish(_attach_external(adaptive, notes), keep_records)
    if fmt == 'xlsx':
        ident = _good_identity(result.get('report_identity'), sources)
        if ident:
            result['report_identity'] = ident
    return _attach_external(result, notes)


def report_records(filename, raw):
    """Detail rows of any report layout (automatic column detection), for source comparison."""
    from hosting.adaptive_reports import inspect_adaptive
    if filename.lower().endswith('.xlsx'):
        rows = [(title, list(it)) for title, it in _xlsx_rows(raw, [])]
        cached = _cached_rows(raw)
    else:
        rows = [('CSV', list(csv.reader(io.StringIO(raw.decode('utf-8-sig'), newline=''), strict=True)))]
        cached = None
    result = inspect_adaptive(rows, {'limitations': []}, cached=cached)
    return result['_detail_records'] if result else []


def report_and_export_records(filename, raw):
    """(record rows, the workbook's own SharePoint export rows) for reconciliation against the live lists."""
    from hosting.adaptive_reports import inspect_adaptive
    if filename.lower().endswith('.xlsx'):
        rows = [(title, list(it)) for title, it in _xlsx_rows(raw, [])]
        cached = _cached_rows(raw)
    else:
        rows = [('CSV', list(csv.reader(io.StringIO(raw.decode('utf-8-sig'), newline=''), strict=True)))]
        cached = None
    result = inspect_adaptive(rows, {'limitations': []}, cached=cached)
    return (result['_detail_records'], result.get('_export_records') or []) if result else ([], [])


def _finish(result, keep_records):
    result.pop('_export_records', None)
    if not keep_records:
        result.pop('_detail_records', None)
    return result


def _hidden(raw):
    from hosting.common_issues import hidden_layout
    return hidden_layout(raw)


_MONTHS = ('january', 'february', 'march', 'april', 'may', 'june', 'july', 'august', 'september', 'october', 'november', 'december')
_JOB_IN_TEXT = re.compile(r'\bjob\s*(?:#|no\.?|number)?\s*[:#-]?\s*([A-Z0-9][A-Z0-9-]*\d[A-Z0-9-]*)', re.I)
_MONTH_DATE = re.compile(r'\b(' + '|'.join(m[:3] for m in _MONTHS) + r')[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})\b', re.I)
_ISO_DATE = re.compile(r'(?<!\d)(\d{4})-(\d{2})-(\d{2})(?![\dT])')


def header_identity(sources):
    """Job number, report date and client from the title lines at the top of a report ("SL Alabama · Job 2207",
    "November 6, 2026 · ..."), for layouts that do not keep them in a fixed cell. Formulas are never read as a title."""
    from datetime import date as _date, datetime as _dt
    job = day = client = None
    ordered = sorted(sources, key=lambda s: 0 if str(s[0]).strip().lower() == 'summary' else 1)
    for _title, rows in ordered[:4]:
        for row in rows[:14]:
            cells = list(row or [])
            for ci, v in enumerate(cells):
                if isinstance(v, _dt) and day is None and ci and isinstance(cells[ci - 1], str) and 'date' in cells[ci - 1].lower():
                    day = v.date()
                if not isinstance(v, str) or not v.strip() or v.lstrip().startswith('='):
                    continue
                text = v.strip()
                low = text.lower()
                if low.startswith(('captured', 'generated', 'printed', 'exported', 'created')):
                    continue   # when the file was made, not the day it reports on
                if job is None:
                    m = _JOB_IN_TEXT.search(text)
                    if m and not re.fullmatch(r'(19|20)\d\d', m.group(1)):
                        job = m.group(1).upper()
                        before = text[:m.start()].strip(' ·|-–—:,')
                        if before and len(before) <= 60:
                            client = before
                    elif low.rstrip(': ') in ('project number', 'project no', 'job number', 'job', 'job no') and ci + 1 < len(cells):
                        nxt = cells[ci + 1]
                        if isinstance(nxt, (str, int)) and str(nxt).strip() and not str(nxt).lstrip().startswith('='):
                            job = str(nxt).strip().upper()
                if day is None:
                    m = _MONTH_DATE.search(text)
                    if m:
                        try:
                            day = _date(int(m.group(3)), [x[:3] for x in _MONTHS].index(m.group(1)[:3].lower()) + 1, int(m.group(2)))
                        except ValueError:
                            pass
                    else:
                        m = _ISO_DATE.search(text)
                        if m:
                            try:
                                day = _date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                            except ValueError:
                                pass
            if job and day:
                break
        if job and day:
            break
    out = {}
    if job:
        out['project_number'] = job
    if day:
        out['report_date'] = day.isoformat()
    if client:
        out['client'] = client
    return out


def _good_identity(identity, sources):
    """The report's identity: a fixed-cell value only when it is real text (never a formula), else the title lines."""
    identity = dict(identity or {})
    pn = identity.get('project_number')
    if not isinstance(pn, str) or not pn.strip() or pn.lstrip().startswith('='):
        identity['project_number'] = None
    found = header_identity(sources) if sources else {}
    for k, v in found.items():
        if not identity.get(k):
            identity[k] = v
    return identity if any(identity.values()) else None


def _exact_or_adaptive(strict, sources, pristine, raw):
    """Keep an exact template match; otherwise use automatic column detection for this layout."""
    if strict.get('template_mapping_status') == 'matched' and strict.get('record_count'):
        return strict
    from datetime import date as _date
    from hosting.adaptive_reports import inspect_adaptive
    identity = _good_identity(strict.get('report_identity'), sources) or {}
    try:
        report_day = _date.fromisoformat(identity['report_date']) if identity.get('report_date') else None
    except ValueError:
        report_day = None
    adaptive = inspect_adaptive(sources, pristine, cached=_cached_rows(raw), report_day=report_day, hidden=_hidden(raw))
    if adaptive is None:
        return strict
    adaptive['recognized_template'] = strict.get('template_id')
    # Keep what differed from the exact template, as information (not blocking).
    adaptive['template_differences'] = dict(strict.get('finding_counts') or {})
    if identity:
        adaptive['report_identity'] = identity
    adaptive['findings'].insert(0, dict(code='known_template_layout_differs', sheet='Workbook', row=None, column=None,
        message='This looks like a known PPG/Pratt report, but its layout differs from the exact template. Columns were detected automatically instead.'))
    adaptive['finding_counts']['known_template_layout_differs'] = 1
    adaptive['finding_count'] = adaptive.get('finding_count', 0) + 1
    return adaptive


def _attach_external(result, notes):
    """Add external/active-content findings (one per code and location) to any result shape."""
    seen = set()
    counts = Counter()
    for code, where in notes:
        counts[code] += 1
        if (code, where) in seen:
            continue
        seen.add((code, where))
        if len(result.setdefault('findings', [])) < MAX_FINDINGS:
            result['findings'].append(dict(code=code, sheet=where, row=None, column=None))
        else:
            result['findings_truncated'] = True
    if counts:
        merged = dict(result.get('finding_counts') or {})
        for code, n in counts.items():
            merged[code] = merged.get(code, 0) + n
        result['finding_counts'] = merged
        result['external_content'] = sorted(counts)
        result.setdefault('limitations', []).append('External links, web functions and DDE/macro-call formulas were recorded as findings. They were never executed or followed.')
        # Plain hyperlinks (e.g. photo links) are listed for information only; links that pull in
        # outside values or can run something make the result need review.
        if result.get('internal_check_status') == 'no_findings_in_checks_performed' and set(counts) - {'hyperlink_formula', 'external_hyperlink'}:
            result['internal_check_status'] = 'findings'
    return result
