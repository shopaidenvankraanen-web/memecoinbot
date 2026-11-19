import asyncio
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from ultimate import analytics, backtest
from ultimate.__main__ import run
from ultimate.clients import PAIRS, PROFILES, RPC, ProviderError, PublicData, RecordedTransport, Response
from ultimate.commands import Commands
from ultimate.market import normalize
from ultimate.notifications import Notifications
from ultimate.paper import Paper, Policy
from ultimate.risk import CHECKS, Evidence, Verdict, assess
from ultimate.scanner import Scanner, Scheduler
from ultimate.storage import MIGRATIONS, ROOT, Store
from ultimate.wallets import Wallets

MINT, PAIR, ALTERNATE = "A" * 32, "B" * 32, "C" * 32
NOW = 10000
LEGACY = """start help scan top_tokens token wallet wallet_watch wallet_unwatch wallet_list
paper_status paper_open paper_close paper_trades paper_report advanced_paper_report market_regime
false_signals learning_status learning_report alert_history alert_settings scheduler_start
scheduler_stop scheduler_status dashboard system_report database_status database_backup database_tables""".split()


def market(price=1, **changes):
    return {
        "chainId": "solana", "baseToken": {"address": MINT, "symbol": "TEST"},
        "pairAddress": PAIR, "priceUsd": price, "liquidity": {"usd": 50000},
        "volume": {"h24": 10000}, "priceChange": {"h24": 12}, "marketCap": 100000,
        "fdv": 200000, "pairCreatedAt": 1000000, **changes,
    }


def quote(price=1, observed=NOW):
    return normalize(market(price), MINT, observed).quote


def capture(observed, pairs, profiles=()):
    return {"observed": observed, "pairs": {MINT: pairs}, "profiles": [
        {"chainId": "solana", "tokenAddress": mint} for mint in profiles]}


class WorkspaceTests:
    def setup_store(self):
        directory = TemporaryDirectory(prefix=".test-", dir=ROOT)
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "paper.sqlite"
        self.store = Store(self.path)


class MarketTests(unittest.TestCase):
    def test_invalid_shape_price_chain_and_base(self):
        self.assertIsNone(normalize([], MINT, NOW))
        for bad in ("NaN", "Infinity", 0, -1, True, None, "1e10000"):
            with self.subTest(bad=bad):
                self.assertIsNone(normalize(market(bad), MINT, NOW))
        self.assertIsNone(normalize(market(chainId="ethereum"), MINT, NOW))
        self.assertIsNone(normalize(market(baseToken=[], quoteToken={"address": MINT}), MINT, NOW))

    def test_score_components_missing_fields(self):
        candidate = normalize(market(liquidity=[], priceChange=[], pairCreatedAt=10**12), MINT, NOW)
        self.assertIsNone(candidate.created)
        self.assertEqual(candidate.components(), {
            "liquidity": 0, "volume": 20, "momentum": 0, "valuation": 0, "pair_timestamp": 0})
        self.assertEqual(normalize(market(), MINT, NOW).score, 100)

    def test_unknown_and_adverse_evidence(self):
        candidate = normalize(market(), MINT, NOW)
        evidence = Evidence(MINT, PAIR, "fixture-provider", NOW, dict.fromkeys(CHECKS, Verdict.CLEAR))
        trusted = {"fixture-provider"}
        self.assertEqual(assess(candidate, NOW).classification, "UNKNOWN")
        self.assertEqual(assess(candidate, NOW, evidence).classification, "UNKNOWN")
        self.assertEqual(assess(candidate, NOW + 301, evidence, trusted_sources=trusted).classification,
                         "UNKNOWN")
        self.assertEqual(assess(candidate, NOW, replace(evidence, pair=ALTERNATE),
                                trusted_sources=trusted).classification, "UNKNOWN")
        self.assertTrue(assess(candidate, NOW, evidence, trusted_sources=trusted).allows_automatic_entry)
        for key in CHECKS:
            with self.subTest(check=key):
                adverse = replace(evidence, checks={**evidence.checks, key: Verdict.ADVERSE})
                self.assertEqual(assess(candidate, NOW, adverse, trusted_sources=trusted).classification,
                                 "HIGH")
                missing = replace(evidence, checks={**evidence.checks, key: "CLEAR"})
                self.assertFalse(assess(candidate, NOW, missing,
                                        trusted_sources=trusted).allows_automatic_entry)

    def test_market_anomaly_remains_heuristic(self):
        candidate = normalize(market(volume={"h24": 9000000}), MINT, NOW)
        result = assess(candidate, NOW)
        self.assertIn("unusual_volume_liquidity_ratio", result.market_flags)
        self.assertEqual(result.checks["fake_volume"], Verdict.UNKNOWN)


class StorageTests(WorkspaceTests, unittest.TestCase):
    def setUp(self):
        self.setup_store()

    def test_upgrade_v1_and_restart(self):
        old = self.path.with_name("v1.sqlite")
        with closing(sqlite3.connect(old)) as db, db:
            for statement in MIGRATIONS[0]:
                db.execute(statement)
            db.execute("UPDATE account SET cents=1800")
            db.execute("PRAGMA user_version=1")
        upgraded = Store(old)
        self.assertEqual(upgraded.status()["version"], len(MIGRATIONS))
        self.assertEqual(Store(old).rows("SELECT cents FROM account")[0]["cents"], 1800)
        self.assertEqual(upgraded.status()["integrity"], ["ok"])

    def test_backup_restore_no_overwrite(self):
        event = Paper(self.store).enter(quote(), NOW)
        backup = self.store.backup()
        restored = Store.recover(backup, self.path.with_name("recovered.sqlite"))
        self.assertEqual(Paper(restored).open_positions()[0]["id"], event["id"])
        self.assertEqual(Paper(restored).report()["cash_cents"], 1798)
        self.assertEqual(restored.status()["foreign_key_violations"], 0)
        with self.assertRaises(FileExistsError):
            Store.recover(backup, self.path)

    def test_path_escape(self):
        for path in (ROOT.parent / "blocked.sqlite", Path("/tmp/blocked.sqlite")):
            with self.assertRaises(ValueError):
                Store(path)
        link = self.path.with_name("escape.sqlite")
        link.symlink_to(ROOT.parent / "blocked.sqlite")
        with self.assertRaises(ValueError):
            Store(link)

    def test_transaction_rolls_back(self):
        with self.assertRaises(RuntimeError):
            with self.store.transaction(write=True) as db:
                db.execute("UPDATE account SET cents=0")
                raise RuntimeError
        self.assertEqual(Paper(self.store).report()["cash_cents"], 2000)


class PaperTests(WorkspaceTests, unittest.TestCase):
    def setUp(self):
        self.setup_store()
        self.paper = Paper(self.store)

    def test_fees_slippage_take_profit_and_cash(self):
        entry = self.paper.enter(quote(), NOW)
        self.assertEqual(entry["cash_delta_cents"], -202)
        outcome = self.paper.close(entry["id"], quote("1.25", NOW + 1), NOW + 1)
        self.assertEqual(outcome["reason"], "take_profit")
        self.assertEqual(outcome["cash_delta_cents"], 246)
        self.assertEqual(outcome["pnl_cents"], 44)
        report = Paper(Store(self.path)).report()
        self.assertEqual(report["cash_cents"], 2044)
        self.assertEqual(report["fees_cents"], 4)
        self.assertEqual(len(self.store.rows("SELECT * FROM audit")), 2)

    def test_stop_loss_and_age_limit(self):
        paper = Paper(self.store, Policy(duration=10))
        first = paper.enter(quote(), NOW)
        self.assertEqual(paper.close(first["id"], quote(".85", NOW + 1), NOW + 1)["reason"], "stop_loss")
        second = paper.enter(quote(observed=NOW + 2), NOW + 2)
        self.assertIsNone(paper.close(second["id"], quote(observed=NOW + 11), NOW + 11))
        self.assertEqual(paper.close(second["id"], quote(observed=NOW + 12), NOW + 12)["reason"],
                         "age_limit")

    def test_missing_stale_future_and_wrong_pair(self):
        self.assertIsNone(self.paper.enter(quote(observed=NOW - 121), NOW))
        self.assertIsNone(self.paper.enter(quote(observed=NOW + 1), NOW))
        event = self.paper.enter(quote(), NOW)
        for invalid in (None, quote(2), quote(2, NOW + 1),
                        replace(quote(2, NOW + 200), pair=ALTERNATE)):
            self.assertIsNone(self.paper.close(event["id"], invalid, NOW + 200))
        self.assertEqual(self.paper.report()["open"], 1)

    def test_raced_entry_and_exit(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            entries = list(pool.map(lambda _: self.paper.enter(quote(), NOW), range(14)))
            valid = [row for row in entries if row]
            self.assertEqual(len(valid), 1)
            trade = valid[0]["id"]
            exits = list(pool.map(lambda _: self.paper.close(trade, quote(2, NOW + 1), NOW + 1),
                                  range(14)))
        self.assertEqual(sum(row is not None for row in exits), 1)
        self.assertEqual(self.paper.report()["closed"], 1)

    def test_limits_and_no_negative_cash(self):
        self.assertIsNone(self.paper.enter(quote(), NOW, 401))
        paper = Paper(self.store, Policy(stake=400, positions=10))
        with ThreadPoolExecutor(max_workers=8) as pool:
            entries = list(pool.map(lambda n: paper.enter(replace(quote(), mint=str(n)), NOW), range(10)))
        self.assertEqual(sum(row is not None for row in entries), 4)
        self.assertEqual(paper.report()["cash_cents"], 384)
        self.assertIsNone(Paper(self.store).enter(replace(quote(), mint="new"), NOW))

    def test_unknown_auto_entry_and_restart_position(self):
        candidate = normalize(market(), MINT, NOW)
        self.assertIsNone(self.paper.enter(quote(), NOW, automatic=True,
                                           assessment=assess(candidate, NOW)))
        self.assertIsNotNone(self.paper.enter(quote(), NOW))
        self.assertEqual(len(Paper(Store(self.path)).open_positions()), 1)


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_429_retry_and_metrics(self):
        transport = AsyncMock(side_effect=[Response(429, None, "3"), Response(503, None),
                                           Response(200, [])])
        sleep = AsyncMock()
        client = PublicData(transport, sleep=sleep)
        self.assertEqual(await client.profiles(), [])
        self.assertEqual([call.args for call in sleep.call_args_list], [(3,), (2,)])
        self.assertEqual(client.metrics["http_429"], 1)
        self.assertEqual(client.metrics["successes"], 1)

    async def test_bounded_concurrency_timeout_and_bad_response(self):
        active = peak = 0

        async def transport(*_):
            nonlocal active, peak
            active += 1
            peak = max(active, peak)
            try:
                await asyncio.sleep(.01)
                return Response(200, {})
            finally:
                active -= 1

        client = PublicData(transport, concurrency=2, attempts=1)
        results = await asyncio.gather(*(client.profiles() for _ in range(6)), return_exceptions=True)
        self.assertTrue(all(isinstance(result, ProviderError) for result in results))
        self.assertEqual(peak, 2)
        timeout = PublicData(transport, timeout=.001, attempts=1)
        with self.assertRaises(ProviderError):
            await timeout.profiles()
        self.assertEqual(timeout.metrics["TimeoutError"], 1)


class IntegrationTests(WorkspaceTests, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.setup_store()
        self.now = NOW
        self.client = PublicData(RecordedTransport(capture(NOW, [])))
        self.paper = Paper(self.store)
        self.sender = AsyncMock()
        self.alerts = Notifications(self.store, {5}, self.sender)
        self.scanner = Scanner(self.store, self.paper, self.client, clock=lambda: self.now,
                               notifications=self.alerts)
        self.scheduler = Scheduler(self.scanner, interval=.01)
        self.addAsyncCleanup(self.scheduler.stop)
        self.commands = Commands(self.scanner, self.scheduler, Wallets(self.store, self.client),
                                 self.alerts, {5}, clock=lambda: self.now)

    async def test_duplicate_profiles_multimarket_and_independent_exit(self):
        self.alerts.set("new_token", True)
        self.client.transport = RecordedTransport(capture(NOW, [
            market(), market(), market(pairAddress=ALTERNATE, liquidity={"usd": 40000}),
        ], [MINT, MINT]))
        result = await self.scanner.scan()
        self.assertEqual(result["profiles"], 1)
        self.assertEqual(result["rejected"], 1)
        self.assertFalse(result["events"])
        self.assertEqual(len(analytics.replay(self.store)), 2)
        self.assertEqual(self.store.rows("SELECT created FROM pairs WHERE pair=?", (PAIR,))[0]["created"],
                         1000)
        await self.scanner.scan()
        self.assertEqual(len(analytics.replay(self.store)), 2)
        self.sender.assert_awaited_once()
        result = json.loads(await self.commands.dispatch(5, True, "paper_open", [MINT]))
        self.assertEqual(result["kind"], "entry")
        self.now += 1
        self.client.transport = RecordedTransport(capture(self.now, [
            market(2), market(20, pairAddress=ALTERNATE, liquidity={"usd": 900000}),
        ]))
        result = await self.scanner.scan()
        self.assertEqual(result["events"][0]["reason"], "take_profit")
        self.assertEqual(self.paper.report()["cash_cents"], 2190)

    async def test_failed_discovery_still_refreshes_open_position(self):
        self.paper.enter(quote(), NOW)
        self.now += 1

        async def transport(method, url, body):
            if url == PROFILES:
                return Response(404, None)
            self.assertEqual(url, PAIRS + MINT)
            return Response(200, [market(".50")])

        self.client.transport = transport
        result = await self.scanner.scan()
        self.assertEqual(result["events"][0]["reason"], "stop_loss")

    async def test_explicit_evidence_provider_entry(self):
        self.scanner.evidence = AsyncMock(return_value=Evidence(
            MINT, PAIR, "fixture", NOW, dict.fromkeys(CHECKS, Verdict.CLEAR)))
        self.scanner.trusted_sources = {"fixture"}
        self.client.transport = RecordedTransport(capture(NOW, [market()], [MINT]))
        result = await self.scanner.scan()
        self.assertEqual(result["accepted"], [MINT])
        self.assertEqual(result["events"][0]["reason"], "automatic")

    async def test_all_telegram_handlers_authorize_and_rate_limit(self):
        class Application:
            def __init__(self):
                self.handlers = {}

            def add_handler(self, item):
                name, callback = item
                self.handlers[name] = callback

        app = Application()
        self.commands.register_telegram(app, lambda *item: item)
        self.assertEqual(set(app.handlers), set(LEGACY + ["pause", "resume"]))
        message = SimpleNamespace(reply_text=AsyncMock(), message_id=1)
        update = SimpleNamespace(effective_user=SimpleNamespace(id=77, is_bot=False), message=message,
                                 effective_chat=SimpleNamespace(id=77, type="private"))
        context = SimpleNamespace(args=[])
        for callback in app.handlers.values():
            await callback(update, context)
        self.assertEqual({call.args[0] for call in message.reply_text.await_args_list}, {"Geen toegang."})
        self.assertFalse(self.scanner.paused())
        update.effective_user.id = update.effective_chat.id = 5
        update.effective_chat.type = "group"
        message.reply_text.reset_mock()
        await app.handlers["pause"](update, context)
        message.reply_text.assert_not_awaited()
        update.effective_chat.type = "private"
        await app.handlers["pause"](update, context)
        self.assertTrue(self.scanner.paused())
        self.assertEqual(await self.scanner.scan(), {"paused": True})
        self.assertIsNone(message.reply_text.await_args.kwargs["parse_mode"])
        message.message_id += 1
        await app.handlers["resume"](update, context)
        self.assertFalse(self.scanner.paused())
        for _ in range(6):
            message.message_id += 1
            await app.handlers["help"](update, context)
        message.message_id += 1
        await app.handlers["pause"](update, context)
        self.assertFalse(self.scanner.paused())
        self.assertIn("Te veel", message.reply_text.await_args.args[0])
        self.now += 10
        message.message_id += 1
        await app.handlers["pause"](update, context)
        self.assertTrue(self.scanner.paused())

    async def test_scheduler_cancel_and_pause_survive_restart(self):
        self.scanner.pause(True)
        self.assertTrue(self.scheduler.start())
        self.assertFalse(self.scheduler.start())
        await asyncio.sleep(.02)
        self.assertTrue(await self.scheduler.stop())
        reopened = Scanner(Store(self.path), self.paper, self.client)
        self.assertTrue(reopened.paused())

    async def test_notification_concurrent_duplicate(self):
        self.alerts.set("paper_trade", True)
        await asyncio.gather(*(self.alerts.emit("paper_trade", "1", "local", NOW)
                               for _ in range(10)))
        self.sender.assert_awaited_once_with(5, "local")
        self.assertEqual(self.commands.alert_history()[0]["state"], "sent")

    async def test_wallet_rpc_and_watchlist(self):
        frame = capture(NOW, [])
        frame["wallets"] = {MINT: [{"signature": "observed", "blockTime": NOW, "err": None}]}
        self.client.transport = RecordedTransport(frame)
        wallet = self.commands.wallets
        wallet.watch(MINT, "test")
        for _ in range(2):
            self.assertEqual((await wallet.activity(MINT))["attribution"], "UNKNOWN")
        self.assertEqual(len(wallet.list()), 1)
        self.assertEqual(len(self.store.rows("SELECT * FROM activity")), 1)
        self.assertTrue(wallet.unwatch(MINT))

    async def test_pair_lookup_fallback_requires_exact_pair_and_mint(self):
        self.paper.enter(quote(), NOW)
        self.now += 1
        frame = capture(self.now, [])
        frame["pair_quotes"] = {PAIR: {"pairs": [
            market(20, pairAddress=ALTERNATE),
            market(20, baseToken={"address": ALTERNATE}),
        ]}}
        self.client.transport = RecordedTransport(frame)
        self.assertEqual((await self.scanner.scan())["events"], [])
        response = json.loads(await self.commands.dispatch(5, True, "paper_close", ["1"]))
        self.assertEqual(response["reason"], "fresh_pair_quote_required")
        frame["pair_quotes"][PAIR]["pairs"].append(market(2))
        result = await self.scanner.scan()
        self.assertEqual(result["events"][0]["reason"], "take_profit")
        self.assertEqual(self.store.rows("SELECT observed FROM observations"), [{"observed": self.now}])

    async def test_wallet_balance_exact_units_and_rpc_allowlist(self):
        frame = capture(NOW, [])
        frame["wallet_balances"] = {MINT: {"context": {"slot": 123}, "value": 1234567890}}
        self.client.transport = RecordedTransport(frame)
        result = await self.commands.wallets.activity(MINT)
        self.assertEqual(result["balance"], {"slot": 123, "lamports": 1234567890, "sol": "1.234567890"})
        self.assertIsNone(result["sample"])
        self.assertEqual(result["attribution"], "UNKNOWN")
        frame["wallet_balances"][MINT]["value"] = True
        with self.assertRaises(ProviderError):
            await self.client.wallet_balance(MINT)
        denied = await self.client.transport("POST", RPC, {"method": "sendTransaction", "params": [MINT]})
        self.assertEqual(denied.status, 404)

    async def test_advanced_paper_report_marks_only_fresh_same_pair_quotes(self):
        first = self.paper.enter(quote(), NOW)
        self.now += 1
        self.paper.close(first["id"], quote(2, self.now), self.now)
        self.paper.enter(quote(2, self.now), self.now)
        self.now += 1
        self.client.transport = RecordedTransport(capture(self.now, [market("2.2")], [MINT]))
        await self.scanner.scan()
        report = json.loads(await self.commands.dispatch(5, True, "advanced_paper_report", []))
        self.assertEqual(report["wins"], 1)
        self.assertEqual(report["best_trade"], {"id": first["id"], "pnl_cents": 190})
        self.assertEqual(report["exit_reasons"], {"take_profit": 1})
        self.assertEqual(report["valuation"]["estimated_equity_cents"], 2204)
        self.assertEqual(report["valuation"]["marked_unrealized_pnl_cents"], 14)

        for timestamp, pair in ((self.now + 121, ALTERNATE), (self.now + 122, PAIR)):
            candidate = normalize(market(3, pairAddress=pair), MINT, timestamp)
            self.scanner.record([], [(candidate, assess(candidate, timestamp))], timestamp)
        self.now += 121
        valuation = self.commands.advanced_paper_report()["valuation"]
        self.assertEqual(valuation["unpriced_positions"], 1)
        self.assertIsNone(valuation["estimated_equity_cents"])

    async def test_telegram_replay_rejects_forged_chat_and_stale_messages(self):
        path = self.path.with_suffix(".json")
        frame = capture(NOW, [])
        message = {"message_id": 7, "from": {"id": 5, "is_bot": False},
                   "chat": {"id": 9, "type": "private"}, "text": "/pause"}
        frame["telegram_update"] = {"message": message}
        args = SimpleNamespace(action="telegram-replay", db=self.path, capture=path, user_id=5)

        async def replay():
            path.write_text(json.dumps([frame]))
            with patch("socket.socket", side_effect=AssertionError("Unexpected network socket")):
                return await run(args)

        self.assertIsNone(await replay())
        self.assertFalse(self.scanner.paused())
        message["chat"]["id"] = 5
        self.assertEqual(json.loads(await replay()), {"paused": True})
        message["text"] = "/resume"
        self.assertIsNone(await replay())
        message["message_id"] = 6
        self.assertIsNone(await replay())
        self.assertTrue(self.scanner.paused())
        message.update(message_id=8, text="/scheduler_start")
        self.assertIn("eindige offline scanreeks", await replay())
        message.update(message_id=9, text="/resume")
        self.assertEqual(json.loads(await replay()), {"paused": False})

    async def test_backtest_is_reproducible_and_isolates_split_cash(self):
        for observed, price in ((NOW, "1"), (NOW + 1, "1.5"),
                                (NOW + 2, "2"), (NOW + 3, "1")):
            self.now = observed
            self.client.transport = RecordedTransport(capture(observed, [market(price)], [MINT]))
            await self.scanner.scan()
        result = backtest.compare_portfolios(self.store, NOW + 2)
        self.assertEqual(result, backtest.compare_portfolios(self.store, NOW + 2))
        self.assertEqual(result["portfolios"]["train"][60]["closed"], 1)
        self.assertGreater(result["portfolios"]["train"][60]["cash_cents"], 2000)
        self.assertEqual(result["portfolios"]["test"][60]["closed"], 1)
        self.assertLess(result["portfolios"]["test"][60]["cash_cents"], 2000)
        self.assertEqual(self.paper.report()["cash_cents"], 2000)
        self.assertEqual(self.paper.report()["closed"], 0)

    async def test_cli_ingest_without_network(self):
        path = self.path.with_suffix(".json")
        path.write_text(json.dumps([capture(NOW, [market()], [MINT])]))
        args = SimpleNamespace(action="ingest", db=self.path, capture=path)
        # Asyncio creates its event-loop sockets before entering this test.
        with patch("socket.socket", side_effect=AssertionError("Unexpected network socket")):
            result = await run(args)
        self.assertEqual(result[0]["profiles"], 1)
        self.assertEqual(self.paper.report()["cash_cents"], 2000)


class AnalyticsTests(unittest.TestCase):
    def test_temporal_label_availability_and_split(self):
        rows = [{"pair": PAIR, "mint": MINT, "observed": t, "price": p, "score": s}
                for t, p, s in ((1, "1", 80), (11, "1.2", 40), (21, "1.5", 80),
                                (31, "1.4", 90), (41, "1.4", 60))]
        labeled = analytics.labeled(rows, horizon=10, tolerance=0)
        result = analytics.compare(labeled, 21)
        self.assertEqual(result["thresholds"][60]["train"]["total"], 1)
        self.assertEqual(result["thresholds"][60]["test"]["fp"], 2)
        self.assertEqual(result["thresholds"][60]["test"]["unknown"], 1)
        self.assertEqual(analytics.rolling(labeled, 10)["unknown"], 1)
        self.assertEqual(len(analytics.false_signals(labeled)), 2)
