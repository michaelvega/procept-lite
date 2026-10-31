"""Build the CIG (graph CSVs, flow_graph.json, run dossiers, summary.json) from normalized runs.

The unit of analysis is the RUN. Nothing is aggregated before each run has its own
row; node/edge/conversation/summary numbers are all derived from the per-run rows.
Needs the ingest and merge sign-offs; runs in the unusable ledger are left out of every output.
Standard library only.
"""
import argparse
import csv
import json
import math
import os
import re
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import checks  # noqa: E402

LABELS = {1: "good", 0: "ambiguous", -1: "bad"}
DIMENSIONS = ["errors", "cost", "latency", "score"]
START_ID = "sentinel:START"
ANSWER_ID = "sentinel:ANSWER"
NOANS_ID = "sentinel:NO_ANSWER"
SENTINELS = {START_ID: ("START", "start"), ANSWER_ID: ("ANSWER", "answer"),
             NOANS_ID: ("NO ANSWER", "no_answer")}

RUN_COLS = ["run_id", "conversation_id", "agent", "environment", "first_ts", "latency_s",
            "input_tokens", "output_tokens", "tokens", "cost_usd", "n_tool_calls",
            "n_tool_failures", "tool_fail", "unrecovered", "no_answer", "slow", "heavy",
            "outcome_score", "label", "persona", "workflow"]
NODE_COLS = ["node_id", "kind", "calls", "runs", "fail_calls", "fail_rate", "median_duration_s",
             "p90_duration_s", "mean_run_tokens", "share_bad_runs", "share_good_runs",
             "mean_outcome"]
EDGE_COLS = ["source", "target", "count", "runs", "fail_rate_target", "mean_outcome"]
CONV_COLS = ["conversation_id", "n_runs", "n_graded", "first_ts", "latency_s", "tokens",
             "n_tool_fail_runs", "n_good", "n_ambiguous", "n_bad", "mean_outcome",
             "worst_outcome"]
ERR_COLS = ["error_class", "count", "runs", "tool", "example"]


# ---------- small numeric helpers ----------
def num(x, default=0.0):
    try:
        v = float(x)
        return default if (math.isnan(v) or math.isinf(v)) else v
    except (TypeError, ValueError):
        return default


def pctl(vals, q):
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    pos = (len(vals) - 1) * q
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(vals) - 1)
    return vals[lo] + (vals[hi] - vals[lo]) * (pos - lo)


def median(vals):
    return pctl(vals, 0.5)


def mean(vals):
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


def rnd(v, nd=4):
    return None if v is None else round(v, nd)


def cell(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, float):
        v = round(v, 4)
        return int(v) if v == int(v) and abs(v) < 1e15 else v
    return v


def write_csv(path, cols, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(cols)
        for r in rows:
            w.writerow([cell(r.get(c)) for c in cols])


# ---------- error normalisation ----------
_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
_HEX = re.compile(r"\b(?:0x)?[0-9a-f]{8,}\b", re.I)
_NUM = re.compile(r"\d+(?:[.,]\d+)*")
_QUOTED = re.compile(r"(['\"`])[^'\"`]{0,80}\1")


def error_class(text):
    t = str(text or "").strip().lower()
    t = _UUID.sub(" ", t)
    t = _HEX.sub(" ", t)
    t = _QUOTED.sub(" ", t)
    t = _NUM.sub(" ", t)
    words = re.findall(r"[a-z_][a-z_\-]*", t)
    return " ".join(words[:6]) or "unknown error"


# ---------- per-run derivations ----------
def tool_failed(tc):
    if tc.get("success") is not False:
        return False
    return "cancel" not in str(tc.get("error") or "").lower()


def tool_name(tc):
    return str(tc.get("name") or "unknown")


def load_jsonl(path):
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    return out


def load_scores(path):
    scores = {}
    if not path or not os.path.exists(path):
        return scores
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rid = (row.get("run_id") or "").strip()
            try:
                s = int(round(float(row.get("outcome_score"))))
            except (TypeError, ValueError):
                continue
            if rid and s in LABELS:
                scores[rid] = row
    return scores


def prepare(runs, scores):
    """Return per-run records: the input dossier plus derived fields. Deterministic order."""
    recs = []
    for r in runs:
        r = dict(r)
        rid = str(r.get("run_id"))
        r["run_id"] = rid
        r["conversation_id"] = str(r.get("conversation_id") or rid)
        tcs = [tc for tc in (r.get("tool_calls") or []) if isinstance(tc, dict)]
        r["tool_calls"] = tcs
        fails = [tc for tc in tcs if tool_failed(tc)]
        r["n_tool_failures"] = len(fails)
        r["tool_fail"] = int(bool(fails))
        ans = r.get("final_answer")
        r["no_answer"] = int(bool(r.get("ends_without_answer")) or not str(ans or "").strip())
        r["unrecovered"] = int(bool(fails) and (bool(r["no_answer"]) or tool_failed(tcs[-1])))
        r["input_tokens"] = int(num(r.get("input_tokens")))
        r["output_tokens"] = int(num(r.get("output_tokens")))
        r["tokens"] = r["input_tokens"] + r["output_tokens"]
        r["cost_usd"] = None if r.get("cost_usd") is None else num(r.get("cost_usd"), None)
        r["latency_s"] = None if r.get("latency_s") in (None, "") else num(r.get("latency_s"))
        r["n_tool_calls"] = len(tcs)
        sc = scores.get(rid)
        if sc is not None:
            r["outcome_score"] = int(round(float(sc["outcome_score"])))
            r["label"] = LABELS[r["outcome_score"]]
            for k in ("confidence", "rationale", "key_quote", "persona", "issue", "reaction_run_id"):
                r[k] = sc.get(k) or ""
        else:
            r["outcome_score"] = None
            r["label"] = "ungraded"
            for k in ("confidence", "rationale", "key_quote", "persona", "issue", "reaction_run_id"):
                r[k] = ""
        seen, wf = set(), []
        for tc in tcs:
            n = tool_name(tc)
            if n not in seen:
                seen.add(n)
                wf.append(n)
        r["workflow"] = " > ".join(wf)
        recs.append(r)
    recs.sort(key=lambda x: (str(x.get("first_ts") or ""), x["run_id"]))
    # slow = slowest decile (needs >= 10 runs); heavy = top-decile tool-call count and >= 8
    lat = [r["latency_s"] for r in recs if r["latency_s"] is not None]
    thr = pctl(lat, 0.9) if len(lat) >= 10 else None
    ncs = [r["n_tool_calls"] for r in recs]
    p90 = pctl(ncs, 0.9)
    for r in recs:
        r["slow"] = int(thr is not None and r["latency_s"] is not None and r["latency_s"] > thr)
        r["heavy"] = int(r["n_tool_calls"] >= 8 and p90 is not None and r["n_tool_calls"] >= p90)
    return recs


# ---------- graph ----------
def build_graph(recs):
    """Nodes (tools + START/ANSWER/NO_ANSWER sentinels) and consecutive-transition edges."""
    n_calls = defaultdict(list)    # id -> [(duration, failed)]
    n_runs = defaultdict(set)
    e_cnt = Counter()
    e_runs = defaultdict(set)
    e_tfail = Counter()
    e_tcalls = Counter()
    e_dur = defaultdict(list)
    for i, r in enumerate(recs):
        seq = [(START_ID, None)]
        for tc in r["tool_calls"]:
            seq.append(("tool:" + tool_name(tc), tc))
        seq.append((NOANS_ID if r["no_answer"] else ANSWER_ID, None))
        for nid, tc in seq:
            n_runs[nid].add(i)
            if tc is not None:
                d = tc.get("duration_s")
                n_calls[nid].append((None if d is None else num(d), tool_failed(tc)))
        for (a, _), (b, tcb) in zip(seq, seq[1:]):
            e_cnt[(a, b)] += 1
            e_runs[(a, b)].add(i)
            if tcb is not None:
                e_tcalls[(a, b)] += 1
                e_tfail[(a, b)] += int(tool_failed(tcb))
                if tcb.get("duration_s") is not None:
                    e_dur[(a, b)].append(num(tcb.get("duration_s")))

    def outcome_stats(idxs):
        outs = [recs[i]["outcome_score"] for i in idxs if recs[i]["outcome_score"] is not None]
        if not outs:
            return None, None, None
        return (mean(outs), sum(1 for o in outs if o == -1) / len(outs),
                sum(1 for o in outs if o == 1) / len(outs))

    nodes = []
    for nid in n_runs:
        idxs = sorted(n_runs[nid])
        calls = n_calls.get(nid, [])
        durs = [d for d, _ in calls if d is not None]
        mo, sb, sg = outcome_stats(idxs)
        if nid in SENTINELS:
            label, kind = SENTINELS[nid]
            ncall, nfail = len(idxs), 0
        else:
            label, kind = nid[5:], "tool"
            ncall, nfail = len(calls), sum(1 for _, f in calls if f)
        nodes.append({
            "node_id": nid, "id": nid, "label": label, "kind": kind, "calls": ncall,
            "runs": len(idxs), "fail_calls": nfail,
            "fail_rate": (nfail / ncall) if (kind == "tool" and ncall) else None,
            "median_duration_s": median(durs), "p90_duration_s": pctl(durs, 0.9),
            "mean_run_tokens": mean([recs[i]["tokens"] for i in idxs]),
            "share_bad_runs": sb, "share_good_runs": sg, "mean_outcome": mo,
        })
    order = {"start": 0, "tool": 1, "answer": 2, "no_answer": 3}
    nodes.sort(key=lambda n: (order[n["kind"]], -n["calls"], n["node_id"]))

    edges = []
    for key in e_cnt:
        a, b = key
        idxs = sorted(e_runs[key])
        mo, _, _ = outcome_stats(idxs)
        edges.append({
            "source": a, "target": b, "count": e_cnt[key], "runs": len(idxs),
            "fail_rate_target": (e_tfail[key] / e_tcalls[key]) if e_tcalls[key] else None,
            "mean_outcome": mo,
            "mean_run_tokens": mean([recs[i]["tokens"] for i in idxs]),
            "median_duration_s": median(e_dur.get(key, [])),
        })
    edges.sort(key=lambda e: (-e["count"], e["source"], e["target"]))
    return nodes, edges


# ---------- errors / conversations ----------
def build_errors(recs):
    cls = {}
    for r in recs:
        seen_texts = set()
        for tc in r["tool_calls"]:
            if tool_failed(tc):
                txt = str(tc.get("error") or tc.get("result") or "").strip() or "unknown error"
                seen_texts.add(txt)
                _add_err(cls, txt, tool_name(tc), r["run_id"])
        for txt in r.get("errors") or []:
            txt = str(txt).strip()
            if txt and txt not in seen_texts:
                _add_err(cls, txt, "", r["run_id"])
    rows = []
    for c, d in cls.items():
        tool = sorted(d["tools"].items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        rows.append({"error_class": c, "count": d["count"], "runs": len(d["runs"]),
                     "tool": tool, "example": d["example"][:300]})
    rows.sort(key=lambda x: (-x["count"], x["error_class"]))
    return rows


def _add_err(cls, txt, tool, rid):
    c = error_class(txt)
    d = cls.setdefault(c, {"count": 0, "runs": set(), "tools": Counter(), "example": txt})
    d["count"] += 1
    d["runs"].add(rid)
    d["tools"][tool] += 1


def build_convs(recs):
    by = defaultdict(list)
    for r in recs:
        by[r["conversation_id"]].append(r)
    rows = []
    for cid, rs in by.items():
        outs = [r["outcome_score"] for r in rs if r["outcome_score"] is not None]
        rows.append({
            "conversation_id": cid, "n_runs": len(rs), "n_graded": len(outs),
            "first_ts": min(str(r.get("first_ts") or "") for r in rs),
            "latency_s": (sum(r["latency_s"] for r in rs if r["latency_s"] is not None)
                          if any(r["latency_s"] is not None for r in rs) else None),
            "tokens": sum(r["tokens"] for r in rs),
            "n_tool_fail_runs": sum(r["tool_fail"] for r in rs),
            "n_good": outs.count(1), "n_ambiguous": outs.count(0), "n_bad": outs.count(-1),
            "mean_outcome": mean(outs), "worst_outcome": min(outs) if outs else None,
        })
    rows.sort(key=lambda x: (x["first_ts"], x["conversation_id"]))
    return rows


# ---------- summary ----------
def build_summary(recs, nodes, errors, convs):
    n = len(recs)
    counts = {"good": 0, "ambiguous": 0, "bad": 0, "ungraded": 0}
    for r in recs:
        counts[r["label"]] += 1
    graded = n - counts["ungraded"]
    outs = [r["outcome_score"] for r in recs if r["outcome_score"] is not None]
    tot_tok = sum(r["tokens"] for r in recs)
    costs = [r["cost_usd"] for r in recs if r["cost_usd"] is not None]
    lat = [r["latency_s"] for r in recs if r["latency_s"] is not None]
    all_calls = [tc for r in recs for tc in r["tool_calls"]]
    durs = [num(tc["duration_s"]) for tc in all_calls if tc.get("duration_s") is not None]
    failed_calls = sum(1 for tc in all_calls if tool_failed(tc))

    def share(pred, key):
        tot = sum(key(r) for r in recs)
        return (sum(key(r) for r in recs if pred(r)) / tot) if tot else None

    def by_label(key):
        out = {}
        for lab in ("good", "ambiguous", "bad", "ungraded"):
            out[lab] = rnd(mean([key(r) for r in recs if r["label"] == lab]))
        return out

    personas = []
    pg = defaultdict(list)
    for r in recs:
        if r["persona"]:
            pg[r["persona"]].append(r)
    for p, rs in pg.items():
        outs_p = [r["outcome_score"] for r in rs if r["outcome_score"] is not None]
        issues = Counter(r["issue"] for r in rs if r["issue"] and r["outcome_score"] != 1)
        worst = sorted([r for r in rs if r["outcome_score"] is not None and r["outcome_score"] < 1],
                       key=lambda r: (r["outcome_score"], r["run_id"]))
        personas.append({
            "persona": p, "runs": len(rs), "share_of_runs": rnd(len(rs) / n),
            "good": outs_p.count(1), "ambiguous": outs_p.count(0), "bad": outs_p.count(-1),
            "mean_outcome": rnd(mean(outs_p)),
            "share_bad": rnd(outs_p.count(-1) / len(outs_p)) if outs_p else None,
            "share_not_good": rnd(1 - outs_p.count(1) / len(outs_p)) if outs_p else None,
            "mean_tokens": rnd(mean([r["tokens"] for r in rs]), 1),
            "median_latency_s": rnd(median([r["latency_s"] for r in rs])),
            "tool_fail_rate": rnd(sum(r["tool_fail"] for r in rs) / len(rs)),
            "top_issue": sorted(issues.items(), key=lambda kv: (-kv[1], kv[0]))[0][0] if issues else "",
            "issues": [{"issue": k, "runs": v} for k, v in
                       sorted(issues.items(), key=lambda kv: (-kv[1], kv[0]))[:5]],
            "example_run_id": worst[0]["run_id"] if worst else "",
            "example_ask": (str(worst[0].get("user_message") or "")[:200]) if worst else "",
        })
    personas.sort(key=lambda p: (-p["runs"], p["persona"]))
    issues_all = Counter(r["issue"] for r in recs if r["issue"] and r["outcome_score"] != 1)

    tool_rows = [nd for nd in nodes if nd["kind"] == "tool"]
    worst_tools = sorted([nd for nd in tool_rows if nd["calls"] >= 1 and nd["fail_calls"] > 0],
                         key=lambda nd: (-nd["fail_rate"], -nd["calls"], nd["node_id"]))[:5]
    slowest_tools = sorted([nd for nd in tool_rows if nd["median_duration_s"] is not None],
                           key=lambda nd: (-nd["median_duration_s"], nd["node_id"]))[:5]
    return {
        "n_runs": n, "n_conversations": len(convs), "n_graded": graded,
        "has_scores": graded > 0, "counts": counts,
        "shares": {k: rnd(v / n) if n else None for k, v in counts.items()},
        "mean_outcome": rnd(mean(outs)),
        "n_tools": len(tool_rows), "n_tool_calls": len(all_calls),
        "errors": {
            "tool_fail_runs": sum(r["tool_fail"] for r in recs),
            "tool_fail_rate": rnd(sum(r["tool_fail"] for r in recs) / n) if n else None,
            "unrecovered_runs": sum(r["unrecovered"] for r in recs),
            "unrecovered_rate": rnd(sum(r["unrecovered"] for r in recs) / n) if n else None,
            "no_answer_runs": sum(r["no_answer"] for r in recs),
            "no_answer_rate": rnd(sum(r["no_answer"] for r in recs) / n) if n else None,
            "failed_calls": failed_calls,
            "call_fail_rate": rnd(failed_calls / len(all_calls)) if all_calls else None,
            "top_error_classes": [{"error_class": e["error_class"], "count": e["count"],
                                   "runs": e["runs"], "tool": e["tool"]} for e in errors[:5]],
            "worst_tools": [{"tool": nd["label"], "calls": nd["calls"],
                             "fail_rate": rnd(nd["fail_rate"])} for nd in worst_tools],
            "by_label_tool_fail_rate": by_label(lambda r: r["tool_fail"]),
        },
        "cost": {
            "total_tokens": tot_tok, "mean_tokens": rnd(tot_tok / n, 1) if n else None,
            "median_tokens": rnd(median([r["tokens"] for r in recs]), 1),
            "p90_tokens": rnd(pctl([r["tokens"] for r in recs], 0.9), 1),
            "has_cost_usd": bool(costs), "cost_usd_total": rnd(sum(costs)) if costs else None,
            "cost_usd_mean": rnd(mean(costs)) if costs else None,
            "heavy_runs": sum(r["heavy"] for r in recs),
            "token_share_bad": rnd(share(lambda r: r["label"] == "bad", lambda r: r["tokens"])),
            "token_share_not_good": rnd(share(lambda r: r["label"] in ("bad", "ambiguous"),
                                              lambda r: r["tokens"])),
            "by_label_mean_tokens": by_label(lambda r: r["tokens"]),
        },
        "latency": {
            "median_s": rnd(median(lat)), "p90_s": rnd(pctl(lat, 0.9)), "mean_s": rnd(mean(lat)),
            "total_s": rnd(sum(lat)) if lat else None, "slow_runs": sum(r["slow"] for r in recs),
            "tool_median_s": rnd(median(durs)), "tool_p90_s": rnd(pctl(durs, 0.9)),
            "slowest_tools": [{"tool": nd["label"], "median_s": rnd(nd["median_duration_s"])}
                              for nd in slowest_tools],
            "latency_share_bad": rnd(share(lambda r: r["label"] == "bad", lambda r: r["latency_s"] or 0)),
            "by_label_median_latency": {
                lab: rnd(median([r["latency_s"] for r in recs if r["label"] == lab]))
                for lab in ("good", "ambiguous", "bad", "ungraded")},
        },
        "score": {"mean_outcome": rnd(mean(outs)),
                  "good_rate": rnd(counts["good"] / graded) if graded else None,
                  "ambiguous_rate": rnd(counts["ambiguous"] / graded) if graded else None,
                  "bad_rate": rnd(counts["bad"] / graded) if graded else None},
        "personas": personas,
        "top_issues": [{"issue": k, "runs": v} for k, v in
                       sorted(issues_all.items(), key=lambda kv: (-kv[1], kv[0]))[:10]],
        "dimensions": DIMENSIONS,
    }


# ---------- orchestration ----------
def build(runs, scores):
    recs = prepare(runs, scores)
    nodes, edges = build_graph(recs)
    errors = build_errors(recs)
    convs = build_convs(recs)
    summary = build_summary(recs, nodes, errors, convs)
    return recs, nodes, edges, errors, convs, summary


def dossier(r):
    d = {k: v for k, v in r.items()}
    d["tool_fail"] = r["tool_fail"]
    return d


def write_all(cig_dir, recs, nodes, edges, errors, convs, summary):
    os.makedirs(cig_dir, exist_ok=True)
    write_csv(os.path.join(cig_dir, "runs.csv"), RUN_COLS, recs)
    write_csv(os.path.join(cig_dir, "nodes.csv"), NODE_COLS,
              [dict(n, node_id=n["node_id"]) for n in nodes])
    write_csv(os.path.join(cig_dir, "edges.csv"), EDGE_COLS, edges)
    write_csv(os.path.join(cig_dir, "convs.csv"), CONV_COLS, convs)
    write_csv(os.path.join(cig_dir, "errors.csv"), ERR_COLS, errors)

    def jn(n):
        return {k: (rnd(v) if isinstance(v, float) else v) for k, v in n.items() if k != "node_id"}

    def je(e):
        return {k: (rnd(v) if isinstance(v, float) else v) for k, v in e.items()}
    flow = {"nodes": [jn(n) for n in nodes], "edges": [je(e) for e in edges],
            "dimensions": DIMENSIONS, "summary": summary}
    with open(os.path.join(cig_dir, "flow_graph.json"), "w", encoding="utf-8") as f:
        json.dump(flow, f, indent=1, sort_keys=True, ensure_ascii=False)
        f.write("\n")
    with open(os.path.join(cig_dir, "run_dossiers.jsonl"), "w", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(dossier(r), sort_keys=True, ensure_ascii=False) + "\n")
    with open(os.path.join(cig_dir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=1, sort_keys=True, ensure_ascii=False)
        f.write("\n")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="plite cig", description="Build the CIG from normalized runs.")
    ap.add_argument("--out", default="./procept-out")
    ap.add_argument("--runs", help="override path to runs.jsonl")
    ap.add_argument("--scores", help="override path to scores.csv")
    a = ap.parse_args(argv)
    out = os.path.abspath(a.out)
    checks.require(out, ["ingest", "merge"])
    runs_path = a.runs or os.path.join(out, "normalized", "runs.jsonl")
    if not os.path.exists(runs_path):
        print("error: %s not found; run `normalize` first" % runs_path, file=sys.stderr)
        return 2
    runs = load_jsonl(runs_path)
    if not runs:
        print("error: %s has no runs" % runs_path, file=sys.stderr)
        return 2
    unusable = checks.unusable_ids(out)
    if unusable:
        runs = [r for r in runs if str(r.get("run_id")) not in unusable]
        if not runs:
            print("error: every run is unusable; there is nothing to build a CIG from", file=sys.stderr)
            return 2
    scores = load_scores(a.scores or os.path.join(out, "scores.csv"))
    scores = {k: v for k, v in scores.items() if k not in unusable}
    recs, nodes, edges, errors, convs, summary = build(runs, scores)
    cig_dir = os.path.join(out, "cig")
    write_all(cig_dir, recs, nodes, edges, errors, convs, summary)
    c = summary["counts"]
    print("CIG written to %s" % cig_dir)
    print("  runs=%d conversations=%d tools=%d edges=%d" % (
        summary["n_runs"], summary["n_conversations"], summary["n_tools"], len(edges)))
    print("  good=%d ambiguous=%d bad=%d ungraded=%d" % (c["good"], c["ambiguous"], c["bad"], c["ungraded"]))
    e = summary["errors"]
    print("  tool_fail_runs=%d unrecovered=%d no_answer=%d median_latency_s=%s mean_tokens=%s" % (
        e["tool_fail_runs"], e["unrecovered_runs"], e["no_answer_runs"],
        summary["latency"]["median_s"], summary["cost"]["mean_tokens"]))
    if not summary["has_scores"]:
        print("  note: no scores.csv, all runs are 'ungraded' (score dimension empty)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
