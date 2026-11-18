"""Provider timeout, idempotency and uncertain-outcome boundary. FULLY SYNTHETIC. TEST ONLY.

Every provider, contract, sender and answer here is a fake. No provider adapter exists, nothing is sent anywhere, and
a pass here is not evidence about any real provider's behaviour.
"""
import unittest
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from unittest import mock

import rehearsal_harness as rh
from hosting import last_scan_dispatch as dispatch, last_scan_transport as transport
from test_last_scan_durable import FAKE_CONTRACT, trust
from test_last_scan_hosted_dispatch import Crash, Dispatch

ROOT = Path(__file__).resolve().parents[1]
CHECK = 'provider_contract_states_outcome_contract_deadline_and_validity'
EXPECTED = dict(operation_id='op-1', attempt_id='attempt-1', idempotency_key='op-1', recipient='Lauren', workbook_sha256='w' * 64, checklist_sha256='c' * 64)
PAYLOAD = dict(receipt=dict(workbook_sha256='w' * 64, checklist_sha256='c' * 64))


def answer(state='delivered', **change):
    return rh.provider_outcome(FAKE_CONTRACT, idempotency_key='op-1', attempt_id='attempt-1', audience='Lauren', payload=PAYLOAD,
                               deadline_seconds=FAKE_CONTRACT.sender_timeout_seconds, observed_at='2026-11-10T19:00:01+00:00', state=state, **change)


def judge(record, elapsed=1, contract=FAKE_CONTRACT, expected=EXPECTED):
    return transport.judge_outcome(record, expected, contract, elapsed)


class OutcomeContract(unittest.TestCase):
    def test_the_four_outcomes(self):
        self.assertEqual((judge(answer())['outcome'], judge(answer())['reasons']), ('delivered', []))
        self.assertEqual(judge(answer('accepted'))['outcome'], 'accepted')
        self.assertEqual(judge(answer('not_delivered'))['outcome'], 'terminal_not_delivered')
        self.assertEqual((judge(answer('unknown'))['outcome'], judge(answer('unknown'))['reasons']), ('unknown', ['provider_does_not_know']))

    def test_state_and_terminal_must_agree(self):
        for record, reason in ((answer(terminal=False), 'delivered_but_not_terminal'), (answer('accepted', terminal=True), 'accepted_cannot_be_terminal'),
                               (answer('not_delivered', terminal=False), 'not_delivered_without_terminal_closure'),
                               (answer('not_delivered', attempt_closed=False), 'not_delivered_without_terminal_closure')):
            self.assertEqual((judge(record)['outcome'], judge(record)['reasons']), ('unknown', [reason]))

    def test_anything_else_is_unknown_and_never_delivered(self):
        other = replace(FAKE_CONTRACT, version='2')
        cases = dict(
            wrong_operation=(dict(operation_id='op-2'), 'is_for_another_operation_id'),
            wrong_attempt=(dict(attempt_id='attempt-0'), 'is_for_another_attempt_id'),
            wrong_idempotency_key=(dict(idempotency_key='op-2'), 'is_for_another_idempotency_key'),
            wrong_recipient=(dict(recipient='buyer@client.invalid'), 'is_for_another_recipient'),
            recipient_list=(dict(recipient=['Lauren', 'someone']), 'is_for_another_recipient'),
            wrong_workbook=(dict(workbook_sha256='0' * 64), 'is_for_another_workbook_sha256'),
            wrong_checklist=(dict(checklist_sha256='0' * 64), 'is_for_another_checklist_sha256'),
            another_provider=(dict(provider='someone-else'), 'not_from_the_reviewed_provider_contract'),
            another_contract=(dict(provider_contract=other.digest()), 'not_from_the_reviewed_provider_contract'),
            no_provider_record=(dict(provider_record_id=' '), 'provider_record_id_missing'),
            record_number=(dict(provider_record_id=7), 'provider_record_id_missing'),
            partial_acceptance_state=(dict(state='partially_accepted'), 'unsupported_state'),
            cancel_requested_state=(dict(state='cancel_requested'), 'unsupported_state'),
            cancelled_state=(dict(state='cancelled'), 'unsupported_state'),
            sent_state=(dict(state='sent'), 'unsupported_state'),
            state_case=(dict(state='Delivered'), 'unsupported_state'),
            partial_acceptance_field=(dict(recipients_accepted=1, recipients_rejected=1), 'unknown_fields:recipients_accepted,recipients_rejected'),
            cancel_field=(dict(cancel_requested=True), 'unknown_fields:cancel_requested'),
            duplicate_field=(dict(duplicate_of='fake-provider-record-0'), 'unknown_fields:duplicate_of'),
            terminal_text=(dict(terminal='true'), 'terminal_and_attempt_closed_must_be_true_or_false'),
            terminal_one=(dict(terminal=1), 'terminal_and_attempt_closed_must_be_true_or_false'),
            observed_naive=(dict(observed_at='2026-11-10T19:00:01'), 'observed_at_missing_malformed_or_without_offset'),
            observed_missing=(dict(observed_at=None), 'observed_at_missing_malformed_or_without_offset'),
            another_deadline=(dict(request_deadline_seconds=3600), 'does_not_state_the_reviewed_request_deadline'),
            no_deadline=(dict(request_deadline_seconds=None), 'does_not_state_the_reviewed_request_deadline'),
            elapsed_text=(dict(elapsed_seconds='fast'), 'does_not_state_the_reviewed_request_deadline'),
            version_2=(dict(version=2), 'unsupported_version'), version_true=(dict(version=True), 'unsupported_version'),
            other_contract_name=(dict(contract='something.else'), 'unsupported_or_missing_contract'))
        for name, (change, reason) in cases.items():
            for state in ('delivered', 'accepted', 'not_delivered'):
                with self.subTest(name=name, state=state):
                    verdict = judge(dict(answer(state), **change))
                    self.assertEqual(verdict['outcome'], 'unknown'); self.assertIn(reason, verdict['reasons'])
        for field in transport.OUTCOME_REQUIRED:
            record = answer(); del record[field]
            self.assertEqual(judge(record)['outcome'], 'unknown', field)
        for shape in (None, True, 'delivered', 200, [answer()], [answer(), answer(record_id='second')], (answer(),), b'ok', object()):
            verdict = judge(shape)
            self.assertEqual((verdict['outcome'], verdict['reasons']), ('unknown', ['not_a_record']))
        loose = dict(operation_id='op-1', attempt_id='attempt-1')               # what used to be enough
        self.assertEqual(judge(loose)['outcome'], 'unknown')
        self.assertEqual(judge(dict(loose, delivered=True, state='delivered'))['outcome'], 'unknown')

    def test_the_request_deadline_is_reported_never_assumed(self):
        limit = FAKE_CONTRACT.sender_timeout_seconds
        self.assertEqual(judge(answer(), elapsed=limit)['deadline_breach'], False)
        for verdict in (judge(answer(), elapsed=limit + 0.001), judge(answer(elapsed_seconds=limit + 1)), judge(answer(), elapsed=None),
                        judge(answer(), elapsed=True), judge(answer(), contract=replace(FAKE_CONTRACT, sender_timeout_seconds=0), elapsed=1)):
            self.assertTrue(verdict['deadline_breach'])
        late = judge(answer(), elapsed=limit * 10)                               # late, and still a provider-stated terminal delivery
        self.assertEqual((late['outcome'], late['deadline_breach']), ('delivered', True))
        late = judge(answer('not_delivered'), elapsed=limit * 10)                # a late proof is still only proof
        self.assertEqual((late['outcome'], late['deadline_breach']), ('terminal_not_delivered', True))

    def test_evidence_is_kept_or_hashed_and_plain(self):
        record = answer('accepted')
        verdict = judge(record)
        self.assertEqual(verdict['evidence']['record'], record); self.assertIsNot(verdict['evidence']['record'], record)
        big = judge(dict(answer(), detail='x' * 5000))
        self.assertNotIn('record', big['evidence']); self.assertEqual(len(big['evidence']['sha256']), 64)
        self.assertEqual(judge(object())['evidence']['sha256'], None)
        class Shifty(dict):
            def get(self, key, default=None):
                return 'delivered' if key == 'state' else super().get(key, default)
        self.assertEqual(judge(Shifty(answer('accepted')))['outcome'], 'accepted')   # taken once, as what it holds


class DispatchOutcomes(Dispatch):
    """The hosted dispatch with FAKE senders answering in every way a provider adapter could."""
    def sender(self, make=None, before=None):
        test = self
        def send(**kw):
            test.r.lauren.calls += 1
            test.sent_with = kw
            if before:
                before()
            out = rh.provider_outcome(FAKE_CONTRACT, idempotency_key=kw['idempotency_key'], attempt_id=kw['attempt_id'], audience=kw['audience'],
                                      payload=kw['payload'], deadline_seconds=kw['deadline_seconds'], observed_at=test.r.clock())
            return make(out) if make else out
        return send

    def uncertain(self, out, provider_outcome, reason=None):
        code, body = out
        self.assertEqual((code, body['status'], body['provider_outcome'], body['sender_calls'], self.r.lauren.calls), (202, 'uncertain', provider_outcome, 1, 1), body)
        if reason:
            self.assertIn(reason, body['provider_reasons'])
        entry = self.entry()
        attempt = entry['attempts'][0]
        self.assertEqual((entry['status'], entry['delivery_status'], entry['provider_outcome'], len(entry['attempts'])), ('uncertain', 'uncertain', provider_outcome, 1))
        self.assertEqual((attempt['state'], attempt['provider_outcome']['outcome']), ('returned_error', provider_outcome))
        for hhmm in ('19:01', '19:30', '21:00'):                                 # asked again, after restarts and polls: never sent again
            self.at(hhmm); self.r.restart(); self.poll()
            code, again = self.go(deps=self.r.dispatch_deps(sender=self.sender()))
            self.assertEqual((code, again['status'], again['sources_read'], again['sender_calls']), (409, 'not_pending', 0, 0), hhmm)
        self.assertEqual((self.r.lauren.calls, len(self.entry()['attempts'])), (1, 1))
        return body, attempt

    def test_delivered_is_the_only_outcome_recorded_as_sent(self):
        code, body = self.go(deps=self.r.dispatch_deps(sender=self.sender()))
        self.assertEqual((code, body['status'], body['provider_outcome'], body['provider_reasons'], body['provider_deadline_breach']), (200, 'sent', 'delivered', [], False))
        self.assertEqual(self.sent_with['deadline_seconds'], FAKE_CONTRACT.sender_timeout_seconds)     # the adapter is told the reviewed deadline
        self.assertEqual((self.sent_with['idempotency_key'], self.sent_with['audience']), (self.op, 'Lauren'))
        entry = self.entry()
        self.assertEqual((entry['status'], entry['delivery_status'], entry['provider_outcome']), ('sent', 'sent', 'delivered'))
        evidence = entry['attempts'][0]['provider_outcome']['evidence']['record']
        self.assertEqual((evidence['operation_id'], evidence['attempt_id'], evidence['recipient'], evidence['workbook_sha256']),
                         (self.op, entry['attempts'][0]['id'], 'Lauren', entry['receipt']['workbook_sha256']))
        self.assertEqual(entry['ack']['provider_record_id'], evidence['provider_record_id'])

    def test_qa_repro_a_loose_acknowledgment_is_no_longer_delivery(self):
        """Before the outcome contract any dict naming the operation counted as sent, whatever else it said."""
        for make in (lambda out: dict(operation_id=out['operation_id'], attempt_id=out['attempt_id']),
                     lambda out: dict(operation_id=out['operation_id'], attempt_id=out['attempt_id'], state='accepted', queued=True),
                     lambda out: dict(operation_id=out['operation_id'])):
            self.setUp()
            code, body = self.go(deps=self.r.dispatch_deps(sender=self.sender(make)))
            self.assertEqual((code, body['status'], self.entry()['status']), (202, 'uncertain', 'uncertain'), body)

    def test_accepted_is_not_delivered(self):
        body, attempt = self.uncertain(self.go(deps=self.r.dispatch_deps(sender=self.sender(lambda out: dict(out, state='accepted', terminal=False, attempt_closed=False)))), 'accepted')
        self.assertEqual(attempt['provider_outcome']['evidence']['record']['state'], 'accepted')
        self.assertNotIn('ack', self.entry())

    def test_a_terminal_not_delivered_proof_is_kept_and_nothing_is_sent_again(self):
        body, attempt = self.uncertain(self.go(deps=self.r.dispatch_deps(sender=self.sender(lambda out: dict(out, state='not_delivered')))), 'terminal_not_delivered')
        self.assertEqual(attempt['provider_outcome']['evidence']['record']['attempt_closed'], True)   # the proof is there for a reviewed reconciliation

    def test_wrong_operation_attempt_key_recipient_artifact_or_provider(self):
        cases = dict(operation_id='another-operation', attempt_id='another-attempt', idempotency_key='another-key', recipient='buyer@client.invalid',
                     workbook_sha256='0' * 64, checklist_sha256='0' * 64, provider='another-provider', provider_contract='0' * 64, provider_record_id='')
        for field, value in cases.items():
            with self.subTest(field):
                self.setUp()
                code, body = self.go(deps=self.r.dispatch_deps(sender=self.sender(lambda out: dict(out, **{field: value}))))
                self.assertEqual((code, body['status'], body['provider_outcome'], self.entry()['status']), (202, 'uncertain', 'unknown', 'uncertain'), body)
                self.assertEqual(self.entry()['attempts'][0]['provider_outcome']['evidence']['record'][field], value)   # what it said is kept

    def test_partial_acceptance_duplicate_receipts_and_an_unconfirmed_cancel(self):
        makes = dict(partial_field=lambda out: dict(out, recipients_accepted=1, recipients_rejected=1),
                     partial_state=lambda out: dict(out, state='partially_accepted'),
                     two_receipts=lambda out: [out, dict(out, provider_record_id='a-second-record')],
                     duplicate_marker=lambda out: dict(out, duplicate_of='an-earlier-record'),
                     cancel_requested=lambda out: dict(out, state='cancel_requested', terminal=False),
                     cancel_field=lambda out: dict(out, state='not_delivered', cancel_requested=True),
                     provider_unsure=lambda out: dict(out, state='unknown', terminal=False, attempt_closed=False),
                     nothing=lambda out: None, text=lambda out: 'ok', true=lambda out: True)
        for name, make in makes.items():
            with self.subTest(name):
                self.setUp()
                code, body = self.go(deps=self.r.dispatch_deps(sender=self.sender(make)))
                self.assertEqual((code, body['status'], body['provider_outcome'], self.entry()['status'], self.r.lauren.calls), (202, 'uncertain', 'unknown', 'uncertain', 1), body)
                self.assertEqual(self.go(deps=self.r.dispatch_deps(sender=self.sender()))[1]['status'], 'not_pending')
                self.assertEqual(self.r.lauren.calls, 1)

    def test_a_timeout_or_disconnect_proves_nothing_and_is_never_retried(self):
        for error in (TimeoutError('no answer within the deadline'), ConnectionResetError('disconnected mid-request'), OSError('broken pipe')):
            self.setUp()
            def raises(out, error=error):
                raise error
            body, attempt = self.uncertain(self.go(deps=self.r.dispatch_deps(sender=self.sender(raises))), 'sender_raised', type(error).__name__)
            self.assertEqual(attempt['error_type'], type(error).__name__)

    def test_a_blocking_sender_is_recorded_as_breaking_its_deadline_not_cancelled_and_not_failed(self):
        limit = FAKE_CONTRACT.sender_timeout_seconds
        code, body = self.go(deps=self.r.dispatch_deps(sender=self.sender(before=lambda: self.r.clock.advance(seconds=61))))
        self.assertEqual((code, body['status'], body['provider_deadline_breach']), (200, 'sent', False))     # 61 s is inside the provider's own deadline
        self.setUp()
        code, body = self.go(deps=self.r.dispatch_deps(sender=self.sender(before=lambda: self.r.clock.advance(seconds=limit + 1))))
        self.assertEqual((code, body['status'], body['provider_outcome'], body['provider_deadline_breach']), (200, 'sent', 'delivered', True))   # late, yet the provider says delivered
        self.assertEqual((self.entry()['provider_deadline_breach'], self.entry()['ack']['deadline_breach']), (True, True))
        self.setUp()
        def gives_up(out):
            raise TimeoutError('gave up')
        code, body = self.go(deps=self.r.dispatch_deps(sender=self.sender(gives_up, before=lambda: self.r.clock.advance(seconds=limit + 1))))
        self.assertEqual((code, body['status'], body['provider_outcome'], body['provider_deadline_breach']), (202, 'uncertain', 'sender_raised', True))
        self.setUp()
        code, body = self.go(deps=self.r.dispatch_deps(sender=self.sender(lambda out: dict(out, elapsed_seconds=limit + 5))))   # by its own statement
        self.assertEqual((body['status'], body['provider_deadline_breach']), ('sent', True))

    def test_process_death_during_the_send_and_restart(self):
        def dies(out):
            raise Crash()
        first = self.r.dispatch_request('alpha', '1')
        with self.assertRaises(Crash):
            dispatch.handle('POST', rh.ALICE, first, self.r.dispatch_deps(sender=self.sender(dies)))
        self.r.restart()
        entry = self.entry()
        self.assertEqual((entry['status'], entry['delivery_status'], entry['attempts'][0]['state'], self.r.lauren.calls), ('uncertain', 'uncertain', 'in_flight', 1))
        self.assertNotIn('provider_outcome', entry)                             # nothing was learned; nothing is invented
        for _ in range(2):
            self.r.restart()
            self.assertEqual(self.go(deps=self.r.dispatch_deps(sender=self.sender()))[1]['status'], 'not_pending')
        self.assertEqual(self.r.lauren.calls, 1)

    def test_concurrent_dispatches_with_a_slow_accepting_provider_call_it_once(self):
        slow = self.sender(lambda out: dict(out, state='accepted', terminal=False, attempt_closed=False))
        with ThreadPoolExecutor(6) as pool:
            out = list(pool.map(lambda i: self.go(deps=self.r.dispatch_deps(sender=slow))[1]['status'], range(6)))
        self.assertEqual((out.count('uncertain'), out.count('sent'), self.r.lauren.calls, len(self.entry()['attempts'])), (1, 0, 1, 1), out)

    def test_the_provider_contract_must_state_outcome_version_deadline_and_validity(self):
        for change in (dict(outcome_contract_version=0), dict(outcome_contract_version=2), dict(outcome_contract_version=True), dict(sender_timeout_seconds=0),
                       dict(sender_timeout_seconds=30.5), dict(valid_until=''), dict(valid_until='2026-11-13'), dict(valid_until='2026-11-10T18:00:00+00:00'),
                       dict(valid_until=(self.r.clock.at + rh.timedelta(seconds=dispatch.GRANT_SECONDS)).isoformat())):
            contract = replace(FAKE_CONTRACT, **change)
            trust(self, contract)
            self.r.approve_dispatch(deps=self.r.dispatch_deps(provider_contract=contract))
            body = self.go(deps=self.r.dispatch_deps(provider_contract=contract))[1]
            self.assertEqual((body['status'], body['sender_calls'], self.r.lauren.calls), ('not_authorized', 0, 0), change)
            self.assertIn(CHECK, self.failed(body))
        trust(self, FAKE_CONTRACT)
        self.assertEqual(self.go()[1]['status'], 'sent')

    def test_a_contract_that_lapses_or_is_withdrawn_before_the_send_does_not_call_the_sender(self):
        soon = replace(FAKE_CONTRACT, valid_until=(self.r.clock.at + rh.timedelta(seconds=120)).isoformat())
        trust(self, soon)
        self.r.approve_dispatch(deps=self.r.dispatch_deps(provider_contract=soon))
        def fetch(scope, settings):
            self.r.clock.advance(seconds=50)
            return self.r.fetch(scope, settings)
        gate = dispatch.ScheduledWorker._gate
        def late(*a, **kw):
            out = gate(*a, **kw)
            trust(self)                                                         # the registry no longer lists it: withdrawn after the claim
            return out
        with mock.patch.object(dispatch.ScheduledWorker, '_gate', staticmethod(late)):
            code, body = self.go(deps=self.r.dispatch_deps(provider_contract=soon, sender=self.sender()))
        self.assertEqual((code, body['status'], body['refusal'], body['sender_calls'], self.r.lauren.calls), (202, 'uncertain', 'provider_contract_no_longer_verified', 0, 0), body)
        self.setUp()
        trust(self, soon)
        self.r.approve_dispatch(deps=self.r.dispatch_deps(provider_contract=soon))
        self.r.clock.advance(seconds=61)                                        # 59 s of review validity left: refused up front
        body = self.go(deps=self.r.dispatch_deps(provider_contract=soon, sender=self.sender()))[1]
        self.assertEqual((body['status'], self.r.lauren.calls), ('not_authorized', 0)); self.assertIn(CHECK, self.failed(body))

    def test_a_schema_change_before_delivery_still_stops_it(self):
        scan = next(c for c in self.r.tenants['alpha'].columns_now if c['name'] == 'ActualScan')
        scan['calculated'] = {'formula': '=[Created]', 'outputType': 'dateTime'}
        code, body = self.go(deps=self.r.dispatch_deps(sender=self.sender()))
        self.assertEqual((body['status'], body['sources_read'], body['sender_calls'], body['provider_outcome'], self.r.lauren.calls), ('held', 0, 0, None, 0))


class Boundaries(unittest.TestCase):
    def test_no_adapter_no_timeout_claim_and_no_exactly_once_claim(self):
        text = (ROOT / 'hosting' / 'last_scan_transport.py').read_text() + (ROOT / 'hosting' / 'last_scan_dispatch.py').read_text()
        for forbidden in ('urllib', 'smtplib', 'http.client', 'socket', 'signal.alarm', 'threading', 'asyncio', 'wait_for', 'concurrent.futures'):
            self.assertNotIn(forbidden, text, forbidden)                         # no client, and no pretend timeout around the sender
        self.assertEqual(dict(transport.VERIFIED_CONTRACTS), {})
        default = transport.ProviderContract(provider='p', version='1')
        self.assertEqual((default.outcome_contract_version, default.valid_until, default.sender_timeout_seconds), (0, '', 0))
        lowered = text.lower()
        self.assertIn('cannot cancel a request that is already with a provider', lowered.replace('\n', ' '))
        self.assertIn('nothing here claims exactly-once delivery', lowered)


if __name__ == '__main__':
    unittest.main()
