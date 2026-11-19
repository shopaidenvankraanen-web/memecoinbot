"""Solana Scanner Ultimate — isolated, read-only test prototype.

No real trades, wallets, signing, or payments. No production files accessed.
Requires: pip install aiohttp python-telegram-bot python-dotenv
Environment: TELEGRAM_BOT_TOKEN and ALLOWED_USER_IDS (comma-separated).
This is a foundation, NOT a drop-in replacement for the full 5,791-line bot.
"""
import asyncio
import logging
import os
from dataclasses import dataclass

import aiohttp
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

load_dotenv()
LOG = logging.getLogger("scanner_test_v2")
PROFILES_URL = "https://api.dexscreener.com/token-profiles/latest/v1"
PAIRS_URL = "https://api.dexscreener.com/token-pairs/v1/solana/"
START_BALANCE_EUR = 20.0
DEFAULT_PAPER_POSITION_EUR = 2.0
MAX_PAPER_POSITION_EUR = 4.0


@dataclass(frozen=True)
class TokenSnapshot:
    address: str
    symbol: str
    price_usd: float
    liquidity_usd: float
    volume_24h_usd: float
    risk: str


def number(value):
    try:
        result = float(value)
        if result != result or result in (float("inf"), float("-inf")):
            return 0.0
        return max(result, 0.0)
    except (ValueError, TypeError, OverflowError):
        return 0.0


def analyse_pair(address, pair):
    if not isinstance(pair, dict) or pair.get("chainId") != "solana":
        return None
    base = pair.get("baseToken") or {}
    if base.get("address") != address:
        return None
    liquidity = number((pair.get("liquidity") or {}).get("usd"))
    volume = number((pair.get("volume") or {}).get("h24"))
    price = number(pair.get("priceUsd"))
    # Unknown means unverified, not safe. Market data alone cannot prove safety.
    risk = "HIGH" if liquidity < 3000 or volume < 1000 else "UNKNOWN"
    return TokenSnapshot(
        address=address, symbol=str(base.get("symbol") or "UNKNOWN")[:24],
        price_usd=price, liquidity_usd=liquidity,
        volume_24h_usd=volume, risk=risk
    )


class ReadOnlyScanner:
    def __init__(self):
        self.paused = True
        self.last_results = []
        self.lock = asyncio.Lock()

    async def scan(self):
        async with self.lock:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(PROFILES_URL) as response:
                    response.raise_for_status()
                    profiles = await response.json()
                results = []
                if not isinstance(profiles, list):
                    return results
                for profile in profiles[:12]:
                    if not isinstance(profile, dict) or profile.get("chainId") != "solana":
                        continue
                    address = profile.get("tokenAddress")
                    if not isinstance(address, str) or not (32 <= len(address) <= 44):
                        continue
                    try:
                        async with session.get(PAIRS_URL + address) as response:
                            response.raise_for_status()
                            pairs = await response.json()
                        if not isinstance(pairs, list):
                            continue
                        candidates = [analyse_pair(address, pair) for pair in pairs]
                        candidates = [x for x in candidates if x is not None]
                        if candidates:
                            results.append(max(candidates, key=lambda x: x.liquidity_usd))
                    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                        LOG.exception("Market-data request failed for a token")
                self.last_results = results
                return results


scanner = ReadOnlyScanner()


def authorized(update):
    allowed = {int(v.strip()) for v in os.getenv("ALLOWED_USER_IDS", "").split(",")
               if v.strip().isdigit()}
    user = update.effective_user
    return bool(allowed and user and user.id in allowed)


async def respond(update, text):
    if not authorized(update):
        return
    if update.effective_message:
        await update.effective_message.reply_text(text)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await respond(update, "Testscanner actief. Alleen onderzoek; geen echte trades. /scan /status /pause /resume /best /help")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await respond(update, f"Testscanner: {'gepauzeerd' if scanner.paused else 'actief'}; laatste scan: {len(scanner.last_results)} tokens. Paper start: €20; standaard €2; maximum €4.")


async def pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if authorized(update):
        scanner.paused = True
        await respond(update, "Automatisch scannen gepauzeerd; Telegram blijft bereikbaar.")


async def resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if authorized(update):
        scanner.paused = False
        await respond(update, "Scanner ingeschakeld. Handmatige scan met /scan. Geen achtergrondscan in deze prototypeversie.")


async def scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    try:
        results = await scanner.scan()
        await respond(update, f"Scan afgerond: {len(results)} tokens. /best voor resultaten.")
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        LOG.exception("Scan failed")
        await respond(update, "Scan mislukt; probeer later opnieuw.")


async def best(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    items = sorted(scanner.last_results, key=lambda x: x.liquidity_usd, reverse=True)[:5]
    if not items:
        await respond(update, "Nog geen scanresultaten. Gebruik /scan.")
        return
    lines = [f"{t.symbol}: liquiditeit ${t.liquidity_usd:,.0f}, 24u-volume ${t.volume_24h_usd:,.0f}, risico {t.risk}" for t in items]
    await respond(update, "Marktdata (geen koopadvies):\n" + "\n".join(lines))


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)


def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN ontbreekt")
    if not any(v.strip().isdigit() for v in os.getenv("ALLOWED_USER_IDS", "").split(",")):
        raise RuntimeError("ALLOWED_USER_IDS ontbreekt")
    logging.basicConfig(level=logging.INFO)
    app = Application.builder().token(token).build()
    for name, handler in (
        ("start", start), ("status", status), ("pause", pause),
        ("resume", resume), ("scan", scan), ("best", best),
        ("help", help_command)
    ):
        app.add_handler(CommandHandler(name, handler))
    app.run_polling()


if __name__ == "__main__":
    main()
