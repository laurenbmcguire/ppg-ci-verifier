"""Continuation links must not widen the schema read. Regression for the query-scope bypass found by independent QA
of 8956c99: a next link that added $expand was followed and the capture reported complete.
MOCKED transport and synthetic columns only."""
import unittest
import demo_client_fixture as demo
import readiness_fixture as rf
from hosting import last_scan_schema_capture as cap
from hosting.last_scan_schema_capture import SchemaCaptureFailure, capture_schema
from test_last_scan_schema_capture import FakeGraph, G, HOST_NOW, graph_for


class TokenGraph(FakeGraph):
    """TEST ONLY: pages addressed by opaque continuation tokens, the way a real source hands them out."""
    def __init__(self, *a, tokens, link=None, **kw):
        super().__init__(*a, **kw)
        self.tokens = list(tokens)                           # token for page 2, page 3, ...
        self.link = link or (lambda token: f'{G}{self.base}/columns?$top=500&$skiptoken={token}')

    def page(self, url):
        index = 0
        for i, token in enumerate(self.tokens):
            if url.endswith('$skiptoken=' + token) or ('$skiptoken=' + token + '&') in url:
                index = i + 1
        chunk = self.columns[index * self.page_size:(index + 1) * self.page_size]
        out = dict(value=[dict(c) for c in chunk])
        if (index + 1) * self.page_size < len(self.columns):
            out['@odata.nextLink'] = self.link(self.tokens[index])
        return out


class Base(unittest.TestCase):
    def setUp(self):
        self.assignment = demo.assignment()
        self.graph, self.schema = graph_for(self.assignment)
        self.columns = list(self.schema['columns'].values())

    def capture(self, graph):
        return capture_schema(graph.site_id, graph.list_id, fetch=graph, clock=lambda: HOST_NOW)

    def rewriting(self, rewrite):
        """A source whose first page hands out a next link changed by ``rewrite``."""
        graph, _ = graph_for(self.assignment)
        def script(url, n, kind):
            if kind == 'columns' and 'skiptoken' not in url:
                page = graph.page(url); page['@odata.nextLink'] = rewrite(page['@odata.nextLink']); return 200, page, {}
        graph.script = script
        return graph

    def refused(self, rewrite, label=None, generic_guard_may_refuse_first=False):
        graph = self.rewriting(rewrite)
        with self.assertRaises(SchemaCaptureFailure, msg=label) as caught:
            self.capture(graph)
        e = caught.exception.evidence
        self.assertEqual((e['stage'], e['accepted']), ('columns', False), label)
        if not (generic_guard_may_refuse_first and e.get('reason') is None):     # the existing destination guard runs first and rejects some of these itself
            self.assertEqual(e.get('reason'), 'continuation_query_out_of_scope', label)
        self.assertEqual(len(graph.requests), 2, label)                          # identity + first page; the link was refused BEFORE the transport
        self.assertNotIn('snapshot', e)
        return caught.exception


class ContinuationCannotWidenTheRead(Base):
    def test_qa_repro_next_link_adds_expand(self):
        error = self.refused(lambda link: link.replace('?$top=', '?$expand=sourceContentType&$top='))
        self.assertIn('$expand', str(error))

    def test_projection_filter_and_other_query_options_cannot_be_added(self):
        for option in ('$expand=fields', '$select=id', '$select=name,id', '$filter=hidden eq false', '$filter=name%20eq%20%27x%27', '$orderby=name', '$count=true',
                       '$search=x', '$skip=5', '$format=json', '$apply=x', '$levels=2', 'expand=fields', 'select=id', 'api-version=beta', 'foo=1', 'x', 'top=1'):
            for place in (lambda link, o: link.replace('?$top=', '?' + o + '&$top='), lambda link, o: link + '&' + o,
                          lambda link, o: link.replace('&$skiptoken=', '&' + o + '&$skiptoken=')):
                self.refused(lambda link, o=option, p=place: p(link, o), option)

    def test_top_cannot_be_changed_duplicated_or_blanked(self):
        for rewrite in (lambda l: l.replace('$top=500', '$top=1'), lambda l: l.replace('$top=500', '$top=999'), lambda l: l.replace('$top=500', '$top=5000'),
                        lambda l: l.replace('$top=500', '$top='), lambda l: l.replace('$top=500', '$top'), lambda l: l.replace('$top=500', '$top=0500'),
                        lambda l: l.replace('$top=500', '$top=500.0'), lambda l: l.replace('$top=500', '$top=500&$top=500'), lambda l: l + '&$top=500',
                        lambda l: l.replace('$top=500', '$top=500%20'), lambda l: l.replace('$top=500', '$top=%35%30%30')):
            self.refused(rewrite)

    def test_skiptoken_must_appear_exactly_once_and_not_be_empty(self):
        for rewrite in (lambda l: l + '&$skiptoken=6', lambda l: l.replace('$skiptoken=5', '$skiptoken='), lambda l: l.replace('$skiptoken=5', '$skiptoken'),
                        lambda l: l.replace('&$skiptoken=5', ''), lambda l: l.replace('$skiptoken=5', '$skiptoken=5&$skiptoken=5'),
                        lambda l: l.replace('$skiptoken=5', '$skiptoken=' + 'x' * 5000)):
            self.refused(rewrite)

    def test_case_and_percent_encoding_tricks(self):
        for rewrite in (lambda l: l.replace('$top', '$Top'), lambda l: l.replace('$top', '$TOP'), lambda l: l.replace('$skiptoken', '$SkipToken'),
                        lambda l: l.replace('$skiptoken', '$SKIPTOKEN'), lambda l: l.replace('$top', '%24top'), lambda l: l.replace('$skiptoken', '%24skiptoken'),
                        lambda l: l + '&%24expand=fields', lambda l: l + '&$Expand=fields', lambda l: l + '&$EXPAND=fields', lambda l: l + '&%24Expand=fields',
                        lambda l: l + '&%2524expand=fields', lambda l: l + '&$e%78pand=fields', lambda l: l + '&$expand%3dfields', lambda l: l + '%26$expand=fields&x=1',
                        lambda l: l.replace('?$top', '?%24expand=fields&$top'), lambda l: l + '&$skip%74oken=9', lambda l: l.replace('$top=500', '$t%6fp=500')):
            self.refused(rewrite)

    def test_malformed_queries(self):
        for rewrite in (lambda l: l + '&', lambda l: l.replace('&', '&&'), lambda l: l.replace('?', '?&'), lambda l: l.replace('&', ';'), lambda l: l + ';$expand=fields',
                        lambda l: l + ' ', lambda l: l + '\t', lambda l: l.replace('$skiptoken=5', '$skiptoken=5 6'), lambda l: l.replace('$skiptoken=5', '$skiptoken=%zz'),
                        lambda l: l.replace('$skiptoken=5', '$skiptoken=%2'), lambda l: l.replace('$skiptoken=5', '$skiptoken=5%'), lambda l: l + '?$expand=fields',
                        lambda l: l.replace('$skiptoken=5', '$skiptoken=5é'), lambda l: l.split('?')[0], lambda l: l.split('?')[0] + '?'):
            self.refused(rewrite, generic_guard_may_refuse_first=True)

    def test_a_bad_link_on_a_later_page_or_the_second_pass_is_also_refused_before_the_transport(self):
        graph, _ = graph_for(self.assignment, page_size=3)
        def later(url, n, kind):
            if kind == 'columns' and url.endswith('$skiptoken=3'):
                page = graph.page(url); page['@odata.nextLink'] += '&$expand=fields'; return 200, page, {}
        graph.script = later
        with self.assertRaises(SchemaCaptureFailure) as caught:
            self.capture(graph)
        self.assertEqual(caught.exception.evidence['reason'], 'continuation_query_out_of_scope')
        self.assertTrue(all('$expand' not in u for u in graph.requests))
        graph, _ = graph_for(self.assignment)
        seen = []
        def second(url, n, kind):
            if kind == 'columns' and 'skiptoken' not in url:
                seen.append(url)
                if len(seen) == 2:
                    page = graph.page(url); page['@odata.nextLink'] += '&$select=id'; return 200, page, {}
        graph.script = second
        with self.assertRaises(SchemaCaptureFailure) as caught:
            self.capture(graph)
        self.assertEqual((caught.exception.evidence['reason'], caught.exception.evidence['capture_pass']), ('continuation_query_out_of_scope', 2))
        self.assertTrue(all('$select' not in u for u in graph.requests if '/columns' in u))

    def test_no_request_ever_carries_anything_but_the_reviewed_parameters(self):
        self.capture(self.graph)
        for url in self.graph.requests:
            query = url.split('?', 1)[1]
            if self.graph.kind(url) == 'columns':
                self.assertTrue(all(part.split('=')[0] in ('$top', '$skiptoken') for part in query.split('&')), url)
            else:
                self.assertEqual(query, '$select=' + cap.LIST_SELECT)


class LegitimateContinuationsStillWork(Base):
    def test_opaque_tokens_are_passed_through_unchanged_and_never_decoded(self):
        tokens = ['Paged%3dTRUE%26p_ID%3d200', 'X2k9LWFiYw==', 'abc-_.~123', 'a%2Bb%2Fc%3D%3D', '1!8!MTAw', "p'q(r)*s", 'a,b:c@d', '%24expand%3dfields']
        graph = TokenGraph(self.graph.site_id, self.graph.list_id, self.columns, page_size=2, tokens=tokens)
        artifact = self.capture(graph)
        self.assertEqual(artifact['snapshot']['columns'], {c['name']: c for c in self.columns})
        asked = [u.split('$skiptoken=')[1] for u in graph.requests if '$skiptoken=' in u]
        used = tokens[:-(-len(self.columns) // 2) - 1]
        self.assertEqual(asked, used * 2)                                        # byte for byte, both passes
        self.assertGreaterEqual(len(used), 5)

    def test_skiptoken_alone_or_before_top(self):
        for link in (lambda t: f'{G}{{base}}/columns?$skiptoken={t}', lambda t: f'{G}{{base}}/columns?$skiptoken={t}&$top=500'):
            graph = TokenGraph(self.graph.site_id, self.graph.list_id, self.columns, page_size=4, tokens=['t1', 't2', 't3', 't4'])
            graph.link = lambda t, link=link, graph=graph: link(t).format(base=graph.base)
            self.assertEqual(self.capture(graph)['observed']['column_count'], len(self.columns))

    def test_relative_next_links_are_held_to_the_same_rule(self):
        graph = TokenGraph(self.graph.site_id, self.graph.list_id, self.columns, page_size=4, tokens=['t1', 't2', 't3', 't4'])
        graph.link = lambda t: f'{graph.base}/columns?$top=500&$skiptoken={t}'
        self.assertEqual(self.capture(graph)['observed']['column_count'], len(self.columns))
        graph = TokenGraph(self.graph.site_id, self.graph.list_id, self.columns, page_size=4, tokens=['t1', 't2', 't3', 't4'])
        graph.link = lambda t: f'{graph.base}/columns?$expand=fields&$top=500&$skiptoken={t}'
        with self.assertRaises(SchemaCaptureFailure):
            self.capture(graph)
        self.assertEqual(len(graph.requests), 2)

    def test_single_page_lists_need_no_continuation(self):
        graph = FakeGraph(self.graph.site_id, self.graph.list_id, self.columns, page_size=500)
        self.assertEqual(len([u for u in graph.requests]), 0)
        self.capture(graph)
        self.assertEqual(len(graph.requests), 4)


class WhatTheArtifactClaims(Base):
    def test_it_claims_terminal_pages_and_two_matching_passes_not_atomic_completeness_or_provenance(self):
        o = self.capture(self.graph)['observed']
        self.assertNotIn('complete', o)
        self.assertEqual((o['each_pass_reached_a_terminal_page'], o['passes_agree'], o['atomic_snapshot'], o['provenance_established']), (True, True, False, False))
        self.assertIn('not an atomic snapshot', o['what_this_shows']); self.assertIn('says nothing about where the answers came from', o['what_this_shows'])


if __name__ == '__main__':
    unittest.main()
