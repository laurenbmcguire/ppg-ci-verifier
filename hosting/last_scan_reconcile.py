"""Hosted reconciliation boundary for an uncertain delivery. PREPARED, NOT CONNECTED.

Nothing imports this module: there is no route, no schedule and no operator screen that reaches it. It contains no
network client, no secret, no identity provider and no status adapter. It never sends anything, never puts an
entry back in the queue, and never makes an entry deliverable again.

What it is for. A delivery attempt whose outcome is not known stays "uncertain" for ever (hosting.last_scan_dispatch,
hosting.last_scan_transport). This boundary lets ONE verified, separately approved caller ask a provider STATUS
adapter about ONE exact attempt and record what the provider states, with an audit entry:
  * a terminal "delivered" for exactly this attempt      -> recorded as sent (it was delivered; nothing is sent);
  * a terminal, closed "not delivered" for this attempt  -> recorded as not delivered. The entry becomes
                                                            "not_delivered", a final state that no poll, timer or
                                                            dispatch reopens. Whether to send the report again is a
                                                            decision for a person and a NEW operation, not for this code;
  * anything else                                        -> nothing changes. "accepted", "unknown", a bare
                                                            delivered:false, a mismatched or unreadable answer, an
                                                            error, no answer, and a passed deadline establish nothing.
Only the provider contract the attempt went out through can attest it: the worker writes the provider and the
contract's hash on the attempt in the claim, before the sender is called, and both must equal the contract in force
here, before the provider is asked and again inside the recording transaction. A separately approved reconciliation
through another provider, version or set of reviewed terms is refused, and an attempt that carries no such record is
left unresolved rather than guessed at; migrating such attempts would need its own reviewed design.
A terminal not-delivered is not recorded while the original sender call may still be running (the attempt is in
flight and its own deadline has not passed): the answer is kept in the audit trail and the entry stays uncertain.
A late acknowledgment from that sender is still recorded by the worker afterwards, and contradicts the proof openly.

Everything is handed in through ``Deps`` and every default means "not configured" = DENY. Needed, and none of it
exists for production (the only implementations are TEST-ONLY fakes):
  * identity (Authorizer), the accepted issuers, the host clock and the store, as for a dispatch;
  * a verified provider contract stating the outcome contract version, a request deadline and a review validity;
  * the server-side recipient policy;
  * a status adapter: status_lookup(idempotency_key=, attempt_id=, audience=, deadline_seconds=) -> one version 1
    outcome record (hosting.last_scan_transport.judge_outcome). It must ask the provider; it must not send;
  * a reconcile quota;
  * a RECONCILE APPROVAL from the ApprovalAuthority, under the authority record contract, for a digest covering the
    action, the provider contract, the recipient policy and exactly this scope, operation, attempt, artifact and
    recipient. A run approval or a dispatch approval is a different digest and never counts.

``handle`` in order: policies frozen before the first callback; caller verified; request validated; the checks above;
the stored entry must be exactly the named artifact and attempt, addressed to the approved recipient, and still
uncertain; ONE transaction takes the clock check, the nonce, the quota and the intent; the approval is asked again and
the pure time gate passes; the status adapter is called once; the approval is asked again and the gate passes again;
then ONE transaction that holds the entry and the clock record: inside it the pure time gate is judged once more (the
wait for the lock counts) and the entry is checked again, as it is then, against everything the approval covers
(artifact, scope, recipient, attempt, still uncertain); only then is at most one of the two recordings above applied,
and the audit entry appended (who, when, under which approval record, which
provider record, the answer's hash, and the entry before and after). No secret is stored: the answer is kept as its
hash, and whole only when small.

NOT claimed: that a provider's answer is true, that a status adapter exists, or exactly-once delivery. A provider that
states "not delivered" and delivers anyway is recorded as having contradicted itself, not prevented.
"""
from copy import deepcopy
from dataclasses import dataclass, replace
from collections.abc import Mapping
from typing import Any, Callable, Optional
from uuid import uuid4

from hosting import source_first as sf
from hosting import last_scan_dispatch as dispatch
from hosting import last_scan_hosted as hosted
from hosting import last_scan_transport as transport
from hosting.last_scan_drafts import instant, scope_key
from hosting.last_scan_service import binding

ACTION = 'last_scan.reconcile'
RECIPIENT = dispatch.RECIPIENT
MODES = ('dry_run', 'reconcile')
SCOPE = dispatch.SCOPE + ('zone',)
ATTEMPT = ('operation_id', 'attempt_id', 'workbook_sha256', 'checklist_sha256')
NAMED = SCOPE + ATTEMPT + ('recipient', 'reconcile_id')
FIELDS = set(NAMED) | {'mode', 'timestamp', 'nonce'}
LIMIT_SECONDS = dispatch.GRANT_SECONDS  # placeholder: how long, on the host clock, one reconciliation may take
MAX_AUDIT = 50                          # audit entries kept per operation; one more is refused, not dropped
INTENT = 'reconcile:intent:'
NO_RESEND = 'never_by_this_path: reconciliation records what a provider states and makes nothing deliverable again'


@dataclass
class Deps(dispatch.Deps):
    """The dispatch dependencies plus what only a reconciliation needs. Every default means "not configured" = deny."""
    status_lookup: Optional[Callable] = None   # asks the provider about one attempt; never sends
    reconcile_quota: Any = None                # hosted.Quota: reconciliations per hour, per principal


def reconcile_digest(recipients, contract, key, body):
    """What a reconcile approval covers: this action, this provider contract, this recipient policy, and exactly this
    scope, operation, attempt, artifact and recipient."""
    return sf.digest(dict(action=ACTION, recipients=[list(pair) for pair in recipients], provider_contract=contract.digest(), scope=key,
                          recipient=body['recipient'], **{k: body[k] for k in ATTEMPT}))


def _look(entry, attempt):
    """The part of an entry an audit entry records before and after. No payload, no evidence bodies."""
    return dict(status=entry.get('status'), delivery_status=entry.get('delivery_status'), attempt_id=entry.get('attempt_id'),
                attempts=len(entry.get('attempts') or []), attempt_state=(attempt or {}).get('state'), provider_outcome=entry.get('provider_outcome'))


def _attempt(entry, attempt_id):
    return next((a for a in entry.get('attempts') or [] if a.get('id') == attempt_id), None)


def _entry(deps, key, body):
    """('ready' | refusal, details, before). Reads the store only."""
    with deps.store.transaction('outbox:' + key) as box:
        entry = deepcopy(box.get('entries', {}).get(body['operation_id']))
    return _fits(entry, body, deps.provider_contract)


def _fits(entry, body, contract):
    """Whether this entry, AS IT IS NOW, is exactly what the caller named and the approval covers: the artifact, the
    scope it belongs to, the recipient, the attempt, and still uncertain. Pure. Used before the provider is asked and
    again inside the recording transaction, because the entry can change in between."""
    if not entry:
        return 'no_such_entry', [], None
    mine, receipt, problems = _attempt(entry, body['attempt_id']), entry.get('receipt') or {}, []
    receipt = receipt if isinstance(receipt, dict) else {}
    if entry.get('audience') != RECIPIENT or entry.get('audience') != body['recipient']:
        problems.append('entry_is_addressed_to_someone_else')
    for k in ('operation_id', 'workbook_sha256', 'checklist_sha256'):
        if receipt.get(k) != body[k]:
            problems.append('stored_receipt_differs:' + k)
    try:
        if receipt.get('binding') != binding({k: body[k] for k in ('client_id', 'site_id', 'list_id', 'job')}):
            problems.append('entry_belongs_to_another_client_site_list_or_job')
    except ValueError:
        problems.append('entry_belongs_to_another_client_site_list_or_job')
    if problems:
        return 'artifact_mismatch', problems, _look(entry, mine)
    if mine is None or entry.get('attempt_id') != body['attempt_id']:
        return 'stale_attempt', ['the_entry_is_not_on_this_attempt'], _look(entry, mine)
    # The attempt names the provider contract it went out through, recorded before the sender was called. Only that
    # exact contract may attest it. An attempt with no such record is NOT guessed at: it stays unresolved.
    made = (mine.get('provider'), mine.get('provider_contract'))
    if not all(isinstance(v, str) and v.strip() for v in made):
        return 'attempt_has_no_provider_binding', [], _look(entry, mine)
    if made != (getattr(contract, 'provider', None), contract.digest() if isinstance(contract, transport.ProviderContract) else None):
        return 'provider_mismatch', ['the_attempt_went_out_through_another_provider_contract'], _look(entry, mine)
    if entry.get('delivery_status') != 'uncertain':
        return 'nothing_to_reconcile', [str(entry.get('delivery_status'))], _look(entry, mine)
    if len(entry.get('reconciliations') or []) >= MAX_AUDIT:
        return 'audit_trail_full', [], _look(entry, mine)
    return 'ready', [], _look(entry, mine)


def _keys(principal, body):
    return ('auth:nonce:' + sf.digest(dict(principal=principal.id, nonce=body['nonce'])),
            INTENT + sf.digest(dict(principal=principal.id, reconcile_id=body['reconcile_id'])))


def _recorded(deps, principal, body, intent):
    nonce_key, intent_key = _keys(principal, body)
    now = deps.clock()
    with deps.store.atomic([nonce_key, intent_key]) as records:
        held = records[intent_key]
        if not held:
            return None
        if records[nonce_key]:
            return 409, dict(status='replayed_request')
        records[nonce_key].update(used_at=now, reconcile_id=body['reconcile_id'], principal=principal.id)
        if held.get('intent') != intent:
            return 409, dict(status='reconcile_id_reused_for_another_intent')
        if held.get('status') == 'finished':
            return held['code'], dict(deepcopy(held['response']), replayed=True)
        return 409, dict(status='reconcile_in_progress_or_interrupted', started_at=held.get('started_at'))


def _reserve(deps, principal, body, intent):
    """Clock check, nonce, quota and intent, in one transaction BEFORE the provider is asked anything."""
    nonce_key, intent_key = _keys(principal, body)
    now = deps.clock()
    hour = instant(now).strftime('%Y-%m-%dT%H')
    quota_key = 'auth:reconcile_quota:' + sf.digest(dict(principal=principal.id, hour=hour))
    with deps.store.atomic([nonce_key, quota_key, intent_key, hosted.CLOCK]) as records:
        if hosted.clock_behind(records[hosted.CLOCK].get('high_water'), now):
            return 'host_clock_behind'
        if records[nonce_key]:
            return 'replayed_request'
        if records[intent_key]:
            return 'reconcile_in_progress_or_interrupted'
        used = int(records[quota_key].get('reconciliations') or 0)
        if used >= deps.reconcile_quota.runs_per_hour:
            return 'quota_exceeded'
        hosted.mark_clock(records[hosted.CLOCK], now)
        records[nonce_key].update(used_at=now, reconcile_id=body['reconcile_id'], principal=principal.id)
        records[quota_key].update(reconciliations=used + 1, hour=hour, principal=principal.id, last_at=now)
        records[intent_key].update(status='started', intent=intent, principal=principal.id, reconcile_id=body['reconcile_id'],
                                   operation_id=body['operation_id'], attempt_id=body['attempt_id'], started_at=now)
    return None


def _respond(code, **body):
    return code, body


def handle(method, headers, body, deps):
    """(status code, JSON-able body). Fails closed on anything malformed; never echoes credentials; never sends."""
    if not isinstance(deps, Deps) or not isinstance(deps.authorizer, hosted.Authorizer):
        return _respond(503, status='not_configured')
    if not isinstance(headers, Mapping) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in headers.items()):
        return _respond(401, status='unauthorized')
    recipients, issuers = dispatch._freeze_recipients(deps.recipients), hosted.authority.freeze_issuers(deps.issuers)
    try:
        body = deepcopy(body)
        headers = dict(headers)
        deps = replace(deps, recipients=recipients, issuers=issuers)
        body_digest = sf.digest(body)
    except Exception:
        return _respond(400, status='bad_request', detail='The body must be plain JSON with finite numbers.')
    lookup, contract, base = deps.status_lookup, deps.provider_contract, dispatch._run_deps(deps)
    try:
        principal = deps.authorizer.verify(method, dict(headers), body_digest)
    except Exception:
        principal = None
    if not isinstance(principal, hosted.Principal) or not isinstance(principal.id, str) or not principal.id.strip():
        return _respond(401, status='unauthorized')
    if method != 'POST':
        return _respond(405, status='method_not_allowed')
    if not isinstance(body, dict) or set(body) - FIELDS:
        return _respond(400, status='bad_request', detail='Allowed fields: ' + ', '.join(sorted(FIELDS)) + '.')
    mode = body.get('mode', 'dry_run')
    if not isinstance(mode, str) or mode not in MODES or any(
            not isinstance(body.get(k), str) or not body[k].strip() or body[k] != body[k].strip() or len(body[k]) > 400 for k in NAMED):
        return _respond(400, status='bad_request', detail='mode dry_run or reconcile, and exact text for: ' + ', '.join(sorted(NAMED)) + '.')
    try:
        started = deps.clock()
        age = abs((instant(started) - instant(body.get('timestamp'))).total_seconds())
    except Exception:
        age = None
    if age is None or age > hosted.REQUEST_WINDOW_SECONDS:
        return _respond(401, status='stale_or_undated_request')
    if mode == 'reconcile' and (not isinstance(body.get('nonce'), str) or not 16 <= len(body['nonce']) <= 200):
        return _respond(400, status='bad_request', detail='A reconciliation needs a single-use nonce of 16 to 200 characters.')
    op, aid = body['operation_id'], body['attempt_id']

    # ---- authorization. The provider is not asked anything and nothing is written.
    checks = []
    def check(name, ok, detail=''):
        checks.append(dict(check=name, ok=bool(ok), detail='' if ok else detail))
    store_ok = False
    if deps.store is not None and all(hasattr(deps.store, n) for n in ('atomic', 'put_blob', 'ready')):
        try:
            store_ok = deps.store.ready() is True
        except Exception:
            store_ok = False
    check('durable_store_ready', store_ok, 'No ready durable store was supplied.')
    behind = hosted.clock_problem(deps) if store_ok else None
    check('host_clock_not_behind_the_last_seen_time', not behind, behind or '')
    check('accepted_authority_issuers_configured', issuers is not None, 'No accepted authority identities are configured on the server side.')
    trusted = transport.verified(contract)
    check('provider_contract_verified', trusted, 'No verified provider contract: there is no provider to ask.')
    try:
        until = instant(contract.valid_until).isoformat() if trusted else None
    except (ValueError, OverflowError, TypeError):
        until = None
    limit = getattr(contract, 'sender_timeout_seconds', None)
    check('provider_contract_states_outcome_contract_deadline_and_validity',
          trusted and type(contract.outcome_contract_version) is int and contract.outcome_contract_version == transport.OUTCOME_VERSION
          and type(limit) is int and limit > 0 and until is not None and (instant(until) - instant(started)).total_seconds() > LIMIT_SECONDS,
          'The provider contract must state the outcome contract version, a request deadline and a review validity with time left.')
    check('status_adapter_configured', callable(lookup), 'No status adapter was supplied. None exists outside the test suite.')
    check('recipient_policy_configured', recipients is not None, 'No server-side recipient policy is configured.')
    check('recipient_is_the_approved_one_for_this_client', dict(recipients or ()).get(body['client_id']) == body['recipient'] == RECIPIENT,
          'The recipient is not the one the server-side policy names for this client, or is not the internal audience.')
    check('reconcile_quota_configured', isinstance(deps.reconcile_quota, hosted.Quota) and isinstance(deps.reconcile_quota.runs_per_hour, int)
          and not isinstance(deps.reconcile_quota.runs_per_hour, bool) and deps.reconcile_quota.runs_per_hour > 0, 'No reconcile quota is configured.')
    try:
        key = sf.digest(scope_key({k: body[k] for k in SCOPE}))
    except Exception:
        key = None
    check('scope_is_well_formed', key is not None, 'The named site, list, job, client, business date, shift and timezone do not form a scope.')
    digest = reconcile_digest(recipients, contract, key, body) if key and recipients is not None and trusted else None
    ok, why, approval = hosted._approval(base, digest, principal, action=ACTION, margin=LIMIT_SECONDS) if digest and issuers is not None else (
        False, 'There is nothing complete enough to approve for reconciliation.', None)
    check('reconcile_approved_for_this_attempt_recipient_contract_and_principal', ok, why)
    authorized = all(c['ok'] for c in checks)
    report = dict(mode=mode, reconcile_id=body['reconcile_id'], principal=principal.id, reconcile_digest=digest, checks=checks, authorized=authorized,
                  scope={k: body[k] for k in SCOPE}, operation_id=op, attempt_id=aid, audience=RECIPIENT, client_release='held', resend=NO_RESEND,
                  approval=approval and {k: v for k, v in approval.items() if k != 'identity'})
    if not authorized:
        return _respond(200 if mode == 'dry_run' else 409, status='dry_run' if mode == 'dry_run' else 'not_authorized', provider_asked=0, **report)
    intent = sf.digest(dict(principal=principal.id, reconcile=digest, approval=approval['identity']))
    if mode == 'reconcile':
        earlier = _recorded(deps, principal, body, intent)
        if earlier is not None:                                              # answered from the record: the provider is not asked again
            return _respond(earlier[0], **dict(report, **dict(earlier[1], provider_asked=0)))
    state, details, before = _entry(deps, key, body)
    report['entry'] = dict(state=state, details=details, now=before)
    if mode == 'dry_run':
        return _respond(200, status='dry_run', provider_asked=0, **report)
    if state != 'ready':
        return _respond(409, status=state, provider_asked=0, **report)
    refused = _reserve(deps, principal, body, intent)
    if refused:
        return _respond(429 if refused == 'quota_exceeded' else 409, status=refused, provider_asked=0, **report)
    bounds = [approval['expires_at'], until]

    def withdrawn():
        """None, or why this reconciliation may not go on. The approval is ONE record judged whole and must be the SAME
        record as at authorization; then, last and asking nobody, the pure time gate."""
        still, reason, now_ = hosted._approval(base, digest, principal, action=ACTION)
        if not still:
            return 'reconcile_approval_withdrawn: ' + reason
        if now_['identity'] != approval['identity']:
            return 'reconcile_approval_is_another_record_than_the_one_this_call_was_authorized_under'
        if not transport.verified(contract):
            return 'provider_contract_no_longer_verified'
        return hosted.time_gate(base, started, bounds, LIMIT_SECONDS)

    def finish(code, **result):
        result = dict(result, provider_asked=asked['n'])
        with deps.store.transaction(_keys(principal, body)[1]) as record:
            record.update(status='finished', code=code, response=result, finished_at=deps.clock())
        return _respond(code, **dict(report, **result))

    asked = dict(n=0)
    why = withdrawn()
    if why:
        return finish(409, status='held', reason=why.split(': ', 1)[0], reason_detail=why)
    expected = dict(operation_id=op, attempt_id=aid, idempotency_key=op, recipient=body['recipient'],
                    workbook_sha256=body['workbook_sha256'], checklist_sha256=body['checklist_sha256'])
    asked['n'], sent_at = 1, deps.clock()
    try:
        answer = lookup(idempotency_key=op, attempt_id=aid, audience=body['recipient'], deadline_seconds=limit)
    except Exception as error:
        verdict = dict(outcome='lookup_raised', reasons=[type(error).__name__], deadline_breach=False, provider_record_id=None, observed_at=None,
                       evidence=dict(sha256=None))
    else:
        verdict = transport.judge_outcome(answer, expected, contract, sent_at, deps.clock())
    why = withdrawn()                                                        # the lookup may have taken time

    # ---- ONE transaction: re-read the entry, record at most one outcome, append the audit entry
    # The pure time gate is judged HERE, once the records are held: waiting for the lock takes time too. The clock
    # record is part of this same transaction, so its watermark is read without opening another one.
    with deps.store.atomic(['outbox:' + key, hosted.CLOCK]) as records:
        box = records['outbox:' + key]
        entry = box.get('entries', {}).get(op)
        if not entry:
            decision, recorded = 'no_such_entry', None
        else:
            mine, now = _attempt(entry, aid), deps.clock()           # ONE reading of the host clock for everything below
            was = _look(entry, mine)
            fits = _fits(entry, body, contract)[0]
            late = hosted.time_gate_at(records[hosted.CLOCK].get('high_water'), now, started, bounds, LIMIT_SECONDS)
            proof = dict(operation_id=op, attempt_id=aid, provider_record_id=verdict['provider_record_id'], observed_at=verdict['observed_at'],
                         outcome_sha256=verdict['evidence']['sha256'], deadline_breach=verdict['deadline_breach'], via='reconciliation')
            recorded = None
            if len(entry.get('reconciliations') or []) >= MAX_AUDIT:
                decision = 'audit_trail_full'                            # nothing is recorded that could not also be audited
            elif why:
                decision = 'authorization_or_time_gate_failed_after_the_lookup'
            elif late:
                decision, why = 'time_gate_failed_inside_the_recording_transaction', late
            elif fits != 'ready':
                decision = fits                                          # artifact_mismatch, stale_attempt, nothing_to_reconcile, ...
            elif verdict['outcome'] == 'delivered':
                mine.update(state='acked', ack=proof, acked_at=now, acked_by='reconciliation')
                entry.update(ack=proof, delivery_status='sent', reconciled_at=now, provider_outcome='delivered')
                if entry.get('status') == 'uncertain':
                    entry['status'] = 'sent'
                decision = recorded = 'recorded_delivered'
            elif verdict['outcome'] == 'terminal_not_delivered':
                if mine.get('state') == 'in_flight' and instant(now) < instant(mine['deadline']):
                    decision = 'attempt_may_still_be_in_flight'          # the sender call may still be running: nothing is closed
                else:
                    mine.update(state='closed_not_delivered', closed_proof=proof, closed_at=now, closed_under_contract=contract.digest())
                    entry.update(delivery_status='not_delivered', reconciled_at=now, provider_outcome='terminal_not_delivered')
                    if entry.get('status') == 'uncertain':
                        entry.update(status='not_delivered', resend=NO_RESEND)   # final: no poll, timer or dispatch reopens it
                    decision = recorded = 'recorded_not_delivered'
            else:
                decision = 'provider_did_not_establish_an_outcome'
            if verdict['deadline_breach']:
                entry['provider_deadline_breach'] = True
            trail = entry.setdefault('reconciliations', [])
            if len(trail) < MAX_AUDIT:
                trail.append(dict(id=uuid4().hex, at=now, principal=principal.id, reconcile_id=body['reconcile_id'], attempt_id=aid,
                                  approval=dict(record_id=approval['record_id'], issuer=approval['issuer'], identity=approval['identity']),
                                  provider_contract=contract.digest(), provider_outcome=verdict['outcome'], provider_reasons=verdict['reasons'],
                                  proof=proof, evidence=verdict['evidence'], decision=decision, before=was, after=_look(entry, mine)))
    result = dict(decision=decision, provider_outcome=verdict['outcome'], provider_reasons=verdict['reasons'],
                  provider_record_id=verdict['provider_record_id'], provider_deadline_breach=bool(verdict['deadline_breach']), reason_detail=why)
    if recorded:
        return finish(200, status=recorded, **result)
    return finish(202, status='held', reason=decision, **result)
