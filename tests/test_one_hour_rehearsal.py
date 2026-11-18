"""ONE-HOUR REPORTING REHEARSAL. End to end, fully synthetic, on the real code paths.

schema candidate capture -> FAKE authority and approval -> hosted preflight -> exact mapped source polling ->
durable state and restarts -> real native Excel draft and checklist per client -> FAKE Lauren-only outbox.

Two invented clients (ALPHA-REHEARSAL / RH-7001, BRAVO-REHEARSAL / RH-7002). No Graph request, token, customer data,
external send or deployment. Every identity, approval, capture authority, probe, provider contract and outbox here is
a TEST-ONLY fake; passing these tests establishes nothing about a real source, a real scan field or a real provider.
"""
import inspect
import json
import socket
import unittest
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from unittest import mock

import rehearsal_harness as rh
from hosting import last_scan_hosted as hosted, last_scan_transport as transport
from rehearsal_harness import CLIENTS, DAY, LAUREN, Rehearsal, png, texts
from test_last_scan_durable import FAKE_CONTRACT, trust

ROOT = Path(__file__).resolve().parents[1]
HTTP_DATE = 'Tue, 10 Nov 2026 19:21:00 GMT'              # an absolute Retry-After, so the hold follows the rehearsal clock


class Base(unittest.TestCase):
    def setUp(self):
        for target in ('socket.socket.connect', 'socket.create_connection', 'urllib.request.urlopen'):   # nothing may reach a network
            patcher = mock.patch(target, side_effect=AssertionError('network use in a rehearsal: ' + target))
            patcher.start(); self.addCleanup(patcher.stop)
        self.r = Rehearsal(self)

    def at(self, text):
        self.r.clock.set('2026-11-10T%s:00+00:00' % text if len(text) == 5 else text)

    def poll(self, **kw):
        out = self.r.poll(**kw)
        self.assertEqual((self.r.last_response[0], self.r.last_response[1]['status']), (200, 'ran'), self.r.last_response[1].get('preflight'))
        return out

    def status(self, out):
        return {k: v['status'] for k, v in out.items()}

    def item_reads(self, client):
        return sum(1 for u in self.r.tenants[client].requests if '/items?' in u)


class Pipeline(Base):
    def test_from_schema_candidate_to_first_drafts(self):
        r = self.r
        for name, artifact in r.schema_artifacts.items():                       # 1. candidate evidence only
            self.assertEqual((artifact['status'], artifact['marked_verified'], artifact['grants_authority'], artifact['samples']['status']),
                             ('candidate_unreviewed', False, False, 'not_performed'))
            self.assertEqual(artifact['snapshot']['site_id'], CLIENTS[name]['site_id'])
        dry = r.dry_run                                                         # 2-3. authority recorded, mapping ready, only approval missing
        self.assertEqual([m['ready'] for m in dry['mapping_readiness']], [True, True])
        self.assertEqual({m['capture']['collector'] for m in dry['mapping_readiness']}, {rh.COLLECTOR})
        self.assertEqual((dry['proof']['runtime_source_proof'], dry['proof']['scan_time_semantics'], dry['transport']), ('not_established', 'not_established_by_this_tool', 'disabled'))
        out = self.poll()                                                       # 4. first hosted run at 19:00
        self.assertEqual(self.status(out), {('alpha', '1'): 'draft_ready', ('alpha', '2'): 'waiting', ('alpha', '3'): 'no_scans',
                                            ('bravo', '1'): 'waiting', ('bravo', '2'): 'waiting'})
        self.assertEqual((out[('alpha', '2')]['due_at'], out[('alpha', '2')]['timing_basis']), ('2026-11-10T22:00:00+00:00', 'verified_schedule'))
        self.assertEqual((out[('bravo', '1')]['due_at'], out[('bravo', '1')]['timing_basis']), ('2026-11-10T19:20:00+00:00', 'provisional_quiet_window_schedule_unknown'))
        body = r.last_response[1]
        self.assertEqual((body['live_schema_drift_check'], body['summary']['notifications_sent'], body['summary']['client_release']), ('performed_before_each_read', 0, 'held'))
        self.assertEqual(r.lauren.calls, 0)

    def test_the_whole_day_ends_with_one_draft_per_shift_that_scanned(self):
        seen = {}
        for hhmm in ('19:00', '19:20', '19:40', '20:00', '21:00', '22:00', '22:30'):
            self.at(hhmm); out = self.poll()
            for k, v in out.items():
                if v['status'] == 'draft_ready':
                    seen.setdefault(k, hhmm)
        self.assertEqual(seen, {('alpha', '1'): '19:00', ('bravo', '1'): '19:20', ('bravo', '2'): '19:40', ('alpha', '2'): '22:00'})
        self.assertEqual(self.status(out)[('alpha', '3')], 'no_scans')
        self.assertEqual(self.r.lauren.messages, [])                           # building drafts sends nothing


class NoEarlyRelease(Base):
    def test_a_waiting_scope_has_no_draft_no_outbox_entry_and_cannot_be_dispatched(self):
        trust(self, FAKE_CONTRACT)
        out = self.poll()
        for key in (('alpha', '2'), ('bravo', '1'), ('bravo', '2')):
            self.assertEqual(self.r.outbox(*key), {})
            op = out[key]['operation_id']
            self.assertIsNone(self.r.router().for_scope(self.r.tenants[key[0]].assignment).lookup(op))
            self.assertEqual(self.r.dispatch(*key, op=op), dict(status='not_pending'))
        self.assertEqual(self.r.lauren.calls, 0)

    def test_scanning_resumes_before_the_hour_and_the_hour_starts_again(self):
        trust(self, FAKE_CONTRACT)
        first = self.poll()[('bravo', '1')]
        self.assertEqual(first['due_at'], '2026-11-10T19:20:00+00:00')
        self.at('19:10'); self.r.tenants['bravo'].scan('1', '2026-11-10T19:08:00+00:00')
        out = self.poll()[('bravo', '1')]
        self.assertEqual((out['status'], out['due_at']), ('waiting', '2026-11-10T20:10:00+00:00'))
        self.assertNotEqual(out['operation_id'], first['operation_id'])
        for hhmm in ('19:20', '19:25', '20:09'):                               # past the ORIGINAL due time: still nothing
            self.at(hhmm); out = self.poll()[('bravo', '1')]
            self.assertEqual(out['status'], 'waiting', hhmm)
            self.assertEqual((self.r.outbox('bravo', '1'), self.r.dispatch('bravo', '1', op=out['operation_id'])), ({}, dict(status='not_pending')))
        self.at('20:10'); out = self.poll()[('bravo', '1')]
        self.assertEqual((out['status'], out['artifact_state']), ('draft_ready', 'built_this_run'))
        d = self.r.draft('bravo', '1')
        self.assertEqual((len(d['checklist']['source_ids']), d['book']['Summary']['F24'].value), (13, 13))
        self.assertEqual(self.r.lauren.calls, 0)

    def test_lunch_pause_does_not_release_before_the_configured_shift_end(self):
        trust(self, FAKE_CONTRACT)
        for hhmm in ('19:00', '19:56', '20:30', '21:59'):                      # quiet since 18:55, well over an hour by 20:30
            self.at(hhmm); out = self.poll()[('alpha', '2')]
            self.assertEqual((out['status'], out['due_at'], out['timing_basis']), ('waiting', '2026-11-10T22:00:00+00:00', 'verified_schedule'), hhmm)
            self.assertEqual((self.r.outbox('alpha', '2'), self.r.dispatch('alpha', '2', op=out['operation_id'])), ({}, dict(status='not_pending')))
        self.at('22:00'); out = self.poll()[('alpha', '2')]
        self.assertEqual(out['status'], 'draft_ready')
        self.assertEqual(self.r.lauren.calls, 0)

    def test_lunch_pause_then_scanning_resumes_and_runs_past_the_shift_end(self):
        self.at('20:30'); self.poll()
        self.at('21:40'); self.r.tenants['alpha'].scan('2', '2026-11-10T21:38:00+00:00', count=3)
        out = self.poll()[('alpha', '2')]
        self.assertEqual((out['status'], out['due_at']), ('waiting', '2026-11-10T22:40:00+00:00'))
        self.at('22:00'); self.assertEqual(self.poll()[('alpha', '2')]['status'], 'waiting')   # shift end alone is not enough either
        self.at('22:39'); self.assertEqual(self.poll()[('alpha', '2')]['status'], 'waiting')
        self.at('22:40'); out = self.poll()[('alpha', '2')]
        self.assertEqual(out['status'], 'draft_ready')
        self.assertEqual(len(self.r.draft('alpha', '2')['checklist']['source_ids']), 15)


class LateAndOutOfOrder(Base):
    def test_a_late_offline_row_supersedes_the_ready_draft_and_restarts_the_hour(self):
        trust(self, FAKE_CONTRACT)
        self.at('19:40'); first = self.poll()[('bravo', '2')]
        self.assertEqual(first['status'], 'draft_ready')
        self.assertEqual(self.r.outbox('bravo', '2')[first['operation_id']]['status'], 'pending')
        self.at('20:30'); self.r.tenants['bravo'].scan('2', '2026-11-10T18:10:00+00:00')          # a tablet that was offline syncs an OLD scan
        out = self.poll()[('bravo', '2')]
        self.assertEqual((out['status'], out['due_at']), ('waiting', '2026-11-10T21:30:00+00:00'))   # counted from when it ARRIVED
        self.assertNotEqual(out['operation_id'], first['operation_id'])
        self.assertEqual(self.r.outbox('bravo', '2')[first['operation_id']]['status'], 'superseded')
        self.assertEqual(self.r.dispatch('bravo', '2', op=first['operation_id']), dict(status='not_pending'))
        self.assertEqual(self.r.lauren.calls, 0)
        self.at('21:30'); out = self.poll()[('bravo', '2')]
        self.assertEqual(out['status'], 'draft_ready')
        new, old = self.r.draft('bravo', '2'), self.r.draft('bravo', '2', op=first['operation_id'])
        self.assertEqual((len(new['checklist']['source_ids']), len(old['checklist']['source_ids'])), (13, 12))
        self.assertEqual(old['export']['provenance']['label'], 'HISTORICAL')    # the superseded draft is kept, labelled, never updated

    def test_a_ready_draft_claimed_just_as_a_late_row_arrives_is_not_sent(self):
        trust(self, FAKE_CONTRACT)
        self.at('19:40'); first = self.poll()[('bravo', '2')]
        self.at('19:45'); self.r.tenants['bravo'].scan('2', '2026-11-10T19:44:00+00:00')          # arrives between the poll and the dispatch
        out = self.r.dispatch('bravo', '2')
        self.assertEqual((out['status'], out['reason'], out['current_status']), ('superseded', 'source_changed_or_not_due', 'waiting'))
        self.assertEqual(self.r.lauren.calls, 0)

    def test_an_out_of_order_capture_is_refused_and_changes_nothing(self):
        self.at('19:02'); before = self.poll()
        self.at('19:04'); self.r.tenants['bravo'].stale_by = timedelta(minutes=3)                 # a capture stamped 19:01, older than the stored 19:02
        out = self.poll()
        self.assertEqual({k: (v['status'], v.get('error_type')) for k, v in out.items() if k[0] == 'bravo'},
                         {('bravo', '1'): ('error', 'ValueError'), ('bravo', '2'): ('error', 'ValueError')})
        self.assertEqual(self.status(out)[('alpha', '1')], 'draft_ready')       # the other client is unaffected
        self.r.tenants['bravo'].stale_by = None
        self.at('19:06'); after = self.poll()
        for key in (('bravo', '1'), ('bravo', '2')):
            self.assertEqual((after[key]['status'], after[key]['operation_id'], after[key]['due_at']),
                             (before[key]['status'], before[key]['operation_id'], before[key]['due_at']))

    def test_a_capture_too_old_to_trust_is_refused(self):
        self.r.tenants['alpha'].stale_by = timedelta(minutes=6)
        out = self.poll()
        self.assertEqual({v['status'] for k, v in out.items() if k[0] == 'alpha'}, {'error'})
        self.assertEqual(self.r.outbox('alpha', '1'), {})


class SourceOutage(Base):
    def test_outage_holds_the_site_reads_nothing_more_and_recovers(self):
        trust(self, FAKE_CONTRACT)
        ready = self.poll()[('alpha', '1')]
        self.at('19:05'); self.r.tenants['alpha'].down = (503, None, {'Retry-After': HTTP_DATE})
        out = self.poll()
        self.assertEqual(sorted(v['status'] for k, v in out.items() if k[0] == 'alpha'), ['cooldown', 'cooldown', 'error'])
        self.assertEqual({v['status'] for k, v in out.items() if k[0] == 'bravo'}, {'waiting'})      # the other client carries on
        failed = next(v for k, v in out.items() if k[0] == 'alpha' and v['status'] == 'error')
        self.assertEqual((failed['error_type'], failed['cooldown_until']), ('CaptureFailure', '2026-11-10T19:21:00+00:00'))
        reads = self.item_reads('alpha')
        self.at('19:10'); out = self.poll()                                     # still inside the wait the source asked for
        self.assertEqual({v['status'] for k, v in out.items() if k[0] == 'alpha'}, {'cooldown'})
        self.assertEqual(self.item_reads('alpha'), reads)                       # not one more item request
        held = self.r.dispatch('alpha', '1', op=ready['operation_id'])          # a ready draft is not sent on stale knowledge,
        self.assertEqual((held['status'], held['reason']), ('held', 'site_cooldown'))   # and the held site is not even asked
        self.assertEqual(self.item_reads('alpha'), reads)
        self.assertEqual(self.r.lauren.calls, 0)
        self.r.tenants['alpha'].down = None
        self.at('19:21'); out = self.poll()
        self.assertEqual(self.status(out)[('alpha', '1')], 'draft_ready')
        self.assertEqual(out[('alpha', '1')]['operation_id'], ready['operation_id'])               # same content, same draft, no rebuild
        self.assertEqual(out[('alpha', '1')]['artifact_state'], 'historical_reused')
        self.assertEqual(self.r.dispatch('alpha', '1')['status'], 'sent')
        self.assertEqual(len(self.r.lauren.messages), 1)

    def test_permission_loss_is_an_error_not_an_empty_shift(self):
        self.poll()
        self.at('19:05'); self.r.tenants['bravo'].down = (403, None, {})
        out = self.poll()
        self.assertEqual({(v['status'], v.get('error_type')) for k, v in out.items() if k[0] == 'bravo'}, {('error', 'CaptureFailure')})
        self.assertNotIn('no_scans', {v['status'] for k, v in out.items() if k[0] == 'bravo'})


class DuplicatePollsAndRestarts(Base):
    def test_the_same_firing_replayed_and_a_restart_in_between_build_nothing_twice(self):
        out = self.poll(run_id='firing-1')
        op, reads = out[('alpha', '1')]['operation_id'], self.item_reads('alpha')
        receipt = self.r.draft('alpha', '1')['receipt']
        code, body = self.r.call(run_id='firing-1')                             # the scheduler fires the same run again
        self.assertEqual((code, body['summary'].get('replayed'), self.item_reads('alpha')), (200, True, reads))
        self.r.restart()
        code, body = self.r.call(run_id='firing-1')
        self.assertEqual((body['summary'].get('replayed'), self.item_reads('alpha')), (True, reads))
        self.at('19:05'); again = self.poll()[('alpha', '1')]                   # a NEW firing after the restart
        self.assertEqual((again['status'], again['operation_id'], again['artifact_state'], again['evidence_as_of']),
                         ('draft_ready', op, 'historical_reused', '2026-11-10T19:00:00+00:00'))
        self.assertEqual(self.r.draft('alpha', '1')['receipt'], receipt)        # the stored draft is byte-identical
        self.assertEqual(list(self.r.outbox('alpha', '1')), [op])               # one outbox entry, not two

    def test_overlapping_firings_produce_one_draft_and_one_outbox_entry(self):
        with ThreadPoolExecutor(4) as pool:
            results = list(pool.map(lambda i: self.r.call(run_id='overlap-%d' % i), range(4)))
        self.assertEqual({code for code, _ in results}, {200})
        ops = {row['operation_id'] for _, body in results for row in body['summary']['results'] if row['job'] == 'RH-7001' and row['shift'] == '1' and row['status'] == 'draft_ready'}
        self.assertEqual(len(ops), 1)
        self.assertEqual(list(self.r.outbox('alpha', '1')), list(ops))
        statuses = {row['status'] for _, body in results for row in body['summary']['results']}
        self.assertTrue(statuses <= {'draft_ready', 'waiting', 'no_scans', 'busy'}, statuses)

    def test_state_survives_a_restart_mid_wait(self):
        first = self.poll()[('bravo', '1')]
        self.r.restart(); self.at('19:19')
        out = self.poll()[('bravo', '1')]
        self.assertEqual((out['status'], out['operation_id'], out['due_at']), ('waiting', first['operation_id'], first['due_at']))   # the hour did not restart
        self.r.restart(); self.at('19:20')
        self.assertEqual(self.poll()[('bravo', '1')]['status'], 'draft_ready')


class Delivery(Base):
    def test_without_a_verified_provider_contract_nothing_can_be_sent(self):
        self.poll()
        self.assertEqual(transport.VERIFIED_CONTRACTS, {})                      # the real registry is empty
        for contract in (None, FAKE_CONTRACT):                                  # the fake contract is not in the real registry either
            self.assertEqual(self.r.dispatch('alpha', '1', contract=contract), dict(status='transport_disabled'))
        self.assertEqual((self.r.lauren.calls, self.r.outbox('alpha', '1')[self.r.last[('alpha', '1')]['operation_id']]['status']), (0, 'pending'))

    def test_one_message_to_the_fake_lauren_inbox_and_no_duplicate_under_the_fake_contract(self):
        trust(self, FAKE_CONTRACT)
        op = self.poll()[('alpha', '1')]['operation_id']
        self.assertEqual(self.r.dispatch('alpha', '1'), dict(status='sent'))
        self.assertEqual(self.r.dispatch('alpha', '1'), dict(status='not_pending'))               # asked again
        self.r.restart(); self.at('19:30')
        self.assertEqual(self.r.dispatch('alpha', '1'), dict(status='not_pending'))               # after a restart
        self.poll()
        self.assertEqual(self.r.dispatch('alpha', '1'), dict(status='not_pending'))               # after another poll of unchanged content
        (message,) = self.r.lauren.messages
        self.assertEqual((message['to'], message['operation_id'], message['payload']['audience']), (LAUREN, op, 'Lauren'))
        self.assertEqual(message['payload']['receipt']['binding']['client_id'], 'ALPHA-REHEARSAL')
        self.assertEqual(message['payload']['receipt']['client_release'], 'held')
        entry = self.r.outbox('alpha', '1')[op]
        self.assertEqual((entry['status'], entry['delivery_status'], len(entry['attempts'])), ('sent', 'sent', 1))

    def test_concurrent_dispatchers_send_once(self):
        trust(self, FAKE_CONTRACT)
        self.at('19:40'); self.poll()
        with ThreadPoolExecutor(6) as pool:
            out = list(pool.map(lambda i: self.r.dispatch('bravo', '2')['status'], range(6)))
        self.assertEqual(out.count('sent'), 1, out)
        self.assertTrue(set(out) <= {'sent', 'not_pending', 'busy'}, out)
        self.assertEqual(len(self.r.lauren.messages), 1)

    def test_an_uncertain_delivery_is_never_retried_blindly(self):
        trust(self, FAKE_CONTRACT)
        op = self.poll()[('alpha', '1')]['operation_id']
        self.r.lauren.fail = TimeoutError('no answer from the fake provider')
        self.assertEqual(self.r.dispatch('alpha', '1'), dict(status='uncertain', delivery_status='uncertain'))
        self.r.lauren.fail = None                                               # the provider is healthy again
        for hhmm in ('19:01', '19:30', '21:00', '23:00'):
            self.at(hhmm); self.r.restart(); self.poll()
            self.assertEqual(self.r.dispatch('alpha', '1'), dict(status='not_pending'), hhmm)
        self.assertEqual((self.r.lauren.calls, self.r.lauren.messages), (1, []))                 # called once, ever
        entry = self.r.outbox('alpha', '1')[op]
        self.assertEqual((entry['status'], entry['delivery_status'], len(entry['attempts'])), ('uncertain', 'uncertain', 1))

    def test_a_wrong_acknowledgment_is_not_taken_as_delivery(self):
        trust(self, FAKE_CONTRACT)
        self.poll()
        out = self.r.dispatch('alpha', '1', sender=lambda **kw: dict(operation_id='something-else'))
        self.assertEqual(out, dict(status='uncertain', delivery_status='uncertain'))

    def test_a_drifted_schema_stops_the_hosted_dispatch_and_the_old_bypass_is_shut(self):
        """Was the pinned KNOWN GAP (dispatch ignored a drifted schema). Turned around: the authorized dispatch path
        refuses before any item is read, and the production door refuses a caller that skips that path."""
        trust(self, FAKE_CONTRACT)
        self.poll()
        self.r.approve_dispatch()
        scan = next(c for c in self.r.tenants['alpha'].columns_now if c['name'] == 'ActualScan')
        scan['calculated'] = {'formula': '=[Created]', 'outputType': 'dateTime'}                  # the live schema drifts after the draft was built
        self.at('19:05')
        self.r.poll()
        self.assertEqual(self.r.last_response[1]['status'], 'schema_drift')     # the hosted poll notices...
        reads = self.item_reads('alpha')
        code, body = self.r.hosted_dispatch('alpha', '1')                       # ...and so does the hosted dispatch
        self.assertEqual((code, body['status'], body['refusal'], body['sources_read'], body['sender_calls']),
                         (409, 'held', 'schema:live_schema_differs_from_the_approved_snapshot', 0, 0))
        self.assertEqual(self.r.through_the_door_without_a_grant('alpha', '1'), dict(status='dispatch_not_authorized'))
        self.assertEqual((self.r.lauren.calls, self.item_reads('alpha')), (0, reads))
        # The direct test seam authorizes nothing and still ignores the drift. It is test-only, not a production path.
        self.assertEqual(self.r.dispatch('alpha', '1'), dict(status='sent'))
        self.assertEqual(len(self.r.lauren.messages), 1)

    def test_no_exactly_once_claim_is_made_anywhere(self):
        for path in (ROOT / 'hosting' / 'last_scan_transport.py', ROOT / 'hosting' / 'last_scan_service.py', ROOT / 'docs' / 'LAST_SCAN_REHEARSAL.md'):
            text = path.read_text().lower()
            for line in text.splitlines():
                if ('exactly once' in line or 'exactly-once' in line) and any(w in line for w in ('deliver', 'send', 'sent')):   # about delivery, not about releasing a hold
                    self.assertTrue(any(w in line for w in ('not ', 'no ', 'never', 'cannot', 'nothing')), line)


class SchemaDriftBeforeRead(Base):
    def test_a_changed_column_stops_the_run_before_any_item_is_read(self):
        self.poll()
        reads = {c: self.item_reads(c) for c in ('alpha', 'bravo')}
        scan = next(c for c in self.r.tenants['alpha'].columns_now if c['name'] == 'ActualScan')
        scan['calculated'] = {'formula': '=[Created]', 'outputType': 'dateTime'}                  # the live list changes after approval
        self.at('19:10'); out = self.r.poll()
        code, body = self.r.last_response
        self.assertEqual((out, code, body['status'], body['live_schema_drift_check']), ({}, 409, 'schema_drift', 'failed_before_any_read'))
        self.assertEqual([d['problem'] for d in body['drift']], ['live_schema_differs_from_the_approved_snapshot'])
        self.assertEqual({c: self.item_reads(c) for c in ('alpha', 'bravo')}, reads)              # neither client was read
        del scan['calculated']
        self.at('19:20'); self.assertEqual(self.status(self.poll())[('bravo', '1')], 'draft_ready')   # back to the approved schema

    def test_a_schema_that_was_never_approved_cannot_be_polled(self):
        self.r.tenants['bravo'].columns_now.append(dict(name='Extra', displayName='Extra', id='id-extra', text={}))
        self.r.poll()
        self.assertEqual(self.r.last_response[1]['status'], 'schema_drift')
        self.assertEqual((self.item_reads('alpha'), self.item_reads('bravo')), (0, 0))


class WhatTheDraftSays(Base):
    def setUp(self):
        super().setUp()
        self.at('19:40'); self.poll()
        self.alpha, self.bravo = self.r.draft('alpha', '1'), self.r.draft('bravo', '2')

    def test_real_native_workbooks_with_each_clients_own_header_and_rows(self):
        for d, mine, theirs, tag, other in ((self.alpha, CLIENTS['alpha'], CLIENTS['bravo'], 'ALPHA', 'BRAVO'), (self.bravo, CLIENTS['bravo'], CLIENTS['alpha'], 'BRAVO', 'ALPHA')):
            book = d['book']
            self.assertEqual(book.sheetnames[:5], list(rh.VISIBLE))
            visible, everything = texts(book), texts(book, book.sheetnames)
            for value in (mine['header']['paying_client'], mine['header']['worksite'], mine['job']):
                self.assertIn(value, visible)
            self.assertIn(mine['line'], visible); self.assertIn(tag + '0', everything)
            for value in (theirs['header']['paying_client'], theirs['header']['worksite'], theirs['job'], theirs['line'], theirs['client_id'], other + '0', theirs['part']):
                self.assertNotIn(value, everything, value)                      # no cross-client header or data anywhere in the file
            self.assertEqual(d['checklist']['binding']['client_id'], mine['client_id'])
            self.assertEqual(d['receipt']['binding'], dict(client_id=mine['client_id'], job=mine['job'], site_id=mine['site_id'], list_id=mine['list_id']))
            self.assertNotIn(theirs['client_id'], json.dumps(d['checklist'])); self.assertNotIn(other + '0', d['note'])
            self.assertEqual(d['book']['Summary']['F24'].value, 12)

    def test_a_publisher_for_one_client_cannot_serve_the_others_draft(self):
        alpha_publisher = self.r.router().for_scope(self.r.tenants['alpha'].assignment)
        self.assertIsNone(alpha_publisher.lookup(self.bravo['op']))
        self.assertIsNotNone(alpha_publisher.lookup(self.alpha['op']))

    def test_missing_nok_stays_unknown_and_known_nok_is_shown(self):
        a, b = self.alpha, self.bravo
        self.assertIsNone(a['book']['Summary']['K24'].value)                    # ALPHA never recorded NOK: blank, not 0
        missing = [f for f in a['checklist']['findings'] if f['code'] == 'missing_value']
        self.assertEqual((len(missing), {f['field'] for f in missing}), (12, {'rejected'}))
        self.assertEqual(sorted(f['record_id'] for f in missing), sorted(a['checklist']['source_ids']))
        self.assertIn('rejected quantity is unknown', texts(a['book']).lower())
        self.assertEqual(b['book']['Summary']['K24'].value, 24)                 # BRAVO recorded 2 per row: 12 x 2
        self.assertEqual([f for f in b['checklist']['findings'] if f['code'] == 'missing_value'], [])

    def test_draft_first_every_unresolved_issue_is_exposed_and_nothing_is_released(self):
        for d in (self.alpha, self.bravo):
            c = d['checklist']
            self.assertEqual((c['status'], c['client_release'], c['day_completion']), ('DRAFT', 'held', 'not_established'))
            self.assertFalse(c['verification']['current_sharepoint_claim'])
            self.assertIn('not approved for client release', d['note'])
            for f in c['findings']:
                self.assertIn('Record %s: ' % f['record_id'], d['note'])
            self.assertIn('not proof of final scan or complete shift', c['timing_note'])
        self.assertEqual(sorted({f['code'] for f in self.alpha['checklist']['findings']}), ['missing_value', 'photo_not_captured', 'unsupported_value_type'])
        self.assertEqual({p['http_status'] for p in self.alpha['checklist']['photo_evidence']}, {403})
        self.assertEqual(self.bravo['checklist']['timing_note'] != '', True)


class LatePhoto(Base):
    def test_a_photo_that_arrives_later_rebuilds_the_draft_and_the_old_one_stays_labelled_historical(self):
        trust(self, FAKE_CONTRACT)
        first = self.poll()[('alpha', '1')]
        old = self.r.draft('alpha', '1')
        self.assertEqual({(p['status'], p['http_status']) for p in old['checklist']['photo_evidence']}, {('unavailable', 403)})
        self.assertEqual(sorted(f['record_id'] for f in old['checklist']['findings'] if f['code'] == 'photo_not_captured'), ['3', '8'])
        self.at('19:30'); alpha = self.r.tenants['alpha']; alpha.photo_status, alpha.photo_bytes = 200, png()       # permission fixed, photos now download
        out = self.poll()[('alpha', '1')]
        self.assertEqual((out['status'], out['due_at']), ('waiting', '2026-11-10T20:30:00+00:00'))   # new evidence: a new hour
        self.assertNotEqual(out['operation_id'], first['operation_id'])
        self.assertEqual(self.r.outbox('alpha', '1')[first['operation_id']]['status'], 'superseded')
        self.assertEqual(self.r.dispatch('alpha', '1', op=first['operation_id']), dict(status='not_pending'))
        self.at('20:30'); rebuilt = self.poll()[('alpha', '1')]
        self.assertEqual((rebuilt['status'], rebuilt['artifact_state'], rebuilt['evidence_as_of']), ('draft_ready', 'built_this_run', '2026-11-10T20:30:00+00:00'))
        new = self.r.draft('alpha', '1')
        self.assertEqual({(p['status'], p['http_status']) for p in new['checklist']['photo_evidence']}, {('supported', 200)})
        self.assertEqual([f for f in new['checklist']['findings'] if f['code'] == 'photo_not_captured'], [])
        self.assertNotEqual(new['receipt']['workbook_sha256'], old['receipt']['workbook_sha256'])
        self.at('20:40'); later = self.poll()[('alpha', '1')]                   # nothing changed: the stored draft is reused and says so
        self.assertEqual((later['artifact_state'], later['evidence_as_of'], later['observed_at']), ('historical_reused', '2026-11-10T20:30:00+00:00', '2026-11-10T20:40:00+00:00'))
        kept = self.r.draft('alpha', '1', op=first['operation_id'])             # the first draft was never rewritten
        self.assertEqual((kept['receipt'], kept['checklist']['evidence_as_of']), (old['receipt'], '2026-11-10T19:00:00+00:00'))
        for d in (kept, new):
            self.assertEqual((d['export']['provenance']['label'], d['export']['provenance']['freshly_verified'], d['export']['provenance']['regenerated']), ('HISTORICAL', False, False))
            self.assertIn('HISTORICAL RECORD', d['note']); self.assertIn('never updated', d['checklist']['historical_notice'])
        self.assertEqual(self.r.dispatch('alpha', '1')['status'], 'sent')       # only the current draft can go out
        self.assertEqual([m['operation_id'] for m in self.r.lauren.messages], [rebuilt['operation_id']])


class TestOnlyBoundaries(Base):
    def test_every_fake_lives_in_the_test_suite_and_says_so(self):
        for cls in (rh.RecordedCaptures, rh.AdapterProbe, rh.FakeLaurenOutbox, rh.Tenant, rh.Rehearsal):
            self.assertTrue(inspect.getsourcefile(cls).startswith(str(ROOT / 'tests')), cls)
        for cls in (rh.RecordedCaptures, rh.AdapterProbe, rh.FakeLaurenOutbox):
            self.assertIn('TEST ONLY', cls.__doc__)
        self.assertTrue(LAUREN.endswith('.invalid')); self.assertEqual(rh.HOST, 'example.sharepoint.com')
        for spec in CLIENTS.values():
            self.assertIn('fictitious', spec['header']['paying_client']); self.assertTrue(spec['site_id'].startswith('example.sharepoint.com,'))
        text = Path(rh.__file__).read_text()                                     # the harness holds every invented name used here
        for real in ('SLAlabama', 'Hyundai', 'Pratt', 'ppgquality', '2207', '2221', '2169'):
            self.assertNotIn(real, text)

    def test_application_code_cannot_reach_the_rehearsal_or_its_fakes(self):
        for path in list((ROOT / 'api').rglob('*.py')) + list((ROOT / 'hosting').glob('*.py')):
            text = path.read_text(errors='ignore')
            for name in ('rehearsal', 'FakeLaurenOutbox', 'RecordedCaptures', 'AdapterProbe', 'FAKE_CONTRACT', 'FakeApprovals', 'FakeAuthorizer'):
                self.assertNotIn(name, text, (str(path), name))

    def test_production_defaults_are_still_deny(self):
        self.assertEqual(transport.VERIFIED_CONTRACTS, {})
        d = hosted.Deps()
        self.assertEqual([getattr(d, f) for f in ('authorizer', 'approvals', 'quota', 'config', 'entries', 'schemas', 'captures', 'collectors', 'schema_probe', 'store', 'artifacts', 'fetch', 'provider_contract')], [None] * 13)
        self.assertEqual(hosted.handle('POST', rh.ALICE, dict(run_id='x', timestamp=self.r.clock(), nonce='n' * 20), d), (503, dict(status='not_configured')))

    def test_the_rehearsal_refuses_without_each_fake(self):
        for missing in ('approvals', 'captures', 'collectors', 'schema_probe', 'schemas', 'authorizer'):
            code, body = self.r.call(deps=self.r.deps(**{missing: None}))
            self.assertIn(code, (409, 503), missing); self.assertNotEqual(body.get('status'), 'ran')
        self.assertEqual((self.item_reads('alpha'), self.item_reads('bravo')), (0, 0))

    def test_no_network_was_touched(self):
        for hhmm in ('19:00', '19:40', '22:00'):
            self.at(hhmm); self.poll()
        self.assertEqual((socket.socket.connect.call_count, socket.create_connection.call_count, urllib.request.urlopen.call_count), (0, 0, 0))
        for tenant in self.r.tenants.values():
            self.assertTrue(all(isinstance(u, str) for u in tenant.requests))


if __name__ == '__main__':
    unittest.main()
