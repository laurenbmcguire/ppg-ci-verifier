"""Production-like SYNTHETIC report, end to end: capture -> verifier -> native workbook + checklist -> download.

Everything comes from tests/demo_client_fixture.py (fictitious DEMO-CLIENT / TEST-9001, invented counts and IDs).
No real list, report, client or person is involved, and nothing is read from or written to a live system.
These tests pin three rules: findings are never suppressed, unknown is never turned into zero, and a draft is
still produced and downloadable when evidence is missing.
"""
import collections
import io
import json
import re
import tempfile
import unittest
from pathlib import Path
from openpyxl import load_workbook
import demo_client_fixture as demo
from hosting import source_first as sf, source_first_workbook as wb, last_scan_runner as runner
from hosting.last_scan_local import StateStore, StoredDraftRouter, StoredDraftPublisher, BlobArtifacts
from hosting.last_scan_service import source_adapter

VISIBLE = ('Summary', '1st Shift', '2nd Shift', '3rd Shift', 'Rejects & Photos')
ALL_IDS = [str(i) for i in range(1, 37)]


def build(source):
    cap, blobs = source.capture()
    rows, findings = sf.reviewed_rows(cap, demo.MAP, [])
    head = wb.header(demo.HEADER, cap); contract = wb.contract()
    bind = sf.binding(cap, demo.MAP, [], head, contract['clean_sha256'], sf.digest(contract), wb.generator_hash())
    raw, verification = wb.build(rows, head, cap, bind, blobs)
    return cap, rows, findings, raw, verification, load_workbook(io.BytesIO(raw), data_only=True)


def by_code(findings):
    out = collections.defaultdict(list)
    for f in findings:
        out[f['code']].append(f)
    return out


def find(sheet, text):
    return next(c for row in sheet.iter_rows() for c in row if c.value == text)


def texts(book, names=VISIBLE):
    return ' | '.join(str(c.value) for n in names for row in book[n].iter_rows() for c in row if c.value is not None)


def images(book):
    return {n: len(getattr(book[n], '_images', [])) for n in book.sheetnames}


class FixtureIsSynthetic(unittest.TestCase):
    def test_everything_is_marked_fictitious(self):
        self.assertEqual((demo.CLIENT, demo.JOB, demo.HOST), ('DEMO-CLIENT', 'TEST-9001', 'example.sharepoint.com'))
        rows = demo.items()
        self.assertEqual(len(rows), 38)                                     # 36 for the day + 2 of another day
        for r in rows:
            self.assertRegex(r['fields'][demo.MAP['fields']['identifier']], r'^00TEST\d{5}$')
            self.assertRegex(r['fields'][demo.MAP['fields']['part']], r'^0009001\d{3}$')
        text = Path(demo.__file__).read_text()
        self.assertIn('fictitious', text.lower())
        for real in ('SLAlabama', 'Hyundai', 'Pratt', 'ppgquality', '2207', '2221', '2169'):
            self.assertNotIn(real, text)


class Capture(unittest.TestCase):
    def setUp(self):
        self.source = demo.Source(); self.cap, self.blobs = self.source.capture()

    def test_exact_day_only_and_read_only(self):
        self.assertEqual(sorted(r['id'] for r in self.cap['records']), sorted(ALL_IDS))
        self.assertEqual(self.cap['uncertain_dates'], [])
        self.assertTrue(self.cap['coverage']['items_terminal'])
        self.assertTrue(all(isinstance(u, str) for u in self.source.requests))   # the fake source only ever answers reads

    def test_denied_photos_are_recorded_not_replaced(self):
        photos = {p['record_id']: p for p in self.cap['photos']}
        self.assertEqual(sorted(photos, key=int), [str(i) for i in demo.PHOTO_ROWS])
        for p in photos.values():
            self.assertEqual((p['status'], p['http_status'], p.get('sha256')), ('unavailable', 403, None))
            self.assertIn('refused permission', p['reason']); self.assertIn('403', p['reason'])
        self.assertEqual((self.blobs, len(self.source.photo_requests)), ({}, len(demo.PHOTO_ROWS)))

    def test_other_photo_failures_keep_their_own_status(self):
        cap, _ = demo.Source(photo_status=404).capture()
        self.assertEqual({(p['status'], p['http_status']) for p in cap['photos']}, {('unavailable', 404)})
        self.assertTrue(all('refused permission' not in p['reason'] for p in cap['photos']))

    def test_available_photo_is_captured_with_its_hash_but_never_auto_selected(self):
        from PIL import Image
        out = io.BytesIO(); Image.new('RGB', (4, 3), (10, 120, 200)).save(out, format='PNG')
        cap, blobs = demo.Source(photo_status=200, photo_bytes=out.getvalue()).capture()
        self.assertEqual({(p['status'], p['http_status']) for p in cap['photos']}, {('supported', 200)})
        self.assertEqual(list(blobs), [sf.digest(out.getvalue())])


class Verifier(unittest.TestCase):
    def setUp(self):
        self.cap, _ = demo.Source().capture()
        self.rows, self.findings = sf.reviewed_rows(self.cap, demo.MAP, [])
        self.codes = by_code(self.findings)

    def test_every_condition_is_reported_once_per_record_and_none_is_dropped(self):
        self.assertEqual({k: len(v) for k, v in self.codes.items()},
                         dict(missing_value=36, unsupported_value_type=len(demo.LOOKUP_ROWS), photo_not_captured=len(demo.PHOTO_ROWS)))
        self.assertEqual(sorted((f['record_id'] for f in self.codes['missing_value']), key=int), ALL_IDS)
        self.assertEqual({f['field'] for f in self.codes['missing_value']}, {'rejected'})
        self.assertEqual(sorted(int(f['record_id']) for f in self.codes['unsupported_value_type']), list(demo.LOOKUP_ROWS))
        self.assertEqual(sorted(int(f['record_id']) for f in self.codes['photo_not_captured']), list(demo.PHOTO_ROWS))
        self.assertTrue(all(f['blocking'] for f in self.codes['missing_value'] + self.codes['unsupported_value_type']))
        self.assertTrue(all('403' in f['text'] for f in self.codes['photo_not_captured']))

    def test_unknown_nok_is_none_never_zero_and_ok_is_not_used_to_derive_it(self):
        for row in self.rows:
            self.assertIsNone(row['resolved']['rejected'])
            self.assertEqual(row['values']['rejected'], {'type': 'absent'})
            self.assertGreater(row['resolved']['inspected'], 0); self.assertGreater(row['resolved']['certified'], 0)
        self.assertNotIn('quantity_consistency', self.codes)               # nothing to compare: NOK is unknown

    def test_lookup_text_is_not_adopted_as_a_value(self):
        for row in self.rows:
            if int(row['id']) in demo.LOOKUP_ROWS:
                self.assertIsNone(row['resolved']['inspected_for'])
                self.assertEqual(row['values']['inspected_for']['type'], 'structured')
            else:
                self.assertEqual(row['resolved']['inspected_for'], 'Surface finish')


class NativeWorkbook(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cap, cls.rows, cls.findings, cls.raw, cls.verification, cls.book = build(demo.Source())

    def test_draft_is_built_despite_missing_evidence(self):
        # Regression: a structured lookup value used to be written into the provenance sheet as a number,
        # producing an unreadable workbook and therefore no draft at all.
        self.assertEqual(self.book.sheetnames, ['Summary', '1st Shift', '2nd Shift', '3rd Shift', 'Rejects & Photos', '_Calc', '_Source'])
        self.assertTrue(self.verification['native_layout_preserved'])
        self.assertEqual((self.verification['record_count'], self.verification['human_release'], self.verification['current_sharepoint_claim']), (36, 'held', False))
        self.assertEqual(self.book['Summary']['B5'].value, 'DRAFT · UNKNOWN QUANTITIES')

    def test_native_template_and_media_are_the_same_as_for_a_clean_report(self):
        from source_first_fixture import capture as simple_capture, MAP as SIMPLE_MAP
        c = simple_capture(); rows, _ = sf.reviewed_rows(c, SIMPLE_MAP, []); head = wb.header({}, c); co = wb.contract()
        raw, _ = wb.build(rows, head, c, sf.binding(c, SIMPLE_MAP, [], head, co['clean_sha256'], sf.digest(co), wb.generator_hash()), {})
        clean = load_workbook(io.BytesIO(raw), data_only=True)
        self.assertEqual(self.book.sheetnames, clean.sheetnames)
        self.assertEqual(images(self.book), images(clean))                 # logos/charts kept; no photo added or removed
        self.assertEqual(self.verification['photo_provenance'], [])
        for name in VISIBLE:
            self.assertEqual(self.book[name].sheet_properties.tabColor, clean[name].sheet_properties.tabColor)
            self.assertEqual([self.book[name].column_dimensions[c].width for c in 'ABCDEFGH'],
                             [clean[name].column_dimensions[c].width for c in 'ABCDEFGH'])

    def test_totals_use_known_values_and_leave_nok_blank(self):
        s = self.book['Summary']
        inspected = sum(r['resolved']['inspected'] for r in self.rows); ok = sum(r['resolved']['certified'] for r in self.rows)
        self.assertEqual((s['F24'].value, s['G24'].value, s['I24'].value), (36, inspected, ok))
        for address in ('K21', 'K22', 'K23', 'K24'):
            self.assertIsNone(s[address].value, address)                   # NOK per shift and for the day: blank, not 0
        self.assertIn(s['L24'].value, (None, ''))                          # no reject rate from an unknown quantity
        for name in ('1st Shift', '2nd Shift', '3rd Shift'):
            sheet = self.book[name]
            data = [r for r in sheet.iter_rows(min_row=16) if isinstance(r[4].value, str) and r[4].value.startswith('00TEST')]
            self.assertEqual(len(data), demo.ROWS_PER_SHIFT)               # every row present, none truncated
            for r in data:
                self.assertIsNone(r[9].value)                              # column J, rejected
                self.assertGreater(r[6].value, 0); self.assertGreater(r[10].value, 0)

    def test_no_zero_is_shown_where_the_reject_quantity_is_unknown(self):
        s = self.book['Summary']
        subtotal = find(s, 'Shown-record subtotal')
        self.assertIsNone(s.cell(subtotal.row, 5).value); self.assertIsNone(s.cell(subtotal.row, 6).value)   # was 0 / 0
        zeros = [(n, c.coordinate) for n in VISIBLE for row in self.book[n].iter_rows() for c in row
                 if type(c.value) in (int, float) and c.value == 0]
        labels = {(n, a): self.book[n].cell(self.book[n][a].row - 1, self.book[n][a].column).value for n, a in zeros}
        self.assertEqual(set(labels.values()), {'Selected photos', 'Known rejects without photo'}, labels)   # true counts only
        text = texts(self.book)
        self.assertIn('the rejected quantity is unknown on 36 records', text)
        self.assertIn('0 known; unknown on 36 records', text)
        self.assertNotIn('0 rejected records have no selected photo', text)

    def test_lookup_text_stays_out_of_the_report_and_in_the_provenance_sheet(self):
        self.assertNotIn('Edge burr', texts(self.book)); self.assertNotIn('LookupValue', texts(self.book))
        source = self.book['_Source']
        cells = [c.value for row in source.iter_rows() for c in row if isinstance(c.value, str) and 'LookupId' in c.value]
        self.assertEqual(len(cells), 2 * len(demo.LOOKUP_ROWS))            # original and approved value, as exact JSON text
        for value in cells:
            self.assertEqual(json.loads(value)['LookupValue'], 'Edge burr (unverified lookup text)')
        self.assertIn('structured', [c.value for row in source.iter_rows() for c in row])

    def test_no_photo_is_inserted_or_invented(self):
        self.assertEqual(self.verification['photo_provenance'], [])
        self.assertIn('4 unavailable or not attempted', texts(self.book))
        self.assertNotIn('View source photo', texts(self.book, ('1st Shift', '2nd Shift', '3rd Shift')))

    def test_boolean_and_structured_values_in_any_optional_field_still_produce_a_workbook(self):
        f = demo.MAP['fields']
        for value in (True, False, {'nested': {'a': [1, 2]}}, [1, 2, 3]):
            rows = demo.items(other_day=False); rows[0]['fields'][f['comments']] = value; rows[1]['fields'][f['location']] = value
            _, _, findings, raw, verification, book = build(demo.Source(rows=rows))
            self.assertTrue(verification['native_layout_preserved'], value)
            self.assertEqual(len(by_code(findings)['unsupported_value_type']), len(demo.LOOKUP_ROWS) + 2, value)


class KnownAndZeroNok(unittest.TestCase):
    def test_known_nok_with_denied_photos_reports_each_missing_photo_and_still_builds(self):
        _, rows, findings, raw, verification, book = build(demo.Source(rows=demo.items(nok=3)))
        codes = by_code(findings)
        self.assertEqual({k: len(v) for k, v in codes.items()}, dict(photo_unavailable=36, unsupported_value_type=len(demo.LOOKUP_ROWS)))
        self.assertFalse(any(f['blocking'] for f in codes['photo_unavailable']))
        s = book['Summary']
        self.assertEqual((s['K24'].value, s['B5'].value != 'DRAFT · UNKNOWN QUANTITIES'), (108, True))
        subtotal = find(s, 'Shown-record subtotal')
        self.assertEqual(s.cell(subtotal.row, 6).value, 13 * 3)            # thirteen shown records, 3 each
        first = book['1st Shift']
        statuses = collections.Counter(r[12].value for r in first.iter_rows(min_row=16) if isinstance(r[4].value, str) and r[4].value.startswith('00TEST'))
        self.assertEqual(statuses, {'Unavailable / not captured': 2, 'No captured photo': 10})
        self.assertEqual(verification['photo_provenance'], [])

    def test_a_recorded_zero_stays_zero_and_is_distinct_from_unknown(self):
        _, rows, findings, raw, verification, book = build(demo.Source(rows=demo.items(nok=0)))
        self.assertNotIn('missing_value', by_code(findings))
        s = book['Summary']
        self.assertEqual((s['K24'].value, s['K21'].value), (0, 0))
        self.assertEqual(book['1st Shift']['J16'].value, 0)
        subtotal = find(s, 'Shown-record subtotal')
        self.assertEqual(s.cell(subtotal.row, 6).value, 0)                 # a real zero is still shown


class DraftDownload(unittest.TestCase):
    """The same source through the scheduled runner: one held draft per shift, stored privately, downloadable."""
    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.TemporaryDirectory()
        cls.store = StateStore(Path(cls.folder.name) / 'state.db'); cls.source = demo.Source()
        cls.assignment = demo.assignment(); cls.router = StoredDraftRouter(BlobArtifacts(cls.store), [cls.assignment])
        fetch = lambda scope, settings: source_adapter(scope, settings, capture_fn=lambda s: cls.source.capture(s))
        cls.out = runner.run_once(cls.store, dict(enabled=True, assignments=[cls.assignment]), fetch=fetch, render=cls.router,
                                  run_id='demo-run-1', entries=demo.REGISTRY)
        cls.publisher = cls.router.for_scope(cls.assignment)
        cls.drafts = {r['shift']: r for r in cls.out['results']}

    @classmethod
    def tearDownClass(cls):
        cls.folder.cleanup()

    def test_one_held_draft_per_shift_and_nothing_sent(self):
        self.assertEqual((self.out['counts'], self.out['issues'], self.out['notifications_sent'], self.out['client_release']),
                         ({'draft_ready': 3}, [], 0, 'held'))
        self.assertIsInstance(self.publisher, StoredDraftPublisher)

    def test_download_returns_the_exact_stored_workbook_and_checklist(self):
        for shift, result in self.drafts.items():
            op = result['operation_id']; receipt = self.publisher.lookup(op)
            workbook = self.publisher.read(op, 'draft.xlsx'); checklist = self.publisher.read(op, 'checklist.json')
            self.assertEqual((sf.digest(workbook), sf.digest(checklist)), (receipt['workbook_sha256'], receipt['checklist_sha256']))
            self.assertEqual(self.publisher.read(op, 'draft.xlsx'), workbook)          # stable across downloads
            book = load_workbook(io.BytesIO(workbook), data_only=True)
            self.assertEqual((book['Summary']['F24'].value, book['Summary']['K24'].value), (demo.ROWS_PER_SHIFT, None))
            self.assertIn('DEMO-CLIENT (fictitious)', texts(book))

    def test_checklist_lists_every_finding_with_its_record_and_nothing_is_marked_verified(self):
        total = collections.Counter()
        for shift, result in self.drafts.items():
            op = result['operation_id']
            checklist = json.loads(self.publisher.read(op, 'checklist.json'))
            ids = checklist['source_ids']
            self.assertEqual(len(ids), demo.ROWS_PER_SHIFT)
            codes = by_code(checklist['findings'])
            self.assertEqual(sorted(f['record_id'] for f in codes['missing_value']), sorted(ids))
            self.assertEqual((checklist['status'], checklist['client_release'], checklist['day_completion']), ('DRAFT', 'held', 'not_established'))
            self.assertEqual({p['http_status'] for p in checklist['photo_evidence']}, {403})
            self.assertEqual(sorted(p['record_id'] for p in checklist['photo_evidence']), sorted(f['record_id'] for f in codes['photo_not_captured']))
            self.assertFalse(checklist['verification']['current_sharepoint_claim'])
            note = self.publisher.read(op, 'checklist.txt').decode()
            self.assertIn('not approved for client release', note)
            for f in checklist['findings']:
                self.assertIn('Record %s: ' % f['record_id'], note)
            self.assertEqual(note.count('[missing_value]'), demo.ROWS_PER_SHIFT)
            self.assertIn('Photos named at the source but not captured:', note); self.assertIn('HTTP 403', note)
            self.assertNotIn('verified', note.lower().replace('verification issues', '').replace('unverified', ''))
            total.update(f['code'] for f in checklist['findings'])
        self.assertEqual(total, dict(missing_value=36, unsupported_value_type=len(demo.LOOKUP_ROWS), photo_not_captured=len(demo.PHOTO_ROWS)))

    def test_source_was_only_read(self):
        self.assertTrue(self.source.requests)
        self.assertTrue(all('$value' in u for u in self.source.photo_requests))
        with self.store.transaction('outbox:' + self.drafts['1']['scope']) as box:
            entry = box['entries'][self.drafts['1']['operation_id']]
        self.assertEqual((entry['status'], 'attempts' in entry), ('pending', False))


if __name__ == '__main__':
    unittest.main()
