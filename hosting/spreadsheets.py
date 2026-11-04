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

MAX_BYTES = 3 * 1024 * 1024
MAX_ROWS = 10000
MAX_COLS = 100
MAX_CELLS = 100000
MAX_UNCOMPRESSED = 20 * 1024 * 1024
MAX_MEMBERS = 1000
MAX_RATIO = 200
MAX_FIELD = 32767
MAX_FINDINGS = 200
CHECKER_VERSION = '2026-11-04-templates-v3'


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


def _xlsx_rows(raw):
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
                if (any(token in low for token in ('vbaproject', 'macrosheet', 'externallink',
                        'embeddings/', 'activex/', 'connections', 'querytables/', 'customui/'))
                        or low.endswith(('.bin', '.exe', '.vbs', '.js'))):
                    raise SpreadsheetError('Macros, active content, and external data are not supported.')
            if not {'[Content_Types].xml', 'xl/workbook.xml'}.issubset(names):
                raise SpreadsheetError('File is not a supported XLSX workbook.')
            cells = 0
            for member in members:
                if not member.filename.lower().endswith(('.xml', '.rels')):
                    continue
                document = ET.fromstring(archive.read(member), forbid_dtd=True)
                for element in document.iter():
                    tag = element.tag.rsplit('}', 1)[-1]
                    if tag == 'Override' and any(t in element.attrib.get('ContentType', '').lower()
                                                 for t in ('macroenabled', 'macrosheet', 'activex')):
                        raise SpreadsheetError('Macros or active content are not supported.')
                    if tag == 'Relationship' and element.attrib.get('TargetMode', '').lower() == 'external':
                        raise SpreadsheetError('External links are not supported.')
                    if tag in ('ddeLink', 'oleLink', 'oleObject', 'control'):
                        raise SpreadsheetError('Active content is not supported.')
                    if member.filename.startswith('xl/worksheets/'):
                        if tag == 'row' and int(element.attrib.get('r', '1')) > MAX_ROWS:
                            raise SpreadsheetError('Spreadsheet exceeds the row limit.')
                        if tag == 'c':
                            cells += 1
                            coordinate = re.fullmatch(r'([A-Z]+)([1-9][0-9]*)', element.attrib.get('r', ''))
                            if not coordinate or len(coordinate[1]) > 3 or len(coordinate[2]) > 5:
                                raise SpreadsheetError('Invalid spreadsheet cell coordinate.')
                            col = 0
                            for letter in coordinate[1]:
                                col = col * 26 + ord(letter) - 64
                            if col > MAX_COLS or int(coordinate[2]) > MAX_ROWS or cells > MAX_CELLS:
                                raise SpreadsheetError('Spreadsheet exceeds the cell, row, or column limit.')
                    if tag in ('f', 'definedName'):
                        formula = element.text or ''
                        if re.search(r'\[|\||https?://|(?:WEBSERVICE|HYPERLINK|IMAGE|RTD|DDE|CALL|REGISTER(?:\.ID)?|EXEC(?:UTE)?|RUN|SQL\.REQUEST)\s*\(', formula, re.I):
                            raise SpreadsheetError('External or active formulas are not supported.')
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
        raise SpreadsheetError('Malformed or unsupported XLSX workbook.') from exc


def inspect_spreadsheet(filename: str, raw: bytes) -> dict:
    """Return internal checks only; rejects unsafe input with SpreadsheetError.

    A successful parse is not source authentication, external reconciliation, or
    approval for release. First row is treated as headers; no schema is guessed.
    """
    if not isinstance(raw, bytes) or not raw:
        raise SpreadsheetError('Spreadsheet is empty.')
    if len(raw) > MAX_BYTES:
        raise SpreadsheetError('Spreadsheet exceeds the 3 MiB upload limit.')
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

    def finding(code, sheet, row=None, column=None):
        counts[code] += 1
        if len(result['findings']) < MAX_FINDINGS:
            result['findings'].append(dict(code=code, sheet=sheet, row=row, column=column))
        else:
            result['findings_truncated'] = True

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
        for title, iterator in _xlsx_rows(raw):
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
        if pratt is not None:return pratt
        from hosting.branded_reports import inspect_branded
        branded=inspect_branded(sources,result)
        if branded is not None:return branded
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
    return result
