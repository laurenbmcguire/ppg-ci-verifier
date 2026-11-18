"""A provider's "wait N seconds" is measured on the RUNNER's clock, from when the runner saw the answer.
Regression for the clock mismatch found by independent QA of the rehearsal: the wait was turned into a time with the
wall clock inside the capture code and then compared with the runner's own clock, so the two could disagree.
Fully synthetic: the rehearsal harness, fake authorities, in-memory tenants."""
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import rehearsal_harness as rh
from hosting import last_scan_runner as runner, source_first_capture as source
from hosting.source_first_capture import cooldown
from rehearsal_harness import Rehearsal, utc


def skewed(clock, delta):
    """The capture module's wall clock, running ``delta`` away from the runner's clock."""
    class Skewed(datetime):
        @classmethod
        def now(cls, tz=None):
            return utc(clock()) + delta
    return mock.patch.object(source, 'datetime', Skewed)


class Base(unittest.TestCase):
    def setUp(self):
        self.r = Rehearsal(self)

    def at(self, hhmmss):
        self.r.clock.set('2026-11-10T%s+00:00' % (hhmmss if len(hhmmss) == 8 else hhmmss + ':00'))

    def down(self, client, retry_after, status=503):
        self.r.tenants[client].down = (status, None, {} if retry_after is ... else {'Retry-After': retry_after})

    def poll(self):
        out = self.r.poll()
        self.assertEqual(self.r.last_response[1]['status'], 'ran', self.r.last_response[1].get('preflight'))
        return out

    def alpha(self, out):
        return sorted(v['status'] for k, v in out.items() if k[0] == 'alpha')

    def until(self, out, client='alpha'):
        return {v['cooldown_until'] for k, v in out.items() if k[0] == client and v.get('cooldown_until')}

    def item_reads(self, client):
        return sum(1 for u in self.r.tenants[client].requests if '/items?' in u)


class ClockSkew(Base):
    def test_qa_repro_capture_clock_two_minutes_behind_the_runner(self):
        self.down('alpha', '60')
        with skewed(self.r.clock, -timedelta(seconds=120)):
            out = self.poll()                                                   # runner clock 19:00:00
        self.assertEqual(self.alpha(out), ['cooldown', 'cooldown', 'error'])    # one read asked to wait; the other two shifts were NOT read
        self.assertEqual(self.until(out), {'2026-11-10T19:01:00+00:00'})        # 60 seconds after the runner saw it, not 18:59
        self.assertEqual(self.item_reads('alpha'), 1)

    def test_capture_clock_ahead_of_the_runner_does_not_overhold(self):
        for ahead in (timedelta(seconds=120), timedelta(hours=6), timedelta(days=8)):
            r = Rehearsal(self); self.r = r
            self.down('alpha', '60')
            with skewed(r.clock, ahead):
                out = self.poll()
            self.assertEqual(self.until(out), {'2026-11-10T19:01:00+00:00'}, ahead)
            self.at('19:00:59'); self.assertEqual(self.alpha(self.poll()), ['cooldown'] * 3)
            self.r.tenants['alpha'].down = None
            self.at('19:01:00'); self.assertEqual(self.alpha(self.poll()), ['draft_ready', 'no_scans', 'waiting'], ahead)   # released on time

    def test_capture_clock_far_behind(self):
        self.down('alpha', '600', status=429)
        with skewed(self.r.clock, -timedelta(days=3)):
            out = self.poll()
        self.assertEqual((self.alpha(out), self.until(out)), (['cooldown', 'cooldown', 'error'], {'2026-11-10T19:10:00+00:00'}))

    def test_the_wall_clock_is_not_consulted_for_a_relative_wait(self):
        self.down('alpha', '90')
        with mock.patch.object(source, 'datetime', side_effect=AssertionError('wall clock used')) as wall:
            wall.now.side_effect = OverflowError('a broken wall clock')        # even a clock that cannot answer changes nothing
            out = self.poll()
        self.assertEqual(self.until(out), {'2026-11-10T19:01:30+00:00'})


class WhenTheAnswerArrives(Base):
    def test_a_delayed_response_counts_from_when_it_was_received(self):
        tenant = self.r.tenants['alpha']
        real = tenant.get
        def slow(url):
            if '/items?' in url:
                self.r.clock.advance(seconds=40)                                # the source takes 40 seconds to say "wait 60"
            return real(url)
        tenant.get = slow
        self.down('alpha', '60')
        out = self.poll()
        self.assertEqual(self.until(out), {'2026-11-10T19:01:40+00:00'})        # 19:00:40 + 60 s, not 19:00:00 + 60 s
        tenant.get = real
        self.at('19:01:39'); self.assertEqual(self.alpha(self.poll()), ['cooldown'] * 3)

    def test_same_site_is_suppressed_and_the_other_site_is_independent(self):
        self.down('alpha', '300')
        with skewed(self.r.clock, -timedelta(minutes=30)):
            out = self.poll()
        self.assertEqual(self.alpha(out), ['cooldown', 'cooldown', 'error'])
        self.assertEqual(sorted(v['status'] for k, v in out.items() if k[0] == 'bravo'), ['waiting', 'waiting'])
        self.assertEqual((self.until(out, 'bravo'), self.item_reads('bravo')), (set(), 2))
        reads = self.item_reads('alpha')
        self.at('19:04:59'); out = self.poll()
        self.assertEqual((self.alpha(out), self.item_reads('alpha')), (['cooldown'] * 3, reads))
        self.assertEqual({v['not_before'] for k, v in out.items() if k[0] == 'alpha'}, {'2026-11-10T19:05:00+00:00'})

    def test_the_hold_survives_a_restart(self):
        self.down('alpha', '600')
        with skewed(self.r.clock, timedelta(hours=2)):
            self.poll()
        reads = self.item_reads('alpha')
        for hhmm in ('19:03', '19:09'):
            self.r.restart(); self.at(hhmm)
            self.assertEqual((self.alpha(self.poll()), self.item_reads('alpha')), (['cooldown'] * 3, reads), hhmm)
        self.r.restart(); self.r.tenants['alpha'].down = None
        self.at('19:10'); self.assertEqual(self.alpha(self.poll()), ['draft_ready', 'no_scans', 'waiting'])

    def test_what_was_recorded_about_the_hold(self):
        self.down('alpha', '60')
        self.poll()
        site = rh.CLIENTS['alpha']['site_id']
        with self.r.store.transaction(runner.HOLD + runner.sf.digest(site)) as hold:
            self.assertEqual((hold['not_before'], hold['basis'], hold['observed_at'], hold['provider_stated'], hold['stated_seconds']),
                             ('2026-11-10T19:01:00+00:00', 'relative_seconds', '2026-11-10T19:00:00+00:00', True, 60))


class KnownGapAtDispatch(Base):
    def test_known_gap_a_throttled_re_read_at_dispatch_sets_no_site_hold(self):
        """Pins a KNOWN GAP: the per-site wait is recorded by the runner's poll, not by a dispatch. A dispatch that is
        told to wait holds its own outbox entry but another dispatch may ask the source again at once. To be closed
        when a hosted dispatch step is designed; then this test must expect no second read."""
        from test_last_scan_durable import FAKE_CONTRACT, trust
        trust(self, FAKE_CONTRACT)
        self.poll()
        self.down('alpha', '600')
        reads = self.item_reads('alpha')
        for n in (1, 2):
            out = self.r.dispatch('alpha', '1')
            self.assertEqual((out['status'], out['reason']), ('held', 'recapture_failed'))
            self.assertEqual(self.item_reads('alpha'), reads + n)               # each dispatch asked the throttled source again
        self.assertEqual(self.r.lauren.calls, 0)                                # nothing was sent either time
        self.assertEqual(self.alpha(self.poll()), ['cooldown', 'cooldown', 'error'])   # the next POLL does record and honour the wait


class AbsoluteDates(Base):
    def test_an_absolute_retry_after_date_is_preserved_whatever_the_clocks_say(self):
        for skew in (-timedelta(minutes=10), timedelta(0), timedelta(days=2)):
            self.r = Rehearsal(self)
            self.down('alpha', 'Tue, 10 Nov 2026 19:21:00 GMT')
            with skewed(self.r.clock, skew):
                out = self.poll()
            self.assertEqual(self.until(out), {'2026-11-10T19:21:00+00:00'}, skew)
            self.at('19:20:59'); self.assertEqual(self.alpha(self.poll()), ['cooldown'] * 3)

    def test_an_absolute_date_already_past_still_gets_the_minimum_wait(self):
        self.down('alpha', 'Tue, 10 Nov 2026 18:00:00 GMT')                     # the provider's clock, or ours, is off
        self.assertEqual(self.until(self.poll()), {'2026-11-10T19:01:00+00:00'})


class ValuePolicy(Base):
    def one(self, value, status=503):
        self.r = Rehearsal(self)
        self.down('alpha', value, status)
        out = self.poll()
        self.assertEqual(self.alpha(out), ['cooldown', 'cooldown', 'error'], value)   # whatever the value: no second read of the site in this poll
        (until,) = self.until(out)
        return until

    def test_malformed_negative_or_missing_values_wait_the_default(self):
        self.assertEqual(runner.DEFAULT_COOLDOWN, timedelta(minutes=15))
        for status in (429, 503):
            for value in (..., '', ' ', 'soon', '-5', '-60', '1.5', '60s', '1e3', '0x10', '+60', '６０', 'Tue, 99 Nov 2026 19:21:00 GMT', 'Tue, 10 Nov 2026 19:21:00'):
                self.assertEqual(self.one(value, status), '2026-11-10T19:15:00+00:00', (status, value))

    def test_zero_or_tiny_values_wait_the_minimum(self):
        self.assertEqual(runner.MIN_COOLDOWN, timedelta(seconds=60))
        for value in ('0', '00', '1', '59'):
            self.assertEqual(self.one(value), '2026-11-10T19:01:00+00:00', value)
        self.assertEqual(self.one('61'), '2026-11-10T19:01:01+00:00')

    def test_excessive_values_are_held_at_the_maximum_and_flagged_never_shortened_to_the_default(self):
        self.assertEqual(runner.MAX_COOLDOWN, timedelta(days=7))
        for value in ('604801', '31536000', '9' * 12, '9' * 40, '9' * 5000, 'Fri, 01 Jan 2099 00:00:00 GMT'):
            self.assertEqual(self.one(value), '2026-11-17T19:00:00+00:00', value[:20])
            with self.r.store.transaction(runner.HOLD + runner.sf.digest(rh.CLIENTS['alpha']['site_id'])) as hold:
                self.assertTrue(hold['capped_at_maximum'], value[:20])
        self.assertEqual(self.one('604800'), '2026-11-17T19:00:00+00:00')
        with self.r.store.transaction(runner.HOLD + runner.sf.digest(rh.CLIENTS['alpha']['site_id'])) as hold:
            self.assertFalse(hold['capped_at_maximum'])


class TheConversionItself(unittest.TestCase):
    NOW = utc('2026-11-10T19:00:00+00:00')

    def test_cooldown_reports_what_the_provider_said_separately_from_any_clock(self):
        self.assertEqual({k: cooldown({'Retry-After': '60'})[k] for k in ('retry_after', 'known', 'basis', 'seconds')},
                         dict(retry_after='60', known=True, basis='relative_seconds', seconds=60))
        c = cooldown({'Retry-After': 'Tue, 10 Nov 2026 19:21:00 GMT'})
        self.assertEqual((c['known'], c['basis'], c['not_before'], c.get('seconds')), (True, 'http_date', '2026-11-10T19:21:00+00:00', None))
        self.assertEqual(cooldown({}), dict(retry_after=None, not_before=None, known=False))            # unchanged shape for the existing app
        self.assertEqual((cooldown({'Retry-After': 'soon'})['known'], cooldown({'Retry-After': '-5'})['known']), (False, False))
        huge = cooldown({'Retry-After': '9' * 12})
        self.assertEqual((huge['basis'], huge['seconds']), ('relative_seconds', int('9' * 12)))

    def test_hold_until_is_pure_and_uses_only_the_time_it_is_given(self):
        from hosting.last_scan_drafts import hold_until
        day = timedelta(days=1)
        cases = [(dict(retry_after='60', known=True, basis='relative_seconds', seconds=60, not_before='1999-01-01T00:00:00+00:00'), 60, 'relative_seconds'),
                 (dict(retry_after='x', known=True, not_before='2026-11-10T19:21:00+00:00'), 21 * 60, 'http_date'),          # the older shape, no basis: an absolute time
                 (dict(retry_after=None, not_before=None, known=False), 15 * 60, 'default'), (None, 15 * 60, 'default'), ('wait', 15 * 60, 'default'),
                 (dict(known=True, basis='relative_seconds', seconds='60'), 15 * 60, 'default'), (dict(known=True, basis='relative_seconds', seconds=True), 15 * 60, 'default'),
                 (dict(known=True, basis='relative_seconds', seconds=-5), 15 * 60, 'default'), (dict(known=True, not_before='not a time'), 15 * 60, 'default'),
                 (dict(known=True, not_before='2026-11-10T19:21:00'), 15 * 60, 'default')]
        for cool, seconds, basis in cases:
            out = hold_until(cool, self.NOW, default=timedelta(minutes=15), minimum=timedelta(seconds=60), maximum=7 * day)
            self.assertEqual((out['until'], out['basis']), (self.NOW + timedelta(seconds=seconds), basis), cool)


class SchemaAdapterWait(unittest.TestCase):
    def test_the_capture_adapter_reports_its_wait_on_the_host_clock_it_was_given(self):
        import demo_client_fixture as demo
        from hosting.last_scan_schema_capture import SchemaCaptureFailure, capture_schema
        from test_last_scan_schema_capture import graph_for
        for kind, stage in (('list', 'list_identity'), ('columns', 'columns')):
            graph, _ = graph_for(demo.assignment())
            graph.script = lambda url, n, k, kind=kind: (429, None, {'Retry-After': '120'}) if k == kind else None
            clock = rh.Clock('2026-11-10T19:00:00+00:00')
            with skewed(clock, -timedelta(hours=5)), self.assertRaises(SchemaCaptureFailure) as caught:
                capture_schema(graph.site_id, graph.list_id, fetch=graph, clock=clock)
            e = caught.exception.evidence
            self.assertEqual((e['stage'], e['wait_until'], e['wait_basis']), (stage, '2026-11-10T19:02:00+00:00', 'relative_seconds'))


if __name__ == '__main__':
    unittest.main()
