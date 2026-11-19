"""Chronological counterfactual paper portfolio comparisons on recorded markets."""
from decimal import Decimal
from itertools import groupby
from pathlib import Path
from tempfile import TemporaryDirectory
import json

from .analytics import compare, labeled, replay
from .market import Quote
from .paper import Paper, Policy
from .storage import ROOT, Store


def frame_quote(row):
    data = json.loads(row["detail"])
    quote = Quote(row["mint"], row["pair"], data["quote"]["symbol"],
                  Decimal(row["price"]), row["observed"])
    return quote, (Decimal(data["liquidity"] or "0"), Decimal(data["volume"] or "0"),
                   row["score"], quote.pair)


def simulate(rows, threshold, directory, policy):
    paper = Paper(Store(directory / f"threshold-{threshold}.sqlite"), policy)
    count = 0
    for observed, frame in groupby(rows, key=lambda row: row["observed"]):
        count += 1
        quotes = {}
        ranked = {}
        for row in frame:
            quote, rank = frame_quote(row)
            quotes[quote.pair] = quote
            previous = ranked.get(quote.mint)
            if previous is None or rank > previous[0]:
                ranked[quote.mint] = (rank, quote)
        positions = paper.open_positions()
        for position in positions:
            paper.close(position["id"], quotes.get(position["pair"]), observed)
        active = {position["mint"] for position in positions}
        # Compare market-score hypotheses only. These counterfactual entries are
        # isolated from the operational scanner and its evidence-based entry gate.
        for rank, quote in sorted(ranked.values(), key=lambda item: (-item[0][2], item[1].mint)):
            if rank[2] >= threshold and quote.mint not in active:
                paper.enter(quote, observed)
    return dict(paper.report(), frames=count)


def compare_portfolios(store, split, thresholds=(60, 80, 100), policy=None):
    """Simulate predefined thresholds on a fresh account per window and strategy."""
    rows = replay(store)
    train = [row for row in rows if row["observed"] < split]
    test = [row for row in rows if row["observed"] >= split]
    policy = policy or Policy()
    result = {}
    with TemporaryDirectory(prefix=".test-backtest-", dir=ROOT) as work:
        for name, window in (("train", train), ("test", test)):
            directory = Path(work) / name
            result[name] = {threshold: simulate(window, threshold, directory, policy)
                            for threshold in dict.fromkeys(thresholds)}
    return {"split": split, "market_score_only": True, "portfolios": result,
            "score_metrics": compare(labeled(rows), split, thresholds)}
