"""Bounds (never forecasts) for what could be reached, derived from the runs themselves."""
from collections import Counter

try:
    from .scorecard import median, num, percentile, pct
except ImportError:  # imported as a top-level module
    from scorecard import median, num, percentile, pct


def goals(sc):
    runs, n = sc["runs"], sc["n"]
    if not sc["graded"] or not n:
        return None
    good = sc["split"]["good"]
    short = [r for r in runs if r["label"] in ("ambiguous", "bad")]
    addr = [r for r in short if r["tool_fail"] or r["unrecovered"] or r["no_answer"]
            or r["heavy"] or r["slow"]]
    now = pct(good, n)
    return {"now": now, "ceiling": pct(good + len(addr), n),
            "half": pct(good + len(addr) / 2.0, n),
            "addressable": len(addr), "short": len(short)}


def cost(sc):
    runs = sc["runs"]
    total = sum(r["tokens"] for r in runs)
    if not runs or total <= 0:
        return None
    med = median([r["tokens"] for r in runs])
    short = [r for r in runs if r["label"] in ("ambiguous", "bad")]
    lev_short = sum(r["tokens"] for r in short)
    lev_heavy = sum(max(0.0, r["tokens"] - med) for r in runs if r["heavy"])
    # tokens spent on calls after a failure: failed-call share of the failing run's tokens
    lev_retry = sum(r["tokens"] * r["fails"] / float(max(r["tools"], 1))
                    for r in runs if r["fails"])
    # per-run union so overlapping levers are not double counted
    union = 0.0
    for r in runs:
        mine = max(max(0.0, r["tokens"] - med) if r["heavy"] else 0.0,
                   r["tokens"] * r["fails"] / float(max(r["tools"], 1)) if r["fails"] else 0.0)
        union += r["tokens"] if r["label"] in ("ambiguous", "bad") else mine
    cost_total = sum(r["cost"] for r in runs if r["cost"] is not None) if sc["has_cost"] else None
    p = lambda x: pct(x, total)
    return {"total": total, "med": med, "short_pct": p(lev_short), "heavy_pct": p(lev_heavy),
            "retry_pct": p(lev_retry), "union_pct": p(union),
            "supported": p(union) >= 50.0, "gap": abs(p(union) - 50.0),
            "cost_total": cost_total, "graded": sc["graded"]}


def errors(sc):
    runs = sc["runs"]
    calls = sum(r["tools"] for r in runs)
    fails = sum(r["fails"] for r in runs)
    if not calls:
        return None
    classes = sorted(sc["errors"], key=lambda e: -num(e.get("count")))
    top = classes[:3]
    removed = sum(num(e.get("count")) for e in top)
    after = max(0.0, fails - removed)
    return {"calls": calls, "fails": fails, "now": pct(fails, calls),
            "after": pct(after, calls) if classes else None,
            "top": [(e.get("error_class", ""), int(num(e.get("count")))) for e in top]}


def latency(sc):
    pairs = [(r["latency"], r) for r in sc["runs"] if r["latency"] is not None]
    lat = [x for x, _ in pairs]
    if not lat or max(lat) <= 0:
        return None
    med, p90 = median(lat), percentile(lat, 0.9)
    n_slow = sum(r["slow"] for _, r in pairs)
    capped = [min(x, med) if r["slow"] else x for x, r in pairs]
    slow_time = sum(x for x, r in pairs if r["slow"])
    return {"med": med, "p90": p90, "med_after": median(capped),
            "p90_after": percentile(capped, 0.9), "n_slow": n_slow,
            "tail": n_slow > 0 and p90 > med, "slow_share": pct(slow_time, sum(lat))}


def fixes(sc, limit=6):
    """Rule-derived fix bullets: (text, runs_touched), most runs first."""
    runs, out = sc["runs"], []
    nodes = [x for x in sc["nodes"] if num(x.get("fail_calls")) > 0]
    if nodes:
        t = max(nodes, key=lambda x: num(x.get("fail_calls")))
        name = t.get("node_id", "").replace("tool:", "", 1)
        cls = next((e.get("error_class") for e in sc["errors"] if e.get("tool") == name), None)
        if not cls and sc["errors"]:
            cls = sc["errors"][0].get("error_class")
        touched = sum(1 for r in runs if r["tool_fail"] and name in r["workflow"].split(" > "))
        out.append(("Validate and constrain the input to \"%s\", the most failing tool; %d%% of its calls fail%s"
                    % (name, round(100 * num(t.get("fail_rate"))),
                       ", mostly %s" % cls if cls else ""), touched or sum(r["tool_fail"] for r in runs)))
    na = sum(r["no_answer"] for r in runs)
    if na:
        out.append(("Always end with an answer, or say what is missing", na))
    retry = sum(1 for r in runs if r["fails"] >= 3)
    if retry:
        out.append(("Stop after 2 identical failures instead of retrying", retry))
    heavy = sum(r["heavy"] for r in runs)
    if heavy:
        out.append(("Cap tool calls per run and plan first", heavy))
    slow = sum(r["slow"] for r in runs)
    lat = latency(sc)
    if slow and lat and lat["tail"]:
        out.append(("Add timeouts and smaller scopes for the slow tail", slow))
    issues = Counter(r["issue"] for r in runs if r["issue"] and r["label"] != "good")
    for label, k in issues.most_common(2):
        out.append(("Targeted fix for the recurring issue \"%s\"" % label, k))
    out.sort(key=lambda x: -x[1])
    return out[:limit]
