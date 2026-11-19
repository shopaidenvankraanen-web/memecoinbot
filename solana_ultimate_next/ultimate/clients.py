"""Validated read-only provider calls through an explicitly supplied transport."""
import asyncio
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from math import isfinite

from .market import address

PROFILES = "https://api.dexscreener.com/token-profiles/latest/v1"
PAIRS = "https://api.dexscreener.com/token-pairs/v1/solana/"
PAIR_BY_ID = "https://api.dexscreener.com/latest/dex/pairs/solana/"
RPC = "https://api.mainnet-beta.solana.com"


@dataclass(frozen=True)
class Response:
    status: int
    payload: object
    retry_after: str | None = None


class ProviderError(RuntimeError):
    pass


def retry_delay(value, attempt):
    delay = 2 ** attempt
    if value:
        try:
            delay = float(value)
        except ValueError:
            try:
                timestamp = parsedate_to_datetime(value)
                delay = (timestamp - datetime.now(timezone.utc)).total_seconds()
            except (ValueError, TypeError, OverflowError):
                pass
    return 2 ** attempt if not isfinite(delay) else max(0, delay)


class PublicData:
    def __init__(self, transport, *, concurrency=5, timeout=10, attempts=3, sleep=asyncio.sleep):
        if concurrency < 1 or attempts < 1 or timeout <= 0:
            raise ValueError("Invalid HTTP limits")
        self.transport = transport
        self.semaphore = asyncio.Semaphore(concurrency)
        self.timeout = timeout
        self.attempts = attempts
        self.sleep = sleep
        self.metrics = Counter()

    async def _request(self, method, url, body, validate):
        for attempt in range(self.attempts):
            self.metrics["attempts"] += 1
            delay = 2 ** attempt
            try:
                async with self.semaphore:
                    response = await asyncio.wait_for(self.transport(method, url, body), self.timeout)
                status = response.status
                self.metrics[f"http_{status}"] += 1
                if status == 200:
                    if not validate(response.payload):
                        self.metrics["invalid_payloads"] += 1
                        raise ProviderError("Invalid provider payload")
                    self.metrics["successes"] += 1
                    return response.payload
                if status == 429:
                    delay = retry_delay(response.retry_after, attempt)
                    if delay > 60:
                        raise ProviderError("Provider retry window exceeds scan budget")
                elif status < 500:
                    raise ProviderError(f"HTTP {status}")
            except (TimeoutError, OSError) as error:
                self.metrics[type(error).__name__] += 1
            if attempt + 1 < self.attempts:
                self.metrics["retries"] += 1
                await self.sleep(delay)
        self.metrics["exhausted"] += 1
        raise ProviderError("Provider unavailable")

    async def profiles(self):
        return await self._request("GET", PROFILES, None, lambda body: isinstance(body, list))

    async def pairs(self, mint):
        if not address(mint):
            raise ValueError("Invalid mint")
        return await self._request("GET", PAIRS + mint, None, lambda body: isinstance(body, list))

    async def pair_by_id(self, pair):
        if not address(pair):
            raise ValueError("Invalid pair")
        result = await self._request(
            "GET", PAIR_BY_ID + pair, None, lambda body: isinstance(body, dict)
            and "pairs" in body and (body["pairs"] is None or isinstance(body["pairs"], list)))
        return result["pairs"] or []

    async def _wallet_rpc(self, wallet, method, options, validate):
        if not address(wallet):
            raise ValueError("Invalid public address")
        request = {"jsonrpc": "2.0", "id": 1, "method": method,
                   "params": [wallet, dict(options, commitment="confirmed")]}
        result = await self._request(
            "POST", RPC, request, lambda body: isinstance(body, dict)
            and type(body.get("id")) is int and body["id"] == 1 and "error" not in body
            and validate(body.get("result")))
        return result["result"]

    async def wallet_activity(self, wallet):
        return await self._wallet_rpc(
            wallet, "getSignaturesForAddress", {"limit": 30}, lambda result: isinstance(result, list))

    async def wallet_balance(self, wallet):
        result = await self._wallet_rpc(
            wallet, "getBalance", {}, lambda result: isinstance(result, dict)
            and type(result.get("value")) is int and 0 <= result["value"] < 2**64
            and isinstance(result.get("context"), dict)
            and type(result["context"].get("slot")) is int and result["context"]["slot"] >= 0)
        return {"lamports": result["value"], "slot": result["context"]["slot"]}


class RecordedTransport:
    """One captured frame. Strictly in-memory and never opens a socket."""

    def __init__(self, frame):
        self.frame = frame

    async def __call__(self, method, url, body):
        if (method, url) == ("GET", PROFILES):
            return Response(200, self.frame.get("profiles", []))
        if method == "GET" and url.startswith(PAIRS):
            return Response(200, self.frame.get("pairs", {}).get(url.removeprefix(PAIRS), []))
        if method == "GET" and url.startswith(PAIR_BY_ID):
            pair = url.removeprefix(PAIR_BY_ID)
            if address(pair) and pair in self.frame.get("pair_quotes", {}):
                return Response(200, self.frame["pair_quotes"][pair])
        if (method, url) == ("POST", RPC) and isinstance(body, dict):
            source = {"getSignaturesForAddress": "wallets", "getBalance": "wallet_balances"}.get(
                body.get("method"))
            params = body.get("params")
            if source and isinstance(params, list) and params and address(params[0]):
                records = self.frame.get(source, {})
                if params[0] in records:
                    return Response(200, {"id": 1, "result": records[params[0]]})
        return Response(404, None)
