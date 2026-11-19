"""Wallet observations have unknown attribution and do not establish intent."""
from decimal import Decimal

from .clients import ProviderError
from .market import address, mapping


class Wallets:
    def __init__(self, store, client):
        self.store = store
        self.client = client

    def watch(self, wallet, label="Monitored"):
        if not address(wallet):
            raise ValueError("Invalid public wallet address")
        with self.store.transaction(write=True) as db:
            db.execute("INSERT OR REPLACE INTO watch VALUES (?,?)", (wallet, label[:100]))
        return {"address": wallet, "label": label[:100], "attribution": "UNKNOWN"}

    def unwatch(self, wallet):
        with self.store.transaction(write=True) as db:
            return bool(db.execute("DELETE FROM watch WHERE address=?", (wallet,)).rowcount)

    def list(self):
        return [dict(row, attribution="UNKNOWN") for row in
                self.store.rows("SELECT * FROM watch ORDER BY address")]

    async def activity(self, wallet):
        try:
            balance = await self.client.wallet_balance(wallet)
        except ProviderError:
            balance = None
        try:
            payload = await self.client.wallet_activity(wallet)
        except ProviderError:
            if balance is None:
                raise
            payload = None
        rows = {}
        for raw in (payload or [])[:30]:
            item = mapping(raw)
            signature = item.get("signature")
            if isinstance(signature, str) and 1 <= len(signature) <= 100 and "err" in item:
                timestamp = item.get("blockTime")
                rows[signature] = (timestamp if type(timestamp) is int and 0 <= timestamp < 2**63
                                   else None, int(item["err"] is None))
        with self.store.transaction(write=True) as db:
            db.executemany("INSERT OR IGNORE INTO activity VALUES (?,?,?,?)",
                           [(wallet, signature, *data) for signature, data in rows.items()])
        failed = sum(not success for _, success in rows.values())
        if balance is not None:
            balance["sol"] = format(Decimal(balance["lamports"]).scaleb(-9), "f")
        return {"address": wallet, "sample": len(rows) if payload is not None else None,
                "failed": failed if payload is not None else None,
                "failure_rate": failed / len(rows) if rows else None, "balance": balance,
                "attribution": "UNKNOWN", "confidence": "LIMITED_SAMPLE"}
