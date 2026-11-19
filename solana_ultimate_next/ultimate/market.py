"""Normalize DexScreener observations and explain the market score."""
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import re

ADDRESS = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}")


def address(value):
    return isinstance(value, str) and ADDRESS.fullmatch(value) is not None


def mapping(value):
    return value if isinstance(value, Mapping) else {}


def number(value, *, minimum=0):
    if isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if result.is_finite() and minimum <= result <= Decimal("1e30"):
        return result
    return None


@dataclass(frozen=True)
class Quote:
    mint: str
    pair: str
    symbol: str
    price: Decimal
    observed: int

    def fresh(self, now, max_age):
        return (number(self.price) == self.price and self.price > 0
                and 0 <= now - self.observed <= max_age)


@dataclass(frozen=True)
class Candidate:
    quote: Quote
    name: str
    dex: str
    liquidity: Decimal | None
    volume: Decimal | None
    momentum: Decimal | None
    cap: Decimal | None
    fdv: Decimal | None
    created: int | None

    def components(self):
        return {
            "liquidity": 30 if self.liquidity is not None and self.liquidity >= 3000 else 0,
            "volume": 20 if self.volume is not None and self.volume >= 1000 else 0,
            "momentum": 20 if self.momentum is not None and -30 <= self.momentum <= 100 else 0,
            "valuation": 20 if self.valuation_sane() else 0,
            "pair_timestamp": 10 if self.created is not None else 0,
        }

    def valuation_sane(self):
        return bool(self.cap and self.fdv and self.liquidity
                    and self.cap <= self.fdv * Decimal("1.05")
                    and self.fdv <= self.liquidity * 1000)

    @property
    def score(self):
        return sum(self.components().values())


def normalize(payload, mint, observed):
    pair = mapping(payload)
    base = mapping(pair.get("baseToken"))
    pair_id = pair.get("pairAddress")
    price = number(pair.get("priceUsd"))
    if (pair.get("chainId") != "solana" or base.get("address") != mint
            or not address(mint) or not address(pair_id) or not price
            or price < Decimal("1e-30")):
        return None
    created = number(pair.get("pairCreatedAt"))
    return Candidate(
        Quote(mint, pair_id, str(base.get("symbol") or "?")[:32], price, observed),
        str(base.get("name") or "?")[:100], str(pair.get("dexId") or "?")[:60],
        number(mapping(pair.get("liquidity")).get("usd")),
        number(mapping(pair.get("volume")).get("h24")),
        number(mapping(pair.get("priceChange")).get("h24"), minimum=-100),
        number(pair.get("marketCap")), number(pair.get("fdv")),
        int(created // 1000) if created and created < (observed + 1) * 1000 else None,
    )


def pair_rank(candidate):
    return (candidate.liquidity or 0, candidate.volume or 0, candidate.score, candidate.quote.pair)
