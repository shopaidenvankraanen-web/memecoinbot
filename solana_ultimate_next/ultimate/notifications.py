"""Selective deduplicated notifications via an injected sender."""
import logging

CATEGORIES = ("new_token", "high_risk", "market_movement", "paper_trade")
LOG = logging.getLogger(__name__)


class Notifications:
    def __init__(self, store, recipients, send):
        self.store = store
        self.recipients = frozenset(recipients)
        self.send = send

    def settings(self):
        overrides = {row["key"]: bool(row["value"]) for row in
                     self.store.rows("SELECT * FROM settings WHERE key LIKE 'alert:%'")}
        return {category: overrides.get(f"alert:{category}", False) for category in CATEGORIES}

    def set(self, category, enabled):
        if category not in CATEGORIES:
            raise ValueError("Unknown notification type")
        with self.store.transaction(write=True) as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)",
                       (f"alert:{category}", int(enabled)))
        return self.settings()

    async def emit(self, category, key, message, now):
        if not self.settings().get(category):
            return
        key = f"{category}:{key}"
        for recipient in sorted(self.recipients):
            with self.store.transaction(write=True) as db:
                claimed = db.execute("INSERT OR IGNORE INTO notifications VALUES (?,?,?,?,?,'claimed')",
                                     (recipient, key, category, message[:3800], now)).rowcount
            if not claimed:
                continue
            try:
                await self.send(recipient, message[:3800])
                state = "sent"
            except Exception as error:
                LOG.warning("event=notification_failure type=%s", type(error).__name__)
                state = "failed"
            with self.store.transaction(write=True) as db:
                db.execute("UPDATE notifications SET state=? WHERE recipient=? AND key=?",
                           (state, recipient, key))
