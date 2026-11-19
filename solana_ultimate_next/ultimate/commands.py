"""Command registry with a single authorization and throttling boundary."""
from collections import deque
from decimal import Decimal
from inspect import isawaitable, signature
import json
import logging
import time

from . import analytics
from .clients import ProviderError
from .market import Quote
from .telegram import Telegram

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
        rows = analytics.labeled(analytics.replay(self.store))
        return analytics.rolling(rows, int(self.scanner.clock()))

    def learning_report(self, split=None):
        now = int(self.scanner.clock())
        rows = analytics.labeled([row for row in analytics.replay(self.store)
                                  if row["observed"] <= now])
        if split is None:
            split = rows[len(rows) // 2]["observed"] if rows else int(self.scanner.clock())
        return analytics.review(rows, int(split))

    def false_signals(self):
        return analytics.false_signals(analytics.labeled(analytics.replay(self.store)))[-20:]

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
