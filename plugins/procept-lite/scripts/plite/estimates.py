"""The System 5 claims as four rows (goals, cost, errors, latency), each x now -> y with System 5.

Claims come from <out>/system5.json over templates/system5.json over DEFAULT_CLAIMS. Goals and cost apply the claimed
percentages as stated; errors and latency are "conservative": half of the improvement the runs themselves support.
x is always measured on the usable runs (see scorecard.compute); nothing here is a forecast model.
"""
import json
import math
import os

try:
    from .scorecard import median, num, pct, percentile
except ImportError:  # imported as a top-level module
    from scorecard import median, num, pct, percentile

DEFAULT_CLAIMS = {"url": "https://procept.tech/system5", "accuracy_gain_pct": 49, "goal_gain_pct": 28.5,
                  "cost_cut_pct": 50, "errors": "conservative", "latency": "conservative"}
CONSERVATIVE = 0.5  # share of the data-supported improvement that is claimed
TOP_CLASSES = 3     # the error classes that make up "the top error classes"
_TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "templates", "system5.json")


def _read(path):
    try:
        with open(path, encoding="utf-8-sig") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def load_claims(out):
    """Defaults, then templates/system5.json, then <out>/system5.json (later wins); validated."""
    c = dict(DEFAULT_CLAIMS)
    for d in (_read(_TEMPLATE), _read(os.path.join(out, "system5.json"))):
        c.update({k: v for k, v in d.items() if k in DEFAULT_CLAIMS})
    for k in ("accuracy_gain_pct", "goal_gain_pct", "cost_cut_pct"):
        v = c[k]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= 1000:
            raise ValueError("system5.json: %s must be a number between 0 and 1000" % k)
    if c["cost_cut_pct"] > 100:
        raise ValueError("system5.json: cost_cut_pct cannot be above 100")
    for k in ("errors", "latency"):
        if c[k] != "conservative":
            raise ValueError('system5.json: %s must be "conservative"' % k)
    if not (isinstance(c["url"], str) and c["url"].lower().startswith(("https://", "http://"))):
        raise ValueError("system5.json: url must start with https://")
    return c


def _change(x, y):
    """Relative change in percent (negative when y is below x); 0 when there is nothing to compare."""
    return (y - x) / x * 100.0 if x else 0.0


def goals(sc, claims):
    n = sc["n_graded"]
    if not n:
        raise ValueError("no graded runs, so there is no user-goal share to improve")
    x = pct(sc["split"]["good"], n)
    y = min(100.0, x * (1 + claims["goal_gain_pct"] / 100.0))
    return {"key": "goals", "unit": "pct", "x": x, "y": y, "delta_pct": _change(x, y),
            "good": sc["split"]["good"], "graded": n}


def cost(sc, claims):
    x = sc["all"]["cost_mean"]
    if x is None:
        raise ValueError("no cost, token or tool-call data in cig/runs.csv, so there is no cost per run")
    y = x * (1 - claims["cost_cut_pct"] / 100.0)
    return {"key": "cost", "unit": sc["unit"], "x": x, "y": y, "delta_pct": _change(x, y),
            "total": sc["all"]["cost_total"], "n": sc["all"]["n_cost"]}


def errors(sc):
    """Share of tool calls that failed. Half of the failures the top error classes account for would go away."""
    calls, fails = sc["all"]["calls"], sc["all"]["fails"]
    if not calls:
        raise ValueError("no tool calls in cig/runs.csv, so there is no error rate")
    top = sorted((num(e.get("count")) for e in sc["errors"]), reverse=True)[:TOP_CLASSES]
    supported = min(float(fails), sum(top))
    x, y = pct(fails, calls), pct(fails - CONSERVATIVE * supported, calls)
    return {"key": "errors", "unit": "pct", "x": x, "y": y, "delta_pct": _change(x, y),
            "calls": calls, "fails": fails, "supported": supported}


def latency(sc):
    """Slowest-10% run time (p90). It moves half of the way to what it would be if the runs slower than p90 took
    the median time."""
    lat = [r["latency"] for r in sc["runs"] if r["latency"] is not None]
    if not lat:
        raise ValueError("no latency in cig/runs.csv, so there is no run time to improve")
    med, p90 = median(lat), percentile(lat, 0.9)
    p90_full = percentile([min(v, med) if v > p90 else v for v in lat], 0.9)
    y = p90 - CONSERVATIVE * (p90 - p90_full)
    return {"key": "latency", "unit": "s", "x": p90, "y": y, "delta_pct": _change(p90, y),
            "median": med, "p90_full": p90_full}


def system5(sc, claims):
    """[goals, cost, errors, latency]: dicts with key, unit (pct|usd|tokens|calls|s), x, y and delta_pct."""
    return [goals(sc, claims), cost(sc, claims), errors(sc), latency(sc)]
