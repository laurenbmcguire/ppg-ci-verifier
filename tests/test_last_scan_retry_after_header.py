"""Retry-After: header names are case-insensitive, conflicting values resolve to the most cautious, and a delay the
provider stated but that cannot be safely interpreted needs a person. Regression for the defects found by independent
QA of 394aee3: a lowercase 'retry-after: 3600' was treated as absent and waited the 15-minute default.
Fully synthetic: the rehearsal harness, in-memory tenants, fake authorities."""
import itertools
import unittest
from datetime import timedelta

import demo_client_fixture as demo
import rehearsal_harness as rh
from hosting import last_scan_runner as runner
from hosting.last_scan_drafts import hold_until
from hosting.source_first_capture import cooldown
from rehearsal_harness import Rehearsal, utc

NOW = utc('2026-11-10T19:00:00+00:00')
NAMES = ('Retry-After', 'retry-after', 'RETRY-AFTER', 'Retry-after', 'rEtRy-AfTeR', ' Retry-After ', 'retry-after\t')
YEAR_10000 = 'Sat, 01 Jan 10000 00:00:00 GMT'


def hold(headers):
    return hold_until(cooldown(headers), NOW)


def after(**kw):
    return NOW + timedelta(**kw)


class HeaderNameCase(unittest.TestCase):
    def test_qa_repro_lowercase_and_uppercase_names_are_the_same_header(self):
        self.assertEqual(hold({'Retry-After': '3600'})['until'], after(hours=1))
        self.assertEqual(hold({'retry-after': '3600'})['until'], after(hours=1))       # was 15 minutes
        self.assertEqual(hold({'RETRY-AFTER': '3600'})['until'], after(hours=1))       # was 15 minutes

    def test_every_legitimate_spelling_of_the_name(self):
        for name in NAMES:
            for value, expected in (('3600', after(hours=1)), (3600, after(hours=1)), (' 3600 ', after(hours=1)), (b'3600', after(hours=1)),
                                    ('Tue, 10 Nov 2026 21:00:00 GMT', after(hours=2)), ('tue, 10 nov 2026 21:00:00 gmt', after(hours=2))):
                out = hold({name: value})
                self.assertEqual((out['until'], out['manual_release_required']), (expected, False), (name, value))
            self.assertEqual(cooldown({name: '3600'})['basis'], 'relative_seconds')

    def test_other_headers_are_not_mistaken_for_it(self):
        for headers in ({'X-Retry-After': '3600'}, {'Retry-After-Ms': '3600'}, {'RetryAfter': '3600'}, {'retry_after': '3600'}, {5: '3600'}, {None: '3600'},
                        {('Retry-After',): '3600'}, {'Retry': '3600'}):
            self.assertEqual(cooldown(headers), dict(retry_after=None, not_before=None, known=False), headers)
            self.assertEqual(hold(headers)['until'], after(minutes=15))


class ConflictingValues(unittest.TestCase):
    def both_orders(self, a, b):
        (name_a, value_a), (name_b, value_b) = a, b
        results = []
        for first, second in (((name_a, value_a), (name_b, value_b)), ((name_b, value_b), (name_a, value_a))):
            headers = {}
            headers[first[0]] = first[1]; headers[second[0]] = second[1]
            results.append(hold(headers))
        self.assertEqual((results[0]['until'], results[0]['manual_release_required']), (results[1]['until'], results[1]['manual_release_required']))   # order never matters
        return results[0]

    def test_two_numeric_values_under_different_casing_keep_the_longer(self):
        for short, long in (('60', '3600'), ('3599', '3600'), ('0', '61')):
            out = self.both_orders(('Retry-After', short), ('retry-after', long))
            self.assertEqual((out['until'], out['basis'], out['manual_release_required']), (after(seconds=int(long)), 'conflict', False))
        three = hold({'Retry-After': '60', 'retry-after': '7200', 'RETRY-AFTER': '3600'})
        self.assertEqual(three['until'], after(hours=2))

    def test_number_against_date_keeps_whichever_ends_later(self):
        out = self.both_orders(('Retry-After', '60'), ('retry-after', 'Tue, 10 Nov 2026 21:00:00 GMT'))
        self.assertEqual(out['until'], after(hours=2))
        out = self.both_orders(('Retry-After', '86400'), ('retry-after', 'Tue, 10 Nov 2026 21:00:00 GMT'))
        self.assertEqual(out['until'], after(days=1))
        out = self.both_orders(('Retry-After', 'Tue, 10 Nov 2026 20:00:00 GMT'), ('RETRY-AFTER', 'Wed, 11 Nov 2026 20:00:00 GMT'))
        self.assertEqual(out['until'], after(days=1, hours=1))

    def test_any_value_that_cannot_be_interpreted_or_represented_makes_the_whole_answer_manual(self):
        for bad in ('soon', '-5', '1.5', '', YEAR_10000, '9' * 14, '9' * 5000, 'Tue, 10 Nov 2026 21:00:00'):
            for good in ('3600', 'Tue, 10 Nov 2026 21:00:00 GMT'):
                out = self.both_orders(('Retry-After', good), ('retry-after', bad))
                self.assertEqual((out['until'], out['manual_release_required'], out['needs_attention']), (None, True, True), (good, bad[:20]))

    def test_identical_values_under_different_casing_are_one_answer(self):
        out = hold({'Retry-After': '3600', 'retry-after': '3600', 'RETRY-AFTER': ' 3600 '})
        self.assertEqual((out['until'], out['basis']), (after(hours=1), 'relative_seconds'))

    def test_a_list_of_values_under_one_name_is_held_to_the_same_rule(self):
        for values in (['60', '3600'], ('3600', '60'), ['3600', None, '60']):
            self.assertEqual(hold({'retry-after': values})['until'], after(hours=1))
        self.assertTrue(hold({'retry-after': ['60', 'soon']})['manual_release_required'])
        self.assertEqual(hold({'retry-after': []})['until'], after(minutes=15))                # nothing stated

    def test_every_permutation_of_a_mixed_set_gives_the_same_hold(self):
        items = [('Retry-After', '60'), ('retry-after', '3600'), ('RETRY-AFTER', 'Tue, 10 Nov 2026 21:00:00 GMT'), ('Retry-after', '120')]
        seen = set()
        for order in itertools.permutations(items):
            out = hold(dict(order))
            seen.add((out['until'], out['manual_release_required']))
        self.assertEqual(seen, {(after(hours=2), False)})
        seen = {hold(dict(order))['manual_release_required'] for order in itertools.permutations(items + [('rEtRy-AfTeR', 'soon')])}
        self.assertEqual(seen, {True})


class AbsentIsNotTheSameAsInvalid(unittest.TestCase):
    def test_absent_waits_the_default(self):
        for headers in ({}, None, {'Retry-After': None}, {'retry-after': None}, {'Content-Type': 'application/json'}, [], 'Retry-After: 60'):
            self.assertEqual(cooldown(headers), dict(retry_after=None, not_before=None, known=False), headers)
            out = hold(headers)
            self.assertEqual((out['until'], out['basis'], out['manual_release_required']), (after(minutes=15), 'default', False), headers)

    def test_a_delay_that_was_stated_but_cannot_be_safely_interpreted_needs_a_person(self):
        invalid = ('', ' ', 'soon', '-5', '-60', '1.5', '60s', '1e3', '0x10', '+60', '６０', 'never', 'Tue, 99 Nov 2026 19:21:00 GMT', 'Tue, 10 Nov 2026 19:21:00',
                   YEAR_10000, 'Sat, 01 Jan 99999 00:00:00 GMT', 'Tue, 10 Nov 2026 21:00:00 +9999', '60, 120', True, False, 1.5, -5, b'\xff\xfe', {'seconds': 60})
        for name in ('Retry-After', 'retry-after', 'RETRY-AFTER'):
            for value in invalid:
                c = cooldown({name: value})
                self.assertEqual((c['basis'], c['known'], c['not_before']), ('invalid', False, None), (name, value))
                out = hold({name: value})
                self.assertEqual((out['until'], out['manual_release_required'], out['needs_attention'], out['basis']), (None, True, True, 'invalid'), (name, value))

    def test_qa_repro_year_10000_is_not_retried_after_fifteen_minutes(self):
        out = hold({'Retry-After': YEAR_10000})
        self.assertEqual((out['until'], out['manual_release_required']), (None, True))

    def test_older_recorded_shapes_stay_conservative(self):
        self.assertEqual(hold_until(dict(retry_after=None, not_before=None, known=False), NOW)['until'], after(minutes=15))       # nothing was stated
        self.assertTrue(hold_until(dict(retry_after='soon', not_before=None, known=False), NOW)['manual_release_required'])       # something was, unusable
        self.assertEqual(hold_until(dict(retry_after='600', not_before='2026-11-10T19:10:00+00:00', known=True), NOW)['until'], after(minutes=10))
        for garbage in (dict(basis='conflict'), dict(basis='conflict', candidates=[]), dict(basis='conflict', candidates='x'), dict(basis='conflict', candidates=[None])):
            self.assertTrue(hold_until(garbage, NOW)['manual_release_required'], garbage)

    def test_the_existing_service_still_sees_no_deadline_for_anything_it_cannot_use(self):
        for headers in ({'Retry-After': 'soon'}, {'retry-after': YEAR_10000}, {'Retry-After': '60', 'retry-after': '3600'}):
            c = cooldown(headers)
            self.assertEqual((c['known'], c['not_before']), (False, None), headers)      # known False + no deadline = its "verified recovery required" path


class ThroughEveryPath(unittest.TestCase):
    def setUp(self):
        self.r = Rehearsal(self)

    def at(self, hhmmss):
        self.r.clock.set('2026-11-10T%s+00:00' % (hhmmss if len(hhmmss) == 8 else hhmmss + ':00'))

    def down(self, headers, client='alpha', status=503):
        self.r.tenants[client].down = (status, None, headers)

    def poll(self):
        out = self.r.poll()
        self.assertEqual(self.r.last_response[1]['status'], 'ran', self.r.last_response[1].get('preflight'))
        return out

    def alpha(self, out):
        return sorted(v['status'] for k, v in out.items() if k[0] == 'alpha')

    def stored(self, client='alpha'):
        with self.r.store.transaction(runner.HOLD + runner.sf.digest(rh.CLIENTS[client]['site_id'])) as h:
            return dict(h)

    def test_poll_honours_a_lowercase_header_for_the_full_hour_across_a_restart(self):
        for name in ('retry-after', 'RETRY-AFTER', 'Retry-after'):
            self.r = Rehearsal(self)
            self.down({name: '3600'}, status=429)
            out = self.poll()
            self.assertEqual(self.alpha(out), ['cooldown', 'cooldown', 'error'], name)
            self.assertEqual({v['cooldown_until'] for k, v in out.items() if v.get('cooldown_until')}, {'2026-11-10T20:00:00+00:00'}, name)
            self.assertEqual((self.stored()['not_before'], self.stored()['basis'], self.stored()['stated_seconds']), ('2026-11-10T20:00:00+00:00', 'relative_seconds', 3600))
            requests = len(self.r.tenants['alpha'].requests)
            self.r.tenants['alpha'].down = None
            for hhmm in ('19:15', '19:16', '19:59:59'):                         # past the old 15-minute default
                self.r.restart(); self.at(hhmm)
                self.assertEqual(self.alpha(self.poll()), ['cooldown'] * 3, (name, hhmm))
            self.assertEqual(len(self.r.tenants['alpha'].requests), requests)
            self.at('20:00'); self.assertEqual(self.alpha(self.poll()), ['draft_ready', 'no_scans', 'waiting'], name)

    def test_poll_with_conflicting_casing_and_with_an_invalid_delay(self):
        self.down({'Retry-After': '60', 'retry-after': '3600'})
        self.poll()
        self.assertEqual((self.stored()['not_before'], self.stored()['basis']), ('2026-11-10T20:00:00+00:00', 'conflict'))
        self.r = Rehearsal(self)
        self.down({'retry-after': YEAR_10000})
        out = self.poll()
        self.assertEqual({(v.get('cooldown_until'), v.get('manual_release_required')) for k, v in out.items() if k[0] == 'alpha' and v['status'] == 'error'}, {(None, True)})
        self.r.tenants['alpha'].down = None
        self.r.restart(); self.at('23:00')
        self.assertEqual({(v['status'], v.get('manual_release_required')) for k, v in self.poll().items() if k[0] == 'alpha'}, {('cooldown', True)})
        self.assertEqual((self.stored()['manual_release_required'], self.stored()['basis']), (True, 'invalid'))

    def test_absent_header_on_a_poll_still_waits_only_the_default(self):
        self.down({})
        self.poll()
        self.assertEqual((self.stored()['not_before'], self.stored()['basis'], self.stored()['manual_release_required']), ('2026-11-10T19:15:00+00:00', 'default', False))

    def test_dispatch_honours_a_lowercase_header(self):
        from test_last_scan_durable import FAKE_CONTRACT, trust
        trust(self, FAKE_CONTRACT)
        self.poll()
        self.down({'retry-after': '3600'})
        first = self.r.dispatch('alpha', '1')
        self.assertEqual((first['reason'], first['cooldown_until']), ('recapture_failed', '2026-11-10T20:00:00+00:00'))
        self.r.tenants['alpha'].down = None
        self.at('19:30'); self.r.restart()
        again = self.r.dispatch('alpha', '1')
        self.assertEqual((again['reason'], again['not_before'], self.r.lauren.calls), ('site_cooldown', '2026-11-10T20:00:00+00:00', 0))
        self.at('20:00'); self.assertEqual(self.r.dispatch('alpha', '1')['status'], 'sent')

    def test_a_throttled_hosted_probe_with_a_lowercase_header(self):
        tenant = self.r.tenants['alpha']; real = tenant.get
        tenant.get = lambda url: (429, None, {'retry-after': '3600'}) if '/columns?' in url else real(url)
        self.r.poll()
        self.assertEqual(self.r.last_response[1]['status'], 'schema_drift')
        self.assertEqual(self.stored()['not_before'], '2026-11-10T20:00:00+00:00')
        self.at('19:30')
        self.assertEqual(self.alpha(self.poll()), ['cooldown'] * 3)

    def test_schema_capture_adapter_with_lower_upper_and_mixed_names(self):
        from hosting.last_scan_schema_capture import SchemaCaptureFailure, capture_schema
        from test_last_scan_schema_capture import graph_for
        for name in NAMES[:5]:
            for kind, stage in (('list', 'list_identity'), ('columns', 'columns')):
                for value, until, manual in (('3600', '2026-11-10T20:00:00+00:00', False), (YEAR_10000, None, True)):
                    graph, _ = graph_for(demo.assignment())
                    graph.script = lambda url, n, k, kind=kind, name=name, value=value: (503, None, {name: value}) if k == kind else None
                    with self.assertRaises(SchemaCaptureFailure) as caught:
                        capture_schema(graph.site_id, graph.list_id, fetch=graph, clock=rh.Clock('2026-11-10T19:00:00+00:00'))
                    e = caught.exception.evidence
                    self.assertEqual((e['stage'], e['wait_until'], e['manual_release_required']), (stage, until, manual), (name, kind, value))


if __name__ == '__main__':
    unittest.main()
