"""Integration readiness and evidence inventory for the last-scan draft runner. LOCAL, READ-ONLY, OFFLINE.

    PYTHONPATH=. python scripts/last_scan_readiness_inventory.py PACKET.json [--format text|json|both] [--now ISO-TIME]

It reads ONE file, the readiness packet (non-secret configuration, schema and contract metadata an operator has
gathered), runs the existing validators over it, and prints what still blocks a pilot and exactly what to ask or
supply next. It opens no store, calls no source, provider or network, sends nothing, writes nothing, and reads no key.

What it can and cannot establish. Five levels are reported for every item, and this command can only ever raise the
first two:
  code_tested              the code path exists and passes its synthetic tests. Nothing about any real system.
  configured               the packet contains it and the existing validators accept it AS SUPPLIED.
  connected                a real system answered with real credentials. NEVER shown here: no call is made.
  independently_verified   someone other than the configurer checked it against the real source. The packet can only
                           CLAIM this ("claimed_in_packet_not_verified"); a hash, a signature field, a "verified"
                           flag or a self-attestation in a file proves nothing about where the file came from.
  production_authorized    a real authority approved it and a reviewed provider contract is registered in code.
                           NEVER shown here: no real authority exists and the provider registry is empty.
So the verdict is always "not ready": the command finds blockers, it does not grant readiness, and a synthetic
packet demonstrates the report format and nothing else.

The hosted preflight section is the real gate (hosting.last_scan_hosted.preflight) run against packet readers that
only read the packet. They are not authorities: an approval or capture receipt in a packet is "presented", never
authenticated.
"""
import argparse
import dataclasses
import json
import math
import re
import sys
from copy import deepcopy
from pathlib import Path

from hosting import source_first as sf
from hosting import last_scan_authority as authority
from hosting import last_scan_hosted as hosted
from hosting import last_scan_readiness as readiness
from hosting import last_scan_runner as runner
from hosting import last_scan_transport as transport
from hosting.last_scan_drafts import WAIT, instant
from hosting.last_scan_local import ArtifactStore

PACKET, VERSION = 'last_scan.readiness_packet', 1
LEVELS = ('code_tested', 'configured', 'connected', 'independently_verified', 'production_authorized')
ORDER = ('packet', 'mapping', 'timing', 'report', 'authorities', 'delivery', 'operations')
TESTED = 'yes_synthetic_tests_only'
CLAIMED = 'claimed_in_packet_not_verified'
KNOWN_KEYS = {'packet', 'version', 'synthetic', 'prepared_by', 'prepared_at', 'pilot_principal', 'config', 'job_registry', 'schemas',
              'capture_receipts', 'approvals', 'accepted_collectors', 'accepted_issuers', 'provider_contract', 'recipient_policy', 'evidence', 'operations'}
OPERATIONS = dict(
    hosting='Where will the runner be hosted, and who reviewed the route that would reach it? (No route exists today.)',
    scheduler='What calls it, how often, and who owns that schedule? (The existing twice-daily reporting task is separate and must not change.)',
    durable_store='Which database holds its state, who approved the table migration, and is the connection-limit decision made?',
    recovery='Who is told when a site is on a provider wait, a draft is held or a delivery is uncertain, and who may release or reconcile it?',
    operator_workflow='Who are the named operators, how do they sign in, and where do they see held and uncertain items? (No operator screen exists.)',
    approvals_process='Who approves a configuration, a dispatch and a reconciliation, and in which system is that recorded outside the runner?',
    recipient='Which internal mailbox receives drafts, and who confirmed it? (Client release stays held; a client is never a recipient.)',
    retention='How long are drafts, photos and audit entries kept, and who may read them?')
CREDENTIALS = ('read-only source access for the one list in the pilot (existing read-only app registration)', 'the runner\'s own database connection',
               'the identity system operators and the scheduler sign in with', 'the delivery provider account', 'the approval and capture authorities')
SECRET_KEY = re.compile(r'(secret|passw|token|api[_-]?key|authorization|credential|private[_-]?key|database[_-]?url|connection[_-]?string|cookie|bearer)', re.I)
SECRET_VALUE = re.compile(r'(^\s*Bearer\s+\S|^eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}|://[^/\s:@]+:[^/\s@]+@|-----BEGIN [A-Z ]*PRIVATE KEY|^sk-[A-Za-z0-9_-]{16,}$)')
KEY_ASSIGN = re.compile(SECRET_KEY.pattern + r'[\w-]*(\s*[=:]\s*\S|\s+\S{12,})', re.I)   # a name with its value attached, pasted where a name belongs
HEX = re.compile(r'^[0-9a-fA-F]+$')
OPAQUE = re.compile(r'^[A-Za-z0-9+/=_-]{48,}$')
RESIDUAL = (
    'An approval or receipt withdrawn at its authority after the last re-check and before the read, claim or send is not seen until the next re-check.',
    'The source can change between the schema probe, the item read, the claim and the provider receiving the message.',
    'A host clock that runs fast, or slow by less than the allowed skew, is not detected.',
    'No exactly-once delivery: an uncertain delivery stays uncertain until a provider states its outcome and a person reconciles it.',
    'A provider that breaks its contract (delivers after stating not delivered, ignores its deadline) is recorded, not prevented.',
    'Every bound in the code (60 minutes quiet, 24 hours schema age, 7 days approval validity, 5 minutes skew, 60 seconds limits) is a placeholder awaiting an owner.')


class _PacketApprovals(hosted.ApprovalAuthority):
    """PACKET READER, NOT AN AUTHORITY: answers only from records presented in the packet file."""
    def __init__(self, records):
        self.records = [r for r in records if isinstance(r, dict)]

    def attestation(self, digest):
        return deepcopy(next((r for r in self.records if r.get('snapshot_digest') == digest), None))


class _PacketCaptures(hosted.CaptureAuthority):
    """PACKET READER, NOT AN AUTHORITY: answers only from receipts presented in the packet file."""
    def __init__(self, records):
        self.records = [r for r in records if isinstance(r, dict)]

    def receipt(self, identity):
        return deepcopy(next((r for r in self.records if r.get('snapshot_identity') == identity), None))


class _NoArtifacts(ArtifactStore):
    """Nothing is stored and nothing can be: lets the renderer versions be computed without any storage."""
    def lookup(self, key):
        return None

    def commit(self, key, files, receipt):
        raise RuntimeError('The readiness inventory writes nothing.')

    def read(self, key, name):
        raise LookupError('The readiness inventory stores nothing.')


def _secret_text(v):
    return bool(SECRET_VALUE.search(v) or (OPAQUE.match(v) and not HEX.match(v)))


def _secret_name(k):
    """A KEY that looks like a secret itself (pasted in the wrong place), as opposed to a key that merely names one."""
    return _secret_text(k) or bool(KEY_ASSIGN.search(k)) or len(k) > 120 or '\n' in k


def redact(value, path='packet', invalid=None):
    """(a deep copy with anything secret-looking replaced, [where it was]). Neither a secret-looking value nor a
    secret-looking KEY is returned, printed or used in a location: such a key becomes [REDACTED-KEY-n]."""
    found = []
    def walk(v, where):
        if isinstance(v, dict):
            out, n = {}, 0
            for k, x in v.items():
                if isinstance(k, str) and _secret_name(k):
                    n += 1
                    k = '[REDACTED-KEY-%d]' % n
                    here = '%s.%s' % (where, k)
                    found.append(here)
                    out[k] = walk(x, here)
                    continue
                here = '%s.%s' % (where, k)
                if isinstance(k, str) and SECRET_KEY.search(k) and x not in (None, '', False, [], {}):
                    found.append(here)
                    out[k] = '[REDACTED]'
                else:
                    out[k] = walk(x, here)
            return out
        if isinstance(v, list):
            return [walk(x, '%s[%d]' % (where, i)) for i, x in enumerate(v)]
        if isinstance(v, float) and not math.isfinite(v):                    # NaN and Infinity are not JSON; treated as absent and reported
            if invalid is not None:
                invalid.append(where)
            return None
        if isinstance(v, str) and _secret_text(v):
            found.append(where)
            return '[REDACTED]'
        return v
    return walk(value, path), found


def _age(stamp, now):
    try:
        seconds = (instant(now) - instant(stamp)).total_seconds()
    except (ValueError, OverflowError, TypeError):
        return dict(stated=stamp if isinstance(stamp, str) else None, age='unknown: missing, malformed or without an offset')
    return dict(stated=stamp, age_hours=round(seconds / 3600, 2), in_the_future=seconds < 0)


def _contract_problems(fields, now):
    """(why the presented provider metadata does not meet the contract's own rules, the contract or None). Never raises.
    Building the dataclass checks nothing, so every field is checked here against its declared type and the rules the
    transport already applies (verified, permits_retry, the outcome contract version, a review that has not lapsed).
    Whether the deadline fits an attempt is decided against the attempt's own length at dispatch, not here."""
    if not isinstance(fields, dict) or not fields:
        return ['No provider contract metadata is presented.'], None
    declared = {f.name: f for f in dataclasses.fields(transport.ProviderContract)}
    out, shape = [], []
    extra = sorted(str(k) for k in fields if k not in declared)
    if extra:
        shape.append('%d unknown field(s) that are not part of the contract: %s' % (len(extra), ', '.join(extra)))
    value = {}
    for name, f in declared.items():
        required = f.default is dataclasses.MISSING
        kind = str if required else type(f.default)
        if name not in fields:
            if required:
                shape.append(name + ' is missing')
            else:
                value[name] = f.default
        elif type(fields[name]) is not kind:
            shape.append('%s must be %s' % (name, {str: 'text', bool: 'true or false', int: 'a whole number'}[kind]))
        else:
            value[name] = fields[name]
    def has(name):
        return name in value
    for name in ('provider', 'version', 'verified_by', 'verified_at', 'evidence'):
        if has(name) and not value[name].strip():
            out.append(name + ' is blank')
    if has('at_most_once_per_idempotency_key') and value['at_most_once_per_idempotency_key'] is not True:
        out.append('at_most_once_per_idempotency_key is not true: no dispatch is allowed without it')
    if has('sender_timeout_seconds') and not value['sender_timeout_seconds'] > 0:
        out.append('sender_timeout_seconds must be a positive whole number: it is the deadline the sender enforces')
    if has('outcome_contract_version') and value['outcome_contract_version'] != transport.OUTCOME_VERSION:
        out.append('outcome_contract_version must be %d, the only outcome contract the transport judges' % transport.OUTCOME_VERSION)
    if has('valid_until'):                                                   # a dispatch needs it stated and in the future
        try:
            if not instant(value['valid_until']) > instant(now):
                out.append('valid_until has passed: the review has lapsed')
        except (ValueError, OverflowError, TypeError):
            out.append('valid_until is not stated as an offset-aware time')
    if shape:
        return shape + out, None
    try:
        return out, transport.ProviderContract(**value)
    except Exception:                                                        # fail closed: unusable metadata is a blocker, never a crash
        return out + ['the contract could not be built from these fields'], None


def _levels(configured, verified=False):
    return dict(code_tested=TESTED, configured='yes_as_supplied' if configured else 'no', connected='unknown_no_call_is_made',
                independently_verified=CLAIMED if verified else 'no', production_authorized='no')


def _evidence(packet, now, **match):
    """Evidence entries the packet presents for a topic. Reported with provenance and age; never treated as proof."""
    out = []
    for e in packet.get('evidence') if isinstance(packet.get('evidence'), list) else []:
        if isinstance(e, dict) and all(e.get(k) == v for k, v in match.items()):
            kind = e.get('kind') if e.get('kind') in ('independent', 'self_attested', 'synthetic') else 'unstated'
            out.append(dict(claim=e.get('claim'), by=e.get('by'), reference=e.get('reference'), kind=kind, when=_age(e.get('at'), now),
                            standing='presented in the packet; not verified by this command'))
    return out


def inventory(packet, now):
    """The whole report as plain data. Pure: reads its arguments, calls nothing, writes nothing."""
    items, shape = [], []
    def add(area, ident, title, levels, blockers=(), needed=(), evidence=(), facts=None):
        items.append(dict(area=area, id=ident, title=title, levels=levels, status='blocked' if blockers else 'no_blocker_found_in_packet',
                          blockers=list(blockers), needed=list(needed), evidence=list(evidence), facts=facts or {}))
    if not isinstance(packet, dict):
        packet, shape = {}, ['The packet is not a JSON object.']
    numbers = []
    packet, secrets = redact(packet, invalid=numbers)
    if numbers:
        shape.append('%d number(s) in the packet are NaN or Infinity, which is not valid JSON. They are treated as absent: %s.' % (len(numbers), ', '.join(numbers[:10])))
    if packet.get('packet') != PACKET or type(packet.get('version')) is not int or packet.get('version') != VERSION:
        shape.append('The packet must say "packet": "%s" and "version": %d.' % (PACKET, VERSION))
    unknown = sorted(set(packet) - KNOWN_KEYS)
    if unknown:
        shape.append('Unknown top-level entries are not interpreted: ' + ', '.join(unknown) + '.')
    synthetic = packet.get('synthetic') is not False                         # anything not explicitly marked real is treated as synthetic
    config = packet.get('config') if isinstance(packet.get('config'), dict) else None
    entries = packet.get('job_registry') if isinstance(packet.get('job_registry'), list) else None
    schemas = packet.get('schemas') if isinstance(packet.get('schemas'), list) else None
    receipts = packet.get('capture_receipts') if isinstance(packet.get('capture_receipts'), list) else []
    approvals = packet.get('approvals') if isinstance(packet.get('approvals'), list) else []
    issuers = authority.freeze_issuers(packet.get('accepted_issuers'))
    add('packet', 'packet', 'The readiness packet itself', _levels(not shape and not secrets),
        blockers=shape + (['The packet contains %d secret-looking value(s) or key(s). They were not read into the report. Remove them: this packet is for non-secret metadata only.' % len(secrets)] if secrets else [])
        + (['The packet is synthetic (or not marked "synthetic": false). It shows the report format; nothing in it is evidence about a real system.'] if synthetic else []),
        needed=(['A packet built from the real pilot list: its configuration, one read-only schema capture, and the registry entry. No keys, tokens or connection strings.'] if synthetic else []),
        facts=dict(prepared_by=packet.get('prepared_by'), prepared=_age(packet.get('prepared_at'), now), synthetic=synthetic, secret_locations=secrets,
                   sha256=sf.digest(packet)))

    # ---- the real hosted gate, against packet readers only
    deps = hosted.Deps(approvals=_PacketApprovals(approvals), config=config, entries=entries, schemas=schemas, captures=_PacketCaptures(receipts),
                       collectors=packet.get('accepted_collectors'), issuers=packet.get('accepted_issuers'), artifacts=_NoArtifacts(), clock=lambda: now)
    who = packet.get('pilot_principal') if isinstance(packet.get('pilot_principal'), str) and packet['pilot_principal'].strip() else 'no-principal-named'
    try:
        checks, scopes, issues, digest, _, mapping, approval = hosted.preflight(deps, hosted.Principal(who))
    except Exception as error:
        checks, scopes, issues, digest, mapping, approval = [dict(check='preflight_could_not_run', ok=False, detail=type(error).__name__)], [], [], None, [], None
    offline = {'live_schema_probe_configured': 'No read-only schema probe exists for a real source; none is supplied to an offline command.',
               'source_adapter_configured': 'No source is read by this command, so nothing shows a real list can be read.',
               'durable_store_ready': 'No store is opened by this command; the hosted table migration is not approved.',
               'quota_configured': 'No run quota is set anywhere; it is a hosting decision.',
               'host_clock_not_behind_the_last_seen_time': 'Not checked: it needs the store.'}
    preflight = [dict(c, standing=('not_connected: ' + offline[c['check']]) if c['check'] in offline else
                      ('passes on packet data only' if c['ok'] else 'blocked')) for c in checks]

    # ---- per client / job / shift
    assignments = config.get('assignments') if config and isinstance(config.get('assignments'), list) else []
    policy = packet.get('recipient_policy') if isinstance(packet.get('recipient_policy'), dict) else {}
    if not assignments:
        add('mapping', 'assignments', 'Client and job assignments', _levels(False), ['No assignment is configured in the packet.'],
            ['The pilot assignment: client, job, site id, list id, timezone, scan column, shift column, recorded shifts, business dates, mapping and report header.'])
    for i, a in enumerate(assignments):
        a = a if isinstance(a, dict) else {}
        label = '%s / %s' % (a.get('client_id') or '?', a.get('job') or '?')
        row = next((m for m in mapping if m.get('assignment') == i), None) or {}
        matches = [s for s in schemas or [] if isinstance(s, dict) and s.get('site_id') == a.get('site_id') and s.get('list_id') == a.get('list_id')]
        try:
            report = readiness.validate(a, matches[0], entries) if len(matches) == 1 else None
        except Exception:                                                    # fail closed: the mapping row below still blocks
            report = None
        needed = [c['evidence_needed'] for c in (report or {}).get('checks', []) if not c['ok'] and c.get('evidence_needed')]
        planned = [x for x in scopes if x['scope'].get('client_id') == a.get('client_id') and x['scope'].get('job') == a.get('job')]
        problems = (list(row.get('blocked_reasons') or []) if row else ['not_evaluated: the mapping check produced no row for this assignment']) + sorted(
            {x['code'] for x in issues if x.get('index') == i})
        meaning = _evidence(packet, now, topic='scan_time_meaning', client_id=a.get('client_id'), job=a.get('job'))
        independent = any(e['kind'] == 'independent' for e in meaning)
        add('mapping', 'assignment[%d].mapping' % i, 'Source mapping for ' + label, _levels(row.get('ready') is True, independent),
            blockers=problems + ([] if independent else ['No independent evidence is presented that the scan column records the moment of the actual scan.'])
            + ['The schema snapshot, the attestations and any "verified" flag come from the packet. Nothing here shows they describe the real list.'],
            needed=needed + ['From the floor, for ' + label + ': which column the tablet fills at the moment of the scan, how (automatic or typed), and one real row '
                             'where that time visibly differs from Created and Modified. Named person, date, and where the record is kept.',
                             'One real read-only schema capture of this list (docs/LAST_SCAN_SCHEMA_CAPTURE_RUNBOOK.md), kept outside the repository.'],
            evidence=meaning, facts=dict(scan_field=a.get('scan_field'), shift_field=a.get('shift_field'), zone=a.get('zone'),
                                         schema_snapshot=row.get('schema_snapshot'), capture=row.get('capture'),
                                         schema_captured=_age(matches[0].get('captured_at'), now) if len(matches) == 1 else None,
                                         schema_snapshots_for_this_list=len(matches), planned_scopes=len(planned)))
        shifts = a.get('recorded_shifts') if isinstance(a.get('recorded_shifts'), list) else []
        ends = a.get('not_before') if isinstance(a.get('not_before'), dict) else {}
        dates = a.get('business_dates') if isinstance(a.get('business_dates'), list) else []
        if not shifts:
            add('timing', 'assignment[%d].shifts' % i, 'Shifts for ' + label, _levels(False), ['No recorded shifts are configured.'],
                ['The exact shift values as they appear in the shift column for ' + label + ', and which of them should produce a report.'])
        for shift in shifts:
            missing = [d for d in dates if '%s|%s' % (d, shift) not in ends] if dates else ['every date']
            timing = _evidence(packet, now, topic='shift_schedule', client_id=a.get('client_id'), job=a.get('job'), shift=shift)
            add('timing', 'assignment[%d].shift[%s]' % (i, shift), 'Timing for %s shift %s' % (label, shift), _levels(not missing, any(e['kind'] == 'independent' for e in timing)),
                blockers=(['No shift end is configured for: ' + ', '.join(map(str, missing)) + '. Without it a draft is due %d minutes after the last scan, so a long lunch or a '
                           'stoppage produces an early draft that later scans supersede.' % (WAIT.total_seconds() // 60)] if missing else [])
                + ['Whether %d quiet minutes means "the shift is finished" for this client is an operating decision nobody has recorded.' % (WAIT.total_seconds() // 60)],
                needed=['Shift %s for %s: scheduled end time and timezone on each pilot date; the longest normal pause (lunch, breaks, changeover); whether tablets ever '
                        'work offline and how late their rows can arrive; and who decides a report may be drafted.' % (shift, label)],
                evidence=timing, facts=dict(quiet_minutes=int(WAIT.total_seconds() // 60), shift_end_configured_for=sorted(k for k in ends if k.endswith('|%s' % shift)),
                                            dates=dates, late_rows='a later row supersedes the draft and restarts the wait; nothing already delivered is recalled'))
        header = a.get('header') if isinstance(a.get('header'), dict) else {}
        fields = (a.get('mapping') or {}).get('fields') if isinstance(a.get('mapping'), dict) else None
        absent = [k for k in ('paying_client', 'worksite', 'part_name', 'evidence') if not isinstance(header.get(k), str) or not header[k].strip()]
        template = _evidence(packet, now, topic='report_template', client_id=a.get('client_id'), job=a.get('job'))
        add('report', 'assignment[%d].report' % i, 'Report template, header and photos for ' + label,
            _levels(not absent and isinstance(fields, dict) and bool(fields), any(e['kind'] == 'independent' for e in template)),
            blockers=(['Report header is missing: ' + ', '.join(absent) + '.'] if absent else []) + ([] if isinstance(fields, dict) and fields else ['No column mapping is configured.'])
            + ['The draft has only ever been compared with synthetic rows, never with a real past report for this client.',
               'Photos: the code embeds a photo it can read and reports one it cannot. Whether the read-only access can read this list\'s photos is unknown.'],
            needed=['A real, already-sent end-of-shift report for ' + label + ' to compare with a draft line by line (header, shift sheets, reject and photo pages, NOK shown as unknown when blank).',
                    'Confirmation of the header values (paying client, worksite, part name) by whoever owns the client report.',
                    'One real row with a defect photo, to learn whether photos can be read at all.'],
            evidence=template, facts=dict(header_fields_present=sorted(k for k in header if k not in absent), mapped_fields=sorted(fields) if isinstance(fields, dict) else [],
                                          checklist='every draft carries a checklist of unresolved issues and is held; client release is a separate human decision'))
        entry = next((e for e in entries or [] if isinstance(e, dict) and e.get('job') == a.get('job') and e.get('site_id') == a.get('site_id') and e.get('list_id') == a.get('list_id')), None)
        add('mapping', 'assignment[%d].registry' % i, 'Job registry entry for ' + label, _levels(bool(entry) and entry.get('verified') is True),
            blockers=([] if entry and entry.get('verified') is True else ['No verified job registry entry names this job, site and list.'])
            + ['The registry in the packet is a copy. Which system is the registry of record, and who verified this entry, is not shown.'],
            needed=['The registry of record for job ' + str(a.get('job')) + ' and who confirmed its site and list.'])
        audience = policy.get(a.get('client_id')) if isinstance(a.get('client_id'), str) else None
        add('delivery', 'assignment[%d].recipient' % i, 'Internal recipient for ' + label, _levels(audience == 'Lauren'),
            blockers=([] if audience == 'Lauren' else ['The recipient policy does not name the internal audience for this client. A client is never a recipient.'])
            + ['The policy in the packet is a statement of intent. The server-side policy and the real mailbox behind the audience do not exist yet.'],
            needed=[OPERATIONS['recipient']], facts=dict(audience=audience, client_release='held'))

    # ---- authorities, provider, operators, hosting
    seen = lambda kind, records: [dict(record_id=r.get('record_id') if isinstance(r, dict) else None, issuer=r.get('issuer') if isinstance(r, dict) else None,
                                       problems=authority.problems(r, kind, issuers, now), expires=_age((r or {}).get('expires_at') if isinstance(r, dict) else None, now),
                                       standing='presented in the packet; not authenticated') for r in records]
    add('authorities', 'identity', 'Identity: who is calling', _levels(False), ['No identity system is wired. A verified principal is required for every run, dispatch and reconciliation.'],
        [OPERATIONS['operator_workflow']])
    add('authorities', 'approval_authority', 'Approval authority and approvals', _levels(bool(approvals) and approval is not None),
        blockers=['No real approval authority exists. Approvals in a packet are presented, not authenticated, and are not an approval.']
        + ([] if issuers is not None else ['No accepted issuer identities are stated (accepted_issuers).'])
        + ([] if approval is not None else ['No presented approval covers the configuration as supplied (snapshot %s) for principal "%s".' % (digest, who)]),
        needed=[OPERATIONS['approvals_process'], 'After a real dry run reports its snapshot digest: one version 1 approval record for exactly that digest (docs/LAST_SCAN_REHEARSAL.md, "Authority adapter contract").'],
        facts=dict(snapshot_digest_as_supplied=digest, presented=seen('approval', approvals), covering_record=approval and {k: v for k, v in approval.items() if k != 'identity'}))
    add('authorities', 'capture_authority', 'Capture authority, collector and schema receipts', _levels(bool(receipts) and all(m.get('capture') for m in mapping) and bool(mapping)),
        blockers=['No real capture authority or collector exists. A receipt in a packet says where a schema claims to come from; it does not show it.'],
        needed=['Who runs the schema capture, on which approved machine, and where its receipt is recorded outside the runner.'],
        facts=dict(presented=seen('capture', receipts), accepted_collectors=packet.get('accepted_collectors')))
    wrong, contract = _contract_problems(packet.get('provider_contract'), now)
    try:
        listed = transport.verified(contract) is True
    except Exception:                                                        # fail closed
        listed = False
    add('delivery', 'provider', 'Delivery provider, sender and status adapter', _levels(not wrong),
        blockers=['No delivery provider is verified in code: delivery is disabled and every dispatch is refused.' if not listed else 'A provider contract is listed in code.',
                  'No sender and no status adapter exist. No message can be sent and no uncertain delivery can be reconciled.']
        + ([('The presented provider contract metadata is not valid under the contract\'s own rules: ' + '; '.join(wrong) + '.') if wrong[0][:2] != 'No' else wrong[0]] if wrong else [])
        + ([] if wrong else ['The metadata meets the contract\'s rules as supplied. That is not a review: nothing shows the provider behaves this way.']),
        needed=['From the provider, in writing: one delivery per idempotency key across its own retries; a per-attempt final status that differs from "not seen yet"; that a closed '
                'attempt is rejected if it arrives later; the request deadline it enforces; what "accepted" becomes; how recipient and exact bytes are proven.',
                'An owner\'s decision to review that evidence and register the contract by a reviewed code change. Pending external approval.'],
        facts=dict(contract_presented=contract is not None, listed_in_code=listed,
                   states=dict(outcome_contract_version=getattr(contract, 'outcome_contract_version', None), request_deadline_seconds=getattr(contract, 'sender_timeout_seconds', None),
                               review_valid_until=_age(getattr(contract, 'valid_until', None), now) if contract else None)))
    add('authorities', 'credentials', 'Credentials', dict(code_tested='not_applicable', configured='unknown_never_read', connected='unknown_no_call_is_made',
                                                         independently_verified='no', production_authorized='no'),
        blockers=['Credential status is UNKNOWN. This command reads no key, token or connection string and makes no call, so it cannot say whether any credential exists, works or is scoped correctly.'],
        needed=['For each of: ' + '; '.join(CREDENTIALS) + '. Who holds it, what it is scoped to (read-only for the source), when it was last proven to work, and when it expires. Do not put the value in the packet.'])
    operations = packet.get('operations') if isinstance(packet.get('operations'), dict) else {}
    for name, question in OPERATIONS.items():
        said = operations.get(name) if isinstance(operations.get(name), dict) else {}
        decided = said.get('decided') is True and all(isinstance(said.get(k), str) and said[k].strip() for k in ('by', 'at', 'reference'))
        add('operations', 'operations.' + name, name.replace('_', ' ').capitalize(), dict(code_tested='not_applicable', configured='stated_in_packet' if decided else 'no',
                                                                                             connected='unknown_no_call_is_made', independently_verified='no', production_authorized='no'),
            blockers=[] if decided else ['No recorded decision.'], needed=[] if decided else [question],
            facts=dict(decision=dict(by=said.get('by'), reference=said.get('reference'), when=_age(said.get('at'), now), standing='stated in the packet; not verified') if decided else None))

    blockers = [dict(id=x['id'], title=x['title'], blocker=b) for x in items for b in x['blockers']]
    blockers += [dict(id='preflight.' + c['check'], title='Hosted preflight', blocker=c['detail'] or c['standing']) for c in preflight if not c['ok']]
    questions, seen_q = [], set()
    for x in items:
        for q in x['needed']:
            if q not in seen_q:
                seen_q.add(q); questions.append(dict(id=x['id'], ask=q))
    by_area = [[q['ask'] for x in items if x['area'] == area for q in questions if q['id'] == x['id']] for area in ORDER]
    steps = ([qs[0] for qs in by_area if qs] + [q for qs in by_area for q in qs[1:]])[:8]   # one from every area first
    unknowns = sorted({'%s: %s' % (x['id'], level) for x in items for level, value in x['levels'].items() if str(value).startswith('unknown')})
    return dict(report='last_scan.readiness_inventory', version=1, generated_at=now,
                verdict=dict(ready_for_pilot=False, launch_blockers=len(blockers), unknowns=len(unknowns), synthetic_packet=synthetic,
                             statement='Not ready. This command finds blockers; it cannot grant readiness. It makes no call, so nothing is shown to be connected, '
                                       'authenticated or authorized, and nothing in a packet proves where the packet came from.'),
                levels={k: v for k, v in zip(LEVELS, ('exists and passes synthetic tests', 'present in the packet and accepted by the validators as supplied',
                                                     'never established here: no call is made', 'only ever claimed in a packet', 'never established here: no real authority or registered provider'))},
                not_performed=['no source, provider, identity system, authority or network was contacted', 'no store was opened or written', 'no credential was read',
                               'nothing was sent, scheduled, approved or activated', 'no sample rows were examined'],
                items=items, hosted_preflight=preflight, blockers=blockers, operator_questions=questions, next_steps=steps, unknowns=unknowns, residual_limits=list(RESIDUAL))


def text(report):
    """A short human-readable rendering of the same report."""
    v, out = report['verdict'], []
    out.append('LAST-SCAN DRAFT RUNNER: INTEGRATION READINESS INVENTORY (offline, read-only)')
    out.append('Verdict: NOT READY FOR A PILOT. %d blockers, %d unknowns.%s' % (v['launch_blockers'], v['unknowns'], ' SYNTHETIC PACKET: format demonstration only.' if v['synthetic_packet'] else ''))
    out.append(v['statement'])
    out.append('')
    out.append('Not done by this command: ' + '; '.join(report['not_performed']) + '.')
    area = None
    for x in sorted(report['items'], key=lambda x: ORDER.index(x['area'])):
        if x['area'] != area:
            area = x['area']
            out.extend(['', area.upper()])
        lv = x['levels']
        out.append('  %s  [%s]' % (x['title'], x['status']))
        out.append('    tested: %s | configured: %s | connected: %s | independently verified: %s | authorized: %s' % tuple(lv[k] for k in LEVELS))
        for b in x['blockers']:
            out.append('    BLOCKED  ' + b)
        for e in x['evidence']:
            out.append('    presented (%s, %s): %s' % (e['kind'], e['when'].get('stated') or 'undated', e.get('claim')))
    failing = [c for c in report['hosted_preflight'] if not c['ok']]
    out.extend(['', 'HOSTED PREFLIGHT (the real gate, on packet data only): %d of %d checks fail' % (len(failing), len(report['hosted_preflight']))])
    for c in failing:
        out.append('  FAIL  %s: %s' % (c['check'], c['detail'] or c['standing']))
    out.extend(['', 'WHAT TO ASK OR SUPPLY NEXT'])
    for n, step in enumerate(report['next_steps'], 1):
        out.append('  %d. %s' % (n, step))
    out.append('  (%d questions in all; the JSON report lists each one against its item.)' % len(report['operator_questions']))
    out.extend(['', 'LIMITS THAT REMAIN EVEN WHEN EVERYTHING ABOVE IS ANSWERED'])
    out.extend('  - ' + r for r in report['residual_limits'])
    return '\n'.join(out) + '\n'


def main(argv=None):
    parser = argparse.ArgumentParser(description='Offline, read-only readiness inventory. Reads one packet file and prints a report.')
    parser.add_argument('packet', help='path of the readiness packet (JSON, non-secret)')
    parser.add_argument('--format', choices=('text', 'json', 'both'), default='text')
    parser.add_argument('--now', help='judge validity as of this offset-aware time (default: the current time)')
    args = parser.parse_args(argv)
    now = args.now or sf.now()
    try:
        instant(now)
    except (ValueError, OverflowError, TypeError):
        print('--now must be an ISO time with an offset, for example 2026-11-10T19:00:00+00:00', file=sys.stderr)
        return 2
    try:
        packet = json.loads(Path(args.packet).read_text())
    except OSError as error:
        print('The packet could not be read: ' + type(error).__name__, file=sys.stderr)
        return 2
    except ValueError:
        packet = None                                                        # reported as an invalid packet, with the standing blockers
    report = inventory(packet, now)
    if args.format in ('text', 'both'):
        sys.stdout.write(text(report))
    if args.format in ('json', 'both'):
        sys.stdout.write(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + '\n')
    return 1                                                                 # never 0: this command never reports readiness


if __name__ == '__main__':
    sys.exit(main())
