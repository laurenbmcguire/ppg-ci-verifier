"""Two shared-consumer defects found by independent QA of cdeb5ca.

(1) The built-in fetch functions read only ONE Retry-After from an HTTP error, so a repeated header on the wire
    (Retry-After: 60 and retry-after: 3600) reached the parser as 60.
(2) The source-first service merged holds so that an existing finite hold survived a newer one with no end.

Real urllib HTTPError objects carrying real email.message.Message headers, raised by a mocked opener. No network: the
tests fail if a socket is opened. No token is read (the token functions are patched). Synthetic data only."""
import email.message
import itertools
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest import mock

from hosting import source_first as sf, source_first_capture as source, source_first_service as svc
from hosting.last_scan_drafts import hold_until
from source_first_fixture import SCOPE, capture
import test_source_first_daily as daily

NOW = datetime(2026, 11, 10, 19, 0, tzinfo=timezone.utc)
URL = '/sites/' + SCOPE['site_id'] + '/lists/' + SCOPE['list_id'] + '/items?$top=200'
YEAR_10000 = 'Sat, 01 Jan 10000 00:00:00 GMT'


def wire(*pairs):
    """Headers exactly as the HTTP client hands them over: one Message, possibly with the same name twice."""
    message = email.message.Message()
    for name, value in pairs:
        message[name] = value                                # Message keeps every occurrence, in order
    return message


def http_error(status, *pairs):
    return urllib.error.HTTPError('https://graph.microsoft.com/v1.0' + URL, status, 'synthetic', wire(*pairs), None)


class NoNetwork(unittest.TestCase):
    def setUp(self):
        for target in ('socket.socket.connect', 'socket.create_connection'):
            patcher = mock.patch(target, side_effect=AssertionError('network use: ' + target)); patcher.start(); self.addCleanup(patcher.stop)

    def opener_raising(self, error):
        class Opener:
            def open(inner, request, timeout=None):
                raise error
        patcher = mock.patch.object(urllib.request, 'build_opener', lambda *handlers: Opener()); patcher.start(); self.addCleanup(patcher.stop)

    def held(self, headers):
        return hold_until(source.cooldown(headers), NOW)


class GraphFetchKeepsEveryRetryAfter(NoNetwork):
    def fetch(self, *pairs, status=429):
        self.opener_raising(http_error(status, *pairs))
        with mock.patch('hosting.sharepoint._token_value', return_value='synthetic-not-a-token'):
            return source.graph_fetch(URL)

    def test_qa_repro_repeated_header_with_different_casing_is_not_reduced_to_the_first(self):
        status, body, headers = self.fetch(('Retry-After', '60'), ('retry-after', '3600'))
        self.assertEqual((status, body), (429, None))
        c = source.cooldown(headers)
        self.assertEqual((c['basis'], sorted(x['seconds'] for x in c['candidates'])), ('conflict', [60, 3600]))
        self.assertEqual(self.held(headers)['until'], NOW + timedelta(hours=1))               # was 60 seconds

    def test_both_orders_and_every_mix(self):
        cases = [((('Retry-After', '60'), ('retry-after', '3600')), NOW + timedelta(hours=1), False),
                 ((('Retry-After', '60'), ('Retry-After', '3600')), NOW + timedelta(hours=1), False),              # same casing, twice
                 ((('RETRY-AFTER', '60'), ('retry-after', 'Tue, 10 Nov 2026 21:00:00 GMT')), NOW + timedelta(hours=2), False),
                 ((('Retry-After', '86400'), ('retry-after', 'Tue, 10 Nov 2026 21:00:00 GMT')), NOW + timedelta(days=1), False),
                 ((('Retry-After', '3600'), ('retry-after', 'soon')), None, True), ((('Retry-After', '3600'), ('retry-after', YEAR_10000)), None, True),
                 ((('Retry-After', '3600'), ('retry-after', '9' * 40)), None, True), ((('Retry-After', 'soon'), ('retry-after', 'later')), None, True),
                 ((('Retry-After', '60'), ('retry-after', '120'), ('RETRY-AFTER', '7200')), NOW + timedelta(hours=2), False)]
        for pairs, until, manual in cases:
            for order in itertools.permutations(pairs):
                for status in (429, 503):
                    _, _, headers = self.fetch(*order, status=status)
                    out = self.held(headers)
                    self.assertEqual((out['until'], out['manual_release_required']), (until, manual), (order, status))

    def test_single_absent_and_identical_headers_are_unchanged(self):
        self.assertEqual(self.fetch(('Retry-After', '3600'))[2], {'Retry-After': '3600'})
        self.assertEqual(self.fetch(('retry-after', '3600'))[2], {'Retry-After': '3600'})
        self.assertEqual(self.fetch()[2], {'Retry-After': None})
        self.assertEqual(self.held(self.fetch()[2])['basis'], 'default')
        self.assertEqual(self.held(self.fetch(('Retry-After', '3600'), ('retry-after', '3600'))[2])['until'], NOW + timedelta(hours=1))
        self.assertEqual(self.fetch(('X-Retry-After', '5'), ('Content-Type', 'application/json'))[2], {'Retry-After': None})

    def test_the_duplicate_reaches_the_failure_evidence_intact(self):
        self.opener_raising(http_error(503, ('Retry-After', '60'), ('retry-after', '3600')))
        with mock.patch('hosting.sharepoint._token_value', return_value='synthetic-not-a-token'), self.assertRaises(source.CaptureFailure) as caught:
            source.collection(URL, 'items', [], source.graph_fetch)
        cool = caught.exception.evidence['cooldown']
        self.assertEqual((cool['basis'], cool['known'], cool['not_before']), ('conflict', False, None))
        self.assertEqual(hold_until(cool, NOW)['until'], NOW + timedelta(hours=1))
        sf.canonical(caught.exception.evidence)                                 # still plain data that can be stored


class PhotoAndRegionalWrappersToo(NoNetwork):
    def test_photo_download_keeps_every_retry_after(self):
        for order in itertools.permutations([('Retry-After', '60'), ('retry-after', '3600')]):
            self.opener_raising(http_error(429, *order))
            with mock.patch('hosting.photo_embed._token', return_value='synthetic-not-a-token'):
                status, raw, headers = source.photo_download('https://example.sharepoint.com/sites/x/_api/photo/$value')
            self.assertEqual((status, raw), (429, b''))
            self.assertEqual(self.held(headers)['until'], NOW + timedelta(hours=1), order)
        self.opener_raising(http_error(503, ('Retry-After', '60'), ('retry-after', 'soon')))
        with mock.patch('hosting.photo_embed._token', return_value='synthetic-not-a-token'):
            self.assertTrue(self.held(source.photo_download('https://example.sharepoint.com/x')[2])['manual_release_required'])

    def test_regional_capture_keeps_every_retry_after(self):
        def contract(site_id, list_id, columns, opener=None):               # like the real one: it makes a request and swallows the HTTP error
            try:
                opener(urllib.request.Request('https://example.sharepoint.com/sites/x/_api/web/RegionalSettings/TimeZone'), timeout=5)
            except urllib.error.HTTPError:
                return None
        for order, until, manual in ((( ('Retry-After', '60'), ('retry-after', '3600')), NOW + timedelta(hours=1), False),
                                     ((('retry-after', '3600'), ('Retry-After', '60')), NOW + timedelta(hours=1), False),
                                     ((('Retry-After', '60'), ('retry-after', YEAR_10000)), None, True)):
            self.opener_raising(http_error(503, *order))
            with mock.patch('hosting.source_date_contracts.capture_contract', contract), self.assertRaises(source.CaptureFailure) as caught:
                source.regional_capture(SCOPE['site_id'], SCOPE['list_id'], {})
            out = hold_until(caught.exception.evidence['cooldown'], NOW)
            self.assertEqual((out['until'], out['manual_release_required']), (until, manual), order)


class ServiceHoldMerge(unittest.TestCase):
    """hosting.source_first_service on its own SQLite workspace: a hold with no end dominates in both orders."""
    setUp, tearDown, rid, new = daily.Durable.setUp, daily.Durable.tearDown, daily.Durable.rid, daily.Durable.new   # the existing service harness, without re-running its tests

    def fresh(self):
        self.tearDown(); self.setUp()
    def fail_with(self, cooldown):
        def fail(scope):
            raise source.CaptureFailure('Cooldown', dict(status=429, remaining_cursor='/x', pages=[], raw_pages=[], cooldown=cooldown))
        return fail

    def hold_row(self):
        with svc.transaction(self.h, 'inspect') as (_, _, db, _):
            row = db.execute('SELECT value_json FROM settings WHERE key=?', (svc._hold_key(SCOPE),)).fetchone()
        return None if not row else __import__('json').loads(row[0])

    def record(self, cooldown, gated=False):
        """A capture that fails with this cooldown. By default the read gate is bypassed, as it is for two overlapping
        captures that both passed the gate before either failed."""
        if gated:
            return svc.capture(self.h, dict(scope=SCOPE, request_id=self.rid()), capture_fn=self.fail_with(cooldown))
        with mock.patch.object(svc, '_check_hold', lambda db, scope: None):
            return svc.capture(self.h, dict(scope=SCOPE, request_id=self.rid()), capture_fn=self.fail_with(cooldown))

    def overlapping(self, cooldowns, workers=4):
        ids = [self.rid() for _ in cooldowns]
        def one(pair):
            return svc.capture(self.h, dict(scope=SCOPE, request_id=pair[0]), capture_fn=self.fail_with(pair[1]))
        with mock.patch.object(svc, '_check_hold', lambda db, scope: None):       # patched ONCE, around every worker
            with ThreadPoolExecutor(workers) as pool:
                return list(pool.map(one, zip(ids, cooldowns)))

    def blocked(self):
        with self.assertRaisesRegex(sf.ReviewError, 'cooldown') as caught:
            self.new()
        return str(caught.exception)

    def test_qa_repro_existing_finite_hold_then_a_newer_invalid_one(self):
        self.record(source.cooldown({'Retry-After': '3600'}))
        self.assertTrue(self.hold_row()['not_before'])
        self.record(source.cooldown({'Retry-After': 'soon'}))                  # stated, unreadable: no end
        self.assertIsNone(self.hold_row()['not_before'])                        # was still the finite 3600
        self.assertIn('verified provider recovery is required', self.blocked())

    def test_no_end_dominates_in_both_orders_and_survives_a_restart(self):
        finite = source.cooldown({'Retry-After': '3600'})
        for no_end in (source.cooldown({'Retry-After': 'soon'}), source.cooldown({'Retry-After': YEAR_10000}), source.cooldown({'Retry-After': '9' * 40}),
                       source.cooldown({'Retry-After': '60', 'retry-after': '3600'}), dict(retry_after=None, not_before=None, known=False)):
            for order in ((finite, no_end), (no_end, finite), (finite, no_end, finite), (no_end, finite, no_end)):
                self.fresh()
                for c in order:
                    self.record(c)
                self.assertIsNone(self.hold_row()['not_before'], (no_end.get('basis'), len(order)))
                self.assertIn('verified provider recovery is required', self.blocked())

    def test_finite_holds_keep_the_later_end_and_never_shorten(self):
        def stated(hours):
            return dict(retry_after=str(hours * 3600), known=True, basis='relative_seconds', seconds=hours * 3600,
                        not_before=(datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat())
        for order, longest in (((stated(1), stated(3)), 3), ((stated(3), stated(1)), 3), ((stated(2), stated(5), stated(1), stated(4)), 5)):
            self.fresh()
            for c in order:
                self.record(c)
            self.assertEqual(self.hold_row()['seconds'], longest * 3600, [c['seconds'] for c in order])
            self.assertIn('Do not read it before', self.blocked())

    def test_ends_are_compared_as_times_not_as_text(self):
        later_in_utc = dict(retry_after='x', known=True, not_before='2099-01-01T12:00:00+00:00')          # 12:00 UTC
        earlier_but_sorts_higher = dict(retry_after='y', known=True, not_before='2099-01-01T13:00:00+05:00')   # 08:00 UTC
        for order in ((later_in_utc, earlier_but_sorts_higher), (earlier_but_sorts_higher, later_in_utc)):
            self.fresh()
            for c in order:
                self.record(c)
            self.assertEqual(self.hold_row()['not_before'], '2099-01-01T12:00:00+00:00')

    def test_an_unreadable_stored_end_is_treated_as_no_end_never_as_expired(self):
        for bad in ('not a time', '2099-01-01T12:00:00', '', 5):
            self.fresh()
            self.record(dict(retry_after='x', known=True, not_before=bad))
            self.assertIn('verified provider recovery is required', self.blocked())
            self.record(source.cooldown({'Retry-After': '60'}))                 # a finite one cannot replace it
            self.assertIn('verified provider recovery is required', self.blocked())

    def test_overlapping_failures_keep_the_strictest(self):
        waits = [source.cooldown({'Retry-After': str(s)}) for s in (100, 600, 30, 3600, 45)] * 2
        self.overlapping(waits)
        self.assertEqual(self.hold_row()['seconds'], 3600)
        self.fresh()
        waits = [source.cooldown({'Retry-After': v}) for v in ('100', 'soon', '3600', '30')] * 2
        self.overlapping(waits)
        self.assertIsNone(self.hold_row()['not_before'])
        self.assertEqual(svc._check_hold.__module__, svc.__name__)               # the gate was put back

    def test_the_read_gate_and_recovery_semantics_are_unchanged(self):
        self.assertEqual(self.new()['status'], 'source_review')                 # no hold: reads normally
        self.fresh()
        expired = dict(retry_after='1', known=True, not_before=(datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat())
        self.record(expired)
        self.assertEqual(self.new()['status'], 'source_review')                 # a finite hold that has passed lets the read through
        self.fresh()
        self.record(dict(retry_after=None, not_before=None, known=False))       # 429 with no header at all: this service has always required a verified recovery
        self.assertIn('verified provider recovery is required', self.blocked())


if __name__ == '__main__':
    unittest.main()
