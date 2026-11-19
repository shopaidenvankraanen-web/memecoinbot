import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from ultimate import analytics
from ultimate.__main__ import run
from ultimate.clients import PAIR_BY_ID, PROFILES, RPC, ProviderError, PublicData, Response
from ultimate.live import MAX_BYTES, DexTransport, market_snapshot
from ultimate.market import normalize
from ultimate.paper import Paper
from ultimate.risk import Assessment
from ultimate.scanner import Scanner
from tests.test_offline import ALTERNATE, MINT, NOW, PAIR, WorkspaceTests, market, quote


class LiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_pair_mint_and_strict_fields(self):
        transport = AsyncMock(return_value=Response(200, {"pairs": [
            market(10, pairAddress=ALTERNATE),
            market(20, baseToken={"address": ALTERNATE}),
            market(30, liquidity={"usd": True}),
            market(40, pairCreatedAt=NOW * 1000 + 1000),
            market(50, chainId="ethereum"), market(2), market(2),
        ]}))
        result = await market_snapshot(MINT, PAIR, transport=transport, clock=lambda: NOW)
        transport.assert_awaited_once_with("GET", PAIR_BY_ID + PAIR, None)
        self.assertEqual(result["observed"], NOW)
        self.assertEqual(len(result["markets"]), 1)
        self.assertEqual(result["markets"][0]["quote"]["price"], 2)
        self.assertEqual(result["markets"][0]["assessment"]["classification"], "UNKNOWN")
        transport.return_value = Response(200, {"pairs": [market(2), market(3)]})
        with self.assertRaises(ProviderError):
            await market_snapshot(MINT, PAIR, transport=transport)

    async def test_cli_live_profiles_only_and_never_opens_store(self):
        response = Response(200, [
            {"chainId": "solana", "tokenAddress": MINT},
            {"chainId": "solana", "tokenAddress": MINT},
            {"chainId": "ethereum", "tokenAddress": ALTERNATE}, {},
        ])
        transport = AsyncMock(return_value=response)
        with (patch("ultimate.live.DexTransport", return_value=transport),
              patch("ultimate.__main__.Store", side_effect=AssertionError("Unexpected store")),
              patch("socket.socket.connect", side_effect=AssertionError("Unexpected network connection"))):
            result = await run(SimpleNamespace(action="live-market", mint=None, pair=None))
        self.assertEqual(result["profile_mints"], [MINT])
        transport.assert_awaited_once_with("GET", PROFILES, None)

    async def test_rate_limit_counts_retries_and_concurrent_calls(self):
        now = 0
        starts = []

        async def sleep(seconds):
            nonlocal now
            now += seconds

        def fetch(_):
            starts.append(now)
            return Response(429, None, "0") if len(starts) == 1 else Response(200, [])

        transport = DexTransport(clock=lambda: now, sleep=sleep)
        client = PublicData(transport, sleep=sleep)
        with patch.object(transport, "_fetch", side_effect=fetch):
            self.assertEqual(await asyncio.gather(client.profiles(), client.profiles()), [[], []])
        self.assertEqual(starts, [0, 1, 2])
        self.assertEqual(client.metrics["retries"], 1)
        for method, url, body in (("POST", PROFILES, None), ("GET", RPC, None),
                                  ("GET", PROFILES + "?extra=1", None), ("GET", PROFILES, {})):
            with self.assertRaises(ProviderError):
                await transport(method, url, body)

    async def test_https_validation_redirect_and_timeout(self):
        with (patch("ultimate.live.HTTPSConnection") as connect,
              patch("socket.socket.connect", side_effect=AssertionError("Unexpected network connection"))):
            connection = connect.return_value
            response = connection.getresponse.return_value
            response.getheader.return_value = "application/json; charset=utf-8"
            response.status = 200
            for body in (b'{"duplicate":1,"duplicate":2}', b'[NaN]', b'\xff',
                         b'[' + b' ' * MAX_BYTES + b']'):
                response.read1.side_effect = [body, b""]
                with self.assertRaises(ProviderError):
                    await market_snapshot()
            response.read1.side_effect = [b"[]", b""]
            self.assertEqual((await market_snapshot())["profile_mints"], [])
            connect.assert_called_with("api.dexscreener.com", timeout=5)
            connection.request.assert_called_with(
                "GET", "/token-profiles/latest/v1",
                headers={"Accept": "application/json", "Accept-Encoding": "identity"})
            response.status = 302
            with self.assertRaisesRegex(ProviderError, "HTTP 302"):
                await market_snapshot()
            connection.request.side_effect = TimeoutError
            client = PublicData(DexTransport(sleep=AsyncMock()), attempts=2, sleep=AsyncMock())
            with self.assertRaisesRegex(ProviderError, "unavailable"):
                await client.profiles()
            self.assertEqual(client.metrics["TimeoutError"], 2)
            connection.close.assert_called()


class SegmentTests(WorkspaceTests, unittest.TestCase):
    def setUp(self):
        self.setup_store()

    def test_cohorts_use_entry_snapshot_and_net_pnl(self):
        paper = Paper(self.store)
        scanner = Scanner(self.store, paper, None)
        early = normalize(market(pairCreatedAt=(NOW - 3600) * 1000), MINT, NOW)
        scanner.record([], [(early, Assessment("HIGH", {}, ()))], NOW)
        entry = paper.enter(early.quote, NOW)
        exit = paper.close(entry["id"], quote(".8", NOW + 1), NOW + 1)
        later = normalize(market(), MINT, NOW + 2)
        scanner.record([], [(later, Assessment("CLEAR", {}, ()))], NOW + 2)
        paper.enter(later.quote, NOW + 2)
        segments = paper.report(now=NOW + 2)["segments"]
        self.assertEqual(segments["pair_age_at_entry"]["1h_to_24h"]["closed"], 1)
        high = segments["risk_at_entry"]["HIGH"]
        self.assertEqual(high["losses"], 1)
        self.assertEqual(high["realized_pnl_cents"], exit["pnl_cents"])
        self.assertEqual(high["realized_return"], exit["pnl_cents"] / 202)
        self.assertEqual(segments["risk_at_entry"]["CLEAR"]["open"], 1)

    def test_missing_context_stays_unknown_after_late_observation(self):
        paper = Paper(self.store)
        paper.enter(quote(), NOW)
        candidate = normalize(market(), MINT, NOW)
        Scanner(self.store, paper, None).record([], [(candidate, Assessment("CLEAR", {}, ()))], NOW)
        self.assertEqual(paper.segments()["risk_at_entry"]["UNKNOWN"]["open"], 1)
        self.assertEqual(paper.segments()["pair_age_at_entry"]["unknown"]["open"], 1)


class LearningTests(unittest.TestCase):
    def test_proposals_require_independent_confirmation_and_minimum_labels(self):
        def sample(start, positive_score=100):
            return [{"observed": start + i, "score": positive_score if i < 5 else 70,
                     "outcome": {"at": start + i + 1, "positive": i < 5}}
                    for i in range(10)]

        train = sample(0)
        test = sample(20)
        report = analytics.review(train + test, 20)
        self.assertEqual(report["thresholds"][60]["test"]["fp"], 5)
        self.assertEqual(report["proposals"][0]["proposed"], 80)
        self.assertTrue(report["manual_review_required"])
        self.assertFalse(analytics.review(train + sample(20, 70), 20)["proposals"])
        self.assertFalse(analytics.review(train + test[:-1], 20)["proposals"])
        self.assertFalse(analytics.review(sample(10) + test, 20)["proposals"])
