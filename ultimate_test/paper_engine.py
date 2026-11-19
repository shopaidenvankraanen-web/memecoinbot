"""Persistent virtual-only trading engine with conservative fee/slippage accounting.

This module NEVER places transactions. Dedicated database prevents touching
the original scanner_data.db. All prices are externally sourced observations.
"""
from __future__ import annotations
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class PaperConfig:
    starting_cash_eur: float = 20.0
    default_stake_eur: float = 2.0
    max_stake_eur: float = 4.0
    max_positions: int = 3
    fee_rate: float = 0.01
    slippage_rate: float = 0.005
    stop_loss: float = 0.15
    take_profit: float = 0.25
    max_hold_seconds: int = 6 * 3600

    def __post_init__(self):
        if not (0 < self.default_stake_eur <= self.max_stake_eur <= self.starting_cash_eur):
            raise ValueError("Invalid paper stake configuration")
        if self.max_positions < 1 or not all(0 <= x < 1 for x in (
            self.fee_rate, self.slippage_rate, self.stop_loss, self.take_profit)):
            raise ValueError("Invalid paper simulation rates")


@dataclass(frozen=True)
class PaperEvent:
    trade_id: int
    kind: str
    symbol: str
    address: str
    cash_delta_eur: float
    reason: str


class PaperEngine:
    def __init__(self, path: str | Path = "ultimate_test_paper.db",
                 config: PaperConfig | None = None,
                 clock: Callable[[], float] = time.time):
        self.path = str(path)
        self.config = config or PaperConfig()
        self.clock = clock
        self.initialize()

    def connect(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def initialize(self):
        with self.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS paper_account (
                id INTEGER PRIMARY KEY CHECK(id=1), cash_eur REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS paper_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                address TEXT NOT NULL, symbol TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('OPEN','CLOSED')),
                stake_eur REAL NOT NULL, quantity REAL NOT NULL,
                entry_market_price REAL NOT NULL, entry_execution_price REAL NOT NULL,
                opened_at INTEGER NOT NULL, exit_market_price REAL,
                exit_execution_price REAL, proceeds_eur REAL,
                closed_at INTEGER, exit_reason TEXT);
            CREATE INDEX IF NOT EXISTS paper_trades_status ON paper_trades(status);
            CREATE INDEX IF NOT EXISTS paper_trades_address ON paper_trades(address);
            """)
            db.execute("INSERT OR IGNORE INTO paper_account VALUES(1,?)",
                       (self.config.starting_cash_eur,))

    def cash(self) -> float:
        with self.connect() as db:
            return float(db.execute("SELECT cash_eur FROM paper_account WHERE id=1").fetchone()[0])

    def open_trades(self):
        with self.connect() as db:
            return [dict(x) for x in db.execute(
                "SELECT * FROM paper_trades WHERE status='OPEN' ORDER BY id")]

    def enter(self, address: str, symbol: str, market_price: float,
              stake_eur: float | None = None) -> PaperEvent | None:
        stake = self.config.default_stake_eur if stake_eur is None else stake_eur
        if not address or not symbol or not (0 < market_price < float("inf")):
            return None
        if not (0 < stake <= self.config.max_stake_eur):
            return None
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            cash = float(db.execute("SELECT cash_eur FROM paper_account WHERE id=1").fetchone()[0])
            if cash + 1e-9 < stake:
                return None
            open_rows = db.execute("SELECT address FROM paper_trades WHERE status='OPEN'").fetchall()
            if len(open_rows) >= self.config.max_positions or any(x[0] == address for x in open_rows):
                return None
            execution = market_price * (1 + self.config.slippage_rate)
            quantity = stake * (1 - self.config.fee_rate) / execution
            cursor = db.execute("""INSERT INTO paper_trades
                (address,symbol,status,stake_eur,quantity,entry_market_price,
                 entry_execution_price,opened_at)
                VALUES(?,?,'OPEN',?,?,?, ?,?)""",
                (address,symbol,stake,quantity,market_price,execution,int(self.clock())))
            db.execute("UPDATE paper_account SET cash_eur=cash_eur-? WHERE id=1",(stake,))
            return PaperEvent(cursor.lastrowid,"ENTRY",symbol,address,-stake,"paper_only")

    def mark(self, prices: dict[str, float]) -> list[PaperEvent]:
        events = []
        now = int(self.clock())
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT * FROM paper_trades WHERE status='OPEN'").fetchall()
            for row in rows:
                price = prices.get(row["address"])
                if price is None or not (0 < price < float("inf")):
                    continue  # Missing quotes are not treated as a loss or a successful exit.
                change = price / row["entry_market_price"] - 1
                age = max(0, now - row["opened_at"])
                reason = ("stop_loss" if change <= -self.config.stop_loss else
                          "take_profit" if change >= self.config.take_profit else
                          "time_limit" if age >= self.config.max_hold_seconds else None)
                if reason is None:
                    continue
                execution = price * (1 - self.config.slippage_rate)
                proceeds = row["quantity"] * execution * (1 - self.config.fee_rate)
                db.execute("""UPDATE paper_trades SET status='CLOSED',
                    exit_market_price=?,exit_execution_price=?,proceeds_eur=?,
                    closed_at=?,exit_reason=? WHERE id=?""",
                    (price,execution,proceeds,now,reason,row["id"]))
                db.execute("UPDATE paper_account SET cash_eur=cash_eur+? WHERE id=1",
                           (proceeds,))
                events.append(PaperEvent(row["id"],"EXIT",row["symbol"],
                                         row["address"],proceeds,reason))
        return events

    def report(self) -> dict:
        with self.connect() as db:
            cash = float(db.execute("SELECT cash_eur FROM paper_account WHERE id=1").fetchone()[0])
            rows = db.execute("SELECT * FROM paper_trades").fetchall()
        closed = [r for r in rows if r["status"] == "CLOSED"]
        opened = [r for r in rows if r["status"] == "OPEN"]
        pnl = sum(r["proceeds_eur"] - r["stake_eur"] for r in closed)
        wins = sum(r["proceeds_eur"] > r["stake_eur"] for r in closed)
        return {"cash_eur":cash, "open_positions":len(opened),
                "closed_positions":len(closed), "realized_pnl_eur":pnl,
                "closed_win_rate":wins/len(closed) if closed else None,
                "reserved_stake_eur":sum(r["stake_eur"] for r in opened),
                "unrealized_pnl_eur":None,
                "note":"Open positions are not marked to market in this report."}
