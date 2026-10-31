"""Scorecard numbers from cig/runs.csv (+ scores.csv, errors.csv).

Only usable runs count (checks.unusable_ids). Outcome splits only cover graded runs; the headline metrics
(error rate, latency, cost) cover every usable run. Pure arithmetic: no wording lives here.
"""
import csv
import json
import math
import os
import re

try:
    from . import checks
except ImportError:  # imported as a top-level module
    import checks

LABELS = ("good", "ambiguous", "bad")
_LABEL_OF = {1: "good", 0: "ambiguous", -1: "bad"}
MIN_COVERAGE = 0.5  # share of runs that must carry dollars (or tokens) for that to be the cost unit
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def num(v, default=0.0):
    """A finite float, else `default` (blank, text, nan and inf all fall back)."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


def median(xs):
    xs = sorted(xs)
    if not xs:
        return 0.0
    m = len(xs) // 2
    return float(xs[m]) if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2.0


def percentile(xs, q):
    """Linear interpolation between closest ranks (the usual spreadsheet definition)."""
    xs = sorted(xs)
    if not xs:
        return 0.0
    i = (len(xs) - 1) * q
    lo = int(i)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (i - lo)


def pct(n, d):
    return 100.0 * n / d if d else 0.0


def read_csv(path):
    if not os.path.isfile(path):
        return []
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def read_json(path):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _score(*vals):
    """First of `vals` that is a valid outcome score (1, 0, -1), else None."""
    for v in vals:
        try:
            s = int(round(float(v)))
        except (TypeError, ValueError):
            continue
        if s in _LABEL_OF:
            return s
    return None


def load_runs(out):
    """Usable runs of cig/runs.csv with their outcome label (scores.csv wins over the runs.csv column)."""
    skip = checks.unusable_ids(out)
    scores = {(r.get("run_id") or "").strip(): r for r in read_csv(os.path.join(out, "scores.csv"))}
    runs, seen = [], set()
    for r in read_csv(os.path.join(out, "cig", "runs.csv")):
        rid = (r.get("run_id") or "").strip()
        if not rid or rid in skip or rid in seen:
            continue
        seen.add(rid)
        s = _score((scores.get(rid) or {}).get("outcome_score"), r.get("outcome_score"))
        tools = max(0, int(num(r.get("n_tool_calls"))))
        cost, lat = num(r.get("cost_usd"), None), num(r.get("latency_s"), None)
        runs.append({
            "run_id": rid, "label": _LABEL_OF.get(s, "ungraded"), "tools": tools,
            "fails": min(tools, max(0, int(num(r.get("n_tool_failures"))))),
            "tokens": max(0.0, num(r.get("tokens")) or num(r.get("input_tokens")) + num(r.get("output_tokens"))),
            "cost": cost if cost is not None and cost >= 0 else None,
            "latency": lat if lat is not None and lat >= 0 else None,
            "first_ts": (r.get("first_ts") or "").strip(),
        })
    return runs


def cost_unit(runs):
    """Dollars when most runs carry a cost, else tokens when most carry them, else tool calls."""
    n = len(runs)
    costs = [r["cost"] for r in runs if r["cost"] is not None]
    if n and len(costs) >= MIN_COVERAGE * n and sum(costs) > 0:
        return "usd"
    if n and sum(1 for r in runs if r["tokens"] > 0) >= MIN_COVERAGE * n:
        return "tokens"
    return "calls"


def run_cost(r, unit):
    if unit == "usd":
        return r["cost"]
    if unit == "tokens":
        return r["tokens"] if r["tokens"] > 0 else None
    return float(r["tools"])


def group_stats(rs, unit):
    calls = sum(r["tools"] for r in rs)
    fails = sum(r["fails"] for r in rs)
    lats = [r["latency"] for r in rs if r["latency"] is not None]
    costs = [c for c in (run_cost(r, unit) for r in rs) if c is not None]
    return {
        "n": len(rs), "calls": calls, "fails": fails,
        "error_pct": pct(fails, calls) if calls else None,
        "latency_median": median(lats) if lats else None,
        "latency_p90": percentile(lats, 0.9) if lats else None,
        "cost_mean": sum(costs) / len(costs) if costs else None,
        "cost_total": sum(costs), "n_cost": len(costs),
    }


def compute(out):
    runs = load_runs(out)
    graded = [r for r in runs if r["label"] in LABELS]
    unit = cost_unit(runs)
    days = sorted(r["first_ts"][:10] for r in graded if _DATE.match(r["first_ts"]))
    return {
        "runs": runs, "n": len(runs), "n_graded": len(graded), "unit": unit,
        "split": {l: sum(1 for r in graded if r["label"] == l) for l in LABELS},
        "all": group_stats(runs, unit),
        "by": {l: group_stats([r for r in graded if r["label"] == l], unit) for l in LABELS},
        "errors": read_csv(os.path.join(out, "cig", "errors.csv")),
        "dates": (days[0], days[-1]) if days else None,
    }
