"""Regressions found by independent QA on 77d9548 (false readiness, and AttributeError on nested malformed input).
Synthetic schema and rows only (fictitious DEMO-CLIENT / TEST-9001)."""
import unittest
from copy import deepcopy
from unittest import mock
import demo_client_fixture as demo
import readiness_fixture as rf
from hosting import last_scan_readiness as readiness, last_scan_runner as runner

F = demo.MAP['fields']
ROWS = [r['fields'] for r in demo.items(other_day=False)]


class Base(unittest.TestCase):
    def setUp(self):
        self.a, self.schema = rf.attested(demo.assignment())
        self.assertTrue(readiness.validate(self.a, self.schema, demo.REGISTRY)['ready_for_dry_run'])     # the baseline really is ready

    def changed(self, name, **change):
        schema = deepcopy(self.schema); schema['columns'][name].update(change)
        return schema

    def blocked(self, a=None, schema=None, entries=..., rows=None):
        report = readiness.validate(self.a if a is None else a, self.schema if schema is None else schema,
                                    demo.REGISTRY if entries is ... else entries, rows)
        self.assertFalse(report['ready_for_dry_run']); self.assertEqual(report['status'], 'blocked')
        self.assertTrue(report['blocked_reasons']); self.assertIsInstance(readiness.report_text(report), str)
        return set(report['blocked_reasons'])


class CalculatedAndContradictoryFacets(Base):
    def test_qa_repro_calculated_facet_beside_the_original_datetime_facet(self):
        schema = self.changed('ActualScan', calculated={'formula': '=[Created]', 'outputType': 'dateTime'})
        got = self.blocked(schema=schema)
        self.assertTrue({'scan_field_is_not_calculated', 'scan_field_type_facets_consistent', 'scan_field_definition_unchanged'} <= got, got)
        self.assertNotEqual(readiness.schema_digest(schema), readiness.schema_digest(self.schema))

    def test_any_calculated_facet_on_the_scan_column_whatever_its_value(self):
        for value in ({}, None, 'x', [], 0, False, {'formula': '=[Modified]'}):
            got = self.blocked(schema=self.changed('ActualScan', calculated=value))
            self.assertIn('scan_field_is_not_calculated', got, repr(value))

    def test_repinning_a_calculated_scan_column_cannot_make_it_ready(self):
        schema = self.changed('ActualScan', calculated={'formula': '=[Created]', 'outputType': 'dateTime'})
        a, _ = rf.attested(demo.assignment(), schema)                       # even freshly pinned AND attested
        self.assertIn('scan_field_is_not_calculated', self.blocked(a, schema))
        only = deepcopy(self.schema); del only['columns']['ActualScan']['dateTime']; only['columns']['ActualScan']['calculated'] = {'outputType': 'dateTime'}
        a, _ = rf.attested(demo.assignment(), only)
        self.assertIn('scan_field_is_not_calculated', self.blocked(a, only))

    def test_calculated_facet_on_any_mapped_column(self):
        for field in ('inspected', 'rejected', 'shift', 'date', 'part'):
            got = self.blocked(schema=self.changed(F[field], calculated={'formula': '=1'}))
            self.assertIn(field + '_is_not_calculated', got, field)

    def test_contradictory_type_facets(self):
        for name, label, extra in (('ActualScan', 'scan_field', dict(text={})), ('ActualScan', 'scan_field', dict(number={})),
                                   (F['inspected'], 'inspected', dict(text={})), (F['shift'], 'shift', dict(text={})),
                                   (F['date'], 'date', dict(lookup={})), (F['part'], 'part', dict(boolean=None))):
            got = self.blocked(schema=self.changed(name, **extra))
            self.assertIn(label + '_type_facets_consistent', got, (name, extra))

    def test_malformed_or_empty_facets(self):
        cases = (('ActualScan', 'scan_field', dict(dateTime=None)), ('ActualScan', 'scan_field', dict(dateTime='dateTime')),
                 ('ActualScan', 'scan_field', dict(dateTime=[])), ('ActualScan', 'scan_field', dict(dateTime={})),
                 ('ActualScan', 'scan_field', dict(dateTime={'format': 5})), ('ActualScan', 'scan_field', dict(dateTime={'format': 'DateTime'})),
                 (F['date'], 'date', dict(dateTime={})), (F['inspected'], 'inspected', dict(number=None)), (F['inspected'], 'inspected', dict(number='number')),
                 (F['shift'], 'shift', dict(choice={})), (F['shift'], 'shift', dict(choice={'choices': '1,2,3'})),
                 (F['shift'], 'shift', dict(choice={'choices': [1, 2, 3]})), (F['shift'], 'shift', dict(choice=None)),
                 (F['part'], 'part', dict(text=None)), (F['part'], 'part', dict(readOnly='no')), (F['part'], 'part', dict(id=7)))
        for name, label, change in cases:
            got = self.blocked(schema=self.changed(name, **change))
            self.assertIn(label + '_type_facets_consistent', got, (name, change))

    def test_unrecognised_column_property_blocks_rather_than_being_ignored(self):
        got = self.blocked(schema=self.changed('ActualScan', geolocation={}))
        self.assertIn('scan_field_type_facets_consistent', got)

    def test_scan_column_with_a_default_value(self):
        for default in ({'formula': '=[Today]'}, {'value': '[Now]'}, {}, None):
            self.assertIn('scan_field_has_no_default_value', self.blocked(schema=self.changed('ActualScan', defaultValue=default)), default)


class SchemaEvidenceIsBound(Base):
    FLAGS = (dict(readOnly=True), dict(readOnly=False), dict(hidden=True), dict(required=True), dict(indexed=True),
             dict(enforceUniqueValues=True), dict(defaultValue={'value': '0'}), dict(validation={'formula': '=1'}),
             dict(displayName='Other'), dict(description='changed'))

    def test_every_safety_relevant_flag_changes_the_schema_digest(self):
        base = readiness.schema_digest(self.schema)
        for name in ('ActualScan', F['inspected'], F['shift']):
            for change in self.FLAGS:
                self.assertNotEqual(readiness.schema_digest(self.changed(name, **change)), base, (name, change))
        deeper = deepcopy(self.schema); deeper['columns']['ActualScan']['dateTime']['displayAs'] = 'friendly'
        self.assertNotEqual(readiness.schema_digest(deeper), base)

    def test_changed_evidence_blocks_until_repinned_and_repinning_makes_the_attestation_stale(self):
        for name, label, semantic in (('ActualScan', 'scan_field', 'scan_time_meaning'), (F['inspected'], 'inspected', 'quantities'),
                                      (F['shift'], 'shift', 'shift_values'), (F['date'], 'date', 'business_date'),
                                      (F['part'], 'part', 'template_and_header')):
            for change in (dict(hidden=True), dict(required=True), dict(description='changed')):
                schema = self.changed(name, **change)
                self.assertIn(label + '_definition_unchanged', self.blocked(schema=schema), (name, change))
                repinned = rf.pin(self.a, schema)                           # contract follows the schema; old attestations kept
                got = self.blocked(repinned, schema)
                self.assertIn(semantic + '_attested', got, (name, change)); self.assertNotIn(label + '_definition_unchanged', got)

    def test_read_only_on_a_quantity_is_caught_even_though_the_type_did_not_change(self):
        got = self.blocked(schema=self.changed(F['inspected'], readOnly=True))
        self.assertEqual(got, {'inspected_definition_unchanged'})

    def test_unpinned_definition_is_blocked(self):
        a = deepcopy(self.a); del a['field_contract']['scan']['definition']
        self.assertIn('scan_field_pinned_by_column_id_and_type', self.blocked(a))


class NeverRaisesOnNestedMalformedInput(Base):
    def test_qa_repro_column_is_none(self):
        schema = deepcopy(self.schema); schema['columns']['ActualScan'] = None
        got = self.blocked(schema=schema)
        self.assertTrue({'schema_columns_are_well_formed', 'scan_field_column_exists'} <= got, got)

    def test_qa_repro_field_contract_is_a_list(self):
        got = self.blocked(dict(self.a, field_contract=[1]))
        self.assertTrue({'field_contract_is_well_formed', 'scan_field_pinned_by_column_id_and_type'} <= got, got)

    def test_qa_repro_sample_rows_are_not_rows(self):
        self.assertIn('sample_rows_are_well_formed', self.blocked(rows=['bad']))

    def test_qa_repro_schema_list_is_text(self):
        self.assertIn('schema_is_for_this_site_and_list', self.blocked(schema=dict(self.schema, list='bad')))

    def test_other_nested_shapes(self):
        s = self.schema
        def cols(**change):
            schema = deepcopy(s); schema['columns'].update(change); return schema
        schemas = dict(column_text=(cols(**{F['inspected']: 'number'}), 'schema_columns_are_well_formed'),
                       column_list=(cols(**{F['shift']: [1]}), 'schema_columns_are_well_formed'),
                       unmapped_column_none=(cols(Extra=None), 'schema_columns_are_well_formed'),
                       columns_list=(dict(s, columns=[1, 2]), 'schema_snapshot_supplied'),
                       columns_text=(dict(s, columns='x'), 'schema_snapshot_supplied'),
                       key_not_text=(dict(s, columns={**s['columns'], 1: {'text': {}}}), 'schema_columns_are_well_formed'),
                       list_number=(dict(s, list=5), 'schema_is_for_this_site_and_list'),
                       list_list=(dict(s, list=[s['list_id']]), 'schema_is_for_this_site_and_list'))
        for name, (schema, code) in schemas.items():
            self.assertIn(code, self.blocked(schema=schema), name)
        assignments = dict(contract_entry_list=(dict(self.a, field_contract=dict(self.a['field_contract'], scan=[1])), 'field_contract_is_well_formed'),
                           contract_text=(dict(self.a, field_contract='x'), 'field_contract_is_well_formed'),
                           contract_entry_none=(dict(self.a, field_contract=dict(self.a['field_contract'], rejected=None)), 'field_contract_is_well_formed'),
                           shifts_values_lists=(dict(self.a, mapping=dict(self.a['mapping'], shifts={'1': ['1st Shift'], '2': {}, '3': None})), 'every_recorded_shift_maps_to_a_template_shift'),
                           shifts_list=(dict(self.a, mapping=dict(self.a['mapping'], shifts=[1])), 'every_recorded_shift_maps_to_a_template_shift'),
                           fields_values=(dict(self.a, mapping=dict(self.a['mapping'], fields=dict(F, inspected=['x'], rejected={'a': 1}))), 'mapping_covers_every_report_field'),
                           recorded_nested=(dict(self.a, recorded_shifts=[['1'], {}]), 'recorded_shifts_listed'),
                           header_list=(dict(self.a, header=['x']), 'header_uses_only_template_fields'),
                           attestations_list=(dict(self.a, attestations=[1]), 'scan_time_meaning_attested'),
                           attestation_entry_list=(dict(self.a, attestations=dict(self.a['attestations'], timezone=[1])), 'timezone_attested'),
                           zone_slash=(dict(self.a, zone='/'), 'timezone_is_a_real_zone'), zone_dots=(dict(self.a, zone='../etc/passwd'), 'timezone_is_a_real_zone'),
                           scan_list=(dict(self.a, scan_field=['ActualScan']), 'scan_field_is_not_date_or_arrival_metadata'),
                           identity_nested=(dict(self.a, client_id={'x': 1}, job=[1]), 'assignment_identity_complete'))
        for name, (a, code) in assignments.items():
            self.assertIn(code, self.blocked(a), name)
        for name, rows in dict(text='bad', mapping={'a': 1}, number=5, mixed=ROWS[:2] + [None], nested=[['x']]).items():
            self.assertIn('sample_rows_are_well_formed', self.blocked(rows=rows), name)
        odd = [dict(ROWS[0], **{F['shift']: ['1'], 'ActualScan': {'x': 1}, F['rejected']: object()})] + ROWS[1:]
        self.assertTrue({'sample_scan_values_are_offset_aware_times', 'sample_shift_values_are_all_recorded_shifts'} <= self.blocked(rows=odd))
        edge = [dict(ROWS[0], ActualScan='0001-01-01T00:00:00+23:00'), dict(ROWS[1], ActualScan='9999-12-31T23:59:59-23:00')] + ROWS[2:]
        self.assertIn('sample_scan_values_are_offset_aware_times', self.blocked(rows=edge))
        for name, entries in dict(text='x', items=['x'], none_item=[None], nested=[dict(demo.REGISTRY[0], client_key=['DEMO-CLIENT'])],
                                  mapping={'a': 1}, number=3).items():
            self.assertIn('registry_entries_are_well_formed', self.blocked(entries=entries), name)

    def test_helpers_do_not_raise_either(self):
        for schema in (None, 'x', [], {}, dict(columns=None), dict(columns=[1]), dict(columns={'A': None, 'B': 'x', 3: {}}), dict(self.schema, list='bad')):
            self.assertIsInstance(readiness.schema_digest(schema), str)
            self.assertIsInstance(readiness.inventory(schema)['columns'], list)
        for a in (None, 'x', [], {}, dict(mapping='x'), dict(field_contract=[1]), dict(mapping=dict(shifts=[1])), dict(header={1, 2})):
            self.assertEqual(set(readiness.attestation_bindings(a)), set(readiness.SEMANTICS))
        for column in (None, 'x', [], {}, dict(dateTime=None), dict(dateTime={}, calculated=None)):
            self.assertIsInstance(readiness.column_type(column), str)
        self.assertEqual([c['type'] for c in readiness.inventory(dict(columns={'A': None, 'B': dict(text={}, number={})}))['columns']], ['malformed', 'contradictory'])


class CodingErrorsAreNotSwallowed(Base):
    def test_a_bug_in_the_registry_rule_surfaces_instead_of_becoming_a_blocked_reason(self):
        with mock.patch.object(runner, '_registry_problem', side_effect=RuntimeError('bug')):
            with self.assertRaises(RuntimeError):
                readiness.validate(self.a, self.schema, demo.REGISTRY)

    def test_validator_has_no_catch_all(self):
        import pathlib, re
        text = pathlib.Path(readiness.__file__).read_text()
        self.assertIsNone(re.search(r'except\s*(Exception|BaseException)?\s*(as \w+)?\s*:', text))


if __name__ == '__main__':
    unittest.main()
