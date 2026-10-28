"""Tiny query CLI over a built cig/ directory. Standard library only.

  python3 cig_query.py [--out DIR] summary
  python3 cig_query.py [--out DIR] runs [--label bad] [--tool NAME] [--min-latency S] [--limit N]
  python3 cig_query.py [--out DIR] run <run-id-prefix>
  python3 cig_query.py [--out DIR] tools
  python3 cig_query.py [--out DIR] errors
"""
import argparse
import csv
import json
import os
import sys


def read_csv(cig, name):
    path = os.path.join(cig, name)
    if not os.path.exists(path):
        raise SystemExit("error: %s not found; run `cig` first" % path)
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def table(rows, cols, widths=None):
    widths = widths or {}
    cells = [[str(r.get(c, ""))[:widths.get(c, 40)] for c in cols] for r in rows]
    w = [max([len(c)] + [len(row[i]) for row in cells]) for i, c in enumerate(cols)]
    lines = ["  ".join(c.ljust(w[i]) for i, c in enumerate(cols)),
             "  ".join("-" * w[i] for i in range(len(cols)))]
    lines += ["  ".join(v.ljust(w[i]) for i, v in enumerate(row)) for row in cells]
    return "\n".join(lines)


def cmd_summary(cig, a):
    with open(os.path.join(cig, "summary.json"), encoding="utf-8") as f:
        s = json.load(f)
    c, e, co, la = s["counts"], s["errors"], s["cost"], s["latency"]
    print("runs %d | conversations %d | tools %d | graded %d" % (
        s["n_runs"], s["n_conversations"], s["n_tools"], s["n_graded"]))
    print("score    good %d | ambiguous %d | bad %d | ungraded %d | mean %s" % (
        c["good"], c["ambiguous"], c["bad"], c["ungraded"], s["mean_outcome"]))
    print("errors   tool-fail runs %d | unrecovered %d | no answer %d | call fail rate %s" % (
        e["tool_fail_runs"], e["unrecovered_runs"], e["no_answer_runs"], e["call_fail_rate"]))
    print("cost     mean tokens %s | p90 %s | heavy runs %d | usd %s" % (
        co["mean_tokens"], co["p90_tokens"], co["heavy_runs"], co["cost_usd_total"]))
    sec = lambda v: "-" if v is None else "%ss" % v
    print("latency  median %s | p90 %s | slow runs %d | tool median %s" % (
        sec(la["median_s"]), sec(la["p90_s"]), la["slow_runs"], sec(la["tool_median_s"])))
    for p in s["personas"][:3]:
        print("persona  %s: %d runs, bad %d, top issue: %s" % (p["persona"], p["runs"], p["bad"], p["top_issue"] or "-"))
    return 0


def cmd_runs(cig, a):
    rows = read_csv(cig, "runs.csv")
    if a.label:
        rows = [r for r in rows if r["label"] == a.label]
    if a.tool:
        rows = [r for r in rows if a.tool in r["workflow"].split(" > ")]
    if a.min_latency is not None:
        rows = [r for r in rows if float(r["latency_s"] or 0) >= a.min_latency]
    rows.sort(key=lambda r: (-float(r["latency_s"] or 0), r["run_id"]))
    print("%d runs" % len(rows))
    if rows:
        print(table(rows[:a.limit], ["run_id", "label", "latency_s", "tokens", "n_tool_calls",
                                     "n_tool_failures", "persona"], {"run_id": 28}))
    return 0


def cmd_run(cig, a):
    path = os.path.join(cig, "run_dossiers.jsonl")
    hits = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                d = json.loads(line)
                if str(d["run_id"]).startswith(a.prefix):
                    hits.append(d)
    if not hits:
        print("no run with id prefix %r" % a.prefix, file=sys.stderr)
        return 1
    if len(hits) > 1:
        print("%d runs match, be more specific:" % len(hits), file=sys.stderr)
        for d in hits[:10]:
            print("  " + str(d["run_id"]), file=sys.stderr)
        return 1
    d = hits[0]
    print("run %s | conv %s | %s | %s | %s tokens" % (
        d["run_id"], d["conversation_id"], d["label"],
        "-" if d["latency_s"] is None else "%ss" % d["latency_s"], d["tokens"]))
    if d.get("persona") or d.get("issue"):
        print("persona: %s | issue: %s" % (d.get("persona") or "-", d.get("issue") or "-"))
    print("\nASK:\n%s\n" % (d.get("user_message") or ""))
    for i, tc in enumerate(d["tool_calls"], 1):
        ok = "ok" if tc.get("success") is not False else "FAIL"
        print("%2d. %s [%s] %ss" % (i, tc.get("name"), ok, tc.get("duration_s")))
        print("    args: %s" % str(tc.get("args") or "")[:200])
        print("    result: %s" % str(tc.get("result") or "")[:200])
        if tc.get("error"):
            print("    error: %s" % str(tc["error"])[:200])
    print("\nANSWER:\n%s" % (d.get("final_answer") or "(none)"))
    if d.get("rationale"):
        print("\nGRADER: %s" % d["rationale"])
    return 0


def cmd_tools(cig, a):
    rows = [r for r in read_csv(cig, "nodes.csv") if r["kind"] == "tool"]
    rows.sort(key=lambda r: (-int(r["calls"]), r["node_id"]))
    print(table(rows, ["node_id", "calls", "runs", "fail_rate", "median_duration_s", "p90_duration_s",
                       "mean_run_tokens", "mean_outcome"], {"node_id": 36}))
    return 0


def cmd_errors(cig, a):
    rows = read_csv(cig, "errors.csv")
    print(table(rows[:a.limit], ["error_class", "count", "runs", "tool", "example"],
                {"error_class": 50, "example": 60}) if rows else "no errors")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="cig_query", description="Query a built cig/ directory.")
    ap.add_argument("--out", default="./procept-out")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("summary")
    r = sub.add_parser("runs")
    r.add_argument("--label", choices=["good", "ambiguous", "bad", "ungraded"])
    r.add_argument("--tool")
    r.add_argument("--min-latency", type=float)
    r.add_argument("--limit", type=int, default=20)
    sub.add_parser("run").add_argument("prefix")
    sub.add_parser("tools")
    e = sub.add_parser("errors")
    e.add_argument("--limit", type=int, default=20)
    a = ap.parse_args(argv)
    cig = os.path.join(a.out, "cig")
    fn = {"summary": cmd_summary, "runs": cmd_runs, "run": cmd_run, "tools": cmd_tools,
          "errors": cmd_errors}[a.cmd]
    return fn(cig, a)


if __name__ == "__main__":
    sys.exit(main())
