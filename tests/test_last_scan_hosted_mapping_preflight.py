"""Source-mapping readiness as a MANDATORY hosted preflight check. Synthetic schema snapshots, FAKE identity and
approval systems. Everything else stays default-deny; nothing here is a route, a schedule or a live read."""
import inspect
import json
import unittest
from copy import deepcopy
from unittest import mock
import readiness_fixture as rf
from hosting import last_scan_hosted as hosted, last_scan_readiness as readiness
from hosting.last_scan_hosted import ApprovalAuthority, Authorizer, Deps, Principal, Quota
from test_last_scan_hosted import ALICE, BOB, ENTRY, FakeApprovals, Hosted, SCOPE, assignment, bare_assignment, schemas_for

MAPPING, SUPPLIED, RENDERERS, APPROVAL = ('source_mapping_ready_for_every_assignment', 'schema_snapshots_supplied',
                                           'renderers_built_from_configuration', 'approval_attested_for_this_snapshot_and_principal')
OTHER_LIST = '44444444-4444-4444-4444-444444444444'
OTHER_ENTRY = dict(ENTRY, list_id=OTHER_LIST, job='OTHER-9002')


class Base(Hosted):
    def setUp(self):
        super().setUp()
        self.schema = schemas_for(self.config)[0]

    def refused(self, deps, reasons=None, checks=(MAPPING,)):
        """A run is refused before anything is read, built or written; a dry run says why."""
        with mock.patch.object(hosted.runner, 'run_once', side_effect=AssertionError('runner reached')) as run, \
             mock.patch.object(hosted, 'StoredDraftRouter', side_effect=AssertionError('renderer built')) as router, \
             mock.patch.object(hosted.runner, 'plan', side_effect=AssertionError('scopes planned')) as plan:
            code, body = self.go(deps)
            dry_code, dry = self.call(deps)
        self.assertEqual((code, body['status'], body['sources_read'], body['records_written']), (409, 'not_activated', 0, 0))
        self.assertEqual((dry_code, dry['status'], dry['ready_to_run'], dry['snapshot_digest'], dry['planned_scopes']), (200, 'dry_run', False, None, 0))
        self.assertEqual((run.call_count, router.call_count, plan.call_count), (0, 0, 0))
        self.untouched()
        failed = self.failed(body)
        for c in checks + (RENDERERS, APPROVAL):
            self.assertIn(c, failed)
        if reasons is not None:
            got = {r for m in body['mapping_readiness'] for r in m['blocked_reasons']}
            self.assertTrue(set(reasons) <= got, got)
        return body

    def with_schema(self, mutate):
        schema = deepcopy(self.schema); mutate(schema)
        return schema


class MandatoryGate(Base):
    def test_ready_mapping_is_reported_and_bound_and_the_approved_run_still_works(self):
        code, dry = self.call()
        self.assertEqual((code, dry['ready_to_run'], self.failed(dry)), (200, True, []))
        m = dry['mapping_readiness'][0]
        self.assertEqual((m['ready'], m['blocked_reasons'], m['schema_snapshot'], m['schema_digest']),
                         (True, [], readiness.snapshot_identity(self.schema), readiness.schema_digest(self.schema)))
        self.assertEqual(m['mapping_contract'], readiness.attestation_bindings(self.config['assignments'][0]))
        self.untouched()
        code, body = self.go()
        self.assertEqual((code, body['status'], set(self.reads)), (200, 'ran', {'1'}))

    def test_missing_snapshots(self):
        for schemas in (None, [], {}, 'schemas', [None], ['x'], [{}], (self.schema,), {SCOPE['list_id']: self.schema}):
            self.refused(self.deps(schemas=schemas))
        self.assertIn(SUPPLIED, self.failed(self.go(self.deps(schemas=None))[1]))
        self.refused(self.deps(schemas=None), ['no_schema_snapshots_supplied'], (MAPPING, SUPPLIED))
        self.refused(self.deps(schemas=[rf.schema_for(bare_assignment(list_id=OTHER_LIST))]), ['no_schema_snapshot_for_this_site_and_list'])

    def test_one_assignment_without_a_snapshot_blocks_the_whole_run(self):
        config = dict(enabled=True, assignments=[assignment(), assignment(list_id=OTHER_LIST, job='OTHER-9002')])
        both = self.deps(config=config, entries=[ENTRY, OTHER_ENTRY])
        self.assertEqual(self.failed(self.call(both)[1]), [APPROVAL])                 # ready, only unapproved
        body = self.refused(self.deps(config=config, entries=[ENTRY, OTHER_ENTRY], schemas=[self.schema]), ['no_schema_snapshot_for_this_site_and_list'])
        self.assertEqual([m['ready'] for m in body['mapping_readiness']], [True, False])

    def test_two_snapshots_for_one_list_are_contradictory_even_when_identical(self):
        later = dict(self.schema, captured_at='2026-11-11T09:00:00+00:00')
        for pair in ([self.schema, deepcopy(self.schema)], [self.schema, later]):
            self.refused(self.deps(schemas=pair), ['more_than_one_schema_snapshot_for_this_site_and_list'])

    def test_blocked_mapping_reasons_reach_the_report(self):
        cases = dict(
            calculated=(lambda s: s['columns']['ActualScan'].update(calculated={'formula': '=[Created]'}), 'scan_field_is_not_calculated'),
            renamed=(lambda s: s['columns']['ActualScan'].update(name='Created'), 'scan_field_column_name_matches_source'),
            read_only=(lambda s: s['columns']['Inspected'].update(readOnly=True), 'inspected_definition_unchanged'),
            new_choice=(lambda s: s['columns']['Shift']['choice']['choices'].append('Night'), 'every_shift_choice_is_covered'),
            column_gone=(lambda s: s['columns'].pop('Rejected'), 'rejected_column_exists'),
            damaged=(lambda s: s['columns'].update(Spare=None), 'schema_columns_are_well_formed'))
        for name, (mutate, reason) in cases.items():
            self.refused(self.deps(schemas=[self.with_schema(mutate)]), [reason])

    def test_stale_or_missing_attestations(self):
        a = self.config['assignments'][0]
        stale = dict(a, zone='America/New_York')                                      # contract changed after it was attested
        self.refused(self.deps(config=dict(enabled=True, assignments=[stale])), ['timezone_attested'])
        unpinned = bare_assignment()
        self.refused(self.deps(config=dict(enabled=True, assignments=[unpinned])), ['scan_time_meaning_attested', 'scan_field_pinned_by_column_id_and_type'])
        partly = deepcopy(a); del partly['attestations']['scan_time_meaning']
        self.refused(self.deps(config=dict(enabled=True, assignments=[partly])), ['scan_time_meaning_attested'])

    def test_there_is_no_switch_to_skip_it(self):
        self.assertEqual(set(hosted.FIELDS), {'run_id', 'mode', 'budget_seconds', 'max_scopes', 'timestamp', 'nonce'})
        self.assertEqual(list(inspect.signature(hosted.handle).parameters), ['method', 'headers', 'body', 'deps'])
        for extra in (dict(skip_mapping_readiness=True), dict(schemas=[self.schema]), dict(mapping_ready=True), dict(force=True)):
            self.assertEqual(self.call(self.deps(schemas=None), self.body(mode='run', **extra))[0], 400)
        flags = dict(mapping_ready=True, schema_verified=True, source_mapping_ready_for_every_assignment=True, skip_preflight=True,
                     readiness=dict(ready_for_dry_run=True, status='ready_for_dry_run_only'), preflight=[dict(check=MAPPING, ok=True)])
        config = dict(self.config, **flags)
        config['assignments'] = [dict(bare_assignment(), **flags)]
        self.refused(self.deps(config=config, schemas=None), checks=(MAPPING, SUPPLIED))
        self.refused(self.deps(config=config), ['scan_time_meaning_attested'])

    def test_direct_runner_helper_remains_available_to_tests_only(self):
        self.assertNotIn('readiness', inspect.getsource(hosted.runner.run_once))       # the gate lives in the hosted entry point
        self.assertIn('mapping_readiness(config', inspect.getsource(hosted.preflight))
        self.assertEqual(Deps().schemas, None)


class NotApprovalAndNotProof(Base):
    def test_self_authored_attestations_and_flags_are_not_the_approval(self):
        empty = FakeApprovals()
        code, body = self.go(self.deps(approvals=empty))                               # mapping fully ready and attested in config
        self.assertEqual((code, body['status'], self.failed(body)), (409, 'not_activated', [APPROVAL]))
        self.assertTrue(body['mapping_readiness'][0]['ready'])
        config = dict(self.config, activation=dict(approved=True, snapshot_digest=body['snapshot_digest']), approved=True)
        code, body = self.go(self.deps(config=config, approvals=empty))
        self.assertEqual((code, self.failed(body)), (409, [APPROVAL]))
        self.untouched()

    def test_an_attestation_naming_an_approver_is_still_only_an_input(self):
        a = deepcopy(self.config['assignments'][0])
        for record in a['attestations'].values():
            record.update(attested_by='synthetic approver', approved=True, approval='granted')
        config = dict(enabled=True, assignments=[a])
        code, body = self.go(self.deps(config=config, approvals=FakeApprovals()))
        self.assertEqual((code, self.failed(body)), (409, [APPROVAL]))

    def test_reports_never_claim_runtime_proof_or_scan_semantics(self):
        for code, body in (self.call(), self.go()):
            for proof in (body['proof'], body['mapping_readiness'][0]['proof']):
                self.assertEqual((proof['runtime_source_proof'], proof['scan_time_semantics'], proof['sample_evidence']),
                                 ('not_established', 'not_established_by_this_tool', 'not_performed'))
            self.assertEqual(body['mapping_readiness'][0]['not_performed'], ['sample_rows_examined', 'scan_compared_with_arrival_metadata'])
            self.assertEqual(body['mapping_readiness'][0]['proof']['schema_dry_run_readiness'], 'ready')
            text = json.dumps([body['proof'], body['mapping_readiness']]).lower()
            for claim in ('scan time confirmed', 'runtime verified', 'source verified', '"verified"'):
                self.assertNotIn(claim, text)


class ApprovalBindsTheExactSnapshot(Base):
    def digest(self, **kw):
        return self.call(self.deps(**kw))[1]['snapshot_digest']

    def test_snapshot_identity_is_part_of_the_approved_digest(self):
        base = self.digest()
        changes = dict(captured_at=lambda s: s.update(captured_at='2026-11-10T21:10:00+00:00'),
                       unmapped_column=lambda s: s['columns'].update(Spare=dict(name='Spare', displayName='Spare', id='dddddddd-0000-4000-8000-000000000999', text={})),
                       unmapped_description=lambda s: s['columns']['DefectPhoto'].update(description='changed'),
                       list_name=lambda s: s['list'].update(displayName='Renamed'), extra_key=lambda s: s.update(note='x'))
        seen = {base}
        for name, mutate in changes.items():
            schema = self.with_schema(mutate)
            code, dry = self.call(self.deps(schemas=[schema]))
            self.assertTrue(dry['mapping_readiness'][0]['ready'], name)                  # still ready...
            self.assertNotIn(dry['snapshot_digest'], seen, name); seen.add(dry['snapshot_digest'])
            code, body = self.go(self.deps(schemas=[schema]))                            # ...but it is not the approved snapshot
            self.assertEqual((code, self.failed(body)), (409, [APPROVAL]), name)
        self.untouched()

    def test_an_extra_unrelated_snapshot_changes_what_is_approved(self):
        extra = rf.schema_for(bare_assignment(list_id=OTHER_LIST))
        self.assertNotEqual(self.digest(schemas=[self.schema, extra]), self.digest())
        self.assertEqual((self.go(self.deps(schemas=[self.schema, extra]))[0]), 409)

    def test_schema_mutation_during_approval_cannot_change_what_runs(self):
        schemas = [deepcopy(self.schema)]
        class Mutating(FakeApprovals):
            def attestation(inner, digest):
                schemas[0]['columns']['ActualScan']['calculated'] = {'formula': '=[Created]'}     # the host's list changes mid-approval
                schemas.append(dict(self.schema, captured_at='later'))
                return super().attestation(digest)
        approvals = Mutating(self.approvals.records)
        code, body = self.go(self.deps(approvals=approvals, schemas=schemas))
        self.assertEqual((code, body['status']), (200, 'ran'))                           # ran on the private copy that was hashed and approved
        self.assertEqual(body['mapping_readiness'][0]['schema_snapshot'], readiness.snapshot_identity(self.schema))
        self.assertEqual(len(schemas), 2)                                                # the caller's object did change...
        reads = list(self.reads)
        code, body = self.go(self.deps(schemas=schemas))                                 # ...and the NEXT request sees it and is blocked
        self.assertEqual((code, body['snapshot_digest'], self.reads), (409, None, reads)); self.assertIn(MAPPING, self.failed(body))

    def test_schema_mutation_during_authentication_or_planning(self):
        schemas = [deepcopy(self.schema)]
        class Mutating(Authorizer):
            def verify(inner, method, headers, body_digest):
                schemas[0]['columns']['Inspected']['readOnly'] = True
                return Principal('alice')
        code, body = self.go(self.deps(authorizer=Mutating(), schemas=schemas))
        self.assertEqual((code, body['mapping_readiness'][0]['schema_snapshot']), (200, readiness.snapshot_identity(self.schema)))
        schemas = [deepcopy(self.schema)]
        def fetch(scope, settings):
            schemas[0]['columns'].clear()
            return self.fetch(scope, settings)
        self.assertEqual(self.go(self.deps(fetch=fetch, schemas=schemas))[0], 200)

    def test_approval_given_then_schema_changes_then_repinned_needs_a_new_approval(self):
        self.assertEqual(self.go()[0], 200)
        reads = list(self.reads)
        changed = self.with_schema(lambda s: s['columns']['Inspected'].update(required=True))
        self.reads.clear()
        code, body = self.go(self.deps(schemas=[changed]))                               # old pins against the new schema
        self.assertEqual((code, body['snapshot_digest']), (409, None)); self.assertIn(MAPPING, self.failed(body))
        repinned = rf.attested(bare_assignment(), changed)[0]
        config = dict(enabled=True, assignments=[repinned])
        code, body = self.go(self.deps(config=config, schemas=[changed]))                # re-pinned and re-attested, not re-approved
        self.assertEqual((code, self.failed(body)), (409, [APPROVAL]))
        self.assertEqual(self.reads, [])
        self.approvals.records[body['snapshot_digest']] = dict(snapshot_digest=body['snapshot_digest'], approved_by='synthetic approver',
                                                               approved_at='2026-11-11', principals=['alice'])
        self.assertEqual(self.go(self.deps(config=config, schemas=[changed]))[0], 200)
        self.assertTrue(self.reads and set(self.reads) == {'1'})                         # only now is the source read again


class CrossScopeSubstitution(Base):
    def setUp(self):
        super().setUp()
        self.second = assignment(list_id=OTHER_LIST, job='OTHER-9002')
        self.config2 = dict(enabled=True, assignments=[self.config['assignments'][0], self.second])
        self.schema2 = rf.schema_for(self.second)
        self.entries = [ENTRY, OTHER_ENTRY]

    def deps2(self, **kw):
        return self.deps(**dict(dict(config=self.config2, entries=self.entries, schemas=[self.schema, self.schema2]), **kw))

    def test_baseline_two_lists_are_ready(self):
        dry = self.call(self.deps2())[1]
        self.assertEqual(([m['ready'] for m in dry['mapping_readiness']], self.failed(dry)), ([True, True], [APPROVAL]))
        self.assertNotEqual(dry['mapping_readiness'][0]['schema_snapshot'], dry['mapping_readiness'][1]['schema_snapshot'])

    def test_one_lists_schema_relabelled_as_the_other(self):
        forged = dict(deepcopy(self.schema), list_id=OTHER_LIST)                         # list A's columns claiming to be list B
        body = self.refused(self.deps2(schemas=[self.schema, forged]))
        self.assertEqual([m['ready'] for m in body['mapping_readiness']], [True, False])
        self.assertTrue({'schema_is_for_this_site_and_list'} <= set(body['mapping_readiness'][1]['blocked_reasons']) or
                        any(r.endswith('_column_id_unchanged') or r.endswith('_definition_unchanged') for r in body['mapping_readiness'][1]['blocked_reasons']))
        relabelled = deepcopy(self.schema); relabelled['list_id'] = OTHER_LIST; relabelled['list']['id'] = OTHER_LIST
        body = self.refused(self.deps2(schemas=[self.schema, relabelled]))               # even fully relabelled, the pinned column IDs are list B's
        self.assertFalse(body['mapping_readiness'][1]['ready'])

    def test_attestations_or_contracts_borrowed_from_the_other_assignment(self):
        first = self.config['assignments'][0]
        for key in ('attestations', 'field_contract'):
            swapped = dict(self.second, **{key: deepcopy(first[key])})
            body = self.refused(self.deps2(config=dict(enabled=True, assignments=[first, swapped])))
            self.assertEqual([m['ready'] for m in body['mapping_readiness']], [True, False], key)
        body = self.refused(self.deps2(config=dict(enabled=True, assignments=[first, dict(self.second, attestations=deepcopy(first['attestations']))])),
                            ['scan_time_meaning_attested', 'identity_and_registry_attested'])

    def test_swapped_snapshots_order_does_not_matter_but_swapped_content_does(self):
        a = self.call(self.deps2())[1]['snapshot_digest']
        b = self.call(self.deps2(schemas=[self.schema2, self.schema]))[1]['snapshot_digest']
        self.assertEqual(a, b)                                                           # matched by site + list, never by position
        swapped_a, swapped_b = dict(deepcopy(self.schema2), list_id=SCOPE['list_id']), dict(deepcopy(self.schema), list_id=OTHER_LIST)
        body = self.refused(self.deps2(schemas=[swapped_a, swapped_b]))
        self.assertEqual([m['ready'] for m in body['mapping_readiness']], [False, False])

    def test_an_approval_for_one_configuration_does_not_cover_another(self):
        digest = self.approve(self.config)                                               # single-list snapshot approved
        code, body = self.go(self.deps2())
        self.assertEqual((code, self.failed(body)), (409, [APPROVAL])); self.assertNotEqual(body['snapshot_digest'], digest)

    def test_registry_entry_for_the_other_list_only(self):
        body = self.refused(self.deps2(entries=[ENTRY]), ['exact_verified_registry_entry'])
        self.assertEqual([m['ready'] for m in body['mapping_readiness']], [True, False])


class CachedReceiptsAndIsolation(Base):
    def test_a_cached_receipt_cannot_be_replayed_once_the_schema_changes(self):
        code, first = self.go(run_id='shared')
        reads = list(self.reads)
        self.assertEqual((code, first['status'], bool(reads)), (200, 'ran', True))
        code, again = self.go(run_id='shared')                                           # same snapshot: replayed from its receipt
        self.assertEqual((code, again['summary'].get('replayed'), self.reads), (200, True, reads))
        changed = self.with_schema(lambda s: s['columns']['ActualScan'].update(defaultValue={'formula': '=[Now]'}))
        code, body = self.go(self.deps(schemas=[changed]), run_id='shared')
        self.assertEqual((code, body['status'], body['snapshot_digest']), (409, 'not_activated', None))
        self.assertNotIn('summary', body); self.assertEqual(self.reads, reads)
        code, body = self.go(self.deps(schemas=None), run_id='shared')
        self.assertEqual((code, body['status']), (409, 'not_activated')); self.assertNotIn('summary', body); self.assertEqual(self.reads, reads)

    def test_a_receipt_from_one_snapshot_identity_is_not_reused_for_another(self):
        self.go(run_id='shared'); reads = list(self.reads)
        later = dict(self.schema, captured_at='2026-11-10T21:20:00+00:00')              # same columns, a different capture
        digest = self.call(self.deps(schemas=[later]))[1]['snapshot_digest']
        self.approvals.records[digest] = dict(snapshot_digest=digest, approved_by='synthetic approver', approved_at='2026-11-11', principals=['alice'])
        code, body = self.go(self.deps(schemas=[later]), run_id='shared')
        self.assertEqual((code, body['status']), (200, 'ran'))
        self.assertNotIn('replayed', body['summary']); self.assertGreater(len(self.reads), len(reads))   # its own receipt, its own read

    def test_quota_and_nonce_are_not_spent_by_a_blocked_mapping(self):
        deps = lambda: self.deps(quota=Quota(1), schemas=[self.with_schema(lambda s: s['columns'].pop('Rejected'))])
        for _ in range(3):
            self.assertEqual(self.go(deps())[0], 409)
        self.assertEqual(self.rows(), (0, 0))
        self.assertEqual(self.go(self.deps(quota=Quota(1)))[0], 200)

    def test_principals_and_other_gates_still_apply_with_a_ready_mapping(self):
        self.assertEqual(self.failed(self.go(headers=BOB)[1]), [APPROVAL])              # bob is not named in the approval
        self.assertEqual(self.go(self.deps(config=dict(self.config, enabled=False)))[0], 409)
        self.assertEqual(self.go(self.deps(authorizer=None))[0], 503)
        self.assertEqual(Deps().schemas, None)


if __name__ == '__main__':
    unittest.main()
