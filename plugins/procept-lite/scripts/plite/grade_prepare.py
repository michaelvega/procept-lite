"""grade-prepare: shard normalized runs for blind outcome grading by cheap reader agents.

Reads <out>/normalized/runs.jsonl. Writes under <out>/grading/: brief.md, shards/shard_NN.jsonl,
audit/audit_NN.jsonl, shards_index.json, audit_index.json, workflow_args.json, scores/ (empty).
A conversation is never split across shards. Dossiers carry no vendor scores (blind)."""
import argparse
import json
import os
import sys

CSV_HEADER = "run_id,outcome_score,confidence,rationale,key_quote,persona,issue,reaction_run_id"
HERE = os.path.dirname(os.path.abspath(__file__))
BRIEF_TEMPLATE = os.path.join(HERE, "..", "..", "skills", "procept-lite", "references", "grading-brief.md")


def load_runs(path):
    runs = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                runs.append(json.loads(line))
    return runs


def group_conversations(runs):
    """-> ordered list of conversations; each a list of enriched runs (time order)."""
    by_conv = {}
    for r in runs:
        by_conv.setdefault(r.get("conversation_id") or r["run_id"], []).append(r)
    for v in by_conv.values():
        v.sort(key=lambda r: (r.get("first_ts") or "", r["run_id"]))
    order = sorted(by_conv, key=lambda c: (by_conv[c][0].get("first_ts") or "", c))
    convs = []
    for c in order:
        seq = by_conv[c]
        enriched = []
        for i, r in enumerate(seq):
            out = dict(r)
            nxt = seq[i + 1] if i + 1 < len(seq) else None
            out["earlier_user_messages"] = [x.get("user_message") for x in seq[:i]]
            out["next_run_id"] = nxt["run_id"] if nxt else None
            out["next_user_message"] = nxt.get("user_message") if nxt else None
            enriched.append(out)
        convs.append(enriched)
    return convs


def make_shards(convs, max_runs=20, max_chars=230000):
    """Greedy packing of whole conversations. An oversize conversation gets a shard of its own."""
    shards, cur, size = [], [], 0
    for runs in convs:
        n = sum(len(json.dumps(r, ensure_ascii=False)) for r in runs)
        if cur and (len(cur) + len(runs) > max_runs or size + n > max_chars):
            shards.append(cur)
            cur, size = [], 0
        cur += runs
        size += n
    if cur:
        shards.append(cur)
    return shards


def audit_ids(ids, every):
    """Every Nth run of a shard (the Nth, 2Nth, ...)."""
    return ids[every - 1::every] if every > 0 else []


def batch(items, size):
    return [items[i:i + size] for i in range(0, len(items), size)]


def _write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _clear(d, prefix, ext):
    for f in os.listdir(d):
        if f.startswith(prefix) and f.endswith(ext):
            os.remove(os.path.join(d, f))


def prepare(out, max_runs=20, max_chars=230000, audit_every=4, audit_batch=20, product=None,
            price_per_mtok=None, force=False):
    out = os.path.abspath(out)
    src = os.path.join(out, "normalized", "runs.jsonl")
    if not os.path.exists(src):
        raise SystemExit("error: %s not found; run `normalize` first" % src)
    runs = load_runs(src)
    if not runs:
        raise SystemExit("error: %s has no runs" % src)
    g = os.path.join(out, "grading")
    dirs = {k: os.path.join(g, k) for k in ("shards", "scores", "audit")}
    for d in dirs.values():
        os.makedirs(d, exist_ok=True)
    existing = [f for f in os.listdir(dirs["scores"]) if f.endswith(".csv")] + \
               [f for f in os.listdir(dirs["audit"]) if f.endswith(".csv")]
    if existing and not force:
        raise SystemExit("error: %s already holds %d graded CSVs; re-preparing would orphan them. "
                         "Pass --force to discard, or use grade-merge --resume." % (g, len(existing)))
    for f in existing:
        for d in (dirs["scores"], dirs["audit"]):
            p = os.path.join(d, f)
            if os.path.exists(p):
                os.remove(p)
    _clear(dirs["shards"], "shard_", ".jsonl")
    _clear(dirs["audit"], "audit_", ".jsonl")

    shards = make_shards(group_conversations(runs), max_runs, max_chars)
    index, all_runs = [], {}
    for i, sh in enumerate(shards, 1):
        name = "shard_%02d" % i
        _write_jsonl(os.path.join(dirs["shards"], name + ".jsonl"), sh)
        ids = [r["run_id"] for r in sh]
        index.append({"shard": name, "n": len(ids), "run_ids": ids, "audit_ids": audit_ids(ids, audit_every)})
        for r in sh:
            all_runs[r["run_id"]] = r
    aud = [rid for x in index for rid in x["audit_ids"]]
    aindex = []
    for j, ids in enumerate(batch(aud, audit_batch), 1):
        name = "audit_%02d" % j
        _write_jsonl(os.path.join(dirs["audit"], name + ".jsonl"), [all_runs[r] for r in ids])
        aindex.append({"shard": name, "n": len(ids), "run_ids": ids})
    with open(os.path.join(g, "shards_index.json"), "w", encoding="utf-8") as f:
        json.dump(index, f, indent=1)
    with open(os.path.join(g, "audit_index.json"), "w", encoding="utf-8") as f:
        json.dump(aindex, f, indent=1)

    with open(BRIEF_TEMPLATE, encoding="utf-8") as f:
        brief = f.read()
    brief = brief.replace("{{PRODUCT}}", product or "infer it from the runs").replace("{{CSV_HEADER}}", CSV_HEADER)
    brief_path = os.path.join(g, "brief.md")
    with open(brief_path, "w", encoding="utf-8") as f:
        f.write(brief)

    def entry(x, kind, sub):
        return {"shard": x["shard"], "n": x["n"],
                "file": os.path.join(dirs[sub], x["shard"] + ".jsonl"),
                "csv": os.path.join(dirs["scores" if kind == "shard" else "audit"], x["shard"] + ".csv")}
    wf = {"brief": brief_path,
          "shards": [entry(x, "shard", "shards") for x in index],
          "audits": [entry(x, "audit", "audit") for x in aindex]}
    with open(os.path.join(g, "workflow_args.json"), "w", encoding="utf-8") as f:
        json.dump(wf, f, indent=1)

    chars = sum(os.path.getsize(e["file"]) for e in wf["shards"] + wf["audits"])
    n_jobs = len(wf["shards"]) + len(wf["audits"])
    tokens = (chars + len(brief) * n_jobs) // 4
    est = {"runs": len(runs), "shards": len(index), "audit_batches": len(aindex), "audit_runs": len(aud),
           "approx_input_tokens": tokens, "approx_usd": None, "price_per_mtok": price_per_mtok,
           "note": "input only, chars/4; readers re-read context and write output, so treat as a lower bound"}
    if price_per_mtok is not None:
        est["approx_usd"] = round(tokens / 1e6 * price_per_mtok, 4)
    with open(os.path.join(g, "cost_estimate.json"), "w", encoding="utf-8") as f:
        json.dump(est, f, indent=1)
    return est, wf


def main(argv=None):
    ap = argparse.ArgumentParser(prog="plite grade-prepare", description=__doc__.split("\n")[0])
    ap.add_argument("--out", default="./procept-out")
    ap.add_argument("--max-runs", type=int, default=20)
    ap.add_argument("--max-chars", type=int, default=230000)
    ap.add_argument("--audit-every", type=int, default=4, help="audit every Nth run of each shard (0 = none)")
    ap.add_argument("--audit-batch", type=int, default=20)
    ap.add_argument("--product", default=None, help="one-line product description for the readers")
    ap.add_argument("--price-per-mtok", type=float, default=None, help="USD per million input tokens of the cheap model")
    ap.add_argument("--force", action="store_true", help="discard existing graded CSVs")
    a = ap.parse_args(argv)
    est, wf = prepare(a.out, a.max_runs, a.max_chars, a.audit_every, a.audit_batch, a.product,
                      a.price_per_mtok, a.force)
    print("grading prepared: %d runs -> %d shards + %d audit batches (%d audit runs)"
          % (est["runs"], est["shards"], est["audit_batches"], est["audit_runs"]))
    print("approx input tokens: %d%s" % (est["approx_input_tokens"],
          "  approx $%.4f" % est["approx_usd"] if est["approx_usd"] is not None else "  (pass --price-per-mtok for $)"))
    print("brief: %s" % wf["brief"])
    print("workflow args: %s" % os.path.join(os.path.dirname(wf["brief"]), "workflow_args.json"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
