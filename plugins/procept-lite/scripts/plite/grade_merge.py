"""grade-merge: validate every grader CSV, write <out>/scores.csv and <out>/grading/summary.json.

Checks per shard: header, row ids match the shard exactly, score in {1,0,-1}, confidence in 0..1.
Audit agreement compares blind re-scores with the primary scores (exact share, Cohen's kappa, opposite signs).
--resume only lists shards with no CSV yet (and writes grading/resume_args.json for workflow/grade.js)."""
import argparse
import collections
import csv
import json
import os
import sys

CSV_HEADER = "run_id,outcome_score,confidence,rationale,key_quote,persona,issue,reaction_run_id"
COLS = CSV_HEADER.split(",")
VALID = ("1", "0", "-1")
csv.field_size_limit(10 ** 9)


def kappa(pairs):
    """Cohen's kappa over (a, b) label pairs. Degenerate chance agreement of 1 -> 1.0 if all agree else 0.0."""
    n = len(pairs)
    if n == 0:
        return None
    po = sum(a == b for a, b in pairs) / n
    ca, cb = collections.Counter(a for a, _ in pairs), collections.Counter(b for _, b in pairs)
    pe = sum(ca[k] * cb[k] for k in set(ca) | set(cb)) / (n * n)
    if pe >= 1.0:
        return 1.0 if po == 1.0 else 0.0
    return (po - pe) / (1 - pe)


def opposite_count(pairs):
    return sum(1 for a, b in pairs if {a, b} == {"1", "-1"})


def validate_csv(path, expected_ids):
    """-> (valid_rows_by_id, problems list). Rows individually valid are kept even if the shard has problems."""
    problems, rows = [], {}
    with open(path, encoding="utf-8-sig", newline="") as f:
        rd = csv.reader(f)
        header = next(rd, None)
        if header != COLS:
            problems.append("header is %r, expected %s" % (header, CSV_HEADER))
            return rows, problems
        got = []
        for ln, rec in enumerate(rd, 2):
            if not rec:
                continue
            if len(rec) != len(COLS):
                problems.append("line %d has %d fields, expected %d" % (ln, len(rec), len(COLS)))
                continue
            r = dict(zip(COLS, rec))
            got.append(r["run_id"])
            ok = True
            if r["outcome_score"].strip() not in VALID:
                problems.append("bad score %r for %s" % (r["outcome_score"], r["run_id"]))
                ok = False
            try:
                c = float(r["confidence"])
                if not 0 <= c <= 1:
                    raise ValueError
            except ValueError:
                problems.append("bad confidence %r for %s" % (r["confidence"], r["run_id"]))
                ok = False
            if r["run_id"] not in expected_ids:
                ok = False
            elif r["run_id"] in rows:
                ok = False
            if ok:
                r["outcome_score"] = r["outcome_score"].strip()
                rows[r["run_id"]] = r
        cnt = collections.Counter(got)
        missing = [i for i in expected_ids if i not in cnt]
        extra = [i for i in cnt if i not in expected_ids]
        dup = [i for i, c in cnt.items() if c > 1]
        if missing:
            problems.append("%d missing run ids (e.g. %s)" % (len(missing), missing[0]))
        if extra:
            problems.append("%d unexpected run ids (e.g. %s)" % (len(extra), extra[0]))
        if dup:
            problems.append("%d duplicated run ids (e.g. %s)" % (len(dup), dup[0]))
    return rows, problems


def load_set(g, index_name, sub):
    """-> rows by run_id, problems {shard: [..]}, missing shard names, entries"""
    p = os.path.join(g, index_name)
    if not os.path.exists(p):
        return {}, {}, [], []
    with open(p, encoding="utf-8") as f:
        idx = json.load(f)
    rows, problems, missing = {}, {}, []
    for x in idx:
        path = os.path.join(g, sub, x["shard"] + ".csv")
        if not os.path.exists(path):
            missing.append(x["shard"])
            continue
        r, pr = validate_csv(path, x["run_ids"])
        for rid, row in r.items():
            row["_shard"] = x["shard"]
            rows[rid] = row
        if pr:
            problems[x["shard"]] = pr
    return rows, problems, missing, idx


def canon_personas(rows):
    """Collapse whitespace and case variants of persona labels onto the first-seen spelling."""
    seen = {}
    for r in rows:
        p = " ".join((r.get("persona") or "").split())
        r["persona"] = seen.setdefault(p.lower(), p) if p else ""
        r["issue"] = " ".join((r.get("issue") or "").split())


def merge(out, resume=False):
    out = os.path.abspath(out)
    g = os.path.join(out, "grading")
    if not os.path.exists(os.path.join(g, "shards_index.json")):
        raise SystemExit("error: %s/shards_index.json not found; run grade-prepare first" % g)
    S, p1, miss1, idx = load_set(g, "shards_index.json", "scores")
    A, p2, miss2, aidx = load_set(g, "audit_index.json", "audit")
    if resume:
        args_p = os.path.join(g, "workflow_args.json")
        wf = json.load(open(args_p, encoding="utf-8")) if os.path.exists(args_p) else {}
        redo = set(miss1) | set(miss2)
        res = {"brief": wf.get("brief"),
               "shards": [s for s in wf.get("shards", []) if s["shard"] in redo],
               "audits": [s for s in wf.get("audits", []) if s["shard"] in redo]}
        with open(os.path.join(g, "resume_args.json"), "w", encoding="utf-8") as f:
            json.dump(res, f, indent=1)
        return {"missing_shards": miss1, "missing_audits": miss2}, 0

    expected = [rid for x in idx for rid in x["run_ids"]]
    rows = [S[rid] for rid in expected if rid in S]
    canon_personas(rows)
    with open(os.path.join(out, "scores.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, COLS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    counts = collections.Counter(r["outcome_score"] for r in rows)
    personas = collections.defaultdict(lambda: [0, 0])
    for r in rows:
        if r["persona"]:
            personas[r["persona"]][0] += 1
            personas[r["persona"]][1] += int(r["outcome_score"])
    pairs = [(S[k]["outcome_score"], A[k]["outcome_score"]) for k in A if k in S]
    audit = {"n": len(pairs), "exact_share": None, "kappa": None, "opposite_sign": 0}
    if pairs:
        audit.update(exact_share=round(sum(a == b for a, b in pairs) / len(pairs), 4),
                     kappa=round(kappa(pairs), 4), opposite_sign=opposite_count(pairs))
    warnings = []
    no_persona = sum(1 for r in rows if not r["persona"])
    if no_persona:
        warnings.append("%d rows have no persona" % no_persona)
    bad_issue = sum(1 for r in rows if r["outcome_score"] == "1" and r["issue"])
    if bad_issue:
        warnings.append("%d +1 rows carry an issue label" % bad_issue)
    summary = {
        "runs_expected": len(expected), "runs_scored": len(rows),
        "counts": {"good": counts.get("1", 0), "ambiguous": counts.get("0", 0), "bad": counts.get("-1", 0)},
        "share": {k: round(v / len(rows), 4) for k, v in
                  (("good", counts.get("1", 0)), ("ambiguous", counts.get("0", 0)), ("bad", counts.get("-1", 0)))} if rows else {},
        "personas": sorted(({"persona": p, "runs": c[0], "mean_score": round(c[1] / c[0], 3)} for p, c in personas.items()),
                           key=lambda d: -d["runs"]),
        "audit": audit,
        "problems": {**p1, **{k: v for k, v in p2.items()}},
        "missing_shards": miss1, "missing_audits": miss2,
        "rerun_shards": sorted(set(p1) | set(miss1)),
        "rerun_audits": sorted(set(p2) | set(miss2)),
        "warnings": warnings,
    }
    with open(os.path.join(g, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=1)
    return summary, (0 if not summary["rerun_shards"] else 1)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="plite grade-merge", description=__doc__.split("\n")[0])
    ap.add_argument("--out", default="./procept-out")
    ap.add_argument("--resume", action="store_true", help="only list shards with no CSV yet")
    a = ap.parse_args(argv)
    s, rc = merge(a.out, a.resume)
    if a.resume:
        print("missing score shards: %s" % (", ".join(s["missing_shards"]) or "none"))
        print("missing audit batches: %s" % (", ".join(s["missing_audits"]) or "none"))
        return 0
    print("scored %d of %d runs: good %d, ambiguous %d, bad %d" % (
        s["runs_scored"], s["runs_expected"], s["counts"]["good"], s["counts"]["ambiguous"], s["counts"]["bad"]))
    au = s["audit"]
    if au["n"]:
        print("audit: n=%d exact=%.0f%% kappa=%.2f opposite-sign=%d" % (au["n"], au["exact_share"] * 100, au["kappa"], au["opposite_sign"]))
    for sh, pr in s["problems"].items():
        print("PROBLEM %s: %s" % (sh, "; ".join(pr[:3])))
    if s["rerun_shards"]:
        print("re-run shards: %s" % ", ".join(s["rerun_shards"]), file=sys.stderr)
    if s["rerun_audits"]:
        print("re-run audit batches: %s" % ", ".join(s["rerun_audits"]))
    for w in s["warnings"]:
        print("warning: %s" % w)
    return rc


if __name__ == "__main__":
    sys.exit(main())
