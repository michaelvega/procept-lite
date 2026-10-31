"""Write one self-contained cig.html (inline CSS/JS/data, no network) from cig/ outputs.

The page is the System 5 teaser: the tool network coloured by user-goal score, errors / cost / latency
as tabs that recolour it (the numbers stay in System 5), and a couple of tool names blurred. Only what is needed to draw the
graph and the panel teasers is embedded: no run dossiers, no per-run data. The real name of a blurred
tool is never written to the file: its node carries an opaque id and a same-length scrambled stand-in,
and the page blurs that text on top.

This script is a starter, like the template next to it (viewer/index.template.html): edit either for a
given dataset. Standard library only.
"""
import argparse
import hashlib
import html as htmllib
import json
import os
import re
import sys

try:
    from . import build_cig as bc
    from . import checks
except ImportError:  # run as a plain script
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import build_cig as bc
    import checks

MAX_TOOL_NODES = 60
OTHER_NAME = "other"
OTHER_ID = "tool:" + OTHER_NAME
MIN_BLUR_LEN = 8  # a shorter name does not hide behind a blur
HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, "..", "..", "viewer", "index.template.html")
SYSTEM5_TEMPLATE = os.path.join(HERE, "..", "..", "templates", "system5.json")
DEFAULT_URL = "https://procept.tech/system5"
DATA_MARK = "/*__DATA__*/null"
URL_MARK = "__SYSTEM5_URL__"
_WEB_URL = re.compile(r"^https?://[^\s<>\"'`\\]+$")
_KEEP = " _-./:"  # separators survive scrambling, so a stand-in still looks like a tool name


def safe_json(obj):
    """JSON safe to embed inside a <script> element (ASCII only, no <, > or &)."""
    s = json.dumps(obj, ensure_ascii=True, separators=(",", ":"))
    return s.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")


def read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def system5_url(out_dir):
    """The System 5 link, and nothing else, from system5.json: <out>/ first, then templates/, then inline."""
    for path in (os.path.join(out_dir, "system5.json"), SYSTEM5_TEMPLATE):
        cfg = read_json(path)
        url = cfg.get("url") if isinstance(cfg, dict) else None
        url = url.strip() if isinstance(url, str) else ""
        if _WEB_URL.match(url):  # http(s) only: never a javascript: or data: link
            return url
    return DEFAULT_URL


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


def load_graph(cig_dir):
    """(nodes, edges, grouped) as the cig stage wrote them; grouped = tools folded into 'other', else 0."""
    flow = read_json(os.path.join(cig_dir, "flow_graph.json"))
    if isinstance(flow, dict) and isinstance(flow.get("nodes"), list) and isinstance(flow.get("edges"), list):
        if sum(1 for n in flow["nodes"] if n.get("kind") == "tool") <= MAX_TOOL_NODES:
            return flow["nodes"], flow["edges"], 0
    path = os.path.join(cig_dir, "run_dossiers.jsonl")  # the tail can only be folded from the runs
    if not os.path.exists(path):
        raise ValueError("%s has no graph; run `cig` first" % cig_dir)
    recs = bc.load_jsonl(path)
    for r in recs:
        r["tool_calls"] = [tc for tc in (r.get("tool_calls") or []) if isinstance(tc, dict)]
        r["outcome_score"] = None if r.get("outcome_score") in (None, "") else r["outcome_score"]
        r["tokens"] = int(bc.num(r.get("tokens")))
        r.setdefault("no_answer", 0)
    grouped, tail = group_tail(recs)
    nodes, edges = bc.build_graph(grouped)
    return nodes, edges, len(tail)


def _digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def pick_blurred(nodes):
    """Ids of the 2-3 tool nodes whose names are hidden.

    Deterministic: candidates are ranked by a hash of the node id, so the choice is the same on every
    rerun and does not depend on counts or order. Never the busiest tool (the most important node), never
    the grouped 'other' tool, and only names long enough to hide behind a blur.
    """
    tools = [n for n in nodes if n.get("kind") == "tool" and n.get("id") != OTHER_ID]
    if not tools:
        return set()
    top = min(tools, key=lambda n: (-(n.get("calls") or 0), -(n.get("runs") or 0), str(n["id"])))
    pool = [n for n in tools if n is not top and len(str(n.get("label") or "").strip()) >= MIN_BLUR_LEN]
    pool.sort(key=lambda n: _digest(str(n["id"])))
    return {n["id"] for n in pool[:3 if len(tools) >= 12 else 2]}


def scramble(text, key):
    """Same-length stand-in for a hidden name: letters and digits become other ones, separators stay."""
    def stream():
        i = 0
        while True:
            yield from hashlib.sha256(("%s|%d" % (key, i)).encode("utf-8")).digest()
            i += 1
    bytes_, out = stream(), []
    for ch in text:
        b = next(bytes_)
        if ch in _KEEP:
            out.append(ch)
        elif ch.isdigit() and ch.isascii():
            out.append(chr(48 + b % 10))
        elif ch.isupper():
            out.append(chr(65 + b % 26))
        else:
            out.append(chr(97 + b % 26))
    return "".join(out)


def r3(v):
    return None if v is None else round(v, 3)


def public_graph(nodes, edges, hidden):
    """Nodes and edges as the page needs them: opaque ids, no real name for a hidden node, score data only."""
    ids = {n["id"]: "n%d" % i for i, n in enumerate(nodes)}  # same order as the cig stage wrote them
    salt = _digest("\n".join(sorted(str(n["id"]) for n in nodes)))  # not in the file: a guessed name cannot be checked
    out_nodes = []
    for n in nodes:
        d = {"id": ids[n["id"]], "kind": n["kind"], "label": str(n.get("label") or ""),
             "calls": n.get("calls") or 0, "runs": n.get("runs") or 0, "score": r3(n.get("mean_outcome")),
             "good": r3(n.get("share_good_runs")), "bad": r3(n.get("share_bad_runs")),
             "err": r3(n.get("fail_rate")), "lat": r3(n.get("p90_duration_s")), "cost": r3(n.get("mean_run_tokens"))}
        if n["id"] in hidden:
            d["label"] = scramble(d["label"], "%s|%s" % (salt, n["id"]))
            d["hidden"] = 1
        out_nodes.append(d)
    out_edges = [{"source": ids[e["source"]], "target": ids[e["target"]], "count": e.get("count") or 0,
                  "runs": e.get("runs") or 0, "score": r3(e.get("mean_outcome")),
                  "err": r3(e.get("fail_rate_target")), "lat": r3(e.get("median_duration_s")),
                  "cost": r3(e.get("mean_run_tokens"))}
                 for e in edges if e.get("source") in ids and e.get("target") in ids]
    return out_nodes, out_edges


def build_payload(cig_dir, url=DEFAULT_URL):
    summary = read_json(os.path.join(cig_dir, "summary.json")) or {}
    nodes, edges, grouped = load_graph(cig_dir)
    out_nodes, out_edges = public_graph(nodes, edges, pick_blurred(nodes))
    counts = summary.get("counts") or {}
    return {
        "url": url,
        "runs": summary.get("n_runs") or 0, "conversations": summary.get("n_conversations") or 0,
        "counts": {k: int(counts.get(k) or 0) for k in ("good", "ambiguous", "bad", "ungraded")},
        "has_scores": bool(summary.get("has_scores")),
        "grouped": grouped, "nodes": out_nodes, "edges": out_edges,
    }


def render(payload, template_path=TEMPLATE):
    with open(template_path, encoding="utf-8") as f:
        tpl = f.read()
    if DATA_MARK not in tpl:
        raise ValueError("template is missing the data marker")
    tpl = tpl.replace(URL_MARK, htmllib.escape(payload["url"], quote=True))  # static links, before any data goes in
    return tpl.replace(DATA_MARK, safe_json(payload), 1)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="plite viewer", description="Build the self-contained cig.html.")
    ap.add_argument("--out", default="./procept-out")
    ap.add_argument("--cig", help="override cig/ directory")
    ap.add_argument("--template", default=TEMPLATE)
    a = ap.parse_args(argv)
    cig_dir = a.cig or os.path.join(a.out, "cig")
    out_dir = os.path.dirname(os.path.abspath(cig_dir)) if a.cig else a.out
    if not os.path.exists(os.path.join(cig_dir, "summary.json")):
        print("error: %s has no CIG; run `cig` first" % cig_dir, file=sys.stderr)
        return 2
    checks.require(out_dir, ["cig"])
    try:
        page = render(build_payload(cig_dir, system5_url(out_dir)), a.template)
    except ValueError as e:
        print("error: %s" % e, file=sys.stderr)
        return 2
    dest = os.path.join(out_dir, "cig.html")
    with open(dest, "w", encoding="utf-8") as f:
        f.write(page)
    print("viewer written to %s (%d KB)" % (dest, len(page) // 1024))
    return 0


if __name__ == "__main__":
    sys.exit(main())
