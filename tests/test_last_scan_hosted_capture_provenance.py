"""Schema-capture provenance, bounded capture age, and live schema drift checks on the hosted entry point.
FAKE capture authority, FAKE probe, synthetic snapshots and synthetic source only. Nothing here is a collector, an
identity system, origin proof, a route or a live read."""
import unittest
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest import mock
import readiness_fixture as rf
from hosting import last_scan_hosted as hosted, last_scan_readiness as readiness
from hosting.last_scan_hosted import ApprovalAuthority, Authorizer, CaptureAuthority, Deps, Quota, SchemaDrift, SchemaProbe
from test_last_scan_hosted import (ALICE, COLLECTOR, ENTRY, FakeApprovals, FakeCaptures, FakeProbe, Hosted, NOW, ROOT, SCOPE,
                                   assignment, bare_assignment, schemas_for)

MAPPING, APPROVAL = 'source_mapping_ready_for_every_assignment', 'approval_attested_for_this_snapshot_and_principal'
T0 = datetime(2026, 11, 10, 21, 35, tzinfo=timezone.utc)          # NOW


def iso(delta=timedelta(0)):
    return (T0 + delta).isoformat()


class ApprovesAnything(ApprovalAuthority):
    """TEST ONLY: an approval authority that says yes to every digest, to show the other gates do not lean on approval."""
    def attestation(self, digest):
        return dict(snapshot_digest=digest, approved_by='synthetic approver', approved_at='2026-11-10', principals=['alice', 'bob'])


class Base(Hosted):
    def setUp(self):
        super().setUp()
        self.schema = schemas_for(self.config)[0]

    def at(self, captured_at, receipt=...):
        """deps whose one snapshot says captured_at, with a receipt saying the same unless told otherwise."""
        schema = deepcopy(self.schema)
        if captured_at is None:
            schema.pop('captured_at', None)
        else:
            schema['captured_at'] = captured_at
        captures = FakeCaptures([schema])
        if receipt is not ...:
            for record in captures.records.values():
                if receipt is None: record.pop('captured_at', None)
                else: record['captured_at'] = receipt
        return self.deps(schemas=[schema], captures=captures, approvals=ApprovesAnything())

    def refused(self, deps, reason=None):
        before = list(self.reads)
        with mock.patch.object(hosted.runner, 'run_once', side_effect=AssertionError('runner reached')) as run:
            code, body = self.go(deps)
        self.assertEqual((code, body['status'], body['sources_read'], run.call_count), (409, 'not_activated', 0, 0))
        self.assertEqual((self.reads, body['snapshot_digest']), (before, None))    # nothing read by this request
        self.assertIn(MAPPING, self.failed(body))
        if reason:
            self.assertIn(reason, body['mapping_readiness'][0]['blocked_reasons'])
        self.assertIsNone(body['mapping_readiness'][0]['capture'])
        return body

    def ran(self, deps):
        code, body = self.go(deps)
        self.assertEqual((code, body['status']), (200, 'ran'), body.get('preflight'))
        return body


class CaptureAge(Base):
    def test_qa_repro_ancient_future_malformed_and_missing_capture_times_do_not_run_even_when_approved(self):
        for label, value, reason in (('year 2000', '2000-01-01T00:00:00+00:00', 'capture_too_old'),
                                     ('year 2099', '2099-01-01T00:00:00+00:00', 'capture_time_in_the_future'),
                                     ('not a date', 'not a date', 'capture_time_missing_or_malformed'),
                                     ('missing', None, 'capture_time_missing_or_malformed')):
            self.refused(self.at(value), reason)
        self.assertEqual(self.rows(), (0, 0))                                    # no nonce, quota, state or draft was written

    def test_other_malformed_capture_times(self):
        for value in ('', ' ', '2026-11-10', '2026-11-10T21:00:00', '21:00', 1762808400, 0, True, ['2026-11-10T21:00:00Z'], {'at': 'x'},
                      '0001-01-01T00:00:00+23:00', '2026-13-40T99:00:00Z'):
            self.refused(self.at(value), 'capture_time_missing_or_malformed')

    def test_bounds_are_exact(self):
        day, skew = hosted.MAX_SCHEMA_CAPTURE_AGE_SECONDS, hosted.SCHEMA_FUTURE_SKEW_SECONDS
        self.assertEqual((day, skew), (86400, 300))
        self.ran(self.at(iso(-timedelta(seconds=day))))
        self.refused(self.at(iso(-timedelta(seconds=day + 1))), 'capture_too_old')
        self.ran(self.at(iso(timedelta(seconds=skew))))
        self.refused(self.at(iso(timedelta(seconds=skew + 1))), 'capture_time_in_the_future')

    def test_offsets_are_honoured_not_assumed(self):
        self.ran(self.at('2026-11-10T15:00:00-06:00'))                           # 21:00Z, 35 minutes old
        self.refused(self.at('2026-11-09T15:00:00+06:00'), 'capture_too_old')    # 09:00Z the day before: 36.6 hours old
        self.refused(self.at('2026-11-11T15:00:00-06:00'), 'capture_time_in_the_future')

    def test_an_approved_snapshot_goes_stale_after_a_delay(self):
        self.ran(self.deps())
        reads = list(self.reads)
        self.now = iso(timedelta(hours=23))
        self.assertEqual(self.go(self.deps())[0], 200)                           # still inside the window
        reads = list(self.reads)
        self.now = iso(timedelta(hours=25))
        code, body = self.go(self.deps())                                        # same snapshot, same approval record, now too old
        self.assertEqual((code, body['status'], body['snapshot_digest'], self.reads), (409, 'not_activated', None, reads))
        self.assertIn('capture_too_old', body['mapping_readiness'][0]['blocked_reasons'])

    def test_the_receipt_time_is_what_counts_and_the_snapshot_cannot_contradict_it(self):
        self.refused(self.at(iso(-timedelta(minutes=5)), receipt='2000-01-01T00:00:00+00:00'), 'capture_too_old')       # snapshot claims fresh
        self.refused(self.at('2000-01-01T00:00:00+00:00', receipt=iso(-timedelta(minutes=5))), 'snapshot_capture_time_contradicts_receipt')
        self.refused(self.at(iso(-timedelta(minutes=5)), receipt=None), 'capture_time_missing_or_malformed')
        self.refused(self.at(None, receipt=iso(-timedelta(minutes=5))), 'snapshot_capture_time_contradicts_receipt')
        self.ran(self.at('2026-11-10T15:00:00-06:00', receipt='2026-11-10T21:00:00Z'))                                   # same instant, other spelling


class ReceiptsCannotBeForged(Base):
    def test_no_capture_authority_means_deny(self):
        class Duck:                                                              # looks like one, is not one
            def receipt(self, identity): return FakeCaptures([schemas_for(dict(assignments=[assignment()]))[0]]).receipt(identity)
        for captures in (None, True, 'trusted', {}, dict(receipt='x'), lambda identity: {}, Duck(), FakeCaptures):
            body = self.refused(self.deps(captures=captures, approvals=ApprovesAnything()), 'no_capture_authority_configured')
            self.assertIn('capture_authority_configured', self.failed(body))

    def test_self_written_hashes_signatures_and_flags_are_not_origin_proof(self):
        identity, digest = readiness.snapshot_identity(self.schema), readiness.schema_digest(self.schema)
        receipt = dict(snapshot_identity=identity, site_id=SCOPE['site_id'], list_id=SCOPE['list_id'], schema_digest=digest, collector=COLLECTOR, captured_at=self.schema['captured_at'])
        for extra in (dict(receipt=receipt), dict(capture_receipt=receipt), dict(trusted=True, verified=True), dict(signature='sha256:' + digest, signed_by=COLLECTOR),
                      dict(collector=COLLECTOR, origin='reported_by_capture_authority'), dict(snapshot_identity=identity, schema_digest=digest)):
            schema = dict(deepcopy(self.schema), **extra)
            self.refused(self.deps(schemas=[schema], captures=FakeCaptures(), approvals=ApprovesAnything()), 'no_capture_receipt_for_this_exact_snapshot')
        config = dict(self.config, capture_receipts=[receipt], captures={identity: receipt}, collectors=[COLLECTOR], trusted_snapshots=[identity])
        config['assignments'] = [dict(self.config['assignments'][0], capture=receipt, capture_receipt=receipt)]
        self.refused(self.deps(config=config, captures=FakeCaptures(), approvals=ApprovesAnything()), 'no_capture_receipt_for_this_exact_snapshot')

    def test_the_request_cannot_carry_a_receipt_collector_or_verifier(self):
        for extra in (dict(captures={}), dict(collector=COLLECTOR), dict(collectors=[COLLECTOR]), dict(receipt={}), dict(schema_probe='skip'), dict(skip_drift_check=True)):
            self.assertEqual(self.call(self.deps(captures=None), self.body(mode='run', **extra))[0], 400)
        self.untouched()

    def test_forged_or_mismatched_receipts(self):
        good = FakeCaptures([self.schema]).records[readiness.snapshot_identity(self.schema)]
        other = rf.schema_for(bare_assignment(list_id='44444444-4444-4444-4444-444444444444'))
        cases = dict(
            other_identity=(dict(good, snapshot_identity='0' * 64), 'no_capture_receipt_for_this_exact_snapshot'),
            no_identity=({k: v for k, v in good.items() if k != 'snapshot_identity'}, 'no_capture_receipt_for_this_exact_snapshot'),
            wrong_schema_digest=(dict(good, schema_digest=readiness.schema_digest(other)), 'capture_receipt_schema_digest_mismatch'),
            no_schema_digest=({k: v for k, v in good.items() if k != 'schema_digest'}, 'capture_receipt_schema_digest_mismatch'),
            wrong_list=(dict(good, list_id=other['list_id']), 'capture_receipt_is_for_another_site_or_list'),
            wrong_site=(dict(good, site_id='example.sharepoint.com,99999999-9999-9999-9999-999999999999,22222222-2222-2222-2222-222222222222'), 'capture_receipt_is_for_another_site_or_list'),
            wrong_collector=(dict(good, collector='someone-else'), 'capture_collector_not_accepted'),
            collector_case=(dict(good, collector=COLLECTOR.upper()), 'capture_collector_not_accepted'),
            collector_padded=(dict(good, collector=COLLECTOR + ' '), 'capture_collector_not_accepted'),
            collector_list=(dict(good, collector=[COLLECTOR]), 'capture_collector_not_accepted'),
            no_collector=({k: v for k, v in good.items() if k != 'collector'}, 'capture_collector_not_accepted'))
        for name, (record, reason) in cases.items():
            class Forged(CaptureAuthority):
                def receipt(inner, identity): return deepcopy(record)
            body = self.refused(self.deps(captures=Forged(), approvals=ApprovesAnything()), reason)
        for shape in (None, 'receipt', [], [good], True, 5):
            class Shaped(CaptureAuthority):
                def receipt(inner, identity): return shape
            self.refused(self.deps(captures=Shaped(), approvals=ApprovesAnything()), 'no_capture_receipt_for_this_exact_snapshot')

    def test_a_receipt_for_one_snapshot_does_not_cover_an_edited_copy(self):
        captures = FakeCaptures([self.schema])
        for mutate in (lambda s: s['columns']['ActualScan'].update(description='x'), lambda s: s.update(note='x'),
                       lambda s: s['columns'].update(Spare=dict(name='Spare', displayName='Spare', id='dddddddd-0000-4000-8000-000000000999', text={}))):
            schema = deepcopy(self.schema); mutate(schema)
            self.refused(self.deps(schemas=[schema], captures=captures, approvals=ApprovesAnything()), 'no_capture_receipt_for_this_exact_snapshot')

    def test_capture_authority_that_fails_or_is_slow_to_answer(self):
        class Raises(CaptureAuthority):
            def receipt(inner, identity): raise TimeoutError('capture record unavailable')
        self.refused(self.deps(captures=Raises(), approvals=ApprovesAnything()), 'capture_authority_unreadable')
        self.refused(self.deps(captures=CaptureAuthority(), approvals=ApprovesAnything()), 'capture_authority_unreadable')       # the bare interface

    def test_accepted_collectors_come_from_the_server_side_only(self):
        for collectors in (None, (), [], '', COLLECTOR, {COLLECTOR: True}, (COLLECTOR, 5), ('',), (' ' + COLLECTOR,), True):
            body = self.refused(self.deps(collectors=collectors, approvals=ApprovesAnything()), 'no_accepted_collectors_configured')
            self.assertIn('accepted_collectors_configured', self.failed(body))
        self.refused(self.deps(collectors=('another-collector',), approvals=ApprovesAnything()), 'capture_collector_not_accepted')
        self.assertEqual(Deps().collectors, None)

    def test_the_authority_cannot_change_its_answer_under_the_run(self):
        class Shifty(FakeCaptures):
            def receipt(inner, identity):
                record = super().receipt(identity); inner.records[identity]['collector'] = 'someone-else'    # changes after answering once
                return record
        deps = self.deps(captures=Shifty([self.schema]))
        self.ran(deps)
        self.refused(self.deps(captures=deps.captures, approvals=ApprovesAnything()), 'capture_collector_not_accepted')

    def test_collector_and_capture_time_are_part_of_what_is_approved(self):
        base = self.call()[1]
        self.assertEqual(base['mapping_readiness'][0]['capture'], dict(collector=COLLECTOR, captured_at='2026-11-10T21:00:00+00:00', origin='reported_by_capture_authority'))
        second = FakeCaptures([self.schema], collector='second-collector')
        code, body = self.go(self.deps(captures=second, collectors=(COLLECTOR, 'second-collector')))          # accepted collector, but not the approved one
        self.assertEqual((code, self.failed(body)), (409, [APPROVAL])); self.assertNotEqual(body['snapshot_digest'], base['snapshot_digest'])
        self.untouched()

    def test_reports_never_call_a_snapshot_trusted_or_verified(self):
        import json
        for code, body in (self.call(), self.go()):
            text = json.dumps([body['proof'], body['mapping_readiness']]).lower()
            for claim in ('"trusted"', 'trusted production', 'origin_verified', '"verified"', 'origin proven'):
                self.assertNotIn(claim, text)
            self.assertEqual(body['proof']['schema_capture_origin'], 'reported_by_capture_authority_not_verified_by_this_module')

    def test_only_interfaces_exist_in_the_hosted_module(self):
        concrete = [n for n in dir(hosted) if isinstance(getattr(hosted, n), type)
                    and issubclass(getattr(hosted, n), (Authorizer, ApprovalAuthority, CaptureAuthority, SchemaProbe))
                    and getattr(hosted, n) not in (Authorizer, ApprovalAuthority, CaptureAuthority, SchemaProbe)]
        self.assertEqual(concrete, [])
        self.assertEqual((Deps().captures, Deps().collectors, Deps().schema_probe), (None, None, None))
        text = (ROOT / 'hosting' / 'last_scan_hosted.py').read_text()
        for forbidden in ('urllib', 'requests', 'http.client', 'socket', 'subprocess', 'os.environ', 'getenv', 'hmac', 'compare_digest', 'Bearer', 'jwt', 'secret_key'):
            self.assertNotIn(forbidden, text, forbidden)


class LiveSchemaDrift(Base):
    def probe(self, mutate=None, **kw):
        live = deepcopy(self.schema)
        if mutate: mutate(live)
        return FakeProbe([live], kw.pop('clock', lambda: self.now), **kw)

    def drifted(self, probe, problem, **kw):
        kw.setdefault('quota', Quota(500))                                       # a failed recheck does spend quota; not what is under test here
        with mock.patch.object(hosted.runner, 'run_once', side_effect=AssertionError('runner reached')) as run:
            code, body = self.go(self.deps(schema_probe=probe, **kw))
        self.assertEqual((code, body['status'], body['sources_read'], run.call_count, self.reads), (409, 'schema_drift', 0, 0, []))
        self.assertEqual(([d['problem'] for d in body['drift']], body['live_schema_drift_check']), ([problem], 'failed_before_any_read'))
        self.assertEqual(self.rows()[1], 0)                                      # no draft was stored
        return body

    def test_no_probe_means_deny(self):
        class Duck:
            def current(self, site_id, list_id): return {}
        for probe in (None, True, 'skip', {}, lambda site, lst: {}, Duck(), FakeProbe):
            code, body = self.go(self.deps(schema_probe=probe))
            self.assertEqual((code, body['status']), (409, 'not_activated')); self.assertIn('live_schema_probe_configured', self.failed(body))
        self.untouched()

    def test_dry_run_never_probes(self):
        probe = self.probe()
        code, body = self.call(self.deps(schema_probe=probe))
        self.assertEqual((probe.calls, body['live_schema_drift_check'], body['ready_to_run']), (0, 'not_performed_in_dry_run', True))

    def test_clean_run_probes_before_every_read(self):
        log = []
        def fetch(scope, settings):
            log.append('read'); return self.fetch(scope, settings)
        probe = self.probe(log=log)
        body = self.ran(self.deps(schema_probe=probe, fetch=fetch))
        self.assertEqual(body['live_schema_drift_check'], 'performed_before_each_read')
        reads = log.count('read')
        self.assertGreaterEqual(reads, 1)
        self.assertEqual(log, ['probe'] + ['probe', 'read'] * reads)             # once up front, then immediately before each read
        self.assertEqual(body['schema_probes'], reads + 1)

    def test_live_column_changes_block_before_any_read(self):
        changes = dict(calculated=lambda s: s['columns']['ActualScan'].update(calculated={'formula': '=[Created]'}),
                       replaced=lambda s: s['columns']['ActualScan'].update(id='dddddddd-0000-4000-8000-000000000998'),
                       read_only=lambda s: s['columns']['Inspected'].update(readOnly=True),
                       new_choice=lambda s: s['columns']['Shift']['choice']['choices'].append('Night'),
                       removed=lambda s: s['columns'].pop('Rejected'),
                       added_unmapped=lambda s: s['columns'].update(Spare=dict(name='Spare', displayName='Spare', id='dddddddd-0000-4000-8000-000000000999', text={})),
                       renamed=lambda s: s['columns']['Part'].update(displayName='Part number'), default=lambda s: s['columns']['ActualScan'].update(defaultValue={'formula': '=[Now]'}))
        for name, mutate in changes.items():
            self.drifted(self.probe(mutate), 'live_schema_differs_from_the_approved_snapshot')

    def test_probe_failures_and_malformed_answers_block(self):
        class Raises(SchemaProbe):
            def current(inner, site_id, list_id): raise ConnectionError('source unavailable')
        self.drifted(Raises(), 'schema_probe_failed:ConnectionError')
        self.drifted(SchemaProbe(), 'schema_probe_failed:NotImplementedError')
        for answer in (None, 'ok', [], True, {}, dict(self.schema, list_id='44444444-4444-4444-4444-444444444444'), dict(self.schema, site_id='other')):
            class Answers(SchemaProbe):
                def current(inner, site_id, list_id): return answer
            self.drifted(Answers(), 'schema_probe_answer_is_malformed_or_for_another_list')

    def test_probe_answers_must_be_dated_and_fresh(self):
        for stamp in (None, '', 'now', '2026-11-10', '2026-11-10T21:35:00', 5):
            self.drifted(self.probe(clock=lambda: stamp), 'schema_probe_answer_is_undated')
        self.assertEqual(hosted.MAX_SCHEMA_PROBE_AGE_SECONDS, 60)
        for stamp in (iso(-timedelta(seconds=61)), iso(-timedelta(hours=1)), '2000-01-01T00:00:00Z', iso(timedelta(seconds=301)), '2099-01-01T00:00:00Z'):
            self.drifted(self.probe(clock=lambda: stamp), 'schema_probe_answer_is_not_fresh')      # a cached or replayed answer
        self.ran(self.deps(schema_probe=self.probe(clock=lambda: iso(-timedelta(seconds=60))), quota=Quota(500)))

    def test_drift_that_appears_mid_run_stops_the_remaining_reads(self):
        config = dict(enabled=True, assignments=[assignment(recorded_shifts=['1', '2', '3'])])
        self.approve(config)
        schema = schemas_for(config)[0]
        class DriftsAfter(FakeProbe):
            def current(inner, site_id, list_id):
                answer = super().current(site_id, list_id)
                if inner.calls > 2:                                              # the up-front look and the first read's look are clean
                    answer['columns']['ActualScan']['calculated'] = {'formula': '=[Created]'}
                return answer
        probe = DriftsAfter([schema], lambda: self.now)
        code, body = self.go(self.deps(config=config, schema_probe=probe))
        self.assertEqual((code, body['status']), (200, 'ran'))
        self.assertEqual(len(self.reads), 1)                                     # exactly the one read that was confirmed
        results = body['summary']['results']
        self.assertEqual(len(results), 3)
        refused = [r for r in results if r['status'] == 'error' and r.get('error_type') == 'SchemaDrift']
        self.assertEqual(len(refused), 2, results)                               # the two scopes whose look came back changed were not read
        self.assertGreater(body['schema_probes'], 3)

    def test_failed_recheck_spends_the_nonce_and_is_not_replayable(self):
        probe = self.probe(lambda s: s['columns'].pop('Rejected'))
        body = self.body(mode='run')
        self.assertEqual(self.call(self.deps(schema_probe=probe), body)[1]['status'], 'schema_drift')
        self.assertEqual(self.call(self.deps(), dict(body))[1]['status'], 'replayed_request')    # same nonce, schema now fine: still refused
        self.assertEqual(self.reads, [])

    def test_a_cached_receipt_is_not_replayed_while_the_live_schema_differs(self):
        self.ran(self.deps())
        code, again = self.go(run_id='r%d' % self.n)
        self.assertEqual(again['summary'].get('replayed'), True)
        reads = list(self.reads)
        code, body = self.go(self.deps(schema_probe=self.probe(lambda s: s['columns'].pop('Rejected'))), run_id='r%d' % (self.n - 1))
        self.assertEqual((code, body['status'], self.reads), (409, 'schema_drift', reads)); self.assertNotIn('summary', body)

    def test_concurrent_runs_each_recheck_and_none_reads_a_drifted_list(self):
        drifted = self.probe(lambda s: s['columns']['ActualScan'].update(calculated={'formula': '=[Created]'}))
        bodies = [self.body(mode='run') for _ in range(6)]
        with ThreadPoolExecutor(6) as pool:
            out = list(pool.map(lambda b: self.call(self.deps(schema_probe=drifted, quota=Quota(50)), b), bodies))
        self.assertEqual({(code, body['status']) for code, body in out}, {(409, 'schema_drift')})
        self.assertEqual((self.reads, drifted.calls), ([], 6))

    def test_approval_does_not_excuse_drift_and_drift_check_does_not_replace_approval(self):
        probe = self.probe(lambda s: s['columns'].pop('Rejected'))
        self.drifted(probe, 'live_schema_differs_from_the_approved_snapshot', approvals=ApprovesAnything())
        code, body = self.go(self.deps(approvals=FakeApprovals()))               # clean live schema, no approval
        self.assertEqual((code, self.failed(body)), (409, [APPROVAL])); self.assertEqual(self.reads, [])

    def test_the_guard_raises_instead_of_reading(self):
        deps = self.deps(schema_probe=self.probe(lambda s: s['columns'].pop('Rejected')))
        mapping = self.call(self.deps())[1]['mapping_readiness']
        guard = hosted._DriftGuard(deps, mapping)
        with self.assertRaises(SchemaDrift):
            guard.fetch(dict(site_id=SCOPE['site_id'], list_id=SCOPE['list_id'], shift='1'), {})
        with self.assertRaises(SchemaDrift):
            guard.fetch(dict(site_id=SCOPE['site_id'], list_id='44444444-4444-4444-4444-444444444444', shift='1'), {})   # a list nothing was approved for
        self.assertEqual(self.reads, [])


class EarlierGatesStillHold(Base):
    def test_identity_approval_replay_quota_and_provider_gates(self):
        self.assertEqual(self.go(self.deps(authorizer=None))[0], 503)
        self.assertEqual(self.go(headers={})[0], 401)
        self.assertEqual(self.failed(self.go(self.deps(approvals=FakeApprovals()))[1]), [APPROVAL])
        body = self.body(mode='run')
        self.assertEqual(self.call(self.deps(), body)[0], 200)
        self.assertEqual(self.call(self.deps(), dict(body))[1]['status'], 'replayed_request')
        self.assertEqual(self.go(self.deps(quota=Quota(1)))[1]['status'], 'quota_exceeded')
        self.assertIn('delivery_transport_disabled', [c['check'] for c in self.call()[1]['preflight'] if c['ok']])
        self.assertEqual(self.call()[1]['transport'], 'disabled')


if __name__ == '__main__':
    unittest.main()
