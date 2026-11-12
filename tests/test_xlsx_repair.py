import io
import re
import zipfile

from openpyxl import Workbook, load_workbook
from openpyxl.worksheet.table import Table

from hosting.xlsx_repair import excel_safe


def _book(headers, rename=None):
    wb = Workbook(); ws = wb.active; ws.title = 'Inspection Detail'
    ws.append(['Title']); ws.append([]); ws.append(headers); ws.append(['a', 1, 'x']); ws.append(['b', 2, 'y'])
    ws.add_table(Table(displayName='InspectionData', ref='A3:C5'))
    buf = io.BytesIO(); wb.save(buf)
    if not rename:
        return buf.getvalue()
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(buf.getvalue())) as src, zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            data = src.read(info.filename)
            if info.filename.startswith('xl/tables/'):
                text = data.decode()
                for old, new in rename.items():
                    text = text.replace(f'name="{old}"', f'name="{new}"')
                data = text.encode()
            dst.writestr(info, data)
    return out.getvalue()


def _names(raw):
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        part = next(n for n in z.namelist() if n.startswith('xl/tables/'))
        return re.findall(r'<tableColumn\b[^>]*\bname="([^"]*)"', z.read(part).decode())


def test_a_consistent_workbook_is_returned_byte_for_byte():
    raw = _book(['Part', 'Quantity', 'Note'])
    assert excel_safe('r.xlsx', raw) is raw
    assert excel_safe('r.csv', b'a,b') == b'a,b'
    assert excel_safe('r.xlsx', b'not a zip') == b'not a zip'


def test_table_columns_take_the_header_cell_text_and_cells_are_untouched():
    raw = _book(['Part & name', 'Quantity\nOK', 'Label photo'], rename={'Part &amp; name': 'Part', 'Label photo': 'Preview'})
    assert _names(raw)[0] == 'Part'
    fixed = excel_safe('r.xlsx', raw)
    assert _names(fixed) == ['Part &amp; name', 'Quantity&#10;OK', 'Label photo']
    assert excel_safe('r.xlsx', fixed) == fixed
    before, after = load_workbook(io.BytesIO(raw)), load_workbook(io.BytesIO(fixed))
    assert [[c.value for c in row] for row in after.active] == [[c.value for c in row] for row in before.active]
    assert list(after.active.tables) == ['InspectionData']
    with zipfile.ZipFile(io.BytesIO(raw)) as a, zipfile.ZipFile(io.BytesIO(fixed)) as b:
        assert a.namelist() == b.namelist()
        assert all(a.read(n) == b.read(n) for n in a.namelist() if not n.startswith('xl/tables/'))


def test_a_table_that_cannot_match_its_headers_is_left_out_and_the_cells_stay():
    raw = _book(['Part', 7, 'Note'])   # a number is not a header Excel accepts
    fixed = excel_safe('r.xlsx', raw)
    book = load_workbook(io.BytesIO(fixed))
    assert list(book.active.tables) == []
    assert [c.value for c in book.active[3]] == ['Part', 7, 'Note']
    with zipfile.ZipFile(io.BytesIO(fixed)) as z:
        assert not [n for n in z.namelist() if n.startswith('xl/tables/')]
        assert 'tables/table' not in z.read('[Content_Types].xml').decode()
