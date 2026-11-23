"""Proposal: plant reporting time zones handled in the app (midnight and daylight-saving cases). Made-up values only."""
import re
import unittest
from pathlib import Path

from hosting import plant_time as pt

CENTRAL = dict(Id=11, Description='(UTC-06:00) Central Time (US and Canada)', Information=dict(Bias=360, StandardBias=0, DaylightBias=-60))
PACIFIC = dict(Id=13, Description='(UTC-08:00) Pacific Time (US and Canada)', Information=dict(Bias=480, StandardBias=0, DaylightBias=-60))


class StoredDates(unittest.TestCase):
    def test_a_site_left_on_pacific_gives_the_date_that_was_picked(self):
        # Lear T1 and Briggs Auburn: SharePoint stored "Nov 19" as midnight Pacific
        for site in ('Lear_T1', 'BriggsStratton_Auburn'):
            got = pt.business_date('2026-11-19T08:00:00Z', site, PACIFIC)
            self.assertEqual((got['date'], got['plant_zone'], got['storage_zone']), ('2026-11-19', 'America/Chicago', 'America/Los_Angeles'))
        self.assertEqual(pt.business_date('2026-07-15T07:00:00Z', 'Lear_T1', PACIFIC)['date'], '2026-07-15')        # daylight time: seven hours

    def test_reading_a_pacific_value_as_central_is_refused_which_is_what_happens_today(self):
        self.assertIsNone(pt.stored_date('2026-11-19T08:00:00Z', CENTRAL))            # 02:00 Central: not a date-only value
        self.assertEqual(pt.stored_date('2026-11-19T06:00:00Z', CENTRAL), '2026-11-19')
        self.assertEqual(pt.business_date('2026-11-19T06:00:00Z', 'SLAlabama_AlexanderCity', CENTRAL)['date'], '2026-11-19')

    def test_the_date_is_not_shifted_into_the_plant_zone(self):
        # midnight Pacific is 02:00 at the plant on the same date; one second earlier would be the previous date in Pacific
        self.assertEqual(pt.business_date('2026-11-19T08:00:00Z', 'Lear_T1', PACIFIC)['date'], '2026-11-19')
        self.assertIsNone(pt.business_date('2026-11-19T07:59:59Z', 'Lear_T1', PACIFIC))

    def test_midnight_exactly_and_nothing_near_it(self):
        for value in ('2026-11-19T08:00:01Z', '2026-11-19T08:00:00.500Z', '2026-11-19T08:01:00Z', '2026-11-19T07:00:00Z', '2026-11-19T08:00:00', '2026-11-19', None, 20261119):
            self.assertIsNone(pt.stored_date(value, PACIFIC), value)
        self.assertEqual(pt.stored_date('2026-11-19T08:00:00.000Z', PACIFIC), '2026-11-19')
        self.assertEqual(pt.stored_date('2026-11-19T00:00:00-08:00', PACIFIC), '2026-11-19')

    def test_dates_around_the_spring_and_autumn_clock_changes(self):
        # 2026: clocks go forward on March 8 and back on November 1, at 02:00 local, so midnight itself always exists once
        cases = [('2026-03-08T08:00:00Z', PACIFIC, '2026-03-08'), ('2026-03-09T07:00:00Z', PACIFIC, '2026-03-09'),
                 ('2026-11-01T07:00:00Z', PACIFIC, '2026-11-01'), ('2026-11-02T08:00:00Z', PACIFIC, '2026-11-02'),
                 ('2026-03-08T06:00:00Z', CENTRAL, '2026-03-08'), ('2026-03-09T05:00:00Z', CENTRAL, '2026-03-09'),
                 ('2026-11-01T05:00:00Z', CENTRAL, '2026-11-01'), ('2026-11-02T06:00:00Z', CENTRAL, '2026-11-02')]
        for value, evidence, want in cases:
            self.assertEqual(pt.stored_date(value, evidence), want, value)
        for value, evidence in (('2026-03-09T08:00:00Z', PACIFIC), ('2026-11-01T08:00:00Z', PACIFIC), ('2026-03-09T06:00:00Z', CENTRAL), ('2026-11-01T06:00:00Z', CENTRAL)):
            self.assertIsNone(pt.stored_date(value, evidence), value)                  # the winter offset on a summer date, and the reverse: an hour off midnight

    def test_evidence_must_be_exactly_a_known_setting(self):
        self.assertEqual((pt.storage_zone(CENTRAL), pt.storage_zone(PACIFIC)), ('America/Chicago', 'America/Los_Angeles'))
        for bad in (dict(PACIFIC, Id=10), dict(PACIFIC, Id='13'), dict(PACIFIC, Id=True), dict(PACIFIC, Description='Pacific'),
                    dict(PACIFIC, Information=dict(Bias=360, StandardBias=0, DaylightBias=-60)), dict(PACIFIC, Information=dict(Bias=480.0, StandardBias=0, DaylightBias=-60)),
                    dict(PACIFIC, Information=None), dict(Id=13), None, 'Pacific', dict(CENTRAL, Id=13)):
            self.assertIsNone(pt.storage_zone(bad), str(bad))
            self.assertIsNone(pt.business_date('2026-11-19T08:00:00Z', 'Lear_T1', bad))

    def test_a_site_without_a_confirmed_plant_zone_gets_no_date(self):
        for site in ('BriggsStratton_Statesboro', 'Lucid_CasaGrande', 'PrattWhitney_Atlanta', 'lear_t1', '', None):
            self.assertIsNone(pt.business_date('2026-11-19T08:00:00Z', site, PACIFIC), site)
            self.assertIsNone(pt.plant_today('2026-11-19T12:00:00Z', site)); self.assertIsNone(pt.plant_day('2026-11-19', site))
        self.assertEqual(sorted(pt.PLANT_ZONES), ['BriggsStratton_Auburn', 'Hyundai_Montgomery', 'Lear_T1', 'SLAlabama_AlexanderCity'])


class ThePlantDay(unittest.TestCase):
    def test_today_changes_at_the_plants_midnight_not_sharepoints(self):
        self.assertEqual(pt.plant_today('2026-11-20T05:59:59Z', 'Lear_T1'), '2026-11-19')        # 11:59:59 PM in Tuscaloosa
        self.assertEqual(pt.plant_today('2026-11-20T06:00:00Z', 'Lear_T1'), '2026-11-20')        # midnight in Tuscaloosa; still the 19th in Pacific
        self.assertEqual(pt.plant_today('2026-07-15T04:59:59Z', 'BriggsStratton_Auburn'), '2026-07-14')
        self.assertEqual(pt.plant_today('2026-07-15T05:00:00Z', 'BriggsStratton_Auburn'), '2026-07-15')
        self.assertIsNone(pt.plant_today('2026-11-20T06:00:00', 'Lear_T1'))                      # no offset: not an instant

    def test_a_plant_day_is_23_24_or_25_hours(self):
        usual = pt.plant_day('2026-11-19', 'Lear_T1')
        self.assertEqual((usual['start_utc'], usual['end_utc'], usual['hours']), ('2026-11-19T06:00:00+00:00', '2026-11-20T06:00:00+00:00', 24.0))
        spring, autumn = pt.plant_day('2026-03-08', 'Lear_T1'), pt.plant_day('2026-11-01', 'Lear_T1')
        self.assertEqual((spring['start_utc'], spring['end_utc'], spring['hours']), ('2026-03-08T06:00:00+00:00', '2026-03-09T05:00:00+00:00', 23.0))
        self.assertEqual((autumn['start_utc'], autumn['end_utc'], autumn['hours']), ('2026-11-01T05:00:00+00:00', '2026-11-02T06:00:00+00:00', 25.0))
        for bad in ('2026-02-30', '11/19/2026', '2026-11-19T00:00', None):
            self.assertIsNone(pt.plant_day(bad, 'Lear_T1'))

    def test_a_late_scan_is_in_the_plant_day_it_happened_in(self):
        day = pt.plant_day('2026-11-19', 'Lear_T1')
        inside = lambda stamp: day['start_utc'] <= stamp < day['end_utc']
        self.assertTrue(inside('2026-11-20T05:30:00+00:00'))                    # 11:30 PM on the 19th at the plant
        self.assertFalse(inside('2026-11-20T06:00:00+00:00'))                   # midnight: the 20th
        self.assertFalse(inside('2026-11-19T05:59:59+00:00'))                   # 11:59 PM on the 18th


class NotConnected(unittest.TestCase):
    def test_nothing_uses_the_proposal_yet_and_it_reads_no_clock_or_network(self):
        root = Path(__file__).resolve().parents[1]
        for path in [p for d in ('api', 'web', 'hosting') if (root / d).is_dir() for p in (root / d).rglob('*') if p.suffix in ('.py', '.js')]:
            if path.name != 'plant_time.py':
                self.assertNotIn('plant_time', path.read_text(errors='ignore'), str(path))
        source = (root / 'hosting' / 'plant_time.py').read_text()
        self.assertEqual(sorted(re.findall(r'^(?:from|import) +([\w.]+)', source, re.M)), ['datetime', 're', 'zoneinfo'])
        for word in ('.now(', 'utcnow', 'urlopen', 'requests', 'open('):
            self.assertNotIn(word, source, word)


if __name__ == '__main__':
    unittest.main()
