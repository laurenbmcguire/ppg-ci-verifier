"""Read-only schema snapshot collection. MOCKED transport and synthetic column definitions only: no Graph request,
no credential, no token, no real list. Nothing here is a collector, a capture authority or origin proof."""
import json
import inspect
import unittest
from copy import deepcopy
from pathlib import Path
from urllib.parse import quote
import demo_client_fixture as demo
import readiness_fixture as rf
from hosting import last_scan_hosted as hosted, last_scan_readiness as readiness, last_scan_schema_capture as cap
from hosting.last_scan_schema_capture import SchemaCaptureFailure, capture_schema, receipt_candidate
from test_last_scan_hosted import COLLECTOR, FakeCaptures, Hosted, NOW, bare_assignment

ROOT = Path(__file__).resolve().parents[1]
HOST_NOW = '2026-11-10T21:35:00+00:00'
G = 'https://graph.microsoft.com/v1.0'


class FakeGraph:
    """TEST ONLY mocked transport. Answers GETs from memory; records every URL it is asked for."""
    def __init__(self, site_id, list_id, columns, page_size=5, name='Synthetic list'):
        self.site_id, self.list_id, self.page_size = site_id, list_id, page_size
        self.columns = deepcopy(columns)                     # a list of Graph-style column definitions
        self.base = '/sites/' + quote(site_id, safe='') + '/lists/' + quote(list_id, safe='')
        self.meta = dict(id=list_id, displayName=name, webUrl='https://example.sharepoint.com/sites/Synthetic/Lists/x',
                         eTag='"etag-1"', lastModifiedDateTime='2099-01-01T00:00:00Z', parentReference=dict(siteId=site_id))
        self.requests, self.script = [], None                # script(url, n, kind) -> response or None

    def kind(self, url):
        path = url[len(G):] if url.startswith(G) else url
        return 'list' if path.startswith(self.base + '?') else 'columns' if path.startswith(self.base + '/columns') else 'other'

    def page(self, url):
        start = int(url.split('skiptoken=')[1]) if 'skiptoken=' in url else 0
        chunk = self.columns[start:start + self.page_size]
        out = dict(value=deepcopy(chunk))
        if start + self.page_size < len(self.columns):
            out['@odata.nextLink'] = f'{G}{self.base}/columns?$top=500&$skiptoken={start + self.page_size}'
        return out

    def __call__(self, url):
        self.requests.append(url)
        kind = self.kind(url)
        if self.script:
            scripted = self.script(url, len(self.requests), kind)
            if scripted is not None:
                return scripted
        if kind == 'list':
            return 200, deepcopy(self.meta), {}
        if kind == 'columns':
            return 200, self.page(url), {}
        return 404, None, {}


def graph_for(assignment, **kw):
    schema = rf.schema_for(assignment)
    return FakeGraph(assignment['site_id'], assignment['list_id'], list(schema['columns'].values()), **kw), schema


class Base(unittest.TestCase):
    def setUp(self):
        self.assignment = demo.assignment()
        self.graph, self.schema = graph_for(self.assignment)
        self.now = HOST_NOW

    def capture(self, graph=None, **kw):
        g = graph or self.graph
        return capture_schema(kw.pop('site_id', g.site_id), kw.pop('list_id', g.list_id), fetch=kw.pop('fetch', g), clock=kw.pop('clock', lambda: self.now))

    def fails(self, stage, graph=None, **kw):
        with self.assertRaises(SchemaCaptureFailure) as caught:
            self.capture(graph, **kw)
        e = caught.exception.evidence
        self.assertEqual((e['stage'], e['accepted']), (stage, False), str(caught.exception))
        self.assertNotIn('snapshot', e); self.assertNotIn('columns', e)          # never a partial snapshot
        return caught.exception

    def scripted(self, script, **kw):
        graph, _ = graph_for(self.assignment, **kw); graph.script = script
        return graph


class CompleteCapture(Base):
    def test_artifact_is_candidate_evidence_and_nothing_more(self):
        a = self.capture()
        self.assertEqual((a['kind'], a['status'], a['marked_verified'], a['grants_authority']), ('candidate_schema_capture', 'candidate_unreviewed', False, False))
        self.assertEqual(a['provenance'], dict(collector='not_established_by_this_code', transport='supplied_by_caller_not_identified_by_this_code',
                                               origin='unverified_candidate', signed=False))
        self.assertEqual((a['samples']['status'], a['field_candidates']['scan_field'], a['field_candidates']['mapping']), ('not_performed', 'not_determined', 'not_determined'))
        self.assertEqual(len(a['must_be_established']), len(cap.MUST_BE_ESTABLISHED))
        text = json.dumps({k: v for k, v in a.items() if k not in ('snapshot', 'inventory')}).lower()
        for claim in ('"verified": true', 'trusted', 'authentic', 'origin_verified', 'is the scan', 'likely', 'recommended', 'suggested'):
            self.assertNotIn(claim, text)

    def test_exact_ids_internal_names_and_full_definitions_are_preserved(self):
        a = self.capture()
        s = a['snapshot']
        self.assertEqual((s['site_id'], s['list_id'], s['list']), (self.graph.site_id, self.graph.list_id, dict(id=self.graph.list_id, displayName='Synthetic list')))
        self.assertEqual(s['columns'], {c['name']: c for c in self.graph.columns})         # every key and value exactly as returned
        self.assertEqual(list(s['columns']), [c['name'] for c in self.graph.columns])     # source order kept
        self.assertEqual((a['snapshot_identity'], a['schema_digest']), (readiness.snapshot_identity(s), readiness.schema_digest(s)))
        self.assertEqual(readiness.schema_digest(s), readiness.schema_digest(dict(self.schema, list=s['list'], captured_at=s['captured_at'])))

    def test_pagination_is_complete_and_each_pass_reaches_a_terminal_page(self):
        a = self.capture()
        o = a['observed']
        pages_per_pass = -(-len(self.graph.columns) // self.graph.page_size)
        self.assertGreater(pages_per_pass, 1)
        self.assertEqual((o['capture_passes'], o['passes_agree'], o['complete'], o['column_count']), (2, True, True, len(self.graph.columns)))
        self.assertEqual([r['capture_pass'] for r in o['requests']], [1] * pages_per_pass + [2] * pages_per_pass)
        self.assertEqual([r['terminal'] for r in o['requests']], ([False] * (pages_per_pass - 1) + [True]) * 2)
        self.assertEqual(sum(r['count'] for r in o['requests'] if r['capture_pass'] == 1), len(self.graph.columns))
        self.assertEqual(len(self.graph.requests), 2 + 2 * pages_per_pass)       # identity, two passes, identity again

    def test_only_this_lists_metadata_is_requested_and_never_any_item(self):
        self.capture()
        for url in self.graph.requests:
            self.assertIn(self.graph.kind(url), ('list', 'columns'), url)
            self.assertNotIn('/items', url); self.assertNotIn('$expand', url)
        source = inspect.getsource(cap)
        for word in ('POST', 'PATCH', 'PUT', 'DELETE', 'method=', 'data=', '/items', 'urllib.request', 'urlopen', 'os.environ', 'getenv', 'token', 'Authorization', 'graph_fetch'):
            self.assertNotIn(word, source, word)

    def test_times_come_from_the_host_clock_never_from_source_text(self):
        a = self.capture()
        self.assertEqual((a['snapshot']['captured_at'], a['observed']['started_at'], a['observed']['completed_at']), (HOST_NOW,) * 3)
        self.assertEqual(a['observed']['source_text_not_used_as_time']['list_last_modified_before'], '2099-01-01T00:00:00Z')   # kept only as quoted text
        ticks = iter(['2026-11-10T15:35:00-06:00', '2026-11-10T21:35:09Z'])
        a = self.capture(clock=lambda: next(ticks))
        self.assertEqual((a['observed']['started_at'], a['snapshot']['captured_at']), ('2026-11-10T21:35:00+00:00', '2026-11-10T21:35:09+00:00'))

    def test_field_candidates_group_by_discovered_type_without_ranking_or_guessing(self):
        fc = self.capture()['field_candidates']
        self.assertEqual(fc['by_discovered_type']['dateTime'], sorted(['ActualScan', demo.MAP['fields']['date']]))   # both, alphabetical, no preference
        self.assertEqual(fc['by_discovered_type']['number'], sorted(demo.MAP['fields'][q] for q in ('inspected', 'rejected', 'certified')))
        self.assertIn('not thereby the scan time', fc['note'])
        self.assertEqual(sorted(fc), ['by_discovered_type', 'mapping', 'note', 'scan_field'])

    def test_the_snapshot_is_the_shape_the_readiness_validator_reads(self):
        snapshot = self.capture()['snapshot']
        report = readiness.validate(self.assignment, snapshot, demo.REGISTRY)    # unpinned, unattested: discovered checks pass, meaning is not established
        self.assertFalse(report['ready_for_dry_run'])
        self.assertNotIn('schema_columns_are_well_formed', report['blocked_reasons']); self.assertNotIn('schema_column_identities_are_consistent', report['blocked_reasons'])
        self.assertIn('scan_time_meaning_attested', report['blocked_reasons'])
        pinned, _ = rf.attested(self.assignment, snapshot)                       # TEST-ONLY fixture reviewer
        self.assertTrue(readiness.validate(pinned, snapshot, demo.REGISTRY)['ready_for_dry_run'])

    def test_item_activity_during_capture_is_reported_not_treated_as_schema_change(self):
        def script(url, n, kind):
            if kind == 'list' and n > 1:
                return 200, dict(graph.meta, eTag='"etag-2"', lastModifiedDateTime='2099-01-01T00:05:00Z'), {}
        graph = self.scripted(script)
        self.assertTrue(self.capture(graph)['observed']['list_items_changed_during_capture'])
        self.assertFalse(self.capture()['observed']['list_items_changed_during_capture'])


class FailsClosed(Base):
    def test_no_defaults_and_bad_identifiers_make_no_request(self):
        with self.assertRaises(TypeError):
            capture_schema(self.graph.site_id, self.graph.list_id)
        self.assertEqual([p.default for p in inspect.signature(capture_schema).parameters.values() if p.name in ('fetch', 'clock')], [inspect.Parameter.empty] * 2)
        for kw in (dict(fetch=None), dict(clock=None), dict(fetch='graph'), dict(site_id='DemoClient'), dict(site_id='https://example.sharepoint.com/sites/x'),
                   dict(site_id=self.graph.site_id.replace('.sharepoint.com', '.example.com')), dict(list_id='Inspection list'), dict(list_id=None), dict(site_id=None),
                   dict(list_id=self.graph.list_id + '/items')):
            self.fails('setup', **kw)
        self.assertEqual(self.graph.requests, [])

    def test_permission_not_found_redirect_server_and_network_errors(self):
        for status, words in ((401, 'not permitted'), (403, 'not permitted'), (404, 'not found'), (301, 'redirect'), (302, 'redirect'), (307, 'redirect'),
                              (500, 'HTTP 500'), (502, 'HTTP 502'), (0, 'could not be reached'), (204, 'HTTP 204')):
            for stage, kind, at in (('list_identity', 'list', 1), ('columns', 'columns', None)):
                def script(url, n, k, status=status, kind=kind):
                    if k == kind: return status, None, {}
                error = self.fails(stage, self.scripted(script))
                if stage == 'list_identity':
                    self.assertIn(words, str(error)); self.assertEqual(error.evidence['status'], status)

    def test_redirect_targets_are_never_requested(self):
        def script(url, n, kind):
            if kind == 'columns': return 302, None, {'Location': 'https://evil.example/v1.0/columns'}
        graph = self.scripted(script)
        self.fails('columns', graph)
        self.assertTrue(all('evil.example' not in u for u in graph.requests)); self.assertEqual(len(graph.requests), 2)

    def test_throttling_keeps_the_cooldown_and_is_not_retried(self):
        for status in (429, 503):
            for stage, kind in (('list_identity', 'list'), ('columns', 'columns')):
                def script(url, n, k, status=status, kind=kind):
                    if k == kind: return status, None, {'Retry-After': '120'}
                graph = self.scripted(script)
                error = self.fails(stage, graph)
                self.assertEqual((error.evidence['status'], error.evidence['cooldown']['known'], error.evidence['cooldown']['retry_after']), (status, True, '120'))
                self.assertEqual(len(graph.requests), 1 if kind == 'list' else 2)          # asked once, never again

    def test_throttling_on_a_later_page_or_the_second_pass(self):
        def later(url, n, kind):
            if kind == 'columns' and 'skiptoken' in url: return 429, None, {'Retry-After': '30'}
        self.assertEqual(self.fails('columns', self.scripted(later)).evidence['capture_pass'], 1)
        seen = []
        def second(url, n, kind):
            if kind == 'columns':
                seen.append(url)
                if seen.count(url) == 2: return 503, None, {}
        error = self.fails('columns', self.scripted(second))
        self.assertEqual((error.evidence['capture_pass'], error.evidence['status']), (2, 503))

    def test_malformed_or_missing_pages(self):
        for body in (None, [], 'text', 5, {}, dict(value=None), dict(value='x'), dict(value={}), dict(items=[])):
            def script(url, n, kind, body=body):
                if kind == 'columns' and 'skiptoken' in url: return 200, body, {}
            self.fails('columns', self.scripted(script))
        for answer in ([200, {}, {}], 'ok', (200,), (200, {}, {}, {}), ('200', {}, {}), (True, {}, {}), {'status': 200}):
            def script(url, n, kind, answer=answer):
                if kind == 'columns': return answer
            self.fails('columns', self.scripted(script))
        def raises(url, n, kind):
            if kind == 'columns': raise ConnectionError('synthetic outage')
        self.fails('columns', self.scripted(raises))
        def nan(url, n, kind):
            if kind == 'columns' and 'skiptoken' not in url:
                page = self.graph.page(url); page['value'][0]['weight'] = float('nan'); return 200, page, {}
        self.fails('columns', self.scripted(nan))

    def test_next_links_that_leave_the_exact_collection_are_refused_and_never_fetched(self):
        base = self.graph.base
        other_list = base.rsplit('/', 1)[0] + '/44444444-4444-4444-4444-444444444444'
        links = ['https://evil.example/v1.0' + base + '/columns?$skiptoken=5', 'http://graph.microsoft.com/v1.0' + base + '/columns?$skiptoken=5',
                 'https://graph.microsoft.com/beta' + base + '/columns?$skiptoken=5', G + base + '/items?$skiptoken=5', G + other_list + '/columns?$skiptoken=5',
                 G + base + '/columns/../items', G + base + '/columns%2f..%2fitems', 'https://graph.microsoft.com.evil.example/v1.0' + base + '/columns',
                 'https://user:pw@graph.microsoft.com/v1.0' + base + '/columns', G + base + '/columns#frag', '', 5, ['x'], {'href': 'x'}, '//evil.example/x']
        for link in links:
            def script(url, n, kind, link=link):
                if kind == 'columns' and 'skiptoken' not in url:
                    page = self.graph.page(url); page['@odata.nextLink'] = link; return 200, page, {}
            graph = self.scripted(script)
            self.fails('columns', graph)
            self.assertEqual(len(graph.requests), 2, link)                       # identity + the first page; the link was not followed

    def test_repeated_cursor_and_endless_pagination(self):
        def loop(url, n, kind):
            if kind == 'columns':
                page = self.graph.page(url); page['@odata.nextLink'] = G + self.graph.base + '/columns?$top=500&$skiptoken=5'; return 200, page, {}
        self.fails('columns', self.scripted(loop))
        def endless(url, n, kind):
            if kind == 'columns':
                return 200, {'value': [dict(name='C%d' % n, id='id-%d' % n, text={})], '@odata.nextLink': G + self.graph.base + '/columns?$top=500&$skiptoken=%d' % (n * 7)}, {}
        graph = self.scripted(endless)
        self.fails('columns', graph)
        self.assertLessEqual(len(graph.requests), 102)

    def test_empty_and_oversized_column_sets(self):
        empty = FakeGraph(self.graph.site_id, self.graph.list_id, [])
        self.fails('columns', empty)
        many = FakeGraph(self.graph.site_id, self.graph.list_id, [dict(name='C%d' % i, id='id-%d' % i, text={}) for i in range(cap.MAX_COLUMNS + 1)], page_size=500)
        self.fails('columns', many)

    def test_host_clock_must_be_valid_and_must_not_run_backwards(self):
        for value in (None, '', 'now', '2026-11-10', '2026-11-10T21:35:00', 1762810500):
            self.fails('setup', clock=lambda: value); 
        self.assertEqual(self.graph.requests, [])
        ticks = iter([HOST_NOW, 'not a time'])
        self.fails('completion', clock=lambda: next(ticks))
        ticks = iter([HOST_NOW, '2026-11-10T21:34:59+00:00'])
        self.fails('completion', clock=lambda: next(ticks))


class IdentitySwaps(Base):
    def test_answer_for_another_list_or_site(self):
        other = '44444444-4444-4444-4444-444444444444'
        for change in (dict(id=other), dict(id=self.graph.list_id.upper()), dict(id=None), dict(id=5), dict(parentReference=dict(siteId='example.sharepoint.com,' + other + ',' + other)),
                       dict(parentReference=dict(siteId=self.graph.site_id.upper())), dict(displayName=['x'])):
            def script(url, n, kind, change=change):
                if kind == 'list': return 200, dict(graph.meta, **change), {}
            graph = self.scripted(script)
            self.fails('list_identity', graph)
            self.assertEqual(len(graph.requests), 1)                             # no column was read from the wrong list
        for body in (None, [], 'x', {}):
            def script(url, n, kind, body=body):
                if kind == 'list': return 200, body, {}
            self.fails('list_identity', self.scripted(script))

    def test_identity_swapped_between_the_first_look_and_the_last(self):
        def script(url, n, kind):
            if kind == 'list' and n > 1: return 200, dict(graph.meta, id='44444444-4444-4444-4444-444444444444'), {}
        graph = self.scripted(script)
        self.fails('list_identity_recheck', graph)
        def renamed(url, n, kind):
            if kind == 'list' and n > 1: return 200, dict(graph2.meta, displayName='Renamed'), {}
        graph2 = self.scripted(renamed)
        self.fails('consistency', graph2)

    def test_site_echo_absent_is_recorded_as_not_confirmed(self):
        def script(url, n, kind):
            if kind == 'list': return 200, {k: v for k, v in graph.meta.items() if k != 'parentReference'}, {}
        graph = self.scripted(script)
        self.assertFalse(self.capture(graph)['observed']['site_identity_echoed_by_source'])
        self.assertTrue(self.capture()['observed']['site_identity_echoed_by_source'])

    def test_duplicate_or_unidentified_columns(self):
        first = self.graph.columns[0]
        bad = dict(same_name=dict(first), same_id=dict(name='Other', displayName='Other', id=first['id'], text={}),
                   case_only=dict(name=first['name'].upper(), displayName='x', id='id-case', text={}),
                   no_name=dict(displayName='x', id='id-x', text={}), blank_name=dict(name=' ', id='id-x', text={}), padded=dict(name=' Padded', id='id-x', text={}),
                   no_id=dict(name='NoId', text={}), id_number=dict(name='NumId', id=7, text={}), not_object='column', none=None, nested_bad=dict(name='Odd', id='id-o', text={'x': {1, 2}}))
        for name, column in bad.items():
            graph = FakeGraph(self.graph.site_id, self.graph.list_id, self.graph.columns + [column])
            self.fails('columns', graph)
        across = FakeGraph(self.graph.site_id, self.graph.list_id, self.graph.columns + [dict(first)], page_size=len(self.graph.columns))   # duplicate arrives on a later page
        self.fails('columns', across)


class SchemaChangedDuringCapture(Base):
    def changed_on_second_pass(self, mutate):
        graph, _ = graph_for(self.assignment)
        seen = []
        def script(url, n, kind):
            if kind == 'columns':
                seen.append(url)
                if seen.count(url) == 2 and 'skiptoken' not in url:
                    mutate(graph.columns)
        graph.script = script
        return graph

    def test_any_difference_between_the_two_passes_is_refused(self):
        changes = dict(facet=lambda c: c[0].update(readOnly=True), calculated=lambda c: next(x for x in c if x['name'] == 'ActualScan').update(calculated={'formula': '=[Created]'}),
                       replaced=lambda c: c[1].update(id='replaced-id'), renamed=lambda c: c[2].update(displayName='Renamed'), removed=lambda c: c.pop(),
                       added=lambda c: c.append(dict(name='Added', displayName='Added', id='id-added', text={})), description=lambda c: c[3].update(description='changed'),
                       choice=lambda c: next(x for x in c if 'choice' in x)['choice']['choices'].append('Night'))
        for name, mutate in changes.items():
            error = self.fails('consistency', self.changed_on_second_pass(mutate))
            self.assertTrue(error.evidence['changed_columns'], name)
            self.assertEqual(len(set(error.evidence['pass_digests'])), 2, name)

    def test_a_change_before_or_after_both_passes_cannot_be_seen_and_this_is_said(self):
        self.assertTrue(any('only sees what the transport returned' in m for m in cap.MUST_BE_ESTABLISHED))
        doc = (ROOT / 'docs' / 'LAST_SCAN_SCHEMA_CAPTURE_RUNBOOK.md').read_text()
        self.assertIn('between the two passes', doc)


class UnknownGraphFacets(Base):
    def test_unknown_properties_are_kept_exactly_and_flagged_never_dropped_or_interpreted(self):
        extra = [dict(name='Where', displayName='Where', id='id-geo', geolocation={}),
                 dict(name='Tagged', displayName='Tagged', id='id-term', term={'allowMultipleValues': False}, text={}),
                 dict(name='Future', displayName='Future', id='id-fut', dateTime={'format': 'dateTime'}, somethingNew={'mode': 'x'}),
                 dict(name='Both', displayName='Both', id='id-both', dateTime={'format': 'dateTime'}, calculated={'formula': '=[Created]', 'outputType': 'dateTime'}),
                 dict(name='Bare', displayName='Bare', id='id-bare')]
        graph = FakeGraph(self.graph.site_id, self.graph.list_id, self.graph.columns + extra)
        a = self.capture(graph)
        for column in extra:
            self.assertEqual(a['snapshot']['columns'][column['name']], column)   # exactly as returned
        flagged = {r['column']: ' '.join(r['problems']) for r in a['columns_needing_review']}
        self.assertEqual(set(flagged), {'Where', 'Tagged', 'Future', 'Both'})
        self.assertIn('unrecognised column properties: geolocation', flagged['Where']); self.assertIn('somethingNew', flagged['Future'])
        self.assertIn('more than one type facet', flagged['Both'])
        types = a['field_candidates']['by_discovered_type']
        self.assertEqual((types['unknown'], types['contradictory']), (['Bare', 'Where'], ['Both']))
        self.assertNotIn('Both', types['dateTime'])                              # a contradictory column is not offered as a date-time

    def test_mapping_onto_a_flagged_column_stays_blocked_downstream(self):
        graph = FakeGraph(self.graph.site_id, self.graph.list_id, self.graph.columns + [dict(name='Future', displayName='Future', id='id-fut', dateTime={'format': 'dateTime'}, somethingNew={})])
        snapshot = self.capture(graph)['snapshot']
        pinned, _ = rf.attested(dict(self.assignment, scan_field='Future'), snapshot)
        report = readiness.validate(pinned, snapshot, demo.REGISTRY)
        self.assertIn('scan_field_type_facets_consistent', report['blocked_reasons'])


class ReceiptHandoff(Hosted):
    def setUp(self):
        super().setUp()
        self.bare = bare_assignment()
        self.graph, _ = graph_for(self.bare)
        self.artifact = capture_schema(self.bare['site_id'], self.bare['list_id'], fetch=self.graph, clock=lambda: '2026-11-10T21:30:00+00:00')
        self.snapshot = self.artifact['snapshot']
        self.config = dict(enabled=True, assignments=[rf.attested(self.bare, self.snapshot)[0]])        # TEST-ONLY fixture reviewer

    def reasons(self, **kw):
        code, body = self.call(self.deps(config=self.config, schemas=[self.snapshot], **kw))
        return body['mapping_readiness'][0]

    def test_candidate_is_the_content_an_authority_would_record_and_is_not_a_receipt(self):
        c = receipt_candidate(self.artifact, COLLECTOR)
        self.assertEqual(c, dict(snapshot_identity=self.artifact['snapshot_identity'], site_id=self.bare['site_id'], list_id=self.bare['list_id'],
                                 schema_digest=self.artifact['schema_digest'], collector=COLLECTOR, captured_at='2026-11-10T21:30:00+00:00',
                                 status='unsigned_candidate_not_a_receipt'))
        for collector in (None, '', ' ', COLLECTOR + ' ', 5):
            with self.assertRaises(ValueError):
                receipt_candidate(self.artifact, collector)
        for bad in (None, {}, dict(self.artifact, kind='receipt'), dict(self.artifact, snapshot=None)):
            with self.assertRaises(ValueError):
                receipt_candidate(bad, COLLECTOR)

    def test_an_artifact_altered_after_capture_cannot_be_handed_off(self):
        altered = deepcopy(self.artifact); altered['snapshot']['columns']['ActualScan']['description'] = 'edited'
        with self.assertRaises(ValueError):
            receipt_candidate(altered, COLLECTOR)
        altered = deepcopy(self.artifact); altered['snapshot']['captured_at'] = '2026-11-10T21:31:00+00:00'
        with self.assertRaises(ValueError):
            receipt_candidate(altered, COLLECTOR)

    def test_the_hosted_entry_point_ignores_the_artifact_and_the_candidate_until_an_authority_records_it(self):
        candidate = receipt_candidate(self.artifact, COLLECTOR)
        row = self.reasons(captures=FakeCaptures())
        self.assertEqual((row['ready'], row['capture']), (False, None)); self.assertIn('no_capture_receipt_for_this_exact_snapshot', row['blocked_reasons'])
        carrying = dict(self.snapshot, receipt=candidate, artifact=dict(kind='candidate_schema_capture'))        # stapling it to the snapshot changes nothing
        code, body = self.call(self.deps(config=self.config, schemas=[carrying], captures=FakeCaptures()))
        self.assertFalse(body['mapping_readiness'][0]['ready'])
        self.assertEqual(self.reasons(captures=None)['blocked_reasons'][-1], 'no_capture_authority_configured')
        authority = FakeCaptures()                                               # TEST ONLY: a fake authority recording the candidate after "its own checks"
        authority.records[candidate['snapshot_identity']] = {k: v for k, v in candidate.items() if k != 'status'}
        row = self.reasons(captures=authority)
        self.assertEqual((row['ready'], row['blocked_reasons']), (True, []))
        self.assertEqual(row['capture'], dict(collector=COLLECTOR, captured_at='2026-11-10T21:30:00+00:00', origin='reported_by_capture_authority'))
        self.assertEqual(self.reasons(captures=authority, collectors=('another-collector',))['ready'], False)

    def test_nothing_wires_the_adapter_in(self):
        for path in list((ROOT / 'api').rglob('*.py')) + [p for p in (ROOT / 'hosting').glob('*.py') if p.name != 'last_scan_schema_capture.py']:
            self.assertNotIn('last_scan_schema_capture', path.read_text(errors='ignore'), str(path))
        self.assertFalse(any(isinstance(getattr(cap, n), type) and issubclass(getattr(cap, n), (hosted.CaptureAuthority, hosted.SchemaProbe)) for n in dir(cap)))


if __name__ == '__main__':
    unittest.main()
