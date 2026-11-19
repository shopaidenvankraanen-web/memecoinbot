"""Atomic virtual EUR ledger priced using fixed-FX USD price ratios."""
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from enum import StrEnum
import json

from .market import Quote


def rounded(value):
    return int(value.to_integral_value(rounding=ROUND_HALF_UP))


@dataclass(frozen=True)
class Policy:
    stake: int = 200
    positions: int = 3
    fee: Decimal = Decimal("0.01")
    slip: Decimal = Decimal("0.005")
    stop: Decimal = Decimal("0.15")
    take: Decimal = Decimal("0.25")
    duration: int = 21600
    quote_age: int = 120

    def __post_init__(self):
        if (type(self.stake) is not int or not 1 <= self.stake <= 400
                or any(type(n) is not int or n < 1 for n in
                       (self.positions, self.duration, self.quote_age))
                or any(not isinstance(n, Decimal) or not n.is_finite() or not 0 <= n < 1
                       for n in (self.fee, self.slip, self.stop, self.take))
                or not self.stop or not self.take):
            raise ValueError("Invalid paper policy")


class Exit(StrEnum):
    STOP = "stop_loss"
    TAKE = "take_profit"
    AGE = "age_limit"
    MANUAL = "manual"


class Paper:
    def __init__(self, store, policy=None):
        self.store = store
        self.policy = policy or Policy()

    def open_positions(self):
        return self.store.rows("SELECT * FROM positions WHERE closed IS NULL ORDER BY id")

    def enter(self, quote, now, stake=None, *, automatic=False, assessment=None):
        policy = self.policy
        stake = policy.stake if stake is None else stake
        if (type(stake) is not int or not 1 <= stake <= 400
                or not quote.fresh(now, policy.quote_age)):
            return None
        if automatic and (assessment is None or not assessment.allows_automatic_entry):
            return None
        fee = rounded(stake * policy.fee)
        cost = stake + fee
        with self.store.transaction(write=True) as db:
            if automatic and db.execute(
                    "SELECT 1 FROM settings WHERE key='paused' AND value=1").fetchone():
                return None
            count, duplicate = db.execute(
                "SELECT COUNT(*), MAX(mint=?) FROM positions WHERE closed IS NULL",
                (quote.mint,)).fetchone()
            if count >= policy.positions or duplicate:
                return None
            if db.execute("UPDATE account SET cents=cents-? WHERE id=1 AND cents>=?",
                          (cost, cost)).rowcount != 1:
                return None
            observation = db.execute("""
                SELECT o.detail, o.risk FROM observations o JOIN pairs p USING(pair)
                WHERE o.pair=? AND o.observed=? AND p.mint=?""",
                (quote.pair, quote.observed, quote.mint)).fetchone()
            created = json.loads(observation["detail"]).get("created") if observation else None
            pair_age = now - created if type(created) is int and 0 < created <= now else None
            risk = observation["risk"] if observation else None
            if risk not in ("CLEAR", "ELEVATED", "HIGH", "UNKNOWN"):
                risk = None
            cursor = db.execute("""
                INSERT INTO positions
                (mint,pair,symbol,entry,opened,entry_quote,stake,entry_fee,fee,slip,stop,take,duration,
                 entry_pair_age,entry_risk)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (quote.mint, quote.pair, quote.symbol, str(quote.price), now, quote.observed,
                 stake, fee, str(policy.fee), str(policy.slip), str(policy.stop),
                 str(policy.take), policy.duration, pair_age, risk))
            trade_id = cursor.lastrowid
            reason = "automatic" if automatic else "manual"
            db.execute("INSERT INTO audit VALUES (?, 'entry', ?, ?, ?)",
                       (trade_id, now, -cost, reason))
            return {"id": trade_id, "kind": "entry", "cash_delta_cents": -cost, "reason": reason}

    def close(self, trade_id, quote, now, *, manual=False):
        if quote is None or not quote.fresh(now, self.policy.quote_age):
            return None
        with self.store.transaction(write=True) as db:
            position = db.execute("SELECT * FROM positions WHERE id=? AND closed IS NULL",
                                  (trade_id,)).fetchone()
            if (position is None or (position["mint"], position["pair"]) != (quote.mint, quote.pair)
                    or quote.observed <= position["entry_quote"] or quote.observed < position["opened"]):
                return None
            ratio = quote.price / Decimal(position["entry"])
            reason = Exit.MANUAL if manual else self.exit_reason(position, ratio, quote.observed)
            if reason is None:
                return None
            settlement = self.settlement(position, ratio)
            if settlement is None:
                return None
            proceeds, exit_fee = settlement
            db.execute("""
                UPDATE positions SET closed=?, exit=?, proceeds=?, exit_fee=?, reason=?
                WHERE id=?""", (now, str(quote.price), proceeds, exit_fee, reason, trade_id))
            db.execute("UPDATE account SET cents=cents+? WHERE id=1", (proceeds,))
            db.execute("INSERT INTO audit VALUES (?, 'exit', ?, ?, ?)",
                       (trade_id, now, proceeds, reason))
            return {"id": trade_id, "kind": "exit", "cash_delta_cents": proceeds,
                    "pnl_cents": proceeds - position["stake"] - position["entry_fee"],
                    "reason": reason}

    @staticmethod
    def settlement(position, ratio):
        slip = Decimal(position["slip"])
        gross = position["stake"] * ratio * (1 - slip) / (1 + slip)
        if gross > Decimal("1000000000000"):
            return None
        fee = rounded(gross * Decimal(position["fee"]))
        return rounded(gross) - fee, fee

    @staticmethod
    def exit_reason(position, ratio, observed):
        if ratio <= 1 - Decimal(position["stop"]):
            return Exit.STOP
        if ratio >= 1 + Decimal(position["take"]):
            return Exit.TAKE
        if observed - position["opened"] >= position["duration"]:
            return Exit.AGE
        return None

    def report(self, *, now=None):
        with self.store.transaction() as db:
            cents = db.execute("SELECT cents FROM account").fetchone()[0]
            rows = db.execute("SELECT * FROM positions ORDER BY closed,id").fetchall()
            valuation = self.valuation(db, now, cents) if now is not None else None
        closed = [row for row in rows if row["closed"] is not None]
        pnl = [row["proceeds"] - row["stake"] - row["entry_fee"] for row in closed]
        wins = [value for value in pnl if value > 0]
        losses = [-value for value in pnl if value < 0]
        equity = peak = 2000
        drawdown = 0
        win_streak = loss_streak = best_win = best_loss = 0
        for value in pnl:
            equity += value
            peak = max(equity, peak)
            drawdown = max(drawdown, (peak - equity) / peak)
            win_streak = win_streak + 1 if value > 0 else 0
            loss_streak = loss_streak + 1 if value < 0 else 0
            best_win = max(best_win, win_streak)
            best_loss = max(best_loss, loss_streak)
        result = {
            "starting_cents": 2000, "cash_cents": cents, "open": len(rows)-len(closed),
            "closed": len(closed), "realized_pnl_cents": sum(pnl),
            "fees_cents": sum(row["entry_fee"] + (row["exit_fee"] or 0) for row in rows),
            "win_rate": len(wins) / len(pnl) if pnl else None,
            "profit_factor": sum(wins) / sum(losses) if losses else None,
            "average_pnl_cents": sum(pnl) / len(pnl) if pnl else None,
            "average_win_cents": sum(wins) / len(wins) if wins else None,
            "average_loss_cents": sum(losses) / len(losses) if losses else None,
            "realized_drawdown": drawdown, "longest_win_streak": best_win,
            "longest_loss_streak": best_loss,
        }
        if valuation is not None:
            def extreme(index):
                row = closed[index]
                return {"id": row["id"], "pnl_cents": pnl[index]}

            cost = sum(row["stake"] + row["entry_fee"] for row in closed)
            result.update(
                wins=len(wins), losses=len(losses), breakeven=len(pnl) - len(wins) - len(losses),
                realized_return_on_closed_cost=sum(pnl) / cost if cost else None,
                average_holding_seconds=(sum(row["closed"] - row["opened"] for row in closed)
                                         / len(closed) if closed else None),
                best_trade=extreme(max(range(len(pnl)), key=pnl.__getitem__)) if pnl else None,
                worst_trade=extreme(min(range(len(pnl)), key=pnl.__getitem__)) if pnl else None,
                exit_reasons=dict(Counter(row["reason"] for row in closed)),
                valuation=valuation,
                segments=self.segment_rows(rows),
            )
        return result

    def segments(self):
        return self.segment_rows(self.store.rows("SELECT * FROM positions"))

    @staticmethod
    def segment_rows(rows):
        groups = {"pair_age_at_entry": {}, "risk_at_entry": {}}
        for row in rows:
            age = row["entry_pair_age"]
            bucket = "unknown"
            if age is not None:
                bucket = next(label for upper, label in (
                    (3600, "under_1h"), (86400, "1h_to_24h"), (604800, "1d_to_7d"),
                    (float("inf"), "7d_or_more"),
                ) if age < upper)
            for dimension, label in (("pair_age_at_entry", bucket),
                                     ("risk_at_entry", row["entry_risk"] or "UNKNOWN")):
                group = groups[dimension].setdefault(label, {
                    "open": 0, "closed": 0, "wins": 0, "losses": 0, "breakeven": 0,
                    "realized_pnl_cents": 0, "closed_cost_cents": 0,
                })
                if row["closed"] is None:
                    group["open"] += 1
                    continue
                cost = row["stake"] + row["entry_fee"]
                pnl = row["proceeds"] - cost
                group["closed"] += 1
                group["closed_cost_cents"] += cost
                group["realized_pnl_cents"] += pnl
                group["wins" if pnl > 0 else "losses" if pnl < 0 else "breakeven"] += 1
        for dimension in groups.values():
            for group in dimension.values():
                cost = group["closed_cost_cents"]
                group["realized_return"] = group["realized_pnl_cents"] / cost if cost else None
        return groups

    def valuation(self, db, now, cash):
        positions = db.execute("""
            SELECT p.*, o.observed AS mark_observed, o.price AS mark_price
            FROM positions p LEFT JOIN observations o ON o.pair=p.pair AND o.observed=(
                SELECT MAX(x.observed) FROM observations x WHERE x.pair=p.pair
                AND x.observed BETWEEN MAX(p.opened, ?) AND ?)
            WHERE p.closed IS NULL
            """, (now - self.policy.quote_age, now)).fetchall()
        marked = value = cost = marked_cost = 0
        for position in positions:
            entry_cost = position["stake"] + position["entry_fee"]
            cost += entry_cost
            if position["mark_price"] is None:
                continue
            quote = Quote(position["mint"], position["pair"], position["symbol"],
                          Decimal(position["mark_price"]), position["mark_observed"])
            if not quote.fresh(now, self.policy.quote_age):
                continue
            settlement = self.settlement(position, quote.price / Decimal(position["entry"]))
            if settlement is not None:
                marked += 1
                value += settlement[0]
                marked_cost += entry_cost
        missing = len(positions) - marked
        return {
            "as_of": now, "open_cost_cents": cost, "marked_positions": marked,
            "unpriced_positions": missing, "marked_net_value_cents": value,
            "marked_unrealized_pnl_cents": value - marked_cost,
            "estimated_equity_cents": None if missing else cash + value,
        }
