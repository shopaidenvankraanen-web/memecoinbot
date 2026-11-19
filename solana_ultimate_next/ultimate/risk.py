"""Only an explicitly wired evidence provider can clear an automatic entry."""
from dataclasses import dataclass
from enum import StrEnum

from .market import Candidate


class Verdict(StrEnum):
    CLEAR = "CLEAR"
    ADVERSE = "ADVERSE"
    UNKNOWN = "UNKNOWN"


CHECKS = (
    "mint_authority", "freeze_authority", "holder_concentration", "wallet_patterns",
    "fake_volume", "sellability", "transfer_restrictions",
)


@dataclass(frozen=True)
class Evidence:
    mint: str
    pair: str
    source: str
    observed: int
    checks: dict[str, Verdict]


@dataclass(frozen=True)
class Assessment:
    classification: str
    checks: dict[str, Verdict]
    market_flags: tuple[str, ...]

    @property
    def allows_automatic_entry(self):
        return self.classification == "CLEAR"


def assess(candidate: Candidate, now, evidence=None, *, trusted_sources=frozenset(), max_age=300):
    quote = candidate.quote
    verified = (evidence is not None and evidence.source in trusted_sources
                and (evidence.mint, evidence.pair) == (quote.mint, quote.pair)
                and 0 <= now - evidence.observed <= max_age)
    states = {}
    for key in CHECKS:
        result = evidence.checks.get(key) if verified else None
        states[key] = result if isinstance(result, Verdict) else Verdict.UNKNOWN
    components = candidate.components()
    flags = [key for key, score in components.items() if score == 0]
    if not quote.fresh(now, max_age):
        flags.append("quote_stale")
    if candidate.liquidity and candidate.volume and candidate.volume > candidate.liquidity * 30:
        flags.append("unusual_volume_liquidity_ratio")
    classification = "CLEAR"
    if Verdict.ADVERSE in states.values():
        classification = "HIGH"
    elif Verdict.UNKNOWN in states.values():
        classification = "UNKNOWN"
    elif flags:
        classification = "ELEVATED"
    return Assessment(classification, states, tuple(flags))
