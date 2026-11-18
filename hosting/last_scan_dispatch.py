"""Hosted dispatch authorization boundary for the last-scan draft runner. PREPARED, NOT CONNECTED.

Nothing imports this module: there is no route, no cron entry and no scheduled task that reaches it. It contains no
network client, no secret, no identity provider and no sender. It hands ONE already-built draft to ONE internal
recipient, and only after every check below passes for exactly that request. A client is never a recipient here:
the only audience the pipeline can address is the internal one (``RECIPIENT``), and client release stays held.

Everything is handed in through ``Deps`` and every default means "not configured" = DENY. On top of what the hosted
run needs (hosting.last_scan_hosted: identity, run approval, capture authority, accepted collectors, live schema
probe, schemas, store, source adapter) a dispatch needs four more things this code does not own:
  * a verified provider contract   listed in the one registry in hosting.last_scan_transport (empty today, so
                                   every dispatch is refused in production whatever else is supplied);
  * a recipient policy             server-side: which audience each client's drafts may go to. Never read from
                                   the configuration or the request;
  * a dispatch approval            from the same ApprovalAuthority, for a digest covering the dispatch action, the
                                   approved run snapshot (configuration, assignments, shifts, templates, schemas,
                                   collectors), the recipient policy and the provider contract. A run approval is
                                   a different digest and never counts as a dispatch approval, and the caller must
                                   be named in BOTH;
  * a sender and a dispatch quota  supplied by the host. No sender exists outside the test suite.
None of these exists for production. The only implementations are TEST-ONLY fakes in the test suite. Handing them in
is a wiring boundary, not cryptographic authenticity.

``handle`` is one bounded call, in this order. Nothing is read from a source and nothing is sent until step 7:
  1. the recipient and collector policies are copied into private immutable values and the configuration, registry
     and schemas are deep-copied, before the first dependency callback;
  2. the caller is verified (401) and the call is validated: exact scope (client, job, site, list, business date,
     shift), exact artifact (operation, workbook and checklist hashes, render version), recipient, timestamp, nonce;
  3. the whole hosted run preflight is repeated (hosting.last_scan_hosted.preflight: run approval for this caller,
     capture receipts, mapping readiness, probe configured, ...), then the dispatch checks above, and the named scope
     must be exactly one scope of the approved plan;
  4. the stored draft must be exactly the named artifact: same operation, hashes and render version, the render
     version must still be the current one for that scope, the stored bytes must still hash to the receipt, and the
     outbox entry must be addressed to the approved recipient and still be pending;
  5. a site on a provider wait is not asked anything;
  6. ONE transaction takes the single-use nonce, the hourly dispatch quota, the intent record (per caller and
     dispatch_id) and a single-use, short-lived grant for the transport door;
  7. under the scope's fenced lease the worker probes the live schema, re-reads the source, and this module re-asks
     the approval and capture authorities (each approval as ONE record judged whole; the capture receipt must be the
     same receipt that was approved, not merely a valid one); then one fenced transaction re-observes freshness and the quiet period and
     claims the entry, and only then is the sender called, once.
A repeated dispatch_id from the same caller returns the recorded outcome without reading or sending. An interrupted
one is reported as interrupted and never repeated here: the outbox entry is the record, and an uncertain delivery is
never retried (hosting.last_scan_transport). Another caller's dispatch_id is a different record.

Every approval and capture receipt must meet the record contract in hosting.last_scan_authority: versioned plain
data, an accepted issuer, status active, no revocation field set, and validity bounds judged on the HOST clock. The
dispatch approval covers the run approval RECORD (its id and validity), so a renewed run approval needs a new
dispatch approval, and the intent and grant name both records. Before the source is read, again after it, the same
records are asked for and must be the same records, still valid; an approval or receipt that would expire within
GRANT_SECONDS is refused up front; a call that takes longer than GRANT_SECONDS, or sees the host clock go backwards,
stops; and the sender is not called once any of the three validity bounds has passed.

NOT CLOSED, and not claimed: the source can change between the schema probe and the read, between the read and the
claim, and between the claim and the provider receiving the message; an approval can be withdrawn after the last
re-check. Those windows are narrowed and the next poll detects a changed source, nothing more. No exactly-once
delivery is claimed.
"""
from copy import deepcopy
from dataclasses import dataclass, fields, replace
from datetime import timedelta
from collections.abc import Mapping
from typing import Any, Callable, Optional
from uuid import uuid4

from hosting import source_first as sf
from hosting import last_scan_hosted as hosted
from hosting import last_scan_transport as transport
from hosting.last_scan_drafts import instant
from hosting.last_scan_service import ScheduledWorker, binding, site_hold

ACTION = 'last_scan.dispatch'
RECIPIENT = 'Lauren'                    # the only audience this pipeline addresses; never a client
MODES = ('dry_run', 'dispatch')
SCOPE = ('client_id', 'job', 'site_id', 'list_id', 'business_date', 'shift')
ARTIFACT = ('operation_id', 'workbook_sha256', 'checklist_sha256', 'render_version')
FIELDS = set(SCOPE) | set(ARTIFACT) | {'dispatch_id', 'mode', 'timestamp', 'nonce', 'recipient'}
GRANT_SECONDS = 60                      # placeholder bound on how long an unused transport grant stays valid
INTENT = 'dispatch:intent:'
RACE = ('not_closed: the source can change between the schema probe, the re-read, the claim and the provider '
        'receiving the message, and an approval can be withdrawn after the last re-check')


class Revoked(Exception):
    """Raised in place of a claim when an authority no longer vouches for this dispatch after the source re-read."""


@dataclass
class Deps(hosted.Deps):
    """The hosted run's dependencies plus what only a dispatch needs. Every default means "not configured" = deny."""
    recipients: Any = None               # {client_id: audience}, server-side; never from the configuration or the caller
    sender: Optional[Callable] = None    # sender(idempotency_key=, attempt_id=, audience=, payload=) -> acknowledgment
    dispatch_quota: Any = None           # hosted.Quota: dispatches per hour, per principal and approved dispatch digest


def _freeze_recipients(value):
    """The recipient policy as one private immutable tuple of (client_id, audience) pairs, or None (deny). The host's
    mapping is read exactly once and only that copy is validated and kept."""
    if not isinstance(value, Mapping):
        return None
    try:
        taken = tuple(value.items())
    except RuntimeError:                                                     # resized by another thread mid-copy
        return None
    if taken and all(isinstance(x, str) and x.strip() and x == x.strip() for pair in taken for x in pair):
        return tuple(sorted(taken))
    return None


def _run_deps(deps):
    """The same dependencies as the hosted run sees them: no provider contract is ever attached to that path."""
    return hosted.Deps(**dict({f.name: getattr(deps, f.name) for f in fields(hosted.Deps)}, provider_contract=None))


def dispatch_digest(snapshot, recipients, contract, run_approval):
    """What a dispatch approval covers: the action, the exact approved run snapshot, the exact run approval RECORD
    (so a renewed run approval needs a new dispatch approval), the recipient policy and the provider contract. Any
    change to one of them is a different digest, so the approval stops counting."""
    return sf.digest(dict(action=ACTION, snapshot_digest=snapshot, run_approval=run_approval, recipients=[list(pair) for pair in recipients],
                          provider_contract=contract.digest()))


def _approved(base, digest, principal):
    """(ok, why, record summary). ONE record from the authority, read once, judged whole under the record contract:
    digest, approver, caller, action, issuer, status and validity (with enough of it left to send) all on that record."""
    return hosted._approval(base, digest, principal, action=ACTION, margin=GRANT_SECONDS)


def _draft(deps, item, body):
    """('ready' | 'not_pending' | 'artifact_mismatch', details). Reads the store only; no source is touched."""
    op = body['operation_id']
    with deps.store.transaction('outbox:' + item['key']) as box:
        entry = deepcopy(box.get('entries', {}).get(op))
    if not ScheduledWorker._claimable(entry):
        return 'not_pending', [(entry or {}).get('status') or 'no_such_entry']
    receipt, problems, publisher = entry.get('receipt') or {}, [], item['render']
    if entry.get('audience') != RECIPIENT:
        problems.append('entry_is_addressed_to_someone_else')
    for k in ARTIFACT:
        if receipt.get(k) != body[k]:
            problems.append('stored_receipt_differs:' + k)
    if publisher.render_version() != body['render_version']:
        problems.append('template_or_mapping_changed_since_the_draft_was_built')
    if receipt.get('binding') != binding(item['scope']):
        problems.append('draft_belongs_to_another_client_site_list_or_job')
    try:
        stored = publisher.lookup(op)                                        # re-hashes the stored bytes against the receipt
    except Exception as error:
        problems.append('stored_artifact_unreadable_or_altered:' + type(error).__name__)
    else:
        if not isinstance(stored, dict) or any(stored.get(k) != body[k] for k in ARTIFACT):
            problems.append('stored_artifact_is_not_the_named_one')
    return ('artifact_mismatch', problems) if problems else ('ready', [])


def _keys(principal, body):
    return ('auth:nonce:' + sf.digest(dict(principal=principal.id, nonce=body['nonce'])),
            INTENT + sf.digest(dict(principal=principal.id, dispatch_id=body['dispatch_id'])))


def _recorded(deps, principal, body, intent):
    """An earlier call with this caller's dispatch_id, if any: (code, body), having spent this call's nonce."""
    nonce_key, intent_key = _keys(principal, body)
    now = deps.clock()
    with deps.store.atomic([nonce_key, intent_key]) as records:
        held = records[intent_key]
        if not held:
            return None
        if records[nonce_key]:
            return 409, dict(status='replayed_request')
        records[nonce_key].update(used_at=now, dispatch_id=body['dispatch_id'], principal=principal.id)
        if held.get('intent') != intent:
            return 409, dict(status='dispatch_id_reused_for_another_intent')
        if held.get('status') == 'finished':
            return held['code'], dict(deepcopy(held['response']), replayed=True)
        return 409, dict(status='dispatch_in_progress_or_interrupted', started_at=held.get('started_at'))


def _reserve(deps, principal, digest, body, item, intent, contract):
    """Nonce, quota, intent and the transport grant, taken together in one transaction BEFORE any source is read."""
    nonce_key, intent_key = _keys(principal, body)
    now, op = deps.clock(), body['operation_id']
    hour = instant(now).strftime('%Y-%m-%dT%H')
    quota_key = 'auth:dispatch_quota:' + sf.digest(dict(principal=principal.id, dispatch=digest, hour=hour))
    grant_key = transport.grant_key(item['scope'], op)
    with deps.store.atomic([nonce_key, quota_key, intent_key, grant_key, hosted.CLOCK]) as records:
        if records[nonce_key]:
            return 'replayed_request', None
        hosted.mark_clock(records[hosted.CLOCK], now)
        if records[intent_key]:
            return 'dispatch_in_progress_or_interrupted', None
        grant = records[grant_key]
        if grant and not grant.get('used_at') and instant(grant['expires_at']) > instant(now):
            return 'busy', None                                              # another dispatch of this operation holds the door
        used = int(records[quota_key].get('dispatches') or 0)
        if used >= deps.dispatch_quota.runs_per_hour:
            return 'quota_exceeded', None
        grant_id = uuid4().hex
        records[nonce_key].update(used_at=now, dispatch_id=body['dispatch_id'], principal=principal.id)
        records[quota_key].update(dispatches=used + 1, hour=hour, principal=principal.id, last_at=now)
        records[intent_key].update(status='started', intent=intent, principal=principal.id, dispatch_id=body['dispatch_id'],
                                   operation_id=op, started_at=now)
        grant.clear()
        grant.update(id=grant_id, operation_id=op, scope=item['key'], provider_contract=contract.digest(), principal=principal.id,
                     intent=intent, issued_at=now, expires_at=(instant(now) + timedelta(seconds=GRANT_SECONDS)).isoformat())
    return None, grant_id


def _respond(code, **body):
    return code, body


def handle(method, headers, body, deps):
    """(status code, JSON-able body). Fails closed on anything malformed; never echoes credentials."""
    if not isinstance(deps, Deps) or not isinstance(deps.authorizer, hosted.Authorizer):
        return _respond(503, status='not_configured')
    if not isinstance(headers, Mapping) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in headers.items()):
        return _respond(401, status='unauthorized')
    # Own every policy and input before any dependency callback can change the originals.
    recipients, collectors = _freeze_recipients(deps.recipients), hosted._freeze_collectors(deps.collectors)
    issuers = hosted.authority.freeze_issuers(deps.issuers)
    try:
        body = deepcopy(body)
        headers = dict(headers)
        deps = replace(deps, config=deepcopy(deps.config), entries=deepcopy(deps.entries), schemas=deepcopy(deps.schemas),
                       collectors=collectors, recipients=recipients, issuers=issuers)
        body_digest = sf.digest(body)
    except Exception:
        return _respond(400, status='bad_request', detail='The body must be plain JSON with finite numbers.')
    sender, contract, base = deps.sender, deps.provider_contract, _run_deps(deps)
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
    named = FIELDS - {'mode', 'nonce', 'timestamp'}
    if not isinstance(mode, str) or mode not in MODES or any(
            not isinstance(body.get(k), str) or not body[k].strip() or body[k] != body[k].strip() or len(body[k]) > 400 for k in named):
        return _respond(400, status='bad_request', detail='mode dry_run or dispatch, and exact text for: ' + ', '.join(sorted(named)) + '.')
    try:
        age = abs((instant(deps.clock()) - instant(body.get('timestamp'))).total_seconds())
    except Exception:
        age = None
    if age is None or age > hosted.REQUEST_WINDOW_SECONDS:
        return _respond(401, status='stale_or_undated_request')
    if mode == 'dispatch' and (not isinstance(body.get('nonce'), str) or not 16 <= len(body['nonce']) <= 200):
        return _respond(400, status='bad_request', detail='A dispatch needs a single-use nonce of 16 to 200 characters.')
    op = body['operation_id']

    # ---- authorization: the whole hosted run preflight again, then what only a dispatch needs. No source is read.
    try:
        started = deps.clock()
        checks, scopes, issues, digest, router, mapping, run_approval = hosted.preflight(base, principal)
    except Exception as error:
        return _respond(409, status='not_authorized', sources_read=0, sender_calls=0, detail='Preflight failed: ' + type(error).__name__)
    def check(name, ok, detail=''):
        checks.append(dict(check=name, ok=bool(ok), detail='' if ok else detail))
    trusted = transport.verified(contract)
    check('dispatch_sender_configured', callable(sender), 'No sender was supplied. None exists outside the test suite.')
    check('provider_contract_verified', trusted, 'No verified provider contract: delivery transport is disabled.')
    check('recipient_policy_configured', recipients is not None, 'No server-side recipient policy is configured.')
    check('dispatch_quota_configured', isinstance(deps.dispatch_quota, hosted.Quota) and isinstance(deps.dispatch_quota.runs_per_hour, int)
          and not isinstance(deps.dispatch_quota.runs_per_hour, bool) and deps.dispatch_quota.runs_per_hour > 0, 'No dispatch quota is configured.')
    found = [x for x in scopes if all(x['scope'].get(k) == body[k] for k in SCOPE)]
    item = found[0] if len(found) == 1 else None
    check('scope_is_exactly_one_approved_planned_scope', item is not None,
          'The named client, job, site, list, business date and shift are not one scope of the approved plan.')
    allowed = dict(recipients or ()).get(body['client_id'])
    check('recipient_is_the_approved_one_for_this_client', allowed == body['recipient'] == RECIPIENT,
          'The recipient is not the one the server-side policy names for this client, or is not the internal audience.')
    approval = dispatch_digest(digest, recipients, contract, run_approval['identity']) if digest and recipients is not None and trusted and run_approval else None
    ok, why, dispatch_approval = _approved(base, approval, principal) if approval else (False, 'There is nothing complete enough to approve for dispatch.', None)
    check('dispatch_approved_for_this_snapshot_recipients_contract_and_principal', ok, why)
    row = next((m for m in mapping if item and all(m[k] == item['scope'][k] for k in ('client_id', 'job', 'site_id', 'list_id'))), None)
    bounds = [x['expires_at'] for x in (run_approval, dispatch_approval, (row or {}).get('capture')) if x]
    check('authorizations_stay_valid_long_enough_to_send', len(bounds) < 3 or all(           # a missing one already failed its own check above
              (instant(b) - instant(started)).total_seconds() > GRANT_SECONDS for b in bounds),
          'The run approval, the dispatch approval or the capture receipt is missing or expires within %d seconds.' % GRANT_SECONDS)
    authorized = all(c['ok'] for c in checks)
    report = dict(mode=mode, dispatch_id=body['dispatch_id'], principal=principal.id, snapshot_digest=digest, dispatch_digest=approval,
                  checks=checks, authorized=authorized, issues=issues, run_approval=run_approval and {k: v for k, v in run_approval.items() if k != 'identity'},
                  dispatch_approval=dispatch_approval and {k: v for k, v in dispatch_approval.items() if k != 'identity'}, scope={k: body[k] for k in SCOPE}, operation_id=op,
                  audience=RECIPIENT, client_release='held', source_send_race=RACE)
    if not authorized:
        return _respond(200 if mode == 'dry_run' else 409, status='dry_run' if mode == 'dry_run' else 'not_authorized',
                        sources_read=0, sender_calls=0, **report)
    intent = sf.digest(dict(principal=principal.id, dispatch=approval, run_approval=run_approval['identity'],
                            dispatch_approval=dispatch_approval['identity'], scope=item['key'], recipient=body['recipient'],
                            artifact={k: body[k] for k in ARTIFACT}))
    if mode == 'dispatch':
        earlier = _recorded(deps, principal, body, intent)
        if earlier is not None:                                              # answered from the record: nothing read, nothing sent
            return _respond(earlier[0], **dict(report, **dict(earlier[1], sources_read=0, sender_calls=0)))
    state, details = _draft(deps, item, body)
    report['draft'] = dict(state=state, details=details)
    if mode == 'dry_run':
        return _respond(200, status='dry_run', sources_read=0, sender_calls=0, **report)
    if state != 'ready':
        return _respond(409, status=state, sources_read=0, sender_calls=0, **report)
    waiting = site_hold(deps.store, item['scope']['site_id'], deps.clock())
    if waiting:                                                              # the site asked to be left alone: not probed, not read
        return _respond(409, status='held', reason='site_cooldown', not_before=waiting.get('not_before'),
                        manual_release_required=waiting.get('manual_release_required') is True, sources_read=0, sender_calls=0, **report)
    refused, grant_id = _reserve(deps, principal, approval, body, item, intent, contract)
    if refused:
        return _respond(429 if refused == 'quota_exceeded' else 409, status=refused, sources_read=0, sender_calls=0, **report)

    # ---- under the scope's lease: live schema, source re-read, authorities re-asked; then the fenced claim; then send
    guard, seen = hosted._DriftGuard(base, mapping), dict(reads=0, sends=0, refusal=None, detail=None)

    def withdrawn():
        """None, or why this dispatch may not go on. Asks the authorities; never the source. Each approval is ONE record
        judged whole and must be the SAME record as at authorization; the capture receipt must be the SAME receipt
        (collector, capture instant, origin, record id, issuer, validity; site, list, schema and snapshot are checked
        against the frozen row), not merely one that is valid now."""
        why = hosted.still_authorized(base, principal, digest, run_approval, mapping, started, item['scope'])
        if why:
            return why
        still, reason, now_ = _approved(base, approval, principal)
        if not still:
            return 'dispatch_approval_withdrawn: ' + reason
        if now_['identity'] != dispatch_approval['identity']:
            return 'dispatch_approval_is_another_record_than_the_one_this_call_was_authorized_under'
        if (instant(deps.clock()) - instant(started)).total_seconds() > GRANT_SECONDS:
            return 'dispatch_took_too_long'
        return None

    def stop(why):
        seen['refusal'], seen['detail'] = why.split(': ', 1)[0], why
        raise Revoked(why)

    def fetch(scope, settings):
        why = withdrawn()                                                    # immediately before the source is touched
        if why:
            stop(why)
        problem = guard.problem(scope.get('site_id'), scope.get('list_id'))
        if problem:
            seen['refusal'] = 'schema:' + problem
            raise hosted.SchemaDrift(problem)                                # the source is not read
        seen['reads'] += 1
        snapshot = base.fetch(scope, settings)
        why = withdrawn()                                                    # and again after the read, before the claim
        if why:
            stop(why)
        return snapshot

    def send(*, idempotency_key, attempt_id, audience, payload):
        at = instant(deps.clock())                                           # clock only; no authority is asked at this point
        if at < instant(started) or any(at >= instant(b) for b in bounds):
            seen['refusal'] = 'validity_passed_or_clock_went_back_before_the_send'
            raise Revoked(seen['refusal'])                                   # the sender is NOT called; the attempt is recorded as returned in error
        receipt = payload.get('receipt') if isinstance(payload, dict) else None
        if audience != body['recipient'] or idempotency_key != op or not isinstance(receipt, dict) or any(receipt.get(k) != body[k] for k in ARTIFACT):
            raise Revoked('The claimed entry is not the authorized artifact for the authorized recipient.')
        seen['sends'] += 1
        return sender(idempotency_key=idempotency_key, attempt_id=attempt_id, audience=audience, payload=payload)

    worker = ScheduledWorker(deps.store, fetch, item['render'], clock=deps.clock, enabled=True, owner='dispatch:' + body['dispatch_id'],
                             provider_contract=contract)
    outcome = transport.deliver(worker, item['scope'], op, send, item['settings'], grant=dict(id=grant_id))
    status = outcome.get('status')
    code = 200 if status == 'sent' else 202 if status == 'uncertain' else 409
    result = dict(status=status, outcome=outcome, refusal=seen['refusal'], refusal_detail=seen['detail'], sources_read=seen['reads'], schema_probes=guard.count,
                  sender_calls=seen['sends'])
    with deps.store.transaction(_keys(principal, body)[1]) as record:
        record.update(status='finished', code=code, response=result, finished_at=deps.clock())
    return _respond(code, **dict(report, **result))
