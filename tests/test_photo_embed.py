"""Download with SharePoint photos: pictures embedded in a copy; the original's parts are untouched."""
import io
import json
import unittest
import zipfile
from unittest.mock import patch

from openpyxl import load_workbook

from hosting import photo_embed
from hosting.spreadsheets import inspect_spreadsheet
from tests.sl_daily_fixture import PNG, sl_daily


def fake_sharepoint(photo_for=('432', '434'), fail=None):
    def get(url, opener=None, raw=False):
        if fail:
            return fail
        if '/fields?' in url:
            return 200, {'value': [{'InternalName': 'PictureLabel', 'Title': 'Picture Label'}, {'InternalName': 'PictureDefect', 'Title': 'Picture Defect'}]}
        if '/AttachmentFiles(' in url:
            return (404, 'gone') if 'missing' in url else (200, PNG)
        item = url.split('/items(')[1].split(')')[0]
        if item not in photo_for:
            return 200, {'Id': int(item)}
        return 200, {'Id': int(item), 'PictureLabel': json.dumps({'fileName': f'label_{item}.png'}),
                     'PictureDefect': json.dumps({'fileName': 'missing.png'}) if item == '434' else None}
    return get


class WithPhotos(unittest.TestCase):
    def test_photos_are_embedded_in_their_rows_and_the_original_parts_are_kept(self):
        raw = sl_daily(photos=False)
        with patch('hosting.photo_embed._get', fake_sharepoint()):
            name, data, info = photo_embed.build('SL Alabama - 2214 - 2026-11-06.xlsx', raw, 'SLAlabama_AlexanderCity', 'list-1')
        self.assertEqual(name, 'SL Alabama - 2214 - 2026-11-06 (with photos).xlsx')
        self.assertEqual((info['with_photos'], info['embedded'], info['missing'], info['tab']), (2, 2, 1, 'Defect Photos'))
        wb = load_workbook(io.BytesIO(data))
        tab = wb['Defect Photos']
        self.assertEqual(len(tab._images), 2)
        self.assertEqual([tab['A3'].value, tab['A4'].value], [432, 434])
        self.assertIn('Photo not available', tab['F4'].value)
        before, after = zipfile.ZipFile(io.BytesIO(raw)), zipfile.ZipFile(io.BytesIO(data))
        for n in before.namelist():
            if n not in ('xl/workbook.xml', 'xl/_rels/workbook.xml.rels', '[Content_Types].xml'):
                self.assertEqual(before.read(n), after.read(n), n)
        with after.open([n for n in after.namelist() if n.startswith('xl/media/sp_photo')][0]) as f:
            self.assertEqual(f.read(), PNG)   # the inspector's original file, not resized or replaced

    def test_the_copy_still_counts_the_same_production(self):
        with patch('hosting.photo_embed._get', fake_sharepoint()):
            _, data, _ = photo_embed.build('r.xlsx', sl_daily(photos=False), 'S', 'L')
        r = inspect_spreadsheet('r.xlsx', data)
        self.assertEqual((r['record_count'], r['totals']['inspected']['total']), (5, '400'))

    def test_existing_defect_photos_tab_is_kept_and_a_new_tab_is_added(self):
        with patch('hosting.photo_embed._get', fake_sharepoint()):
            _, data, info = photo_embed.build('r.xlsx', sl_daily(photos=True), 'S', 'L')
        self.assertEqual(info['tab'], 'SharePoint Photos')
        self.assertIn('Defect Photos', load_workbook(io.BytesIO(data)).sheetnames)

    def test_refused_app_token_explains_the_admin_step(self):
        with patch('hosting.photo_embed._get', fake_sharepoint(fail=(401, 'Unsupported app only token.'))):
            with self.assertRaises(photo_embed.PhotoError) as caught:
                photo_embed.build('r.xlsx', sl_daily(), 'S', 'L')
        self.assertIn('certificate', str(caught.exception))

    def test_image_size_reader(self):
        self.assertEqual(photo_embed.image_size(PNG), (1, 1))


if __name__ == '__main__':
    unittest.main()
