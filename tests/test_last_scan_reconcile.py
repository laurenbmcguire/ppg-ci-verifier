"""Hosted reconciliation of an uncertain delivery (hosting.last_scan_reconcile). FULLY SYNTHETIC. TEST ONLY.

Every identity, approval, provider contract, status adapter and answer here is a fake. No provider or status adapter
exists, nothing is sent anywhere, and a pass here is not evidence about any real provider's behaviour.
"""
import itertools
import unittest
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import authority_fixture as af
import rehearsal_harness as rh
from hosting import last_scan_dispatch as dispatch, last_scan_hosted as hosted, last_scan_reconcile as reconcile, last_scan_transport as transport
from hosting.last_scan_hosted import Quota
from test_last_scan_authority_lifecycle import COMMON, APPROVAL_ONLY, ahead, changed
from test_last_scan_durable import FAKE_CONTRACT, trust
from test_last_scan_hosted import BOB, FakeApprovals
from test_last_scan_hosted_dispatch import Crash, Dispatch

ROOT = Path(__file__).resolve().parents[1]
APPROVED = 'reconcile_approved_for_this_attempt_recipient_contract_and_principal'
ZONE = rh.ZONE


class Reconcile(Dispatch):
    """One ALPHA shift-1 draft whose single delivery attempt ended uncertain (the fake provider did not answer)."""
    def setUp(self):
        super().setUp()
        self.n = itertools.count(1)
        self.asked = []
        self.r.lauren.fail = TimeoutError('no answer from the fake provider')
        self.assertEqual(self.go()[1]['status'], 'uncertain')
        self.r.lauren.fail = None
        self.attempt = self.entry()['attempt_id']
        self.digest = self.approve()

    # ---- a FAKE status adapter and the wiring
    def status(self, state='delivered', make=None, before=None, **change):
        def lookup(**kw):
            self.asked.append(kw)
            if before:
                before()
            out = rh.provider_outcome(FAKE_CONTRACT, idempotency_key=kw['idempotency_key'], attempt_id=kw['attempt_id'], audience=kw['audience'],
                                      payload=dict(receipt=self.entry()['receipt']), deadline_seconds=kw['deadline_seconds'], observed_at=self.r.clock(),
                                      state=state, record_id='fake-status-record-1')
            out.update(change)
            return make(out) if make else out
        return lookup

    def deps(self, **kw):
        base = dict(authorizer=self.r.dispatch_deps().authorizer, approvals=self.r.approvals, store=self.r.store, clock=self.r.clock, issuers=af.ISSUERS,
                    provider_contract=FAKE_CONTRACT, recipients={t.spec['client_id']: 'Lauren' for t in self.r.tenants.values()},
                    status_lookup=self.status(), reconcile_quota=Quota(10000))
        base.update(kw)
        return reconcile.Deps(**base)

    def request(self, **change):
        s, n = self.r.item('alpha', '1')['scope'], next(self.n)
        receipt = self.entry()['receipt']
        body = dict({k: s[k] for k in reconcile.SCOPE}, reconcile_id='rehearsal-reconcile-%04d' % n, mode='reconcile', timestamp=self.r.clock(),
                    nonce='rehearsal-reconcile-nonce-%06d' % n, recipient='Lauren', operation_id=self.op, attempt_id=self.attempt,
                    workbook_sha256=receipt['workbook_sha256'], checklist_sha256=receipt['checklist_sha256'])
        body.update(change)
        return body

    def rec(self, headers=rh.ALICE, deps=None, **change):
        return reconcile.handle('POST', headers, self.request(**change), deps or self.deps())

    def approve(self, principals=('alice',), deps=None, **change):
        """A FAKE approver approves exactly the reconcile digest a dry run reports (TEST ONLY)."""
        code, dry = self.rec(deps=deps, mode='dry_run', **{k: v for k, v in change.items() if k in reconcile.FIELDS})
        assert code == 200 and dry['reconcile_digest'], dry
        self.r.approvals.records[dry['reconcile_digest']] = af.approval(dry['reconcile_digest'], principals, action=reconcile.ACTION,
                                                                       **{k: v for k, v in change.items() if k not in reconcile.FIELDS})
        return dry['reconcile_digest']

    def failed(self, body):
        return [c['check'] for c in body['checks'] if not c['ok']]

    def unchanged(self, asked=0):
        """Still uncertain on the same single attempt, the fake sender called once ever, the status adapter ``asked`` times."""
        entry = self.entry()
        self.assertEqual((entry['status'], entry['delivery_status'], entry['attempt_id'], len(entry['attempts']), self.r.lauren.calls, len(self.asked)),
                         ('uncertain', 'uncertain', self.attempt, 1, 1, asked))
        return entry

    def never_resent(self):
        """Polls, restarts and fully authorized dispatches afterwards: the entry never becomes deliverable and the sender is never called again."""
        calls = self.r.lauren.calls
        for hhmm in ('19:10', '19:40', '21:00', '23:30'):
            self.at(hhmm); self.r.restart(); self.poll()
            self.assertNotIn(self.entry()['status'], ('pending', 'held'), hhmm)
            code, body = self.go()
            self.assertEqual((code, body['status'], body['sender_calls']), (409, 'not_pending', 0), hhmm)
        self.assertEqual((self.r.lauren.calls, len(self.entry()['attempts'])), (calls, 1))


class Recording(Reconcile):
    def test_a_confirmed_delivery_is_recorded_with_an_audit_entry_and_nothing_is_sent(self):
        code, body = self.rec()
        self.assertEqual((code, body['status'], body['decision'], body['provider_outcome'], body['provider_asked']), (200, 'recorded_delivered', 'recorded_delivered', 'delivered', 1), body)
        self.assertTrue(body['resend'].startswith('never_by_this_path'))
        (asked,) = self.asked
        self.assertEqual((asked['idempotency_key'], asked['attempt_id'], asked['audience'], asked['deadline_seconds']), (self.op, self.attempt, 'Lauren', FAKE_CONTRACT.sender_timeout_seconds))
        entry = self.entry()
        self.assertEqual((entry['status'], entry['delivery_status'], entry['attempts'][0]['state'], entry['attempts'][0]['acked_by']), ('sent', 'sent', 'acked', 'reconciliation'))
        self.assertEqual((entry['ack']['provider_record_id'], entry['ack']['via'], self.r.lauren.calls), ('fake-status-record-1', 'reconciliation', 1))
        (audit,) = entry['reconciliations']
        self.assertEqual((audit['principal'], audit['decision'], audit['attempt_id'], audit['provider_outcome'], audit['provider_contract']),
                         ('alice', 'recorded_delivered', self.attempt, 'delivered', FAKE_CONTRACT.digest()))
        self.assertEqual((audit['before']['status'], audit['before']['delivery_status'], audit['after']['status'], audit['after']['delivery_status']),
                         ('uncertain', 'uncertain', 'sent', 'sent'))
        self.assertEqual((audit['approval']['record_id'], audit['approval']['issuer'], audit['proof']['provider_record_id'], len(audit['evidence']['sha256'])),
                         (self.r.approvals.records[self.digest]['record_id'], af.APPROVAL_ISSUER, 'fake-status-record-1', 64))
        self.never_resent()

    def test_an_authoritative_terminal_non_delivery_is_recorded_and_never_resent(self):
        code, body = self.rec(deps=self.deps(status_lookup=self.status('not_delivered')))
        self.assertEqual((code, body['status'], body['provider_outcome']), (200, 'recorded_not_delivered', 'terminal_not_delivered'), body)
        entry = self.entry()
        self.assertEqual((entry['status'], entry['delivery_status'], entry['attempts'][0]['state'], entry['attempts'][0]['closed_proof']['provider_record_id']),
                         ('not_delivered', 'not_delivered', 'closed_not_delivered', 'fake-status-record-1'))
        self.assertEqual((entry['reconciliations'][0]['decision'], entry['reconciliations'][0]['after']['status'], self.r.lauren.calls), ('recorded_not_delivered', 'not_delivered', 1))
        self.assertNotIn('ack', entry)
        self.never_resent()
        self.assertEqual(self.entry()['status'], 'not_delivered')              # still exactly that, after polls, restarts and dispatch attempts

    def test_anything_short_of_a_terminal_answer_changes_nothing(self):
        answers = dict(
            accepted=self.status('accepted'), provider_unknown=self.status('unknown', terminal=False, attempt_closed=False),
            bare_delivered_false=self.status(make=lambda o: dict(delivered=False)),
            loose_delivered_false=self.status(make=lambda o: dict(operation_id=o['operation_id'], attempt_id=o['attempt_id'], delivered=False, terminal=True)),
            loose_delivered_true=self.status(make=lambda o: dict(operation_id=o['operation_id'], attempt_id=o['attempt_id'], delivered=True)),
            not_found=self.status(make=lambda o: None), text=self.status(make=lambda o: 'not delivered'),
            not_delivered_open=self.status('not_delivered', attempt_closed=False), not_delivered_not_terminal=self.status('not_delivered', terminal=False),
            delivered_open=self.status(attempt_closed=False), cancel_requested=self.status(state='cancel_requested'),
            partial=self.status(recipients_accepted=1), two_answers=self.status(make=lambda o: [o, o]),
            from_2099=self.status(observed_at='2099-01-01T00:00:00+00:00'))
        for i, (name, lookup) in enumerate(answers.items(), 1):
            with self.subTest(name):
                code, body = self.rec(deps=self.deps(status_lookup=lookup))
                self.assertEqual((code, body['status'], body['reason'], body['provider_asked']), (202, 'held', 'provider_did_not_establish_an_outcome', 1), body)
                entry = self.unchanged(asked=i)
                self.assertEqual((len(entry['reconciliations']), entry['reconciliations'][-1]['decision'], entry['reconciliations'][-1]['before'], ),
                                 (i, 'provider_did_not_establish_an_outcome', entry['reconciliations'][-1]['after']))
        def down(**kw):
            self.asked.append(kw); raise ConnectionError('status service unreachable')
        code, body = self.rec(deps=self.deps(status_lookup=down))
        self.assertEqual((code, body['status'], body['provider_outcome'], body['provider_reasons']), (202, 'held', 'lookup_raised', ['ConnectionError']))
        self.unchanged(asked=len(answers) + 1)

    def test_a_passed_deadline_alone_reopens_nothing(self):
        for hhmm in ('19:30', '23:00', '2026-11-11T12:00:00+00:00'):            # far past the attempt's own deadline and the provider's
            self.at(hhmm)
            self.approve()
            code, body = self.rec(deps=self.deps(status_lookup=self.status('unknown', terminal=False, attempt_closed=False)))
            self.assertEqual((code, body['status']), (202, 'held'), hhmm)
            self.assertEqual((self.entry()['status'], self.r.lauren.calls), ('uncertain', 1))

    def test_a_proof_for_another_operation_attempt_artifact_or_recipient_is_not_a_proof(self):
        cases = dict(operation_id='another-operation', attempt_id='another-attempt', idempotency_key='another', recipient='buyer@client.invalid',
                     workbook_sha256='0' * 64, checklist_sha256='0' * 64, provider='another-provider', provider_contract='0' * 64, provider_record_id=' ')
        for i, (field, value) in enumerate(cases.items(), 1):
            for state in ('delivered', 'not_delivered'):
                code, body = self.rec(deps=self.deps(status_lookup=self.status(state, **{field: value})))
                self.assertEqual((code, body['status'], body['provider_outcome']), (202, 'held', 'unknown'), (field, state))
        self.unchanged(asked=2 * len(cases))


class InFlightAndLateAnswers(Dispatch):
    def setUp(self):
        super().setUp()
        self.helper = Reconcile('unchanged')

    def uncertain_in_flight(self):
        """The host died while the sender was running: the attempt is still marked in flight."""
        def dies(**kw):
            self.r.lauren.calls += 1
            raise Crash()
        with self.assertRaises(Crash):
            dispatch.handle('POST', rh.ALICE, self.r.dispatch_request('alpha', '1'), self.r.dispatch_deps(sender=dies))
        self.r.restart()
        attempt = self.entry()['attempts'][0]
        self.assertEqual((attempt['state'], self.entry()['delivery_status']), ('in_flight', 'uncertain'))
        return attempt

    def reconciler(self):
        h = Reconcile('unchanged')
        h.r, h.op, h.n, h.asked, h.attempt, h.approved = self.r, self.op, itertools.count(1), [], self.entry()['attempt_id'], self.approved
        h._testMethodName = self._testMethodName
        h.at, h.poll, h.item_reads = self.at, self.poll, self.item_reads
        h.digest = h.approve()
        return h

    def test_a_not_delivered_answer_does_not_close_an_attempt_that_may_still_be_in_flight(self):
        attempt = self.uncertain_in_flight()
        h = self.reconciler()
        code, body = h.rec(deps=h.deps(status_lookup=h.status('not_delivered')))
        self.assertEqual((code, body['status'], body['reason'], body['provider_outcome']), (202, 'held', 'attempt_may_still_be_in_flight', 'terminal_not_delivered'))
        entry = self.entry()
        self.assertEqual((entry['status'], entry['attempts'][0]['state'], entry['reconciliations'][0]['decision']), ('uncertain', 'in_flight', 'attempt_may_still_be_in_flight'))
        self.r.clock.set(attempt['deadline'])                                   # the sender call can no longer be running
        h.approve()
        code, body = h.rec(deps=h.deps(status_lookup=h.status('not_delivered')))
        self.assertEqual((code, body['status']), (200, 'recorded_not_delivered'))
        self.assertEqual((self.entry()['status'], self.r.lauren.calls), ('not_delivered', 1))

    def test_a_confirmed_delivery_is_recorded_even_while_in_flight(self):
        self.uncertain_in_flight()
        h = self.reconciler()
        self.assertEqual(h.rec()[1]['status'], 'recorded_delivered')
        self.assertEqual((self.entry()['status'], self.entry()['attempts'][0]['state'], self.r.lauren.calls), ('sent', 'acked', 1))

    def test_a_late_acknowledgment_from_the_sender_contradicts_a_recorded_non_delivery_openly(self):
        """A concurrent sender: while its call is still out, past the attempt's deadline, the provider's status says
        not delivered and that is recorded; then the sender returns a delivery. Both are kept; nothing is sent twice."""
        seen = {}
        def slow_sender(**kw):
            self.r.lauren.calls += 1
            self.r.clock.advance(seconds=901)                                   # past the attempt's own deadline
            h = self.reconciler()
            seen['reconcile'] = h.rec(deps=h.deps(status_lookup=h.status('not_delivered')))
            return rh.provider_outcome(FAKE_CONTRACT, idempotency_key=kw['idempotency_key'], attempt_id=kw['attempt_id'], audience=kw['audience'],
                                       payload=kw['payload'], deadline_seconds=kw['deadline_seconds'], observed_at=self.r.clock())
        code, body = self.go(deps=self.r.dispatch_deps(sender=slow_sender))
        self.assertEqual(seen['reconcile'][1]['status'], 'recorded_not_delivered')
        entry = self.entry()
        self.assertEqual((entry['delivery_status'], entry['proof_contradicted_by_late_ack'], entry['attempts'][0]['state'], self.r.lauren.calls), ('sent', True, 'acked', 1))
        self.assertEqual((entry['attempts'][0]['closed_proof']['provider_record_id'], entry['reconciliations'][0]['decision'], body['provider_deadline_breach']),
                         ('fake-status-record-1', 'recorded_not_delivered', True))
        self.assertEqual(self.go()[1]['status'], 'not_pending')

    def test_a_sender_that_fails_after_a_delivery_was_recorded_changes_nothing(self):
        seen = {}
        def failing_sender(**kw):
            self.r.lauren.calls += 1
            h = self.reconciler()
            seen['reconcile'] = h.rec()
            raise TimeoutError('gave up')
        code, body = self.go(deps=self.r.dispatch_deps(sender=failing_sender))
        self.assertEqual(seen['reconcile'][1]['status'], 'recorded_delivered')
        entry = self.entry()
        self.assertEqual((entry['status'], entry['delivery_status'], entry['attempts'][0]['state'], entry['attempts'][0]['late_error_type']), ('sent', 'sent', 'acked', 'TimeoutError'))


class Authorization(Reconcile):
    def denied(self, out, *checks):
        code, body = out
        self.assertEqual((code, body['status'], body['provider_asked']), (409, 'not_authorized', 0), body)
        for name in checks:
            self.assertIn(name, self.failed(body))
        self.unchanged()
        self.assertNotIn('reconciliations', self.entry())
        return body

    def test_default_deny_for_every_absent_interface(self):
        self.assertEqual(self.rec(deps=self.deps(authorizer=None)), (503, dict(status='not_configured')))
        self.assertEqual(reconcile.handle('POST', rh.ALICE, self.request(), self.r.dispatch_deps())[0], 503)      # the dispatch Deps is not enough
        self.assertEqual(self.rec(headers={'x-test-principal-proof': 'nobody'}), (401, dict(status='unauthorized')))
        for missing in ('approvals', 'issuers', 'recipients', 'status_lookup', 'reconcile_quota', 'provider_contract', 'store'):
            self.denied(self.rec(deps=self.deps(**{missing: None})))
        d = reconcile.Deps()
        self.assertEqual([getattr(d, n) for n in ('authorizer', 'approvals', 'status_lookup', 'reconcile_quota', 'provider_contract', 'recipients', 'issuers')], [None] * 7)
        with unittest.mock.patch.object(transport, 'VERIFIED_CONTRACTS', {}):   # as in production: there is no provider to ask
            self.denied(self.rec(), 'provider_contract_verified')
        for extra in (dict(approved=True), dict(proof=dict(delivered=True)), dict(status='delivered'), dict(resend=True), dict(principals=['alice'])):
            self.assertEqual(self.rec(**extra)[0], 400)
        for bad in (dict(mode='resend'), dict(attempt_id=''), dict(zone=5), dict(nonce='short'), dict(recipient=' Lauren')):
            self.assertEqual(self.rec(**bad)[0], 400)
        self.assertEqual(reconcile.handle('GET', rh.ALICE, self.request(), self.deps())[0], 405)
        self.unchanged()

    def test_only_an_approval_for_exactly_this_attempt_and_this_caller(self):
        records = self.r.approvals.records
        kept = deepcopy(records)
        del records[self.digest]
        self.assertEqual(self.failed(self.denied(self.rec(), APPROVED)), [APPROVED])     # run and dispatch approvals exist and do not count
        records[self.digest] = dict(kept[self.approved], snapshot_digest=self.digest)     # the dispatch approval's record under the reconcile digest
        self.assertIn('another action', self.denied(self.rec(), APPROVED)['checks'][-1]['detail'])
        records.clear(); records.update(deepcopy(kept))
        self.denied(self.rec(headers=BOB), APPROVED)                            # bob is not named
        for change in (dict(attempt_id='another-attempt'), dict(operation_id='another-operation'), dict(workbook_sha256='0' * 64), dict(checklist_sha256='0' * 64),
                       dict(shift='2'), dict(business_date='2026-11-09'), dict(client_id='BRAVO-REHEARSAL'), dict(zone='UTC')):
            self.denied(self.rec(**change))                                     # another attempt, artifact or scope is another digest
        self.denied(self.rec(recipient='buyer@client.invalid'), 'recipient_is_the_approved_one_for_this_client')
        self.denied(self.rec(deps=self.deps(recipients={'ALPHA-REHEARSAL': 'Client', 'BRAVO-REHEARSAL': 'Lauren'})), 'recipient_is_the_approved_one_for_this_client')
        other = replace(FAKE_CONTRACT, sender_timeout_seconds=601)
        trust(self, FAKE_CONTRACT, other)
        self.denied(self.rec(deps=self.deps(provider_contract=other)), APPROVED)   # another provider contract is another digest

    def test_every_lifecycle_failure_of_the_approval_denies(self):
        good = deepcopy(self.r.approvals.records[self.digest])
        for name, (change, reason) in dict(COMMON, **APPROVAL_ONLY).items():
            with self.subTest(name):
                change = dict(change, expires_at='2026-11-10T19:00:00+00:00') if name == 'expires_this_instant' else change
                change = dict(change, not_before='2026-11-10T19:00:01+00:00') if name == 'not_yet_valid' else change
                change = dict(change, approved_at='2026-11-10T19:05:01+00:00') if name == 'approved_in_the_future' else change
                change = dict(change, expires_at='2026-11-10T18:00:00+00:00') if name == 'expired_long_ago' else change
                self.r.approvals.records[self.digest] = changed(good, change)
                self.assertEqual(self.failed(self.denied(self.rec(), APPROVED)), [APPROVED])
        soon = (self.r.clock.at + rh.timedelta(seconds=reconcile.LIMIT_SECONDS)).isoformat()
        self.r.approvals.records[self.digest] = dict(good, expires_at=soon)
        self.denied(self.rec(), APPROVED)

    def test_alternating_records_never_add_up(self):
        good, digest, records = self.r.approvals.records[self.digest], self.digest, self.r.approvals.records
        for pair in ((dict(good, action='not_reconcile'), dict(good, principals=['bob'])), (dict(good, status='revoked'), dict(good, action=dispatch.ACTION)),
                     (dict(good, revoked=True), dict(good, issuer='another'))):
            for answers in (pair, pair[::-1]):
                class Alternates(FakeApprovals):
                    def __init__(s): s.records, s.asked = records, 0
                    def attestation(s, d):
                        if d != digest:
                            return deepcopy(records.get(d))
                        s.asked += 1
                        return deepcopy(answers[(s.asked - 1) % 2])
                split = Alternates()
                self.denied(self.rec(deps=self.deps(approvals=split)), APPROVED)
                self.assertEqual(split.asked, 1)

    def test_revoked_expired_or_renewed_around_the_lookup_records_nothing(self):
        good = deepcopy(self.r.approvals.records[self.digest])
        acts = dict(revoked=lambda: self.r.approvals.records[self.digest].update(status='revoked'),
                    arbitrary_flag=lambda: self.r.approvals.records[self.digest].update(revoked=True),
                    renewed=lambda: self.r.approvals.records[self.digest].update(record_id='renewed-during-lookup'),
                    gone=lambda: self.r.approvals.records.pop(self.digest),
                    expired=lambda: self.r.clock.advance(days=3), too_long=lambda: self.r.clock.advance(seconds=reconcile.LIMIT_SECONDS),
                    clock_back=lambda: setattr(self.r.clock, 'at', self.r.clock.at - rh.timedelta(seconds=1)),
                    watermark=lambda: ahead(self.r.store, self.r.clock), contract_withdrawn=lambda: trust(self))
        for name, act in acts.items():
            for state in ('delivered', 'not_delivered'):
                with self.subTest(name=name, state=state):
                    self.setUp()
                    code, body = self.rec(deps=self.deps(status_lookup=self.status(state, before=act)))      # happens while the provider is being asked
                    self.assertEqual((code, body['status'], body['reason']), (202, 'held', 'authorization_or_time_gate_failed_after_the_lookup'), body)
                    entry = self.entry()
                    self.assertEqual((entry['status'], entry['delivery_status'], entry['reconciliations'][0]['decision'], self.r.lauren.calls),
                                     ('uncertain', 'uncertain', 'authorization_or_time_gate_failed_after_the_lookup', 1))
                    trust(self, FAKE_CONTRACT)
        self.setUp()
        class Acts(FakeApprovals):                                               # withdrawn BEFORE the lookup: the provider is not asked at all
            def __init__(s, records): s.records, s.n = records, 0
            def attestation(s, d):
                s.n += 1
                return None if s.n >= 2 else deepcopy(s.records.get(d))
        code, body = self.rec(deps=self.deps(approvals=Acts(self.r.approvals.records)))
        self.assertEqual((code, body['status'], body['reason'], body['provider_asked']), (409, 'held', 'reconcile_approval_withdrawn', 0))
        self.unchanged()

    def test_the_host_clock_and_the_provider_contract_gates(self):
        ahead(self.r.store, self.r.clock)
        self.denied(self.rec(), 'host_clock_not_behind_the_last_seen_time')
        with self.r.store.transaction(hosted.CLOCK) as seen:
            seen.clear()
        for change in (dict(outcome_contract_version=0), dict(sender_timeout_seconds=0), dict(valid_until=''), dict(valid_until='2026-11-10T18:00:00+00:00')):
            contract = replace(FAKE_CONTRACT, **change)
            trust(self, contract)
            self.denied(self.rec(deps=self.deps(provider_contract=contract)), 'provider_contract_states_outcome_contract_deadline_and_validity')
        self.assertEqual(self.rec(timestamp='2026-11-10T18:00:00+00:00'), (401, dict(status='stale_or_undated_request')))


class StaleAndCross(Reconcile):
    def test_a_stale_attempt_another_clients_entry_or_another_artifact_is_refused_before_the_provider_is_asked(self):
        self.approve(attempt_id='an-earlier-attempt')
        code, body = self.rec(attempt_id='an-earlier-attempt')
        self.assertEqual((code, body['status'], body['provider_asked']), (409, 'stale_attempt', 0))
        key = 'outbox:' + self.r.item('alpha', '1')['key']
        with self.r.store.transaction(key) as box:
            box['entries'][self.op]['audience'] = 'Client'
        code, body = self.rec()
        self.assertEqual((code, body['status'], body['entry']['details'], body['provider_asked']), (409, 'artifact_mismatch', ['entry_is_addressed_to_someone_else'], 0))
        with self.r.store.transaction(key) as box:
            box['entries'][self.op]['audience'] = 'Lauren'
            box['entries'][self.op]['receipt']['binding'] = dict(box['entries'][self.op]['receipt']['binding'], client_id='BRAVO-REHEARSAL')
        code, body = self.rec()
        self.assertEqual((code, body['status'], body['provider_asked']), (409, 'artifact_mismatch', 0))
        self.assertIn('entry_belongs_to_another_client_site_list_or_job', body['entry']['details'])
        self.at('19:40'); self.poll()                                           # BRAVO's scope has no such operation
        bravo = self.r.item('bravo', '2')['scope']
        change = {k: bravo[k] for k in reconcile.SCOPE}
        self.approve(**change)
        code, body = self.rec(**change)
        self.assertEqual((code, body['status'], body['provider_asked']), (409, 'no_such_entry', 0))
        self.assertEqual((len(self.asked), self.r.lauren.calls), (0, 1))

    def test_nonce_replay_quota_and_one_callers_record_is_not_anothers(self):
        first = self.request()
        self.assertEqual(reconcile.handle('POST', rh.ALICE, first, self.deps())[1]['status'], 'recorded_delivered')
        code, body = reconcile.handle('POST', rh.ALICE, first, self.deps())      # the identical call again
        self.assertEqual((code, body['status'], body['provider_asked']), (409, 'replayed_request', 0))
        code, body = reconcile.handle('POST', rh.ALICE, dict(first, nonce='a-fresh-reconcile-nonce-1'), self.deps())
        self.assertEqual((code, body['status'], body['replayed'], body['provider_asked']), (200, 'recorded_delivered', True, 0))
        self.r.approvals.records[self.digest]['principals'] = ['alice', 'bob']
        code, body = reconcile.handle('POST', BOB, first, self.deps())           # same reconcile_id and nonce, another caller: his own record
        self.assertEqual((code, body['status'], body['principal'], body['provider_asked']), (409, 'nothing_to_reconcile', 'bob', 0))
        self.assertNotIn('provider_record_id', body); self.assertNotIn('replayed', body)
        self.assertEqual((len(self.asked), len(self.entry()['reconciliations'])), (1, 1))
        self.setUp()
        deps = lambda: self.deps(reconcile_quota=Quota(1), status_lookup=self.status('accepted'))
        self.assertEqual(self.rec(deps=deps())[1]['status'], 'held')
        code, body = self.rec(deps=deps())
        self.assertEqual((code, body['status'], body['provider_asked']), (429, 'quota_exceeded', 0))
        for bad in (Quota(0), Quota(True), 5):
            self.assertEqual(self.rec(deps=self.deps(reconcile_quota=bad))[1]['status'], 'not_authorized')

    def test_a_repeated_proof_and_concurrent_reconcilers_record_once(self):
        with ThreadPoolExecutor(6) as pool:
            out = list(pool.map(lambda i: self.rec()[1], range(6)))
        statuses = [b['status'] for b in out]
        self.assertEqual(statuses.count('recorded_delivered'), 1, statuses)
        self.assertTrue(set(statuses) <= {'recorded_delivered', 'nothing_to_reconcile', 'held'}, statuses)
        entry = self.entry()
        self.assertEqual((entry['status'], [a['decision'] for a in entry['reconciliations']].count('recorded_delivered'), self.r.lauren.calls), ('sent', 1, 1))
        for b in out:
            if b['status'] == 'held':
                self.assertEqual(b['reason'], 'nothing_to_reconcile')
        asked = len(self.asked)
        code, body = self.rec()                                                 # the same proof again, later
        self.assertEqual((code, body['status'], body['provider_asked'], len(self.asked)), (409, 'nothing_to_reconcile', 0, asked))

    def test_interrupted_persistence_records_nothing_and_is_never_resumed(self):
        def dies(**kw):
            self.asked.append(kw)
            raise Crash()                                                       # the host dies while the provider is being asked
        first = self.request()
        with self.assertRaises(Crash):
            reconcile.handle('POST', rh.ALICE, first, self.deps(status_lookup=dies))
        self.r.restart()
        self.unchanged(asked=1); self.assertNotIn('reconciliations', self.entry())
        code, body = reconcile.handle('POST', rh.ALICE, dict(first, nonce='a-fresh-reconcile-nonce-2'), self.deps())
        self.assertEqual((code, body['status'], body['provider_asked']), (409, 'reconcile_in_progress_or_interrupted', 0))
        self.assertEqual(self.rec()[1]['status'], 'recorded_delivered')         # a NEW, fully re-authorized reconciliation
        self.r.restart()
        self.assertEqual((self.entry()['status'], len(self.entry()['reconciliations'])), ('sent', 1))

    def test_the_audit_trail_is_bounded_and_nothing_is_recorded_beyond_it(self):
        key = 'outbox:' + self.r.item('alpha', '1')['key']
        with self.r.store.transaction(key) as box:
            box['entries'][self.op]['reconciliations'] = [dict(id=str(i), decision='provider_did_not_establish_an_outcome') for i in range(reconcile.MAX_AUDIT)]
        code, body = self.rec()
        self.assertEqual((code, body['status'], body['provider_asked']), (409, 'audit_trail_full', 0))
        self.assertEqual((self.entry()['status'], len(self.entry()['reconciliations'])), ('uncertain', reconcile.MAX_AUDIT))


class Boundaries(unittest.TestCase):
    def test_the_boundary_cannot_send_enqueue_or_reach_anything(self):
        text = (ROOT / 'hosting' / 'last_scan_reconcile.py').read_text()
        for forbidden in ('urllib', 'requests', 'http.client', 'smtplib', 'os.environ', 'getenv', 'psycopg', 'socket', 'subprocess', 'Bearer',
                          'deliver(', 'dispatch_test', 'reconcile_test', 'grant_key', '_queue', '_enqueue', "'pending'", 'sender(', 'ScheduledWorker'):
            self.assertNotIn(forbidden, text, forbidden)
        for path in list((ROOT / 'api').rglob('*.py')) + list((ROOT / 'hosting').glob('*.py')) + list((ROOT / 'scripts').glob('*.py')) + [ROOT / 'vercel.json']:
            if path.is_file() and path.name != 'last_scan_reconcile.py':
                self.assertNotIn('last_scan_reconcile', path.read_text(errors='ignore'), str(path))
        self.assertEqual(dict(transport.VERIFIED_CONTRACTS), {})
        lowered = text.lower()
        self.assertIn('prepared, not connected', lowered); self.assertNotIn('production-ready', lowered)


if __name__ == '__main__':
    unittest.main()
