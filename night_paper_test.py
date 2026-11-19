"""Nightly read-only Solana launch scanner and virtual trading experiment.

Independent of production bot.py and scanner_data.db.
No private keys, wallet signing, swaps, or real money.
Run: python night_paper_test.py
Requires: aiohttp, python-dotenv, python-telegram-bot
Set TELEGRAM_BOT_TOKEN and ALLOWED_USER_IDS in your existing environment.
DO NOT run alongside another polling bot using the same Telegram token.
"""
import asyncio
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("night_paper_test")
DB = Path("night_paper_test.db")
INTERVAL = 90
START_EUR = 20.0
DEFAULT_EUR = 2.0
MAX_EUR = 4.0
MAX_POSITIONS = 3
FEE_RATE = 0.01
SLIPPAGE_RATE = 0.005
PROFILES = "https://api.dexscreener.com/token-profiles/latest/v1"
PAIRS = "https://api.dexscreener.com/token-pairs/v1/solana/"
paused = False
scan_lock = asyncio.Lock()
latest = []


def connect():
    con = sqlite3.connect(DB, timeout=20)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con


def initialize():
    with connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS account(id INTEGER PRIMARY KEY CHECK(id=1),
            cash_eur REAL NOT NULL);
        INSERT OR IGNORE INTO account(id,cash_eur) VALUES(1,20);
        CREATE TABLE IF NOT EXISTS observations(
            address TEXT PRIMARY KEY, symbol TEXT NOT NULL,
            price_usd REAL NOT NULL, liquidity REAL NOT NULL,
            volume REAL NOT NULL, updated_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS trades(
            id INTEGER PRIMARY KEY AUTOINCREMENT, address TEXT NOT NULL,
            symbol TEXT NOT NULL, status TEXT NOT NULL,
            stake_eur REAL NOT NULL, entry_price REAL NOT NULL,
            quantity REAL NOT NULL, opened_at INTEGER NOT NULL,
            exit_price REAL, proceeds_eur REAL, closed_at INTEGER,
            reason TEXT);
        CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
        """)


def numeric(v):
    try:
        x = float(v)
        return x if 0 < x < float("inf") else 0.0
    except (TypeError, ValueError, OverflowError):
        return 0.0


def authorized(update):
    ids = {int(x.strip()) for x in os.getenv("ALLOWED_USER_IDS", "").split(",")
           if x.strip().isdigit()}
    return bool(update.effective_user and update.effective_user.id in ids)


async def reply(update, message):
    if authorized(update) and update.effective_message:
        await update.effective_message.reply_text(message[:3900])


async def fetch(session, url):
    async with session.get(url) as response:
        response.raise_for_status()
        return await response.json()


async def discover(session):
    profiles = await fetch(session, PROFILES)
    if not isinstance(profiles, list):
        return []
    addresses = list(dict.fromkeys(
        p.get("tokenAddress") for p in profiles
        if isinstance(p, dict) and p.get("chainId") == "solana"
        and isinstance(p.get("tokenAddress"), str)
        and 32 <= len(p["tokenAddress"]) <= 44
    ))[:20]
    found = []
    for address in addresses:
        try:
            pairs = await fetch(session, PAIRS + address)
            if not isinstance(pairs, list):
                continue
            candidates = []
            for pair in pairs:
                if not isinstance(pair, dict) or pair.get("chainId") != "solana":
                    continue
                base = pair.get("baseToken") or {}
                if base.get("address") != address:
                    continue
                price = numeric(pair.get("priceUsd"))
                liquidity = numeric((pair.get("liquidity") or {}).get("usd"))
                volume = numeric((pair.get("volume") or {}).get("h24"))
                if price and liquidity and volume:
                    candidates.append(dict(address=address,
                        symbol=str(base.get("symbol") or "?")[:20],
                        price=price, liquidity=liquidity, volume=volume))
            if candidates:
                found.append(max(candidates, key=lambda x: x["liquidity"]))
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            log.warning("Pair lookup failed for %s", address)
    return found


def balance_report():
    with connect() as db:
        cash = db.execute("SELECT cash_eur FROM account WHERE id=1").fetchone()[0]
        open_trades = db.execute(
            "SELECT COUNT(*) FROM trades WHERE status='OPEN'").fetchone()[0]
        realized = db.execute(
            "SELECT COALESCE(SUM(proceeds_eur-stake_eur),0) FROM trades WHERE status='CLOSED'"
        ).fetchone()[0]
        return cash, open_trades, realized


def enter_virtual(token, stake=DEFAULT_EUR):
    if not (0 < stake <= MAX_EUR):
        return False
    if token["liquidity"] < 10000 or token["volume"] < 3000:
        return False
    if token["price"] <= 0:
        return False
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        cash = db.execute("SELECT cash_eur FROM account WHERE id=1").fetchone()[0]
        if cash < stake:
            return False
        if db.execute("SELECT COUNT(*) FROM trades WHERE status='OPEN'").fetchone()[0] >= MAX_POSITIONS:
            return False
        if db.execute("SELECT 1 FROM trades WHERE address=? LIMIT 1",
                      (token["address"],)).fetchone():
            return False
        execution_price = token["price"] * (1 + SLIPPAGE_RATE)
        quantity = stake * (1 - FEE_RATE) / execution_price
        db.execute("UPDATE account SET cash_eur=cash_eur-? WHERE id=1", (stake,))
        db.execute("""INSERT INTO trades(address,symbol,status,stake_eur,entry_price,
                      quantity,opened_at) VALUES(?,?,'OPEN',?,?,?,?)""",
                   (token["address"],token["symbol"],stake,execution_price,
                    quantity,int(time.time())))
    return True


def update_virtual_positions(prices):
    closed = []
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        rows = db.execute("SELECT * FROM trades WHERE status='OPEN'").fetchall()
        for trade in rows:
            token = prices.get(trade["address"])
            if not token or not token["price"]:
                continue  # Missing data is NOT a zero-price exit.
            current = token["price"]
            gross_ratio = current / trade["entry_price"]
            age = time.time() - trade["opened_at"]
            reason = ("stop" if gross_ratio <= 0.85 else
                      "target" if gross_ratio >= 1.25 else
                      "time" if age >= 6*3600 else None)
            if reason is None:
                continue
            execution = current * (1-SLIPPAGE_RATE)
            proceeds = trade["quantity"] * execution * (1-FEE_RATE)
            db.execute("""UPDATE trades SET status='CLOSED',exit_price=?,
                       proceeds_eur=?,closed_at=?,reason=? WHERE id=?""",
                       (execution,proceeds,int(time.time()),reason,trade["id"]))
            db.execute("UPDATE account SET cash_eur=cash_eur+? WHERE id=1",
                       (proceeds,))
            closed.append((trade["symbol"], proceeds-trade["stake_eur"],reason))
    return closed


async def scan_once():
    global latest
    async with scan_lock:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            tokens = await discover(session)
            latest = tokens
            with connect() as db:
                for t in tokens:
                    db.execute("""INSERT INTO observations
                      (address,symbol,price_usd,liquidity,volume,updated_at)
                      VALUES(?,?,?,?,?,?)
                      ON CONFLICT(address) DO UPDATE SET
                      symbol=excluded.symbol,price_usd=excluded.price_usd,
                      liquidity=excluded.liquidity,volume=excluded.volume,
                      updated_at=excluded.updated_at""",
                      (t["address"],t["symbol"],t["price"],t["liquidity"],
                       t["volume"],int(time.time())))
            closed = update_virtual_positions({t["address"]:t for t in tokens})
            opened = []
            for t in sorted(tokens, key=lambda x:x["liquidity"],reverse=True):
                if enter_virtual(t):
                    opened.append(t)
            return tokens, opened, closed


async def notify(app, message):
    ids = [int(x.strip()) for x in os.getenv("ALLOWED_USER_IDS", "").split(",")
           if x.strip().isdigit()]
    for uid in ids:
        try:
            await app.bot.send_message(uid, message[:3900])
        except Exception:
            log.exception("Telegram notification failed")


async def background(app):
    global paused
    while True:
        try:
            if not paused:
                tokens, opened, closed = await scan_once()
                for t in opened:
                    await notify(app, f"🧪 VIRTUELE ENTRY {t['symbol']} (€2)\n"
                        f"Token: {t['address']}\nLiquiditeit: ${t['liquidity']:,.0f}\n"
                        "Echte marktdata, GEEN echte aankoop. Risico niet geverifieerd.")
                for symbol, pnl, reason in closed:
                    await notify(app, f"🧪 VIRTUELE EXIT {symbol}: €{pnl:+.2f} ({reason})")
                log.info("Scanned %d Solana profiles",len(tokens))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Night scan failed; retry next cycle")
        await asyncio.sleep(INTERVAL)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await reply(update, "Nacht-testscanner: echte Solana marktdata, alleen virtuele trades. /status /scan /best /paper_report /pause /resume")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cash, count, realized = balance_report()
    await reply(update, f"Scanner: {'PAUZE' if paused else 'ACTIEF'}\n"
        f"Cash: €{cash:.2f}; open posities: {count}; gerealiseerd: €{realized:+.2f}\n"
        "Waarde van open posities is NIET in dit saldo verwerkt.")


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global paused
    if authorized(update):
        paused = True
        await reply(update,"Scannen gepauzeerd; Telegram blijft actief.")


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global paused
    if authorized(update):
        paused = False
        await reply(update,"Automatisch scannen hervat.")


async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    try:
        tokens, opened, closed = await scan_once()
        await reply(update, f"Scan: {len(tokens)} tokens; {len(opened)} virtuele entries; {len(closed)} exits.")
    except Exception:
        log.exception("Manual scan failed")
        await reply(update,"Scan mislukt. Bekijk de logs.")


async def cmd_best(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    if not latest:
        await reply(update,"Nog geen resultaten; gebruik /scan.")
        return
    lines = [f"{t['symbol']}: ${t['liquidity']:,.0f} liquiditeit | {t['address']}"
             for t in latest[:8]]
    await reply(update,"Nieuwe Solana tokens (NIET geverifieerd):\n" + "\n".join(lines))


async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    cash, count, realized = balance_report()
    with connect() as db:
        trades = db.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        wins = db.execute("""SELECT COUNT(*) FROM trades
                             WHERE status='CLOSED' AND proceeds_eur>stake_eur""").fetchone()[0]
    await reply(update, f"Virtueel startkapitaal: €20.00\nCash: €{cash:.2f}\n"
        f"Open: {count}\nTotaal entries: {trades}\nGesloten winnaars: {wins}\n"
        f"Gerealiseerd resultaat: €{realized:+.2f}\n"
        "Open posities niet gewaardeerd; dit is geen totaalrendement.")


async def on_startup(app):
    app.bot_data["scan_task"] = asyncio.create_task(background(app))


async def on_shutdown(app):
    task = app.bot_data.get("scan_task")
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN","").strip()
    ids = os.getenv("ALLOWED_USER_IDS","").strip()
    if not token or not ids:
        raise RuntimeError("TELEGRAM_BOT_TOKEN en ALLOWED_USER_IDS zijn vereist")
    initialize()
    app = (Application.builder().token(token)
           .post_init(on_startup).post_shutdown(on_shutdown).build())
    for name, handler in (
        ("start",cmd_start),("help",cmd_start),("status",cmd_status),
        ("pause",cmd_pause),("resume",cmd_resume),("scan",cmd_scan),
        ("best",cmd_best),("paper_report",cmd_report),
        ("paper_status",cmd_status)):
        app.add_handler(CommandHandler(name,handler))
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
