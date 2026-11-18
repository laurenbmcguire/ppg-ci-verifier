"""Delivery transport gate for the last-scan draft pipeline. NO transport is enabled: the registry below is empty.

Nothing a caller passes at run time (a flag, a lookup answer, arbitrary JSON) can enable sending or retrying. That
takes a ``ProviderContract`` whose exact content hash is listed in ``VERIFIED_CONTRACTS`` in this file, i.e. a
reviewed code change made after the provider's behaviour has been independently verified. Until then:
  * ``deliver`` refuses to send;
  * an uncertain delivery is never retried and reconciliation does not even query a provider.
Nothing here claims exactly-once delivery. With a verified contract the claim is only: at most one attempt is open
at a time, and another attempt is made only on the provider's terminal, attempt-bound proof.
"""
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from types import MappingProxyType

from hosting import source_first as sf

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


def verified(contract, registry=None):
    """True only for a contract whose exact content is listed in the registry (default: the empty one above)."""
    registry = VERIFIED_CONTRACTS if registry is None else registry
    return (isinstance(contract, ProviderContract) and isinstance(registry, Mapping)
            and registry.get((contract.provider, contract.version)) == contract.digest()
            and contract.at_most_once_per_idempotency_key is True
            and all(isinstance(v, str) and v.strip() for v in (contract.provider, contract.version, contract.verified_by,
                                                               contract.verified_at, contract.evidence)))


def permits_retry(contract, registry=None, attempt_seconds=None):
    """A new attempt after an unknown outcome needs every one of these. A deadline alone never does."""
    return (verified(contract, registry) and contract.terminal_status_per_attempt is True
            and contract.honours_attempt_closed is True and contract.retry_after_terminal_non_delivery is True
            and isinstance(contract.sender_timeout_seconds, int) and not isinstance(contract.sender_timeout_seconds, bool)
            and isinstance(attempt_seconds, (int, float)) and 0 < contract.sender_timeout_seconds <= attempt_seconds)


def deliver(worker, scope, op, sender, settings=None):
    """The only door to a sender outside tests. Closed unless the worker carries a verified provider contract."""
    if not verified(getattr(worker, 'provider_contract', None), getattr(worker, 'contract_registry', None)):
        return dict(status='transport_disabled')
    return worker.dispatch_test(scope, op, sender, settings)
