"""Contract tests for reading a real source, run ONLY against a synthetic read-only fake.

They exercise the real capture and adapter code with tests/demo_client_fixture.py (fictitious DEMO-CLIENT /
TEST-9001). No live source is contacted and no client data is involved. They prove what the adapter insists on;
they do not make any real list ready: that still needs a person-verified mapping and scan-time contract per list.
"""
import inspect
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import demo_client_fixture as demo
from hosting import source_first as sf, source_first_capture as source, last_scan_runner as runner, registry
from hosting.last_scan_drafts import FORBIDDEN_SCAN_FIELDS, capture_adapter, scope_key
from hosting.last_scan_local import StateStore, StoredDraftRouter, BlobArtifacts
from hosting.last_scan_service import source_adapter

F = demo.MAP['fields']
SCOPE1 = scope_key(dict(demo.SCOPE, client_id=demo.CLIENT, shift='1', zone='America/Chicago'))


def settings(**kw):
    return demo.assignment(**kw)


def adapt(src, scope=SCOPE1, **kw):
    return source_adapter(scope, settings(**kw), capture_fn=lambda s: src.capture(s))


class Harness(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(); self.addCleanup(self.folder.cleanup)
        self.store = StateStore(Path(self.folder.name) / 'state.db')

    def run_once(self, src, run_id='r1', clock=None, **kw):
        a = settings(**kw)
        router = StoredDraftRouter(BlobArtifacts(self.store), [a])
        fetch = lambda scope, s: source_adapter(scope, s, capture_fn=lambda sc: src.capture(sc))
        extra = dict(clock=clock) if clock else {}
        return runner.run_once(self.store, dict(enabled=True, assignments=[a]), fetch=fetch, render=router, run_id=run_id,
                               entries=demo.REGISTRY, **extra)

    def nothing_stored(self):
        import sqlite3
        with sqlite3.connect(self.store.path) as db:
            keys = [k for (k,) in db.execute("SELECT key FROM scan_draft_state WHERE value != '{}'")]
        return not any(k.startswith(('artifact:', 'outbox:')) for k in keys) and not any(len(k) == 64 for k in keys)


class PaginationCompleteness(Harness):
    def test_every_page_is_followed_and_the_chain_is_recorded(self):
        src = demo.Source(page_size=8)
        cap, _ = src.capture()
        self.assertEqual(len(cap['records']), 36)
        item_pages = [p for p in cap['coverage']['pages'] if p['kind'] == 'items']
        self.assertEqual((len(item_pages), [p['terminal'] for p in item_pages]), (5, [False, False, False, False, True]))
        self.assertTrue(all(p['status'] == 200 and p['page_hash'] for p in item_pages))
        self.assertTrue(cap['coverage']['items_terminal']); self.assertIsNone(cap['coverage']['remaining_cursor'])
        self.assertEqual(self.run_once(demo.Source(page_size=8))['counts'], {'draft_ready': 3})

    def test_a_failed_page_means_no_snapshot_no_timer_and_no_draft(self):
        for status in (500, 403, 404, 0):
            with self.subTest(status):
                self.setUp()
                src = demo.Source(page_size=8, answers={'$skiptoken=16': (status, None, {})})
                with self.assertRaises(source.CaptureFailure) as failure:
                    src.capture()
                self.assertFalse(failure.exception.evidence.get('items_terminal'))
                out = self.run_once(demo.Source(page_size=8, answers={'$skiptoken=16': (status, None, {})}))
                self.assertEqual(out['counts'], {'error': 3}); self.assertTrue(self.nothing_stored())

    def test_a_failed_page_is_requested_once_never_retried_inside_a_capture(self):
        src = demo.Source(page_size=8, answers={'$skiptoken=16': (500, None, {})})
        with self.assertRaises(source.CaptureFailure):
            src.capture()
        self.assertEqual(len([u for u in src.requests if '$skiptoken=16' in u]), 1)

    def test_continuation_to_another_host_list_or_a_repeated_cursor_fails_closed(self):
        base = "/sites/%s/lists/%s/items?$expand=fields&$top=999" % (demo.SITE, demo.LIST)
        other_list = base.replace(demo.LIST, 'ffffffff-0000-4000-8000-000000009001')
        for link in ('https://evil.invalid/v1.0' + base, other_list + '&$skiptoken=8', '', 7):
            with self.subTest(link=str(link)[:40]):
                self.setUp()
                first = lambda url, link=link: (200, {'value': demo.items()[:8], '@odata.nextLink': link})
                make = lambda: demo.Source(answers={'/items?': first})
                with self.assertRaises((source.CaptureFailure, sf.ReviewError, RuntimeError)):
                    make().capture()
                self.assertEqual(self.run_once(make())['counts'], {'error': 3}); self.assertTrue(self.nothing_stored())
        loop = demo.Source(answers={'/items?': lambda url: (200, {'value': demo.items()[:8], '@odata.nextLink': url})})
        with self.assertRaises((source.CaptureFailure, sf.ReviewError, RuntimeError)):
            loop.capture()
        self.assertLessEqual(len([u for u in loop.requests if '/items?' in u]), 2)

    def test_duplicate_ids_across_pages_are_refused_not_deduplicated(self):
        rows = demo.items(); rows[10] = dict(rows[2])
        with self.assertRaises(sf.ReviewError):
            demo.Source(rows=rows, page_size=8).capture()


class ExactSourceShiftMappingTimezone(Harness):
    def test_the_list_must_identify_itself_as_the_configured_list(self):
        src = demo.Source(list_id='ffffffff-0000-4000-8000-000000009001')
        with self.assertRaises(source.CaptureFailure):
            src.capture()
        self.assertEqual(len(src.requests), 1)                              # nothing else was read from the wrong list

    def test_a_capture_for_another_scope_is_refused(self):
        other = dict(demo.SCOPE, job='TEST-9002')
        with self.assertRaises(ValueError):
            source_adapter(SCOPE1, settings(), capture_fn=lambda s: demo.Source().capture(other))

    def test_only_rows_of_the_exact_shift_are_in_a_scope(self):
        for shift in ('1', '2', '3'):
            snap = adapt(demo.Source(), scope_key(dict(demo.SCOPE, client_id=demo.CLIENT, shift=shift, zone='America/Chicago')))
            ids = sorted(int(r['id']) for r in snap['rows'])
            first = (int(shift) - 1) * demo.ROWS_PER_SHIFT + 1
            self.assertEqual(ids, list(range(first, first + demo.ROWS_PER_SHIFT)))
            self.assertEqual(snap['unassigned_shifts'], {})

    def test_a_row_without_a_shift_value_blocks_the_scope(self):
        rows = demo.items(); rows[4]['fields'][F['shift']] = None
        with self.assertRaises(ValueError):
            adapt(demo.Source(rows=rows))
        rows = demo.items(); del rows[4]['fields'][F['shift']]
        with self.assertRaises(ValueError):
            adapt(demo.Source(rows=rows))

    def test_rows_with_a_shift_no_assignment_covers_are_counted_and_reported_never_dropped_silently(self):
        rows = demo.items()
        for i in (30, 31, 32):
            rows[i]['fields'][F['shift']] = 'Night'                         # a label nobody verified
        out = self.run_once(demo.Source(rows=rows))
        self.assertEqual(out['counts'], {'draft_ready': 3})
        for result in out['results']:
            self.assertEqual(result['unassigned_shifts'], {'Night': 3})
        a = settings(); publisher = StoredDraftRouter(BlobArtifacts(self.store), [a]).for_scope(a)
        op = out['results'][0]['operation_id']
        self.assertEqual(json.loads(publisher.read(op, 'checklist.json'))['unassigned_shifts'], {'Night': 3})
        self.assertIn('3 row(s) with shift "Night"', publisher.read(op, 'checklist.txt').decode())
        ids = {i for r in out['results'] for i in json.loads(publisher.read(r['operation_id'], 'checklist.json'))['source_ids']}
        self.assertEqual(len(ids), 33); self.assertFalse(ids & {'31', '32', '33'})   # not guessed into any shift

    def test_shift_values_must_match_exactly(self):
        rows = demo.items(other_day=False)
        for r in rows[:12]:
            r['fields'][F['shift']] = '1st'                                 # looks like shift 1, is not the configured value
        snap = adapt(demo.Source(rows=rows))
        self.assertEqual((snap['rows'], snap['unassigned_shifts']), ([], {'1st': 12}))

    def test_timezone_must_be_explicit_and_real(self):
        for zone in (None, '', 'Central', 'UTC-6'):
            with self.assertRaises(Exception):
                scope_key(dict(demo.SCOPE, client_id=demo.CLIENT, shift='1', zone=zone))

    def test_unknown_business_dates_block_the_day(self):
        rows = demo.items(); rows[3]['fields'][F['date']] = '2026-11-10T22:15:00Z'   # a timestamp, not a date: day unknown
        cap, _ = demo.Source(rows=rows).capture()
        self.assertEqual(cap['uncertain_dates'], ['4'])
        with self.assertRaises(ValueError):
            adapt(demo.Source(rows=rows))

    def test_mapping_comes_only_from_the_assignment(self):
        a = {k: v for k, v in settings().items() if k not in ('mapping', 'header')}
        router = StoredDraftRouter(BlobArtifacts(self.store), [a])
        with self.assertRaises(LookupError):
            router.for_scope(a)


class ActualScanTimeContract(Harness):
    def test_nothing_is_read_until_the_scan_time_meaning_is_verified(self):
        for change in (dict(timestamp_semantics_verified=False), dict(timestamp_semantics_verified=None), dict(timestamp_semantics_verified='yes')):
            src = demo.Source()
            if change['timestamp_semantics_verified'] == 'yes':
                continue                                                    # truthy text is refused by the runner, covered there
            with self.assertRaises(ValueError):
                adapt(src, **change)
            self.assertEqual(src.requests, [])

    def test_date_created_modified_and_their_spellings_are_never_a_scan_field(self):
        self.assertTrue({'date', 'date0', 'created', 'createddatetime', 'modified', 'lastmodifieddatetime'} <= FORBIDDEN_SCAN_FIELDS)
        for name in ('Date', 'Date0', 'Created', 'Modified', 'createdDateTime', 'lastModifiedDateTime', ' created ', 'CREATED', '', None, 5):
            src = demo.Source()
            with self.assertRaises(ValueError, msg=name):
                adapt(src, scan_field=name)
            self.assertEqual(src.requests, [], name)                        # refused before any read
            if isinstance(name, str) and name.strip():
                out = runner.plan(dict(assignments=[settings(scan_field=name)]), demo.REGISTRY, '2026-11-10T21:35:00Z')
                self.assertEqual([i['code'] for i in out[1]], ['scan_field_is_date_or_arrival_metadata'], name)

    def test_a_missing_scan_time_is_never_replaced_by_created_or_modified(self):
        rows = demo.items()
        rows[2]['fields']['ActualScan'] = None
        rows[2]['fields']['Created'] = '2026-11-10T20:00:00Z'; rows[2]['createdDateTime'] = '2026-11-10T20:00:00Z'
        self.assertTrue(rows[2]['lastModifiedDateTime'])                     # arrival metadata IS present on the item
        with self.assertRaises(ValueError):
            adapt(demo.Source(rows=rows))
        out = self.run_once(demo.Source(rows=rows))
        by = {r['shift']: r['status'] for r in out['results']}
        self.assertEqual(by, {'1': 'error', '2': 'draft_ready', '3': 'draft_ready'})   # only the affected shift is held

    def test_naive_date_only_or_unparseable_scan_times_are_refused(self):
        for bad in ('2026-11-10T20:30:00', '2026-11-10', '11/10/2026 8:30 PM', 1794342600, True):
            rows = demo.items(); rows[0]['fields']['ActualScan'] = bad
            with self.assertRaises(ValueError, msg=bad):
                adapt(demo.Source(rows=rows))

    def test_scan_times_drive_the_timer_not_modified_times(self):
        rows = demo.items(scanned_at='2026-11-10T20:30:00+00:00')
        for r in rows:
            r['lastModifiedDateTime'] = '2026-11-10T21:34:00Z'               # edited a minute ago; scanned an hour ago
        at = '2026-11-10T21:35:00+00:00'
        with mock.patch.object(sf, 'now', lambda: at):
            out = self.run_once(demo.Source(rows=rows), clock=lambda: at)
        self.assertEqual(out['counts'], {'draft_ready': 3})


class PhotoAuthorisationAndFailures(Harness):
    def test_401_and_403_are_recorded_as_refused_and_other_failures_keep_their_status(self):
        for status, refused in ((401, True), (403, True), (404, False), (500, False), (0, False)):
            cap, blobs = demo.Source(photo_status=status).capture()
            self.assertEqual(({p['status'] for p in cap['photos']}, {p['http_status'] for p in cap['photos']}, blobs), ({'unavailable'}, {status}, {}))
            self.assertEqual(all('refused permission' in p['reason'] for p in cap['photos']), refused, status)

    def test_a_throttled_photo_stops_the_whole_capture_instead_of_dropping_the_photo(self):
        src = demo.Source(photo_status=429)
        with self.assertRaises(source.CaptureFailure) as failure:
            src.capture()
        self.assertTrue(failure.exception.evidence['cooldown']); self.assertEqual(len(src.photo_requests), 1)

    def test_photo_requests_go_only_to_the_configured_source_host_and_list(self):
        src = demo.Source(); src.capture()
        for url in src.photo_requests:
            self.assertTrue(url.startswith('https://%s/sites/DemoClient/_api/web/lists(guid\'%s\')/items(' % (demo.HOST, demo.LIST)), url)
            self.assertTrue(url.endswith("/$value"))

    def test_a_photo_named_with_a_path_or_odd_characters_is_not_requested(self):
        rows = demo.items()
        rows[2]['fields']['DefectPhoto'] = json.dumps({'fileName': '../../secret.jpg', 'type': 'thumbnail'})
        rows[7]['fields']['DefectPhoto'] = json.dumps({'fileName': "x')/other('y.jpg", 'type': 'thumbnail'})
        src = demo.Source(rows=rows); cap, _ = src.capture()
        self.assertEqual(len(src.photo_requests), 2)                         # only the two well-formed names
        self.assertEqual({p['status'] for p in cap['photos']}, {'unavailable'})


class UnknownQuantities(Harness):
    def test_absent_null_blank_and_text_quantities_stay_unknown_through_to_the_draft(self):
        rows = demo.items(nok=1)
        del rows[0]['fields'][F['rejected']]; rows[1]['fields'][F['rejected']] = None; rows[2]['fields'][F['rejected']] = ''
        rows[3]['fields'][F['rejected']] = 'n/a'; rows[4]['fields'][F['rejected']] = '1,5'; rows[5]['fields'][F['rejected']] = -1
        cap, _ = demo.Source(rows=rows).capture()
        got, findings = sf.reviewed_rows(cap, demo.MAP, [])
        by = {r['id']: r['resolved']['rejected'] for r in got}
        self.assertEqual([by[str(i)] for i in range(1, 8)], [None, None, None, None, None, None, 1])
        codes = sorted((f['record_id'], f['code']) for f in findings if f.get('field') == 'rejected')
        self.assertEqual(codes, [('1', 'missing_value'), ('2', 'missing_value'), ('3', 'missing_value'),
                                 ('4', 'invalid_quantity'), ('5', 'invalid_quantity'), ('6', 'invalid_quantity')])


class RateLimitsAndRetries(Harness):
    def throttled(self, headers):
        return demo.Source(answers={'/items?': (429, None, headers)})

    def test_throttling_holds_the_site_and_is_not_retried_early(self):
        from datetime import datetime, timedelta
        clock = ['2026-11-10T21:35:00+00:00']
        with mock.patch.object(sf, 'now', lambda: clock[0]):
            src = self.throttled({'Retry-After': '600'})
            first = self.run_once(src, 'r1', clock=lambda: clock[0])
            self.assertEqual(sorted(r['status'] for r in first['results']), ['cooldown', 'cooldown', 'error'])
            self.assertEqual(len([u for u in src.requests if '/items?' in u]), 1)       # asked once, then left alone
            # The provider's own "wait 600 seconds" is honoured from the moment it was received (real time).
            until = datetime.fromisoformat([r for r in first['results'] if r['status'] == 'error'][0]['cooldown_until'])
            good = demo.Source(rows=demo.items(scanned_at=(until - timedelta(hours=2)).isoformat()))
            clock[0] = (until - timedelta(seconds=1)).isoformat()
            self.assertEqual(self.run_once(good, 'r2', clock=lambda: clock[0])['counts'], {'cooldown': 3})
            self.assertEqual(good.requests, [])                                         # not even the list identity was read
            clock[0] = until.isoformat()
            self.assertEqual(self.run_once(good, 'r3', clock=lambda: clock[0])['counts'], {'draft_ready': 3})

    def test_unstated_or_unreadable_retry_time_waits_the_default(self):
        for headers in ({}, {'Retry-After': None}, {'Retry-After': 'soon'}):
            self.setUp()
            clock = '2026-11-10T21:35:00+00:00'
            with mock.patch.object(sf, 'now', lambda: clock):
                out = self.run_once(self.throttled(headers), clock=lambda: clock)
            held = [r for r in out['results'] if r.get('cooldown_until')]
            self.assertEqual([r['cooldown_until'] for r in held], ['2026-11-10T21:50:00+00:00'], headers)

    def test_service_unavailable_on_the_list_identity_also_holds(self):
        clock = '2026-11-10T21:35:00+00:00'
        src = demo.Source(answers={'?$select=id': (503, None, {'Retry-After': '120'})})
        with mock.patch.object(sf, 'now', lambda: clock):
            out = self.run_once(src, clock=lambda: clock)
        self.assertEqual(sorted(r['status'] for r in out['results']), ['cooldown', 'cooldown', 'error'])
        self.assertEqual(len(src.requests), 1)


class OnlyReadRequests(unittest.TestCase):
    def test_the_live_transports_issue_only_get_requests_without_a_body_and_never_follow_redirects(self):
        seen = []
        class Opener:
            def __init__(self, handlers): self.handlers = handlers
            def open(self, request, timeout=None):
                seen.append((request.get_method(), request.data, request.full_url, [type(h).__name__ for h in self.handlers]))
                raise OSError('synthetic: no network in tests')
        with mock.patch('urllib.request.build_opener', lambda *handlers: Opener(handlers)), \
                mock.patch('hosting.sharepoint._token_value', return_value='synthetic-not-a-token'), \
                mock.patch('hosting.photo_embed._token', return_value='synthetic-not-a-token'):
            self.assertEqual(source.graph_fetch('/sites/x/lists/y/items?$top=1')[0], 0)
            self.assertEqual(source.photo_download("https://example.sharepoint.com/sites/A/_api/web/lists(guid'x')/items(1)/AttachmentFiles('a.jpg')/$value")[0], 0)
        self.assertEqual([(m, d) for m, d, _, _ in seen], [('GET', None), ('GET', None)])
        self.assertTrue(seen[0][2].startswith('https://graph.microsoft.com/v1.0/sites/x/'))
        self.assertTrue(all('NoRedirect' in handlers for *_, handlers in seen))

    def test_the_graph_transport_refuses_other_hosts_before_any_request(self):
        with mock.patch('urllib.request.build_opener', side_effect=AssertionError('network touched')), \
                mock.patch('hosting.sharepoint._token_value', side_effect=AssertionError('token read')):
            for url in ('https://evil.invalid/v1.0/sites/x', 'http://graph.microsoft.com/v1.0/sites/x', 'https://graph.microsoft.com/beta/sites/x',
                        '/sites/x/../../admin', '//evil.invalid/x'):
                with self.assertRaises(Exception, msg=url):
                    source.graph_fetch(url)

    def test_no_write_verb_or_request_body_appears_in_the_capture_or_adapter_code(self):
        root = Path(source.__file__).parent
        for name in ('source_first_capture.py', 'last_scan_drafts.py', 'last_scan_service.py', 'last_scan_runner.py', 'last_scan_hosted.py'):
            text = (root / name).read_text()
            for verb in ("'POST'", '"POST"', "'PATCH'", "'PUT'", "'DELETE'", "'MERGE'", 'method=', '.post(', '.patch(', '.put(', '.delete(', 'X-HTTP-Method'):
                if name == 'last_scan_hosted.py' and verb == "'POST'":
                    continue                                                 # the entry point only ACCEPTS a POST; it sends none
                self.assertNotIn(verb, text, (name, verb))
            self.assertIsNone(re.search(r'[(,]\s*data\s*=', text), name)     # no request body is ever passed to a call

    def test_the_fake_source_was_only_ever_asked_to_read(self):
        src = demo.Source(); src.capture()
        self.assertEqual({type(u) for u in src.requests + src.photo_requests}, {str})
        self.assertFalse([n for n in dir(src) if re.search('write|update|create|delete|post|patch', n, re.I)])
        self.assertEqual(list(inspect.signature(src.get).parameters), ['url'])   # a request is a URL and nothing else


class RealListsStayBlocked(unittest.TestCase):
    """Synthetic proof does not make any real list ready."""
    def test_no_seed_registry_entry_can_satisfy_an_assignment(self):
        seeded = registry.entries([])
        self.assertTrue(seeded)
        for entry in seeded:
            self.assertFalse(entry.get('site_id'))                           # seed entries carry site names only
            a = settings(client_id=str(entry.get('client_key')), job=str(entry.get('job')), list_id=str(entry.get('list_id') or demo.LIST),
                         site_id=str(entry.get('site') or demo.SITE))
            scopes, issues = runner.plan(dict(assignments=[a]), seeded, '2026-11-10T21:35:00Z')
            self.assertEqual((scopes, [i['code'] for i in issues]), ([], ['not_in_verified_job_registry']))

    def test_the_demo_assignment_itself_is_refused_without_its_own_registry_entry(self):
        scopes, issues = runner.plan(dict(assignments=[settings()]), registry.entries([]), '2026-11-10T21:35:00Z')
        self.assertEqual((scopes, [i['code'] for i in issues]), ([], ['not_in_verified_job_registry']))

    def test_no_assignment_or_enabled_configuration_ships_in_the_repository(self):
        root = Path(source.__file__).resolve().parents[1]
        for path in list((root / 'hosting').glob('*.py')) + list((root / 'api').rglob('*.py')) + [root / 'vercel.json']:
            if path.is_file() and not path.name.startswith('last_scan_'):
                text = path.read_text(errors='ignore')
                self.assertNotIn('last_scan_runner', text, path.name); self.assertNotIn('timestamp_semantics_verified', text, path.name)
        for name in ('last_scan_runner.py', 'last_scan_hosted.py', 'last_scan_service.py'):
            text = (root / 'hosting' / name).read_text()
            self.assertIsNone(re.search(r"timestamp_semantics_verified\s*[=:]\s*True", text), name)
        from hosting.last_scan_service import ScheduledWorker
        from hosting import last_scan_hosted
        self.assertIs(inspect.signature(ScheduledWorker.__init__).parameters['enabled'].default, False)
        self.assertEqual(runner.run_once(None, {}, fetch=None, render=None, run_id='x'), dict(status='disabled'))
        self.assertIsNone(last_scan_hosted.Deps().config)


if __name__ == '__main__':
    unittest.main()
