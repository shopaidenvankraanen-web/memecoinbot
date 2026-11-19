"""Explicit, foreground, read-only requests to the public DexScreener API."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from http.client import HTTPException, HTTPSConnection
import json
import time

from .clients import PAIR_BY_ID, PAIRS, PROFILES, RPC, ProviderError, PublicData, Response, retry_delay
from .market import address, normalize, number, pair_rank
from .risk import assess

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
