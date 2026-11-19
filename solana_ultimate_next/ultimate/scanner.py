"""Discovery, risk assessment, persistence, and paper execution orchestration."""
import asyncio
from dataclasses import asdict
import json
import logging
import time

from .clients import ProviderError
from .live import valid_record
from .market import address, mapping, normalize, pair_rank
from .risk import assess

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
