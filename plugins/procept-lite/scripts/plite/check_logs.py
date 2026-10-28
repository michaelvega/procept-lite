"""check_logs (command `inspect`): is this trace export sufficient to build the run dossiers and the CIG?

Usage: python3 plite.py inspect --src FILE_OR_DIR... [--format ..] [--map mapping.json] [--prices prices.json]
                                [--noise-regex RE] [--tool-regex RE] [--out DIR]
Writes DIR/inspect.json and DIR/inspect.md. Exit code 0 even for NOT_READY (the report is the product); 2 only
when nothing could be read.

Requirements (each GREEN / AMBER / RED with the exact missing field and a fix pointer):
  R1 run id derivable          R2 conversation/session id     R3 timestamps (latency)
  R4 user message text         R5 final answer text           R6 tool calls with name
  R7 tool args and results     R8 tool success/error status   R9 token usage (cost)
  R10 model name (cost via prices)                            R11 volume (>= 30 runs recommended)
Thresholds: GREEN >= 90% of runs (or tool calls) have the field, AMBER > 0%, RED none.
Verdict: NOT_READY only when no runs could be built; READY if everything is GREEN;
otherwise USABLE_WITH_GAPS. Message contents are shown only as <= 80 char redacted examples.
"""
import argparse
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import add_ingest_args, short, write_json  # noqa: E402
import normalize as N  # noqa: E402

GREEN, AMBER, RED = "GREEN", "AMBER", "RED"
CRITICAL = ()  # degrade, never block: gaps shrink the analysis (see cleaning_report.md), they do not stop it
DOC = "references/data-gaps.md#"
RUN_ID_GOOD = ("call_id", "call_id_ancestor", "call_id_trace", "langsmith_trace")


def _grade(frac, n):
    if n == 0:
        return RED
    return GREEN if frac >= 0.9 else (AMBER if frac > 0 else RED)


def _pct(a, b):
    return (100.0 * a / b) if b else 0.0


def _req(rid, name, status, detail, missing, anchor):
    return {"id": rid, "name": name, "status": status, "detail": detail,
            "missing": missing if status != GREEN else "", "fix": DOC + anchor}


def evaluate(runs, spans, stats, opts):
    n = len(runs)
    tools = [c for r in runs for c in r["tool_calls"]]
    tool_spans = [s for s in spans if s.kind == "tool"]
    reqs = []

    # R1
    src = Counter(r["extra"].get("run_id_source") for r in runs)
    good = sum(src[k] for k in RUN_ID_GOOD)
    if n == 0:
        st, miss = RED, "no runs could be built"
    elif good >= 0.9 * n:
        st, miss = GREEN, ""
    elif src["trace_id"] + src["conversation_turn"] + src["time_containment"] + src["conversation"] >= 0.5 * n:
        st = AMBER
        miss = ("gen_ai.agent.call.id missing on %d%% of runs; falling back to %s (a trace holding several turns "
                "would merge into one run)" % (round(_pct(n - good, n)), "trace_id / conversation turns"))
    else:
        st, miss = RED, "no gen_ai.agent.call.id, trace_id or run_id column; only %s" % dict(src)
    reqs.append(_req("R1", "run id derivable", st, "run id sources: %s" % dict(src),
                     miss + " (accepted: %s)" % ", ".join(N.F["call"] + N.F["trace"]), "run-id"))

    # R2
    with_conv = sum(1 for r in runs if r["conversation_id"] != r["run_id"])
    convs = len({r["conversation_id"] for r in runs})
    st = _grade(with_conv / n if n else 0, n)
    reqs.append(_req("R2", "conversation / session id", st,
                     "%d of %d runs carry a conversation id (%d conversations)" % (with_conv, n, convs),
                     "no conversation id; every run is treated as its own conversation (accepted: %s)" %
                     ", ".join(N.F["conv"]), "conversation-id"))

    # R3
    timed = sum(1 for r in runs if r["latency_s"] and r["latency_s"] > 0)
    st = _grade(timed / n if n else 0, n)
    reqs.append(_req("R3", "timestamps (latency)", st, "%d of %d runs have a measurable latency" % (timed, n),
                     "start/end timestamps or duration missing: the latency dimension is dropped, cost/errors/score are kept "
                     "(accepted: %s; durations: duration, duration_ms)" %
                     ", ".join(N.F["start"][:4] + N.F["end"][:2]), "timestamps"))

    # R4 / R5
    hu = sum(1 for r in runs if r["user_message"])
    ha = sum(1 for r in runs if r["final_answer"])
    reqs.append(_req("R4", "user message text", _grade(hu / n if n else 0, n), "%d of %d runs have the user message" % (hu, n),
                     "no user message found: those runs are marked thin and graded on what exists "
                     "(accepted: %s, or input events gen_ai.user.message)" %
                     ", ".join(N.F["in_msgs"][:3]), "message-content"))
    reqs.append(_req("R5", "final answer text", _grade(ha / n if n else 0, n), "%d of %d runs have a final answer" % (ha, n),
                     "no assistant answer found: those runs are marked thin and graded on what exists "
                     "(accepted: %s, final_result, or events gen_ai.choice)" %
                     ", ".join(N.F["out_msgs"][:3]), "message-content"))

    # R6 / R7 / R8
    names = Counter(c["name"] for c in tools)
    with_tools = sum(1 for r in runs if r["tool_calls"])
    reqs.append(_req("R6", "tool calls with name", GREEN if tools else RED,
                     "%d tool calls, %d distinct tools, in %d of %d runs" % (len(tools), len(names), with_tools, n),
                     "no tool spans (accepted: gen_ai.operation.name=execute_tool or %s; LangSmith run_type=tool; "
                     "flat: map tool_name). If the agent has no tools the CIG has no tool nodes" %
                     ", ".join(N.F["tool"]), "tool-calls"))
    na = sum(1 for c in tools if c["args"])
    nr = sum(1 for c in tools if c["result"])
    fa, fr = (na / len(tools), nr / len(tools)) if tools else (0, 0)
    st = RED if not tools else _grade(min(fa, fr), len(tools))
    if tools and st == RED and max(fa, fr) > 0:
        st = AMBER
    gaps = []
    if fa < 0.9:
        gaps.append("arguments (%s)" % ", ".join(N.F["args"][:3]))
    if fr < 0.9:
        gaps.append("results (%s)" % ", ".join(N.F["result"][:3]))
    reqs.append(_req("R7", "tool args and results", st,
                     "args on %d%% and results on %d%% of tool calls" % (round(100 * fa), round(100 * fr)),
                     "missing tool " + " and ".join(gaps) if tools else "no tool calls to check", "tool-args-results"))
    hs = sum(1 for s in tool_spans if s.has_status)
    fails = sum(r["n_tool_failures"] for r in runs)
    canc = sum(r["extra"].get("cancelled", 0) for r in runs)
    st = _grade(hs / len(tool_spans) if tool_spans else 0, len(tool_spans))
    reqs.append(_req("R8", "tool success/error status", st,
                     "status field on %d of %d tool spans; %d failed calls, %d cancelled" % (hs, len(tool_spans), fails, canc),
                     "no explicit status/error/exception field on tool spans, so failures cannot be told from "
                     "successes (accepted: status_code, level, is_exception, error.type, exception.*, "
                     "LangSmith error)", "tool-status"))

    # R9 / R10
    tk = sum(1 for r in runs if (r["input_tokens"] or r["output_tokens"]))
    reqs.append(_req("R9", "token usage (cost)", _grade(tk / n if n else 0, n), "%d of %d runs have token counts" % (tk, n),
                     "no token usage: tokens are estimated as chars/4 and flagged estimated (accepted: %s / %s)" % (", ".join(N.F["itok"][:2]), ", ".join(N.F["otok"][:2])),
                     "token-usage"))
    models = Counter(m for r in runs for m in r["models"])
    has_cost = sum(1 for r in runs if r["cost_usd"] is not None)
    unpriced = sorted({m for r in runs for m in r["extra"].get("unpriced_models", [])})
    wm = sum(1 for r in runs if r["models"])
    if has_cost >= 0.9 * n and n:
        st, miss = GREEN, ""
    elif wm >= 0.9 * n and n and not opts.prices:
        st, miss = AMBER, "model names present (%s) but no prices: pass --prices prices.json to get cost_usd" % \
                          ", ".join(list(models)[:3])
    elif unpriced:
        st, miss = AMBER, "no price for model(s): %s (add them to prices.json)" % ", ".join(unpriced[:5])
    elif wm == 0:
        st, miss = RED, "no model name (accepted: %s)" % ", ".join(N.F["model"][:3])
    else:
        st, miss = (GREEN if wm >= 0.9 * n else AMBER), "model name missing on some runs"
    reqs.append(_req("R10", "model name / cost", st,
                     "models: %s; cost_usd on %d of %d runs" % (", ".join(list(models)[:4]) or "none", has_cost, n),
                     miss, "model-name"))

    # R11
    st = GREEN if n >= 30 else (AMBER if n >= 10 else RED)
    reqs.append(_req("R11", "volume", st, "%d runs (>= 30 recommended for a scorecard)" % n,
                     "only %d runs; patterns will be anecdotal" % n, "volume"))
    return reqs, models


def verdict_of(reqs, n):
    if n == 0:
        return "NOT_READY"
    return "READY" if all(r["status"] == GREEN for r in reqs) else "USABLE_WITH_GAPS"


def prioritized(reqs):
    def key(r):
        crit = r["id"] in CRITICAL
        return (0 if r["status"] == RED and crit else 1 if r["status"] == RED else 2 if crit else 3, r["id"])
    return ["%s %s [%s]: %s -> %s" % (r["id"], r["name"], r["status"], r["missing"], r["fix"])
            for r in sorted((r for r in reqs if r["status"] != GREEN), key=key)]


def to_markdown(rep):
    c = rep["counts"]
    L = ["# Log sufficiency report", "", "**Verdict: %s**" % rep["verdict"], "",
         "Formats detected: %s. Files: %d." % (", ".join("%s (%d records)" % kv for kv in rep["formats"].items()) or "none",
                                               len(rep["files"])), "",
         "| spans read | spans kept | runs | conversations | tool calls | noise dropped | duplicates |", "|---|---|---|---|---|---|---|",
         "| %(records)d | %(spans)d | %(runs)d | %(conversations)d | %(tool_calls)d | %(noise_spans_dropped)d | %(duplicate_spans)d |" % c,
         "", "| # | Requirement | Status | Detail |", "|---|---|---|---|"]
    for r in rep["requirements"]:
        L.append("| %s | %s | %s | %s |" % (r["id"], r["name"], r["status"], r["detail"]))
    L += ["", "## What to fix (most important first)", ""]
    L += ["%d. %s" % (i + 1, x) for i, x in enumerate(rep["fix_list"])] or ["Nothing: all requirements are GREEN."]
    if rep["examples"]:
        L += ["", "## Examples (redacted, <= 80 chars)", ""] + ["- %s: `%s`" % kv for kv in rep["examples"].items()]
    if rep["columns_seen"]:
        L += ["", "## Columns seen", "", ", ".join(rep["columns_seen"])]
    return "\n".join(L) + "\n"


def build_report(runs, spans, stats, opts):
    reqs, models = evaluate(runs, spans, stats, opts)
    n = len(runs)
    ex = {}
    if runs:
        r0 = next((r for r in runs if r["user_message"]), None)
        if r0:
            ex["user message"] = short(r0["user_message"])
        r1 = next((r for r in runs if r["final_answer"]), None)
        if r1:
            ex["final answer"] = short(r1["final_answer"])
        t0 = next((c for r in runs for c in r["tool_calls"]), None)
        if t0:
            ex["tool call"] = short("%s(%s)" % (t0["name"], t0["args"]))
    rep = {
        "verdict": verdict_of(reqs, n),
        "formats": dict(stats["formats"]),
        "files": [os.path.basename(f) for f in stats.get("files", [])],
        "counts": {"records": stats["records"], "spans": len(spans), "runs": n,
                   "conversations": len({r["conversation_id"] for r in runs}),
                   "tool_calls": sum(len(r["tool_calls"]) for r in runs),
                   "noise_spans_dropped": stats["noise_dropped"], "duplicate_spans": stats["duplicates"],
                   "unreadable_records": stats["unknown_records"] + stats["bad_lines"]},
        "requirements": reqs,
        "fix_list": prioritized(reqs),
        "examples": ex,
        "columns_seen": [k for k, _ in stats["columns"].most_common(40)],
    }
    if n == 0:
        rep["fix_list"].insert(0, stats.get("mapping_failed") or (
            "No runs could be built (formats seen: %s). Write a mapping file (templates/mapping.example.json) "
            "and pass --map; see references/data-gaps.md#flat-tables" % (rep["formats"] or "none")))
    return rep


def write_report(out, rep):
    os.makedirs(out, exist_ok=True)
    write_json(os.path.join(out, "inspect.json"), rep)
    with open(os.path.join(out, "inspect.md"), "w", encoding="utf-8") as f:
        f.write(to_markdown(rep))


def main(argv=None):
    p = argparse.ArgumentParser(prog="plite inspect", description="check that trace logs are sufficient")
    add_ingest_args(p)
    a = p.parse_args(argv)
    try:
        opts = N.opts_from_args(a)
        runs, spans, stats = N.run_all(a.src, opts)
    except (FileNotFoundError, ValueError) as e:
        print("error: %s" % e, file=sys.stderr)
        return 2
    except SystemExit as e:
        print("error: %s" % e, file=sys.stderr)
        return 2
    if stats["records"] == 0:
        print("error: the sources contain no readable records (empty, binary, or not JSON/CSV)", file=sys.stderr)
        return 2
    rep = build_report(runs, spans, stats, opts)
    write_report(a.out, rep)
    c = rep["counts"]
    reqs = rep["requirements"]
    print("verdict: %s | %d runs, %d conversations, %d tool calls (%d spans, %d noise dropped)" % (
        rep["verdict"], c["runs"], c["conversations"], c["tool_calls"], c["spans"], c["noise_spans_dropped"]))
    for r in reqs:
        print("  %-3s %-5s %s" % (r["id"], r["status"], r["name"]))
    for i, x in enumerate(rep["fix_list"][:5]):
        print("  fix %d: %s" % (i + 1, x))
    print("wrote %s and inspect.md" % os.path.join(a.out, "inspect.json"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
