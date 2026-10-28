"""Scorecard numbers from cig/runs.csv (+ scores.csv, errors.csv, nodes.csv, dossiers)."""
import csv
import json
import os
import re
from collections import Counter

LABELS = ("good", "ambiguous", "bad")
_LABEL_OF = {"1": "good", "0": "ambiguous", "-1": "bad"}


def median(xs):
    xs = sorted(xs)
    if not xs:
        return 0.0
    m = len(xs) // 2
    return float(xs[m]) if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2.0


def percentile(xs, q):
    xs = sorted(xs)
    if not xs:
        return 0.0
    i = (len(xs) - 1) * q
    lo = int(i)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (i - lo)


def num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def read_csv(path):
    if not os.path.isfile(path):
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def pct(n, d):
    return 100.0 * n / d if d else 0.0


def load_runs(out):
    """Runs from cig/runs.csv merged with scores.csv; derived flags recomputed if absent."""
    rows = read_csv(os.path.join(out, "cig", "runs.csv"))
    scores = {r["run_id"]: r for r in read_csv(os.path.join(out, "scores.csv"))}
    runs = []
    for r in rows:
        s = scores.get(r.get("run_id"), {})
        sc = (s.get("outcome_score") or r.get("outcome_score") or "").strip()
        try:
            sc = str(int(float(sc)))
        except ValueError:
            sc = ""
        label = _LABEL_OF.get(sc, "ungraded")
        tools = int(num(r.get("n_tool_calls")))
        fails = int(num(r.get("n_tool_failures")))
        tokens = num(r.get("tokens")) or num(r.get("input_tokens")) + num(r.get("output_tokens"))
        cost = r.get("cost_usd")
        runs.append({
            "run_id": r.get("run_id", ""), "conv": r.get("conversation_id", ""),
            "label": label, "tools": tools, "fails": fails, "tokens": tokens,
            "cost": num(cost) if str(cost or "").strip() != "" else None,
            "latency": num(r.get("latency_s"), None) if str(r.get("latency_s") or "").strip() != "" else None,
            "tool_fail": int(num(r.get("tool_fail"), 1 if fails else 0)),
            "unrecovered": int(num(r.get("unrecovered"))),
            "no_answer": int(num(r.get("no_answer"))),
            "slow": r.get("slow"), "heavy": r.get("heavy"),
            "persona": (s.get("persona") or r.get("persona") or "").strip(),
            "issue": (s.get("issue") or "").strip(),
            "workflow": r.get("workflow", ""),
        })
    if runs:
        if all(r["slow"] in (None, "") for r in runs):
            cut = percentile([r["latency"] for r in runs if r["latency"] is not None], 0.9)
            for r in runs:
                r["slow"] = 1 if r["latency"] is not None and r["latency"] > cut else 0
        if all(r["heavy"] in (None, "") for r in runs):
            cut = max(8, percentile([r["tools"] for r in runs], 0.9))
            for r in runs:
                r["heavy"] = 1 if r["tools"] >= cut else 0
    for r in runs:
        r["slow"] = int(num(r["slow"]))
        r["heavy"] = int(num(r["heavy"]))
    lat = [r["latency"] for r in runs if r["latency"] is not None]
    if not lat or percentile(lat, 0.9) <= median(lat):  # no slow tail: ties or missing data
        for r in runs:
            r["slow"] = 0
    return runs


def _safe_text(s):
    s = " ".join(str(s).split())
    if "@" in s or re.search(r"\S{25,}", s) or re.search(r"\d{6,}", s):
        return ""
    return s


def _ask_pattern(asks):
    """Most common ask (or its first words) if it repeats; '' when there is no safe summary."""
    asks = [_safe_text(a).lower() for a in asks]
    asks = [a for a in asks if a]
    for words in (None, 5):
        c = Counter(a if words is None else " ".join(a.split()[:words]) for a in asks)
        if c:
            text, n = c.most_common(1)[0]
            if n >= 2:
                return text if len(text) <= 70 else text[:67].rstrip() + "..."
    if asks:  # no repeated ask: show one representative (the median-length) ask as an example
        ex = sorted(asks, key=len)[len(asks) // 2]
        return 'e.g. "%s"' % (ex if len(ex) <= 60 else ex[:57].rstrip() + "...")
    return ""


def _asks(out):
    for p in (os.path.join(out, "cig", "run_dossiers.jsonl"),
              os.path.join(out, "normalized", "runs.jsonl")):
        if os.path.isfile(p):
            d = {}
            with open(p, encoding="utf-8") as f:
                for line in f:
                    try:
                        o = json.loads(line)
                    except ValueError:
                        continue
                    d[o.get("run_id")] = o.get("user_message") or ""
            return d
    return {}


def group_stats(rs):
    n = len(rs)
    costs = [r["cost"] for r in rs if r["cost"] is not None]
    lats = [r["latency"] for r in rs if r["latency"] is not None]
    return {
        "n": n,
        "tool_fail_pct": pct(sum(1 for r in rs if r["tool_fail"]), n),
        "unrecovered_pct": pct(sum(1 for r in rs if r["unrecovered"]), n),
        "no_answer_pct": pct(sum(1 for r in rs if r["no_answer"]), n),
        "median_tokens": median([r["tokens"] for r in rs]) if any(r["tokens"] for r in rs) else None,
        "median_cost": median(costs) if costs else None,
        "median_tools": median([r["tools"] for r in rs]),
        "median_latency": median(lats) if lats else None,
        "slow_pct": pct(sum(1 for r in rs if r["slow"]), n),
    }


def personas(runs, out, top=3):
    graded = [r for r in runs if r["label"] in LABELS]
    asks = _asks(out)
    by = {}
    for r in graded:
        by.setdefault(r["persona"] or "(unlabelled)", []).append(r)
    res = []
    for name, rs in sorted(by.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:top]:
        short = [r for r in rs if r["label"] != "good"]
        issues = Counter(r["issue"] for r in short if r["issue"])
        res.append({
            "persona": name, "n": len(rs),
            "goal": _ask_pattern([asks.get(r["run_id"], "") for r in rs]),
            "fell_short_pct": pct(len(short), len(rs)),
            "top_pain": issues.most_common(1)[0][0] if issues else "",
            "pain_n": issues.most_common(1)[0][1] if issues else 0,
        })
    return res


def compute(out):
    runs = load_runs(out)
    summ = read_json(os.path.join(out, "cig", "summary.json"))
    graded = [r for r in runs if r["label"] in LABELS]
    sc = {
        "runs": runs, "summary": summ, "n": len(runs), "graded": bool(graded),
        "n_conversations": len({r["conv"] for r in runs if r["conv"]}) or len(runs),
        "split": {l: sum(1 for r in runs if r["label"] == l) for l in LABELS},
        "ungraded": sum(1 for r in runs if r["label"] == "ungraded"),
        "all": group_stats(runs),
        "by": {l: group_stats([r for r in runs if r["label"] == l]) for l in LABELS},
        "has_cost": any(r["cost"] is not None for r in runs),
        "personas": personas(runs, out) if graded else [],
        "errors": read_csv(os.path.join(out, "cig", "errors.csv")),
        "nodes": read_csv(os.path.join(out, "cig", "nodes.csv")),
    }
    return sc
