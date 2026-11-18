"""Source-mapping readiness validator. Synthetic schema and rows only (fictitious DEMO-CLIENT / TEST-9001).
It must never infer a business meaning, never mark anything verified, and block on any drift or ambiguity."""
import json
import unittest
from copy import deepcopy
import demo_client_fixture as demo
import readiness_fixture as rf
from hosting import last_scan_readiness as readiness, last_scan_runner as runner, source_first as sf

F = demo.MAP['fields']


def ready():
    return rf.attested(demo.assignment())


def blocked(report):
    return set(report['blocked_reasons'])


class Base(unittest.TestCase):
    def setUp(self):
        self.a, self.schema = ready()

    def validate(self, a=None, schema=..., entries=..., rows=None):
        return readiness.validate(self.a if a is None else a, self.schema if schema is ... else schema,
                                  demo.REGISTRY if entries is ... else entries, rows)

    def with_column(self, name, **change):
        schema = deepcopy(self.schema); schema['columns'][name].update(change)
        return schema

    def retype(self, name, facet, value=None):
        schema = deepcopy(self.schema)
        for f in readiness.FACETS:
            schema['columns'][name].pop(f, None)
        if facet:
            schema['columns'][name][facet] = value or {}
        return schema


class Inventory(Base):
    def test_inventory_lists_discovered_metadata_and_assigns_no_meaning(self):
        inv = readiness.inventory(self.schema)
        by = {c['name']: c for c in inv['columns']}
        self.assertEqual((by[F['inspected']]['type'], by[F['date']]['format'], by['ActualScan']['format'], by[F['shift']]['choices']),
                         ('number', 'dateOnly', 'dateTime', ['1', '2', '3']))
        self.assertEqual({c['basis'] for c in inv['columns']}, {'discovered'})
        text = json.dumps(inv).lower()
        for word in ('verified', 'scan time', 'is_scan', 'suggest', 'likely', 'recommended'):
            self.assertNotIn(word, text)
        self.assertIn('Nothing here says what a column means', inv['note'])

    def test_a_date_time_column_is_not_offered_as_the_scan_field(self):
        a = {k: v for k, v in self.a.items() if k != 'scan_field'}
        report = self.validate(a)
        self.assertIn('scan_field_is_not_date_or_arrival_metadata', blocked(report))
        self.assertNotIn('ActualScan', json.dumps({k: v for k, v in report.items() if k != 'attestation_bindings'}))   # no suggestion


class ReadyPath(Base):
    def test_fully_pinned_and_attested_synthetic_assignment_is_ready_for_a_dry_run_only(self):
        before = (json.dumps(self.a, sort_keys=True), json.dumps(self.schema, sort_keys=True))
        report = self.validate(rows=[r['fields'] for r in demo.items(other_day=False)])
        self.assertEqual((report['blocked_reasons'], report['ready_for_dry_run'], report['status'], report['marked_verified_by_this_tool']),
                         ([], True, 'ready_for_dry_run_only', False))
        self.assertIn('not production-ready', report['note'])
        self.assertEqual((json.dumps(self.a, sort_keys=True), json.dumps(self.schema, sort_keys=True)), before)   # inputs untouched
        self.assertEqual({c['basis'] for c in report['checks']}, {'configured', 'discovered', 'attested'})
        text = readiness.report_text(report)
        self.assertIn('READY FOR A DRY RUN ONLY', text); self.assertIn('Nothing was read, changed, approved or marked verified', text)

    def test_readiness_does_not_switch_anything_on(self):
        self.validate()
        self.assertEqual(runner.run_once(None, dict(assignments=[self.a]), fetch=None, render=None, run_id='x'), dict(status='disabled'))


class NothingIsVerifiedAutomatically(Base):
    def test_without_attestations_every_business_meaning_is_blocked_with_what_is_needed(self):
        a = {k: v for k, v in self.a.items() if k != 'attestations'}
        report = self.validate(a)
        for semantic in readiness.SEMANTICS:
            self.assertIn(semantic + '_attested', blocked(report))
        needed = {m['check']: m['needed'] for m in report['missing_evidence']}
        self.assertIn('not when the row was created, saved or synced', needed['scan_time_meaning_attested'])
        self.assertIn('bound_to=' + report['attestation_bindings']['scan_time_meaning'], needed['scan_time_meaning_attested'])
        self.assertIn('verified_flag_is_backed_by_attestations', blocked(report))
        self.assertIn('timestamp_semantics_verified_flag_is_backed_by_attestations', blocked(report))
        self.assertNotIn('attestations', report); self.assertNotIn('attestations', a)        # the tool wrote none
        self.assertIn('BLOCKED  scan_time_meaning_attested', readiness.report_text(report))

    def test_flags_alone_or_incomplete_or_borrowed_attestations_do_not_count(self):
        good = self.a['attestations']['scan_time_meaning']
        other, _ = rf.attested(demo.assignment(scan_field='OtherScan'), rf.schema_for(demo.assignment(scan_field='OtherScan')))
        cases = dict(true=True, text='verified', empty={}, no_name=dict(good, attested_by=''), no_evidence=dict(good, evidence=' '),
                     no_date=dict(good, attested_at=None), no_binding={k: v for k, v in good.items() if k != 'bound_to'},
                     wrong_binding=dict(good, bound_to='0' * 64), borrowed=other['attestations']['scan_time_meaning'])
        for name, record in cases.items():
            a = deepcopy(self.a); a['attestations']['scan_time_meaning'] = record
            self.assertIn('scan_time_meaning_attested', blocked(self.validate(a)), name)

    def test_changing_what_was_attested_makes_the_attestation_stale(self):
        changes = dict(scan_time_meaning=dict(scan_field='OtherScan'), timezone=dict(zone='America/New_York'),
                       shift_values=dict(recorded_shifts=['1', '2']), identity_and_registry=dict(job='TEST-9002'),
                       template_and_header=dict(header=dict(demo.HEADER, worksite='Somewhere else')))
        for semantic, change in changes.items():
            a = dict(deepcopy(self.a), **change)
            self.assertIn(semantic + '_attested', blocked(self.validate(a)), semantic)
        a = deepcopy(self.a); a['field_contract'][ 'inspected']['column_id'] = 'dddddddd-0000-4000-8000-999999999999'
        self.assertIn('quantities_attested', blocked(self.validate(a)))


class Drift(Base):
    def test_replaced_column_is_detected_by_its_id(self):
        report = self.validate(schema=self.with_column(F['inspected'], id='dddddddd-0000-4000-8000-999999999999'))
        self.assertEqual(blocked(report), {'inspected_column_id_unchanged', 'inspected_definition_unchanged'})
        report = self.validate(schema=self.with_column('ActualScan', id='dddddddd-0000-4000-8000-999999999998'))
        self.assertEqual(blocked(report), {'scan_field_column_id_unchanged', 'scan_field_definition_unchanged'})

    def test_re_pinning_to_the_new_column_needs_a_new_attestation(self):
        schema = self.with_column('ActualScan', id='dddddddd-0000-4000-8000-999999999998')
        a = rf.pin(self.a, schema)                                          # contract follows the schema; old attestations kept
        self.assertEqual(blocked(self.validate(a, schema)), {'scan_time_meaning_attested', 'template_and_header_attested', 'verified_flag_is_backed_by_attestations',
                                                             'timestamp_semantics_verified_flag_is_backed_by_attestations'})

    def test_changed_types_are_detected(self):
        self.assertIn('inspected_type_unchanged', blocked(self.validate(schema=self.retype(F['inspected'], 'text'))))
        scan = self.retype('ActualScan', 'dateTime', {'format': 'dateOnly'})
        self.assertTrue({'scan_field_type_unchanged', 'scan_field_is_a_date_and_time'} <= blocked(self.validate(schema=scan)))
        self.assertTrue({'rejected_type_unchanged', 'rejected_type_supported'} <= blocked(self.validate(schema=self.retype(F['rejected'], 'lookup'))))
        self.assertTrue({'certified_type_unchanged', 'certified_type_known'} <= blocked(self.validate(schema=self.retype(F['certified'], None))))
        self.assertIn('date_is_a_date_only_column', blocked(self.validate(schema=self.retype(F['date'], 'dateTime', {'format': 'dateTime'}))))

    def test_removed_or_renamed_column_is_detected(self):
        schema = deepcopy(self.schema); schema['columns']['Scanned'] = schema['columns'].pop('ActualScan'); schema['columns']['Scanned']['name'] = 'Scanned'
        self.assertIn('scan_field_column_exists', blocked(self.validate(schema=schema)))
        schema = deepcopy(self.schema); del schema['columns'][F['identifier']]
        self.assertIn('identifier_column_exists', blocked(self.validate(schema=schema)))

    def test_shift_choices_added_or_removed(self):
        more = self.retype(F['shift'], 'choice', {'choices': ['1', '2', '3', 'Night']})
        self.assertEqual(blocked(self.validate(schema=more)), {'every_shift_choice_is_covered', 'shift_definition_unchanged'})
        fewer = self.retype(F['shift'], 'choice', {'choices': ['1', '2']})
        self.assertEqual(blocked(self.validate(schema=fewer)), {'recorded_shifts_exist_as_choices', 'shift_definition_unchanged'})

    def test_schema_digest_changes_with_any_of_it(self):
        base = readiness.schema_digest(self.schema)
        for schema in (self.with_column(F['part'], id='x'), self.retype(F['part'], 'number'), self.with_column(F['part'], displayName='Part no.')):
            self.assertNotEqual(readiness.schema_digest(schema), base)


class MismatchedSource(Base):
    def test_schema_of_another_site_or_list_is_refused(self):
        for change in (dict(list_id='ffffffff-0000-4000-8000-000000009001'), dict(site_id=demo.SITE.replace('aaaaaaaa', 'eeeeeeee')),
                       dict(list=dict(id='ffffffff-0000-4000-8000-000000009001'))):
            self.assertIn('schema_is_for_this_site_and_list', blocked(self.validate(schema=dict(self.schema, **change))), change)

    def test_no_schema_means_nothing_discovered_can_pass(self):
        for schema in (None, {}, dict(self.schema, columns={}), 'schema'):
            report = self.validate(schema=schema)
            self.assertIn('schema_snapshot_supplied', blocked(report)); self.assertFalse(report['ready_for_dry_run'])

    def test_names_and_urls_are_not_identifiers(self):
        for change in (dict(site_id='DemoClient_Plant1'), dict(site_id='https://example.sharepoint.com/sites/DemoClient'), dict(list_id='TEST-9001 list'),
                       dict(site_id=demo.HOST + ',not-a-guid,' + demo.SITE.split(',')[2])):
            self.assertIn('site_and_list_ids_are_exact_identifiers', blocked(self.validate(dict(self.a, **change))), change)

    def test_registry_must_match_exactly(self):
        for entries, code in ((None, 'exact_verified_registry_entry'), ([], 'exact_verified_registry_entry'),
                              ([dict(demo.REGISTRY[0], client_key='OTHER-CLIENT')], 'exact_verified_registry_entry'),
                              ([dict(demo.REGISTRY[0], job='9001')], 'exact_verified_registry_entry'),
                              ([demo.REGISTRY[0], dict(demo.REGISTRY[0], client_key='SECOND')], 'exact_verified_registry_entry')):
            self.assertIn(code, blocked(self.validate(entries=entries)))


class AmbiguousFields(Base):
    def test_two_columns_sharing_a_display_name_block_the_mapping(self):
        schema = deepcopy(self.schema)
        schema['columns']['Inspected0'] = dict(schema['columns'][F['inspected']], name='Inspected0', id='dddddddd-0000-4000-8000-777777777777')
        report = self.validate(schema=schema)
        self.assertEqual(blocked(report), {'inspected_column_unambiguous'})
        self.assertTrue(readiness.inventory(schema)['columns'][[c['name'] for c in readiness.inventory(schema)['columns']].index('Inspected0')]['display_name_shared'])
        needed = {m['check']: m['needed'] for m in report['missing_evidence']}
        self.assertIn('by column ID', needed['inspected_column_unambiguous'])

    def test_a_column_whose_display_name_is_another_columns_name(self):
        schema = self.with_column(F['certified'], displayName=F['rejected'])        # "Certified" shown to people as "Rejected"
        self.assertIn('rejected_column_unambiguous', blocked(self.validate(schema=schema)))

    def test_one_column_mapped_to_two_fields_or_a_field_left_unmapped(self):
        a = deepcopy(self.a); a['mapping']['fields']['certified'] = a['mapping']['fields']['inspected']
        self.assertIn('no_column_mapped_to_two_report_fields', blocked(self.validate(a)))
        a = deepcopy(self.a); del a['mapping']['fields']['rejected']
        self.assertIn('mapping_covers_every_report_field', blocked(self.validate(a)))

    def test_unpinned_fields_are_blocked(self):
        a = deepcopy(self.a); del a['field_contract']['rejected']
        self.assertIn('rejected_pinned_by_column_id_and_type', blocked(self.validate(a)))
        a = {k: v for k, v in self.a.items() if k != 'field_contract'}
        self.assertTrue({'scan_field_pinned_by_column_id_and_type', 'date_pinned_by_column_id_and_type'} <= blocked(self.validate(a)))


class ShiftAndTimezone(Base):
    def test_shift_values_must_be_exact_listed_once_and_mapped_to_template_shifts(self):
        for shifts, code in (([], 'recorded_shifts_listed'), (['1', '1'], 'recorded_shifts_listed'), (['1', ' 2'], 'recorded_shifts_listed'),
                             ('1,2,3', 'recorded_shifts_listed'), (['1', '2', 'Night'], 'every_recorded_shift_maps_to_a_template_shift')):
            self.assertIn(code, blocked(self.validate(dict(self.a, recorded_shifts=shifts))), shifts)
        a = deepcopy(self.a); a['mapping']['shifts']['3'] = 'Late shift'
        self.assertIn('every_recorded_shift_maps_to_a_template_shift', blocked(self.validate(a)))

    def test_shift_field_must_be_the_mapped_shift_column(self):
        self.assertIn('shift_field_is_the_mapped_shift_column', blocked(self.validate(dict(self.a, shift_field=F['location']))))

    def test_sample_rows_with_other_or_missing_shift_values_block(self):
        rows = [r['fields'] for r in demo.items(other_day=False)]
        rows[3] = dict(rows[3], **{F['shift']: 'Night'}); rows[4] = dict(rows[4], **{F['shift']: None})
        report = self.validate(rows=rows)
        self.assertTrue({'sample_shift_values_are_all_recorded_shifts', 'sample_rows_all_have_a_shift'} <= blocked(report))

    def test_timezone_must_be_a_real_zone(self):
        for zone in (None, '', 'Central', 'CST', 'UTC-6', ' America/Chicago'):
            self.assertIn('timezone_is_a_real_zone', blocked(self.validate(dict(self.a, zone=zone))), zone)


class ScanTimeIsNeverInferred(Base):
    def test_date_created_modified_are_refused_and_no_attestation_can_rescue_them(self):
        for name in ('Created', 'Modified', 'createdDateTime', 'lastModifiedDateTime', 'Date', 'Date0', F['date'], '', None):
            a, _ = rf.attested(dict(demo.assignment(), scan_field=name), self.schema)
            report = self.validate(a)
            self.assertIn('scan_field_is_not_date_or_arrival_metadata', blocked(report), name)
            self.assertFalse(report['ready_for_dry_run'])

    def test_computed_or_read_only_scan_columns_are_refused(self):
        self.assertIn('scan_field_is_entered_not_computed', blocked(self.validate(schema=self.with_column('ActualScan', readOnly=True))))
        self.assertTrue({'scan_field_type_supported', 'scan_field_type_unchanged'} <= blocked(self.validate(schema=self.retype('ActualScan', 'calculated'))))
        self.assertIn('scan_field_type_supported', blocked(self.validate(schema=self.retype('ActualScan', 'text'))))

    def test_sample_rows_where_scan_equals_created_are_a_red_flag(self):
        rows = []
        for r in demo.items(other_day=False):
            f = dict(r['fields']); f['Created'] = f['ActualScan']; rows.append(f)
        report = self.validate(rows=rows)
        self.assertEqual(blocked(report), {'scan_values_differ_from_created'})
        self.assertIn('how the scan field is filled in on the tablet', report['missing_evidence'][0]['needed'])

    def test_sample_rows_differing_from_created_prove_nothing_by_themselves(self):
        rows = [dict(r['fields'], Created='2026-11-10T12:00:00Z') for r in demo.items(other_day=False)]
        a = deepcopy(self.a); del a['attestations']['scan_time_meaning']
        report = self.validate(a, rows=rows)
        self.assertIn('scan_time_meaning_attested', blocked(report))                # looking plausible is not an attestation
        self.assertIn('never evidence', report['sample_observations'][-1]['note'])

    def test_naive_or_missing_sample_scan_values_block(self):
        rows = [r['fields'] for r in demo.items(other_day=False)]
        rows[0] = dict(rows[0], ActualScan='2026-11-10T20:30:00'); rows[1] = dict(rows[1], ActualScan=None)
        self.assertIn('sample_scan_values_are_offset_aware_times', blocked(self.validate(rows=rows)))


class Offline(unittest.TestCase):
    def test_validator_has_no_network_database_environment_or_file_access(self):
        import pathlib
        text = (pathlib.Path(readiness.__file__)).read_text()
        for forbidden in ('urllib', 'requests', 'http.client', 'socket', 'subprocess', 'psycopg', 'sqlite', 'os.environ', 'getenv',
                          'open(', 'run_once', '.tick(', 'fetch', 'deliver', 'dispatch'):
            self.assertNotIn(forbidden, text, forbidden)


class TemplateAndUnknowns(Base):
    def test_unknown_header_field_blocks(self):
        self.assertIn('header_uses_only_template_fields', blocked(self.validate(dict(self.a, header=dict(demo.HEADER, logo='x')))))

    def test_unknown_quantities_in_the_sample_are_reported_not_judged(self):
        report = self.validate(rows=[r['fields'] for r in demo.items(other_day=False)])
        by = {o.get('field'): o for o in report['sample_observations']}
        self.assertEqual((by['rejected']['absent_or_blank'], by['rejected']['rows'], by['inspected']['absent_or_blank']), (36, 36, 0))
        self.assertEqual(report['blocked_reasons'], [])                             # an unknown value is a finding in the report, not a mapping fault

    def test_garbage_input_never_raises_and_is_never_ready(self):
        for a in (None, {}, 'assignment', dict(mapping='x'), dict(mapping=dict(fields=[1])), dict(self.a, mapping=None), dict(self.a, attestations='yes')):
            report = readiness.validate(a, self.schema, demo.REGISTRY)
            self.assertFalse(report['ready_for_dry_run'], a)


if __name__ == '__main__':
    unittest.main()
