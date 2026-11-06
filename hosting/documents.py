"""PDF and Word documents: read their tables (and text) into a workbook so the normal report checks can run on them.

The workbook is built the same way every time for the same document (fixed timestamps), so uploading the same
PDF twice is recognised as the same file. Numbers and dates become real numbers and dates, because a PDF has no
cell types and "stored as text" warnings would only be noise from the conversion.
"""
import io
import re
import zipfile
from datetime import datetime

DOC_TYPES = {'.pdf': 'PDF', '.docx': 'Word'}
MAX_PAGES = 200
MAX_TEXT = 20000


class DocumentError(ValueError):
    pass


def kind(filename):
    m = re.search(r'\.[A-Za-z0-9]+$', filename or '')
    return DOC_TYPES.get(m.group(0).lower()) if m else None


def _clean(v):
    return re.sub(r'[ \t\r\f\v]+', ' ', str(v if v is not None else '')).replace('\n', ' ').strip()


NUM = re.compile(r'^-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?$')
DATE_FORMATS = ('%Y-%m-%d', '%m/%d/%Y', '%m/%d/%y', '%m-%d-%Y', '%m.%d.%Y')


def _value(text):
    """'34' -> 34, '1,250.5' -> 1250.5, '10/09/2026' -> date; IDs with leading zeros and everything else stay text."""
    t = text.strip()
    if not t:
        return None
    if NUM.match(t) and not (len(t) > 1 and t[0] == '0' and t[1] != '.'):
        n = float(t.replace(',', ''))
        return int(n) if n.is_integer() and '.' not in t and abs(n) < 1e15 else n
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(t, fmt).date()
        except ValueError:
            pass
    return t


def _tidy(rows):
    """Drop empty rows and columns; pad to a rectangle."""
    rows = [[_clean(c) for c in r] for r in rows if r and any(_clean(c) for c in r)]
    if not rows:
        return []
    width = max(len(r) for r in rows)
    rows = [r + [''] * (width - len(r)) for r in rows]
    keep = [j for j in range(width) if any(r[j] for r in rows)]
    return [[r[j] for j in keep] for r in rows]


def _merge(tables):
    """A table that runs onto the next page (same columns, header repeated or not) becomes one table."""
    out = []
    for t in tables:
        if out and len(out[-1][0]) == len(t[0]):
            prev = out[-1]
            body = t[1:] if [c.lower() for c in t[0]] == [c.lower() for c in prev[0]] else t
            if t[0] == prev[0] or _looks_like_data(t[0]):
                prev.extend(body)
                continue
        out.append([list(r) for r in t])
    return out


def _looks_like_data(row):
    vals = [c for c in row if c]
    return bool(vals) and sum(1 for c in vals if NUM.match(c)) >= max(1, len(vals) // 2)


def read_pdf(raw):
    import pdfplumber
    tables, text, pages = [], [], 0
    try:
        pdf = pdfplumber.open(io.BytesIO(raw))
    except Exception as exc:
        raise DocumentError('This PDF could not be opened. It may be damaged or password-protected. Save a new copy and upload it again.') from exc
    with pdf:
        pages = len(pdf.pages)
        if pages > MAX_PAGES:
            raise DocumentError(f'This PDF has {pages} pages. Upload one with {MAX_PAGES} pages or fewer.')
        for page in pdf.pages:
            found = [t for t in (page.extract_tables() or []) if t and len(t) > 1]
            if not found:   # tables without lines: line the columns up by the text instead
                found = [t for t in (page.extract_tables({'vertical_strategy': 'text', 'horizontal_strategy': 'text'}) or [])
                         if t and len(t) > 2 and max(len(r) for r in t) > 2]
            tables += [x for x in (_tidy(t) for t in found) if len(x) > 1 and len(x[0]) > 1]
            text.append(page.extract_text() or '')
    return tables, '\n'.join(text).strip(), pages


def read_docx(raw):
    try:
        import docx
        d = docx.Document(io.BytesIO(raw))
    except Exception as exc:
        raise DocumentError('This Word file could not be opened. It may be damaged, or an older .doc file. In Word choose File, Save As, Word Document (.docx), then upload that copy.') from exc
    tables = []
    for t in d.tables:
        rows = []
        for r in t.rows:
            cells, seen = [], set()
            for c in r.cells:   # merged cells repeat; keep each once
                cells.append('' if id(c._tc) in seen else c.text)
                seen.add(id(c._tc))
            rows.append(cells)
        x = _tidy(rows)
        if len(x) > 1 and len(x[0]) > 1:
            tables.append(x)
    text = '\n'.join(p.text for p in d.paragraphs if p.text.strip())
    return tables, text, None


def _stable(xlsx):
    """Same document, same bytes: fixed zip dates and document timestamps."""
    src = zipfile.ZipFile(io.BytesIO(xlsx))
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
        for info in src.infolist():
            data = src.read(info.filename)
            if info.filename == 'docProps/core.xml':
                data = re.sub(rb'(<dcterms:(?:created|modified)[^>]*>)[^<]*', rb'\g<1>2000-01-01T00:00:00Z', data)
            z.writestr(zipfile.ZipInfo(info.filename, date_time=(1980, 1, 1, 0, 0, 0)), data, zipfile.ZIP_DEFLATED)
    return out.getvalue()


def to_workbook(filename, raw):
    """(xlsx bytes, new filename, info) for a PDF or Word document."""
    k = kind(filename)
    if not k:
        raise DocumentError('Upload a PDF (.pdf) or Word (.docx) file.')
    if not raw:
        raise DocumentError(f'{filename} is empty.')
    tables, text, pages = read_pdf(raw) if k == 'PDF' else read_docx(raw)
    tables = _merge(tables)
    from openpyxl import Workbook
    wb = Workbook()
    wb.remove(wb.active)
    for n, t in enumerate(tables, 1):
        ws = wb.create_sheet(f'Table {n}' if len(tables) > 1 else 'Table')
        for r, row in enumerate(t):
            ws.append([c or None for c in row] if r == 0 else [_value(c) for c in row])
    if not tables:   # no tables: the text, one line per row, so the report still opens and can be read
        ws = wb.create_sheet('Text')
        ws.append(['Text'])
        for line in [l for l in text.splitlines() if l.strip()][:5000]:
            ws.append([line.strip()])
    wb.properties.creator = 'PPG Verifier'
    buf = io.BytesIO()
    wb.save(buf)
    stem = re.sub(r'\.[A-Za-z0-9]+$', '', filename)
    info = dict(type=k, filename=filename, pages=pages, tables=len(tables), rows=sum(len(t) - 1 for t in tables),
                text=text[:MAX_TEXT], text_truncated=len(text) > MAX_TEXT)
    return _stable(buf.getvalue()), f'{stem} (from {k}).xlsx', info
