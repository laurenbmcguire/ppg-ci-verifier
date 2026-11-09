"""Synthetic OOXML registration regressions; no network or customer data."""
import io
import posixpath
import zipfile
from xml.etree import ElementTree as ET
from unittest.mock import patch

import pytest
from openpyxl import Workbook, load_workbook
from openpyxl.drawing.image import Image

from hosting import photo_embed
from tests.sl_daily_fixture import PNG

MAIN = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
REL = 'http://schemas.openxmlformats.org/package/2006/relationships'
DOCREL = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
CT = 'http://schemas.openxmlformats.org/package/2006/content-types'
REGISTRY = {'xl/workbook.xml', 'xl/_rels/workbook.xml.rels', '[Content_Types].xml'}


def original(prefixed):
    wb = Workbook()
    ws = wb.active
    ws.title = 'Production & totals'
    ws['A1'] = 'Synthetic only'
    ws['B2'] = '=SUM(1,2)'
    ws.merge_cells('D4:E4')
    ws.add_image(Image(io.BytesIO(PNG)), 'G7')
    for name in ('Defect Photos', 'SharePoint Photos', 'SharePoint Photos 2'):
        wb.create_sheet(name)['A1'] = 'Existing'
    stream = io.BytesIO()
    wb.save(stream)
    raw = stream.getvalue()
    if not prefixed:
        return raw
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw)) as src, zipfile.ZipFile(out, 'w') as dst:
        for name in src.namelist():
            data = src.read(name)
            if name in REGISTRY:
                tree = ET.fromstring(data)
                if name == 'xl/workbook.xml':
                    # Non-contiguous IDs catch accidental duplicates/resetting to one.
                    for i, sheet in enumerate(tree.find(f'{{{MAIN}}}sheets')):
                        sheet.set('sheetId', str(20 + i))
                data = ET.tostring(tree)
            dst.writestr(name, data)
    return out.getvalue()


def rows():
    return [dict(item='123', identifier='Synthetic', part='Test', defect='Test',
                 photos=[dict(title='Label', name='synthetic.png', bytes=PNG)])]


def check_package(raw, data, title):
    with zipfile.ZipFile(io.BytesIO(raw)) as before, zipfile.ZipFile(io.BytesIO(data)) as after:
        assert len(after.namelist()) == len(set(after.namelist()))
        for name in before.namelist():
            if name not in REGISTRY:
                assert before.read(name) == after.read(name), name
        sheets = ET.fromstring(after.read('xl/workbook.xml')).find(f'{{{MAIN}}}sheets')
        ids = [s.get('sheetId') for s in sheets]
        assert len(ids) == len(set(ids))
        assert sheets[-1].get('name') == title
        assert int(ids[-1]) > max(map(int, ids[:-1]))
        # Every internal relationship must resolve to a real ZIP part, with unique IDs.
        for name in after.namelist():
            if not name.endswith('.rels'):
                continue
            root = ET.fromstring(after.read(name))
            ids = [r.get('Id') for r in root]
            assert len(ids) == len(set(ids))
            base = posixpath.dirname(posixpath.dirname(name))
            for rel in root:
                if rel.get('TargetMode') == 'External':
                    continue
                target = rel.get('Target')
                path = target.lstrip('/') if target.startswith('/') else posixpath.normpath(posixpath.join(base, target))
                assert path in after.namelist(), (name, path)
        rels = ET.fromstring(after.read('xl/_rels/workbook.xml.rels'))
        targets = {r.get('Id'): r.get('Target') for r in rels}
        assert targets[sheets[-1].get(f'{{{DOCREL}}}id')] == 'worksheets/sheet5.xml'
        ct = ET.fromstring(after.read('[Content_Types].xml'))
        overrides = {c.get('PartName'): c.get('ContentType') for c in ct if c.tag.endswith('Override')}
        defaults = {c.get('Extension'): c.get('ContentType') for c in ct if c.tag.endswith('Default')}
        assert overrides['/xl/worksheets/sheet5.xml'].endswith('worksheet+xml')
        assert overrides['/xl/drawings/drawing2.xml'].endswith('drawing+xml')
        assert defaults['png'] == 'image/png'
        assert after.read('xl/media/sp_photo_5_1.png') == PNG
    wb = load_workbook(io.BytesIO(data))
    assert wb.sheetnames == ['Production & totals', 'Defect Photos', 'SharePoint Photos', 'SharePoint Photos 2', title]
    ws = wb.worksheets[0]
    assert ws['A1'].value == 'Synthetic only'
    assert ws['B2'].value == '=SUM(1,2)'
    assert str(ws.merged_cells) == 'D4:E4'
    assert len(ws._images) == 1
    assert (ws._images[0].anchor._from.col, ws._images[0].anchor._from.row) == (6, 6)
    assert len(wb[title]._images) == 1
    assert wb[title]._images[0]._data() == PNG
    assert (wb[title]._images[0].anchor._from.col, wb[title]._images[0].anchor._from.row) == (4, 2)


@pytest.mark.parametrize('prefixed', [False, True])
def test_namespace_safe_registration_and_original_preservation(prefixed):
    raw = original(prefixed)
    check_package(raw, photo_embed.embed(raw, rows()), 'SharePoint Photos 3')


def test_build_summary_reports_actual_unique_tab_without_network():
    raw = original(True)
    record = dict(source={'item': '123', 'site_id': 'site-id', 'list_id': 'list-id'}, labels={'identifier': 'Synthetic', 'part': 'Test', 'defect': 'Test'})
    with patch('hosting.spreadsheets.report_and_export_records', return_value=([record], None)), \
         patch('hosting.photo_embed.fetch_photos', return_value={'123': rows()[0]['photos']}):
        _, data, summary = photo_embed.build('synthetic.xlsx', raw, 'Synthetic', 'list-id', site_id='site-id',
                                             list_url='https://ppgquality.sharepoint.com/sites/Synthetic/Lists/Test')
    assert summary['tab'] == 'SharePoint Photos 3'
    check_package(raw, data, summary['tab'])


def replace_parts(raw, changes):
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw)) as src, zipfile.ZipFile(out, 'w') as dst:
        for name in src.namelist():
            dst.writestr(name, changes.get(name, src.read(name)))
    return out.getvalue()


def test_repeated_export_adds_distinct_sheet_relationship_drawing_and_media():
    raw = original(True)
    first = photo_embed.embed(raw, rows())
    second = photo_embed.embed(first, rows())
    wb = load_workbook(io.BytesIO(second))
    assert wb.sheetnames[-2:] == ['SharePoint Photos 3', 'SharePoint Photos 4']
    assert [len(ws._images) for ws in wb.worksheets] == [1, 0, 0, 0, 1, 1]
    with zipfile.ZipFile(io.BytesIO(first)) as before, zipfile.ZipFile(io.BytesIO(second)) as after:
        assert len(after.namelist()) == len(set(after.namelist()))
        for name in before.namelist():
            if name not in REGISTRY:
                assert before.read(name) == after.read(name)
        sheets = ET.fromstring(after.read('xl/workbook.xml')).find(f'{{{MAIN}}}sheets')
        assert len({s.get('sheetId') for s in sheets}) == len(sheets)
        rels = ET.fromstring(after.read('xl/_rels/workbook.xml.rels'))
        assert len({r.get('Id') for r in rels}) == len(rels)
        assert after.read('xl/media/sp_photo_6_1.png') == PNG


def test_quoted_metadata_and_case_insensitive_sheet_name_collision():
    raw = original(True)
    data_rows = rows()
    data_rows[0]['photos'][0].update(title='Label "A" & B', name='"synthetic" & test.png')
    data = photo_embed.embed(raw, data_rows, sheet_title='DEFECT PHOTOS')
    wb = load_workbook(io.BytesIO(data))
    assert wb.sheetnames[-1] == 'SharePoint Photos 3'
    assert len(wb.worksheets[-1]._images) == 1
    data = photo_embed.embed(raw, data_rows, sheet_title='Test "A" & B')
    assert load_workbook(io.BytesIO(data)).sheetnames[-1] == 'Test "A" & B'


@pytest.mark.parametrize('name,xml', [
    ('xl/workbook.xml', b'<workbook><sheets/></workbook>'),
    ('xl/workbook.xml', f'<workbook xmlns="{MAIN}"/>'.encode()),
    ('xl/workbook.xml', b'<broken'),
    ('xl/_rels/workbook.xml.rels', b'<Relationships/>'),
    ('[Content_Types].xml', b'<Types/>'),
])
def test_unsupported_or_invalid_registry_fails_instead_of_orphan_output(name, xml):
    with pytest.raises(photo_embed.PhotoError):
        photo_embed.embed(replace_parts(original(True), {name: xml}), rows())


def test_namespace_declarations_used_in_attribute_values_survive():
    from xml.dom import minidom
    raw = original(True)
    with zipfile.ZipFile(io.BytesIO(raw)) as src:
        doc = minidom.parseString(src.read('xl/workbook.xml'))
    root = doc.documentElement
    root.setAttribute('xmlns:mc', 'http://schemas.openxmlformats.org/markup-compatibility/2006')
    root.setAttribute('xmlns:x15', 'http://schemas.microsoft.com/office/spreadsheetml/2010/11/main')
    root.setAttribute('mc:Ignorable', 'x15')
    raw = replace_parts(raw, {'xl/workbook.xml': doc.toxml(encoding='utf-8')})
    data = photo_embed.embed(raw, rows())
    with zipfile.ZipFile(io.BytesIO(data)) as result:
        root = minidom.parseString(result.read('xl/workbook.xml')).documentElement
        assert root.getAttribute('mc:Ignorable') == 'x15'
        assert root.getAttribute('xmlns:x15') == 'http://schemas.microsoft.com/office/spreadsheetml/2010/11/main'
    check_package(raw, data, 'SharePoint Photos 3')


def test_export_without_readable_photos_still_registers_visible_error_sheet():
    data = photo_embed.embed(original(True), [dict(item='123', photos=[dict(title='Label', error='Unavailable')])])
    wb = load_workbook(io.BytesIO(data))
    ws = wb['SharePoint Photos 3']
    assert ws['E3'].value == 'Photo not available: Unavailable'
    assert not ws._images


def test_31_synthetic_jpegs_are_reachable_from_the_visible_sheet():
    from PIL import Image as PILImage
    blob = io.BytesIO()
    PILImage.new('RGB', (12, 8), 'blue').save(blob, format='JPEG')
    jpeg = blob.getvalue()
    entries = [dict(item=str(i), photos=[dict(title='Label', name=f'synthetic-{i}.jpg', bytes=jpeg)]) for i in range(31)]
    data = photo_embed.embed(original(True), entries)
    wb = load_workbook(io.BytesIO(data))
    images = wb['SharePoint Photos 3']._images
    assert len(images) == 31
    assert [im.anchor._from.row for im in images] == list(range(2, 33))
    assert all(im._data() == jpeg for im in images)
    with zipfile.ZipFile(io.BytesIO(data)) as result:
        sheet = ET.fromstring(result.read('xl/worksheets/sheet5.xml'))
        drawing_id = sheet.find(f'{{{MAIN}}}drawing').get(f'{{{DOCREL}}}id')
        sheet_rels = ET.fromstring(result.read('xl/worksheets/_rels/sheet5.xml.rels'))
        assert any(r.get('Id') == drawing_id and r.get('Target') == '../drawings/drawing2.xml' for r in sheet_rels)
        draw = ET.fromstring(result.read('xl/drawings/drawing2.xml'))
        embeds = [n.get(f'{{{DOCREL}}}embed') for n in draw.iter('{http://schemas.openxmlformats.org/drawingml/2006/main}blip')]
        rels = ET.fromstring(result.read('xl/drawings/_rels/drawing2.xml.rels'))
        assert set(embeds) == {r.get('Id') for r in rels}
        assert len(embeds) == 31
        for r in rels:
            assert result.read(posixpath.normpath(posixpath.join('xl/drawings', r.get('Target')))) == jpeg
        ct = ET.fromstring(result.read('[Content_Types].xml'))
        jpeg_types = [c for c in ct if c.get('Extension') == 'jpeg']
        assert len(jpeg_types) == 1
        assert jpeg_types[0].get('ContentType') == 'image/jpeg'
