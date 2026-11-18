"""Hosted dispatch authorization boundary (hosting.last_scan_dispatch). FULLY SYNTHETIC. TEST ONLY.

Every identity, approval, capture record, probe, provider contract, recipient and sender here is a fake from the test
suite (tests/rehearsal_harness.py). Nothing reaches a network, a real source or a real inbox, and a pass here is not
evidence that any real authority or provider exists.
"""
import inspect
import io
import re
import unittest
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path

from openpyxl import load_workbook

import readiness_fixture as rf
import rehearsal_harness as rh
from hosting import last_scan_dispatch as dispatch, last_scan_hosted as hosted, last_scan_transport as transport, source_first as sf
from hosting.last_scan_hosted import Quota
from rehearsal_harness import DAY, LAUREN
from test_last_scan_durable import FAKE_CONTRACT, trust
from test_last_scan_hosted import BOB, FakeAuthorizer
from test_one_hour_rehearsal import HTTP_DATE, Base

ROOT = Path(__file__).resolve().parents[1]
ALPHA, BRAVO = 'ALPHA-REHEARSAL', 'BRAVO-REHEARSAL'


class Crash(BaseException):
    """The host process dies: not an Exception, so nothing in the code under test may swallow it."""


class Dispatch(Base):
    """One ready ALPHA shift-1 draft, a fake verified contract, and fake run and dispatch approvals for alice."""
    def setUp(self):
        super().setUp()
        trust(self, FAKE_CONTRACT)
        self.op = self.poll()[('alpha', '1')]['operation_id']
        self.approved = self.r.approve_dispatch()
        self.reads = self.item_reads('alpha')

    def go(self, client='alpha', shift='1', **kw):
        return self.r.hosted_dispatch(client, shift, **kw)

    def raw(self, **change):
        """A call with fields altered after the harness built it (wrong types, another scope)."""
        return dispatch.handle('POST', rh.ALICE, dict(self.r.dispatch_request('alpha', '1'), **change), self.r.dispatch_deps())

    def failed(self, body):
        return [c['check'] for c in body['checks'] if not c['ok']]

    def entry(self, client='alpha', shift='1', op=None):
        return self.r.outbox(client, shift)[op or self.op]

    def untouched(self, client='alpha'):
        """No item was read after setUp, the fake sender was never called, and the entry is still pending."""
        self.assertEqual((self.item_reads(client), self.r.lauren.calls), (self.reads, 0))

    def refused(self, out, *checks):
        code, body = out
        self.assertEqual((code, body['status'], body['sources_read'], body['sender_calls']), (409, 'not_authorized', 0, 0), body)
        for name in checks:
            self.assertIn(name, self.failed(body))
        self.untouched()
        self.assertEqual(self.entry()['status'], 'pending')
        return body

    def reapprove(self, principals=('alice',), **kw):
        """The FAKE approver approves a changed configuration or policy (run snapshot and dispatch digest)."""
        code, dry = self.r.call(mode='dry_run', deps=self.r.deps(**{k: v for k, v in kw.items() if k == 'config'}))
        self.assertEqual(code, 200, dry)
        self.r.approvals.records[dry['snapshot_digest']] = dict(snapshot_digest=dry['snapshot_digest'], approved_by='rehearsal fake approver',
                                                                approved_at=DAY, principals=list(principals))
        return self.r.approve_dispatch(principals, deps=self.r.dispatch_deps(**kw))


class Positive(Dispatch):
    def test_one_real_native_draft_reaches_the_fake_inbox_once(self):
        draft = self.r.draft('alpha', '1')
        code, body = self.go()
        self.assertEqual((code, body['status'], body['sources_read'], body['schema_probes'], body['sender_calls']), (200, 'sent', 1, 1, 1), body)
        self.assertEqual((body['audience'], body['client_release'], body['principal'], body['dispatch_digest']), ('Lauren', 'held', 'alice', self.approved))
        self.assertTrue(body['source_send_race'].startswith('not_closed'))
        (message,) = self.r.lauren.messages
        self.assertEqual((message['to'], message['operation_id'], message['payload']['audience']), (LAUREN, self.op, 'Lauren'))
        receipt = message['payload']['receipt']
        self.assertEqual((receipt['binding']['client_id'], receipt['client_release'], receipt['format']), (ALPHA, 'held', draft['receipt']['format']))
        self.assertEqual(receipt['workbook_sha256'], sf.digest(draft['workbook']))             # the exact stored native workbook
        book = load_workbook(io.BytesIO(draft['workbook']), data_only=True)
        self.assertIn('Summary', book.sheetnames); self.assertIn('ALPHA REHEARSAL CO (fictitious)', rh.texts(book))
        entry = self.entry()
        self.assertEqual((entry['status'], entry['delivery_status'], len(entry['attempts'])), ('sent', 'sent', 1))
        self.assertEqual(self.item_reads('alpha'), self.reads + 1)                             # exactly one re-read, under the lease

    def test_a_dry_run_reads_nothing_sends_nothing_and_spends_nothing(self):
        code, body = self.go(mode='dry_run')
        self.assertEqual((code, body['status'], body['authorized'], body['draft']), (200, 'dry_run', True, dict(state='ready', details=[])))
        self.untouched(); self.assertEqual(self.entry()['status'], 'pending')
        self.assertEqual(self.go()[1]['status'], 'sent')

    def test_asked_again_nothing_more_is_sent(self):
        self.assertEqual(self.go()[1]['status'], 'sent')
        for _ in range(3):
            code, body = self.go()
            self.assertEqual((code, body['status'], body['sources_read'], body['sender_calls']), (409, 'not_pending', 0, 0))
        self.assertEqual(self.item_reads('alpha'), self.reads + 1)              # the one dispatch re-read, nothing since
        self.at('19:30'); self.r.restart(); self.poll()
        after_poll = self.item_reads('alpha')
        self.assertEqual(self.go()[1]['status'], 'not_pending')
        self.assertEqual((len(self.r.lauren.messages), self.item_reads('alpha')), (1, after_poll))


class DefaultDeny(Dispatch):
    def test_no_identity_no_dispatch(self):
        self.assertEqual(self.go(deps=self.r.dispatch_deps(authorizer=None)), (503, dict(status='not_configured')))
        self.assertEqual(self.go(headers={'x-test-principal-proof': 'nobody'}), (401, dict(status='unauthorized')))
        self.assertEqual(self.go(headers={}), (401, dict(status='unauthorized')))
        self.assertEqual(dispatch.handle('POST', rh.ALICE, self.r.dispatch_request('alpha', '1'), self.r.deps())[0], 503)   # the run's Deps is not enough
        self.untouched()

    def test_every_absent_interface_denies_before_any_read(self):
        for missing in ('approvals', 'captures', 'collectors', 'schema_probe', 'schemas', 'recipients', 'sender', 'dispatch_quota',
                        'provider_contract', 'quota', 'fetch', 'store', 'artifacts', 'entries', 'config'):
            with self.subTest(missing):
                code, body = self.go(deps=self.r.dispatch_deps(**{missing: None}))
                self.assertEqual((code, body['status'], body['sources_read'], body['sender_calls']), (409, 'not_authorized', 0, 0), missing)
                self.untouched()
        self.assertEqual(self.entry()['status'], 'pending')

    def test_defaults_are_all_none_and_the_real_registry_is_empty(self):
        d = dispatch.Deps()
        self.assertEqual([getattr(d, n) for n in ('authorizer', 'approvals', 'recipients', 'sender', 'dispatch_quota', 'provider_contract')], [None] * 6)
        self.assertEqual(dispatch.handle('POST', rh.ALICE, self.r.dispatch_request('alpha', '1'), d), (503, dict(status='not_configured')))
        with unittest.mock.patch.object(transport, 'VERIFIED_CONTRACTS', {}):                  # as in production: no provider is verified
            self.refused(self.go(), 'provider_contract_verified', 'dispatch_approved_for_this_snapshot_recipients_contract_and_principal')

    def test_a_run_approval_is_not_a_dispatch_approval(self):
        del self.r.approvals.records[self.approved]
        body = self.refused(self.go(), 'dispatch_approved_for_this_snapshot_recipients_contract_and_principal')
        self.assertEqual(self.failed(body), ['dispatch_approved_for_this_snapshot_recipients_contract_and_principal'])
        run_record = self.r.approvals.records[body['snapshot_digest']]
        self.r.approvals.records[self.approved] = dict(run_record, snapshot_digest=self.approved)        # a run-shaped record under the dispatch digest
        self.assertIn('not for the dispatch action', self.refused(self.go())['checks'][-1]['detail'])

    def test_the_caller_must_be_named_in_both_approvals(self):
        self.refused(self.go(headers=BOB), 'approval_attested_for_this_snapshot_and_principal',
                     'dispatch_approved_for_this_snapshot_recipients_contract_and_principal')
        self.r.approvals.records[self.approved]['principals'] = ['alice', 'bob']                  # dispatch only: still not allowed to run
        body = self.refused(self.go(headers=BOB), 'approval_attested_for_this_snapshot_and_principal')
        self.assertEqual(self.failed(body), ['approval_attested_for_this_snapshot_and_principal'])

    def test_nothing_in_the_call_or_the_configuration_can_approve_itself(self):
        for extra in (dict(approved=True), dict(principals=['alice']), dict(grant='x'), dict(provider_contract='x'), dict(sender='x')):
            self.assertEqual(self.go(**extra)[0], 400, extra)
        config = deepcopy(self.r.config)
        config['dispatch_approval'] = dict(action=dispatch.ACTION, principals=['alice'], snapshot_digest=self.approved)
        config['recipients'] = {ALPHA: 'Lauren'}
        self.refused(self.go(deps=self.r.dispatch_deps(config=config, recipients=None)), 'recipient_policy_configured')
        self.refused(self.go(deps=self.r.dispatch_deps(config=config)), 'approval_attested_for_this_snapshot_and_principal')

    def test_malformed_calls_are_refused(self):
        for change in (dict(mode='send'), dict(recipient=' Lauren'), dict(operation_id=''), dict(shift=1), dict(nonce='short'), dict(dispatch_id='x' * 401)):
            self.assertEqual(self.raw(**change)[0], 400, change)
        self.assertEqual(self.go(timestamp='2026-11-10T18:00:00+00:00'), (401, dict(status='stale_or_undated_request')))
        self.assertEqual(dispatch.handle('GET', rh.ALICE, self.r.dispatch_request('alpha', '1'), self.r.dispatch_deps())[0], 405)
        self.untouched()


class SchemaDrift(Dispatch):
    def drift(self):
        scan = next(c for c in self.r.tenants['alpha'].columns_now if c['name'] == 'ActualScan')
        scan['calculated'] = {'formula': '=[Created]', 'outputType': 'dateTime'}
        return scan

    def test_a_column_that_drifted_after_the_draft_was_built_stops_the_dispatch_before_any_item_read(self):
        scan = self.drift()
        code, body = self.go()
        self.assertEqual((code, body['status'], body['sources_read'], body['schema_probes'], body['sender_calls']), (409, 'held', 0, 1, 0), body)
        self.assertEqual((body['refusal'], body['outcome']['error_type']), ('schema:live_schema_differs_from_the_approved_snapshot', 'SchemaDrift'))
        self.untouched()
        self.assertEqual((self.entry()['status'], self.entry()['held_reason']), ('held', 'recapture_failed'))
        self.assertEqual(self.go()[1]['refusal'], 'schema:live_schema_differs_from_the_approved_snapshot')   # still drifted: still nothing read
        self.untouched()
        del scan['calculated']                                                  # the list is put back exactly as approved
        self.assertEqual(self.go()[1]['status'], 'sent')
        self.assertEqual(len(self.r.lauren.messages), 1)

    def test_a_probe_answer_that_is_stale_or_for_another_list_is_drift(self):
        class Stale(rh.AdapterProbe):
            def current(self, site_id, list_id):
                return dict(super().current(site_id, list_id), captured_at='2026-11-10T18:00:00+00:00')
        class Other(rh.AdapterProbe):
            def current(self, site_id, list_id):
                return dict(super().current(site_id, list_id), list_id='another-list')
        class Broken(rh.AdapterProbe):
            def current(self, site_id, list_id):
                raise RuntimeError('probe down')
        for probe, reason in ((Stale, 'schema_probe_answer_is_not_fresh'), (Other, 'schema_probe_answer_is_malformed_or_for_another_list'),
                              (Broken, 'schema_probe_failed:RuntimeError')):
            code, body = self.go(deps=self.r.dispatch_deps(schema_probe=probe(self.r.tenants, self.r.clock)))
            self.assertEqual((code, body['status'], body['refusal'], body['sources_read']), (409, 'held', 'schema:' + reason, 0))
        self.untouched()


class Revocation(Dispatch):
    def test_a_withdrawn_run_approval_or_dispatch_approval_denies(self):
        records = self.r.approvals.records
        kept = dict(records)
        run = next(k for k in records if k != self.approved)
        del records[run]
        self.refused(self.go(), 'approval_attested_for_this_snapshot_and_principal')
        records.clear(); records.update(kept); del records[self.approved]
        self.refused(self.go(), 'dispatch_approved_for_this_snapshot_recipients_contract_and_principal')
        records.clear(); records.update(kept)
        self.assertEqual(self.go()[1]['status'], 'sent')

    def test_an_approval_withdrawn_while_the_source_is_being_re_read_stops_the_claim(self):
        for which, refusal in (('dispatch', 'dispatch_approval_withdrawn'), ('run', 'run_approval_withdrawn')):
            records = self.r.approvals.records
            kept = dict(records)
            key = self.approved if which == 'dispatch' else next(k for k in records if k != self.approved)
            def fetch(scope, settings):
                records.pop(key, None)                                          # withdrawn after every earlier check passed
                return self.r.fetch(scope, settings)
            code, body = self.go(deps=self.r.dispatch_deps(fetch=fetch))
            self.assertEqual((code, body['status'], body['refusal'], body['outcome']['error_type'], body['sources_read'], body['sender_calls']),
                             (409, 'held', refusal, 'Revoked', 1, 0), which)
            self.assertEqual(self.r.lauren.calls, 0)
            records.clear(); records.update(kept)
        self.assertEqual(self.go()[1]['status'], 'sent')                        # approved again: a fresh recapture, then one message
        self.assertEqual(len(self.r.lauren.messages), 1)

    def test_a_capture_receipt_withdrawn_or_from_a_collector_no_longer_accepted_denies(self):
        captures = self.r.captures.records
        kept = deepcopy(captures)
        self.refused(self.go(deps=self.r.dispatch_deps(collectors=('someone-else',))), 'source_mapping_ready_for_every_assignment')
        captures.clear()
        self.refused(self.go(), 'source_mapping_ready_for_every_assignment')
        captures.update(kept)
        def fetch(scope, settings):
            captures.clear()                                                    # withdrawn after every earlier check passed
            return self.r.fetch(scope, settings)
        code, body = self.go(deps=self.r.dispatch_deps(fetch=fetch))
        self.assertEqual((code, body['status'], body['refusal'], body['sources_read'], body['sender_calls']),
                         (409, 'held', 'capture_receipt:no_capture_receipt_for_this_exact_snapshot', 1, 0))
        captures.update(kept)
        self.at('2026-11-11T19:00:00+00:00')                                    # the approved capture is now older than the bound
        self.assertEqual(self.go()[1]['status'], 'not_authorized')
        self.assertEqual(self.r.lauren.calls, 0)

    def test_a_scope_removed_from_the_approved_plan_cannot_be_dispatched(self):
        config = deepcopy(self.r.config)
        config['assignments'][0]['active'] = False                              # ALPHA is switched off
        self.refused(self.go(deps=self.r.dispatch_deps(config=config)), 'approval_attested_for_this_snapshot_and_principal')
        self.reapprove(config=config)                                           # even with the changed configuration approved
        body = self.refused(self.go(deps=self.r.dispatch_deps(config=config)), 'scope_is_exactly_one_approved_planned_scope')
        self.assertEqual(self.failed(body), ['scope_is_exactly_one_approved_planned_scope'])
        for change in (dict(shift='9'), dict(business_date='2026-11-09'), dict(job='RH-7002'), dict(list_id=rh.CLIENTS['bravo']['list_id'])):
            self.refused(self.raw(**change), 'scope_is_exactly_one_approved_planned_scope')


class ChangedTemplate(Dispatch):
    def test_a_draft_built_with_an_earlier_header_or_template_is_not_sent_under_a_new_one(self):
        config = deepcopy(self.r.config)
        config['assignments'][0]['header']['part_name'] = 'Alpha bracket rev B'
        self.assertEqual(self.go(deps=self.r.dispatch_deps(config=config))[1]['status'], 'not_authorized')   # changed, neither attested nor approved
        config['assignments'][0] = rf.attested(config['assignments'][0], self.r.schemas[0])[0]               # the TEST-ONLY reviewer attests the new header
        self.refused(self.go(deps=self.r.dispatch_deps(config=config)), 'approval_attested_for_this_snapshot_and_principal',
                     'dispatch_approved_for_this_snapshot_recipients_contract_and_principal')
        self.reapprove(config=config)                                           # the new template is approved; the old draft is still the old draft
        code, body = self.go(deps=self.r.dispatch_deps(config=config))
        self.assertEqual((code, body['status'], body['sources_read'], body['sender_calls']), (409, 'artifact_mismatch', 0, 0), body)
        self.assertIn('template_or_mapping_changed_since_the_draft_was_built', body['draft']['details'])
        self.untouched()

    def test_a_call_naming_another_render_version_or_hash_is_refused(self):
        for field in ('render_version', 'workbook_sha256', 'checklist_sha256'):
            code, body = self.go(**{field: 'f' * 64})
            self.assertEqual((code, body['status']), (409, 'artifact_mismatch'), field)
            self.assertIn('stored_receipt_differs:' + field, body['draft']['details'])
        self.untouched(); self.assertEqual(self.entry()['status'], 'pending')


class WrongRecipient(Dispatch):
    def test_only_the_recipient_the_server_side_policy_names_and_only_the_internal_audience(self):
        self.refused(self.go(recipient='buyer@client.invalid'), 'recipient_is_the_approved_one_for_this_client')
        self.refused(self.go(recipient='lauren'), 'recipient_is_the_approved_one_for_this_client')
        client_policy = {ALPHA: 'Client', BRAVO: 'Lauren'}
        self.refused(self.go(deps=self.r.dispatch_deps(recipients=client_policy)), 'recipient_is_the_approved_one_for_this_client',
                     'dispatch_approved_for_this_snapshot_recipients_contract_and_principal')
        self.reapprove(recipients=client_policy)                                # even an approved policy cannot name a client audience
        body = self.refused(self.go(deps=self.r.dispatch_deps(recipients=client_policy), recipient='Client'), 'recipient_is_the_approved_one_for_this_client')
        self.assertEqual(self.failed(body), ['recipient_is_the_approved_one_for_this_client'])
        self.refused(self.go(deps=self.r.dispatch_deps(recipients={BRAVO: 'Lauren'})), 'recipient_is_the_approved_one_for_this_client')
        for bad in ({}, [(ALPHA, 'Lauren')], {ALPHA: ' Lauren'}, {ALPHA: None}, 'Lauren'):
            self.refused(self.go(deps=self.r.dispatch_deps(recipients=bad)), 'recipient_policy_configured')

    def test_an_outbox_entry_addressed_elsewhere_is_not_sent(self):
        key = 'outbox:' + self.r.item('alpha', '1')['key']
        with self.r.store.transaction(key) as box:
            box['entries'][self.op]['audience'] = 'Client'
        code, body = self.go()
        self.assertEqual((code, body['status'], body['draft']['details']), (409, 'artifact_mismatch', ['entry_is_addressed_to_someone_else']))
        self.untouched()

    def test_the_policy_is_frozen_before_the_first_callback(self):
        class Meddler(FakeAuthorizer):
            def __init__(self, act):
                super().__init__(); self.act = act
            def verify(self, *a):
                self.act()
                return super().verify(*a)
        bad = {ALPHA: 'Client', BRAVO: 'Lauren'}
        self.refused(self.go(deps=self.r.dispatch_deps(recipients=bad, authorizer=Meddler(lambda: bad.update({ALPHA: 'Lauren'})))),
                     'recipient_is_the_approved_one_for_this_client')              # fixed too late: judged as it arrived
        good, config = {ALPHA: 'Lauren', BRAVO: 'Lauren'}, self.r.config
        def meddle():
            good[ALPHA] = 'Client'; good['EXTRA'] = 'Client'
            config['assignments'][0]['header']['part_name'] = 'changed during the call'
        try:
            code, body = self.go(deps=self.r.dispatch_deps(recipients=good, authorizer=Meddler(meddle)))
        finally:
            config['assignments'][0]['header']['part_name'] = 'Alpha bracket'
        self.assertEqual((code, body['status'], body['dispatch_digest']), (200, 'sent', self.approved))   # judged, and sent, as approved
        self.assertEqual(self.r.lauren.messages[0]['payload']['audience'], 'Lauren')


class CrossClient(Dispatch):
    def setUp(self):
        super().setUp()
        self.at('19:40')
        self.bravo = self.poll()[('bravo', '2')]['operation_id']
        self.reads = self.item_reads('alpha')

    def test_one_clients_draft_cannot_be_dispatched_through_anothers_scope(self):
        code, body = self.go('bravo', '2', op=self.op)                          # ALPHA's operation, BRAVO's scope
        self.assertEqual((code, body['status'], body['draft']['details'], body['sources_read']), (409, 'not_pending', ['no_such_entry'], 0))
        alpha, bravo = self.entry()['receipt'], self.entry('bravo', '2', self.bravo)['receipt']
        code, body = self.go(workbook_sha256=bravo['workbook_sha256'], checklist_sha256=bravo['checklist_sha256'], render_version=bravo['render_version'])
        self.assertEqual((code, body['status']), (409, 'artifact_mismatch'))
        self.refused(self.go(client_id=BRAVO), 'scope_is_exactly_one_approved_planned_scope')
        self.refused(self.go(site_id=rh.CLIENTS['bravo']['site_id'], list_id=rh.CLIENTS['bravo']['list_id']), 'scope_is_exactly_one_approved_planned_scope')
        key, planted = 'outbox:' + self.r.item('bravo', '2')['key'], deepcopy(self.entry())   # ALPHA's entry planted in BRAVO's outbox
        with self.r.store.transaction(key) as box:
            box['entries'][self.op] = planted
        code, body = self.go('bravo', '2', op=self.op, workbook_sha256=alpha['workbook_sha256'], checklist_sha256=alpha['checklist_sha256'],
                             render_version=alpha['render_version'])
        self.assertEqual((code, body['status']), (409, 'artifact_mismatch'))
        self.assertIn('draft_belongs_to_another_client_site_list_or_job', body['draft']['details'])
        self.assertEqual((self.r.lauren.calls, self.item_reads('alpha'), self.entry()['status']), (0, self.reads, 'pending'))

    def test_each_client_gets_its_own_draft(self):
        self.assertEqual((self.go()[1]['status'], self.go('bravo', '2')[1]['status']), ('sent', 'sent'))
        clients = [m['payload']['receipt']['binding']['client_id'] for m in self.r.lauren.messages]
        self.assertEqual(clients, [ALPHA, BRAVO])
        self.assertEqual([m['operation_id'] for m in self.r.lauren.messages], [self.op, self.bravo])


class Mutation(Dispatch):
    def test_stored_bytes_or_a_stored_receipt_changed_after_the_draft_was_built(self):
        publisher = self.r.router().for_scope(self.r.tenants['alpha'].assignment)
        other = self.r.store.put_blob(b'not the approved workbook')
        with self.r.store.transaction('artifact:' + publisher.key(self.op)) as record:
            original = record['files']['draft.xlsx']
            record['files']['draft.xlsx'] = other
        code, body = self.go()
        self.assertEqual((code, body['status'], body['draft']['details']), (409, 'artifact_mismatch', ['stored_artifact_unreadable_or_altered:ValueError']))
        with self.r.store.transaction('artifact:' + publisher.key(self.op)) as record:
            record['files']['draft.xlsx'] = original
        key = 'outbox:' + self.r.item('alpha', '1')['key']
        with self.r.store.transaction(key) as box:
            box['entries'][self.op]['receipt']['workbook_sha256'] = 'e' * 64
        code, body = self.go(workbook_sha256='e' * 64)                          # the call agrees with the altered receipt; the stored draft does not
        self.assertEqual((code, body['status'], body['draft']['details']), (409, 'artifact_mismatch', ['stored_artifact_is_not_the_named_one']))
        self.untouched()

    def test_the_production_door_refuses_a_caller_without_the_hosted_grant(self):
        """The old helper's bypass: a worker, a (fake) verified contract and a sender, brought straight to deliver."""
        for grant in (None, dict(id='made-up'), dict(id=''), 'x', dict(id=self.approved)):
            self.assertEqual(self.r.through_the_door_without_a_grant('alpha', '1', grant=grant), dict(status='dispatch_not_authorized'))
        self.untouched(); self.assertEqual(self.entry()['status'], 'pending')

    def test_a_grant_is_for_one_operation_one_contract_once_and_expires(self):
        item, now = self.r.item('alpha', '1'), self.r.clock()
        key = transport.grant_key(item['scope'], self.op)
        def plant(**change):
            with self.r.store.transaction(key) as record:
                record.clear()
                record.update(dict(dict(id='g1', operation_id=self.op, scope=item['key'], provider_contract=FAKE_CONTRACT.digest(),
                                        expires_at='2026-11-10T19:01:00+00:00'), **change))
        for change in (dict(operation_id='another'), dict(scope='another'), dict(provider_contract='another'), dict(used_at=now),
                       dict(expires_at=now), dict(expires_at='not a time'), dict(id='g2')):
            plant(**change)
            self.assertEqual(self.r.through_the_door_without_a_grant('alpha', '1', grant=dict(id='g1')), dict(status='dispatch_not_authorized'), change)
        self.untouched()
        # A grant written into the store by hand DOES open the door: the grant is a wiring boundary, not cryptography.
        plant()
        self.assertEqual(self.r.through_the_door_without_a_grant('alpha', '1', grant=dict(id='g1')), dict(status='sent'))
        self.assertEqual(self.r.through_the_door_without_a_grant('alpha', '1', grant=dict(id='g1')), dict(status='dispatch_not_authorized'))   # spent


class ReplayQuotaAndReceipts(Dispatch):
    def test_a_nonce_is_single_use_and_a_dispatch_id_names_one_intent(self):
        first = self.r.dispatch_request('alpha', '1')
        deps = self.r.dispatch_deps
        self.assertEqual(dispatch.handle('POST', rh.ALICE, first, deps())[1]['status'], 'sent')
        code, body = dispatch.handle('POST', rh.ALICE, first, deps())           # the identical call again
        self.assertEqual((code, body['status'], body['sources_read'], body['sender_calls']), (409, 'replayed_request', 0, 0))
        code, body = dispatch.handle('POST', rh.ALICE, dict(first, nonce='a-fresh-nonce-0000001'), deps())
        self.assertEqual((code, body['status'], body.get('replayed'), body['sources_read'], body['sender_calls']), (200, 'sent', True, 0, 0))
        code, body = dispatch.handle('POST', rh.ALICE, dict(first, nonce='a-fresh-nonce-0000002', recipient='Lauren', shift='2'), deps())
        self.assertIn(body['status'], ('dispatch_id_reused_for_another_intent', 'not_authorized'))
        self.assertEqual(self.item_reads('alpha'), self.reads + 1)
        self.at('19:40'); self.poll()
        reads = (self.item_reads('alpha'), self.item_reads('bravo'))
        other = self.r.dispatch_request('bravo', '2', dispatch_id=first['dispatch_id'])
        code, body = dispatch.handle('POST', rh.ALICE, other, deps())
        self.assertEqual((code, body['status'], body['sender_calls']), (409, 'dispatch_id_reused_for_another_intent', 0))
        self.assertEqual((len(self.r.lauren.messages), self.item_reads('alpha'), self.item_reads('bravo')), (1,) + reads)

    def test_the_dispatch_quota_is_taken_before_any_read(self):
        self.at('19:40'); self.poll()
        reads = self.item_reads('bravo')
        deps = lambda: self.r.dispatch_deps(dispatch_quota=Quota(1))
        self.assertEqual(self.go(deps=deps())[1]['status'], 'sent')
        code, body = self.go('bravo', '2', deps=deps())
        self.assertEqual((code, body['status'], body['sources_read'], body['sender_calls']), (429, 'quota_exceeded', 0, 0))
        self.assertEqual((self.item_reads('bravo'), len(self.r.lauren.messages)), (reads, 1))
        for bad in (Quota(0), Quota(True), Quota(1.5), 5):
            self.assertEqual(self.go('bravo', '2', deps=self.r.dispatch_deps(dispatch_quota=bad))[1]['status'], 'not_authorized')

    def test_one_callers_recorded_outcome_is_not_served_to_another(self):
        for record in self.r.approvals.records.values():
            record['principals'] = ['alice', 'bob']
        first = self.r.dispatch_request('alpha', '1')
        self.assertEqual(dispatch.handle('POST', rh.ALICE, first, self.r.dispatch_deps())[1]['status'], 'sent')
        code, body = dispatch.handle('POST', BOB, first, self.r.dispatch_deps())                  # same dispatch_id, same nonce, another caller
        self.assertEqual((code, body['status'], body['principal'], body['sources_read'], body['sender_calls']), (409, 'not_pending', 'bob', 0, 0))
        self.assertNotIn('outcome', body); self.assertNotIn('replayed', body)
        self.assertEqual(len(self.r.lauren.messages), 1)


class Concurrent(Dispatch):
    def test_concurrent_hosted_dispatches_call_the_sender_once(self):
        with ThreadPoolExecutor(6) as pool:
            out = list(pool.map(lambda i: self.go()[1]['status'], range(6)))
        self.assertEqual(out.count('sent'), 1, out)
        self.assertTrue(set(out) <= {'sent', 'not_pending', 'busy'}, out)
        self.assertEqual((self.r.lauren.calls, len(self.r.lauren.messages), len(self.entry()['attempts'])), (1, 1, 1))

    def test_two_callers_at_once_send_once(self):
        for record in self.r.approvals.records.values():
            record['principals'] = ['alice', 'bob']
        with ThreadPoolExecutor(4) as pool:
            out = list(pool.map(lambda i: self.go(headers=(rh.ALICE, BOB)[i % 2])[1]['status'], range(8)))
        self.assertEqual(out.count('sent'), 1, out)
        self.assertEqual((self.r.lauren.calls, len(self.entry()['attempts'])), (1, 1))


class RestartAndUncertain(Dispatch):
    def test_an_uncertain_delivery_is_never_retried(self):
        self.r.lauren.fail = TimeoutError('no answer from the fake provider')
        first = self.r.dispatch_request('alpha', '1')
        code, body = dispatch.handle('POST', rh.ALICE, first, self.r.dispatch_deps())
        self.assertEqual((code, body['status'], body['outcome']), (202, 'uncertain', dict(status='uncertain', delivery_status='uncertain')))
        self.r.lauren.fail = None                                               # the provider is healthy again
        for hhmm in ('19:01', '19:30', '21:00'):
            self.at(hhmm); self.r.restart(); self.poll()
            code, body = self.go()
            self.assertEqual((code, body['status'], body['sources_read'], body['sender_calls']), (409, 'not_pending', 0, 0), hhmm)
        code, body = dispatch.handle('POST', rh.ALICE, dict(first, timestamp=self.r.clock(), nonce='a-fresh-nonce-0000003'), self.r.dispatch_deps())
        self.assertEqual((code, body['status'], body['replayed'], body['sender_calls']), (202, 'uncertain', True, 0))
        entry = self.entry()
        self.assertEqual((self.r.lauren.calls, self.r.lauren.messages, entry['status'], entry['delivery_status'], len(entry['attempts'])),
                         (1, [], 'uncertain', 'uncertain', 1))

    def test_the_host_dies_while_the_message_is_in_flight(self):
        def dies(**kw):
            self.r.lauren.calls += 1
            raise Crash()
        first = self.r.dispatch_request('alpha', '1')
        with self.assertRaises(Crash):
            dispatch.handle('POST', rh.ALICE, first, self.r.dispatch_deps(sender=dies))
        self.r.restart()
        code, body = dispatch.handle('POST', rh.ALICE, dict(first, nonce='a-fresh-nonce-0000004'), self.r.dispatch_deps())
        self.assertEqual((code, body['status'], body['sources_read'], body['sender_calls']), (409, 'dispatch_in_progress_or_interrupted', 0, 0))
        for hhmm in ('19:02', '19:30'):
            self.at(hhmm); self.r.restart(); self.poll()
            self.assertEqual(self.go()[1]['status'], 'not_pending', hhmm)       # whether it arrived is unknown: never sent again
        entry = self.entry()
        self.assertEqual((self.r.lauren.calls, self.r.lauren.messages, entry['delivery_status'], len(entry['attempts'])), (1, [], 'uncertain', 1))

    def test_the_host_dies_before_the_claim_and_nothing_was_sent(self):
        def dies(scope, settings):
            raise Crash()
        first = self.r.dispatch_request('alpha', '1')
        with self.assertRaises(Crash):
            dispatch.handle('POST', rh.ALICE, first, self.r.dispatch_deps(fetch=dies))
        self.r.restart()
        self.assertEqual((self.r.lauren.calls, self.entry()['status']), (0, 'pending'))
        code, body = dispatch.handle('POST', rh.ALICE, dict(first, nonce='a-fresh-nonce-0000005'), self.r.dispatch_deps())
        self.assertEqual((code, body['status']), (409, 'dispatch_in_progress_or_interrupted'))     # that dispatch_id is never resumed
        self.at('19:07')                                                        # the dead worker's lease has run out
        self.assertEqual(self.go()[1]['status'], 'sent')                        # a NEW, fully re-authorized dispatch
        self.assertEqual((self.r.lauren.calls, len(self.entry()['attempts'])), (1, 1))

    def test_a_site_on_a_provider_wait_is_not_probed_read_or_sent(self):
        probe = rh.AdapterProbe(self.r.tenants, self.r.clock)
        self.r.tenants['alpha'].down = (503, None, {'Retry-After': HTTP_DATE})
        code, body = self.go(deps=self.r.dispatch_deps(schema_probe=probe))
        self.assertEqual((code, body['status'], body['outcome']['reason'], body['sender_calls']), (409, 'held', 'recapture_failed', 0))
        self.assertTrue(body['outcome']['cooldown_until'].startswith('2026-11-10T19:21:00'))
        calls, requests = probe.calls, len(self.r.tenants['alpha'].requests)
        for hhmm in ('19:05', '19:20'):
            self.at(hhmm)
            code, body = self.go(deps=self.r.dispatch_deps(schema_probe=probe))
            self.assertEqual((code, body['status'], body['reason'], body['sources_read']), (409, 'held', 'site_cooldown', 0), hhmm)
        self.assertEqual((probe.calls, len(self.r.tenants['alpha'].requests), self.r.lauren.calls), (calls, requests, 0))
        self.r.tenants['alpha'].down = None
        self.at('19:21')
        self.assertEqual(self.go(deps=self.r.dispatch_deps(schema_probe=probe))[1]['status'], 'sent')

    def test_a_late_row_seen_at_the_re_read_supersedes_the_draft_instead_of_sending_it(self):
        self.r.tenants['alpha'].scan('1', '2026-11-10T18:59:30+00:00')          # arrives after the draft was built
        code, body = self.go()
        self.assertEqual((code, body['sources_read'], body['sender_calls']), (409, 1, 0), body)
        self.assertIn(body['status'], ('held', 'superseded'))
        self.assertEqual(self.r.lauren.calls, 0)


class Boundaries(unittest.TestCase):
    def test_nothing_reaches_the_dispatch_module_and_it_owns_no_authority_or_sender(self):
        for path in list((ROOT / 'api').rglob('*.py')) + list((ROOT / 'hosting').glob('*.py')) + list((ROOT / 'scripts').glob('*.py')) + [ROOT / 'vercel.json']:
            if path.is_file() and path.name != 'last_scan_dispatch.py':
                text = path.read_text(errors='ignore')
                self.assertNotIn('last_scan_dispatch', text.replace('hosting.last_scan_dispatch)', ''), str(path))   # one docstring mention in the transport module
        text = (ROOT / 'hosting' / 'last_scan_dispatch.py').read_text()
        for forbidden in ('urllib', 'requests', 'http.client', 'smtplib', 'os.environ', 'getenv', 'psycopg', 'socket', 'subprocess', 'Bearer',
                          'mailer', 'graph.microsoft', 'dispatch_test'):
            self.assertNotIn(forbidden, text, forbidden)
        concrete = [n for n in dir(dispatch) if isinstance(getattr(dispatch, n), type)
                    and issubclass(getattr(dispatch, n), (hosted.Authorizer, hosted.ApprovalAuthority, hosted.CaptureAuthority, hosted.SchemaProbe))]
        self.assertEqual(concrete, [])
        self.assertEqual(dict(transport.VERIFIED_CONTRACTS), {})

    def test_only_the_dispatch_boundary_calls_the_door_and_only_the_door_calls_the_test_transport(self):
        for path in list((ROOT / 'api').rglob('*.py')) + list((ROOT / 'hosting').glob('*.py')) + list((ROOT / 'scripts').glob('*.py')):
            text = path.read_text(errors='ignore')
            if path.name != 'last_scan_dispatch.py':
                self.assertIsNone(re.search(r'transport\.deliver\(|grant_key\(', text) if path.name != 'last_scan_transport.py' else None, str(path))
            if path.name not in ('last_scan_transport.py', 'last_scan_service.py'):
                self.assertNotIn('dispatch_test', text, str(path))
        self.assertEqual(list(inspect.signature(transport.deliver).parameters), ['worker', 'scope', 'op', 'sender', 'settings', 'grant'])
        self.assertEqual(inspect.signature(transport.deliver).parameters['grant'].default, None)

    def test_no_exactly_once_or_production_ready_claim(self):
        text = (ROOT / 'hosting' / 'last_scan_dispatch.py').read_text().lower()
        self.assertIn('no exactly-once', text.replace('\n', ' '))
        self.assertIn('prepared, not connected', text)
        self.assertNotIn('production-ready', text); self.assertNotIn('production ready', text)


if __name__ == '__main__':
    unittest.main()
