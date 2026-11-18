"""Revocation and expiry under the authority record contract (hosting.last_scan_authority). FULLY SYNTHETIC. TEST ONLY.

Covers the contract itself, the hosted poll, the capture receipts and the hosted dispatch. Every authority, issuer,
record, clock and sender here is a fake from the test suite. No real authority exists and nothing here creates one.
"""
import unittest
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from unittest import mock

import authority_fixture as af
import rehearsal_harness as rh
from hosting import last_scan_authority as authority, last_scan_dispatch as dispatch, last_scan_hosted as hosted
from hosting.last_scan_local import StateStore
from hosting.last_scan_service import ScheduledWorker
from test_last_scan_hosted import COLLECTOR, FakeApprovals, FakeCaptures, FakeProbe, Hosted, NOW, assignment, schemas_for
from test_last_scan_hosted_capture_provenance import iso
from test_last_scan_hosted_dispatch import Dispatch

ROOT = Path(__file__).resolve().parents[1]
APPROVAL = 'approval_attested_for_this_snapshot_and_principal'
MAPPING = 'source_mapping_ready_for_every_assignment'
DISPATCH = 'dispatch_approved_for_this_snapshot_recipients_contract_and_principal'
ISSUERS = authority.freeze_issuers(af.ISSUERS)
AT = '2026-11-10T21:35:00+00:00'

# Every way a record can fail its lifecycle, shared by the contract, hosted and dispatch tests: name -> (change, reason).
# ``DROP`` removes the field. Each of these was, or would have been, read as "still valid" if unknown fields were ignored.
DROP = object()
COMMON = dict(
    arbitrary_revoked_flag=(dict(revoked=True), 'unknown_fields:revoked'),
    arbitrary_expires_field=(dict(expires='2000-01-01T00:00:00+00:00'), 'unknown_fields:expires'),
    arbitrary_valid_flag=(dict(valid=False), 'unknown_fields:valid'),
    arbitrary_note=(dict(note='superseded by another record'), 'unknown_fields:note'),
    revoked_at=(dict(revoked_at='2026-11-10T21:00:00+00:00'), 'revoked'),
    revoked_by=(dict(revoked_by='someone'), 'revoked'),
    revocation_reason=(dict(revocation_reason='withdrawn'), 'revoked'),
    revoked_at_false=(dict(revoked_at=False), 'revoked'),
    status_revoked=(dict(status='revoked'), 'revoked'),
    status_other=(dict(status='superseded'), 'status_not_active'),
    status_case=(dict(status='ACTIVE'), 'status_not_active'),
    status_true=(dict(status=True), 'status_not_active'),
    status_missing=(dict(status=DROP), 'missing_fields:status'),
    version_2=(dict(version=2), 'unsupported_version'),
    version_text=(dict(version='1'), 'unsupported_version'),
    version_float=(dict(version=1.0), 'unsupported_version'),
    version_true=(dict(version=True), 'unsupported_version'),
    version_missing=(dict(version=DROP), 'unsupported_version'),
    contract_missing=(dict(contract=DROP), 'unsupported_or_missing_contract'),
    contract_other=(dict(contract='some.other.contract'), 'unsupported_or_missing_contract'),
    record_id_missing=(dict(record_id=DROP), 'missing_fields:record_id'),
    record_id_blank=(dict(record_id=' '), 'record_id_missing'),
    record_id_number=(dict(record_id=7), 'record_id_missing'),
    issuer_missing=(dict(issuer=DROP), 'missing_fields:issuer'),
    issuer_unknown=(dict(issuer='another-authority'), 'issuer_not_accepted'),
    issuer_padded=(dict(issuer=' ' + af.APPROVAL_ISSUER), 'issuer_not_accepted'),
    issuer_list=(dict(issuer=[af.APPROVAL_ISSUER]), 'issuer_not_accepted'),
    expires_missing=(dict(expires_at=DROP), 'missing_fields:expires_at'),
    expires_null=(dict(expires_at=None), 'validity_missing_malformed_or_without_offset'),
    expires_naive=(dict(expires_at='2026-11-12T00:00:00'), 'validity_missing_malformed_or_without_offset'),
    expires_date_only=(dict(expires_at='2026-11-12'), 'validity_missing_malformed_or_without_offset'),
    expires_number=(dict(expires_at=1762905600), 'validity_missing_malformed_or_without_offset'),
    expires_garbage=(dict(expires_at='when it is over'), 'validity_missing_malformed_or_without_offset'),
    expired_long_ago=(dict(expires_at='2026-11-10T20:00:00+00:00'), 'expired'),
    expires_this_instant=(dict(expires_at=AT), 'expired'),
)
APPROVAL_ONLY = dict(
    not_before_missing=(dict(not_before=DROP), 'missing_fields:not_before'),
    not_before_naive=(dict(not_before='2026-11-10T00:00:00'), 'validity_missing_malformed_or_without_offset'),
    not_yet_valid=(dict(not_before='2026-11-10T21:35:01+00:00'), 'not_yet_valid'),
    approved_in_the_future=(dict(approved_at='2026-11-10T21:40:01+00:00'), 'issued_in_the_future'),
    approved_at_date_only=(dict(approved_at='2026-11-10'), 'validity_missing_malformed_or_without_offset'),
    valid_too_long=(dict(not_before='2026-11-10T00:00:00+00:00', expires_at='2026-11-17T00:00:01+00:00'), 'validity_window_empty_or_longer_than_allowed'),
    window_empty=(dict(not_before='2026-11-12T00:00:00+00:00', expires_at='2026-11-12T00:00:00+00:00'), 'validity_window_empty_or_longer_than_allowed'),
)


def changed(record, change):
    out = dict(record)
    for k, v in change.items():
        if v is DROP:
            out.pop(k, None)
        else:
            out[k] = v
    return out


def good_receipt(**change):
    return af.receipt(dict(snapshot_identity='s' * 64, site_id='site', list_id='list', schema_digest='d' * 64, collector=COLLECTOR,
                           captured_at='2026-11-10T21:00:00+00:00'), **change)


class Contract(unittest.TestCase):
    def test_a_well_formed_active_record_counts_and_nothing_else_does(self):
        self.assertEqual(authority.problems(af.approval('x' * 64), 'approval', ISSUERS, AT), [])
        self.assertEqual(authority.problems(good_receipt(), 'capture', ISSUERS, AT), [])
        for name, (change, reason) in dict(COMMON, **APPROVAL_ONLY).items():
            with self.subTest('approval ' + name):
                self.assertIn(reason, authority.problems(changed(af.approval('x' * 64), change), 'approval', ISSUERS, AT))
        for name, (change, reason) in COMMON.items():
            with self.subTest('receipt ' + name):
                change = dict(change, issuer=' ' + af.CAPTURE_ISSUER) if name == 'issuer_padded' else change
                self.assertIn(reason, authority.problems(changed(good_receipt(), change), 'capture', ISSUERS, AT))
        for shape in (None, True, 'approved', [af.approval('x')], 5):
            self.assertEqual(authority.problems(shape, 'approval', ISSUERS, AT), ['not_a_record'])

    def test_qa_repro_arbitrary_revoked_or_expires_fields_are_refused_not_ignored(self):
        for extra in (dict(revoked=True), dict(expires='2000-01-01T00:00:00+00:00'), dict(is_revoked=True), dict(active=False), dict(deleted=True)):
            for record, kind in ((af.approval('x' * 64), 'approval'), (good_receipt(), 'capture')):
                reasons = authority.problems(dict(record, **extra), kind, ISSUERS, AT)
                self.assertEqual(reasons, ['unknown_fields:' + next(iter(extra))], (kind, extra))

    def test_one_kind_is_not_the_other_and_issuers_are_per_kind(self):
        self.assertIn('unsupported_or_missing_contract', authority.problems(good_receipt(), 'approval', ISSUERS, AT))
        self.assertIn('unsupported_or_missing_contract', authority.problems(af.approval('x'), 'capture', ISSUERS, AT))
        self.assertIn('issuer_not_accepted', authority.problems(af.approval('x', issuer=af.CAPTURE_ISSUER), 'approval', ISSUERS, AT))
        self.assertIn('issuer_not_accepted', authority.problems(good_receipt(issuer=af.APPROVAL_ISSUER), 'capture', ISSUERS, AT))
        for none in (None, (), authority.freeze_issuers(dict(approval=('a',)))):
            self.assertIn('issuer_not_accepted', authority.problems(af.approval('x'), 'approval', none, AT))

    def test_validity_is_judged_on_the_supplied_clock_to_the_second(self):
        record = af.approval('x', not_before='2026-11-10T21:00:00+00:00', expires_at='2026-11-10T22:00:00+00:00')
        say = lambda at, margin=0: authority.problems(record, 'approval', ISSUERS, at, margin)
        self.assertEqual(say('2026-11-10T20:59:59+00:00'), ['not_yet_valid'])
        self.assertEqual(say('2026-11-10T21:00:00+00:00'), [])
        self.assertEqual(say('2026-11-10T21:59:59+00:00'), [])
        self.assertEqual(say('2026-11-10T22:00:00+00:00'), ['expired'])           # the end instant itself is already too late
        self.assertEqual(say('2026-11-10T16:00:00-06:00'), ['expired'])           # the same instant in another offset
        self.assertEqual(say('2026-11-10T21:59:00+00:00', 60), ['expires_too_soon_to_act_on'])
        self.assertEqual(say('2026-11-10T21:58:59+00:00', 60), [])
        for clock in ('2026-11-10T21:30:00', None, 'now', 5):                     # a host clock without an offset decides nothing
            self.assertEqual(say(clock), ['validity_missing_malformed_or_without_offset'])
        receipt = good_receipt()
        self.assertEqual(authority.problems(receipt, 'capture', ISSUERS, '2026-11-11T20:59:59+00:00'), [])
        self.assertEqual(authority.problems(receipt, 'capture', ISSUERS, '2026-11-11T21:00:00+00:00'), ['expired'])
        self.assertIn('validity_window_empty_or_longer_than_allowed',
                      authority.problems(good_receipt(expires_at='2026-11-11T21:00:01+00:00'), 'capture', ISSUERS, AT))

    def test_identity_covers_the_whole_record_and_a_renewal_is_another_record(self):
        record = af.approval('x')
        same = dict(record, expires_at='2026-11-12T18:00:00-06:00', not_before='2026-11-10T01:00:00+01:00')
        self.assertEqual(authority.identity(record, 'approval'), authority.identity(same, 'approval'))
        for change in (dict(record_id='renewed-1'), dict(expires_at='2026-11-13T00:00:01+00:00'), dict(not_before='2026-11-10T00:00:01+00:00'),
                       dict(issuer='another'), dict(principals=['alice', 'bob']), dict(approved_by='someone else'), dict(status='revoked')):
            self.assertNotEqual(authority.identity(record, 'approval'), authority.identity(dict(record, **change), 'approval'), change)

    def test_the_issuer_policy_is_read_once_and_strictly(self):
        self.assertEqual(ISSUERS, (('approval', (af.APPROVAL_ISSUER,)), ('capture', (af.CAPTURE_ISSUER,))))
        self.assertEqual(authority.freeze_issuers(ISSUERS), ISSUERS)
        for bad in (None, {}, dict(approval=('a',)), dict(approval=('a',), capture=()), dict(approval=('a',), capture=(' b',)),
                    dict(approval=('a',), capture=('b',), other=('c',)), dict(approval='a', capture=('b',))[0:0] if False else dict(approval=(5,), capture=('b',)),
                    'issuers', [('approval', ('a',))], dict(approval=None, capture=('b',))):
            self.assertIsNone(authority.freeze_issuers(bad), bad)

    def test_the_contract_module_is_a_contract_only(self):
        text = (ROOT / 'hosting' / 'last_scan_authority.py').read_text()
        for forbidden in ('urllib', 'requests', 'http.client', 'smtplib', 'os.environ', 'getenv', 'psycopg', 'socket', 'subprocess', 'Bearer', 'jwt', 'time.time', 'sf.now'):
            self.assertNotIn(forbidden, text, forbidden)
        self.assertNotIn('class ', text)                                         # no authority is implemented here


class HostedPoll(Hosted):
    """The hosted run: the approval and the capture receipt must meet the contract before a run and before every read."""
    def setUp(self):
        super().setUp()
        self.digest = next(iter(self.approvals.records))
        self.record = self.approvals.records[self.digest]

    def denied(self, deps, *checks):
        code, body = self.go(deps)
        self.assertEqual((code, body['status'], body['sources_read']), (409, 'not_activated', 0), body.get('preflight'))
        for name in checks:
            self.assertIn(name, self.failed(body))
        self.untouched()
        return body

    def test_every_approval_lifecycle_failure_denies_the_run(self):
        self.assertEqual(self.go()[0], 200)
        self.reads.clear()
        for name, (change, reason) in dict(COMMON, **APPROVAL_ONLY).items():
            with self.subTest(name):
                self.store = StateStore(Path(self.folder.name) / (name + '.db'))
                self.path = Path(self.folder.name) / (name + '.db')
                self.approvals.records[self.digest] = changed(self.record, change)
                body = self.denied(self.deps(), APPROVAL)
                self.assertEqual(self.failed(body), [APPROVAL])
                self.assertIn(reason, next(c['detail'] for c in body['preflight'] if c['check'] == APPROVAL))

    def test_every_receipt_lifecycle_failure_denies_the_run(self):
        schema = schemas_for(self.config)[0]
        for name, (change, reason) in COMMON.items():
            with self.subTest(name):
                change = dict(change, issuer=' ' + af.CAPTURE_ISSUER) if name == 'issuer_padded' else change
                captures = FakeCaptures([schema])
                key = next(iter(captures.records))
                captures.records[key] = changed(captures.records[key], change)
                body = self.denied(self.deps(captures=captures), MAPPING)
                self.assertIn('capture_receipt_contract:' + reason, body['mapping_readiness'][0]['blocked_reasons'])

    def test_expiry_is_on_the_host_clock_not_on_anything_the_record_or_the_caller_says(self):
        self.approvals.records[self.digest] = dict(self.record, expires_at=iso(timedelta(minutes=10)))
        digest = self.digest
        self.now = iso(timedelta(minutes=9, seconds=59))
        self.assertEqual(self.go()[0], 200)
        reads = list(self.reads)
        self.now = iso(timedelta(minutes=10))
        code, body = self.go()
        self.assertEqual((code, self.failed(body), self.reads), (409, [APPROVAL], reads))
        code, body = self.call(body=self.body(mode='run', timestamp=iso(timedelta(minutes=9))))   # a caller's earlier timestamp changes nothing
        self.assertEqual((code, self.failed(body), self.reads), (409, [APPROVAL], reads))
        self.assertEqual(body['snapshot_digest'], digest)

    def scripted(self, answers):
        """A FAKE approval authority answering in turn (the last answer repeats)."""
        test = self
        class Scripted(FakeApprovals):
            def __init__(s): s.asked = 0
            def attestation(s, digest):
                s.asked += 1
                answer = answers[min(s.asked, len(answers)) - 1]
                return deepcopy(answer) if digest == test.digest else None
        return Scripted()

    def stopped(self, approvals=None, captures=None, reason=''):
        deps = self.deps(quota=hosted.Quota(500), **{k: v for k, v in dict(approvals=approvals, captures=captures).items() if v is not None})
        code, body = self.go(deps)
        self.assertEqual((code, body['status'], self.reads), (200, 'ran', []), body)             # authorized at the start, and then nothing was read
        results = body['summary']['results']
        self.assertTrue(results and all((r['status'], r['error_type']) == ('error', 'AuthorityWithdrawn') for r in results), results)
        self.assertEqual(self.rows()[1], 0)                                      # no draft, no blob
        return body

    def test_an_approval_revoked_expired_or_renewed_after_the_run_started_stops_every_read(self):
        for name, later in dict(revoked=dict(self.record, status='revoked'), revoked_at=dict(self.record, revoked_at=NOW),
                                arbitrary_flag=dict(self.record, revoked=True), gone=None,
                                renewed=dict(self.record, record_id='renewed-approval-1'),
                                extended=dict(self.record, expires_at='2026-11-13T00:00:01+00:00'),
                                other_caller=dict(self.record, principals=['bob'])).items():
            with self.subTest(name):
                self.stopped(approvals=self.scripted((self.record, later)))

    def test_a_receipt_revoked_or_renewed_after_the_run_started_stops_every_read(self):
        schema = schemas_for(self.config)[0]
        for name, change in dict(revoked=dict(status='revoked'), arbitrary_flag=dict(revoked=True), renewed=dict(record_id='renewed-receipt-1'),
                                 shortened=dict(expires_at='2026-11-11T20:00:00+00:00'), other_collector=dict(collector='second-collector'),
                                 other_issuer=dict(issuer='another-authority'), gone=None).items():
            with self.subTest(name):
                class Later(FakeCaptures):
                    def receipt(s, identity):
                        s.asked = getattr(s, 'asked', 0) + 1
                        if s.asked == 1:
                            return super().receipt(identity)
                        return None if change is None else dict(super().receipt(identity), **change)
                self.stopped(captures=Later([schema]))

    def test_expiry_during_the_run_is_seen_before_the_next_read(self):
        self.approvals.records[self.digest] = dict(self.record, expires_at=iso(timedelta(seconds=30)))
        class Slow(FakeProbe):
            def current(s, site_id, list_id):
                answer = super().current(site_id, list_id)
                self.now = iso(timedelta(seconds=31))                            # the run-start probe takes 31 seconds on the host clock
                return answer
        code, body = self.go(self.deps(schema_probe=Slow(schemas_for(self.config), lambda: self.now)))
        self.assertEqual((code, body['status'], self.reads), (200, 'ran', []))
        self.assertEqual({r['error_type'] for r in body['summary']['results']}, {'AuthorityWithdrawn'})

    def test_the_host_clock_going_backwards_is_refused(self):
        self.assertEqual(self.go()[0], 200)
        reads = list(self.reads)
        self.store = StateStore(self.path)                                       # the host restarts: the last seen time is in the store
        self.now = iso(-timedelta(seconds=300))
        self.assertEqual(self.go()[0], 200)                                      # within the allowed skew
        reads = list(self.reads)
        for back in (301, 900, 2000):
            self.now = iso(-timedelta(seconds=back))
            code, body = self.go()
            self.assertEqual((code, self.failed(body), self.reads), (409, ['host_clock_not_behind_the_last_seen_time'], reads), back)
        self.now = iso(-timedelta(days=30))                                      # far back: other checks fail too, and this one still does
        code, body = self.go()
        self.assertEqual((code, self.reads), (409, reads)); self.assertIn('host_clock_not_behind_the_last_seen_time', self.failed(body))
        self.now = iso(-timedelta(seconds=301))
        code, body = self.call()                                                 # a dry run reports it too, and writes nothing
        self.assertEqual(self.failed(body), ['host_clock_not_behind_the_last_seen_time'])
        self.now = NOW
        self.assertEqual(self.go()[0], 200)

    def test_the_host_clock_going_backwards_during_a_run_stops_the_reads(self):
        class Rewinds(FakeProbe):
            def current(s, site_id, list_id):
                answer = super().current(site_id, list_id)
                self.now = iso(-timedelta(seconds=1))
                return answer
        code, body = self.go(self.deps(schema_probe=Rewinds(schemas_for(self.config), lambda: self.now)))
        self.assertEqual((code, body['status'], self.reads), (200, 'ran', []))
        self.assertEqual({r['error_type'] for r in body['summary']['results']}, {'AuthorityWithdrawn'})

    def test_accepted_issuers_are_server_side_frozen_and_part_of_what_is_approved(self):
        for bad in (None, {}, dict(approval=(af.APPROVAL_ISSUER,)), 'issuers', dict(approval=(af.APPROVAL_ISSUER,), capture=(' x',))):
            self.denied(self.deps(issuers=bad), 'accepted_authority_issuers_configured')
        self.denied(self.deps(issuers=dict(approval=('another',), capture=(af.CAPTURE_ISSUER,))), APPROVAL)
        self.denied(self.deps(issuers=dict(approval=(af.APPROVAL_ISSUER,), capture=('another',))), MAPPING)
        wider = dict(approval=(af.APPROVAL_ISSUER, 'second-approver'), capture=(af.CAPTURE_ISSUER,))
        body = self.denied(self.deps(issuers=wider), APPROVAL)                   # a wider policy is another snapshot: not approved
        self.assertNotEqual(body['snapshot_digest'], self.digest)
        self.assertEqual(self.call(body=self.body(issuers=af.ISSUERS))[0], 400)  # never from the call
        host = dict(approval=['another'], capture=[af.CAPTURE_ISSUER])
        class Meddler(FakeApprovals):
            def attestation(s, digest):
                host['approval'] = [af.APPROVAL_ISSUER]                          # fixed too late
                return deepcopy(self.approvals.records.get(digest))
        self.denied(self.deps(issuers=host, approvals=Meddler()), APPROVAL)

    def test_an_approval_written_into_the_configuration_is_not_an_approval(self):
        bare = dict(enabled=True, assignments=[assignment(recorded_shifts=['1', '2'])])
        digest = self.call(self.deps(config=bare))[1]['snapshot_digest']
        for where in ('activation', 'approval', 'attestation'):
            code, body = self.go(self.deps(config=dict(bare, **{where: af.approval(digest)})))
            self.assertEqual(code, 409); self.assertIn(APPROVAL, self.failed(body))
        self.untouched()

    def test_alternating_records_never_add_up_and_one_record_is_read_per_check(self):
        revoked_right_caller = dict(self.record, status='revoked')
        active_wrong_caller = dict(self.record, principals=['bob'])
        expired_right_caller = dict(self.record, expires_at=iso(-timedelta(seconds=1)))
        for pair in ((revoked_right_caller, active_wrong_caller), (expired_right_caller, active_wrong_caller),
                     (dict(self.record, issuer='another'), dict(self.record, snapshot_digest='0' * 64))):
            for answers in (pair, pair[::-1]):
                test = self
                class Alternates(FakeApprovals):
                    def __init__(s): s.asked = 0
                    def attestation(s, digest):
                        s.asked += 1
                        return deepcopy(answers[(s.asked - 1) % 2]) if digest == test.digest else None
                split = Alternates()
                self.denied(self.deps(approvals=split), APPROVAL)
                self.assertEqual(split.asked, 1)

    def test_revocation_holds_across_restarts_and_concurrent_callers(self):
        self.approvals.records[self.digest] = dict(self.record, status='revoked', revoked_at=NOW, revoked_by='synthetic approver')
        for _ in range(3):
            self.store = StateStore(self.path)
            self.denied(self.deps(), APPROVAL)
        with ThreadPoolExecutor(6) as pool:
            out = list(pool.map(lambda i: self.go()[0], range(6)))
        self.assertEqual(out, [409] * 6); self.untouched()
        self.approvals.records[self.digest] = af.approval(self.digest)           # a NEW record from the fake approver
        self.assertEqual(self.go()[0], 200)


class DispatchLifecycle(Dispatch):
    def setUp(self):
        super().setUp()
        self.records = self.r.approvals.records
        self.run_key = next(k for k in self.records if k != self.approved)
        self.kept = deepcopy(self.records)

    def restore(self):
        self.records.clear(); self.records.update(deepcopy(self.kept))

    def detail(self, body, name):
        return next(c['detail'] for c in body['checks'] if c['check'] == name)

    def test_every_dispatch_approval_lifecycle_failure_denies(self):
        for name, (change, reason) in dict(COMMON, **APPROVAL_ONLY).items():
            with self.subTest(name):
                change = dict(change, expires_at='2026-11-10T19:00:00+00:00') if name == 'expires_this_instant' else change
                change = dict(change, not_before='2026-11-10T19:00:01+00:00') if name == 'not_yet_valid' else change
                change = dict(change, approved_at='2026-11-10T19:05:01+00:00') if name == 'approved_in_the_future' else change
                reason = 'expired' if name == 'expired_long_ago' else reason
                change = dict(change, expires_at='2026-11-10T18:00:00+00:00') if name == 'expired_long_ago' else change
                self.records[self.approved] = changed(self.kept[self.approved], change)
                body = self.refused(self.go(), DISPATCH)
                self.assertEqual(self.failed(body), [DISPATCH])
                self.assertIn(reason, self.detail(body, DISPATCH))
                self.restore()
        self.assertEqual(self.go()[1]['status'], 'sent')

    def test_qa_repro_an_arbitrary_revoked_flag_on_either_approval_or_on_the_receipt_denies(self):
        for key, check in ((self.approved, DISPATCH), (self.run_key, 'approval_attested_for_this_snapshot_and_principal')):
            self.records[key] = dict(self.kept[key], revoked=True)
            self.assertIn('unknown_fields:revoked', self.detail(self.refused(self.go(), check), check))
            self.restore()
        captures = self.r.captures.records
        kept = deepcopy(captures)
        for identity in list(captures):
            captures[identity] = dict(captures[identity], revoked=True)
        self.refused(self.go(), 'source_mapping_ready_for_every_assignment')
        captures.clear(); captures.update(kept)
        self.assertEqual(self.go()[1]['status'], 'sent')

    def test_an_authorization_about_to_expire_is_refused_up_front(self):
        soon = (self.r.clock.at + timedelta(seconds=dispatch.GRANT_SECONDS)).isoformat()
        later = (self.r.clock.at + timedelta(seconds=dispatch.GRANT_SECONDS + 1)).isoformat()
        self.records[self.approved] = dict(self.kept[self.approved], expires_at=soon)
        self.assertIn('expires_too_soon_to_act_on', self.detail(self.refused(self.go(), DISPATCH), DISPATCH))
        self.records[self.approved] = dict(self.kept[self.approved], expires_at=later)
        self.assertEqual(self.go(mode='dry_run')[1]['authorized'], True)
        self.restore()
        # the run approval: renewed with a short life, then covered by a new dispatch approval, is still too short to send under
        self.records[self.run_key] = dict(self.kept[self.run_key], expires_at=soon)
        self.r.approve_dispatch()
        body = self.refused(self.go(), 'authorizations_stay_valid_long_enough_to_send')
        self.assertEqual(self.failed(body), ['authorizations_stay_valid_long_enough_to_send'])
        self.restore()
        self.at('2026-11-11T18:29:30+00:00')                                    # 30 seconds before the capture receipts run out
        body = self.go()[1]
        self.assertEqual((body['status'], body['sender_calls']), ('not_authorized', 0))
        self.assertIn('authorizations_stay_valid_long_enough_to_send', self.failed(body))

    def test_a_renewed_run_approval_needs_a_new_dispatch_approval(self):
        for change in (dict(record_id='renewed-run-approval'), dict(expires_at='2026-11-12T12:00:00+00:00'), dict(not_before='2026-11-10T01:00:00+00:00')):
            self.records[self.run_key] = dict(self.kept[self.run_key], **change)        # valid, same snapshot, but another record
            body = self.refused(self.go(), DISPATCH)
            self.assertEqual(self.failed(body), [DISPATCH])
            self.restore()
        self.records[self.run_key] = dict(self.kept[self.run_key], record_id='renewed-run-approval')
        fresh = self.r.approve_dispatch()                                        # explicit: the fake approver authorizes dispatch under the renewal
        self.assertNotEqual(fresh, self.approved)
        self.assertEqual(self.go()[1]['status'], 'sent')

    def test_a_renewed_receipt_needs_new_approvals(self):
        captures = self.r.captures.records
        identity = next(k for k, v in captures.items() if v['list_id'] == rh.CLIENTS['alpha']['list_id'])
        for change in (dict(record_id='renewed-receipt'), dict(expires_at='2026-11-11T18:00:00+00:00')):
            kept = deepcopy(captures)
            captures[identity].update(change)
            self.refused(self.go(), 'approval_attested_for_this_snapshot_and_principal')
            captures.clear(); captures.update(kept)

    def during_the_read(self, act):
        def fetch(scope, settings):
            act()
            return self.r.fetch(scope, settings)
        return self.go(deps=self.r.dispatch_deps(fetch=fetch))

    def test_revocation_expiry_or_renewal_during_the_read_stops_the_claim(self):
        cases = dict(
            dispatch_revoked=(lambda: self.records[self.approved].update(status='revoked'), 'dispatch_approval_withdrawn'),
            dispatch_arbitrary_flag=(lambda: self.records[self.approved].update(revoked=True), 'dispatch_approval_withdrawn'),
            dispatch_renewed=(lambda: self.records[self.approved].update(record_id='renewed-dispatch'), 'dispatch_approval_is_another_record_than_the_one_this_call_was_authorized_under'),
            dispatch_extended=(lambda: self.records[self.approved].update(expires_at='2026-11-13T00:00:01+00:00'), 'dispatch_approval_is_another_record_than_the_one_this_call_was_authorized_under'),
            run_revoked=(lambda: self.records[self.run_key].update(revoked_at=self.r.clock()), 'run_approval_withdrawn'),
            run_renewed=(lambda: self.records[self.run_key].update(record_id='renewed-run'), 'run_approval_is_another_record_than_the_one_this_call_was_authorized_under'),
            run_gone=(lambda: self.records.pop(self.run_key), 'run_approval_withdrawn'))
        for name, (act, refusal) in cases.items():
            with self.subTest(name):
                code, body = self.during_the_read(act)
                self.assertEqual((code, body['status'], body['refusal'], body['sources_read'], body['sender_calls']), (409, 'held', refusal, 1, 0), body.get('refusal_detail'))
                self.assertEqual(self.r.lauren.calls, 0)
                self.restore()
        self.assertEqual(self.go()[1]['status'], 'sent')

    def test_expiry_during_the_read_and_a_read_that_takes_too_long(self):
        self.records[self.approved] = dict(self.kept[self.approved], expires_at=(self.r.clock.at + timedelta(seconds=100)).isoformat())
        code, body = self.during_the_read(lambda: self.r.clock.advance(seconds=100))     # the approval runs out while the source is read
        self.assertEqual((code, body['status'], body['refusal'], body['sender_calls']), (409, 'held', 'dispatch_approval_withdrawn', 0))
        self.assertIn('expired', body['refusal_detail'])
        self.restore()
        code, body = self.during_the_read(lambda: self.r.clock.advance(seconds=dispatch.GRANT_SECONDS + 1))
        self.assertEqual((code, body['status'], body['refusal'], body['sender_calls']), (409, 'held', 'dispatch_took_too_long', 0))
        self.assertEqual(self.r.lauren.calls, 0)

    def test_the_host_clock_going_backwards(self):
        def rewind():
            self.r.clock.at -= timedelta(seconds=1)
        code, body = self.during_the_read(rewind)                               # during the call: any step back at all
        self.assertEqual((code, body['status'], body['refusal'], body['sender_calls']), (409, 'held', 'host_clock_went_backwards_during_the_call', 0))
        reads = self.item_reads('alpha')
        self.r.clock.at -= timedelta(seconds=301)                               # between calls: beyond the allowed skew since the store last saw it
        for _ in range(2):
            code, body = self.go()
            self.assertEqual((code, body['status'], self.failed(body), body['sender_calls']), (409, 'not_authorized', ['host_clock_not_behind_the_last_seen_time'], 0))
            self.r.restart()                                                    # the last seen time is in the store, not in memory
        self.assertEqual((self.item_reads('alpha'), self.r.lauren.calls), (reads, 0))
        self.r.clock.at += timedelta(seconds=302)
        self.assertEqual(self.go()[1]['status'], 'sent')

    def test_validity_running_out_between_the_claim_and_the_send_does_not_call_the_sender(self):
        self.records[self.approved] = dict(self.kept[self.approved], expires_at=(self.r.clock.at + timedelta(seconds=90)).isoformat())
        gate = ScheduledWorker._gate
        def late(*a, **kw):                                                     # inside the claim, after the last authority check
            self.r.clock.advance(seconds=90)
            return gate(*a, **kw)
        with mock.patch.object(ScheduledWorker, '_gate', staticmethod(late)):
            code, body = self.go()
        self.assertEqual((code, body['status'], body['refusal'], body['sender_calls']), (202, 'uncertain', 'validity_passed_or_clock_went_back_before_the_send', 0), body)
        self.assertEqual(self.r.lauren.calls, 0)                                # the fake sender was never called
        attempt = self.entry()['attempts'][0]
        self.assertEqual((attempt['state'], attempt['error_type']), ('returned_error', 'Revoked'))
        self.restore()
        self.assertEqual(self.go()[1]['status'], 'not_pending')                 # recorded as an attempt; never tried again here

    def test_alternating_lifecycle_records_never_add_up(self):
        good = self.kept[self.approved]
        pairs = ((dict(good, status='revoked'), dict(good, principals=['bob'])),
                 (dict(good, expires_at='2026-11-10T18:00:00+00:00'), dict(good, action='not_dispatch')),
                 (dict(good, issuer='another'), dict(good, revoked=True)),
                 (dict(good, version=2), dict(good, record_id=' ')))
        for pair in pairs:
            for answers in (pair, pair[::-1]):
                approved, records = self.approved, self.records
                class Alternates(FakeApprovals):
                    def __init__(s): s.records, s.asked = records, 0
                    def attestation(s, digest):
                        if digest != approved:
                            return deepcopy(records.get(digest))
                        s.asked += 1
                        return deepcopy(answers[(s.asked - 1) % 2])
                split = Alternates()
                body = self.refused(self.go(deps=self.r.dispatch_deps(approvals=split)), DISPATCH)
                self.assertEqual((self.failed(body), split.asked), ([DISPATCH], 1))

    def test_revocation_holds_across_restarts_and_concurrent_callers(self):
        self.records[self.approved] = dict(self.kept[self.approved], status='revoked', revoked_at=self.r.clock(), revoked_by='rehearsal fake approver')
        for _ in range(2):
            self.r.restart()
            self.refused(self.go(), DISPATCH)
        with ThreadPoolExecutor(6) as pool:
            out = list(pool.map(lambda i: self.go()[1]['status'], range(6)))
        self.assertEqual((out, self.r.lauren.calls), (['not_authorized'] * 6, 0))
        self.restore()
        with ThreadPoolExecutor(6) as pool:
            out = list(pool.map(lambda i: self.go()[1]['status'], range(6)))
        self.assertEqual((out.count('sent'), self.r.lauren.calls), (1, 1), out)

    def test_issuers_are_required_frozen_and_per_kind(self):
        self.refused(self.go(deps=self.r.dispatch_deps(issuers=None)), 'accepted_authority_issuers_configured')
        self.refused(self.go(deps=self.r.dispatch_deps(issuers=dict(approval=('another',), capture=(af.CAPTURE_ISSUER,)))))
        self.records[self.approved] = dict(self.kept[self.approved], issuer=af.CAPTURE_ISSUER)   # a capture authority cannot approve
        self.assertIn('issuer_not_accepted', self.detail(self.refused(self.go(), DISPATCH), DISPATCH))


class RehearsalPoll(Dispatch):
    def test_the_hourly_poll_stops_when_its_approval_is_revoked_and_resumes_only_on_a_new_record(self):
        run = next(k for k in self.r.approvals.records if k != self.approved)
        kept = deepcopy(self.r.approvals.records[run])
        reads = (self.item_reads('alpha'), self.item_reads('bravo'))
        self.r.approvals.records[run].update(status='revoked', revoked_at=self.r.clock(), revoked_by='rehearsal fake approver')
        for hhmm in ('19:05', '19:40'):
            self.at(hhmm); self.r.restart()
            self.assertEqual(self.r.poll(), {})
            self.assertEqual((self.r.last_response[0], self.r.last_response[1]['status']), (409, 'not_activated'))
        self.assertEqual(self.go()[1]['status'], 'not_authorized')              # nothing may be dispatched under a revoked run approval either
        self.assertEqual((self.item_reads('alpha'), self.item_reads('bravo'), self.r.lauren.calls), reads + (0,))
        self.r.approvals.records[run] = dict(kept, record_id='reissued-run-approval')
        self.assertTrue(self.r.poll())


if __name__ == '__main__':
    unittest.main()
