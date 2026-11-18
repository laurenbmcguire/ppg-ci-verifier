"""Accepted-collector policy is frozen before any callback. Regression for the TOCTOU found by independent QA of
03c74e0: Authorizer.verify appended a collector to the host's mutable list and a denied request then ran.
FAKE identity, approval, capture authority and probe; synthetic snapshots and source only."""
import threading
import unittest
import unittest.mock
from concurrent.futures import ThreadPoolExecutor
from hosting import last_scan_hosted as hosted
from hosting.last_scan_hosted import Authorizer, Principal, Quota
from test_last_scan_hosted import COLLECTOR, FakeApprovals, FakeAuthorizer, FakeCaptures, FakeProbe, Hosted, schemas_for

APPROVAL, MAPPING = 'approval_attested_for_this_snapshot_and_principal', 'source_mapping_ready_for_every_assignment'
OTHER = 'some-other-collector'


class Base(Hosted):
    def setUp(self):
        super().setUp()
        self.schema = schemas_for(self.config)[0]

    def denied(self, deps, **kw):
        before = list(self.reads)
        code, body = self.go(deps, **kw)
        self.assertEqual((code, body['status'], self.reads), (409, 'not_activated', before), body.get('summary'))
        return body

    def reasons(self, body):
        return body['mapping_readiness'][0]['blocked_reasons']

    def mutating(self, policy, change, where):
        """deps in which one callback changes the HOST's policy object while the request is in flight."""
        test = self
        class A(FakeAuthorizer):
            def verify(self, method, headers, body_digest):
                if where == 'authorizer': change(policy)
                return super().verify(method, headers, body_digest)
        class C(FakeCaptures):
            def receipt(self, identity):
                if where == 'capture_authority': change(policy)
                return super().receipt(identity)
        class P(FakeApprovals):
            def attestation(self, digest):
                if where == 'approval': change(policy)
                return super().attestation(digest)
        class L(FakeProbe):
            def current(self, site_id, list_id):
                if where == 'probe': change(policy)
                return super().current(site_id, list_id)
        def fetch(scope, settings):
            if where == 'fetch': change(policy)
            return test.fetch(scope, settings)
        def clock():
            if where == 'clock': change(policy)
            return test.now
        return self.deps(authorizer=A(), captures=C([self.schema]), approvals=P(self.approvals.records),
                         schema_probe=L([self.schema], lambda: self.now), fetch=fetch, clock=clock, collectors=policy, quota=Quota(500))


class AddingCannotPermitADeniedRequest(Base):
    def test_qa_repro_authorizer_appends_the_collector_to_the_original_list(self):
        policy = [OTHER]
        self.assertIn('capture_collector_not_accepted', self.reasons(self.denied(self.deps(collectors=policy))))
        body = self.denied(self.mutating(policy, lambda p: p.append(COLLECTOR), 'authorizer'))
        self.assertEqual(policy, [OTHER, COLLECTOR])                             # the host's object did change...
        self.assertIn('capture_collector_not_accepted', self.reasons(body))      # ...the request was judged on the policy it arrived under
        self.assertEqual(body['accepted_collectors'], [OTHER])
        self.assertEqual((self.reads, self.rows()), ([], (0, 0)))

    def test_every_callback_and_every_container_type(self):
        for where in ('authorizer', 'capture_authority', 'approval', 'clock'):
            for make, add in ((lambda: [OTHER], lambda p: p.append(COLLECTOR)), (lambda: {OTHER}, lambda p: p.add(COLLECTOR))):
                policy = make()
                body = self.denied(self.mutating(policy, add, where))
                if where != 'approval':                                       # with the mapping blocked there is no digest, so approval is never asked
                    self.assertIn(COLLECTOR, policy, where)
                self.assertEqual((body['accepted_collectors'], body['snapshot_digest']), ([OTHER], None), where)
        self.assertEqual((self.reads, self.rows()), ([], (0, 0)))

    def test_replacing_an_empty_or_invalid_policy_mid_request(self):
        for start in ([], [5], ['']):
            policy = list(start)
            def fix(p): p.clear(); p.append(COLLECTOR)
            body = self.denied(self.mutating(policy, fix, 'authorizer'))
            self.assertEqual(policy, [COLLECTOR])
            self.assertIn('no_accepted_collectors_configured', self.reasons(body)); self.assertIsNone(body['accepted_collectors'])
        self.assertEqual(self.reads, [])

    def test_the_next_request_sees_the_new_policy_and_needs_a_new_approval(self):
        policy = [OTHER]
        self.denied(self.mutating(policy, lambda p: p.append(COLLECTOR), 'authorizer'))
        body = self.denied(self.deps(collectors=policy))                         # now [OTHER, COLLECTOR]: mapping is ready...
        self.assertEqual((self.failed(body), body['accepted_collectors']), ([APPROVAL], sorted([OTHER, COLLECTOR])))   # ...but this policy was never approved
        self.approvals.records[body['snapshot_digest']] = dict(snapshot_digest=body['snapshot_digest'], approved_by='synthetic approver',
                                                               approved_at='2026-11-10', principals=['alice'])
        code, body = self.go(self.deps(collectors=policy))
        self.assertEqual((code, body['status']), (200, 'ran'))


class RemovingCannotChangeARequestInFlight(Base):
    def test_removal_during_any_callback_leaves_the_frozen_policy_in_force(self):
        for where in ('authorizer', 'capture_authority', 'approval', 'probe', 'fetch', 'clock'):
            policy = [COLLECTOR]
            code, body = self.go(self.mutating(policy, lambda p: p.clear(), where))
            self.assertEqual((code, body['status'], policy), (200, 'ran', []), where)
            self.assertEqual(body['accepted_collectors'], [COLLECTOR], where)    # the whole request used the policy it was approved under
            self.assertEqual(body['mapping_readiness'][0]['capture']['collector'], COLLECTOR)
            nxt = self.denied(self.deps(collectors=policy, quota=Quota(50)))     # the NEXT request sees the removal
            self.assertIn('no_accepted_collectors_configured', self.reasons(nxt))

    def test_swapping_to_another_collector_mid_request(self):
        policy = [COLLECTOR]
        def swap(p): p.clear(); p.append(OTHER)
        code, body = self.go(self.mutating(policy, swap, 'approval'))
        self.assertEqual((code, body['accepted_collectors']), (200, [COLLECTOR]))
        self.assertIn('capture_collector_not_accepted', self.reasons(self.denied(self.deps(collectors=policy))))


class PolicyIsPartOfWhatIsApproved(Base):
    def test_a_policy_change_requires_a_new_approval(self):
        base = self.call()[1]
        self.assertEqual((base['accepted_collectors'], self.failed(base)), ([COLLECTOR], []))
        for policy in ((COLLECTOR, OTHER), [OTHER, COLLECTOR], {COLLECTOR, 'third-collector'}):
            body = self.denied(self.deps(collectors=policy))
            self.assertEqual(self.failed(body), [APPROVAL], policy)              # same collector on the receipt, wider policy: not what was approved
            self.assertNotEqual(body['snapshot_digest'], base['snapshot_digest'])
        self.assertEqual(self.reads, [])

    def test_container_type_order_and_duplicates_do_not_matter(self):
        base = self.call()[1]['snapshot_digest']
        for policy in ([COLLECTOR], (COLLECTOR,), {COLLECTOR}, frozenset([COLLECTOR]), [COLLECTOR, COLLECTOR]):
            self.assertEqual(self.call(self.deps(collectors=policy))[1]['snapshot_digest'], base, policy)
        a = self.call(self.deps(collectors=[COLLECTOR, OTHER]))[1]['snapshot_digest']
        self.assertEqual(a, self.call(self.deps(collectors=(OTHER, COLLECTOR)))[1]['snapshot_digest'])

    def test_invalid_policies_freeze_to_deny(self):
        def gen(): yield COLLECTOR
        for policy in (None, (), [], '', COLLECTOR, {COLLECTOR: True}, [COLLECTOR, 5], [COLLECTOR, ''], [' ' + COLLECTOR], True, gen(), iter([COLLECTOR]), [[COLLECTOR]]):
            body = self.denied(self.deps(collectors=policy))
            self.assertIn('accepted_collectors_configured', self.failed(body)); self.assertIsNone(body['accepted_collectors'])

    def test_the_frozen_policy_cannot_be_altered_by_a_callback_that_reaches_deps(self):
        seen = []
        real = hosted.capture_problems
        def spy(deps, schema, row):
            seen.append(deps.collectors); return real(deps, schema, row)
        with unittest.mock.patch.object(hosted, 'capture_problems', spy):
            self.go()
        self.assertTrue(seen and all(isinstance(c, frozenset) for c in seen))


class ConcurrentHostMutation(Base):
    def test_a_host_thread_adding_the_collector_never_lets_a_denied_request_run(self):
        policy, stop = [OTHER], threading.Event()
        def host():
            while not stop.is_set():
                policy.append(COLLECTOR); policy.remove(COLLECTOR)
        thread = threading.Thread(target=host); thread.start()
        try:
            bodies = [self.body(mode='run') for _ in range(40)]
            with ThreadPoolExecutor(8) as pool:
                out = list(pool.map(lambda b: self.call(self.deps(collectors=policy, quota=Quota(500)), b), bodies))
        finally:
            stop.set(); thread.join()
        self.assertEqual({code for code, _ in out}, {409})                      # [OTHER] is denied; [OTHER, COLLECTOR] was never approved
        self.assertEqual(self.reads, [])
        for code, body in out:
            self.assertIn(body['accepted_collectors'], ([OTHER], sorted([OTHER, COLLECTOR])))
            failed = self.failed(body)
            self.assertEqual(failed == [APPROVAL], body['accepted_collectors'] == sorted([OTHER, COLLECTOR]))   # one policy, used throughout

    def test_a_host_thread_removing_the_collector_gives_each_request_one_consistent_policy(self):
        policy, stop = [COLLECTOR], threading.Event()
        def host():
            while not stop.is_set():
                policy.remove(COLLECTOR); policy.append(COLLECTOR)
        thread = threading.Thread(target=host); thread.start()
        try:
            bodies = [self.body(mode='run') for _ in range(40)]
            with ThreadPoolExecutor(8) as pool:
                out = list(pool.map(lambda b: self.call(self.deps(collectors=policy, quota=Quota(500)), b), bodies))
        finally:
            stop.set(); thread.join()
        for code, body in out:
            if code == 200:
                self.assertEqual((body['status'], body['accepted_collectors']), ('ran', [COLLECTOR]))
            else:
                self.assertEqual((code, body['status'], body['accepted_collectors']), (409, 'not_activated', None))
                self.assertIn('accepted_collectors_configured', self.failed(body))


if __name__ == '__main__':
    import unittest.mock
    unittest.main()
