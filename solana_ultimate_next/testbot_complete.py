"""Solana Scanner Ultimate — standalone paper research CLI (Python 3.11+).

Telegram hosting uses an optional dependency. See README.md for configuration.
"""
import argparse
import asyncio
import json
import logging
import os
import re
import sqlite3
import time
from bisect import bisect_left
from collections import Counter, defaultdict, deque
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from email.utils import parsedate_to_datetime
from enum import StrEnum
from http.client import HTTPException, HTTPSConnection
from inspect import isawaitable, signature
from itertools import groupby
from math import isfinite
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

LOG = logging.getLogger(__name__)


# Normalize DexScreener observations and explain the market score.
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


# Only an explicitly wired evidence provider can clear an automatic entry.
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


# SQLite ownership, migrations, and consistent snapshots.
ROOT = Path(__file__).resolve().parent
MIGRATIONS = (
    (
        """CREATE TABLE profiles (
            mint TEXT PRIMARY KEY, first_seen INTEGER NOT NULL, last_seen INTEGER NOT NULL)""",
        """CREATE TABLE pairs (
            pair TEXT PRIMARY KEY, mint TEXT NOT NULL, created INTEGER)""",
        """CREATE TABLE observations (
            pair TEXT REFERENCES pairs(pair), observed INTEGER NOT NULL,
            price TEXT NOT NULL, score INTEGER NOT NULL, risk TEXT NOT NULL,
            detail TEXT NOT NULL, PRIMARY KEY(pair, observed))""",
        "CREATE TABLE account (id INTEGER PRIMARY KEY CHECK(id=1), cents INTEGER NOT NULL CHECK(cents>=0))",
        "INSERT INTO account VALUES (1, 2000)",
        """CREATE TABLE positions (
            id INTEGER PRIMARY KEY, mint TEXT NOT NULL, pair TEXT NOT NULL, symbol TEXT NOT NULL,
            entry TEXT NOT NULL, opened INTEGER NOT NULL, entry_quote INTEGER NOT NULL,
            stake INTEGER NOT NULL CHECK(stake BETWEEN 1 AND 400),
            entry_fee INTEGER NOT NULL, fee TEXT NOT NULL, slip TEXT NOT NULL,
            stop TEXT NOT NULL, take TEXT NOT NULL, duration INTEGER NOT NULL,
            closed INTEGER, exit TEXT, proceeds INTEGER, exit_fee INTEGER, reason TEXT)""",
        "CREATE UNIQUE INDEX one_open ON positions(mint) WHERE closed IS NULL",
        """CREATE TABLE audit (
            position INTEGER REFERENCES positions(id), kind TEXT NOT NULL,
            at INTEGER NOT NULL, delta INTEGER NOT NULL, reason TEXT NOT NULL,
            PRIMARY KEY(position, kind))""",
    ),
    (
        "CREATE INDEX observations_time ON observations(observed, pair)",
        "CREATE TABLE watch (address TEXT PRIMARY KEY, label TEXT NOT NULL)",
        """CREATE TABLE activity (
            address TEXT NOT NULL, signature TEXT NOT NULL, block_time INTEGER, success INTEGER,
            PRIMARY KEY(address, signature))""",
        "CREATE TABLE settings (key TEXT PRIMARY KEY, value INTEGER NOT NULL)",
        """CREATE TABLE notifications (
            recipient INTEGER, key TEXT, category TEXT NOT NULL, message TEXT NOT NULL,
            created INTEGER NOT NULL, state TEXT NOT NULL, PRIMARY KEY(recipient, key))""",
    ),
    (
        "ALTER TABLE positions ADD COLUMN entry_pair_age INTEGER CHECK(entry_pair_age >= 0)",
        """ALTER TABLE positions ADD COLUMN entry_risk TEXT CHECK(
            entry_risk IN ('CLEAR','ELEVATED','HIGH','UNKNOWN'))""",
    ),
)


def owned(path):
    path = Path(path).resolve()
    if ROOT not in path.parents:
        raise ValueError("SQLite path must be inside solana_ultimate_next")
    return path


class Store:
    def __init__(self, path=None):
        self.path = owned(path if path is not None else ROOT / "runtime" / "testbot-paper.sqlite")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
        with self.transaction(write=True) as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > len(MIGRATIONS):
                raise ValueError("Unsupported schema version")
            for index, statements in enumerate(MIGRATIONS[version:], version + 1):
                for statement in statements:
                    db.execute(statement)
                db.execute(f"PRAGMA user_version={index}")

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self, *, write=False):
        with self.connection() as db:
            with db:
                db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                yield db

    def rows(self, sql, args=()):
        with self.transaction() as db:
            return [dict(row) for row in db.execute(sql, args)]

    def status(self):
        with self.transaction() as db:
            return {
                "integrity": [row[0] for row in db.execute("PRAGMA integrity_check")],
                "foreign_key_violations": len(db.execute("PRAGMA foreign_key_check").fetchall()),
                "version": db.execute("PRAGMA user_version").fetchone()[0],
                "bytes": self.path.stat().st_size,
            }

    def backup(self):
        path = owned(self.path.parent / f"backup-{uuid4().hex}.sqlite")
        with self.connection() as source:
            with sqlite_connection(path) as target:
                source.backup(target)
                if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("Backup integrity check failed")
        return path

    @staticmethod
    def recover(backup, destination):
        backup, destination = owned(backup), owned(destination)
        with sqlite_connection(backup.as_uri() + "?mode=ro", uri=True) as source:
            version = source.execute("PRAGMA user_version").fetchone()[0]
            if (not 1 <= version <= len(MIGRATIONS)
                    or source.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
                    or source.execute("PRAGMA foreign_key_check").fetchone() is not None):
                raise ValueError("Backup validation failed")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.touch(exist_ok=False)
            try:
                with sqlite_connection(destination) as target:
                    source.backup(target)
                return Store(destination)
            except Exception:
                destination.unlink()
                raise


@contextmanager
def sqlite_connection(path, **options):
    db = sqlite3.connect(path, **options)
    try:
        yield db
    finally:
        db.close()


# Validated read-only provider calls through an explicitly supplied transport.
PROFILES = "https://api.dexscreener.com/token-profiles/latest/v1"
PAIRS = "https://api.dexscreener.com/token-pairs/v1/solana/"
PAIR_BY_ID = "https://api.dexscreener.com/latest/dex/pairs/solana/"
RPC = "https://api.mainnet-beta.solana.com"


@dataclass(frozen=True)
class Response:
    status: int
    payload: object
    retry_after: str | None = None


class ProviderError(RuntimeError):
    pass


def retry_delay(value, attempt):
    delay = 2 ** attempt
    if value:
        try:
            delay = float(value)
        except ValueError:
            try:
                timestamp = parsedate_to_datetime(value)
                delay = (timestamp - datetime.now(timezone.utc)).total_seconds()
            except (ValueError, TypeError, OverflowError):
                pass
    return 2 ** attempt if not isfinite(delay) else max(0, delay)


class PublicData:
    def __init__(self, transport, *, concurrency=5, timeout=10, attempts=3, sleep=asyncio.sleep):
        if concurrency < 1 or attempts < 1 or timeout <= 0:
            raise ValueError("Invalid HTTP limits")
        self.transport = transport
        self.semaphore = asyncio.Semaphore(concurrency)
        self.timeout = timeout
        self.attempts = attempts
        self.sleep = sleep
        self.metrics = Counter()

    async def _request(self, method, url, body, validate):
        for attempt in range(self.attempts):
            self.metrics["attempts"] += 1
            delay = 2 ** attempt
            try:
                async with self.semaphore:
                    response = await asyncio.wait_for(self.transport(method, url, body), self.timeout)
                status = response.status
                self.metrics[f"http_{status}"] += 1
                if status == 200:
                    if not validate(response.payload):
                        self.metrics["invalid_payloads"] += 1
                        raise ProviderError("Invalid provider payload")
                    self.metrics["successes"] += 1
                    return response.payload
                if status == 429:
                    delay = retry_delay(response.retry_after, attempt)
                    if delay > 60:
                        raise ProviderError("Provider retry window exceeds scan budget")
                elif status < 500:
                    raise ProviderError(f"HTTP {status}")
            except (TimeoutError, OSError) as error:
                self.metrics[type(error).__name__] += 1
            if attempt + 1 < self.attempts:
                self.metrics["retries"] += 1
                await self.sleep(delay)
        self.metrics["exhausted"] += 1
        raise ProviderError("Provider unavailable")

    async def profiles(self):
        return await self._request("GET", PROFILES, None, lambda body: isinstance(body, list))

    async def pairs(self, mint):
        if not address(mint):
            raise ValueError("Invalid mint")
        return await self._request("GET", PAIRS + mint, None, lambda body: isinstance(body, list))

    async def pair_by_id(self, pair):
        if not address(pair):
            raise ValueError("Invalid pair")
        result = await self._request(
            "GET", PAIR_BY_ID + pair, None, lambda body: isinstance(body, dict)
            and "pairs" in body and (body["pairs"] is None or isinstance(body["pairs"], list)))
        return result["pairs"] or []

    async def _wallet_rpc(self, wallet, method, options, validate):
        if not address(wallet):
            raise ValueError("Invalid public address")
        request = {"jsonrpc": "2.0", "id": 1, "method": method,
                   "params": [wallet, dict(options, commitment="confirmed")]}
        result = await self._request(
            "POST", RPC, request, lambda body: isinstance(body, dict)
            and type(body.get("id")) is int and body["id"] == 1 and "error" not in body
            and validate(body.get("result")))
        return result["result"]

    async def wallet_activity(self, wallet):
        return await self._wallet_rpc(
            wallet, "getSignaturesForAddress", {"limit": 30}, lambda result: isinstance(result, list))

    async def wallet_balance(self, wallet):
        result = await self._wallet_rpc(
            wallet, "getBalance", {}, lambda result: isinstance(result, dict)
            and type(result.get("value")) is int and 0 <= result["value"] < 2**64
            and isinstance(result.get("context"), dict)
            and type(result["context"].get("slot")) is int and result["context"]["slot"] >= 0)
        return {"lamports": result["value"], "slot": result["context"]["slot"]}


class RecordedTransport:
    """One captured frame. Strictly in-memory and never opens a socket."""

    def __init__(self, frame):
        self.frame = frame

    async def __call__(self, method, url, body):
        if (method, url) == ("GET", PROFILES):
            return Response(200, self.frame.get("profiles", []))
        if method == "GET" and url.startswith(PAIRS):
            return Response(200, self.frame.get("pairs", {}).get(url.removeprefix(PAIRS), []))
        if method == "GET" and url.startswith(PAIR_BY_ID):
            pair = url.removeprefix(PAIR_BY_ID)
            if address(pair) and pair in self.frame.get("pair_quotes", {}):
                return Response(200, self.frame["pair_quotes"][pair])
        if (method, url) == ("POST", RPC) and isinstance(body, dict):
            source = {"getSignaturesForAddress": "wallets", "getBalance": "wallet_balances"}.get(
                body.get("method"))
            params = body.get("params")
            if source and isinstance(params, list) and params and address(params[0]):
                records = self.frame.get(source, {})
                if params[0] in records:
                    return Response(200, {"id": 1, "result": records[params[0]]})
        return Response(404, None)


# Atomic virtual EUR ledger priced using fixed-FX USD price ratios.
def rounded(value):
    return int(value.to_integral_value(rounding=ROUND_HALF_UP))


@dataclass(frozen=True)
class Policy:
    stake: int = 200
    positions: int = 3
    fee: Decimal = Decimal("0.01")
    slip: Decimal = Decimal("0.005")
    stop: Decimal = Decimal("0.15")
    take: Decimal = Decimal("0.25")
    duration: int = 21600
    quote_age: int = 120

    def __post_init__(self):
        if (type(self.stake) is not int or not 1 <= self.stake <= 400
                or any(type(n) is not int or n < 1 for n in
                       (self.positions, self.duration, self.quote_age))
                or any(not isinstance(n, Decimal) or not n.is_finite() or not 0 <= n < 1
                       for n in (self.fee, self.slip, self.stop, self.take))
                or not self.stop or not self.take):
            raise ValueError("Invalid paper policy")


class Exit(StrEnum):
    STOP = "stop_loss"
    TAKE = "take_profit"
    AGE = "age_limit"
    MANUAL = "manual"


class Paper:
    def __init__(self, store, policy=None):
        self.store = store
        self.policy = policy or Policy()

    def open_positions(self):
        return self.store.rows("SELECT * FROM positions WHERE closed IS NULL ORDER BY id")

    def enter(self, quote, now, stake=None, *, automatic=False, assessment=None):
        policy = self.policy
        stake = policy.stake if stake is None else stake
        if (type(stake) is not int or not 1 <= stake <= 400
                or not quote.fresh(now, policy.quote_age)):
            return None
        if automatic and (assessment is None or not assessment.allows_automatic_entry):
            return None
        fee = rounded(stake * policy.fee)
        cost = stake + fee
        with self.store.transaction(write=True) as db:
            if automatic and db.execute(
                    "SELECT 1 FROM settings WHERE key='paused' AND value=1").fetchone():
                return None
            count, duplicate = db.execute(
                "SELECT COUNT(*), MAX(mint=?) FROM positions WHERE closed IS NULL",
                (quote.mint,)).fetchone()
            if count >= policy.positions or duplicate:
                return None
            if db.execute("UPDATE account SET cents=cents-? WHERE id=1 AND cents>=?",
                          (cost, cost)).rowcount != 1:
                return None
            observation = db.execute("""
                SELECT o.detail, o.risk FROM observations o JOIN pairs p USING(pair)
                WHERE o.pair=? AND o.observed=? AND p.mint=?""",
                (quote.pair, quote.observed, quote.mint)).fetchone()
            created = json.loads(observation["detail"]).get("created") if observation else None
            pair_age = now - created if type(created) is int and 0 < created <= now else None
            risk = observation["risk"] if observation else None
            if risk not in ("CLEAR", "ELEVATED", "HIGH", "UNKNOWN"):
                risk = None
            cursor = db.execute("""
                INSERT INTO positions
                (mint,pair,symbol,entry,opened,entry_quote,stake,entry_fee,fee,slip,stop,take,duration,
                 entry_pair_age,entry_risk)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (quote.mint, quote.pair, quote.symbol, str(quote.price), now, quote.observed,
                 stake, fee, str(policy.fee), str(policy.slip), str(policy.stop),
                 str(policy.take), policy.duration, pair_age, risk))
            trade_id = cursor.lastrowid
            reason = "automatic" if automatic else "manual"
            db.execute("INSERT INTO audit VALUES (?, 'entry', ?, ?, ?)",
                       (trade_id, now, -cost, reason))
            return {"id": trade_id, "kind": "entry", "cash_delta_cents": -cost, "reason": reason}

    def close(self, trade_id, quote, now, *, manual=False):
        if quote is None or not quote.fresh(now, self.policy.quote_age):
            return None
        with self.store.transaction(write=True) as db:
            position = db.execute("SELECT * FROM positions WHERE id=? AND closed IS NULL",
                                  (trade_id,)).fetchone()
            if (position is None or (position["mint"], position["pair"]) != (quote.mint, quote.pair)
                    or quote.observed <= position["entry_quote"] or quote.observed < position["opened"]):
                return None
            ratio = quote.price / Decimal(position["entry"])
            reason = Exit.MANUAL if manual else self.exit_reason(position, ratio, quote.observed)
            if reason is None:
                return None
            settlement = self.settlement(position, ratio)
            if settlement is None:
                return None
            proceeds, exit_fee = settlement
            db.execute("""
                UPDATE positions SET closed=?, exit=?, proceeds=?, exit_fee=?, reason=?
                WHERE id=?""", (now, str(quote.price), proceeds, exit_fee, reason, trade_id))
            db.execute("UPDATE account SET cents=cents+? WHERE id=1", (proceeds,))
            db.execute("INSERT INTO audit VALUES (?, 'exit', ?, ?, ?)",
                       (trade_id, now, proceeds, reason))
            return {"id": trade_id, "kind": "exit", "cash_delta_cents": proceeds,
                    "pnl_cents": proceeds - position["stake"] - position["entry_fee"],
                    "reason": reason}

    @staticmethod
    def settlement(position, ratio):
        slip = Decimal(position["slip"])
        gross = position["stake"] * ratio * (1 - slip) / (1 + slip)
        if gross > Decimal("1000000000000"):
            return None
        fee = rounded(gross * Decimal(position["fee"]))
        return rounded(gross) - fee, fee

    @staticmethod
    def exit_reason(position, ratio, observed):
        if ratio <= 1 - Decimal(position["stop"]):
            return Exit.STOP
        if ratio >= 1 + Decimal(position["take"]):
            return Exit.TAKE
        if observed - position["opened"] >= position["duration"]:
            return Exit.AGE
        return None

    def report(self, *, now=None):
        with self.store.transaction() as db:
            cents = db.execute("SELECT cents FROM account").fetchone()[0]
            rows = db.execute("SELECT * FROM positions ORDER BY closed,id").fetchall()
            valuation = self.valuation(db, now, cents) if now is not None else None
        closed = [row for row in rows if row["closed"] is not None]
        pnl = [row["proceeds"] - row["stake"] - row["entry_fee"] for row in closed]
        wins = [value for value in pnl if value > 0]
        losses = [-value for value in pnl if value < 0]
        equity = peak = 2000
        drawdown = 0
        win_streak = loss_streak = best_win = best_loss = 0
        for value in pnl:
            equity += value
            peak = max(equity, peak)
            drawdown = max(drawdown, (peak - equity) / peak)
            win_streak = win_streak + 1 if value > 0 else 0
            loss_streak = loss_streak + 1 if value < 0 else 0
            best_win = max(best_win, win_streak)
            best_loss = max(best_loss, loss_streak)
        result = {
            "starting_cents": 2000, "cash_cents": cents, "open": len(rows)-len(closed),
            "closed": len(closed), "realized_pnl_cents": sum(pnl),
            "fees_cents": sum(row["entry_fee"] + (row["exit_fee"] or 0) for row in rows),
            "win_rate": len(wins) / len(pnl) if pnl else None,
            "profit_factor": sum(wins) / sum(losses) if losses else None,
            "average_pnl_cents": sum(pnl) / len(pnl) if pnl else None,
            "average_win_cents": sum(wins) / len(wins) if wins else None,
            "average_loss_cents": sum(losses) / len(losses) if losses else None,
            "realized_drawdown": drawdown, "longest_win_streak": best_win,
            "longest_loss_streak": best_loss,
        }
        if valuation is not None:
            def extreme(index):
                row = closed[index]
                return {"id": row["id"], "pnl_cents": pnl[index]}

            cost = sum(row["stake"] + row["entry_fee"] for row in closed)
            result.update(
                wins=len(wins), losses=len(losses), breakeven=len(pnl) - len(wins) - len(losses),
                realized_return_on_closed_cost=sum(pnl) / cost if cost else None,
                average_holding_seconds=(sum(row["closed"] - row["opened"] for row in closed)
                                         / len(closed) if closed else None),
                best_trade=extreme(max(range(len(pnl)), key=pnl.__getitem__)) if pnl else None,
                worst_trade=extreme(min(range(len(pnl)), key=pnl.__getitem__)) if pnl else None,
                exit_reasons=dict(Counter(row["reason"] for row in closed)),
                valuation=valuation,
                segments=self.segment_rows(rows),
            )
        return result

    def segments(self):
        return self.segment_rows(self.store.rows("SELECT * FROM positions"))

    @staticmethod
    def segment_rows(rows):
        groups = {"pair_age_at_entry": {}, "risk_at_entry": {}}
        for row in rows:
            age = row["entry_pair_age"]
            bucket = "unknown"
            if age is not None:
                bucket = next(label for upper, label in (
                    (3600, "under_1h"), (86400, "1h_to_24h"), (604800, "1d_to_7d"),
                    (float("inf"), "7d_or_more"),
                ) if age < upper)
            for dimension, label in (("pair_age_at_entry", bucket),
                                     ("risk_at_entry", row["entry_risk"] or "UNKNOWN")):
                group = groups[dimension].setdefault(label, {
                    "open": 0, "closed": 0, "wins": 0, "losses": 0, "breakeven": 0,
                    "realized_pnl_cents": 0, "closed_cost_cents": 0,
                })
                if row["closed"] is None:
                    group["open"] += 1
                    continue
                cost = row["stake"] + row["entry_fee"]
                pnl = row["proceeds"] - cost
                group["closed"] += 1
                group["closed_cost_cents"] += cost
                group["realized_pnl_cents"] += pnl
                group["wins" if pnl > 0 else "losses" if pnl < 0 else "breakeven"] += 1
        for dimension in groups.values():
            for group in dimension.values():
                cost = group["closed_cost_cents"]
                group["realized_return"] = group["realized_pnl_cents"] / cost if cost else None
        return groups

    def valuation(self, db, now, cash):
        positions = db.execute("""
            SELECT p.*, o.observed AS mark_observed, o.price AS mark_price
            FROM positions p LEFT JOIN observations o ON o.pair=p.pair AND o.observed=(
                SELECT MAX(x.observed) FROM observations x WHERE x.pair=p.pair
                AND x.observed BETWEEN MAX(p.opened, ?) AND ?)
            WHERE p.closed IS NULL
            """, (now - self.policy.quote_age, now)).fetchall()
        marked = value = cost = marked_cost = 0
        for position in positions:
            entry_cost = position["stake"] + position["entry_fee"]
            cost += entry_cost
            if position["mark_price"] is None:
                continue
            quote = Quote(position["mint"], position["pair"], position["symbol"],
                          Decimal(position["mark_price"]), position["mark_observed"])
            if not quote.fresh(now, self.policy.quote_age):
                continue
            settlement = self.settlement(position, quote.price / Decimal(position["entry"]))
            if settlement is not None:
                marked += 1
                value += settlement[0]
                marked_cost += entry_cost
        missing = len(positions) - marked
        return {
            "as_of": now, "open_cost_cents": cost, "marked_positions": marked,
            "unpriced_positions": missing, "marked_net_value_cents": value,
            "marked_unrealized_pnl_cents": value - marked_cost,
            "estimated_equity_cents": None if missing else cash + value,
        }


# Selective deduplicated notifications via an injected sender.
CATEGORIES = ("new_token", "high_risk", "market_movement", "paper_trade")


class Notifications:
    def __init__(self, store, recipients, send):
        self.store = store
        self.recipients = frozenset(recipients)
        self.send = send

    def settings(self):
        overrides = {row["key"]: bool(row["value"]) for row in
                     self.store.rows("SELECT * FROM settings WHERE key LIKE 'alert:%'")}
        return {category: overrides.get(f"alert:{category}", False) for category in CATEGORIES}

    def set(self, category, enabled):
        if category not in CATEGORIES:
            raise ValueError("Unknown notification type")
        with self.store.transaction(write=True) as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)",
                       (f"alert:{category}", int(enabled)))
        return self.settings()

    async def emit(self, category, key, message, now):
        if not self.settings().get(category):
            return
        key = f"{category}:{key}"
        for recipient in sorted(self.recipients):
            with self.store.transaction(write=True) as db:
                claimed = db.execute("INSERT OR IGNORE INTO notifications VALUES (?,?,?,?,?,'claimed')",
                                     (recipient, key, category, message[:3800], now)).rowcount
            if not claimed:
                continue
            try:
                await self.send(recipient, message[:3800])
                state = "sent"
            except Exception as error:
                LOG.warning("event=notification_failure type=%s", type(error).__name__)
                state = "failed"
            with self.store.transaction(write=True) as db:
                db.execute("UPDATE notifications SET state=? WHERE recipient=? AND key=?",
                           (state, recipient, key))


# Private-chat command adapter and replay of trusted local Telegram fixtures.
COMMAND = re.compile(r"/([a-z0-9_]{1,32})")


class Telegram:
    def __init__(self, commands):
        self.commands = commands

    async def handle(self, user, chat, chat_type, message_id, name, args, *, replay=False):
        if (chat_type != "private" or type(user) is not int or type(chat) is not int
                or chat != user or not 0 < user < 2**52):
            return None
        if user not in self.commands.allowed:
            return "Geen toegang."
        if (type(message_id) is not int or not 0 < message_id < 2**53
                or not isinstance(args, (tuple, list)) or len(args) > 32
                or any(not isinstance(arg, str) or len(arg) > 512 for arg in args)):
            return "Ongeldige invoer."
        # Use separate watermarks because fixtures can reuse real message IDs.
        namespace = "telegram_replay" if replay else "telegram"
        key = f"{namespace}:{chat}"
        with self.commands.store.transaction(write=True) as db:
            claimed = db.execute("""
                INSERT INTO settings VALUES (?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                WHERE excluded.value > settings.value
                """, (key, message_id)).rowcount
        if not claimed:
            return None
        if replay and name == "scheduler_start":
            return "Gebruik ingest voor een eindige offline scanreeks."
        return await self.commands.dispatch(user, True, name, args)

    async def replay(self, update):
        message = mapping(mapping(update).get("message"))
        user = mapping(message.get("from"))
        chat = mapping(message.get("chat"))
        text = message.get("text")
        if (user.get("is_bot") is not False or not isinstance(text, str)
                or not text.startswith("/") or len(text) > 4096):
            return None
        command, *args = text.split()
        match = COMMAND.fullmatch(command)
        if not match:
            return None
        return await self.handle(user.get("id"), chat.get("id"), chat.get("type"),
                                 message.get("message_id"), match[1], args, replay=True)

    def register(self, application, handler_factory, *, silent_unauthorized=False):
        for name in self.commands.registry:
            async def callback(update, context, command=name):
                user, chat, message = update.effective_user, update.effective_chat, update.message
                if message is None or chat is None or user is None or user.is_bot:
                    return
                if silent_unauthorized and user.id not in self.commands.allowed:
                    return
                response = await self.handle(user.id, chat.id, chat.type, message.message_id,
                                             command, context.args or ())
                if response is not None:
                    await message.reply_text(response, parse_mode=None, disable_web_page_preview=True)

            application.add_handler(handler_factory(name, callback))


# Wallet observations have unknown attribution and do not establish intent.
class Wallets:
    def __init__(self, store, client):
        self.store = store
        self.client = client

    def watch(self, wallet, label="Monitored"):
        if not address(wallet):
            raise ValueError("Invalid public wallet address")
        with self.store.transaction(write=True) as db:
            db.execute("INSERT OR REPLACE INTO watch VALUES (?,?)", (wallet, label[:100]))
        return {"address": wallet, "label": label[:100], "attribution": "UNKNOWN"}

    def unwatch(self, wallet):
        with self.store.transaction(write=True) as db:
            return bool(db.execute("DELETE FROM watch WHERE address=?", (wallet,)).rowcount)

    def list(self):
        return [dict(row, attribution="UNKNOWN") for row in
                self.store.rows("SELECT * FROM watch ORDER BY address")]

    async def activity(self, wallet):
        try:
            balance = await self.client.wallet_balance(wallet)
        except ProviderError:
            balance = None
        try:
            payload = await self.client.wallet_activity(wallet)
        except ProviderError:
            if balance is None:
                raise
            payload = None
        rows = {}
        for raw in (payload or [])[:30]:
            item = mapping(raw)
            signature = item.get("signature")
            if isinstance(signature, str) and 1 <= len(signature) <= 100 and "err" in item:
                timestamp = item.get("blockTime")
                rows[signature] = (timestamp if type(timestamp) is int and 0 <= timestamp < 2**63
                                   else None, int(item["err"] is None))
        with self.store.transaction(write=True) as db:
            db.executemany("INSERT OR IGNORE INTO activity VALUES (?,?,?,?)",
                           [(wallet, signature, *data) for signature, data in rows.items()])
        failed = sum(not success for _, success in rows.values())
        if balance is not None:
            balance["sol"] = format(Decimal(balance["lamports"]).scaleb(-9), "f")
        return {"address": wallet, "sample": len(rows) if payload is not None else None,
                "failed": failed if payload is not None else None,
                "failure_rate": failed / len(rows) if rows else None, "balance": balance,
                "attribution": "UNKNOWN", "confidence": "LIMITED_SAMPLE"}


# Discovery, risk assessment, persistence, and paper execution orchestration.
LOG = logging.getLogger(__name__)


class Scanner:
    def __init__(self, store, paper, client, *, clock=time.time, evidence=None,
                 trusted_sources=frozenset(), minimum_score=60, notifications=None,
                 public_quotes=False):
        self.store = store
        self.paper = paper
        self.client = client
        self.clock = clock
        self.evidence = evidence
        self.trusted_sources = frozenset(trusted_sources)
        self.minimum_score = minimum_score
        self.notifications = notifications
        self.public_quotes = public_quotes
        self.lock = asyncio.Lock()
        self.scans = 0
        self.failures = 0
        self.last_scan = None

    def paused(self):
        return bool(self.store.rows("SELECT 1 FROM settings WHERE key='paused' AND value=1"))

    def pause(self, paused):
        with self.store.transaction(write=True) as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES ('paused',?)", (int(paused),))

    async def candidates(self, mint, observed):
        try:
            payload = await self.client.pairs(mint)
        except ProviderError:
            self.failures += 1
            LOG.warning("event=pair_lookup_failed")
            return {}
        return self.parse_quotes(payload, mint, observed)

    def parse_quotes(self, payload, mint, observed):
        if self.public_quotes:
            observed = int(self.clock())
        result = {}
        conflicts = set()
        for item in payload:
            if self.public_quotes and not valid_record(item, observed):
                continue
            candidate = normalize(item, mint, observed)
            if candidate:
                key = candidate.quote.pair
                previous = result.get(key)
                if self.public_quotes and previous is not None and candidate != previous:
                    conflicts.add(key)
                if previous is None or pair_rank(candidate) > pair_rank(previous):
                    result[key] = candidate
        for key in conflicts:
            del result[key]
        return result

    async def position_candidate(self, position, observed, markets):
        pair = position["pair"]
        if pair in markets:
            return markets[pair]
        try:
            payload = await self.client.pair_by_id(pair)
        except ProviderError:
            return None
        return self.parse_quotes(payload, position["mint"], observed).get(pair)

    def record(self, profiles, observations, now):
        new = set()
        with self.store.transaction(write=True) as db:
            for mint in profiles:
                if db.execute("INSERT OR IGNORE INTO profiles VALUES (?,?,?)",
                              (mint, now, now)).rowcount:
                    new.add(mint)
                db.execute("UPDATE profiles SET last_seen=MAX(last_seen,?) WHERE mint=?", (now, mint))
            for candidate, risk in observations:
                quote = candidate.quote
                db.execute("INSERT OR IGNORE INTO pairs VALUES (?,?,?)",
                           (quote.pair, quote.mint, candidate.created))
                cursor = db.execute("""
                    UPDATE pairs SET created=COALESCE(created,?) WHERE pair=? AND mint=?""",
                    (candidate.created, quote.pair, quote.mint))
                if cursor.rowcount != 1:
                    raise ValueError("Pair identity changed")
                detail = asdict(candidate)
                detail.update(components=candidate.components(), assessment=asdict(risk),
                              discovery="latest_profile" if quote.mint in profiles else "position_refresh")
                db.execute("INSERT OR IGNORE INTO observations VALUES (?,?,?,?,?,?)",
                           (quote.pair, quote.observed, str(quote.price), candidate.score,
                            risk.classification, json.dumps(detail, default=str, sort_keys=True)))
        return new

    async def scan(self):
        async with self.lock:
            if self.paused():
                return {"paused": True}
            now = int(self.clock())
            try:
                payload = await self.client.profiles()
            except ProviderError:
                payload = []
                self.failures += 1
                LOG.warning("event=profile_lookup_failed")
            profiles = list(dict.fromkeys(
                item["tokenAddress"] for raw in payload if (item := mapping(raw))
                and item.get("chainId") == "solana" and address(item.get("tokenAddress"))
            ))[:30]
            positions = self.paper.open_positions()
            mints = list(dict.fromkeys(profiles + [row["mint"] for row in positions]))
            markets = dict(zip(mints, await asyncio.gather(
                *(self.candidates(mint, now) for mint in mints))))
            for position in positions:
                market = markets[position["mint"]]
                candidate = await self.position_candidate(position, now, market)
                if candidate:
                    market[candidate.quote.pair] = candidate
            observations = []
            assessments = {}
            for pairs in markets.values():
                for candidate in pairs.values():
                    evidence = await self.evidence(candidate) if self.evidence else None
                    risk = assess(candidate, int(self.clock()), evidence,
                                  trusted_sources=self.trusted_sources)
                    observations.append((candidate, risk))
                    assessments[candidate.quote.pair] = risk
            if self.paused():
                return {"paused": True}
            new = self.record(profiles, observations, now)
            events = []
            active_mints = {row["mint"] for row in positions}
            for position in positions:
                candidate = markets[position["mint"]].get(position["pair"])
                if candidate:
                    event = self.paper.close(position["id"], candidate.quote, int(self.clock()))
                    if event:
                        events.append(event)
            ranked = sorted((max(markets[mint].values(), key=pair_rank)
                             for mint in profiles if markets[mint]),
                            key=lambda candidate: (-candidate.score, candidate.quote.mint))
            accepted = []
            for candidate in ranked:
                quote = candidate.quote
                risk = assessments[quote.pair]
                if (candidate.score >= self.minimum_score and risk.allows_automatic_entry
                        and quote.fresh(int(self.clock()), self.paper.policy.quote_age)):
                    accepted.append(quote.mint)
                    if quote.mint not in active_mints:
                        event = self.paper.enter(quote, int(self.clock()), automatic=True,
                                                 assessment=risk)
                        if event:
                            events.append(event)
            self.scans += 1
            self.last_scan = now
            if self.notifications:
                await self.notify(ranked, new, assessments, events, now)
            LOG.info("event=scan_completed profiles=%d markets=%d events=%d",
                     len(profiles), len(observations), len(events))
            return {"profiles": len(profiles), "new_profiles": len(new),
                    "accepted": accepted, "rejected": len(ranked)-len(accepted),
                    "unpriced_profiles": len(profiles)-len(ranked), "events": events}

    async def notify(self, ranked, new, assessments, events, now):
        for candidate in ranked[:5]:
            quote = candidate.quote
            if quote.mint in new:
                await self.notifications.emit("new_token", quote.mint,
                    f"Nieuw profiel in de lokale dataset: {quote.symbol} {quote.mint}", now)
            if assessments[quote.pair].classification == "HIGH":
                await self.notifications.emit("high_risk", quote.mint,
                    f"Ongunstig veiligheidsbewijs: {quote.symbol}", now)
            if candidate.momentum is not None and abs(candidate.momentum) >= 50:
                await self.notifications.emit("market_movement", f"{quote.pair}:{now // 3600}",
                    f"{quote.symbol} 24u-beweging {candidate.momentum}%", now)
        for event in events:
            await self.notifications.emit("paper_trade", f"{event['id']}:{event['kind']}",
                                          json.dumps(event), now)

    def latest(self, mint=None, *, limit=30):
        return self.store.rows("""
            SELECT p.mint, o.* FROM observations o JOIN pairs p USING(pair)
            WHERE (? IS NULL OR p.mint=?) AND o.observed=(
                SELECT MAX(x.observed) FROM observations x WHERE x.pair=o.pair)
            ORDER BY o.observed DESC,o.score DESC,o.pair LIMIT ?""", (mint, mint, limit))

    def status(self):
        return {"paused": self.paused(), "scans": self.scans, "last_scan": self.last_scan,
                "provider_failures": self.failures, "api": dict(self.client.metrics)}


class Scheduler:
    def __init__(self, scanner, interval=60):
        self.scanner = scanner
        self.interval = interval
        self.task = None
        self.errors = 0

    async def run(self):
        while True:
            try:
                await self.scanner.scan()
            except Exception as error:
                self.errors += 1
                LOG.error("event=scheduler_failure type=%s", type(error).__name__)
            await asyncio.sleep(self.interval)

    def start(self):
        if self.task and not self.task.done():
            return False
        self.task = asyncio.create_task(self.run())
        return True

    async def stop(self):
        if self.task is None:
            return False
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)
        self.task = None
        return True

    def status(self):
        return dict(self.scanner.status(), running=bool(self.task and not self.task.done()),
                    scheduler_errors=self.errors)


# Time-split evaluation of score signals on the observed pair universe.
def replay(store):
    return store.rows("""
        SELECT p.mint,o.* FROM observations o JOIN pairs p USING(pair)
        ORDER BY observed,pair""")


def labeled(observations, horizon=3600, tolerance=900, target=Decimal("0.10")):
    if horizon <= 0 or tolerance < 0:
        raise ValueError("Invalid outcome window")
    pairs = defaultdict(list)
    for row in observations:
        pairs[row["pair"]].append(row)
    result = []
    for series in pairs.values():
        series.sort(key=lambda row: row["observed"])
        times = [row["observed"] for row in series]
        for row in series:
            start = row["observed"]
            index = bisect_left(times, start + horizon)
            outcome = None
            if index < len(series) and times[index] <= start + horizon + tolerance:
                change = Decimal(series[index]["price"]) / Decimal(row["price"]) - 1
                outcome = {"at": times[index], "positive": change >= target, "return": str(change)}
            result.append(dict(row, outcome=outcome))
    return sorted(result, key=lambda row: (row["observed"], row["pair"]))


def metrics(rows, threshold):
    known = [row for row in rows if row["outcome"] is not None]
    tp = fp = tn = fn = 0
    for row in known:
        signal = row["score"] >= threshold
        positive = row["outcome"]["positive"]
        tp += signal and positive
        fp += signal and not positive
        tn += not signal and not positive
        fn += not signal and positive
    return {"total": len(rows), "unknown": len(rows)-len(known), "tp": tp, "fp": fp,
            "tn": tn, "fn": fn, "precision": tp / (tp + fp) if tp + fp else None,
            "recall": tp / (tp + fn) if tp + fn else None}


def compare(rows, split, thresholds=(60, 80, 100)):
    # No training label can mature on or after the start of the test period.
    train = [row for row in rows if row["observed"] < split
             and row["outcome"] is not None and row["outcome"]["at"] < split]
    test = [row for row in rows if row["observed"] >= split]
    calibration = {}
    for bucket in range(0, 101, 20):
        group = [row for row in train if row["score"] // 20 * 20 == bucket]
        calibration[bucket] = {
            "count": len(group),
            "positive_rate": sum(row["outcome"]["positive"] for row in group) / len(group)
            if group else None,
        }
    return {"split": split, "training_calibration": calibration,
            "thresholds": {n: {"train": metrics(train, n), "test": metrics(test, n)}
                           for n in thresholds}}


def rolling(rows, as_of, window=86400, threshold=60):
    cutoff = as_of - window
    visible = []
    for row in rows:
        if cutoff <= row["observed"] <= as_of:
            outcome = row["outcome"]
            visible.append(row if outcome and outcome["at"] <= as_of else dict(row, outcome=None))
    return metrics(visible, threshold)


def false_signals(rows, threshold=60):
    return [{"mint": row["mint"], "pair": row["pair"], "observed": row["observed"],
             "score": row["score"], "outcome": row["outcome"]}
            for row in rows if row["score"] >= threshold and row["outcome"]
            and not row["outcome"]["positive"]]


def review(rows, split):
    """Evaluate false positives and missed signals and suggest a manual threshold trial."""
    report = compare(rows, split)
    baseline = report["thresholds"][60]

    def improves(current, reference):
        # Require examples from both classes and a Pareto improvement on errors.
        positives = reference["tp"] + reference["fn"]
        negatives = reference["tn"] + reference["fp"]
        return (min(positives, negatives) >= 5
                and current["fp"] <= reference["fp"] and current["fn"] <= reference["fn"]
                and current["fp"] + current["fn"] < reference["fp"] + reference["fn"])

    training_choices = [(threshold, scores) for threshold, scores in report["thresholds"].items()
                        if threshold != 60 and improves(scores["train"], baseline["train"])]
    proposals = []
    if training_choices:
        threshold, scores = min(training_choices, key=lambda item:
                                item[1]["train"]["fp"] + item[1]["train"]["fn"])
        if improves(scores["test"], baseline["test"]):
            proposals.append({
                "action": "review_score_threshold", "current": 60, "proposed": threshold,
                "training_false_positives_avoided": baseline["train"]["fp"] - scores["train"]["fp"],
                "test_false_positives_avoided": baseline["test"]["fp"] - scores["test"]["fp"],
            })
    report["manual_review_required"] = True
    report["proposals"] = proposals
    return report


# Chronological counterfactual paper portfolio comparisons on recorded markets.
def frame_quote(row):
    data = json.loads(row["detail"])
    quote = Quote(row["mint"], row["pair"], data["quote"]["symbol"],
                  Decimal(row["price"]), row["observed"])
    return quote, (Decimal(data["liquidity"] or "0"), Decimal(data["volume"] or "0"),
                   row["score"], quote.pair)


def simulate(rows, threshold, directory, policy):
    paper = Paper(Store(directory / f"threshold-{threshold}.sqlite"), policy)
    count = 0
    for observed, frame in groupby(rows, key=lambda row: row["observed"]):
        count += 1
        quotes = {}
        ranked = {}
        for row in frame:
            quote, rank = frame_quote(row)
            quotes[quote.pair] = quote
            previous = ranked.get(quote.mint)
            if previous is None or rank > previous[0]:
                ranked[quote.mint] = (rank, quote)
        positions = paper.open_positions()
        for position in positions:
            paper.close(position["id"], quotes.get(position["pair"]), observed)
        active = {position["mint"] for position in positions}
        # Compare market-score hypotheses only. These counterfactual entries are
        # isolated from the operational scanner and its evidence-based entry gate.
        for rank, quote in sorted(ranked.values(), key=lambda item: (-item[0][2], item[1].mint)):
            if rank[2] >= threshold and quote.mint not in active:
                paper.enter(quote, observed)
    return dict(paper.report(), frames=count)


def compare_portfolios(store, split, thresholds=(60, 80, 100), policy=None):
    """Simulate predefined thresholds on a fresh account per window and strategy."""
    rows = replay(store)
    train = [row for row in rows if row["observed"] < split]
    test = [row for row in rows if row["observed"] >= split]
    policy = policy or Policy()
    result = {}
    with TemporaryDirectory(prefix=".test-backtest-", dir=ROOT) as work:
        for name, window in (("train", train), ("test", test)):
            directory = Path(work) / name
            result[name] = {threshold: simulate(window, threshold, directory, policy)
                            for threshold in dict.fromkeys(thresholds)}
    return {"split": split, "market_score_only": True, "portfolios": result,
            "score_metrics": compare(labeled(rows), split, thresholds)}


# Explicit, foreground, read-only requests to the public DexScreener API.
HOST = "api.dexscreener.com"
ORIGIN = f"https://{HOST}"
MAX_BYTES = 1_048_576
MAX_ITEMS = 1000


def valid_record(item, observed):
    if not isinstance(item, dict):
        return False
    base = item.get("baseToken")
    if not isinstance(base, dict):
        return False
    for owner, fields in ((base, ("name", "symbol")), (item, ("dexId",))):
        if any(key in owner and not isinstance(owner[key], str) for key in fields):
            return False
    for key, field, minimum in (("liquidity", "usd", 0), ("volume", "h24", 0),
                                ("priceChange", "h24", -100)):
        if key in item:
            group = item[key]
            if not isinstance(group, dict):
                return False
            if field in group and number(group[field], minimum=minimum) is None:
                return False
    for key in ("marketCap", "fdv"):
        if key in item and item[key] is not None and number(item[key]) is None:
            return False
    created = item.get("pairCreatedAt")
    return created is None or type(created) is int and 0 < created < (observed + 1) * 1000


def allowed(url):
    return url == PROFILES or any(
        url.startswith(prefix) and address(url.removeprefix(prefix))
        for prefix in (PAIRS, PAIR_BY_ID)
    )


def unique_object(items):
    output = dict(items)
    if len(output) != len(items):
        raise ValueError("Duplicate JSON key")
    return output


def invalid_constant(_):
    raise ValueError("Nonstandard JSON constant")


class DexTransport:
    """One request at a time with at least one second between starts, including retries."""

    def __init__(self, *, clock=time.monotonic, sleep=asyncio.sleep):
        self.clock = clock
        self.sleep = sleep
        self.next_request = 0
        self.lock = asyncio.Lock()
        self.inflight = None

    async def __call__(self, method, url, body):
        if method != "GET" or body is not None or not allowed(url):
            raise ProviderError("DexScreener request outside allowlist")
        return await self.request(self._fetch, url)

    async def request(self, fetch, *args):
        async with self.lock:
            if self.inflight and not self.inflight.done():
                await asyncio.wait({self.inflight})
            delay = self.next_request - self.clock()
            if delay > 0:
                await self.sleep(delay)
            self.next_request = self.clock() + 1
            self.inflight = asyncio.create_task(self.fetch_off_loop(fetch, *args))
            # Retries and other callers share the slot even if the waiter times out.
            self.inflight.add_done_callback(self.finished)
            return await asyncio.shield(self.inflight)

    async def fetch_off_loop(self, fetch, *args):
        # A cancelled asyncio waiter cannot interrupt a blocking DNS or socket call.
        worker = ThreadPoolExecutor(max_workers=1)
        try:
            return await asyncio.get_running_loop().run_in_executor(worker, fetch, *args)
        finally:
            worker.shutdown(wait=False)

    def finished(self, task):
        if task.cancelled() or task.exception() is not None:
            return
        response = task.result()
        if response.status == 429:
            self.next_request = max(self.next_request,
                                    self.clock() + retry_delay(response.retry_after, 0))

    def _fetch(self, url):
        return self.fetch_json(HOST, "GET", url.removeprefix(ORIGIN))

    def fetch_json(self, host, method, path, body=None):
        connection = HTTPSConnection(host, timeout=5)
        deadline = self.clock() + 8
        try:
            headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
            if body is None:
                connection.request(method, path, headers=headers)
            else:
                headers["Content-Type"] = "application/json"
                connection.request(method, path, body=json.dumps(body), headers=headers)
            response = connection.getresponse()
            if self.clock() >= deadline:
                raise TimeoutError("DexScreener response deadline exceeded")
            if response.status != 200:
                return Response(response.status, None, response.getheader("Retry-After"))
            content_type = (response.getheader("Content-Type") or "").split(";", 1)[0]
            if content_type.strip().lower() != "application/json":
                raise ProviderError("Expected JSON from DexScreener")
            payload = bytearray()
            while True:
                if self.clock() >= deadline:
                    raise TimeoutError("DexScreener response deadline exceeded")
                chunk = response.read1(min(65536, MAX_BYTES + 1 - len(payload)))
                if self.clock() >= deadline:
                    raise TimeoutError("Public API response deadline exceeded")
                if not chunk:
                    break
                payload.extend(chunk)
                if len(payload) > MAX_BYTES:
                    raise ProviderError("DexScreener body exceeds size limit")
            data = json.loads(payload.decode("utf-8"), object_pairs_hook=unique_object,
                              parse_constant=invalid_constant)
            if any(isinstance(items, list) and len(items) > MAX_ITEMS
                   for items in (data, data.get("pairs") if isinstance(data, dict) else None)):
                raise ProviderError("Too many public API records")
            return Response(200, data)
        except HTTPException as error:
            raise OSError("DexScreener HTTP protocol failure") from error
        except (ValueError, RecursionError) as error:
            raise ProviderError("Invalid DexScreener JSON") from error
        finally:
            connection.close()


class MarketTransport(DexTransport):
    """The host also exposes two public Solana RPC reads through the shared limiter."""

    async def __call__(self, method, url, body):
        if method != "POST" or url != RPC or not isinstance(body, dict):
            return await super().__call__(method, url, body)
        rpc_method = body.get("method")
        params = body.get("params")
        options = {"commitment": "confirmed"}
        if rpc_method == "getSignaturesForAddress":
            options["limit"] = 30
        elif rpc_method != "getBalance":
            raise ProviderError("RPC method outside allowlist")
        if (not isinstance(params, list) or len(params) != 2 or not address(params[0])
                or body != {"jsonrpc": "2.0", "id": 1, "method": rpc_method,
                            "params": [params[0], options]}):
            raise ProviderError("Invalid public RPC request")
        return await self.request(self.fetch_json, "api.mainnet-beta.solana.com", "POST", "/", body)


async def market_snapshot(mint=None, pair=None, *, transport=None, clock=time.time):
    if ((mint is not None and not address(mint))
            or (pair is not None and (mint is None or not address(pair)))):
        raise ValueError("Expected a Solana mint and optional pair address")
    client = PublicData(transport if transport is not None else DexTransport(), concurrency=1)
    if mint is None:
        payload = await client.profiles()
    elif pair is None:
        payload = await client.pairs(mint)
    else:
        payload = await client.pair_by_id(pair)
    if len(payload) > MAX_ITEMS:
        raise ProviderError("Too many DexScreener records")

    observed = int(clock())
    result = {"source": "DexScreener", "observed": observed}
    if mint is None:
        result["profile_mints"] = list(dict.fromkeys(
            item["tokenAddress"] for item in payload if isinstance(item, dict)
            and item.get("chainId") == "solana" and address(item.get("tokenAddress"))
        ))
    else:
        candidates = {}
        for item in payload:
            if not valid_record(item, observed):
                continue
            candidate = normalize(item, mint, observed)
            if candidate is None or (pair is not None and candidate.quote.pair != pair):
                continue
            key = candidate.quote.pair
            if key in candidates and candidate != candidates[key]:
                raise ProviderError("Conflicting quotes for the same pair")
            candidates[key] = candidate
        result["markets"] = [
            dict(asdict(candidate), score=candidate.score,
                 assessment=asdict(assess(candidate, observed)))
            for candidate in sorted(candidates.values(), key=pair_rank, reverse=True)
        ]
    return result


# Command registry with a single authorization and throttling boundary.
LOG = logging.getLogger(__name__)


class Commands:
    def __init__(self, scanner, scheduler, wallets, notifications, allowed, *, clock=time.monotonic):
        self.scanner = scanner
        self.scheduler = scheduler
        self.wallets = wallets
        self.alerts = notifications
        self.store = scanner.store
        self.paper = scanner.paper
        self.allowed = frozenset(allowed)
        self.clock = clock
        self.calls = {}
        self.registry = {
            "start": lambda: "Solana Scanner Ultimate PAPER-ONLY. Gebruik /help.",
            "help": lambda: sorted(self.registry),
            "scan": scanner.scan,
            "top_tokens": self.top_tokens,
            "token": self.token,
            "wallet": wallets.activity,
            "wallet_watch": self.watch,
            "wallet_unwatch": wallets.unwatch,
            "wallet_list": wallets.list,
            "paper_status": self.paper.report,
            "paper_open": self.paper_open,
            "paper_close": self.paper_close,
            "paper_trades": self.paper.open_positions,
            "paper_report": self.paper.report,
            "advanced_paper_report": self.advanced_paper_report,
            "market_regime": self.regime,
            "false_signals": self.false_signals,
            "learning_status": self.learning_status,
            "learning_report": self.learning_report,
            "alert_history": self.alert_history,
            "alert_settings": self.alert_settings,
            "scheduler_start": scheduler.start,
            "scheduler_stop": scheduler.stop,
            "scheduler_status": scheduler.status,
            "dashboard": self.dashboard,
            "system_report": self.system_report,
            "database_status": self.store.status,
            "database_backup": self.store.backup,
            "database_tables": self.tables,
            "pause": lambda: self.pause(True),
            "resume": lambda: self.pause(False),
        }

    async def dispatch(self, user_id, private, name, args):
        if type(user_id) is not int or user_id not in self.allowed or not private:
            return "Geen toegang."
        now = self.clock()
        calls = self.calls.setdefault(user_id, deque())
        while calls and now - calls[0] >= 10:
            calls.popleft()
        if len(calls) >= 8:
            return "Te veel verzoeken. Probeer over enkele seconden opnieuw."
        calls.append(now)
        callback = self.registry.get(name)
        if callback is None:
            return "Onbekend commando. Gebruik /help."
        try:
            try:
                signature(callback).bind(*args)
            except TypeError:
                return f"Ongeldig aantal argumenten voor /{name}."
            result = callback(*args)
            if isawaitable(result):
                result = await result
            output = json.dumps(result, default=str, ensure_ascii=False, indent=2)
            if len(output) > 3800 and name in ("advanced_paper_report", "learning_report"):
                output = json.dumps(result, default=str, ensure_ascii=False, separators=(",", ":"))
            return output[:3800]
        except (ValueError, ProviderError):
            return f"Ongeldige invoer of gegevens niet beschikbaar voor /{name}."
        except Exception as error:
            LOG.error("event=command_failure command=%s type=%s", name, type(error).__name__)
            return "Commando mislukt."

    def register_telegram(self, application, handler_factory, *, silent_unauthorized=False):
        """Register python-telegram-bot-compatible callbacks without owning a transport."""
        Telegram(self).register(application, handler_factory,
                                silent_unauthorized=silent_unauthorized)

    def top_tokens(self):
        rows = self.store.rows("""
            WITH latest AS (
              SELECT p.mint,o.*,row_number() OVER (
                PARTITION BY p.mint ORDER BY o.observed DESC,o.score DESC,o.pair) AS rank
              FROM observations o JOIN pairs p USING(pair))
            SELECT mint,pair,observed,price,score,risk FROM latest WHERE rank=1
            ORDER BY score DESC,mint LIMIT 10""")
        return rows

    def token(self, mint):
        return [json.loads(row["detail"]) for row in self.scanner.latest(mint, limit=5)]

    def watch(self, wallet, *label):
        return self.wallets.watch(wallet, " ".join(label) or "Monitored")

    async def paper_open(self, mint):
        rows = self.scanner.latest(mint)
        if not rows:
            return {"opened": False, "reason": "missing_quote"}
        def rank(row):
            detail = json.loads(row["detail"])
            return (row["observed"], Decimal(detail["liquidity"] or "0"),
                    Decimal(detail["volume"] or "0"), row["score"], row["pair"])

        best = max(rows, key=rank)
        data = json.loads(best["detail"])["quote"]
        data["price"] = Decimal(data["price"])
        now = int(self.scanner.clock())
        event = self.paper.enter(Quote(**data), now)
        if event:
            await self.alerts.emit("paper_trade", f"{event['id']}:entry", json.dumps(event), now)
        return event or {"opened": False, "reason": "stale_quote_or_account_limit"}

    async def paper_close(self, trade_id):
        trade_id = int(trade_id)
        position = next((row for row in self.paper.open_positions() if row["id"] == trade_id), None)
        if position is None:
            return {"closed": False, "reason": "position_missing"}
        now = int(self.scanner.clock())
        markets = await self.scanner.candidates(position["mint"], now)
        candidate = await self.scanner.position_candidate(position, now, markets)
        event = self.paper.close(trade_id, candidate.quote if candidate else None,
                                 int(self.scanner.clock()), manual=True)
        if event:
            await self.alerts.emit("paper_trade", f"{event['id']}:exit", json.dumps(event), now)
        return event or {"closed": False, "reason": "fresh_pair_quote_required"}

    def advanced_paper_report(self):
        return self.paper.report(now=int(self.scanner.clock()))

    def learning_status(self):
        rows = labeled(replay(self.store))
        return rolling(rows, int(self.scanner.clock()))

    def learning_report(self, split=None):
        now = int(self.scanner.clock())
        rows = labeled([row for row in replay(self.store)
                                  if row["observed"] <= now])
        if split is None:
            split = rows[len(rows) // 2]["observed"] if rows else int(self.scanner.clock())
        return review(rows, int(split))

    def false_signals(self):
        return false_signals(labeled(replay(self.store)))[-20:]

    def regime(self):
        rows = [json.loads(row["detail"]) for row in self.scanner.latest(limit=100)]
        by_mint = {}
        for row in rows:
            by_mint.setdefault(row["quote"]["mint"], row)
        usable = [row for row in by_mint.values() if row["volume"] is not None
                  and row["momentum"] is not None]
        if len(usable) < 3:
            return {"regime": "UNKNOWN", "sample_size": len(usable)}
        volume = sum(Decimal(row["volume"]) for row in usable) / len(usable)
        positive = sum(Decimal(row["momentum"]) > 0 for row in usable) / len(usable)
        regime = "MIXED"
        if volume < 5000:
            regime = "LOW_ACTIVITY"
        elif volume >= 50000 and positive >= .60:
            regime = "ACTIVE_BULLISH"
        elif volume >= 50000 and positive <= .40:
            regime = "ACTIVE_BEARISH"
        return {"regime": regime, "sample_size": len(usable), "average_volume_usd": str(volume)}

    def alert_history(self, limit=20):
        return self.store.rows("SELECT * FROM notifications ORDER BY created DESC,key LIMIT ?",
                               (min(50, max(1, int(limit))),))

    def alert_settings(self, category=None, value=None):
        if category is None:
            return self.alerts.settings()
        if value not in ("on", "off", "aan", "uit"):
            raise ValueError("Expected on/off")
        return self.alerts.set(category, value in ("on", "aan"))

    def tables(self):
        return [row["name"] for row in
                self.store.rows("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]

    def pause(self, paused):
        self.scanner.pause(paused)
        return {"paused": paused}

    def dashboard(self):
        return {"paper": self.paper.report(), "scanner": self.scheduler.status(),
                "market": self.regime(), "top_tokens": self.top_tokens()[:5],
                "wallet_count": len(self.wallets.list())}

    def system_report(self):
        return {"database": self.store.status(), "tables": self.tables(),
                "scanner": self.scheduler.status()}


# Opt-in Telegram polling and public market scanning for the virtual ledger.
class HostError(RuntimeError):
    pass


def host_settings(environment):
    token = environment.get("ULTIMATE_TELEGRAM_TOKEN", "")
    ids = environment.get("ULTIMATE_TELEGRAM_USER_IDS", "")
    if not re.fullmatch(r"[0-9]{1,20}:[A-Za-z0-9_-]{20,100}", token):
        raise HostError("Configure ULTIMATE_TELEGRAM_TOKEN.")
    if not re.fullmatch(r"[0-9]{1,16}(?:,[0-9]{1,16}){0,31}", ids):
        raise HostError("Configure ULTIMATE_TELEGRAM_USER_IDS with comma-separated user IDs.")
    allowed = frozenset(map(int, ids.split(",")))
    if any(not 0 < value < 2**52 for value in allowed):
        raise HostError("Telegram user ID outside supported range.")
    return token, allowed


def telegram_host(db, auto_scan, interval, *, environment=None):
    token, allowed = host_settings(os.environ if environment is None else environment)
    if db is None or not 15 <= interval <= 86400:
        raise HostError("Use an explicit --db and a scan interval of 15–86400 seconds.")
    try:
        from telegram.error import Conflict
        from telegram.ext import Application, CommandHandler
        from telegram.request import HTTPXRequest
    except ImportError:
        raise HostError("Install the optional dependencies from requirements-telegram.txt.") from None

    # Telegram request URLs contain credentials. Suppress library logs, including
    # errors and their tracebacks, throughout the polling lifecycle.
    previous_logging = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        with asyncio.Runner() as runner:
            runner.get_loop()
            store = Store(db)
            client = PublicData(MarketTransport(), concurrency=1)

            async def send(recipient, message):
                await application.bot.send_message(chat_id=recipient, text=message, parse_mode=None,
                                                   disable_web_page_preview=True)

            alerts = Notifications(store, allowed, send)
            scanner = Scanner(store, Paper(store), client, notifications=alerts, public_quotes=True)
            scheduler = Scheduler(scanner, interval)
            commands = Commands(scanner, scheduler, Wallets(store, client), alerts, allowed)

            async def start(_):
                if auto_scan:
                    scheduler.start()

            async def stop(_):
                await scheduler.stop()

            async def error(_, context):
                if isinstance(context.error, Conflict):
                    application.stop_running()

            def request():
                return HTTPXRequest(connect_timeout=5, read_timeout=5, write_timeout=5,
                                    pool_timeout=5, httpx_kwargs={"trust_env": False})

            application = (Application.builder().token(token).request(request())
                           .get_updates_request(request()).concurrent_updates(False)
                           .post_init(start).post_stop(stop).post_shutdown(stop).build())
            commands.register_telegram(application, CommandHandler, silent_unauthorized=True)
            application.add_error_handler(error)
            try:
                application.run_polling(allowed_updates=["message"], drop_pending_updates=True,
                                        bootstrap_retries=0, timeout=10, close_loop=False)
            finally:
                runner.run(scheduler.stop())
    except Exception as error:
        raise HostError(f"Telegram host stopped ({type(error).__name__}).") from None
    finally:
        logging.disable(previous_logging)


# Paper research CLI with an explicit read-only DexScreener lookup.
async def run(args):
    if args.action == "live-market":
        return await market_snapshot(args.mint, args.pair)
    if args.action == "recover":
        return Store.recover(args.backup, args.destination).status()
    store = Store(args.db)
    if args.action == "status":
        return {"database": store.status(), "paper": Paper(store).report()}
    if args.action == "paper-analysis":
        return Paper(store).segments()
    if args.action == "backup":
        return str(store.backup())
    if args.action == "evaluate":
        return review(labeled(replay(store)), args.split)
    if args.action == "backtest":
        return compare_portfolios(store, args.split)
    if args.action == "replay":
        return replay(store)
    frames = json.loads(owned(args.capture).read_text(encoding="utf-8"))
    if not isinstance(frames, list) or not frames:
        raise ValueError("Capture must contain a nonempty array of frames")
    if args.action in ("command", "telegram-replay") and len(frames) != 1:
        raise ValueError("Command requires exactly one frame")
    current = None
    client = PublicData(None)
    paper = Paper(store)

    async def local_notice(recipient, message):
        logging.getLogger(__name__).info("event=local_notification")

    user_id = args.user_id if args.action == "telegram-replay" else 1
    if not 0 < user_id < 2**52:
        raise ValueError("Invalid Telegram user ID")
    alerts = Notifications(store, {user_id}, local_notice)
    scanner = Scanner(store, paper, client, clock=lambda: current, notifications=alerts)
    scheduler = Scheduler(scanner)
    commands = Commands(scanner, scheduler, Wallets(store, client), alerts, {user_id})
    results = []
    try:
        for frame in frames:
            timestamp = frame["observed"]
            if type(timestamp) is not int or not 0 < timestamp < 2**53:
                raise ValueError("Invalid capture timestamp")
            if current is not None and timestamp <= current:
                raise ValueError("Capture timestamps must strictly increase")
            current = timestamp
            client.transport = RecordedTransport(frame)
            if args.action == "telegram-replay":
                return await Telegram(commands).replay(frame["telegram_update"])
            if args.action == "command":
                if args.name.removeprefix("/") == "scheduler_start":
                    raise ValueError("Use a finite capture with ingest")
                return await commands.dispatch(1, True, args.name.removeprefix("/"), args.arguments)
            results.append(await scanner.scan())
        return results
    finally:
        await scheduler.stop()


def main():
    parser = argparse.ArgumentParser(description="Paper research CLI with opt-in Telegram hosting.")
    parser.add_argument("--db", help="SQLite path inside this build folder")
    actions = parser.add_subparsers(dest="action", required=True)
    for action in ("status", "backup", "replay", "paper-analysis"):
        actions.add_parser(action)
    live = actions.add_parser("live-market")
    live.add_argument("mint", nargs="?")
    live.add_argument("--pair")
    actions.add_parser("ingest").add_argument("capture")
    for action in ("evaluate", "backtest"):
        actions.add_parser(action).add_argument("split", type=int)
    recovery = actions.add_parser("recover")
    recovery.add_argument("backup")
    recovery.add_argument("destination")
    telegram = actions.add_parser("telegram-replay")
    telegram.add_argument("capture")
    telegram.add_argument("--user-id", required=True, type=int)
    host = actions.add_parser("telegram-host", help="Start the PAPER-ONLY Telegram host")
    host.add_argument("--auto-scan", action="store_true", help="Start periodic public market scans")
    host.add_argument("--interval", type=int, default=120, help="Seconds between scan rounds")
    command = actions.add_parser("command")
    command.add_argument("capture")
    command.add_argument("name")
    command.add_argument("arguments", nargs="*")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s level=%(levelname)s %(message)s")
    args = parser.parse_args()
    try:
        if args.action == "telegram-host":
            telegram_host(args.db, args.auto_scan, args.interval)
            return
        result = asyncio.run(run(args))
    except HostError as error:
        parser.exit(2, f"{error}\n")
    except ProviderError as error:
        parser.exit(2, f"Market provider failed ({error}).\n")
    except (ValueError, OSError, KeyError, TypeError) as error:
        parser.exit(2, f"Operation failed ({type(error).__name__}).\n")
    print(result if isinstance(result, str) else json.dumps(result, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
