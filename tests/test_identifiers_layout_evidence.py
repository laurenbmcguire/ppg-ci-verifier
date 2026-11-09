"""Identifiers, merged headers and photo evidence (November 8, item 5).

None of these may produce an invented replacement: exact identifiers (leading zeros, free-text part identifiers),
an unusual batch segment, a high auditor lookup ID, a quantity of 29 where 30 is usual, overnight UTC times and
different photo roles. Merged header cells are never written. Pictures and links are shown from the file itself.
"""
import datetime
import hashlib
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from openpyxl import load_workbook
from openpyxl.drawing.image import Image as XLImage

from hosting import photos
from hosting.fixes import FixError, apply_fixes
from hosting.review_summary import summarize
from hosting.spreadsheet_inbox import SpreadsheetInbox
from tests.eos_fixture import add_cached_values
from tests.sl_daily_fixture import PNG, records, sl_daily

PNG_RED = (b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x02\x00\x00\x00\x01\x08\x02\x00\x00\x00{@\xe8\xdd\x00\x00\x00\x0fIDATx\x9cc\xf8\xcf\xc0\x00'
           b'\x00\x03\x01\x01\x00\xc9\xfe\x92\xef\x00\x00\x00\x00IEND\xaeB`\x82')


def save(wb):
    buf = io.BytesIO(); wb.save(buf)
    return add_cached_values(buf.getvalue(), wb.sheetnames, {})


def odd_values_report(day='2026-11-06', ident_overrides=None):
    recs = records(12, inspected=30)
    for i, ident in (ident_overrides or {3: '090726', 4: '1021', 5: 'Sl mex slp', 6: '090726'}).items():
        recs[i]['ident'] = ident
    recs[7]['inspected'] = recs[7]['ok'] = 29                      # 29 where 30 is usual
    wb = load_workbook(io.BytesIO(sl_daily(recs=recs, job='2200', day=day)))
    d = wb['Inspection Detail']
    d['V8'] = 98113                                                # a high auditor lookup ID
    d['W9'] = '2026-11-07T03:12:00Z'                               # an overnight time (still the 6th's third shift)
    d['G10'] = '00356685-B7X-RH'                                   # an unusual batch segment
    d['R11'] = 'Yes'; d['X11'] = 'Label photo'                     # a different photo role on one row
    return save(wb)


class NothingIsInvented(unittest.TestCase):

    def setUp(self):
        self.box = SpreadsheetInbox(tempfile.mkdtemp())
        self.raw = odd_values_report()
        self.item = self.box.ingest('SL Alabama - 2200 - 2026-11-06.xlsx', self.raw)
        self.h = self.item['file_hash']

    def test_no_proposal_changes_any_of_these_values(self):
        props = self.box.fix_proposals(self.h)['proposals']
        touched = {(p['sheet'], p['cell']) for p in props}
        for cell in ('F7', 'F8', 'F9', 'F10', 'J11', 'K11', 'V8', 'W9', 'G10', 'X11'):
            self.assertNotIn(('Inspection Detail', cell), touched, cell)

    def test_identifiers_keep_their_exact_text(self):
        recs = {r['row']: r for r in self.item['records']}
        self.assertEqual((recs[7]['identifier'], recs[8]['identifier'], recs[9]['identifier'], recs[10]['identifier']), ('090726', '1021', 'Sl mex slp', '090726'))
        displayed = self.box.get(self.h)
        v2 = self.box.apply_fixes(self.h, [dict(sheet='Inspection Detail', cell='N4', before='', after='None seen', reason='typed', source='Your edit', kind='text')], expected_check_hash=displayed['check_hash'])
        self.assertEqual(self.box.download(self.h)[1], self.raw)
        ws = load_workbook(io.BytesIO(self.box.download(v2['file_hash'])[1]))['Inspection Detail']
        self.assertEqual((ws['F7'].value, ws['F10'].value), ('090726', '090726'))   # leading zero kept, still text

    def test_a_repeated_identifier_on_distinct_items_is_a_question_not_a_duplicate(self):
        rv = summarize(self.item)
        self.assertFalse([i for i in rv['issues'] if i['code'] in ('duplicate_row', 'duplicate_identifier') and i.get('row') in (7, 10)])
        self.assertNotEqual(self.item['records'][3]['source']['item'], self.item['records'][6]['source']['item'])

    def test_part_identifiers_are_not_vins_and_repeat_across_days_without_billing_alarms(self):
        other = self.box.ingest('SL Alabama - 2200 - 2026-11-05.xlsx', odd_values_report(day='2026-11-05'))
        self.assertEqual(self.box.repeats_elsewhere(self.h), [])
        self.assertEqual(self.box.repeats_elsewhere(other['file_hash']), [])

    def test_unusual_values_are_findings_or_nothing_never_a_fabricated_value(self):
        rv = summarize(self.item)
        fmt = [i for i in rv['issues'] if i['code'] == 'part_format_differs']
        self.assertTrue(fmt)                                        # the batch segment is a question for a person ...
        self.assertTrue(all(i.get('row') == 10 for i in fmt))
        self.assertFalse([p for p in self.box.fix_proposals(self.h)['proposals'] if p['cell'].startswith('G')])


class MergedHeaderAndDate(unittest.TestCase):
    """The issue-log case: the date is in B5; C4 is the blank covered part of a merged header, not a missing date."""

    def setUp(self):
        wb = load_workbook(io.BytesIO(sl_daily()))
        s = wb['Daily Inspection Summary']
        s['A4'] = None
        s['B4'] = 'Report date'; s.merge_cells('B4:D4')
        s['B5'] = datetime.datetime(2026, 11, 6); s['B5'].number_format = 'mmmm d, yyyy'; s.merge_cells('B5:D5')
        self.raw = save(wb)
        self.box = SpreadsheetInbox(tempfile.mkdtemp())
        self.item = self.box.ingest('SL Alabama - 2214 - 2026-11-06.xlsx', self.raw)

    def test_the_date_is_read_from_b5_and_c4_is_not_flagged_or_filled(self):
        self.assertEqual(self.item['report_day'], '2026-11-06')
        self.assertFalse([f for f in self.item['findings'] if f.get('sheet') == 'Daily Inspection Summary' and f.get('row') in (4, 5)])
        props = self.box.fix_proposals(self.item['file_hash'])['proposals']
        self.assertFalse([p for p in props if p['sheet'] == 'Daily Inspection Summary' and p['cell'] in ('B4', 'C4', 'D4', 'B5', 'C5', 'D5')])

    def test_covered_merged_cells_and_labels_are_never_overwritten(self):
        for cell, before, src in (('C4', '', 'Your edit'), ('C5', '', 'Known detail'), ('D4', '', 'Suggested')):
            with self.assertRaises(FixError) as ctx:
                apply_fixes('x.xlsx', self.raw, [dict(sheet='Daily Inspection Summary', cell=cell, before=before, after='2026-11-06', source=src, kind='text')])
            self.assertIn('merged', str(ctx.exception))
        with self.assertRaises(FixError):   # the label itself is not a value to replace
            apply_fixes('x.xlsx', self.raw, [dict(sheet='Daily Inspection Summary', cell='B4', before='Report date', after='2026-11-06', source='Known detail', kind='text')])


def evidence_report():
    wb = load_workbook(io.BytesIO(sl_daily()))
    p = wb['Defect Photos']
    p['A22'] = 'Historical reference 2026-11-04 (broken clip on an earlier day)'
    img = XLImage(io.BytesIO(PNG_RED)); p.add_image(img, 'C22')
    p['B30'] = 'Photo folder'
    p['B30'].hyperlink = 'https://ppg.sharepoint.com/sites/SLAlabama_AlexanderCity/Shared%20Documents/2214'
    p['B31'] = 'Run me'
    p['B31'].hyperlink = 'javascript:alert(1)'
    d = wb['Inspection Detail']
    d['Q4'].hyperlink = 'https://ppg.sharepoint.com/sites/SLAlabama_AlexanderCity/label-432.jpg'
    return save(wb)


class PhotosAndLinks(unittest.TestCase):

    def setUp(self):
        self.box = SpreadsheetInbox(tempfile.mkdtemp())
        self.raw = evidence_report()
        self.h = self.box.ingest('SL Alabama - 2214 - 2026-11-06.xlsx', self.raw)['file_hash']

    def test_the_sheet_view_lists_each_picture_with_its_record_and_purpose(self):
        g = self.box.sheet_grid(self.h, 'Defect Photos')
        self.assertEqual(g['image_summary'], dict(placements=2, unique=2, missing=0))
        first, ref = sorted(g['images'], key=lambda x: x['row'])
        self.assertEqual((first['cell'], first['item'], first['historical']), ('C4', '432', False))
        self.assertEqual((ref['cell'], ref['historical']), ('C22', True))
        self.assertIn('cannot show the outcome on 2026-11-06', ref['note'])

    def test_links_are_clickable_only_when_safe(self):
        g = self.box.sheet_grid(self.h, 'Defect Photos')
        self.assertEqual(g['links']['B30'], dict(url='https://ppg.sharepoint.com/sites/SLAlabama_AlexanderCity/Shared%20Documents/2214', safe=True, kind='hyperlink'))
        self.assertFalse(g['links']['B31']['safe'])
        self.assertTrue(self.box.sheet_grid(self.h, 'Inspection Detail')['links']['Q4']['safe'])

    def test_picture_bytes_are_the_originals_and_nothing_else_can_be_read(self):
        g = self.box.sheet_grid(self.h, 'Defect Photos')
        for p in g['images']:
            data = photos.media_bytes(self.raw, p['media'])
            self.assertEqual(hashlib.sha256(data).hexdigest(), p['sha256'])
        self.assertIn(PNG_RED, [photos.media_bytes(self.raw, p['media']) for p in g['images']])
        for bad in ('xl/workbook.xml', 'xl/media/../workbook.xml', '../../etc/passwd', 'xl/media/nope.png'):
            self.assertIsNone(photos.media_bytes(self.raw, bad), bad)
        self.assertEqual(self.box.download(self.h)[1], self.raw)


class MediaOverHttp(unittest.TestCase):

    def setUp(self):
        from api.index import handler
        sys.path.insert(0, os.path.dirname(__file__))
        from legacy_storage_fake import PostgresWorkspace
        from test_vercel import FakeDatabase
        self.env = patch.dict(os.environ, dict(CI_LOCAL_DEV='1', VERCEL='0')); self.env.start()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        self.server.workspace_store = PostgresWorkspace('postgresql://test.invalid/x', FakeDatabase().connect)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        self.raw = evidence_report()
        with self.server.workspace_store.session() as directory:
            self.h = SpreadsheetInbox(directory).ingest('SL Alabama - 2214 - 2026-11-06.xlsx', self.raw)['file_hash']

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.env.stop()

    def get(self, path):
        try:
            with urllib.request.urlopen(urllib.request.Request(self.base + path, headers={'X-Review-Token': 'same-origin'}), timeout=30) as r:
                return r.status, r.headers.get('Content-Type'), r.read()
        except urllib.error.HTTPError as e:
            return e.code, None, e.read()

    def test_the_picture_endpoint_serves_only_drawing_media(self):
        st, kind, body = self.get(f'/api/spreadsheets/media?id={self.h}&media=xl/media/image1.png')
        self.assertEqual((st, kind), (200, 'image/png'))
        self.assertTrue(body.startswith(b'\x89PNG'))
        self.assertEqual(self.get(f'/api/spreadsheets/media?id={self.h}&media=xl/workbook.xml')[0], 404)


if __name__ == '__main__':
    unittest.main()
