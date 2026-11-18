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


def hold_until_for(cool):
    from hosting.last_scan_drafts import hold_until
    return hold_until(cool, utc('2026-11-10T19:00:00+00:00'))


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
            self.assertEqual((hold['not_before'], hold['basis'], hold['observed_at'], hold['provider_stated'], hold['stated_seconds'], hold['manual_release_required']),
                             ('2026-11-10T19:01:00+00:00', 'relative_seconds', '2026-11-10T19:00:00+00:00', True, 60, False))


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

    def test_no_header_at_all_waits_the_default(self):
        self.assertEqual(runner.DEFAULT_COOLDOWN, timedelta(minutes=15))
        for status in (429, 503):
            self.assertEqual(self.one(..., status), '2026-11-10T19:15:00+00:00', status)

    def test_a_stated_but_unreadable_wait_is_never_replaced_by_the_default(self):
        for status in (429, 503):
            for value in ('', ' ', 'soon', '-5', '-60', '1.5', '60s', '1e3', '0x10', '+60', '６０', 'Tue, 99 Nov 2026 19:21:00 GMT', 'Tue, 10 Nov 2026 19:21:00'):
                self.r = Rehearsal(self)
                self.down('alpha', value, status)
                out = self.poll()
                self.assertEqual(self.alpha(out), ['cooldown', 'cooldown', 'error'], (status, value))
                self.assertEqual({(v.get('cooldown_until'), v.get('manual_release_required')) for k, v in out.items() if k[0] == 'alpha' and v['status'] == 'error'},
                                 {(None, True)}, (status, value))

    def test_zero_or_tiny_values_wait_the_minimum(self):
        self.assertEqual(runner.MIN_COOLDOWN, timedelta(seconds=60))
        for value in ('0', '00', '1', '59'):
            self.assertEqual(self.one(value), '2026-11-10T19:01:00+00:00', value)
        self.assertEqual(self.one('61'), '2026-11-10T19:01:01+00:00')

    def test_a_long_stated_wait_is_honoured_in_full_never_shortened(self):
        self.assertEqual(runner.ATTENTION_COOLDOWN, timedelta(days=7)); self.assertFalse(hasattr(runner, 'MAX_COOLDOWN'))
        for value, until in (('604800', '2026-11-17T19:00:00+00:00'), ('604801', '2026-11-17T19:00:01+00:00'), ('31536000', '2027-11-10T19:00:00+00:00'),
                             ('3153600000', '2126-10-17T19:00:00+00:00'), ('Fri, 01 Jan 2099 00:00:00 GMT', '2099-01-01T00:00:00+00:00'),
                             ('Wed, 18 Nov 2026 19:00:01 GMT', '2026-11-18T19:00:01+00:00')):
            self.assertEqual(self.one(value), until, value)
            hold = self.hold()
            self.assertEqual((hold['manual_release_required'], hold['needs_attention']), (False, value != '604800'), value)

    def hold(self, client='alpha'):
        with self.r.store.transaction(runner.HOLD + runner.sf.digest(rh.CLIENTS[client]['site_id'])) as hold:
            return dict(hold)

    def test_a_wait_that_cannot_be_represented_needs_a_person_and_is_never_retried_automatically(self):
        for value in ('9' * 14, '9' * 40, '9' * 5000):
            self.r = Rehearsal(self)
            self.down('alpha', value)
            out = self.poll()
            self.assertEqual(self.alpha(out), ['cooldown', 'cooldown', 'error'], value[:20])
            self.assertEqual({(v.get('cooldown_until'), v.get('manual_release_required')) for k, v in out.items() if k[0] == 'alpha' and v['status'] == 'error'}, {(None, True)})
            hold = self.hold()
            self.assertEqual((hold['not_before'], hold['manual_release_required'], hold['needs_attention'], hold['basis']), (None, True, True, 'relative_seconds'))
            reads = len(self.r.tenants['alpha'].requests)
            self.r.tenants['alpha'].down = None                                 # the source is healthy again; that changes nothing
            for hhmm in ('19:30', '23:00'):
                self.r.restart(); self.at(hhmm)
                out = self.poll()
                self.assertEqual({(v['status'], v.get('manual_release_required')) for k, v in out.items() if k[0] == 'alpha'}, {('cooldown', True)}, hhmm)
            self.assertEqual(len(self.r.tenants['alpha'].requests), reads)      # not one request of any kind
            self.assertEqual(sorted(v['status'] for k, v in out.items() if k[0] == 'bravo'), ['draft_ready', 'draft_ready'])   # the other site is unaffected

    def test_only_a_named_manual_release_ends_such_a_hold(self):
        from hosting.last_scan_service import release_site_hold
        self.down('alpha', '9' * 40); self.poll(); self.r.tenants['alpha'].down = None
        site = rh.CLIENTS['alpha']['site_id']
        for bad in (dict(released_by='', reason='x'), dict(released_by='root', reason=' '), dict(released_by=None, reason='x')):
            with self.assertRaises(ValueError):
                release_site_hold(self.r.store, site, now=self.r.clock(), **bad)
        self.assertEqual(self.alpha(self.poll()), ['cooldown'] * 3)
        released = release_site_hold(self.r.store, site, released_by='root (rehearsal)', reason='provider confirmed the wait was a mistake', now=self.r.clock())
        self.assertEqual((released['manual_release_required'], released['released_by'], released['released_hold']['manual_release_required']), (False, 'root (rehearsal)', True))
        self.assertEqual(self.alpha(self.poll()), ['draft_ready', 'no_scans', 'waiting'])
        import inspect
        from hosting import last_scan_hosted, last_scan_runner, last_scan_service
        for module in (last_scan_hosted, last_scan_runner):
            self.assertNotIn('release_site_hold', inspect.getsource(module))
        self.assertEqual(inspect.getsource(last_scan_service).count('release_site_hold('), 1)   # defined, never called


class LongHoldsAtTheStoreLevel(unittest.TestCase):
    """Beyond what a rehearsal day can reach: the stored hold is honoured to the second, months and years out."""
    SITE = rh.CLIENTS['alpha']['site_id']

    def setUp(self):
        import tempfile
        from pathlib import Path
        from hosting.last_scan_local import StateStore
        self.folder = tempfile.TemporaryDirectory(); self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'holds.db'; self.Store = StateStore
        self.store = StateStore(self.path)

    def record(self, cool, observed='2026-11-10T19:00:00+00:00', store=None):
        from hosting.last_scan_service import record_site_hold
        return record_site_hold(store or self.store, self.SITE, cool, utc(observed), observed)

    def held(self, at):
        from hosting.last_scan_service import site_hold
        return site_hold(self.Store(self.path), self.SITE, at)                 # a fresh store object every time: as after a restart

    def test_relative_and_absolute_holds_longer_than_seven_days_are_kept_to_the_second(self):
        for cool, until in ((cooldown({'Retry-After': '2592000'}), '2026-12-10T19:00:00+00:00'),                 # 30 days
                            (cooldown({'Retry-After': 'Fri, 01 Jan 2027 00:00:00 GMT'}), '2027-01-01T00:00:00+00:00'),
                            (cooldown({'Retry-After': '315360000'}), '2036-11-07T19:00:00+00:00')):               # ten years
            self.setUp()
            self.assertEqual(self.record(cool)['not_before'], until)
            before = (utc(until) - timedelta(seconds=1)).isoformat()
            self.assertIsNotNone(self.held('2026-11-17T19:00:01+00:00'))           # day 7 + 1 s: still held
            self.assertIsNotNone(self.held(before)); self.assertIsNone(self.held(until))

    def test_an_overflowing_wait_is_manual_and_survives_restart_at_any_later_time(self):
        self.assertEqual(self.record(cooldown({'Retry-After': '9' * 14}))['manual_release_required'], True)
        for at in ('2026-11-10T19:00:00+00:00', '2027-01-01T00:00:00+00:00', '2099-01-01T00:00:00+00:00', '9999-12-31T23:59:59+00:00'):
            self.assertTrue(self.held(at)['manual_release_required'], at)


class MergeIsAtomicAndNeverLoosens(LongHoldsAtTheStoreLevel):
    def test_a_shorter_wait_arriving_after_a_longer_one_does_not_shorten_it_in_either_order(self):
        short, long = cooldown({'Retry-After': '100'}), cooldown({'Retry-After': '600'})
        for order in ((long, short), (short, long)):
            self.setUp()
            for cool in order:
                self.record(cool)
            self.assertEqual(self.held('2026-11-10T19:00:00+00:00')['not_before'], '2026-11-10T19:10:00+00:00')
        self.setUp()                                                            # a later, shorter answer that still ends later wins
        self.record(long); self.record(short, observed='2026-11-10T19:09:00+00:00')
        self.assertEqual(self.held('2026-11-10T19:09:00+00:00')['not_before'], '2026-11-10T19:10:40+00:00')
        self.setUp()                                                            # an absolute date and a relative wait: the later end wins
        self.record(cooldown({'Retry-After': 'Tue, 10 Nov 2026 19:21:00 GMT'})); self.record(short)
        self.assertEqual(self.held('2026-11-10T19:00:00+00:00')['not_before'], '2026-11-10T19:21:00+00:00')
        self.record(cooldown({})); self.record(None)                           # a default wait never shortens a stated one
        self.assertEqual(self.held('2026-11-10T19:00:00+00:00')['not_before'], '2026-11-10T19:21:00+00:00')

    def test_a_manual_hold_dominates_every_finite_one_in_either_order_and_after_restart(self):
        manual, finite = cooldown({'Retry-After': '9' * 40}), cooldown({'Retry-After': '600'})
        for order in ((manual, finite), (finite, manual), (manual, finite, cooldown({'Retry-After': 'Fri, 01 Jan 2099 00:00:00 GMT'}), cooldown({}))):
            self.setUp()
            for cool in order:
                self.record(cool, store=self.Store(self.path))
            hold = self.held('2030-01-01T00:00:00+00:00')
            self.assertEqual((hold['manual_release_required'], hold['not_before']), (True, None))

    def test_overlapping_responses_from_many_workers_keep_the_longest(self):
        from concurrent.futures import ThreadPoolExecutor
        import random
        for round_number in range(5):
            self.setUp()
            waits = [100, 600, 30, 599, 601, 45, 600, 1] * 3
            random.Random(round_number).shuffle(waits)
            with ThreadPoolExecutor(8) as pool:
                list(pool.map(lambda w: self.record(cooldown({'Retry-After': str(w)}), store=self.Store(self.path)), waits))
            self.assertEqual(self.held('2026-11-10T19:00:00+00:00')['not_before'], '2026-11-10T19:10:01+00:00', round_number)
        self.setUp()
        waits = ['100', '600', '9' * 40, '30', '601'] * 4
        with ThreadPoolExecutor(8) as pool:
            list(pool.map(lambda w: self.record(cooldown({'Retry-After': w}), store=self.Store(self.path)), waits))
        self.assertTrue(self.held('2026-11-10T19:00:00+00:00')['manual_release_required'])


class DispatchSharesTheSiteHold(Base):
    """Was a pinned known gap; now fixed: a dispatch re-read honours and records the same per-site wait."""
    def setUp(self):
        super().setUp()
        from test_last_scan_durable import FAKE_CONTRACT, trust
        trust(self, FAKE_CONTRACT)
        self.poll()                                                             # alpha shift 1 is ready and pending

    def requests(self, client='alpha'):
        return len(self.r.tenants[client].requests)

    def test_a_throttled_re_read_at_dispatch_records_the_site_hold_and_the_next_dispatch_does_not_read(self):
        self.down('alpha', '600')
        reads = self.item_reads('alpha')
        first = self.r.dispatch('alpha', '1')
        self.assertEqual((first['status'], first['reason'], first['cooldown_until']), ('held', 'recapture_failed', '2026-11-10T19:10:00+00:00'))
        self.assertEqual(self.item_reads('alpha'), reads + 1)                   # the one read that was told to wait
        before = self.requests()
        for _ in range(3):                                                      # other dispatchers, same site
            again = self.r.dispatch('alpha', '1')
            self.assertEqual((again['status'], again['reason'], again['not_before']), ('held', 'site_cooldown', '2026-11-10T19:10:00+00:00'))
        self.r.restart(); self.at('19:09:59')
        self.assertEqual(self.r.dispatch('alpha', '1')['reason'], 'site_cooldown')
        self.assertEqual(self.alpha(self.poll()), ['cooldown'] * 3)             # the poll honours the wait a DISPATCH recorded
        self.assertEqual(self.requests(), before)                               # not one alpha request of any kind since
        self.assertEqual(self.r.lauren.calls, 0)
        self.r.tenants['alpha'].down = None
        self.at('19:10:00'); self.assertEqual(self.r.dispatch('alpha', '1')['status'], 'sent')
        self.assertEqual(len(self.r.lauren.messages), 1)

    def test_a_hold_recorded_by_a_poll_stops_a_dispatch_before_any_read(self):
        self.at('19:01'); self.down('alpha', '600')
        self.assertEqual(self.alpha(self.poll()), ['cooldown', 'cooldown', 'error'])
        before = self.requests()
        out = self.r.dispatch('alpha', '1')
        self.assertEqual((out['status'], out['reason'], out['not_before']), ('held', 'site_cooldown', '2026-11-10T19:11:00+00:00'))
        self.assertEqual((self.requests(), self.r.lauren.calls), (before, 0))

    def test_dispatch_uses_the_same_clock_and_duration_rules(self):
        self.down('alpha', '60')
        with skewed(self.r.clock, -timedelta(seconds=120)):
            self.assertEqual(self.r.dispatch('alpha', '1')['cooldown_until'], '2026-11-10T19:01:00+00:00')
        self.setUp(); self.down('alpha', '9' * 40)
        out = self.r.dispatch('alpha', '1')
        self.assertEqual((out['cooldown_until'], out['manual_release_required']), (None, True))
        self.r.tenants['alpha'].down = None; self.at('23:00')
        self.assertEqual((self.r.dispatch('alpha', '1')['reason'], self.r.lauren.calls), ('site_cooldown', 0))

    def test_concurrent_dispatchers_read_a_throttled_site_once(self):
        from concurrent.futures import ThreadPoolExecutor
        self.down('alpha', '600')
        reads = self.item_reads('alpha')
        with ThreadPoolExecutor(6) as pool:
            out = list(pool.map(lambda i: self.r.dispatch('alpha', '1'), range(6)))
        self.assertTrue({o['status'] for o in out} <= {'held', 'busy'}, out)
        self.assertEqual(self.item_reads('alpha'), reads + 1)
        self.assertEqual(self.r.lauren.calls, 0)

    def test_the_other_site_is_independent(self):
        self.at('19:40'); self.poll()                                           # bravo shift 2 becomes ready
        self.down('alpha', '600'); self.r.dispatch('alpha', '1')
        self.assertEqual(self.r.dispatch('bravo', '2')['status'], 'sent')
        self.assertEqual([m['payload']['receipt']['binding']['client_id'] for m in self.r.lauren.messages], ['BRAVO-REHEARSAL'])


class HostedProbesHonourTheHold(Base):
    def requests(self, client='alpha'):
        return len(self.r.tenants[client].requests)

    def test_qa_repro_second_poll_makes_no_request_of_any_kind_to_the_held_site(self):
        self.down('alpha', '600')
        self.assertEqual(self.alpha(self.poll()), ['cooldown', 'cooldown', 'error'])
        before, bravo = self.requests(), self.requests('bravo')
        out = self.poll()                                                       # no clock advance
        self.assertEqual(self.alpha(out), ['cooldown'] * 3)
        self.assertEqual(self.requests(), before)                               # no list metadata, no columns, no items
        self.assertGreater(self.requests('bravo'), bravo)                       # the other site is probed and read as usual
        self.assertEqual(sorted(v['status'] for k, v in out.items() if k[0] == 'bravo'), ['waiting', 'waiting'])
        body = self.r.last_response[1]
        self.assertEqual(body['sites_on_provider_wait'], [dict(site_id=rh.CLIENTS['alpha']['site_id'], list_id=rh.CLIENTS['alpha']['list_id'])])

    def test_a_throttled_probe_holds_the_whole_site_and_is_not_asked_again(self):
        tenant = self.r.tenants['alpha']
        real = tenant.get
        tenant.get = lambda url: (429, None, {'Retry-After': '300'}) if '/columns?' in url else real(url)
        self.r.poll()
        code, body = self.r.last_response
        self.assertEqual((code, body['status']), (409, 'schema_drift'))         # the probe failed: nothing is read in this request
        self.assertEqual((self.item_reads('alpha'), self.item_reads('bravo')), (0, 0))
        before = self.requests()
        out = self.poll()                                                       # next request: alpha is held, bravo proceeds
        self.assertEqual((self.alpha(out), self.requests()), (['cooldown'] * 3, before))
        self.assertEqual({v['not_before'] for k, v in out.items() if k[0] == 'alpha'}, {'2026-11-10T19:05:00+00:00'})
        self.assertEqual(sorted(v['status'] for k, v in out.items() if k[0] == 'bravo'), ['waiting', 'waiting'])

    def test_a_failing_probe_never_clears_or_shortens_a_hold(self):
        self.down('alpha', '600'); self.poll()
        tenant = self.r.tenants['alpha']
        tenant.get = lambda url: (500, None, {})                                # the source is now broken in another way
        for hhmm in ('19:03', '19:09:59'):
            self.at(hhmm)
            self.assertEqual(self.alpha(self.poll()), ['cooldown'] * 3, hhmm)
        with self.r.store.transaction(runner.HOLD + runner.sf.digest(rh.CLIENTS['alpha']['site_id'])) as hold:
            self.assertEqual(hold['not_before'], '2026-11-10T19:10:00+00:00')
        self.at('19:10'); self.r.poll()                                         # the wait is over; the broken probe now stops the run
        self.assertEqual(self.r.last_response[1]['status'], 'schema_drift')

    def test_a_manual_hold_also_stops_the_probes(self):
        self.down('alpha', '9' * 40); self.poll(); self.r.tenants['alpha'].down = None
        before = self.requests()
        self.at('22:00'); out = self.poll()
        self.assertEqual((self.requests(), self.alpha(out)), (before, ['cooldown'] * 3))


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
        self.assertEqual((hold_until_for(huge)['until'], hold_until_for(huge)['manual_release_required']), (None, True))   # about 31,700 years: past the calendar
        century = hold_until_for(cooldown({'Retry-After': '3153600000'}))
        self.assertEqual((century['until'].year, century['manual_release_required'], century['needs_attention']), (2126, False, True))   # a century: representable, kept in full
        self.assertEqual((hold_until_for(cooldown({'Retry-After': '9' * 14}))['until'], hold_until_for(cooldown({'Retry-After': '9' * 14}))['manual_release_required']), (None, True))

    def test_hold_until_is_pure_and_uses_only_the_time_it_is_given(self):
        from hosting.last_scan_drafts import hold_until
        day = timedelta(days=1)
        cases = [(dict(retry_after='60', known=True, basis='relative_seconds', seconds=60, not_before='1999-01-01T00:00:00+00:00'), 60, 'relative_seconds'),
                 (dict(retry_after='x', known=True, not_before='2026-11-10T19:21:00+00:00'), 21 * 60, 'http_date'),          # the older shape, no basis: an absolute time
                 (dict(retry_after=None, not_before=None, known=False), 15 * 60, 'default'), (None, 15 * 60, 'default'), ({}, 15 * 60, 'default')]
        for cool, seconds, basis in cases:
            out = hold_until(cool, self.NOW, default=timedelta(minutes=15), minimum=timedelta(seconds=60))
            self.assertEqual((out['until'], out['basis'], out['manual_release_required']), (self.NOW + timedelta(seconds=seconds), basis, False), cool)
        for damaged in ('wait', 5, ['60'], dict(known=True, basis='relative_seconds', seconds='60'), dict(known=True, basis='relative_seconds', seconds=True),
                        dict(known=True, basis='relative_seconds', seconds=-5), dict(known=True, not_before='not a time'), dict(known=True, not_before='2026-11-10T19:21:00'),
                        dict(known=True), dict(basis='something-new')):
            out = hold_until(damaged, self.NOW)                                  # a record that says a wait was stated but cannot be read: a person, not a guess
            self.assertEqual((out['until'], out['manual_release_required']), (None, True), damaged)


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
            self.assertEqual(e['manual_release_required'], False)


if __name__ == '__main__':
    unittest.main()
