"""Time-split evaluation of score signals on the observed pair universe."""
from bisect import bisect_left
from collections import defaultdict
from decimal import Decimal


def replay(store):
    return store.rows("""
        SELECT p.mint,o.* FROM observations o JOIN pairs p USING(pair)
        ORDER BY observed,pair""")


def labeled(observations, horizon=3600, tolerance=900, target=Decimal("0.10")):
    if horizon <= 0 or tolerance < 0:
        raise ValueError("Invalid outcome window")
    pairs = defaultdict(list)
    for row in observations:
        pairs[row["pair"]].append(row)
    result = []
    for series in pairs.values():
        series.sort(key=lambda row: row["observed"])
        times = [row["observed"] for row in series]
        for row in series:
            start = row["observed"]
            index = bisect_left(times, start + horizon)
            outcome = None
            if index < len(series) and times[index] <= start + horizon + tolerance:
                change = Decimal(series[index]["price"]) / Decimal(row["price"]) - 1
                outcome = {"at": times[index], "positive": change >= target, "return": str(change)}
            result.append(dict(row, outcome=outcome))
    return sorted(result, key=lambda row: (row["observed"], row["pair"]))


def metrics(rows, threshold):
    known = [row for row in rows if row["outcome"] is not None]
    tp = fp = tn = fn = 0
    for row in known:
        signal = row["score"] >= threshold
        positive = row["outcome"]["positive"]
        tp += signal and positive
        fp += signal and not positive
        tn += not signal and not positive
        fn += not signal and positive
    return {"total": len(rows), "unknown": len(rows)-len(known), "tp": tp, "fp": fp,
            "tn": tn, "fn": fn, "precision": tp / (tp + fp) if tp + fp else None,
            "recall": tp / (tp + fn) if tp + fn else None}


def compare(rows, split, thresholds=(60, 80, 100)):
    # No training label can mature on or after the start of the test period.
    train = [row for row in rows if row["observed"] < split
             and row["outcome"] is not None and row["outcome"]["at"] < split]
    test = [row for row in rows if row["observed"] >= split]
    calibration = {}
    for bucket in range(0, 101, 20):
        group = [row for row in train if row["score"] // 20 * 20 == bucket]
        calibration[bucket] = {
            "count": len(group),
            "positive_rate": sum(row["outcome"]["positive"] for row in group) / len(group)
            if group else None,
        }
    return {"split": split, "training_calibration": calibration,
            "thresholds": {n: {"train": metrics(train, n), "test": metrics(test, n)}
                           for n in thresholds}}


def rolling(rows, as_of, window=86400, threshold=60):
    cutoff = as_of - window
    visible = []
    for row in rows:
        if cutoff <= row["observed"] <= as_of:
            outcome = row["outcome"]
            visible.append(row if outcome and outcome["at"] <= as_of else dict(row, outcome=None))
    return metrics(visible, threshold)


def false_signals(rows, threshold=60):
    return [{"mint": row["mint"], "pair": row["pair"], "observed": row["observed"],
             "score": row["score"], "outcome": row["outcome"]}
            for row in rows if row["score"] >= threshold and row["outcome"]
            and not row["outcome"]["positive"]]


def review(rows, split):
    """Evaluate false positives and missed signals and suggest a manual threshold trial."""
    report = compare(rows, split)
    baseline = report["thresholds"][60]

    def improves(current, reference):
        # Require examples from both classes and a Pareto improvement on errors.
        positives = reference["tp"] + reference["fn"]
        negatives = reference["tn"] + reference["fp"]
        return (min(positives, negatives) >= 5
                and current["fp"] <= reference["fp"] and current["fn"] <= reference["fn"]
                and current["fp"] + current["fn"] < reference["fp"] + reference["fn"])

    training_choices = [(threshold, scores) for threshold, scores in report["thresholds"].items()
                        if threshold != 60 and improves(scores["train"], baseline["train"])]
    proposals = []
    if training_choices:
        threshold, scores = min(training_choices, key=lambda item:
                                item[1]["train"]["fp"] + item[1]["train"]["fn"])
        if improves(scores["test"], baseline["test"]):
            proposals.append({
                "action": "review_score_threshold", "current": 60, "proposed": threshold,
                "training_false_positives_avoided": baseline["train"]["fp"] - scores["train"]["fp"],
                "test_false_positives_avoided": baseline["test"]["fp"] - scores["test"]["fp"],
            })
    report["manual_review_required"] = True
    report["proposals"] = proposals
    return report
