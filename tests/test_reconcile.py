"""Reconciliation to the live SharePoint list: coverage, business dates, item-level differences. Read-only."""
import tempfile
import unittest
from unittest.mock import patch

from hosting import reconcile
from hosting.spreadsheet_inbox import SpreadsheetInbox
from hosting.spreadsheets import report_and_export_records
from tests.sl_daily_fixture import sl_daily as _sl_daily


def sl_daily(**kwargs):
    # Successful reconciliation supplies measured reject counts, including explicit zeros.
    return _sl_daily(**dict({"nok_blank": False}, **kwargs))

SITE, LIST = 'SLAlabama_AlexanderCity', 'list-2214'
COLS = [dict(name='Date', displayName='Date', dateTime=dict(format='dateOnly')), dict(name='Date0', displayName='Scan time', dateTime=dict(format='dateTime')),
        dict(name='Identifier', displayName='Identifier'), dict(name='QuantityInspected', displayName='Quantity Inspected'),
        dict(name='QuantityOK', displayName='Quantity OK'), dict(name='QtyNOK', displayName='Qty NOK')]


def sp_item(i, inspected=80, date='2026-11-06', scan='2026-11-06T13:05:00Z', version='3.0'):
    return {'id': str(i), 'webUrl': f'https://ppg.sharepoint.com/sites/{SITE}/Lists/x/{i}_.000', 'lastModifiedDateTime': '2026-11-06T14:00:00Z',
            'fields': {'Date': date, 'Date0': scan, 'Identifier': f'LOT-{i - 431:02d}', 'QuantityInspected': inspected, 'QuantityOK': inspected, 'QtyNOK': 0,
                       '_UIVersionString': version}}


def graph(pages, columns=COLS, fail=None):
    """A fake Graph: pages is a list of item lists; fail maps a page number (1-based) to an HTTP status."""
    def fetch(url, opener=None, retries=2):
        if url.endswith('?$select=id,displayName,webUrl,lastModifiedDateTime'):
            return 200, {'id': LIST, 'displayName': 'Data #2214', 'webUrl': f'https://ppg.sharepoint.com/sites/{SITE}/Lists/Data%202214SLALSLAL'}
        if '/columns?' in url:
            return 200, {'value': columns}
        n = int(url.rsplit('page=', 1)[1]) if 'page=' in url else 1
        if fail and n in fail:
            return fail[n], None
        nxt = f'https://graph.microsoft.com/v1.0/sites/site-1/lists/{LIST}/items?page={n + 1}' if n < len(pages) else None
        return 200, dict({'value': pages[n - 1]}, **({'@odata.nextLink': nxt} if nxt else {}))
    return fetch


def snap(pages, **kw):
    with patch('hosting.reconcile.site_id_for', return_value='site-1'), patch('hosting.reconcile._fetch', graph(pages, **kw)):
        return reconcile.snapshot(SITE, LIST)


def run(raw, snapshot, day='2026-11-06'):
    records, exports = report_and_export_records('r.xlsx', raw)
    return reconcile.compare(day, records, exports, snapshot)


ALL = [sp_item(i) for i in range(432, 437)]


class Coverage(unittest.TestCase):
    def test_full_coverage_and_matching_items_verify(self):
        s = snap([ALL[:3], ALL[3:]])
        self.assertTrue(s['coverage']['complete'])
        self.assertEqual(s['coverage']['pages'], 2)
        r = run(sl_daily(), s)
        self.assertEqual(r['status'], 'verified', r['rows'])
        self.assertTrue(r['captured_at'])
        self.assertEqual(r['day_basis'], ['business_date'])

    def test_missing_reject_evidence_never_becomes_zero(self):
        source_missing = [dict(it, fields={k: v for k, v in it['fields'].items() if k != 'QtyNOK'}) for it in ALL]
        for workbook, source in ((sl_daily(nok_blank=True), ALL), (sl_daily(), source_missing)):
            result = run(workbook, snap([source]))
            self.assertFalse(result['verified'])
            self.assertNotEqual(result['status'], 'verified')

    def test_a_throttled_page_is_incomplete_never_verified(self):
        r = run(sl_daily(), snap([ALL[:3], ALL[3:]], fail={2: 429}))
        self.assertEqual(r['status'], 'incomplete')
        self.assertTrue(r['coverage']['throttled'])
        self.assertFalse(r['verified'])

    def test_denied_access_is_incomplete(self):
        r = run(sl_daily(), snap([ALL], fail={1: 403}))
        self.assertEqual(r['status'], 'incomplete')
        self.assertTrue(r['coverage']['denied'])

    def test_unknown_schema_is_incomplete(self):
        r = run(sl_daily(), snap([ALL], columns=[c for c in COLS if c['name'] != 'QuantityInspected']))
        self.assertEqual(r['status'], 'incomplete')
        self.assertTrue(r['coverage']['unknown_schema'])


class ColumnChoice(unittest.TestCase):
    def test_the_column_the_tablets_fill_is_used_not_an_empty_legacy_one(self):
        cols = COLS + [dict(name='QtyInspected', displayName='Qty Inspected'), dict(name='Quantity', displayName='Quantity')]
        items = []
        for it in ALL:
            f = dict(it['fields']); f['Quantity'] = f.pop('QuantityInspected'); f['QtyInspected'] = None
            items.append(dict(it, fields=f))
        s = snap([items], columns=[c for c in cols if c['name'] != 'QuantityInspected'])
        r = run(sl_daily(), s)
        self.assertEqual(r['quantity_columns']['inspected'], 'Quantity')
        self.assertEqual(r['column_mapping']['inspected']['status'], 'single')   # the only inspected column with values that day
        confirmed = reconcile.compare('2026-11-06', *report_and_export_records('r.xlsx', sl_daily()), s, confirmed={'inspected': 'Quantity'})
        self.assertEqual(confirmed['status'], 'verified')

    def test_two_filled_columns_that_disagree_are_a_conflict(self):
        cols = COLS + [dict(name='Quantity', displayName='Quantity')]
        items = [dict(it, fields=dict(it['fields'], Quantity=70)) for it in ALL]
        r = run(sl_daily(), snap([items], columns=cols))
        self.assertEqual(r['column_mapping']['inspected']['status'], 'conflict')
        self.assertNotEqual(r['status'], 'verified')


class Differences(unittest.TestCase):
    def test_value_difference_is_listed_with_link_and_a_proposal_not_applied(self):
        items = list(ALL); items[1] = sp_item(433, inspected=72)
        r = run(sl_daily(), snap([items]))
        self.assertEqual(r['status'], 'discrepancies')
        diff = next(x for x in r['rows'] if x['status'] == 'value_differs')
        self.assertEqual((diff['item'], diff['field'], diff['report'], diff['sharepoint']), ('433', 'inspected', '80', '72'))
        self.assertIsNone(diff['url'])  # synthetic site-1 is not a validated SharePoint tenant ID
        self.assertEqual(r['proposed_corrections'][0]['after'], '72')
        self.assertFalse(r['corrections_applied'])

    def test_missing_items_both_ways(self):
        r = run(sl_daily(), snap([ALL[:4] + [sp_item(499)]]))
        codes = {x['status'] for x in r['rows']}
        self.assertIn('missing_from_report', codes)      # 499 is on SharePoint for the day
        self.assertIn('not_on_sharepoint', codes)        # 436 is in the report only

    def test_export_edited_after_capture_is_shown(self):
        items = list(ALL); items[0] = sp_item(432, version='4.0')
        r = run(sl_daily(), snap([items]))
        self.assertTrue(any(x['status'] == 'changed_since_export' for x in r['rows']))
        self.assertEqual(r['export']['rows'], 5)


class BusinessDates(unittest.TestCase):
    def test_date_only_field_is_read_in_chicago_not_cut_from_utc(self):
        self.assertEqual(reconcile.business_day('2026-11-06T06:00:00Z', True), '2026-11-06')   # CST midnight
        self.assertEqual(reconcile.business_day('2026-07-06T05:00:00Z', True), '2026-07-06')   # CDT midnight
        self.assertEqual(reconcile.business_day('2026-11-06', True), '2026-11-06')

    def test_dst_change_days(self):
        self.assertEqual(reconcile.business_day('2026-11-01T05:00:00Z', True), '2026-11-01')  # fall back day starts in CDT
        self.assertEqual(reconcile.business_day('2026-03-08T06:00:00Z', True), '2026-03-08')  # spring forward day starts in CST

    def test_overnight_scan_belongs_to_its_business_date(self):
        late = sp_item(436, date='2026-11-06', scan='2026-11-07T07:30:00Z')   # 01:30 on Nov 7 in Chicago
        day, basis = reconcile.item_day(late['fields'], {c['name']: c for c in COLS})
        self.assertEqual((day, basis), ('2026-11-06', 'business_date'))
        r = run(sl_daily(), snap([ALL[:4] + [late]]))
        self.assertEqual(r['status'], 'verified', r['rows'])

    def test_without_a_business_date_scan_time_remains_unconfirmed(self):
        day, basis = reconcile.item_day({'Date0': '2026-11-07T03:00:00Z'}, {})
        self.assertEqual((day, basis), (None, 'submission_time_unconfirmed'))

    def test_excel_serial_dates(self):
        self.assertEqual(reconcile.excel_serial_day(46332), '2026-11-06')
        self.assertIsNone(reconcile.excel_serial_day(60))


class SavedNotApplied(unittest.TestCase):
    def test_reconciliation_is_saved_on_a_new_revision_and_the_original_is_untouched(self):
        with tempfile.TemporaryDirectory() as root:
            inbox = SpreadsheetInbox(root)
            raw = sl_daily()
            item = inbox.ingest('SL Alabama - 2214 - 2026-11-06.xlsx', raw)
            items = list(ALL); items[1] = sp_item(433, inspected=72)
            out = inbox.source_reconcile(item['file_hash'], snap([items]), '2026-11-06', expected_check_hash=inbox.get(item['file_hash'])['check_hash'])
            self.assertEqual(out['source_reconciliation']['status'], 'discrepancies')
            again = SpreadsheetInbox(root)
            self.assertEqual(again.get(item['file_hash'])['source_reconciliation']['status'], 'discrepancies')
            self.assertEqual(again.download(item['file_hash'])[1], raw)
            with self.assertRaises(ValueError):   # discrepancies keep the hold unless a person overrides with a reason
                again.approve(item['file_hash'], 'Chris')


if __name__ == '__main__':
    unittest.main()


class Identity(unittest.TestCase):
    def test_same_item_id_on_another_list_is_not_a_match(self):
        s = snap([ALL])
        s['list']['url'] = f'https://ppg.sharepoint.com/sites/{SITE}/Lists/Data%202207SLALSLAL'
        r = run(sl_daily(), s)
        self.assertNotEqual(r['status'], 'verified')
        self.assertTrue(any(x['status'] == 'wrong_list' for x in r['rows']))

    def test_another_site_is_not_a_match(self):
        s = snap([ALL]); s['site'] = 'Hyundai_Montgomery'; s['list']['url'] = s['list']['url'].replace(SITE, 'Hyundai_Montgomery')
        self.assertTrue(any(x['status'] == 'wrong_list' for x in run(sl_daily(), s)['rows']))

    def test_report_values_with_no_source_column_are_not_verified(self):
        r = run(sl_daily(nok_blank=False), snap([ALL], columns=[c for c in COLS if c['name'] != 'QtyNOK']))   # report has actual zeros; source mapping is absent
        self.assertIn('rejected', r['fields_not_checked'])
        self.assertTrue(any(x['status'] == 'field_not_in_source' for x in r['rows']))
        self.assertNotEqual(r['status'], 'verified')
        self.assertEqual(r['fields_checked'], ['certified', 'inspected'])


class MissingDates(unittest.TestCase):
    def test_an_item_without_a_business_date_does_not_crash_and_keeps_coverage_incomplete(self):
        undated = sp_item(436); undated['fields'].pop('Date'); undated['fields'].pop('Date0')
        r = run(sl_daily(), snap([ALL[:4] + [undated]]))
        self.assertEqual(r['status'], 'incomplete')
        self.assertEqual(r['coverage']['undated'], 1)

    def test_timestamp_business_date_requires_verified_timezone(self):
        cols = [dict(c, dateTime=dict(format='dateTime')) if c['name'] == 'Date' else c for c in COLS]
        items = [sp_item(i, date='2026-11-06T14:30:00Z') for i in range(432, 437)]
        r = run(sl_daily(), snap([items], columns=cols))
        self.assertEqual(r['sharepoint_items_for_day'], 0)
        self.assertEqual(r['coverage']['timezone_unconfirmed'], 5)
        self.assertFalse(r['verified'])
        self.assertEqual(r['count_reconciliation']['exclusions'], [])
