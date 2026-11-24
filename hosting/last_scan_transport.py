"""Delivery transport gate for the last-scan draft pipeline. NO transport is enabled: the registry below is empty.

Nothing a caller passes at run time (a flag, a lookup answer, arbitrary JSON, a registry of its own) can enable
sending or retrying. That takes a ``ProviderContract`` whose exact content hash is listed in ``VERIFIED_CONTRACTS``
in this file, i.e. a reviewed code change made after the provider's behaviour has been independently verified.
The functions below read that one registry and accept no other. Until then:
  * ``deliver`` refuses to send;
  * an uncertain delivery is never retried and reconciliation does not even query a provider.
A verified contract is necessary and NOT sufficient. ``deliver`` also needs a single-use dispatch grant: a record in
the durable store that names this exact scope, operation and contract, written by the hosted dispatch boundary
(hosting.last_scan_dispatch) in the same transaction as its nonce, quota and intent, and consumed here. A caller that
brings a worker, a contract and a sender but no such grant is refused before any source is read. This is a wiring
boundary, not cryptography: code that can write the store can write a grant, and ``ScheduledWorker.dispatch_test``
remains directly callable by tests. Nothing in production calls either.
What a provider adapter answers is judged by ``judge_outcome`` below, under a strict versioned outcome contract.
Accepted is not delivered; a late answer, a disconnect or no answer is not a failure; and a request deadline is
something the adapter must enforce and report, not something this code can impose on a request already sent.
Nothing here claims exactly-once delivery. With a verified contract the claim is only: at most one attempt is open
at a time, and another attempt is made only on the provider's terminal, attempt-bound proof.
"""
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from types import MappingProxyType

from hosting import source_first as sf
from hosting.last_scan_drafts import instant, scope_key

# (provider, version) -> sha256 of the reviewed ProviderContract. EMPTY ON PURPOSE: no provider has been verified.
VERIFIED_CONTRACTS = MappingProxyType({})


@dataclass(frozen=True)
class ProviderContract:
    provider: str
    version: str
    at_most_once_per_idempotency_key: bool = False     # the provider delivers at most once per operation ID
    terminal_status_per_attempt: bool = False          # it can state, per attempt, a FINAL not-delivered status
    honours_attempt_closed: bool = False               # once it says an attempt is closed, a late arrival is rejected
    sender_timeout_seconds: int = 0                    # the sender call is cut off after this long
    retry_after_terminal_non_delivery: bool = False    # the reviewers allow a new attempt after such proof
    verified_by: str = ''
    verified_at: str = ''
    evidence: str = ''                                 # where the verification record lives
    outcome_contract_version: int = 0                  # the adapter answers in this version of the outcome contract below (0: none)
    valid_until: str = ''                              # the review lapses at this time; a dispatch needs it stated and in the future

    def digest(self):
        return sf.digest(asdict(self))


def verified(contract):
    """True only for a contract whose exact content is listed in VERIFIED_CONTRACTS above. No other registry exists."""
    registry = VERIFIED_CONTRACTS
    return (isinstance(contract, ProviderContract) and isinstance(registry, Mapping)
            and registry.get((contract.provider, contract.version)) == contract.digest()
            and contract.at_most_once_per_idempotency_key is True
            and all(isinstance(v, str) and v.strip() for v in (contract.provider, contract.version, contract.verified_by,
                                                               contract.verified_at, contract.evidence)))


def permits_retry(contract, attempt_seconds=None):
    """A new attempt after an unknown outcome needs every one of these. A deadline alone never does."""
    return (verified(contract) and contract.terminal_status_per_attempt is True
            and contract.honours_attempt_closed is True and contract.retry_after_terminal_non_delivery is True
            and isinstance(contract.sender_timeout_seconds, int) and not isinstance(contract.sender_timeout_seconds, bool)
            and isinstance(attempt_seconds, (int, float)) and 0 < contract.sender_timeout_seconds <= attempt_seconds)


GRANT = 'dispatch:grant:'                 # one record per scope and operation; see grant_key


def grant_key(scope, op):
    return GRANT + sf.digest(dict(scope=sf.digest(scope_key(scope)), operation_id=op))


def _consume(worker, scope, op, grant):
    """True once, for the grant the hosted dispatch boundary stored for exactly this scope, operation and contract."""
    store, clock = getattr(worker, 'store', None), getattr(worker, 'clock', None)
    if not isinstance(grant, Mapping) or not isinstance(grant.get('id'), str) or not grant['id'] or not isinstance(op, str) \
            or not hasattr(store, 'atomic') or not callable(clock):
        return False
    try:
        key, now = grant_key(scope, op), instant(clock())
        with store.atomic([key]) as records:
            held = records[key]
            if (held.get('id') != grant['id'] or held.get('operation_id') != op or held.get('scope') != sf.digest(scope_key(scope))
                    or held.get('provider_contract') != worker.provider_contract.digest() or held.get('used_at')
                    or instant(held.get('expires_at')) <= now):
                return False
            held['used_at'] = clock()
        return True
    except Exception:
        return False


def deliver(worker, scope, op, sender, settings=None, *, grant=None):
    """The only door to a sender outside tests. Closed unless the worker carries a verified provider contract AND the
    hosted dispatch boundary stored an unused, unexpired grant for exactly this scope, operation and contract."""
    if not verified(getattr(worker, 'provider_contract', None)):
        return dict(status='transport_disabled')
    if not _consume(worker, scope, op, grant):
        return dict(status='dispatch_not_authorized')        # nothing read, nothing claimed, nothing sent
    return worker.dispatch_test(scope, op, sender, settings)


# ---- what a provider adapter must answer -------------------------------------------------------------------------
# No provider adapter exists. This is the ONLY shape of answer the hosted dispatch boundary will read from one, and
# the only state it will record as delivered is a terminal "delivered" that names exactly this operation, attempt,
# idempotency key, recipient and artifact, from exactly the reviewed provider contract, with the provider's own record.
OUTCOME_CONTRACT, OUTCOME_VERSION = 'last_scan.transport_outcome', 1
OUTCOME_STATES = ('delivered', 'accepted', 'not_delivered', 'unknown')
OUTCOME_BINDING = ('operation_id', 'attempt_id', 'idempotency_key', 'recipient', 'workbook_sha256', 'checklist_sha256')
OUTCOME_REQUIRED = ('contract', 'version', 'provider', 'provider_contract', 'provider_record_id') + OUTCOME_BINDING + (
    'state', 'terminal', 'attempt_closed', 'observed_at', 'request_deadline_seconds', 'elapsed_seconds')
OUTCOME_OPTIONAL = ('detail',)
OUTCOME_EVIDENCE_BYTES = 4000           # a larger answer is kept as its hash only
OUTCOME_SKEW_SECONDS = 300              # placeholder: how far a provider's clock may differ from the host's


def judge_outcome(answer, expected, contract, sent_at, received_at):
    """What ONE answer from a provider adapter establishes. Returns
    {outcome, reasons, deadline_breach, provider_record_id, observed_at, evidence} where outcome is exactly one of:

      delivered               the provider states, terminally, that THIS attempt of THIS operation reached THIS recipient
                              with THIS artifact, under the reviewed contract, and names its own record of it;
      accepted                the provider took the message and has NOT yet delivered it. Not delivery, and not failure;
      terminal_not_delivered  the provider states, terminally, that this attempt was not delivered and is closed. This
                              is proof to be kept, never by itself a reason to send again;
      unknown                 everything else: a malformed, partial, mismatched or unsupported answer, a state this
                              contract does not define (a cancellation that was only requested, a partial acceptance),
                              or the provider itself saying it does not know.

    Only ``delivered`` is ever recorded as sent. The other three leave the attempt uncertain: an answer that took
    too long, a disconnect or a missing answer is NOT evidence that nothing was delivered, and nothing here cancels a
    request that is already with a provider. ``deadline_breach`` reports that the adapter did not keep the request
    deadline of the reviewed contract (measured on the host clock, or by its own statement); it is evidence about
    the adapter, and never turns a delivery into a failure or a failure into a retry.

    ``sent_at`` and ``received_at`` are the HOST clock immediately before the sender was called and when it returned.
    The answer's own ``observed_at`` must lie between them, give or take OUTCOME_SKEW_SECONDS: an answer dated before
    the request was made, or after it came back, is not an observation of this request. A delivered or not-delivered
    answer must be terminal AND closed; an accepted one must be neither.

    ``provider_record_id`` and ``observed_at`` are returned on their own, taken from the judged record. ``evidence``
    is the answer's hash, plus the answer itself only when it is small enough to keep; nothing may depend on it."""
    def verdict(outcome, reasons, record=None, breach=False):
        body = None
        try:
            body = sf.canonical(record if record is not None else answer)
        except Exception:
            pass
        evidence = dict(sha256=sf.digest(body)) if body is not None else dict(sha256=None, unreadable=type(answer).__name__)
        if record is not None and len(body) <= OUTCOME_EVIDENCE_BYTES:
            evidence['record'] = record
        named = record.get('provider_record_id') if record is not None else None
        seen = record.get('observed_at') if record is not None else None
        return dict(outcome=outcome, reasons=reasons, deadline_breach=bool(breach), evidence=evidence,
                    provider_record_id=named if isinstance(named, str) and named.strip() else None,
                    observed_at=seen if isinstance(seen, str) else None)
    limit = getattr(contract, 'sender_timeout_seconds', None)
    try:
        sent, received = instant(sent_at), instant(received_at)
        elapsed_seconds = (received - sent).total_seconds()
    except (ValueError, OverflowError, TypeError):
        sent = received = elapsed_seconds = None
    slow = elapsed_seconds is None or elapsed_seconds < 0 or not isinstance(limit, int) or isinstance(limit, bool) or elapsed_seconds > limit
    try:
        record = json.loads(sf.canonical(answer)) if isinstance(answer, dict) else None      # one private copy of plain data
    except Exception:
        record = None
    if record is None:
        return verdict('unknown', ['not_a_record'], breach=slow)
    if record.get('contract') != OUTCOME_CONTRACT:
        return verdict('unknown', ['unsupported_or_missing_contract'], record, slow)
    if type(record.get('version')) is not int or record['version'] != OUTCOME_VERSION:
        return verdict('unknown', ['unsupported_version'], record, slow)
    reasons = []
    missing, extra = sorted(set(OUTCOME_REQUIRED) - set(record)), sorted(set(record) - set(OUTCOME_REQUIRED) - set(OUTCOME_OPTIONAL))
    if missing:
        reasons.append('missing_fields:' + ','.join(missing))
    if extra:                                                                # never ignored: it may say "only partly", "cancelled", "to someone else"
        reasons.append('unknown_fields:' + ','.join(extra))
    for field in OUTCOME_BINDING:
        if not isinstance(expected.get(field), str) or record.get(field) != expected[field]:
            reasons.append('is_for_another_' + field)
    if record.get('provider') != contract.provider or record.get('provider_contract') != contract.digest():
        reasons.append('not_from_the_reviewed_provider_contract')
    if not isinstance(record.get('provider_record_id'), str) or not record['provider_record_id'].strip():
        reasons.append('provider_record_id_missing')
    if record.get('state') not in OUTCOME_STATES:
        reasons.append('unsupported_state')
    if type(record.get('terminal')) is not bool or type(record.get('attempt_closed')) is not bool:
        reasons.append('terminal_and_attempt_closed_must_be_true_or_false')
    try:
        observed = instant(record.get('observed_at'))
    except (ValueError, OverflowError, TypeError):
        reasons.append('observed_at_missing_malformed_or_without_offset')
    else:
        if sent is None or elapsed_seconds < 0:
            reasons.append('no_readable_request_window_on_the_host_clock')
        elif not (sent.timestamp() - OUTCOME_SKEW_SECONDS <= observed.timestamp() <= received.timestamp() + OUTCOME_SKEW_SECONDS):
            reasons.append('observed_at_outside_the_request_window')
    stated, took = record.get('request_deadline_seconds'), record.get('elapsed_seconds')
    numbers = all(isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 for v in (stated, took))
    if not numbers or stated != limit:
        reasons.append('does_not_state_the_reviewed_request_deadline')
    breach = slow or not numbers or took > limit
    if reasons:
        return verdict('unknown', reasons, record, breach)
    state, terminal, closed = record['state'], record['terminal'], record['attempt_closed']
    if state == 'delivered':
        return verdict('delivered', [], record, breach) if terminal and closed else verdict('unknown', ['delivered_without_terminal_closure'], record, breach)
    if state == 'accepted':
        return verdict('accepted', [], record, breach) if not terminal and not closed else verdict('unknown', ['accepted_cannot_be_terminal_or_closed'], record, breach)
    if state == 'not_delivered':
        return verdict('terminal_not_delivered', [], record, breach) if terminal and closed else verdict('unknown', ['not_delivered_without_terminal_closure'], record, breach)
    return verdict('unknown', ['provider_does_not_know'], record, breach)
