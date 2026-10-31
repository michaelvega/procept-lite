"""grade-merge: validate every grader CSV, write <out>/scores.csv and <out>/grading/summary.json.

Checks per shard: header, row ids match the shard exactly, score in {1,0,-1}, confidence in 0..1.
summary.json also lists the rows whose key_quote is not found verbatim (whitespace-normalised) in that run's dossier
text (quote_not_found), and the share of blind re-scores that agree with the primary scores (internal numbers).
<out>/grading/overrides.csv (run_id,outcome_score,rationale,reason), written by the merge verifier, is applied on top of
the shard rows when scores.csv is written (overrides_applied). The merge sign-off hashes the shard CSVs only, so
applying overrides never makes it stale.
--resume only lists shards with no CSV yet (and writes grading/resume_args.json for workflow/grade.js)."""
import argparse
import collections
import csv
import hashlib
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import checks  # noqa: E402

CSV_HEADER = "run_id,outcome_score,confidence,rationale,key_quote,persona,issue,reaction_run_id"
COLS = CSV_HEADER.split(",")
VALID = ("1", "0", "-1")
csv.field_size_limit(10 ** 9)


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
        try:
            r, pr = validate_csv(path, x["run_ids"])
        except (OSError, ValueError, csv.Error) as e:  # unreadable bytes: the shard is simply not usable yet
            r, pr = {}, ["the CSV could not be read (%s)" % type(e).__name__]
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


# ---------------------------------------------------------------- quotes

def _squash(s):
    return " ".join(str(s).split())


def _strings(o, acc, depth=0):
    if isinstance(o, str):
        if o.strip():
            acc.append(_squash(o))
    elif depth < 24 and isinstance(o, dict):
        for v in o.values():
            _strings(v, acc, depth + 1)
    elif depth < 24 and isinstance(o, list):
        for v in o:
            _strings(v, acc, depth + 1)


def quote_not_found(g, idx, rows):
    """run ids (in shard order) whose key_quote is not found verbatim, whitespace-normalised, in the text of that run's
    dossier (the shard line the grader read: the run plus its conversation context). Empty quotes are not checked."""
    wanted = {r["run_id"]: _squash(r.get("key_quote") or "") for r in rows}
    wanted = {k: v for k, v in wanted.items() if v}
    missing = []
    for x in idx:
        ids = [i for i in x["run_ids"] if i in wanted]
        if not ids:
            continue
        texts = {}
        try:
            with open(os.path.join(g, "shards", x["shard"] + ".jsonl"), encoding="utf-8") as f:
                for line in f:
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(d, dict) and d.get("run_id") in wanted:
                        acc = []
                        _strings(d, acc)
                        texts[d["run_id"]] = acc
        except OSError:
            pass
        for rid in ids:
            if not any(wanted[rid] in t for t in texts.get(rid, ())):
                missing.append(rid)
    return missing


# ---------------------------------------------------------------- overrides

def file_sha(path):
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return ""


def load_overrides(g, known_ids):
    """-> ({run_id: row}, [problems]). Rows with an unknown run id or a score outside {1,0,-1} are listed and ignored."""
    path = os.path.join(g, "overrides.csv")
    if not os.path.isfile(path):
        return {}, []
    out, bad = {}, []
    try:
        with open(path, encoding="utf-8-sig", newline="") as f:
            rd = csv.DictReader(f)
            if not rd.fieldnames or "run_id" not in rd.fieldnames or "outcome_score" not in rd.fieldnames:
                return {}, ["overrides.csv needs the columns run_id,outcome_score,rationale,reason"]
            for ln, r in enumerate(rd, 2):
                rid = (r.get("run_id") or "").strip()
                sc = (r.get("outcome_score") or "").strip()
                if not rid and not sc:
                    continue
                if rid not in known_ids:
                    bad.append("line %d: run id %r is not a scored run" % (ln, rid))
                elif sc not in VALID:
                    bad.append("line %d: score %r for %s is not 1, 0 or -1" % (ln, sc, rid))
                else:
                    out[rid] = {"outcome_score": sc, "rationale": " ".join((r.get("rationale") or "").split()),
                                "reason": " ".join((r.get("reason") or "").split())}
    except (OSError, ValueError, csv.Error) as e:
        return {}, ["overrides.csv could not be read (%s)" % type(e).__name__]
    return out, bad


def apply_overrides(rows, ov):
    """Overrides replace the score (and the rationale when given) of the shard row; a run moved to +1 loses its issue
    label. -> [{run_id, from, to, reason}] for the runs that were overridden."""
    done = []
    for r in rows:
        o = ov.get(r["run_id"])
        if o is None:
            continue
        done.append({"run_id": r["run_id"], "from": r["outcome_score"], "to": o["outcome_score"], "reason": o["reason"]})
        r["outcome_score"] = o["outcome_score"]
        if o["rationale"]:
            r["rationale"] = o["rationale"]
        if o["outcome_score"] == "1":
            r["issue"] = ""
    return done


# ---------------------------------------------------------------- merge

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
    quote_missing = quote_not_found(g, idx, rows)
    ov, ov_bad = load_overrides(g, {r["run_id"] for r in rows})
    overridden = apply_overrides(rows, ov)
    buf = io.StringIO()
    w = csv.DictWriter(buf, COLS, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow(r)
    checks.atomic_write(os.path.join(out, "scores.csv"), buf.getvalue())
    counts = collections.Counter(r["outcome_score"] for r in rows)
    personas = collections.defaultdict(lambda: [0, 0])
    for r in rows:
        if r["persona"]:
            personas[r["persona"]][0] += 1
            personas[r["persona"]][1] += int(r["outcome_score"])
    pairs = [(S[k]["outcome_score"], A[k]["outcome_score"]) for k in A if k in S]
    audit = {"n": len(pairs), "exact_share": None, "opposite_sign": 0}
    if pairs:
        audit.update(exact_share=round(sum(a == b for a, b in pairs) / len(pairs), 4), opposite_sign=opposite_count(pairs))
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
        "quote_not_found": quote_missing,
        "overrides_applied": len(overridden),
        "overrides_detail": overridden,
        "overrides_invalid": ov_bad,
        "problems": {**p1, **{k: v for k, v in p2.items()}},
        "missing_shards": miss1, "missing_audits": miss2,
        "rerun_shards": sorted(set(p1) | set(miss1)),
        "rerun_audits": sorted(set(p2) | set(miss2)),
        "warnings": warnings,
        "inputs_hash": checks.artifact_hash(out, "merge"),
        "overrides_hash": file_sha(os.path.join(g, "overrides.csv")),
    }
    checks.atomic_write(os.path.join(g, "summary.json"), json.dumps(summary, indent=1))
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
        print("audit: n=%d exact=%.0f%% opposite-sign=%d" % (au["n"], au["exact_share"] * 100, au["opposite_sign"]))
    if s["quote_not_found"]:
        print("key_quote not found verbatim in the run: %d rows (e.g. %s)" % (len(s["quote_not_found"]), s["quote_not_found"][0]))
    if s["overrides_applied"]:
        print("overrides applied: %d" % s["overrides_applied"])
    for pr in s["overrides_invalid"][:3]:
        print("warning: %s" % pr)
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
