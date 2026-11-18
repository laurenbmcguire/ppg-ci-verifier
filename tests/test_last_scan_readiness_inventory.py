"""The offline readiness inventory: useful blocked output, bad input, redaction, and no side effects."""
import builtins
import importlib.util
import io
import json
import socket
import sqlite3
import subprocess
import unittest
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / 'scripts' / 'last_scan_readiness_inventory.py'
EXAMPLE = ROOT / 'docs' / 'examples' / 'last_scan_readiness_packet.synthetic.json'
NOW = '2026-11-10T19:00:00+00:00'
_spec = importlib.util.spec_from_file_location('last_scan_readiness_inventory', SCRIPT)
inv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(inv)
BAD = (None, 5, 'x', True, [], {}, [None], [5], {'a': None}, [[1]], 1.5)


def example():
    return json.loads(EXAMPLE.read_text())


def item(report, ident):
    return next(x for x in report['items'] if x['id'] == ident)


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = inv.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def never_ready(test, report):
    test.assertIs(report['verdict']['ready_for_pilot'], False)
    test.assertGreater(report['verdict']['launch_blockers'], 0)
    for x in report['items']:
        test.assertTrue(x['levels']['connected'].startswith('unknown'), x['id'])
        test.assertEqual(x['levels']['production_authorized'], 'no', x['id'])
        test.assertIn(x['levels']['independently_verified'], ('no', inv.CLAIMED), x['id'])
        test.assertNotIn(x['levels']['configured'], ('yes', 'verified'), x['id'])


class ActionableBlockedOutput(unittest.TestCase):
    def test_the_synthetic_example_is_blocked_and_says_what_to_ask(self):
        report = inv.inventory(example(), NOW)
        never_ready(self, report)
        self.assertIs(report['verdict']['synthetic_packet'], True)
        self.assertIn('synthetic', ' '.join(item(report, 'packet')['blockers']))
        # the real gate ran on packet data: mapping and the presented approval pass there, and that changes nothing
        checks = {c['check']: c for c in report['hosted_preflight']}
        self.assertTrue(checks['source_mapping_ready_for_every_assignment']['ok'])
        self.assertEqual(item(report, 'assignment[0].mapping')['levels']['configured'], 'yes_as_supplied')
        for name in ('live_schema_probe_configured', 'source_adapter_configured', 'durable_store_ready', 'quota_configured'):
            self.assertFalse(checks[name]['ok'])
            self.assertTrue(checks[name]['standing'].startswith('not_connected'))
        self.assertTrue(report['next_steps'] and len(report['next_steps']) <= 8)
        self.assertTrue(all(q['ask'].strip() and q['id'] for q in report['operator_questions']))
        self.assertEqual(len({q['ask'] for q in report['operator_questions']}), len(report['operator_questions']))
        self.assertTrue(report['residual_limits'] and report['not_performed'] and report['unknowns'])
        self.assertEqual(report, inv.inventory(example(), NOW))               # deterministic

    def test_exact_gaps_per_client_job_and_shift(self):
        report = inv.inventory(example(), NOW)
        bravo = item(report, 'assignment[1].shift[1]')
        self.assertIn('BRAVO-REHEARSAL / RH-7002 shift 1', bravo['title'])
        self.assertTrue(any('No shift end is configured for: 2026-11-10' in b for b in bravo['blockers']))
        self.assertEqual(bravo['levels']['configured'], 'no')
        alpha = item(report, 'assignment[0].shift[1]')
        self.assertFalse(any('No shift end is configured' in b for b in alpha['blockers']))
        self.assertTrue(any('lunch' in q and 'offline' in q for q in alpha['needed']))
        self.assertEqual(alpha['evidence'][0]['kind'], 'self_attested')
        self.assertEqual(alpha['levels']['independently_verified'], 'no')
        for i in (0, 1):
            for part in ('mapping', 'report', 'registry', 'recipient'):
                self.assertTrue(item(report, 'assignment[%d].%s' % (i, part))['blockers'])
        self.assertTrue(any('photo' in b.lower() for b in item(report, 'assignment[0].report')['blockers']))

    def test_removed_pieces_are_named(self):
        packet = example()
        del packet['config']['assignments'][0]['header']['worksite']
        packet['job_registry'] = packet['job_registry'][1:]
        packet['recipient_policy']['ALPHA-REHEARSAL'] = 'client@example.invalid'
        packet['approvals'] = []
        packet['provider_contract'] = None
        report = inv.inventory(packet, NOW)
        never_ready(self, report)
        self.assertIn('Report header is missing: worksite.', item(report, 'assignment[0].report')['blockers'])
        self.assertTrue(any('No verified job registry entry' in b for b in item(report, 'assignment[0].registry')['blockers']))
        self.assertTrue(any('never a recipient' in b for b in item(report, 'assignment[0].recipient')['blockers']))
        self.assertTrue(any('No presented approval covers' in b for b in item(report, 'approval_authority')['blockers']))
        self.assertTrue(any('No provider contract metadata' in b for b in item(report, 'provider')['blockers']))
        self.assertFalse({c['check']: c for c in report['hosted_preflight']}['source_mapping_ready_for_every_assignment']['ok'])

    def test_a_packet_that_claims_everything_is_still_not_ready(self):
        packet = example()
        packet['synthetic'] = False
        said = dict(kind='independent', by='Somebody Else', at='2026-11-10T12:00:00+00:00', claim='checked', reference='REF-1', verified=True, signature='abc', sha256='0' * 64)
        for a in packet['config']['assignments']:
            for topic in ('scan_time_meaning', 'report_template'):
                packet['evidence'].append(dict(said, topic=topic, client_id=a['client_id'], job=a['job']))
            for shift in a['recorded_shifts']:
                packet['evidence'].append(dict(said, topic='shift_schedule', client_id=a['client_id'], job=a['job'], shift=shift))
        packet['operations'] = {k: dict(decided=True, by='X', at='2026-11-10T12:00:00+00:00', reference='R') for k in inv.OPERATIONS}
        packet['production_authorized'] = packet['connected'] = packet['ready_for_pilot'] = True
        report = inv.inventory(packet, NOW)
        never_ready(self, report)
        self.assertIs(report['verdict']['synthetic_packet'], False)
        self.assertEqual(item(report, 'assignment[0].mapping')['levels']['independently_verified'], inv.CLAIMED)
        self.assertTrue(any('Unknown top-level entries' in b for b in item(report, 'packet')['blockers']))
        for x in report['items']:
            for e in x['evidence']:
                self.assertEqual(e['standing'], 'presented in the packet; not verified by this command')
        self.assertTrue(item(report, 'credentials')['blockers'][0].startswith('Credential status is UNKNOWN'))
        self.assertEqual(item(report, 'credentials')['levels']['configured'], 'unknown_never_read')
        code, out, _ = run_packet(self, packet, '--format', 'text')
        self.assertEqual(code, 1)
        self.assertIn('NOT READY FOR A PILOT', out)

    def test_evidence_age_and_provenance_are_reported_not_trusted(self):
        packet = example()
        packet['evidence'] = [dict(topic='scan_time_meaning', client_id='ALPHA-REHEARSAL', job='RH-7001', kind='notarized', by='A', at='2027-01-01T00:00:00+00:00', claim='c'),
                              dict(topic='scan_time_meaning', client_id='ALPHA-REHEARSAL', job='RH-7001', kind='independent', by='B', at='yesterday', claim='c'),
                              'not an entry', None]
        found = item(inv.inventory(packet, NOW), 'assignment[0].mapping')['evidence']
        self.assertEqual([e['kind'] for e in found], ['unstated', 'independent'])
        self.assertIs(found[0]['when']['in_the_future'], True)
        self.assertTrue(found[1]['when']['age'].startswith('unknown'))

    def test_an_expired_presented_approval_is_reported(self):
        report = inv.inventory(example(), '2026-12-25T00:00:00+00:00')
        never_ready(self, report)
        facts = item(report, 'approval_authority')['facts']
        self.assertTrue(facts['presented'][0]['problems'])
        self.assertIsNone(facts['covering_record'])
        self.assertTrue(any('No presented approval covers' in b for b in item(report, 'approval_authority')['blockers']))


def run_packet(test, packet, *flags):
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / 'packet.json'
        path.write_text(packet if isinstance(packet, str) else json.dumps(packet))
        return run(str(path), '--now', NOW, *flags)


class AbsentAndInvalidInput(unittest.TestCase):
    def test_non_packets_and_wrong_versions(self):
        for packet in (None, [], 'x', 5, {}, {'packet': inv.PACKET}, {'packet': inv.PACKET, 'version': True}, {'packet': inv.PACKET, 'version': 2},
                       {'packet': inv.PACKET, 'version': '1'}, {'packet': 'other', 'version': 1}):
            with self.subTest(packet=packet):
                report = inv.inventory(packet, NOW)
                never_ready(self, report)
                self.assertTrue(any('The packet' in b for b in item(report, 'packet')['blockers']))
                self.assertTrue(any('No assignment is configured' in b for b in item(report, 'assignments')['blockers']))
                self.assertTrue(inv.text(report))

    def test_every_top_level_entry_survives_bad_values(self):
        for key in sorted(inv.KNOWN_KEYS):
            for bad in BAD:
                with self.subTest(key=key, bad=bad):
                    packet = example()
                    packet[key] = bad
                    report = inv.inventory(packet, NOW)
                    never_ready(self, report)
                    json.dumps(report)
                    inv.text(report)
            packet = example()
            del packet[key]
            never_ready(self, inv.inventory(packet, NOW))

    def test_every_assignment_field_survives_bad_values(self):
        for key in sorted(example()['config']['assignments'][0]):
            for bad in BAD:
                with self.subTest(key=key, bad=bad):
                    packet = example()
                    packet['config']['assignments'][0][key] = bad
                    report = inv.inventory(packet, NOW)
                    never_ready(self, report)
                    json.dumps(report)
                    inv.text(report)
        for bad in BAD:
            packet = example()
            packet['config']['assignments'] = bad
            never_ready(self, inv.inventory(packet, NOW))

    def test_command_line_failures(self):
        code, out, err = run('/nonexistent/readiness-packet.json', '--now', NOW)
        self.assertEqual((code, out), (2, ''))
        self.assertIn('could not be read', err)
        code, out, err = run(str(EXAMPLE), '--now', 'tomorrow')
        self.assertEqual((code, out), (2, ''))
        code, out, err = run(str(EXAMPLE), '--now', '2026-11-10T19:00:00')         # no offset
        self.assertEqual(code, 2)
        code, out, _ = run_packet(self, '{not json')
        self.assertEqual(code, 1)
        self.assertIn('The packet is not a JSON object.', out)
        with self.assertRaises(SystemExit):
            with redirect_stderr(io.StringIO()):
                inv.main([])

    def test_formats_and_exit_code(self):
        code, out, _ = run(str(EXAMPLE), '--now', NOW, '--format', 'json')
        self.assertEqual(code, 1)
        report = json.loads(out)
        self.assertEqual(report, json.loads(json.dumps(inv.inventory(example(), NOW))))
        code, text, _ = run(str(EXAMPLE), '--now', NOW)
        self.assertEqual(code, 1)
        for part in ('NOT READY FOR A PILOT', 'SYNTHETIC PACKET', 'WHAT TO ASK OR SUPPLY NEXT', 'HOSTED PREFLIGHT', 'LIMITS THAT REMAIN', 'Credential status is UNKNOWN'):
            self.assertIn(part, text)
        code, both, _ = run(str(EXAMPLE), '--now', NOW, '--format', 'both')
        self.assertEqual(code, 1)
        self.assertTrue(both.startswith(text))
        code, _, _ = run(str(EXAMPLE))                                            # the current time: approval long expired or not, still 1
        self.assertEqual(code, 1)


class SecretsAreNeverEchoed(unittest.TestCase):
    SECRETS = ('postgresql://ci_user:hunter2-very-secret@db.example.invalid/prod', 'sk-abcdefghijklmnopqrstuvwxyz012345', 'Bearer abc.def.ghi-not-a-real-one',
               'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJlLW5vdC1yZWFs', 'plain-words-under-a-secret-key', 'QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVphYmNkZWZnaGlqa2xtbm9wcXJzdHV2d3h5ejAxMjM0NTY3ODk=',
               '-----BEGIN RSA PRIVATE KEY-----\nMIIB\n-----END RSA PRIVATE KEY-----', 'ordinary words kept under a key named api_key')

    def packet(self):
        p = example()
        s = self.SECRETS
        p['config']['database_url'] = s[0]
        p['operations']['hosting'] = dict(decided=True, by='X', at='2026-11-10T12:00:00+00:00', reference=s[1])
        p['evidence'].append(dict(topic='scan_time_meaning', client_id='ALPHA-REHEARSAL', job='RH-7001', kind='independent', by='B', at='2026-11-10T12:00:00+00:00', claim=s[2], reference=s[3]))
        p['config']['assignments'][0]['header']['client_secret'] = s[4]
        p['prepared_by'] = s[5]
        p['provider_contract']['private_key'] = s[6]
        p['pilot_principal'] = s[2]
        p['accepted_collectors'] = list(p['accepted_collectors']) + [{'api_key': s[7]}]     # this entry is echoed in the report's facts
        return p

    def test_values_never_reach_either_output_and_locations_do(self):
        packet = self.packet()
        before = deepcopy(packet)
        report = inv.inventory(packet, NOW)
        self.assertEqual(packet, before)                                          # the caller's packet is not changed
        never_ready(self, report)
        code, both, err = run_packet(self, packet, '--format', 'both')
        self.assertEqual(code, 1)
        for rendered in (json.dumps(report), inv.text(report), both, err):
            for secret in self.SECRETS:
                self.assertNotIn(secret, rendered)
                self.assertNotIn(secret.split('\n')[0], rendered)
            self.assertNotIn('hunter2', rendered)
        where = item(report, 'packet')['facts']['secret_locations']
        for path in ('packet.config.database_url', 'packet.operations.hosting.reference', 'packet.config.assignments[0].header.client_secret',
                     'packet.prepared_by', 'packet.provider_contract.private_key', 'packet.pilot_principal', 'packet.accepted_collectors[1].api_key'):
            self.assertIn(path, where)
        self.assertTrue(any('secret-looking value' in b for b in item(report, 'packet')['blockers']))
        self.assertEqual(item(report, 'packet')['levels']['configured'], 'no')

    def test_ordinary_identifiers_are_not_mistaken_for_secrets(self):
        self.assertEqual(item(inv.inventory(example(), NOW), 'packet')['facts']['secret_locations'], [])
        clean, found = inv.redact({'sha256': 'a' * 64, 'site_id': 'example.sharepoint.com,a1a1a1a1-0000-4000-8000-000000007001,b1b1b1b1-0000-4000-8000-000000007001', 'token': '', 'password': None})
        self.assertEqual(found, [])


class MalformedProviderMetadata(unittest.TestCase):
    """QA d496b2c (1) and (2): malformed contract metadata is a blocked report, and a constructor is not a validation."""

    def provider(self, **change):
        packet = example()
        packet['provider_contract'].update(change)
        report = inv.inventory(packet, NOW)
        never_ready(self, report)
        json.dumps(report)
        inv.text(report)
        return item(report, 'provider')

    def test_the_exact_qa_cases_do_not_throw(self):
        for change in (dict(provider=[]), dict(version={}), dict(provider=[], version={}), dict(provider={'a': []}), dict(version=[[1]])):
            with self.subTest(change=change):
                found = self.provider(**change)
                self.assertEqual(found['levels']['configured'], 'no')
                self.assertIs(found['facts']['listed_in_code'], False)
                self.assertTrue(any('provider contract metadata is not valid' in b for b in found['blockers']))

    def test_every_contract_field_with_every_bad_value(self):
        for name in sorted(example()['provider_contract']):
            for bad in BAD + (float('nan'), float('inf'), -1, 0, ''):
                with self.subTest(name=name, bad=bad):
                    found = self.provider(**{name: bad})
                    self.assertIs(found['facts']['listed_in_code'], False)
                    self.assertTrue(found['levels']['connected'].startswith('unknown'))
                    if type(bad) is not type(example()['provider_contract'][name]):
                        self.assertEqual(found['levels']['configured'], 'no', (name, bad))
        for whole in BAD + ([example()['provider_contract']], {'provider': 'p'}, {'unknown_field': 1}):
            with self.subTest(whole=whole):
                packet = example()
                packet['provider_contract'] = whole
                found = item(inv.inventory(packet, NOW), 'provider')
                self.assertEqual(found['levels']['configured'], 'no')

    def test_a_deadline_that_is_not_a_positive_whole_number_is_not_configured(self):
        for bad in ('never', '60', 0, -5, True, False, 1.5, 60.0, float('nan'), float('inf'), float('-inf'), None, [], {}):
            with self.subTest(bad=bad):
                found = self.provider(sender_timeout_seconds=bad)
                self.assertEqual(found['levels']['configured'], 'no')
                self.assertTrue(any('sender_timeout_seconds' in b for b in found['blockers']), found['blockers'])
                self.assertTrue(found['levels']['connected'].startswith('unknown'))
                self.assertEqual(found['levels']['production_authorized'], 'no')

    def test_other_contract_rules_decide_the_label(self):
        self.assertEqual(self.provider()['levels']['configured'], 'yes_as_supplied')      # the synthetic contract meets the rules as supplied
        self.assertEqual(self.provider(sender_timeout_seconds=10 ** 400)['levels']['configured'], 'yes_as_supplied')   # no existing rule caps it here; nothing throws
        for change, word in ((dict(at_most_once_per_idempotency_key=False), 'at_most_once_per_idempotency_key'), (dict(verified_by='  '), 'verified_by'),
                             (dict(evidence=''), 'evidence'), (dict(outcome_contract_version=0), 'outcome_contract_version'),
                             (dict(outcome_contract_version=99), 'outcome_contract_version'), (dict(valid_until='2026-11-10T18:00:00+00:00'), 'valid_until'),
                             (dict(valid_until='soon'), 'valid_until'), (dict(valid_until=''), 'valid_until'), (dict(extra_field=1), 'unknown')):
            with self.subTest(change=change):
                found = self.provider(**change)
                self.assertEqual(found['levels']['configured'], 'no')
                self.assertTrue(any(word in b for b in found['blockers']), found['blockers'])

    def test_every_value_anywhere_in_the_packet_can_be_malformed(self):
        """Replace each value at each path in the example, one at a time. Nothing may throw, and nothing may become ready."""
        def paths(node, here=()):
            if isinstance(node, dict):
                for k, v in node.items():
                    yield here + (k,)
                    yield from paths(v, here + (k,))
            elif isinstance(node, list):
                for i, v in enumerate(node):
                    yield here + (i,)
                    yield from paths(v, here + (i,))
        base, count = example(), 0
        for path in paths(base):
            for bad in (None, 5, 'x', [], {}, [[1]], {'a': []}, float('nan')):
                packet = deepcopy(base)
                node = packet
                for step in path[:-1]:
                    node = node[step]
                node[path[-1]] = bad
                try:
                    report = inv.inventory(packet, NOW)
                    json.dumps(report)
                    inv.text(report)
                except Exception as error:                                         # noqa: BLE001 - the point of the test
                    self.fail('%r = %r raised %s: %s' % (path, bad, type(error).__name__, error))
                self.assertIs(report['verdict']['ready_for_pilot'], False)
                count += 1
        self.assertGreater(count, 2000)


class SecretLookingKeys(unittest.TestCase):
    """QA d496b2c (3): a secret pasted as a JSON KEY must not come back in a message, a path, or anywhere else."""
    KEYS = ('Bearer SYNTHETIC_KEY_VALUE', 'eyJTWU5USEVUSUM.eyJTWU5USEVUSUMx.U1lOVEhFVElDLXNpZw', 'https://synthetic-user:SYNTHETIC-PASS@host.example.invalid/x',
            'password=SYNTHETIC-PASSWORD-VALUE', 'api_key: SYNTHETIC-API-KEY-VALUE', 'sk-SYNTHETICsyntheticSYNTHETIC0123', 'U1lOVEhFVElDLUtFWS1WQUxVRS1TWU5USEVUSUMtS0VZLVZBTFVFLVNZTlRIRVRJQw==',
            '-----BEGIN PRIVATE KEY----- SYNTHETIC', 'token SYNTHETIC-TOKEN-VALUE-1234567890')
    PARTS = ('SYNTHETIC_KEY_VALUE', 'eyJTWU5USEVUSUM', 'SYNTHETIC-PASS', 'SYNTHETIC-PASSWORD-VALUE', 'SYNTHETIC-API-KEY-VALUE', 'sk-SYNTHETIC', 'U1lOVEhFVElD', 'BEGIN PRIVATE KEY', 'SYNTHETIC-TOKEN-VALUE')

    def clean(self, packet):
        before = deepcopy(packet)
        report = inv.inventory(packet, NOW)
        self.assertEqual(packet, before)
        never_ready(self, report)
        code, both, err = run_packet(self, packet, '--format', 'both')
        self.assertEqual(code, 1)
        for rendered in (json.dumps(report), inv.text(report), both, err):
            for part in self.PARTS + self.KEYS:
                self.assertNotIn(part, rendered)
        return report

    def test_the_exact_qa_case_an_unknown_top_level_key(self):
        packet = example()
        packet['Bearer SYNTHETIC_KEY_VALUE'] = 'x'
        report = self.clean(packet)
        blockers = item(report, 'packet')['blockers']
        self.assertTrue(any('Unknown top-level entries' in b for b in blockers))
        self.assertTrue(any('secret-looking' in b for b in blockers))
        self.assertEqual(item(report, 'packet')['levels']['configured'], 'no')
        self.assertEqual(len(item(report, 'packet')['facts']['secret_locations']), 1)

    def test_secret_looking_keys_at_every_depth(self):
        for key in self.KEYS:
            for place in ('top', 'config', 'header', 'mapping', 'not_before', 'recipient_policy', 'operations', 'operation', 'evidence', 'provider', 'issuers', 'schema', 'collector'):
                with self.subTest(key=key[:12], place=place):
                    p = example()
                    a = p['config']['assignments'][0]
                    target = dict(top=p, config=p['config'], header=a['header'], mapping=a['mapping'], not_before=a['not_before'], recipient_policy=p['recipient_policy'],
                                  operations=p['operations'], operation=p['operations']['scheduler'], evidence=p['evidence'][0], provider=p['provider_contract'],
                                  issuers=p['accepted_issuers'], schema=p['schemas'][0])
                    if place == 'collector':
                        p['accepted_collectors'] = [{key: {key: [{key: 'x'}]}}]
                    else:
                        target[place][key] = {key: 'x'} if place in ('top', 'config', 'operations') else 'x'
                    report = self.clean(p)
                    self.assertTrue(item(report, 'packet')['facts']['secret_locations'])
                    self.assertTrue(any('secret-looking' in b for b in item(report, 'packet')['blockers']))

    def test_several_such_keys_in_one_object_stay_distinct_and_named_keys_stay_readable(self):
        clean, found = inv.redact({self.KEYS[0]: 1, self.KEYS[1]: {'api_key': 'v'}, 'ordinary name': 2, '2026-11-10|1': 3, 'ALPHA-REHEARSAL': 4})
        self.assertEqual(len(clean), 5)
        self.assertEqual({k for k in clean if not k.startswith('[REDACTED-KEY')}, {'ordinary name', '2026-11-10|1', 'ALPHA-REHEARSAL'})
        self.assertEqual(len(found), 2)                                          # one per redacted key; nothing beneath one is walked
        self.assertTrue(all(not any(part in where for part in self.PARTS) for where in found))
        self.assertFalse(any('api_key' in where for where in found))


class RedactedKeysTakeTheirValuesWithThem(unittest.TestCase):
    """QA ff298be: when a key is redacted, everything under it goes too. Nothing below it is walked, echoed or located."""
    KEY = 'password=SYNTHETIC_KEY'
    PLAIN = 'SYNTHETIC_PLAIN_SECRET'

    def clean(self, packet, locations=1):
        before = deepcopy(packet)
        report = inv.inventory(packet, NOW)
        self.assertEqual(packet, before)
        never_ready(self, report)
        code, both, err = run_packet(self, packet, '--format', 'both')
        self.assertEqual(code, 1)
        for rendered in (json.dumps(report), inv.text(report), both, err):
            for part in (self.PLAIN, 'SYNTHETIC_KEY', 'SYNTHETIC_INNER_NAME', self.KEY):
                self.assertNotIn(part, rendered)
        where = item(report, 'packet')['facts']['secret_locations']
        self.assertEqual(len(where), locations, where)
        self.assertTrue(all(w.endswith(']') and 'REDACTED-KEY' in w.rsplit('.', 1)[-1] for w in where), where)   # the location stops at the key
        return report

    def test_the_exact_qa_case(self):
        packet = example()
        packet['prepared_by'] = {self.KEY: self.PLAIN}
        report = self.clean(packet)
        self.assertEqual(item(report, 'packet')['facts']['prepared_by'], {'[REDACTED-KEY-1]': '[REDACTED]'})

    def test_nested_lists_and_objects_under_a_redacted_key(self):
        below = (self.PLAIN, [self.PLAIN], [[self.PLAIN, 1], {'note': self.PLAIN}], {'SYNTHETIC_INNER_NAME': self.PLAIN}, {'a': {'b': [{'c': self.PLAIN}]}},
                 {'ordinary': 'x', 'list': [self.PLAIN, {'SYNTHETIC_INNER_NAME': [self.PLAIN]}]}, 5, None, True, [], {})
        for value in below:
            for place in ('prepared_by', 'top', 'header', 'evidence', 'collectors', 'operation', 'provider', 'recipient_policy', 'registry'):
                with self.subTest(value=str(type(value)), place=place):
                    p = example()
                    wrapped = {self.KEY: value}
                    if place == 'prepared_by':
                        p['prepared_by'] = wrapped
                    elif place == 'top':
                        p.update(wrapped)
                    elif place == 'header':
                        p['config']['assignments'][0]['header'].update(wrapped)
                    elif place == 'evidence':
                        p['evidence'][0]['claim'] = wrapped
                    elif place == 'collectors':
                        p['accepted_collectors'] = [wrapped, [wrapped]]
                    elif place == 'operation':
                        p['operations']['scheduler']['reference'] = wrapped
                    elif place == 'provider':
                        p['provider_contract'].update(wrapped)
                    elif place == 'recipient_policy':
                        p['recipient_policy'].update(wrapped)
                    else:
                        p['job_registry'].append(wrapped)
                    self.clean(p, locations=2 if place == 'collectors' else 1)

    def test_the_whole_subtree_is_one_marker(self):
        clean, found = inv.redact({self.KEY: {'api_key': 'v', 'deep': [{'x': self.PLAIN}]}, 'kept': {'api_key': 'v'}})
        self.assertEqual(clean, {'[REDACTED-KEY-1]': '[REDACTED]', 'kept': {'api_key': '[REDACTED]'}})
        self.assertEqual(found, ['packet.[REDACTED-KEY-1]', 'packet.kept.api_key'])


class ProviderReviewMarginMatchesTheSendDoor(unittest.TestCase):
    """QA ff298be: a review that ends within the send door's margin is not 'configured'. Strictly more than 60 seconds."""

    def label(self, seconds):
        from datetime import timedelta
        packet = example()
        packet['provider_contract']['valid_until'] = (inv.instant(NOW) + timedelta(seconds=seconds)).isoformat()
        found = item(inv.inventory(packet, NOW), 'provider')
        return found['levels']['configured'], found['blockers']

    def test_the_margin_is_the_send_doors_own(self):
        from hosting import last_scan_dispatch as door
        self.assertEqual(inv.SEND_MARGIN_SECONDS, door.GRANT_SECONDS)
        self.assertEqual(inv.SEND_MARGIN_SECONDS, 60)

    def test_boundaries(self):
        for seconds in (-1, 0, 1, 59, 59.999, 60):
            with self.subTest(seconds=seconds):
                label, blockers = self.label(seconds)
                self.assertEqual(label, 'no')
                self.assertTrue(any('valid_until' in b for b in blockers), blockers)
        for seconds in (60.001, 61, 3600):
            with self.subTest(seconds=seconds):
                label, blockers = self.label(seconds)
                self.assertEqual(label, 'yes_as_supplied')
                self.assertTrue(any('not a review' in b for b in blockers))


class TheOneHourRuleIsARequirement(unittest.TestCase):
    """One hour after the last scan is the fixed user requirement. The output must say so, never call it a placeholder
    or an unrecorded decision, and still name what is unverified around it."""

    def outputs(self, packet=None):
        report = inv.inventory(example() if packet is None else packet, NOW)
        return report, inv.text(report), json.dumps(report)

    def test_the_requirement_is_stated_and_matches_the_timer_that_is_not_changed_here(self):
        from datetime import timedelta
        self.assertEqual(inv.WAIT, timedelta(hours=1))
        report, text, dumped = self.outputs()
        self.assertIn('one hour after the last scan', report['requirement'])
        self.assertIn('fixed user requirement', report['requirement'])
        self.assertIn('not a placeholder', report['requirement'])
        self.assertIn(report['requirement'], text)
        for empty in (None, {}, []):
            self.assertEqual(self.outputs(empty)[0]['requirement'], report['requirement'])

    def test_no_output_calls_the_hour_a_placeholder_or_an_open_decision(self):
        for packet in (None, {}, dict(example(), synthetic=False)):
            report, text, dumped = self.outputs(packet)
            for rendered in (text, dumped):
                for wrong in ('60 minutes quiet', 'minutes quiet', 'quiet minutes', 'operating decision nobody has recorded', 'means \\"the shift is finished\\"',
                              'means "the shift is finished"', 'early draft'):
                    self.assertNotIn(wrong, rendered)
            placeholders = [r for r in report['residual_limits'] if 'placeholder' in r]
            self.assertEqual(len(placeholders), 1)
            self.assertNotIn('60 minutes', placeholders[0].split('not one of them')[0])
            self.assertIn('one-hour wait after the last scan is not one of them', placeholders[0])
            for needed in ('24 hours', '7 days', '5 minutes', '60 seconds'):
                self.assertIn(needed, placeholders[0])

    def test_what_is_still_unverified_stays_actionable(self):
        report, text, _ = self.outputs()
        mapping = item(report, 'assignment[0].mapping')
        self.assertTrue(any('scan column records the moment of the actual scan' in b for b in mapping['blockers']))
        self.assertTrue(any('which column the tablet fills at the moment of the scan' in q for q in mapping['needed']))
        for ident in ('assignment[0].shift[1]', 'assignment[1].shift[1]', 'assignment[1].shift[2]'):
            shift = item(report, ident)
            unverified = [b for b in shift['blockers'] if b.startswith('Not yet verified')]
            self.assertEqual(len(unverified), 1, shift['blockers'])
            for word in ('shift end', 'lunch', 'offline', 'late-arriving', 'one-hour wait itself is fixed'):
                self.assertIn(word, unverified[0])
            self.assertTrue(any('scheduled end time' in q and 'lunch' in q and 'offline' in q for q in shift['needed']))
            self.assertEqual(shift['facts']['wait_after_last_scan_minutes'], 60)
            self.assertEqual(shift['facts']['wait_is'], 'fixed user requirement')
            self.assertNotIn('quiet_minutes', shift['facts'])
            self.assertEqual(shift['status'], 'blocked')
        missing = [b for b in item(report, 'assignment[1].shift[1]')['blockers'] if b.startswith('No shift end is configured for: 2026-11-10')]
        self.assertEqual(len(missing), 1)
        self.assertIn('still due 60 minutes after the last scan', missing[0])
        self.assertIn('supersede', missing[0])
        self.assertIn('Not yet verified', text)

    def test_the_documents_say_the_same(self):
        docs = ROOT / 'docs'
        for name in ('LAST_SCAN_PILOT_HANDOFF.md', 'LAST_SCAN_ACTIVATION_CHECKLIST.md', 'LAST_SCAN_REHEARSAL.md', 'LAST_SCAN_DRAFT_PIPELINE.md'):
            body = ' '.join((docs / name).read_text().split())
            with self.subTest(doc=name):
                self.assertRegex(body, r"(?i)one hour after the last scan")
                self.assertRegex(body, r"(?i)requirement")
                self.assertNotRegex(body, r"(?i)60 minutes quiet")
                self.assertNotRegex(body, r"(?i)quiet[- ](period|time|hour)s? (is|are) (a )?placeholder")
                self.assertRegex(body, r"(?i)not a statement about any live environment")
        handoff = ' '.join((docs / 'LAST_SCAN_PILOT_HANDOFF.md').read_text().split())
        self.assertIn('Never bypass an authorization, source-scope or safety gate', handoff)
        self.assertIn('report the exact blocker', handoff)
        self.assertNotIn('Stop the run itself', handoff)
        checklist = ' '.join((docs / 'LAST_SCAN_ACTIVATION_CHECKLIST.md').read_text().split())
        self.assertIn('Confirm the current scheduled reporting', checklist)
        self.assertNotIn('It already builds reports for a fixed list of jobs', checklist)


class NoSideEffects(unittest.TestCase):
    def test_no_network_no_store_no_process_and_no_write(self):
        real_open = builtins.open
        before = EXAMPLE.read_bytes()

        def refuse(*a, **k):
            raise AssertionError('the readiness inventory reached for something it must not touch')

        def read_only(file, mode='r', *a, **k):
            if set(mode) & set('wax+'):
                raise AssertionError('the readiness inventory tried to write ' + str(file))
            return real_open(file, mode, *a, **k)

        with mock.patch.object(socket, 'socket', refuse), mock.patch.object(socket, 'create_connection', refuse), mock.patch.object(socket, 'getaddrinfo', refuse), \
                mock.patch.object(urllib.request, 'urlopen', refuse), mock.patch.object(sqlite3, 'connect', refuse), mock.patch.object(subprocess, 'Popen', refuse), \
                mock.patch.object(builtins, 'open', read_only), mock.patch.object(io, 'open', read_only), \
                mock.patch.object(Path, 'write_text', refuse), mock.patch.object(Path, 'write_bytes', refuse), mock.patch.object(Path, 'mkdir', refuse), \
                mock.patch.object(Path, 'unlink', refuse), mock.patch('os.remove', refuse), mock.patch('os.rename', refuse), mock.patch('os.replace', refuse), \
                mock.patch('os.makedirs', refuse), mock.patch('os.mkdir', refuse), mock.patch('os.getenv', refuse):
            code, out, _ = run(str(EXAMPLE), '--now', NOW, '--format', 'both')
            self.assertEqual(code, 1)
            self.assertIn('NOT READY', out)
            for bad in (None, {}, {'packet': inv.PACKET, 'version': 1, 'config': {'assignments': [{}]}}):
                never_ready(self, inv.inventory(bad, NOW))
        self.assertEqual(EXAMPLE.read_bytes(), before)

    def test_the_packet_readers_cannot_store_or_authorize(self):
        with self.assertRaises(RuntimeError):
            inv._NoArtifacts().commit('k', {}, {})
        self.assertIsNone(inv._NoArtifacts().lookup('k'))
        self.assertIsNone(inv._PacketApprovals([]).attestation('d'))
        self.assertIsNone(inv._PacketCaptures(['x', None]).receipt('i'))

    def test_the_script_names_no_send_door_registry_network_or_environment(self):
        source = SCRIPT.read_text()
        for word in ('VERIFIED_CONTRACTS', 'contract_registry', 'last_scan_dispatch', 'last_scan_reconcile', 'last_scan_service', 'last_scan_store', 'dispatch_test',
                     'reconcile_test', 'transport.deliver(', 'grant_key(', 'urllib', 'requests', 'http.client', 'smtplib', 'socket', 'subprocess', 'os.environ', 'getenv',
                     'psycopg', 'sqlite', '.install(', 'write_text', 'write_bytes', "'w'", '"w"'):
            self.assertNotIn(word, source, word)

    def test_the_example_packet_is_marked_synthetic_and_invented(self):
        packet = example()
        self.assertIs(packet['synthetic'], True)
        self.assertEqual({a['client_id'] for a in packet['config']['assignments']}, {'ALPHA-REHEARSAL', 'BRAVO-REHEARSAL'})
        self.assertTrue(all('example.sharepoint.com' in a['site_id'] for a in packet['config']['assignments']))
        self.assertEqual(inv.redact(packet)[1], [])


if __name__ == '__main__':
    unittest.main()
