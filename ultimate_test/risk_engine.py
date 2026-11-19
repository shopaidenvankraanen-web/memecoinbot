"""Explainable Solana token risk scoring for research and paper-only simulations.

Risk checks are conservative: unavailable security evidence is not treated as safe.
All amounts are USD and inputs are untrusted public-market observations.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from math import isfinite
from typing import Any, Mapping


class Severity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


@dataclass(frozen=True)
class Finding:
    code: str
    severity: Severity
    message: str
    penalty: int


@dataclass(frozen=True)
class RiskAssessment:
    score: int
    classification: str
    findings: tuple[Finding, ...]
    missing_evidence: tuple[str, ...]
    eligible_for_paper: bool
    confidence: str


def number(value: Any) -> float:
    try:
        n = float(value)
        return n if isfinite(n) and n >= 0 else 0.0
    except (ValueError, TypeError, OverflowError):
        return 0.0


def evaluate_token(
    pair: Mapping[str, Any],
    *,
    token_security: Mapping[str, Any] | None = None,
    holder_analysis: Mapping[str, Any] | None = None,
    min_liquidity: float = 15_000,
    min_volume: float = 5_000,
) -> RiskAssessment:
    findings: list[Finding] = []
    missing: list[str] = []
    penalty = 0
    blockers = False

    def flag(code: str, severity: Severity, message: str, points: int, block: bool = False) -> None:
        nonlocal penalty, blockers
        findings.append(Finding(code, severity, message, points))
        penalty += points
        blockers = blockers or block

    if pair.get("chainId") != "solana":
        flag("CHAIN", Severity.CRITICAL, "Not a Solana market", 100, True)

    liquidity = number((pair.get("liquidity") or {}).get("usd"))
    volume = number((pair.get("volume") or {}).get("h24"))
    price = number(pair.get("priceUsd"))
    buys = number(((pair.get("txns") or {}).get("h24") or {}).get("buys"))
    sells = number(((pair.get("txns") or {}).get("h24") or {}).get("sells"))
    fdv = number(pair.get("fdv"))
    created = number(pair.get("pairCreatedAt"))

    if not price:
        flag("PRICE_MISSING", Severity.CRITICAL, "No valid market price", 100, True)
    if liquidity < min_liquidity:
        flag("LOW_LIQUIDITY", Severity.CRITICAL, "Insufficient market liquidity", 35, True)
    elif liquidity < min_liquidity * 3:
        flag("MODEST_LIQUIDITY", Severity.WARNING, "Liquidity is relatively limited", 10)
    if volume < min_volume:
        flag("LOW_VOLUME", Severity.WARNING, "Limited 24-hour trading volume", 20, True)
    if volume > liquidity * 30 and liquidity > 0:
        flag("VOLUME_RATIO", Severity.WARNING, "Unusually high volume/liquidity ratio", 15)
    if sells == 0 and buys >= 20:
        flag("NO_SELLS", Severity.CRITICAL, "No sells visible in the 24-hour aggregate", 35, True)
    elif buys > 20 and sells > 0 and buys / sells > 15:
        flag("BUY_SELL_IMBALANCE", Severity.WARNING, "Large buy/sell imbalance", 10)
    if fdv and liquidity and fdv / liquidity > 1000:
        flag("FDV_LIQUIDITY", Severity.WARNING, "Very high FDV relative to liquidity", 15)
    if not created:
        missing.append("pair creation time")
    if token_security is None:
        missing.extend(["mint authority", "freeze authority", "sellability", "liquidity lock"])
    else:
        for key, code, label in (
            ("mint_authority_enabled", "MINT_AUTHORITY", "Mint authority enabled"),
            ("freeze_authority_enabled", "FREEZE_AUTHORITY", "Freeze authority enabled"),
            ("transfer_restrictions", "TRANSFER_RESTRICTIONS", "Potential transfer restrictions"),
        ):
            value = token_security.get(key)
            if value is True:
                flag(code, Severity.CRITICAL, label, 35, True)
            elif value is None:
                missing.append(key)
        if token_security.get("sellability_verified") is False:
            flag("SELLABILITY", Severity.CRITICAL, "Sellability check failed", 60, True)
        elif token_security.get("sellability_verified") is None:
            missing.append("sellability")
        if token_security.get("liquidity_lock_verified") is None:
            missing.append("liquidity lock")

    if holder_analysis is None:
        missing.extend(["holder concentration", "wallet clustering"])
    else:
        top10 = holder_analysis.get("top10_share")
        if top10 is None:
            missing.append("holder concentration")
        elif number(top10) > 0.8:
            flag("CONCENTRATION", Severity.CRITICAL, "Top ten holders control over 80%", 40, True)
        elif number(top10) > 0.55:
            flag("CONCENTRATION", Severity.WARNING, "High top-ten concentration", 20)
        if holder_analysis.get("cluster_risk") is True:
            flag("WALLET_CLUSTER", Severity.CRITICAL, "Suspicious wallet cluster signal", 30, True)
        elif holder_analysis.get("cluster_risk") is None:
            missing.append("wallet clustering")

    score = max(0, 100 - penalty)
    classification = "HIGH" if blockers or score < 40 else "MEDIUM" if score < 75 else "UNKNOWN"
    confidence = "LOW" if len(missing) >= 4 else "MEDIUM" if missing else "HIGH"
    # Paper eligibility does not imply real-world safety or suitability.
    eligible = not blockers and score >= 70 and price > 0
    return RiskAssessment(score, classification, tuple(findings), tuple(dict.fromkeys(missing)), eligible, confidence)
