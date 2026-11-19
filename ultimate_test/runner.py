"""Safer integrated research + paper-only scanner.

Run from repository root: python -m ultimate_test.runner
This is an experimental addition, NOT a replacement for the original test bot.
The paper account lives in ultimate_test_v3.db; no real orders are possible.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any

import aiohttp
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from .paper_engine import PaperEngine
from .risk_engine import evaluate_token, number

log = logging.getLogger("ultimate_test.runner")
PROFILE_URL = "https://api.dexscreener.com/token-profiles/latest/v1"
PAIRS_URL = "https://api.dexscreener.com/token-pairs/v1/solana/"
BASE58 = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
INTERVAL_SECONDS = 120
MAX_DISCOVERED = 16
MAX_QUOTE_AGE_SECONDS = 300


@dataclass
class Candidate:
    address: str
    symbol: str
    pair: dict[str, Any]
    score: int
    risk: str
    confidence: str
    missing: tuple[str, ...]
    eligible: bool


class Scanner:
    def __init__(self):
        self.paper = PaperEngine("ultimate_test_v3.db")
        self.paused = False
        self.lock = asyncio.Lock()
        self.latest: list[Candidate] = []
        self.last_scan_at: float | None = None
        self.last_error: str | None = None
        self.task: asyncio.Task | None = None
        self.known: set[str] = set()
        self.session: aiohttp.ClientSession | None = None

    async def start(self, app: Application):
        timeout = aiohttp.ClientTimeout(total=18, connect=8)
        self.session = aiohttp.ClientSession(timeout=timeout,
            headers={"User-Agent": "SolanaUltimatePaperResearch/0.3"})
        self.task = asyncio.create_task(self.loop(app), name="solana-paper-scan")

    async def close(self):
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        if self.session is not None:
            await self.session.close()

    async def json(self, url: str) -> Any:
        if self.session is None:
            raise RuntimeError("HTTP session not initialized")
        for attempt in range(3):
            try:
                async with self.session.get(url) as response:
                    if response.status == 429 or response.status >= 500:
                        raise aiohttp.ClientResponseError(
                            response.request_info, response.history,
                            status=response.status, message="rate limit/server error")
                    response.raise_for_status()
                    return await response.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError):
                if attempt == 2:
                    raise
                await asyncio.sleep(1.5 * (2 ** attempt))
        raise RuntimeError("Unreachable")

    async def fetch_pairs(self, address: str) -> list[dict]:
        if not BASE58.fullmatch(address):
            return []
        payload = await self.json(PAIRS_URL + address)
        if not isinstance(payload, list):
            return []
        return [p for p in payload if isinstance(p, dict)
                and p.get("chainId") == "solana"
                and (p.get("baseToken") or {}).get("address") == address]

    async def discover(self) -> list[Candidate]:
        profiles = await self.json(PROFILE_URL)
        if not isinstance(profiles, list):
            raise ValueError("Unexpected profile response")
        addresses = list(dict.fromkeys(
            x.get("tokenAddress") for x in profiles if isinstance(x, dict)
            and x.get("chainId") == "solana"
            and isinstance(x.get("tokenAddress"), str)
            and BASE58.fullmatch(x["tokenAddress"])
        ))[:MAX_DISCOVERED]
        result: list[Candidate] = []
        for address in addresses:
            try:
                pairs = await self.fetch_pairs(address)
                if not pairs:
                    continue
                pair = max(pairs, key=lambda p: number((p.get("liquidity") or {}).get("usd")))
                base = pair.get("baseToken") or {}
                assessment = evaluate_token(pair)
                result.append(Candidate(
                    address=address, symbol=str(base.get("symbol") or "?")[:18],
                    pair=pair, score=assessment.score,
                    risk=assessment.classification, confidence=assessment.confidence,
                    missing=assessment.missing_evidence,
                    eligible=assessment.eligible_for_paper))
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError) as exc:
                log.warning("Pair lookup failed for token %s: %s", address, type(exc).__name__)
            await asyncio.sleep(0.2)
        return result

    async def quotes_for_open_positions(self) -> dict[str, float]:
        # Critical: monitor open positions independently of the latest-profile feed.
        quotes: dict[str, float] = {}
        addresses = list(dict.fromkeys(t["address"] for t in self.paper.open_trades()))
        for address in addresses:
            try:
                pairs = await self.fetch_pairs(address)
                valid = [p for p in pairs if number(p.get("priceUsd")) > 0
                         and number((p.get("liquidity") or {}).get("usd")) > 0]
                if valid:
                    best = max(valid, key=lambda p: number((p.get("liquidity") or {}).get("usd")))
                    quotes[address] = number(best["priceUsd"])
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError) as exc:
                log.warning("Cannot refresh open position %s: %s", address, type(exc).__name__)
            await asyncio.sleep(0.2)
        return quotes

    async def scan(self) -> tuple[list[Candidate], list, list]:
        async with self.lock:
            # A failed discovery does not cause liquidation or pretend prices are zero.
            discovered = await self.discover()
            prices = await self.quotes_for_open_positions()
            closed = self.paper.mark(prices)
            self.latest = sorted(discovered, key=lambda x: x.score, reverse=True)
            self.last_scan_at = time.time()
            self.last_error = None
            # Security/holder/sellability not independently verified.
            # Do not automatically enter even simulated trades on unknown risk.
            # Entries can be enabled in a later reviewed version with verified checks.
            return self.latest, [], closed

    async def loop(self, app: Application):
        while True:
            try:
                if not self.paused:
                    discovered, opened, closed = await self.scan()
                    for event in closed:
                        await notify(app, f"🧪 PAPER EXIT {event.symbol}: "
                                     f"€{event.cash_delta_eur:.2f} proceeds ({event.reason})")
                    new = [c for c in discovered if c.address not in self.known]
                    self.known.update(c.address for c in discovered)
                    if new:
                        selected = new[:3]
                        msg = "🔎 Nieuwe profielwaarnemingen (geen koopadvies):\n" + "\n".join(
                            f"{c.symbol}: risk={c.risk}, confidence={c.confidence} "
                            f"score={c.score}; {c.address}" for c in selected)
                        await notify(app, msg)
                    log.info("Scan complete: %d profile candidates", len(discovered))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = type(exc).__name__
                log.exception("Background scanner error")
            await asyncio.sleep(INTERVAL_SECONDS)


scanner: Scanner | None = None


def allowed_ids() -> set[int]:
    raw = os.getenv("ALLOWED_USER_IDS", "")
    ids: set[int] = set()
    for piece in raw.split(","):
        piece = piece.strip()
        if piece:
            if not piece.isdecimal():
                raise ValueError("ALLOWED_USER_IDS must be comma-separated numeric Telegram IDs")
            ids.add(int(piece))
    return ids


def authorized(update: Update) -> bool:
    return bool(update.effective_user and update.effective_user.id in allowed_ids())


async def reply(update: Update, text: str):
    if authorized(update) and update.effective_message:
        await update.effective_message.reply_text(text[:3900])


async def notify(app: Application, text: str):
    for uid in allowed_ids():
        try:
            await app.bot.send_message(uid, text[:3900])
        except Exception:
            log.exception("Could not send Telegram notification")


def get_scanner() -> Scanner:
    if scanner is None:
        raise RuntimeError("Scanner not initialized")
    return scanner


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await reply(update,
        "Solana Ultimate onderzoeksversie, PAPER ONLY.\n"
        "/status /scan /best /coin ADDRESS /risk ADDRESS /paper_report "
        "/paper_status /positions /pause /resume /help\n"
        "Automatische entries UIT: veiligheidschecks ontbreken nog. "
        "Scanner pauzeren stopt Telegram niet.")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    s = get_scanner()
    r = s.paper.report()
    await reply(update, f"Scanner: {'PAUZE' if s.paused else 'ACTIEF'}\n"
        f"Laatste scan: {int(time.time()-s.last_scan_at)} sec geleden\n" if s.last_scan_at else
        f"Scanner: {'PAUZE' if s.paused else 'ACTIEF'}\nNog geen scan\n")
    await reply(update, f"Virtuele cash €{r['cash_eur']:.2f}; open: {r['open_positions']}; "
                       f"gerealiseerd: €{r['realized_pnl_eur']:+.2f}\n"
                       f"Laatste fout: {s.last_error or 'geen'}")


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if authorized(update):
        get_scanner().paused = True
        await reply(update, "Scanner gepauzeerd; Telegram blijft actief.")


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if authorized(update):
        get_scanner().paused = False
        await reply(update, "Scanner hervat.")


async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    try:
        candidates, opened, closed = await get_scanner().scan()
        await reply(update, f"Onderzochte profielen: {len(candidates)}; "
            f"virtuele entries: {len(opened)}; exits: {len(closed)}. "
            "Nieuwste profielen zijn niet noodzakelijk nieuwe launches.")
    except Exception:
        log.exception("Manual scan failed")
        await reply(update, "Scan mislukt; controleer de logs.")


async def cmd_best(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    candidates = get_scanner().latest[:8]
    if not candidates:
        await reply(update, "Nog geen profielwaarnemingen. Probeer /scan.")
        return
    await reply(update, "Onderzoeksranglijst (GEEN koopadvies):\n" +
        "\n".join(f"{c.symbol} | score {c.score} | risk {c.risk} | "
                  f"confidence {c.confidence}\n{c.address}" for c in candidates))


async def cmd_coin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    address = context.args[0] if context.args else ""
    if not BASE58.fullmatch(address):
        await reply(update, "Gebruik /coin SOLANA_TOKEN_ADDRESS")
        return
    try:
        pairs = await get_scanner().fetch_pairs(address)
        if not pairs:
            await reply(update, "Geen geldige Solana-markt gevonden.")
            return
        best = max(pairs, key=lambda p: number((p.get("liquidity") or {}).get("usd")))
        assessment = evaluate_token(best)
        findings = ", ".join(f.code for f in assessment.findings) or "geen uit marktdata"
        missing = ", ".join(assessment.missing_evidence) or "geen"
        await reply(update, f"Token: {address}\n"
            f"Price USD: {number(best.get('priceUsd')):.9g}\n"
            f"Liquidity USD: {number((best.get('liquidity') or {}).get('usd')):,.0f}\n"
            f"Risk: {assessment.classification}; score {assessment.score}; "
            f"confidence {assessment.confidence}\n"
            f"Signalen: {findings}\nNiet geverifieerd: {missing}\n"
            "Onbekende veiligheidsstatus is geen bevestiging van veiligheid.")
    except Exception:
        log.exception("Token lookup failed")
        await reply(update, "Tokenanalyse mislukt.")


async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    r = get_scanner().paper.report()
    rate = "onbekend" if r["closed_win_rate"] is None else f"{r['closed_win_rate']*100:.1f}%"
    await reply(update, f"🧪 PAPER ONLY\nCash: €{r['cash_eur']:.2f}\n"
        f"Open posities: {r['open_positions']}\n"
        f"Gesloten posities: {r['closed_positions']}\n"
        f"Gerealiseerd: €{r['realized_pnl_eur']:+.2f}\n"
        f"Winrate: {rate}\nOpen posities zijn niet gewaardeerd.")


async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    trades = get_scanner().paper.open_trades()
    if not trades:
        await reply(update, "Geen open virtuele posities.")
        return
    await reply(update, "\n".join(
        f"#{t['id']} {t['symbol']} €{t['stake_eur']:.2f} | {t['address']}"
        for t in trades[:20]))


async def startup(app: Application):
    await get_scanner().start(app)


async def shutdown(app: Application):
    await get_scanner().close()


def main():
    global scanner
    load_dotenv()
    logging.basicConfig(level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token or not allowed_ids():
        raise RuntimeError("TELEGRAM_BOT_TOKEN and numeric ALLOWED_USER_IDS required")
    scanner = Scanner()
    app = Application.builder().token(token).post_init(startup).post_shutdown(shutdown).build()
    commands = {
        "start": cmd_help, "help": cmd_help, "status": cmd_status,
        "pause": cmd_pause, "resume": cmd_resume, "scan": cmd_scan,
        "best": cmd_best, "coin": cmd_coin, "token": cmd_coin,
        "risk": cmd_coin, "paper_report": cmd_report,
        "paper_status": cmd_report, "positions": cmd_positions,
    }
    for name, callback in commands.items():
        app.add_handler(CommandHandler(name, callback))
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
