"""Offline host lifecycle, public provider failures and persistence."""
import asyncio
import json
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from ultimate.clients import PAIR_BY_ID, PAIRS, PROFILES, RPC, ProviderError, PublicData, Response
from ultimate.host import HostError, host_settings, telegram_host
from ultimate.live import MarketTransport
from ultimate.notifications import Notifications
from ultimate.paper import Paper
from ultimate.scanner import Scanner, Scheduler
from ultimate.storage import Store
from tests.test_offline import LEGACY, MINT, NOW, PAIR, WorkspaceTests, market, quote


class PublicScannerTests(WorkspaceTests, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.setup_store()

    async def test_scanner_restart_recovery_conflicts_and_failures(self):
        now = NOW
        discovery_failure = False
        fallback_failure = False
        profiles = [{"chainId": "solana", "tokenAddress": MINT}] * 2
        records = [market(2, priceChange={"h24": 80})] * 2

        async def fetch(method, url, body):
            if url == PROFILES:
                return Response(503, None) if discovery_failure else Response(200, profiles)
            if url == PAIRS + MINT:
                return Response(200, records)
            if url == PAIR_BY_ID + PAIR:
                return Response(503, None) if fallback_failure else Response(200, {"pairs": records})
            raise AssertionError("Unexpected request")

        client = PublicData(fetch, sleep=AsyncMock())
        send = AsyncMock()

        def open_scanner(store):
            alerts = Notifications(store, {42}, send)
            return Scanner(store, Paper(store), client, clock=lambda: now,
                           public_quotes=True, notifications=alerts)

        scanner = open_scanner(self.store)
        for alert in ("new_token", "market_movement", "paper_trade"):
            scanner.notifications.set(alert, True)
        report = await scanner.scan()
        self.assertEqual((report["profiles"], report["accepted"], report["events"]), (1, [], []))
        self.assertEqual(scanner.latest()[0]["risk"], "UNKNOWN")
        self.assertEqual(len(scanner.latest()), 1)
        self.assertEqual(scanner.paper.report()["cash_cents"], 2000)
        self.assertEqual(send.await_count, 2)
        scanner.pause(True)
        backup = self.store.backup()
        recovered = Store.recover(backup, self.store.path.with_name("recovered.sqlite"))
        scanner = open_scanner(recovered)
        self.assertEqual(recovered.status()["integrity"], ["ok"])
        self.assertEqual(await scanner.scan(), {"paused": True})
        now += 1
        scanner.pause(False)
        await scanner.scan()
        self.assertEqual(send.await_count, 2)
        trade = scanner.paper.enter(quote(observed=now), now)
        self.assertIsNotNone(trade)
        now += 1
        discovery_failure = fallback_failure = True
        records[:] = [market(2), market(3)]
        self.assertEqual((await scanner.scan())["events"], [])
        self.assertEqual(len(scanner.paper.open_positions()), 1)
        self.assertGreater(client.metrics["retries"], 0)
        # Both discovery and the primary quote can fail while a valid exact-pair
        # fallback still protects the existing virtual position.
        fallback_failure = False
        records[:] = [market(2)]
        self.assertEqual((await scanner.scan())["events"][0]["kind"], "exit")
        self.assertEqual(len(scanner.paper.open_positions()), 0)
        self.assertEqual(send.await_count, 3)

    async def test_old_quote_after_delayed_refresh_does_not_close(self):
        now = NOW
        paper = Paper(self.store)
        self.assertIsNotNone(paper.enter(quote(), now))
        now += 1

        async def fetch(method, url, body):
            return Response(200, [] if url == PROFILES else [market(2)])

        def clock():
            return now

        async def evidence(candidate):
            nonlocal now
            now += 301
            return None

        scanner = Scanner(self.store, paper, PublicData(fetch), clock=clock,
                          evidence=evidence, public_quotes=True)
        self.assertEqual((await scanner.scan())["events"], [])
        self.assertEqual(len(paper.open_positions()), 1)

    async def test_shared_pacing_and_wallet_allowlist(self):
        elapsed = 0
        starts = []

        async def sleep(seconds):
            nonlocal elapsed
            elapsed += seconds

        def get(host, method, path, body=None):
            starts.append((elapsed, host, method))
            if len(starts) == 1:
                return Response(429, None, "2")
            if method == "GET":
                return Response(200, [])
            return Response(200, {"id": 1, "result": {"value": 2, "context": {"slot": 1}}})

        transport = MarketTransport(clock=lambda: elapsed, sleep=sleep)
        client = PublicData(transport, concurrency=1, sleep=sleep)
        with patch.object(transport, "fetch_json", side_effect=get):
            results = await asyncio.gather(client.profiles(), client.wallet_balance(MINT))
            self.assertEqual(results, [[], {"lamports": 2, "slot": 1}])
            self.assertEqual(starts, [(0, "api.dexscreener.com", "GET"),
                                      (2, "api.mainnet-beta.solana.com", "POST"),
                                      (3, "api.dexscreener.com", "GET")])
            with self.assertRaises(ProviderError):
                await transport("POST", RPC, {"method": "unsupported"})
            self.assertEqual(len(starts), 3)

    async def test_timeout_keeps_one_inflight_provider_request(self):
        transport = MarketTransport(sleep=AsyncMock())
        client = PublicData(transport, concurrency=1, attempts=1, timeout=.02)
        release = asyncio.Event()

        async def fetch(*args):
            await release.wait()
            return Response(200, [])

        with patch.object(transport, "fetch_off_loop", side_effect=fetch) as worker:
            for _ in range(2):
                with self.assertRaisesRegex(ProviderError, "unavailable"):
                    await client.profiles()
            self.assertEqual(worker.await_count, 1)
            release.set()
            await transport.inflight
            self.assertEqual(await client.profiles(), [])
            self.assertEqual(worker.await_count, 2)

    async def test_scheduler_scans_periodically_and_stops(self):
        tick = asyncio.Event()
        calls = 0

        async def fetch(method, url, body):
            nonlocal calls
            self.assertEqual((method, url, body), ("GET", PROFILES, None))
            calls += 1
            if calls == 3:
                tick.set()
            return Response(200, [])

        scanner = Scanner(self.store, Paper(self.store), PublicData(fetch),
                          public_quotes=True)
        scheduler = Scheduler(scanner, interval=0)
        self.assertTrue(scheduler.start())
        self.assertFalse(scheduler.start())
        try:
            await asyncio.wait_for(tick.wait(), 1)
        finally:
            self.assertTrue(await scheduler.stop())
        self.assertFalse(scheduler.status()["running"])
        self.assertEqual(calls, 3)

    async def test_failed_alert_is_not_retried_after_restart(self):
        send = AsyncMock(side_effect=OSError("simulated ambiguous response"))
        alerts = Notifications(self.store, {42}, send)
        alerts.set("new_token", True)
        await alerts.emit("new_token", MINT, "Paper alert", NOW)
        restart = Notifications(Store(self.store.path), {42}, send)
        await restart.emit("new_token", MINT, "Paper alert", NOW + 1)
        self.assertEqual(send.await_count, 1)
        self.assertEqual(self.store.rows("SELECT state FROM notifications"), [{"state": "failed"}])


class TelegramHostTests(WorkspaceTests, unittest.TestCase):
    def setUp(self):
        self.setup_store()
        self.environment = {"ULTIMATE_TELEGRAM_TOKEN": "12345:" + "x" * 35,
                            "ULTIMATE_TELEGRAM_USER_IDS": "42,43"}

    def fake_framework(self, exercise):
        app = SimpleNamespace(handlers={}, bot=SimpleNamespace(send_message=AsyncMock()),
                              stop_running=Mock(), hooks={})

        class Conflict(Exception):
            pass

        class Builder:
            def __getattr__(self, key):
                def configure(value):
                    app.hooks[key] = value
                    return self
                return configure

            def build(self):
                return app

        def poll(**kwargs):
            self.assertEqual(kwargs, {"allowed_updates": ["message"], "drop_pending_updates": True,
                                      "bootstrap_retries": 0, "timeout": 10, "close_loop": False})
            asyncio.get_event_loop().run_until_complete(exercise(app, Conflict))

        app.add_handler = lambda entry: app.handlers.update([entry])
        app.add_error_handler = lambda callback: setattr(app, "error", callback)
        app.run_polling = poll
        modules = {name: ModuleType(name) for name in
                   ("telegram", "telegram.error", "telegram.ext", "telegram.request")}
        modules["telegram.error"].Conflict = Conflict
        modules["telegram.ext"].Application = SimpleNamespace(builder=Builder)
        modules["telegram.ext"].CommandHandler = lambda name, callback: (name, callback)
        modules["telegram.request"].HTTPXRequest = lambda **kwargs: kwargs
        return modules

    async def command(self, app, name, message_id, *, user=42, chat=42, kind="private",
                      bot=False, args=()):
        reply = AsyncMock()
        update = SimpleNamespace(effective_user=SimpleNamespace(id=user, is_bot=bot),
                                 effective_chat=SimpleNamespace(id=chat, type=kind),
                                 message=SimpleNamespace(message_id=message_id, reply_text=reply))
        await app.handlers[name](update, SimpleNamespace(args=args))
        if reply.await_count:
            self.assertEqual(reply.await_args.kwargs,
                             {"parse_mode": None, "disable_web_page_preview": True})
            return json.loads(reply.await_args.args[0])
        return None

    def test_host_registration_authorization_pause_restart_and_shutdown(self):
        transport = AsyncMock(return_value=Response(200, []))

        async def first(app, conflict):
            self.assertEqual(set(app.handlers), set(LEGACY) | {"pause", "resume"})
            self.assertIs(app.hooks["concurrent_updates"], False)
            self.assertIs(app.hooks["request"]["httpx_kwargs"]["trust_env"], False)
            await app.hooks["post_init"](app)
            self.assertFalse((await self.command(app, "scheduler_status", 1))["running"])
            for kwargs in ({"user": 99, "chat": 99}, {"chat": 43},
                           {"kind": "group"}, {"bot": True}):
                self.assertIsNone(await self.command(app, "pause", 99, **kwargs))
            self.assertEqual(await self.command(app, "pause", 2), {"paused": True})
            self.assertIsNone(await self.command(app, "resume", 2))
            self.assertEqual(await self.command(app, "scan", 3), {"paused": True})
            transport.assert_not_awaited()
            await app.hooks["post_stop"](app)
            await app.hooks["post_shutdown"](app)

        async def second(app, conflict):
            await app.hooks["post_init"](app)
            self.assertIsNone(await self.command(app, "resume", 3))
            status = await self.command(app, "scheduler_status", 4)
            self.assertTrue(status["running"])
            self.assertTrue(status["paused"])
            self.assertEqual(await self.command(app, "resume", 5), {"paused": False})
            await self.command(app, "alert_settings", 6, args=("new_token", "on"))
            await self.command(app, "scan", 7)
            self.assertEqual(app.bot.send_message.await_count, 2)
            self.assertEqual({call.kwargs["chat_id"] for call in app.bot.send_message.await_args_list},
                             {42, 43})
            await app.error(None, SimpleNamespace(error=conflict()))
            app.stop_running.assert_called_once()
            await app.hooks["post_stop"](app)
            self.assertFalse((await self.command(app, "scheduler_status", 8))["running"])
            await app.hooks["post_shutdown"](app)

        with patch("ultimate.host.MarketTransport", return_value=transport):
            with patch.dict(sys.modules, self.fake_framework(first)):
                telegram_host(self.store.path, False, 120, environment=self.environment)
            transport.return_value = Response(200, [])
            transport.side_effect = lambda method, url, body: Response(200, (
                [{"chainId": "solana", "tokenAddress": MINT}] if url == PROFILES else [market()]))
            with patch.dict(sys.modules, self.fake_framework(second)):
                telegram_host(self.store.path, True, 120, environment=self.environment)

    def test_configuration_and_framework_errors_are_sanitized(self):
        for invalid in ("", "0", "-1", "99, 42", str(2**52)):
            with self.assertRaises(HostError):
                host_settings(dict(self.environment, ULTIMATE_TELEGRAM_USER_IDS=invalid))
        with self.assertRaises(HostError):
            telegram_host(None, False, 120, environment=self.environment)

        async def fail(app, conflict):
            raise RuntimeError(self.environment["ULTIMATE_TELEGRAM_TOKEN"])

        with patch.dict(sys.modules, self.fake_framework(fail)):
            with self.assertRaisesRegex(HostError, r"Telegram host stopped \(RuntimeError\)") as error:
                telegram_host(self.store.path, False, 120, environment=self.environment)
        self.assertNotIn(self.environment["ULTIMATE_TELEGRAM_TOKEN"], str(error.exception))
