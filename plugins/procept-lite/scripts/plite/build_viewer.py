"""Write one self-contained cig.html (inline CSS/JS/data, no network) from cig/ outputs."""
import argparse
import json
import os
import sys

try:
    from . import build_cig as bc
except ImportError:  # run as a plain script
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import build_cig as bc

MAX_TOOL_NODES = 60
MAX_RUNS = 3000
OTHER_NAME = "other"
OTHER_ID = "tool:" + OTHER_NAME
MAX_CALLS_SHOWN = 60
HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, "..", "..", "viewer", "index.template.html")


def clip(s, n):
    s = "" if s is None else str(s)
    return s if len(s) <= n else s[:n] + "..."


def safe_json(obj):
    """JSON safe to embed inside a <script> element."""
    s = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    return (s.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
             .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def group_tail(recs):
    """If more than MAX_TOOL_NODES tools, regroup the tail as one 'other' tool."""
    counts = {}
    for r in recs:
        for tc in r["tool_calls"]:
            n = bc.tool_name(tc)
            counts[n] = counts.get(n, 0) + 1
    if len(counts) <= MAX_TOOL_NODES:
        return recs, []
    ranked = sorted(counts, key=lambda n: (-counts[n], n))
    keep = set(ranked[:MAX_TOOL_NODES - 1])
    tail = sorted(n for n in counts if n not in keep)
    out = []
    for r in recs:
        r2 = dict(r)
        r2["tool_calls"] = [dict(tc, name=OTHER_NAME) if bc.tool_name(tc) not in keep else tc
                            for tc in r["tool_calls"]]
        out.append(r2)
    return out, tail


def run_payload(r):
    names, seen = [], set()
    for tc in r["tool_calls"]:
        n = bc.tool_name(tc)
        if n not in seen:
            seen.add(n)
            names.append(n)
    calls = []
    for tc in r["tool_calls"][:MAX_CALLS_SHOWN]:
        d = tc.get("duration_s")
        calls.append([bc.tool_name(tc), None if d is None else round(bc.num(d), 3),
                      0 if bc.tool_failed(tc) else (2 if tc.get("success") is False else 1),
                      clip(tc.get("args"), 400), clip(tc.get("result"), 400),
                      clip(tc.get("error"), 240)])
    return {
        "i": r["run_id"], "c": r["conversation_id"], "l": r["label"], "s": r["outcome_score"],
        "lat": None if r["latency_s"] is None else round(r["latency_s"], 3), "tok": r["tokens"], "f": r["n_tool_failures"],
        "n": r["n_tool_calls"], "na": r["no_answer"], "ur": r["unrecovered"],
        "t": names, "p": r.get("persona") or "", "q": r.get("issue") or "",
        "a": clip(r.get("user_message"), 1200), "z": clip(r.get("final_answer"), 1500),
        "rs": clip(r.get("rationale"), 500), "k": clip(r.get("key_quote"), 300),
        "calls": calls, "more": max(0, len(r["tool_calls"]) - MAX_CALLS_SHOWN),
    }


def select_runs(recs):
    if len(recs) <= MAX_RUNS:
        return recs, 0
    key = lambda r: (r["outcome_score"] if r["outcome_score"] is not None else 0.5,
                     -r["n_tool_failures"], -(r["latency_s"] or 0), r["run_id"])
    keep = sorted(recs, key=key)[:MAX_RUNS]
    return keep, len(recs) - MAX_RUNS


def build_payload(cig_dir):
    with open(os.path.join(cig_dir, "summary.json"), encoding="utf-8") as f:
        summary = json.load(f)
    recs = bc.load_jsonl(os.path.join(cig_dir, "run_dossiers.jsonl"))
    for r in recs:  # dossiers already carry derived fields; make sure types are sane
        r["tool_calls"] = [tc for tc in (r.get("tool_calls") or []) if isinstance(tc, dict)]
        r.setdefault("outcome_score", None)
        if r["outcome_score"] == "":
            r["outcome_score"] = None
        r.setdefault("label", "ungraded")
        r["latency_s"] = None if r.get("latency_s") in (None, "") else bc.num(r.get("latency_s"))
        r["tokens"] = int(bc.num(r.get("tokens")))
    grouped, tail = group_tail(recs)
    nodes, edges = bc.build_graph(grouped)
    graph = {
        "nodes": [{k: (bc.rnd(v) if isinstance(v, float) else v) for k, v in n.items()
                   if k != "node_id"} for n in nodes],
        "edges": [{k: (bc.rnd(v) if isinstance(v, float) else v) for k, v in e.items()} for e in edges],
        "dimensions": bc.DIMENSIONS,
    }
    shown, hidden = select_runs(recs)
    return {
        "graph": graph, "summary": summary,
        "runs": [run_payload(r) for r in shown],
        "hidden_runs": hidden,
        "other_tools": tail, "other_id": OTHER_ID,
        "max_tool_nodes": MAX_TOOL_NODES,
    }


def render(payload, template_path=TEMPLATE):
    with open(template_path, encoding="utf-8") as f:
        tpl = f.read()
    marker = "/*__DATA__*/null"
    if marker not in tpl:
        raise ValueError("template is missing the data marker")
    return tpl.replace(marker, safe_json(payload), 1)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="plite viewer", description="Build the self-contained cig.html.")
    ap.add_argument("--out", default="./procept-out")
    ap.add_argument("--cig", help="override cig/ directory")
    ap.add_argument("--template", default=TEMPLATE)
    a = ap.parse_args(argv)
    cig_dir = a.cig or os.path.join(a.out, "cig")
    if not os.path.exists(os.path.join(cig_dir, "summary.json")):
        print("error: %s has no CIG; run `cig` first" % cig_dir, file=sys.stderr)
        return 2
    html = render(build_payload(cig_dir), a.template)
    dest = os.path.join(os.path.dirname(os.path.abspath(cig_dir)), "cig.html") if a.cig \
        else os.path.join(a.out, "cig.html")
    with open(dest, "w", encoding="utf-8") as f:
        f.write(html)
    print("viewer written to %s (%d KB)" % (dest, len(html) // 1024))
    return 0


if __name__ == "__main__":
    sys.exit(main())
