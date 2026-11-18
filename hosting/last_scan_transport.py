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
Nothing here claims exactly-once delivery. With a verified contract the claim is only: at most one attempt is open
at a time, and another attempt is made only on the provider's terminal, attempt-bound proof.
"""
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
