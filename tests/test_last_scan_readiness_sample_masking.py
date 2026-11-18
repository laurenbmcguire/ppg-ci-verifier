"""Regression found by independent QA of fd0e013: an invalid arrival value counted as counter-evidence and erased an
established scan==Created red flag. Also: what was not performed is said explicitly, and schema dry-run readiness is
kept apart from runtime source proof. Synthetic rows only (fictitious DEMO-CLIENT / TEST-9001)."""
import unittest
from datetime import datetime, timedelta, timezone
import demo_client_fixture as demo
import readiness_fixture as rf
from hosting import last_scan_readiness as readiness

ROWS = [r['fields'] for r in demo.items(other_day=False)]
CENTRAL = timezone(timedelta(hours=-6))
FLAG, COMPARABLE = 'scan_values_differ_from_created', 'sample_created_values_are_comparable'


def at(row):
    return datetime.fromisoformat(row['ActualScan'].replace('Z', '+00:00')).astimezone(timezone.utc).replace(microsecond=0)


def equal(row):                                     # scan written in Central, Created in Z: same instant, different text
    return dict(row, ActualScan=at(row).astimezone(CENTRAL).isoformat(), Created=at(row).isoformat().replace('+00:00', 'Z'))


def valid_scan(row, **extra):
    return dict(row, ActualScan=at(row).astimezone(CENTRAL).isoformat(), **extra)


class Base(unittest.TestCase):
    def setUp(self):
        self.a, self.schema = rf.attested(demo.assignment())

    def report(self, rows=None):
        return readiness.validate(self.a, self.schema, demo.REGISTRY, rows)

    def note(self, report, arrival='Created'):
        return [o for o in report['sample_observations'] if o.get('arrival_field') == arrival][0]


class InvalidValuesNeverEraseARedFlag(Base):
    def test_qa_repro_one_equal_pair_then_a_row_whose_created_is_invalid(self):
        alone = self.report([equal(ROWS[0])])
        self.assertEqual(alone['blocked_reasons'], [FLAG])                       # blocks alone
        report = self.report([equal(ROWS[0]), valid_scan(ROWS[1], Created='invalid')])
        self.assertFalse(report['ready_for_dry_run'], 'FALSE READY')
        self.assertEqual(set(report['blocked_reasons']), {FLAG, COMPARABLE})
        n = self.note(report)
        self.assertEqual((n['rows_with_both'], n['same_instant'], n['different_instant'], n['not_comparable']), (2, 1, 0, 1))

    def test_every_kind_of_unusable_arrival_value(self):
        unusable = dict(invalid='invalid', naive='2026-11-10T21:00:00', naive_same_wall_clock=at(ROWS[1]).astimezone(CENTRAL).replace(tzinfo=None).isoformat(),
                        date_only='2026-11-10', blank='', number=5, zero=0, false=False, mapping={'value': '2026-11-10T21:00:00Z'}, items=['2026-11-10T21:00:00Z'],
                        out_of_range='0001-01-01T00:00:00+23:00')
        for name, value in unusable.items():
            report = self.report([equal(ROWS[0]), valid_scan(ROWS[1], Created=value)])
            self.assertFalse(report['ready_for_dry_run'], 'FALSE READY: ' + name)
            self.assertTrue({FLAG, COMPARABLE} <= set(report['blocked_reasons']), (name, report['blocked_reasons']))

    def test_unusable_scan_value_on_the_other_row(self):
        for name, value in dict(naive='2026-11-10T15:00:00', date_only='2026-11-10', words='n/a', number=7, mapping={'x': 1}).items():
            report = self.report([equal(ROWS[0]), dict(ROWS[1], ActualScan=value, Created='2026-11-10T22:00:00Z')])
            self.assertTrue({FLAG, 'sample_scan_values_are_offset_aware_times'} <= set(report['blocked_reasons']), (name, report['blocked_reasons']))

    def test_absent_created_on_other_rows_does_not_erase_it(self):
        for second in (valid_scan(ROWS[1]), valid_scan(ROWS[1], Created=None)):
            report = self.report([equal(ROWS[0]), second])
            self.assertEqual(report['blocked_reasons'], [FLAG])
            self.assertEqual(self.note(report)['rows_without_arrival_value'], 1)

    def test_many_equal_and_many_unusable(self):
        rows = [equal(r) for r in ROWS[:10]] + [valid_scan(r, Created='invalid') for r in ROWS[10:20]] + [valid_scan(r) for r in ROWS[20:]]
        report = self.report(rows)
        self.assertEqual(set(report['blocked_reasons']), {FLAG, COMPARABLE})
        n = self.note(report)
        self.assertEqual((n['same_instant'], n['different_instant'], n['not_comparable'], n['rows_without_arrival_value']), (10, 0, 10, len(ROWS) - 20))

    def test_valid_unequal_control_does_lift_the_flag(self):
        later = at(ROWS[1]) + timedelta(minutes=7)
        report = self.report([equal(ROWS[0]), valid_scan(ROWS[1], Created=later.isoformat().replace('+00:00', 'Z'))])
        self.assertEqual(report['blocked_reasons'], []); self.assertTrue(report['ready_for_dry_run'])
        n = self.note(report)
        self.assertEqual((n['same_instant'], n['different_instant'], n['not_comparable']), (1, 1, 0))

    def test_valid_unequal_plus_unusable_is_incomplete_not_ready(self):
        later = (at(ROWS[1]) + timedelta(minutes=7)).isoformat().replace('+00:00', 'Z')
        report = self.report([equal(ROWS[0]), valid_scan(ROWS[1], Created=later), valid_scan(ROWS[2], Created='invalid')])
        self.assertEqual(report['blocked_reasons'], [COMPARABLE])                # no red flag, but the supplied comparison is incomplete

    def test_only_unusable_values_establish_nothing_and_do_not_pass(self):
        report = self.report([valid_scan(r, Created='2026-11-10T21:00:00') for r in ROWS])
        self.assertEqual(report['blocked_reasons'], [COMPARABLE])
        self.assertIn('no timezone was assumed', self.note(report)['note'])

    def test_the_other_arrival_fields_behave_the_same(self):
        for arrival in ('Modified', 'createdDateTime', 'lastModifiedDateTime'):
            first = {k: v for k, v in equal(ROWS[0]).items() if k != 'Created'}; first[arrival] = equal(ROWS[0])['Created']
            report = self.report([first, valid_scan(ROWS[1], **{arrival: 'invalid'})])
            self.assertEqual(set(report['blocked_reasons']), {'scan_values_differ_from_' + arrival.lower(), f'sample_{arrival.lower()}_values_are_comparable'}, arrival)


class WhatWasNotPerformedIsSaid(Base):
    def test_no_sample_or_an_empty_sample_is_marked_not_performed(self):
        for rows in (None, []):
            report = self.report(rows)
            self.assertTrue(report['ready_for_dry_run'])                         # the optional check is not a gate...
            self.assertEqual(report['proof']['sample_evidence'], 'not_performed')   # ...but its absence is stated, never read as a pass
            self.assertEqual([n['check'] for n in report['not_performed']], ['sample_rows_examined', 'scan_compared_with_arrival_metadata'])
            self.assertEqual(report['sample_observations'], [])
            text = readiness.report_text(report)
            self.assertIn('NOT PERFORMED  sample_rows_examined', text); self.assertIn('NOT PERFORMED  scan_compared_with_arrival_metadata', text)

    def test_sample_without_any_arrival_field_marks_only_that_comparison(self):
        report = self.report(ROWS)
        self.assertNotIn('Created', ROWS[0])
        self.assertEqual((report['blocked_reasons'], report['proof']['sample_evidence']), ([], 'performed_no_red_flag_found'))
        self.assertEqual([n['check'] for n in report['not_performed']], ['scan_compared_with_arrival_metadata'])

    def test_sample_with_a_comparison(self):
        later = lambda r: (at(r) + timedelta(minutes=3)).isoformat().replace('+00:00', 'Z')
        clean = self.report([valid_scan(r, Created=later(r)) for r in ROWS])
        self.assertEqual((clean['not_performed'], clean['proof']['sample_evidence']), ([], 'performed_no_red_flag_found'))
        self.assertEqual(self.report([equal(r) for r in ROWS])['proof']['sample_evidence'], 'performed_red_flag_found')
        self.assertEqual(self.report(['bad'])['proof']['sample_evidence'], 'rejected_malformed')


class ReadinessIsNotRuntimeProof(Base):
    def test_ready_report_claims_only_schema_dry_run_readiness(self):
        later = lambda r: (at(r) + timedelta(minutes=3)).isoformat().replace('+00:00', 'Z')
        for rows in (None, ROWS, [valid_scan(r, Created=later(r)) for r in ROWS]):
            report = self.report(rows)
            self.assertTrue(report['ready_for_dry_run'])
            proof = report['proof']
            self.assertEqual((proof['schema_dry_run_readiness'], proof['runtime_source_proof'], proof['scan_time_semantics']),
                             ('ready', 'not_established', 'not_established_by_this_tool'))
            self.assertEqual(report['marked_verified_by_this_tool'], False)
            text = readiness.report_text(report)
            for line in ('Schema dry-run readiness: READY', 'Runtime source proof: NOT ESTABLISHED', 'Scan-time meaning: NOT ESTABLISHED BY THIS TOOL'):
                self.assertIn(line, text)

    def test_blocked_report_says_blocked_and_still_claims_no_proof(self):
        proof = self.report([equal(r) for r in ROWS])['proof']
        self.assertEqual((proof['schema_dry_run_readiness'], proof['runtime_source_proof'], proof['scan_time_semantics']),
                         ('blocked', 'not_established', 'not_established_by_this_tool'))

    def test_a_sample_that_looks_fine_never_becomes_a_claim_about_scan_time(self):
        later = lambda r: (at(r) + timedelta(minutes=3)).isoformat().replace('+00:00', 'Z')
        report = self.report([valid_scan(r, Created=later(r)) for r in ROWS])
        import json
        text = (json.dumps(report['proof']) + json.dumps(report['sample_observations'])).lower()
        for claim in ('scan time confirmed', 'is the actual scan', 'verified', 'proves', 'proven'):
            self.assertNotIn(claim, text)
        self.assertIn('never evidence', report['sample_observations'][-1]['note'])


if __name__ == '__main__':
    unittest.main()
