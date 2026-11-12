"""Make a downloaded workbook open in Excel without the "We found a problem with some content" prompt.

Excel insists that every table column is named exactly like the text in its header cell. Workbooks built from the
SharePoint template carry a table whose column names no longer match the headers on the sheet, so Excel offers to
repair the file each time it is opened. The download gets the table's column names from the header cells that are
really there. No cell value, formula or style is touched, and the stored version is never changed.
"""
import html
import io
import posixpath
import re
import zipfile
from xml.etree import ElementTree as ET

_MAIN = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
_TABLE_REL = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships/table'


def _col_number(letters):
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n


def _col_letters(n):
    out = ''
    while n:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out


def _shared_strings(z, names):
    if 'xl/sharedStrings.xml' not in names:
        return []
    root = ET.fromstring(z.read('xl/sharedStrings.xml'))
    return [''.join(t.text or '' for t in si.iter('{%s}t' % _MAIN)) for si in root.findall('{%s}si' % _MAIN)]


def _cell_text(sheet_xml, address, shared):
    """The text shown in one cell, or None when the cell is empty or does not hold text."""
    m = re.search(rf'<(?:\w+:)?c\b(?=[^>]*\br="{address}")([^>]*?)(?:/>|>(.*?)</(?:\w+:)?c>)', sheet_xml, re.S)
    if not m or not m.group(2):
        return None
    kind = re.search(r'\bt="([^"]*)"', m.group(1))
    kind = kind.group(1) if kind else 'n'
    if kind == 'inlineStr':
        text = ''.join(re.findall(r'<(?:\w+:)?t\b[^>]*>(.*?)</(?:\w+:)?t>', m.group(2), re.S))
        return html.unescape(text) or None
    value = re.search(r'<(?:\w+:)?v\b[^>]*>(.*?)</(?:\w+:)?v>', m.group(2), re.S)
    if not value:
        return None
    if kind == 's':
        index = int(value.group(1))
        return (shared[index] or None) if 0 <= index < len(shared) else None
    if kind == 'str':
        return html.unescape(value.group(1)) or None
    return None   # a number, date or error is not a header Excel accepts


def _attr(text):
    return (text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')
            .replace('\r\n', '\n').replace('\r', '\n').replace('\n', '&#10;').replace('\t', '&#9;'))


def _fixed_table(table_xml, sheet_xml, shared):
    """The table part with column names taken from the header cells, None when nothing needs changing, or False
    when the table cannot be made to agree with the sheet (the caller then leaves the table out)."""
    head = re.search(r'<(?:\w+:)?table\b[^>]*>', table_xml)
    if not head:
        return False
    ref = re.search(r'\bref="([A-Z]+)(\d+):([A-Z]+)(\d+)"', head.group(0))
    rows = re.search(r'\bheaderRowCount="(\d+)"', head.group(0))
    if not ref or (rows and rows.group(1) != '1'):
        return None if ref else False   # a table without a header row has nothing to compare
    first, last, row = _col_number(ref.group(1)), _col_number(ref.group(3)), ref.group(2)
    columns = list(re.finditer(r'(<(?:\w+:)?tableColumn\b[^>]*?\bname=")([^"]*)(")', table_xml))
    if len(columns) != last - first + 1:
        return False
    wanted, seen = [], set()
    for index in range(first, last + 1):
        text = _cell_text(sheet_xml, f'{_col_letters(index)}{row}', shared)
        if text is None or text.casefold() in seen:
            return False
        seen.add(text.casefold())
        wanted.append(text)
    if [html.unescape(c.group(2)) for c in columns] == wanted:
        return None
    out, at = [], 0
    for column, text in zip(columns, wanted):
        out.append(table_xml[at:column.start()] + column.group(1) + _attr(text) + column.group(3))
        at = column.end()
    return ''.join(out) + table_xml[at:]


def excel_safe(filename, raw):
    """``raw`` with every table agreeing with its sheet. Anything unexpected returns the bytes exactly as stored."""
    if not str(filename).lower().endswith('.xlsx'):
        return raw
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            names = set(z.namelist())
            shared, changed, dropped = None, {}, set()
            for sheet in sorted(n for n in names if re.fullmatch(r'xl/worksheets/[^/]+\.xml', n)):
                rels_path = posixpath.join(posixpath.dirname(sheet), '_rels', posixpath.basename(sheet) + '.rels')
                if rels_path not in names:
                    continue
                rels = z.read(rels_path).decode('utf-8-sig')
                sheet_xml = None
                for rel in re.finditer(r'<(?:\w+:)?Relationship\b[^>]*/>', rels):
                    tag = rel.group(0)
                    if f'Type="{_TABLE_REL}"' not in tag:
                        continue
                    target = re.search(r'\bTarget="([^"]*)"', tag)
                    rid = re.search(r'\bId="([^"]*)"', tag)
                    if not target or not rid:
                        continue
                    part = target.group(1).lstrip('/') if target.group(1).startswith('/') else posixpath.normpath(posixpath.join(posixpath.dirname(sheet), target.group(1)))
                    if part not in names:
                        continue
                    if sheet_xml is None:
                        sheet_xml = z.read(sheet).decode('utf-8-sig')
                    if shared is None:
                        shared = _shared_strings(z, names)
                    fixed = _fixed_table(z.read(part).decode('utf-8-sig'), changed.get(sheet, sheet_xml), shared)
                    if fixed is None:
                        continue
                    if fixed is not False:
                        changed[part] = fixed
                        continue
                    # The table cannot agree with the sheet: the cells keep their own look, only the table wrapper goes.
                    current = changed.get(sheet, sheet_xml)
                    current = re.sub(rf'<(?:\w+:)?tablePart\b[^>]*"{re.escape(rid.group(1))}"[^>]*/>', '', current)
                    left = len(re.findall(r'<(?:\w+:)?tablePart\b', current))
                    current = (re.sub(r'(<(?:\w+:)?tableParts\b[^>]*\bcount=")\d+(")', rf'\g<1>{left}\2', current) if left
                               else re.sub(r'<(?:\w+:)?tableParts\b[^>]*>\s*</(?:\w+:)?tableParts>|<(?:\w+:)?tableParts\b[^>]*/>', '', current))
                    changed[sheet] = current
                    changed[rels_path] = changed.get(rels_path, rels).replace(tag, '')
                    dropped.add(part)
            if not changed:
                return raw
            if dropped:
                types = z.read('[Content_Types].xml').decode('utf-8-sig')
                for part in dropped:
                    types = re.sub(rf'<Override\b[^>]*PartName="/{re.escape(part)}"[^>]*/>', '', types)
                changed['[Content_Types].xml'] = types
            out = io.BytesIO()
            with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as dst:
                for info in z.infolist():
                    if info.filename in dropped:
                        continue
                    dst.writestr(info, changed[info.filename].encode('utf-8') if info.filename in changed else z.read(info.filename))
            return out.getvalue()
    except Exception:
        return raw
