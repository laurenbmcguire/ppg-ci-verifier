"""A correction from review to export (November 8, item 3).

review (SharePoint-backed proposal) -> select -> apply into a new version -> the new version is checked and
reconciled again -> export is opened and compared cell by cell with the original -> change summary PDF.
A SharePoint read made after the review makes the reviewed correction stale; a change set is applied whole or not
at all; the original version never changes.
"""
import hashlib
import io
import os
import shutil
import subprocess
import tempfile
import unittest

from openpyxl import load_workbook

from tests.review_request_fixture import apply_displayed, approve_displayed
from hosting.fixes import FixError
from hosting.review_summary import summarize
from hosting.spreadsheet_inbox import SpreadsheetInbox
from tests.sl_daily_fixture import sl_daily
from tests.test_reconcile import ALL, snap, sp_item

NAME = 'SL Alabama - 2214 - 2026-11-06.xlsx'
DAY = '2026-11-06'


def reviewed(inspected=72):
    """A report whose item 433 says 80 inspected / 80 OK while SharePoint says 72 / 72, reconciled and reviewed."""
    root = tempfile.mkdtemp()
    box = SpreadsheetInbox(root)
    raw = sl_daily(nok_blank=False)
    h = box.ingest(NAME, raw)['file_hash']
    items = list(ALL); items[1] = sp_item(433, inspected=inspected)
    box.source_reconcile(h, snap([items]), DAY, expected_check_hash=box.get(h)['check_hash'])
    props = [p for p in box.fix_proposals(h)['proposals'] if p['source'] == 'SharePoint']
    return box, h, raw, props


def cells(raw):
    wb = load_workbook(io.BytesIO(raw))
    out = {}
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for c in row:
                if c.value is not None:
                    out[(ws.title, c.coordinate)] = c.value
    return wb, out


def chosen(props):
    return [{k: p.get(k) for k in ('id', 'sheet', 'cell', 'before', 'after', 'reason', 'source', 'kind', 'snapshot_id', 'item', 'field')} for p in props]


class AcceptedCorrectionEndToEnd(unittest.TestCase):

    def test_review_preview_apply_recheck_export(self):
        box, h, raw, props = reviewed()
        # review: the proposal shows the report value, the SharePoint value and where it came from, separately
        self.assertEqual(sorted((p['cell'], p['before'], p['after'], p['item'], p['field']) for p in props),
                         [('J5', '80', '72', '433', 'inspected'), ('K5', '80', '72', '433', 'certified')])
        snap_a = summarize(box.get(h))['sharepoint']['snapshot_id']
        self.assertTrue(all(p['snapshot_id'] == snap_a for p in props))
        # preview is the exact list sent to apply (the dialog shows these rows); apply makes v2
        new = apply_displayed(box, h, chosen(props))
        v2 = new['file_hash']
        self.assertNotEqual(v2, h)
        # the original version is unchanged, byte for byte, and records what was done to it
        self.assertEqual(box.download(h)[1], raw)
        self.assertEqual(hashlib.sha256(raw).hexdigest(), h)
        parent = box.get(h)
        self.assertEqual([(c['cell'], c['before'], c['after'], c['snapshot_id']) for c in parent['fixes_applied'][-1]['changes']],
                         [('J5', '80', '72', snap_a), ('K5', '80', '72', snap_a)])
        # revalidation: v2 is checked again on its own and reconciled again with the same SharePoint data
        items = list(ALL); items[1] = sp_item(433, inspected=72)
        box.source_reconcile(v2, snap([items]), DAY, expected_check_hash=box.get(v2)['check_hash'])
        rv2 = summarize(box.get(v2))
        self.assertFalse([i for i in rv2['issues'] if i['code'] == 'sharepoint_value_differs'])
        self.assertEqual(rv2['inspected'], summarize(parent)['inspected'] - 8)
        # the parent now shows those two findings as corrected in v2, and every finding still adds up
        rvp = summarize(box.get(h))
        self.assertEqual(sorted(i['cell'] for i in rvp['dismissed'] if i.get('dismissed_by') == 'corrected' and i['code'] == 'sharepoint_value_differs'), ['J5', 'K5'])
        self.assertTrue(rvp['reconcile']['adds_up'])
        # export: exactly J5 and K5 changed; every other value, formula, merged range, link, image and style is as it was
        out = box.download(v2)[1]
        wb0, c0 = cells(raw)
        wb1, c1 = cells(out)
        changed = {k for k in set(c0) | set(c1) if c0.get(k) != c1.get(k)}
        self.assertEqual(changed, {('Inspection Detail', 'J5'), ('Inspection Detail', 'K5')})
        self.assertEqual((c1[('Inspection Detail', 'J5')], c1[('Inspection Detail', 'K5')]), (72, 72))
        for ws0, ws1 in zip(wb0.worksheets, wb1.worksheets):
            self.assertEqual(ws0.title, ws1.title)
            self.assertEqual(sorted(map(str, ws0.merged_cells.ranges)), sorted(map(str, ws1.merged_cells.ranges)), ws0.title)
            self.assertEqual(len(ws0._images), len(ws1._images), ws0.title)
            links0 = {c.coordinate: c.hyperlink.target for r in ws0.iter_rows() for c in r if c.hyperlink}
            links1 = {c.coordinate: c.hyperlink.target for r in ws1.iter_rows() for c in r if c.hyperlink}
            self.assertEqual(links0, links1, ws0.title)
        for ref in ('J5', 'K5'):
            a, b = wb0['Inspection Detail'][ref], wb1['Inspection Detail'][ref]
            self.assertEqual((a.number_format, a.font.b, a.fill.fgColor.rgb, a.alignment.horizontal), (b.number_format, b.font.b, b.fill.fgColor.rgb, b.alignment.horizontal))
        formulas = {k: v for k, v in c1.items() if isinstance(v, str) and v.startswith('=')}
        self.assertTrue(formulas)
        self.assertEqual(formulas, {k: v for k, v in c0.items() if isinstance(v, str) and v.startswith('=')})
        # the change summary PDF is made from v2 itself: its changes, its check run, nothing from another version
        from hosting.change_summary import collect, render
        from pypdf import PdfReader
        data = collect(box, v2)
        self.assertEqual([(c['cell'], c['before'], c['after']) for c in data['changes']], [('J5', '80', '72'), ('K5', '80', '72')])
        self.assertEqual(data['run']['file_hash'], v2)
        text = ' '.join(page.extract_text() for page in PdfReader(io.BytesIO(render(data))).pages)
        self.assertIn('J5', text); self.assertIn('K5', text)
        self.assertIn(f'v{data["revision"]}', text)

    @unittest.skipUnless(shutil.which('soffice'), 'LibreOffice is not installed')
    def test_libreoffice_shows_summary_totals_that_include_the_corrected_cells(self):
        """Not native Excel: the exported file, opened in LibreOffice, shows summary totals that include the corrected cells
        (the app brings every saved formula result up to date, including whole-column SUMIF ranges)."""
        box, h, raw, props = reviewed()
        out = box.download(apply_displayed(box, h, chosen(props))['file_hash'])[1]
        d = tempfile.mkdtemp()
        with open(os.path.join(d, 'v2.xlsx'), 'wb') as f:
            f.write(out)
        subprocess.run(['soffice', '--headless', '--norestore', '-env:UserInstallation=file://' + d + '/profile', '--convert-to', 'csv', '--outdir', d,
                        os.path.join(d, 'v2.xlsx')], check=True, capture_output=True, timeout=120)
        summary = open(os.path.join(d, 'v2.csv'), encoding='utf-8', errors='replace').read()
        row = next(line for line in summary.splitlines() if line.startswith('80139189'))
        self.assertIn('232', row.split(','))     # 80 + 72 + 80 inspected for part 80139189 after the correction (was 240)


class NothingStaleIsApplied(unittest.TestCase):

    def test_a_sharepoint_read_after_the_review_refuses_the_reviewed_correction(self):
        box, h, raw, props = reviewed()
        items = list(ALL); items[1] = sp_item(433, inspected=75)            # SharePoint changed after the review
        box.source_reconcile(h, snap([items]), DAY, expected_check_hash=box.get(h)['check_hash'])
        with self.assertRaises(ValueError) as ctx:
            apply_displayed(box, h, chosen(props))
        self.assertIn('read again', str(ctx.exception))
        self.assertFalse(box.get(h).get('fixes_applied'))
        self.assertEqual(box.download(h)[1], raw)
        fresh = [p for p in box.fix_proposals(h)['proposals'] if p['source'] == 'SharePoint']
        self.assertEqual(sorted(p['after'] for p in fresh), ['75', '75'])   # the corrections are proposed again from the new read

    def test_a_cell_that_changed_since_the_review_refuses_everything(self):
        box, h, raw, props = reviewed()
        stale = chosen(props); stale[0]['before'] = '81'
        with self.assertRaises(FixError):
            apply_displayed(box, h, stale)
        self.assertFalse(box.get(h).get('fixes_applied'))


class ChangeSetsAreWhole(unittest.TestCase):
    """Item 6190: OK, NOK, Condition and Recorded defect of one record change together or not at all."""

    def setUp(self):
        self.box = SpreadsheetInbox(tempfile.mkdtemp())
        self.raw = sl_daily()
        self.h = self.box.ingest(NAME, self.raw)['file_hash']

    def change_set(self, nok_before=''):
        base = dict(sheet='Inspection Detail', source='Your edit', change_set='set-5', change_set_size=4, reason='Confirmed by the source owner')
        return [dict(base, cell='K5', before='80', after='79', kind='number'), dict(base, cell='M5', before=nok_before, after='1', kind='number'),
                dict(base, cell='P5', before='OK', after='NOK', kind='text'), dict(base, cell='N5', before='', after='Broken clip', kind='text')]

    def test_all_four_are_applied_together(self):
        v2 = apply_displayed(self.box, self.h, self.change_set())['file_hash']
        ws = load_workbook(io.BytesIO(self.box.download(v2)[1]))['Inspection Detail']
        self.assertEqual([ws[c].value for c in ('K5', 'M5', 'P5', 'N5')], [79, 1, 'NOK', 'Broken clip'])

    def test_one_stale_cell_means_none_are_applied(self):
        with self.assertRaises(FixError):
            apply_displayed(self.box, self.h, self.change_set(nok_before='0'))     # NOK is no longer what was reviewed
        self.assertFalse(self.box.get(self.h).get('fixes_applied'))
        self.assertEqual(self.box.download(self.h)[1], self.raw)

    def test_part_of_a_change_set_is_refused(self):
        with self.assertRaises(ValueError) as ctx:
            apply_displayed(self.box, self.h, self.change_set()[:3])
        self.assertIn('incomplete', str(ctx.exception))
        self.assertFalse(self.box.get(self.h).get('fixes_applied'))


if __name__ == '__main__':
    unittest.main()
