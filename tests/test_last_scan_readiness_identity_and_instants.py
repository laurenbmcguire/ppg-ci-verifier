"""Regressions found by independent review of 99339c7: two adjacent false-ready cases.
(1) a column whose own internal name contradicts the key it is filed under could be re-pinned and attested to ready;
(2) a scan value equal to Created as an instant, but written with another offset, was not flagged.
Synthetic schema and rows only (fictitious DEMO-CLIENT / TEST-9001)."""
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import demo_client_fixture as demo
import readiness_fixture as rf
from hosting import last_scan_readiness as readiness

F = demo.MAP['fields']
ROWS = [r['fields'] for r in demo.items(other_day=False)]
CENTRAL, INDIA = timezone(timedelta(hours=-6)), timezone(timedelta(hours=5, minutes=30))


def utc(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(timezone.utc)


class Base(unittest.TestCase):
    def setUp(self):
        self.a, self.schema = rf.attested(demo.assignment())
        self.assertTrue(readiness.validate(self.a, self.schema, demo.REGISTRY, ROWS)['ready_for_dry_run'])     # baseline is ready

    def report(self, schema=None, rows=None, repin=True):
        schema = self.schema if schema is None else schema
        a = rf.attested(demo.assignment(), schema)[0] if repin else self.a      # freshly pinned AND attested against this schema
        return readiness.validate(a, schema, demo.REGISTRY, rows)

    def blocked(self, **kw):
        report = self.report(**kw)
        self.assertFalse(report['ready_for_dry_run'], 'FALSE READY'); self.assertEqual(report['status'], 'blocked')
        self.assertIsInstance(readiness.report_text(report), str)
        return set(report['blocked_reasons'])

    def column(self, key, /, **change):
        schema = deepcopy(self.schema); schema['columns'][key].update(change)
        return schema


class SourceIdentityCannotBeRepinnedAway(Base):
    def test_review_repro_scan_column_internal_name_differs_from_its_key(self):
        got = self.blocked(schema=self.column('ActualScan', name='DifferentInternalName'))
        self.assertTrue({'scan_field_column_name_matches_source', 'schema_column_identities_are_consistent'} <= got, got)

    def test_scan_key_filed_over_arrival_metadata(self):
        for real in ('Created', 'Modified', F['date']):
            self.assertIn('scan_field_column_name_matches_source', self.blocked(schema=self.column('ActualScan', name=real)), real)

    def test_name_missing_blank_wrong_case_padded_or_not_text(self):
        for value in (None, '', ' ', 'actualscan', 'ACTUALSCAN', ' ActualScan', 'ActualScan ', 7, ['ActualScan'], {'n': 1}):
            self.assertIn('scan_field_column_name_matches_source', self.blocked(schema=self.column('ActualScan', name=value)), repr(value))
        schema = deepcopy(self.schema); del schema['columns']['ActualScan']['name']
        self.assertIn('scan_field_column_name_matches_source', self.blocked(schema=schema))

    def test_every_mapped_field_is_held_to_it(self):
        for field in ('date', 'shift', 'inspected', 'rejected', 'certified', 'part', 'identifier'):
            got = self.blocked(schema=self.column(F[field], name='SomethingElse'))
            self.assertIn(field + '_column_name_matches_source', got, field)

    def test_without_repinning_it_is_also_caught(self):
        got = self.blocked(schema=self.column('ActualScan', name='DifferentInternalName'), repin=False)
        self.assertTrue({'scan_field_column_name_matches_source', 'scan_field_definition_unchanged'} <= got, got)

    def test_unmapped_column_with_a_contradictory_name_makes_the_snapshot_untrustworthy(self):
        schema = deepcopy(self.schema)
        schema['columns']['Spare'] = dict(name='ActualScan', displayName='Spare', id='dddddddd-0000-4000-8000-555555555555', text={})
        self.assertIn('schema_column_identities_are_consistent', self.blocked(schema=schema))

    def test_two_columns_with_one_id(self):
        schema = deepcopy(self.schema)
        schema['columns']['Spare'] = dict(name='Spare', displayName='Spare', id=schema['columns']['ActualScan']['id'], dateTime={'format': 'dateTime'})
        got = self.blocked(schema=schema)
        self.assertTrue({'scan_field_column_id_is_unique', 'schema_column_identities_are_consistent'} <= got, got)
        schema = self.column(F['inspected'], id=self.schema['columns'][F['rejected']]['id'])
        got = self.blocked(schema=schema)
        self.assertTrue({'inspected_column_id_is_unique', 'rejected_column_id_is_unique'} <= got, got)

    def test_column_without_an_id(self):
        schema = deepcopy(self.schema); del schema['columns']['ActualScan']['id']
        self.assertIn('schema_column_identities_are_consistent', self.blocked(schema=schema))

    def test_keys_that_differ_only_by_case_or_padding(self):
        for twin in ('actualscan', 'ACTUALSCAN', 'ActualScan '):
            schema = deepcopy(self.schema)
            schema['columns'][twin] = dict(name=twin, displayName='Twin', id='dddddddd-0000-4000-8000-444444444444', dateTime={'format': 'dateTime'})
            self.assertIn('schema_column_identities_are_consistent', self.blocked(schema=schema), repr(twin))

    def test_schema_site_and_list_identity_cannot_contradict_itself(self):
        other = 'ffffffff-0000-4000-8000-000000009001'
        self.assertIn('schema_is_for_this_site_and_list', self.blocked(schema=dict(self.schema, list=dict(id=other))))
        self.assertIn('schema_is_for_this_site_and_list', self.blocked(schema=dict(self.schema, list=dict(id=['x']))))

    def test_inventory_shows_the_mismatch(self):
        inv = readiness.inventory(self.column('ActualScan', name='DifferentInternalName'))
        row = [c for c in inv['columns'] if c['name'] == 'ActualScan'][0]
        self.assertEqual((row['internal_name'], row['name_matches_key']), ('DifferentInternalName', False))


class EquivalentInstants(Base):
    def rows(self, arrival='Created', scan=lambda d: d.astimezone(CENTRAL).isoformat(), other=lambda d: d.isoformat().replace('+00:00', 'Z')):
        out = []
        for r in ROWS:
            d = utc(r['ActualScan']).replace(microsecond=0)      # whole seconds, so every written form below is the same instant
            out.append(dict(r, **{'ActualScan': scan(d), arrival: other(d)}))
        return out

    def test_review_repro_central_offset_scan_equals_created_in_z(self):
        rows = self.rows()
        self.assertNotEqual(rows[0]['ActualScan'], rows[0]['Created'])           # different text
        self.assertEqual(utc(rows[0]['ActualScan']), utc(rows[0]['Created']))    # same instant
        self.assertTrue(rows[0]['ActualScan'].endswith('-06:00') and rows[0]['Created'].endswith('Z'))
        self.assertEqual(self.blocked(rows=rows), {'scan_values_differ_from_created'})

    def test_alternate_offsets_and_fractional_forms(self):
        forms = dict(z=lambda d: d.strftime('%Y-%m-%dT%H:%M:%SZ'), plus_zero=lambda d: d.strftime('%Y-%m-%dT%H:%M:%S+00:00'),
                     millis_z=lambda d: d.strftime('%Y-%m-%dT%H:%M:%S.000Z'), micros=lambda d: d.strftime('%Y-%m-%dT%H:%M:%S.000000+00:00'),
                     central=lambda d: d.astimezone(CENTRAL).isoformat(), india=lambda d: d.astimezone(INDIA).isoformat(),
                     central_millis=lambda d: d.astimezone(CENTRAL).strftime('%Y-%m-%dT%H:%M:%S.000-06:00'))
        for s_name, scan in forms.items():
            for o_name, other in forms.items():
                got = self.blocked(rows=self.rows(scan=scan, other=other))
                self.assertEqual(got, {'scan_values_differ_from_created'}, (s_name, o_name))

    def test_other_arrival_fields(self):
        for arrival in ('Modified', 'createdDateTime', 'lastModifiedDateTime'):
            self.assertEqual(self.blocked(rows=self.rows(arrival=arrival)), {'scan_values_differ_from_' + arrival.lower()}, arrival)

    def test_unequal_instants_are_not_flagged(self):
        z = lambda d: d.isoformat().replace('+00:00', 'Z')
        controls = dict(second_later=lambda d: z(d + timedelta(seconds=1)), millisecond_later=lambda d: z(d + timedelta(milliseconds=1)),
                        hour_earlier=lambda d: z(d - timedelta(hours=1)),
                        same_wall_clock_other_zone=lambda d: d.astimezone(CENTRAL).replace(tzinfo=timezone.utc).isoformat())   # 15:00Z is not 15:00-06:00
        for name, other in controls.items():
            report = self.report(rows=self.rows(other=other))
            self.assertEqual(report['blocked_reasons'], [], name); self.assertTrue(report['ready_for_dry_run'], name)

    def test_one_differing_row_is_enough_to_not_flag(self):
        rows = self.rows(); rows[5] = dict(rows[5], Created='2026-11-10T23:59:59Z')
        self.assertEqual(self.report(rows=rows)['blocked_reasons'], [])

    def test_no_timezone_is_assumed_for_naive_or_invalid_arrival_values(self):
        naive = lambda d: d.astimezone(CENTRAL).replace(tzinfo=None).isoformat()      # same wall clock as the scan, no offset
        for name, other in dict(naive_local=naive, naive_utc=lambda d: d.replace(tzinfo=None).isoformat(), date_only=lambda d: d.date().isoformat(),
                                words=lambda d: 'n/a', number=lambda d: 5, structured=lambda d: {'value': d.isoformat()}).items():
            report = self.report(rows=self.rows(other=other))
            self.assertNotIn('scan_values_differ_from_created', report['blocked_reasons'], name)
            notes = [o for o in report['sample_observations'] if o.get('arrival_field') == 'Created']
            self.assertEqual((notes[0]['compared'], notes[0]['not_comparable']), (0, len(ROWS)), name)   # said out loud, not guessed

    def test_identical_text_is_still_flagged_even_when_it_cannot_be_parsed(self):
        rows = [dict(r, ActualScan='2026-11-10T15:00:00', Created='2026-11-10T15:00:00') for r in ROWS]   # naive on both sides
        got = self.blocked(rows=rows)
        self.assertTrue({'scan_values_differ_from_created', 'sample_scan_values_are_offset_aware_times'} <= got, got)

    def test_observation_counts_equal_instants(self):
        note = [o for o in self.report(rows=self.rows())['sample_observations'] if o.get('arrival_field') == 'Created'][0]
        self.assertEqual((note['compared'], note['same_instant'], note['not_comparable']), (len(ROWS), len(ROWS), 0))


if __name__ == '__main__':
    unittest.main()
