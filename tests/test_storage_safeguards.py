"""Storage and review safeguards: immutable originals, duplicates, multipart retries, proposed rechecks, photos."""
import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from hosting.fixes import apply_fixes
from hosting.spreadsheet_inbox import SpreadsheetInbox
from hosting.spreadsheets import inspect_spreadsheet
from tests.sl_daily_fixture import PNG, sl_daily

NAME = 'SL Alabama - 2214 - 2026-11-06.xlsx'


def media(raw):
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        return {n: z.read(n) for n in z.namelist() if n.startswith('xl/media/')}


class Originals(unittest.TestCase):
    def test_original_is_stored_once_with_its_sha256(self):
        with tempfile.TemporaryDirectory() as root:
            inbox = SpreadsheetInbox(root)
            raw = sl_daily()
            first = inbox.ingest(NAME, raw)
            self.assertEqual(first['file_hash'], hashlib.sha256(raw).hexdigest())
            again = inbox.ingest(NAME, raw)
            self.assertTrue(again['duplicate'])
            self.assertEqual(inbox.list()['count'], 1)
            self.assertEqual(SpreadsheetInbox(root).download(first['file_hash'])[1], raw)

    def test_a_changed_file_is_a_new_version_and_the_old_one_stays(self):
        with tempfile.TemporaryDirectory() as root:
            inbox = SpreadsheetInbox(root)
            a = inbox.ingest(NAME, sl_daily())
            b = inbox.ingest(NAME, sl_daily(source_records=False))
            self.assertEqual((a['version'], b['version']), (1, 2))
            self.assertEqual(inbox.list()['count'], 2)


class Multipart(unittest.TestCase):
    def test_retried_parts_and_a_retried_finish_are_safe(self):
        with tempfile.TemporaryDirectory() as root:
            inbox = SpreadsheetInbox(root)
            raw = sl_daily()
            sha = hashlib.sha256(raw).hexdigest()
            parts = [raw[i:i + 3000] for i in range(0, len(raw), 3000)]
            for i, p in enumerate(parts):
                inbox.store_part('a'*32, i, p)
            inbox.store_part('a'*32, 1, parts[1])   # a part sent twice after a dropped connection
            item = inbox.finish_upload('a'*32, NAME, len(parts), sha)
            self.assertEqual(item['file_hash'], sha)
            again = inbox.finish_upload('a'*32, NAME, len(parts), sha)   # the answer was lost; the browser asks again
            self.assertEqual(again['file_hash'], sha)
            self.assertTrue(again['duplicate'])
            self.assertEqual(inbox.list()['count'], 1)
            self.assertEqual(inbox.download(sha)[1], raw)

    def test_a_missing_part_saves_nothing(self):
        with tempfile.TemporaryDirectory() as root:
            inbox = SpreadsheetInbox(root)
            raw = sl_daily()
            inbox.store_part('b'*32, 0, raw[:1000])
            with self.assertRaises(ValueError):
                inbox.finish_upload('b'*32, NAME, 2, hashlib.sha256(raw).hexdigest())
            self.assertEqual(inbox.list()['count'], 0)


class ProposedRecheck(unittest.TestCase):
    def test_outdated_checks_are_shown_as_a_proposal_and_saved_only_when_applied(self):
        with tempfile.TemporaryDirectory() as root:
            inbox = SpreadsheetInbox(root)
            item = inbox.ingest(NAME, sl_daily())
            inbox.approve(item['file_hash'], 'Chris', note='looked fine', override=True)
            # make the stored result look like the old checker (12 rows / 1,200 units)
            with inbox.connect() as db:
                for table in ('spreadsheets', 'spreadsheet_checks'):
                    for row in db.execute(f'SELECT rowid AS rid, result_json FROM {table}').fetchall():
                        r = json.loads(row['result_json'])
                        r.update(checker_version='old', record_count=12, totals={'inspected': {'total': '1200'}, 'certified': {'total': '1200'}, 'rejected': {'total': None}})
                        db.execute(f'UPDATE {table} SET result_json=? WHERE rowid=?', (json.dumps(r), row['rid']))
            seen = inbox.get(item['file_hash'])
            self.assertEqual(seen['record_count'], 12)            # nothing changed by opening it
            self.assertTrue(seen['recheck']['available'])
            self.assertIn('checks_outdated', [h['code'] for h in inbox.holds(seen)])
            proposal = inbox.propose_recheck(item['file_hash'])
            changes = {c['field']: (c['before'], c['after']) for c in proposal['changes']}
            self.assertEqual(changes['record_count'], (12, 5))
            self.assertEqual(changes['inspected'], ('1200', '400'))
            self.assertEqual(inbox.get(item['file_hash'])['record_count'], 12)   # still unchanged
            applied = inbox.apply_recheck(item['file_hash'], 'Lauren')
            self.assertEqual(applied['record_count'], 5)
            self.assertEqual(applied['approval']['by'], 'Chris')                   # approvals are kept
            self.assertEqual(applied['recheck_applied'][-1]['by'], 'Lauren')
            self.assertEqual(applied['release_status'], 'held')


class InventoryLoading(unittest.TestCase):
    def test_one_unreadable_check_does_not_empty_the_list(self):
        with tempfile.TemporaryDirectory() as root:
            inbox = SpreadsheetInbox(root)
            inbox.ingest(NAME, sl_daily())
            inbox.ingest('other.xlsx', sl_daily(source_records=False))
            real = inbox.latest
            calls = {'n': 0}
            def flaky(db, h, fallback):
                calls['n'] += 1
                if calls['n'] == 1:
                    raise ValueError('broken row')
                return real(db, h, fallback)
            with patch.object(inbox, 'latest', flaky):
                listed = inbox.list()['spreadsheets']
            self.assertEqual(len(listed), 2)
            self.assertEqual(listed[0]['internal_check_status'], 'could_not_load_latest_check')

    def test_reload_after_save_returns_the_same_reports(self):
        with tempfile.TemporaryDirectory() as root:
            item = SpreadsheetInbox(root).ingest(NAME, sl_daily())
            listed = SpreadsheetInbox(root).list()['spreadsheets']
            self.assertEqual([x['file_hash'] for x in listed], [item['file_hash']])
            self.assertEqual(listed[0]['review']['inspected'], 400)


class Photos(unittest.TestCase):
    def test_embedded_photo_is_listed_hashed_and_tied_to_its_record(self):
        r = inspect_spreadsheet(NAME, sl_daily())
        p = r['photos']
        self.assertEqual(p['image_count'], 1)
        self.assertEqual(p['images'][0]['item'], '432')
        self.assertEqual(p['images'][0]['sha256'], hashlib.sha256(PNG).hexdigest())
        self.assertFalse(p['production'])
        self.assertEqual(r['record_count'], 5)

    def test_fixes_keep_the_original_photo_bytes(self):
        raw = sl_daily()
        fixed = apply_fixes(NAME, raw, [dict(sheet='Inspection Detail', cell='O4', before='', after='checked')])
        self.assertEqual(media(fixed), media(raw))

    def test_local_photo_links_are_flagged(self):
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(sl_daily()))
        wb['Inspection Detail']['R4'] = 'C:\\\\Users\\\\tablet\\\\photo1.jpg'
        wb['Inspection Detail']['R5'] = 'https://ppg.sharepoint.com/sites/x/photo2.jpg'
        buf = io.BytesIO(); wb.save(buf)
        r = inspect_spreadsheet(NAME, buf.getvalue())
        self.assertEqual(r['finding_counts'].get('photo_link_inaccessible'), 1)
        self.assertEqual(len(r['photos']['links']), 2)


if __name__ == '__main__':
    unittest.main()
