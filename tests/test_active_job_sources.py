"""The seven pinned job sources through the real source-selection and report-build code (SYNTHETIC rows only).

No network: the site-name lookup and the list read are replaced with made-up fixtures. The list IDs are the pinned ones;
every record, identifier and quantity is invented.
"""
import contextlib
import io
import tempfile
import unittest
from unittest.mock import patch

from openpyxl import load_workbook

from hosting import builder, public_api, registry, sharepoint, shifts
from hosting.spreadsheet_inbox import SpreadsheetInbox

SL, HY, LEAR, BRIGGS = 'SLAlabama_AlexanderCity', 'Hyundai_Montgomery', 'Lear_T1', 'BriggsStratton_Auburn'
SITE_ID = {name: f'ppg.sharepoint.com,{n}0000000-0000-0000-0000-000000000000,{n}1111111-1111-1111-1111-111111111111' for n, name in enumerate((SL, HY, LEAR, BRIGGS), 1)}
L2207, L2207_HY = '32a42ee0-4592-4d73-b893-1b98970b3d4b', '47f89ceb-d8b9-45c3-932a-66d46e3f4272'
L2169, L2169_SECOND = '38718598-a97e-4388-bcc5-709ede868b21', 'b85d1fdb-d7b3-4f5c-8223-8d8f737e06d5'
SEVEN = {   # the job labels the daily run uses -> the one source each must build from
    'SLALAL-SLAL-2169': (SL, L2169),
    'SLALAL-SLALANNX-2200': (SL, 'e402d426-4595-47bc-a305-a0704df18258'),
    'SLAL-SLAL-2214': (SL, 'd1a655b8-dc19-4d0f-bad2-35ed9999ccc2'),
    'SLAL-HMMA-2236': (HY, '9876a097-558d-4407-9b90-216d46a81217'),
    'LSAMX-HMMA-2250': (HY, '4a6b1fb9-ede1-4d5c-80dd-9ad1d889df91'),
    'BROQU-LEART1-2206': (LEAR, '4cd2ca3c-c0c6-4092-8b8d-8b52ada1fb3d'),
    'TENFM-BSAL-2228': (BRIGGS, 'fe964017-6594-4f24-8e8a-deb82119418c'),
}
DAY = '2026-11-19'
ROLES = {'Identifier': 'identifier', 'Qty': 'inspected', 'NOK': 'rejected'}


def item(n, day=DAY, shift='1', **fields):
    return {'id': str(n), 'fields': dict({'Date': day, 'Date0': f'{day}T15:{n % 60:02d}:00Z', 'Shift': shift, 'Identifier': f'SYN{n:05d}', 'Qty': 10, 'NOK': None}, **fields)}


def capture(site, list_id, items):
    return (site, {'id': list_id, 'displayName': 'Data (synthetic)', '_capture': {'site_id': SITE_ID[site], 'list_id': list_id, 'columns': []}}, {'Shift': 'Shift'}, items)


class Box:
    """Settings as the app would hold them: nothing saved, so only the pinned entries exist."""
    def __init__(self, saved=()):
        self.saved = list(saved)
    def setting(self, key, default=None):
        return self.saved if key == registry.REGISTRY_KEY else default


@contextlib.contextmanager
def offline(lists=None, unresolvable=()):
    """Site names resolve from a fixture; list reads return fixture rows for the exact site and list asked for."""
    asked = []
    def read(job, opener=None, *, site_id=None, list_id=None, refresh=False):
        asked.append((site_id, list_id))
        site = next(name for name, sid in SITE_ID.items() if sid == site_id)
        return capture(site, list_id, (lists or {}).get(list_id, []))
    def site_id_for(site, opener=None):
        return None if site in unresolvable else SITE_ID[site]
    with patch('hosting.reconcile.site_id_for', side_effect=site_id_for), patch.object(sharepoint, 'configured', return_value=True), \
            patch.object(sharepoint, '_list_items', side_effect=read), patch.object(builder, 'column_roles', return_value=ROLES):
        yield asked


def select(job, source=None, saved=()):
    return public_api._build_binding(Box(saved), job, source)


class SourceSelection(unittest.TestCase):
    def test_each_of_the_seven_selects_its_one_pinned_list(self):
        with offline():
            for job, (site, list_id) in SEVEN.items():
                self.assertEqual(select(job), dict(site_id=SITE_ID[site], list_id=list_id), job)

    def test_2169_never_builds_from_its_secondary_list(self):
        with offline():
            self.assertEqual(select('SLALAL-SLAL-2169')['list_id'], L2169)
            for source in (dict(site=SL, list_id=L2169_SECOND), dict(site_id=SITE_ID[SL], list_id=L2169_SECOND)):
                with self.assertRaises(public_api.ApiError) as e:
                    select('SLALAL-SLAL-2169', source)
                self.assertEqual(e.exception.status, 409)
            # even if somebody saves it as verified in the app, it is still not a production source and the primary still builds
            saved = [dict(client_key='SLAlabama', client='SL Alabama', job='2169', site=SL, site_id=SITE_ID[SL], list_id=L2169_SECOND, verified=True)]
            self.assertEqual(select('SLALAL-SLAL-2169', saved=saved)['list_id'], L2169)
            with self.assertRaises(public_api.ApiError):
                select('SLALAL-SLAL-2169', dict(site=SL, list_id=L2169_SECOND), saved=saved)
        second = next(e for e in registry.SEED if e.get('list_id') == L2169_SECOND)
        self.assertEqual((second['verified'], second['separate'], second['production']), (False, True, False))
        self.assertIn('lack an authoritative work date', second['note']); self.assertNotIn('empty', second['note'].lower())

    def test_2207_resolves_to_alexander_city_and_the_hyundai_copy_is_never_substituted(self):
        with offline():
            self.assertEqual(select('SLALAL-CJAL-2207', dict(site=SL, list_id=L2207)), dict(site_id=SITE_ID[SL], list_id=L2207))
            for wrong in (None,                                            # no source named: two lists carry 2207, none is guessed
                          dict(site=SL, list_id=L2207_HY),                 # the Hyundai list under the Alexander City name
                          dict(site=HY, list_id=L2207),                    # the Alexander City list under the Hyundai name
                          dict(site_id=SITE_ID[HY], list_id=L2207)):
                with self.assertRaises(public_api.ApiError, msg=str(wrong)) as e:
                    select('SLALAL-CJAL-2207', wrong)
                self.assertEqual(e.exception.status, 409)
        with offline(unresolvable=(SL,)):                                   # Alexander City cannot be looked up: stop, do not fall back
            with self.assertRaises(public_api.ApiError):
                select('SLALAL-CJAL-2207', dict(site=SL, list_id=L2207))
        named = registry.resolve({}, 'SL Alabama - 2207 - 2026-11-19.xlsx', '2207', DAY, registry.entries([]), [])
        self.assertEqual((named['status'], named['site'], named['list_id']), ('verified', SL, L2207))

    def test_jobs_that_are_not_pinned_and_pratt_sources_do_not_build(self):
        with offline():
            for job in ('SCTMFG-BSAL-2229', 'OWENS-BASCO-2230', 'SLAL-HYMOBAL-2221', 'Audits_KN', 'DCA Main Audit'):
                with self.assertRaises(public_api.ApiError, msg=job):
                    select(job)
        pratt = [e for e in registry.SEED if e['client_key'] == 'PrattWhitney']
        self.assertEqual(pratt, [dict(client_key='PrattWhitney', client='Pratt & Whitney', source=s, job=None, verified=True, note='No verified job number.')
                                 for s in ('Audits_KN', 'DCA Main Audit', 'DCA Multi-Pack Audit')])


class Store:
    def __init__(self, directory):
        self.directory = directory
    @contextlib.contextmanager
    def session(self):
        yield self.directory


class Handler:
    """Only what op_build needs from the web handler; the follow-up checks are not run here."""
    def __init__(self):
        self.directory = tempfile.mkdtemp()
    def store(self):
        return Store(self.directory)
    def sharepoint_check(self, file_hash):
        pass
    def baseline_check(self, file_hash):
        pass


def build(job, source=None, lists=None, handler=None, day=DAY):
    handler = handler or Handler()
    with offline(lists) as asked, patch('api.index.sharepoint_configured', return_value=False):
        out = public_api.op_build(handler, job, day, source=source)
    return out, asked, handler


class ReportBuild(unittest.TestCase):
    def test_each_of_the_seven_builds_from_its_own_list_and_no_other(self):
        for job, (site, list_id) in SEVEN.items():
            rows = [item(n) for n in range(1, 6)] + [item(9, day='2026-11-18')]
            out, asked, handler = build(job, lists={list_id: rows})
            self.assertEqual(asked, [(SITE_ID[site], list_id)], job)                 # exactly one list was read: the pinned one
            self.assertEqual((out['built']['scans'], out['built']['site_id'], out['built']['list_id']), (5, SITE_ID[site], list_id), job)
            self.assertEqual(out['report']['filename'], f'{job} - EOS - 11.19.26.xlsx')
            name, raw = SpreadsheetInbox(handler.directory).download(out['report']['id'])
            book = load_workbook(io.BytesIO(raw))
            self.assertIn('1st Shift', book.sheetnames)

    def test_2207_builds_only_with_its_alexander_city_source_named(self):
        rows = {L2207: [item(n) for n in range(1, 4)], L2207_HY: [item(n) for n in range(50, 60)]}
        out, asked, _ = build('SLALAL-CJAL-2207', dict(site=SL, list_id=L2207), rows)
        self.assertEqual((asked, out['built']['scans']), ([(SITE_ID[SL], L2207)], 3))
        with self.assertRaises(public_api.ApiError):
            build('SLALAL-CJAL-2207', None, rows)


class TheMissingNov19Report(unittest.TestCase):
    """What the build code does with a day that has 22 dated rows, and what stops it. Fixtures only: these show what the
    code would do in each case; they do not show which case happened on the live site."""
    SOURCE = dict(site=SL, list_id=L2207)

    def day_rows(self, extra=()):
        return [item(n, shift='1' if n <= 12 else '2') for n in range(1, 23)] + [item(100 + n, day='2026-11-18') for n in range(1, 6)] + list(extra)

    def test_twenty_two_dated_rows_build_a_report(self):
        out, _, _ = build('SLALAL-CJAL-2207', self.SOURCE, {L2207: self.day_rows()})
        self.assertEqual((out['built']['scans'], out['report']['filename']), (22, 'SLALAL-CJAL-2207 - EOS - 11.19.26.xlsx'))

    def test_it_is_not_suppressed_because_a_report_already_exists(self):
        handler = Handler()
        build('SLALAL-CJAL-2207', self.SOURCE, {L2207: self.day_rows()}, handler, day='2026-11-18')      # yesterday's report is there
        first, _, _ = build('SLALAL-CJAL-2207', self.SOURCE, {L2207: self.day_rows()}, handler)
        again, _, _ = build('SLALAL-CJAL-2207', self.SOURCE, {L2207: self.day_rows()}, handler)          # the same day built twice
        self.assertEqual((first['built']['scans'], again['built']['scans']), (22, 22))
        self.assertEqual(again['report']['id'], first['report']['id'])                                   # same rows, same file: returned, not refused
        later, _, _ = build('SLALAL-CJAL-2207', self.SOURCE, {L2207: self.day_rows([item(23)])}, handler)
        self.assertEqual((later['built']['scans'], later['report']['id'] != first['report']['id']), (23, True))

    def test_rows_with_a_blank_date_are_in_no_report_and_are_counted_not_dated(self):
        blank = []
        for n in (900, 901):
            row = item(n, day='2026-11-20'); del row['fields']['Date']; blank.append(row)
        for day in (DAY, '2026-11-18'):                                    # every day still builds from its own dated rows
            dated = self.day_rows([item(950, day='2026-11-18')])
            want, _, _ = build('SLALAL-CJAL-2207', self.SOURCE, {L2207: dated}, day=day)
            out, _, _ = build('SLALAL-CJAL-2207', self.SOURCE, {L2207: dated + blank}, day=day)
            self.assertEqual(out['built']['scans'], want['built']['scans'])                       # the blank rows are added to no day
            self.assertEqual(out['report']['id'], want['report']['id'])                           # the same workbook, row for row
            self.assertEqual(out['built']['undated'], {'count': 2, 'items': ['900', '901']})
            self.assertIn("2 rows in this SharePoint list have no Date, so they are in no day's report. No date was given to them.", out['built']['completeness_note'])
        clean, _, _ = build('SLALAL-CJAL-2207', self.SOURCE, {L2207: self.day_rows()})
        self.assertEqual(clean['built']['undated'], {'count': 0, 'items': []})
        self.assertNotIn('no Date', clean['built']['completeness_note'])

    def test_a_day_whose_only_rows_have_a_blank_date_says_so_instead_of_inventing_one(self):
        blank = item(900, day='2026-11-20'); del blank['fields']['Date']
        with self.assertRaises(public_api.ApiError) as e:
            build('SLALAL-CJAL-2207', self.SOURCE, {L2207: [item(1, day='2026-11-18'), blank]}, day='2026-11-20')
        self.assertIn("SharePoint has no scans for SLALAL-CJAL-2207 on 2026-11-20. 1 row in this SharePoint list has no Date, so it is in no day's report. No date was given to it.", str(e.exception.args))

    def test_dates_stored_as_timestamps_block_when_the_site_date_settings_cannot_be_confirmed(self):
        stamped = [item(n, **{'Date': '2026-11-19T06:00:00Z'}) for n in range(1, 23)]     # how SharePoint returns a date-only column
        with self.assertRaises(public_api.ApiError) as e:
            build('SLALAL-CJAL-2207', self.SOURCE, {L2207: stamped})
        self.assertIn('unconfirmed business dates', str(e.exception.args))

    def test_a_day_with_no_rows_says_no_scans_and_an_unknown_shift_says_so(self):
        with self.assertRaises(public_api.ApiError) as e:
            build('SLALAL-CJAL-2207', self.SOURCE, {L2207: [item(1, day='2026-11-18')]})
        self.assertIn('SharePoint has no scans for SLALAL-CJAL-2207 on 2026-11-19', str(e.exception.args))
        with self.assertRaises(public_api.ApiError) as e:
            build('SLALAL-CJAL-2207', self.SOURCE, {L2207: self.day_rows([item(24, shift='9')])})
        self.assertIn('unrecognized recorded Shift', str(e.exception.args))

    def test_source_selection_is_not_what_stops_2207_when_its_source_is_named(self):
        with offline():
            self.assertEqual(select('SLALAL-CJAL-2207', self.SOURCE)['list_id'], L2207)


if __name__ == '__main__':
    unittest.main()
