"""checks: stage sign-offs written by a smart model, and the gates that read them.

The scripts in this plugin are starters. Before a stage's output is used, a model that meets the floor in
templates/models.json (Sonnet 5.5 or GPT 5.6 Terra, or better; never Haiku) checks it against the real data and signs
it off. A sign-off is a JSON record at <out>/checks/<stage>.json. A gate refuses to run the next stage until the record
exists, passes, and still matches the artifact it was written for (inputs_hash).

  python3 plite.py check record --out DIR --file record.json    validate, stamp and write a sign-off
  python3 plite.py check show   --out DIR [--stage S]
  python3 plite.py status       --out DIR

Record the verifying model writes:
  {"stage": "ingest|merge|cig|study|scorecard", "model": "<its own model id>", "verdict": "pass|fail",
   "checked": int, "mismatches": int, "notes": str,
   "evidence": [{"id": "<run id or item>", "finding": str}, ...],
   "unusable": [{"run_id": str, "reason": str}]      (ingest only: runs that cannot be trusted; kept out of everything)
   "overridden": int}                                   (merge only: rows corrected through grading/overrides.csv)
Evidence ids must exist: for ingest they are run ids in normalized/runs.jsonl, for merge run ids in the shard CSVs.
"""
# Dev/test bypass: PLITE_NO_GATE=1 (starter behaviour, no sign-offs needed). Never offered to users.
import csv
import glob
import hashlib
import json
import os
import re
import sys
import time

STAGES = ("ingest", "merge", "cig", "study", "scorecard")
MIN_CHECKED = {"ingest": 30, "merge": 40, "cig": 5, "study": 1, "scorecard": 1}
MAX_MISMATCH_SHARE = {"ingest": 0.0, "merge": 0.15, "cig": 0.0, "study": 0.0, "scorecard": 0.0}
ARTIFACTS = {
    "ingest": ["mapping.json", "normalized/runs.jsonl"],
    "merge": ["grading/scores/*.csv"],
    "cig": ["cig/runs.csv", "cig/nodes.csv", "cig/edges.csv"],
    "study": ["scores.csv", "cig/runs.csv"],
    "scorecard": ["scorecard.html"],
}
UNUSABLE_LEDGER = os.path.join("normalized", "unusable.csv")  # run_id,reason,source(auto|model); written by grade-prepare
csv.field_size_limit(10 ** 9)

_DEFAULT_CFG = {"allow": ["sonnet-5-5", "opus-5-5", "fable-5-1", "gpt-5-6-terra"],
                "deny": ["haiku", "mini", "nano"]}


def gates_disabled():
    return os.environ.get("PLITE_NO_GATE") == "1"


# ---------------------------------------------------------------- model floor

def _norm_model(s):
    """'GPT 5.6 Terra' / 'gpt-5.6-terra' / 'GPT5.6_Terra' -> 'gpt-5-6-terra'."""
    t = re.sub(r"[\s_.]+", "-", str(s if s is not None else "").lower())
    t = re.sub(r"(?<=[a-z])(?=\d)|(?<=\d)(?=[a-z])", "-", t)
    return re.sub(r"-{2,}", "-", t).strip("-")


def _models_cfg():
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "templates", "models.json")
    try:
        with open(p, encoding="utf-8") as f:
            cfg = json.load(f)
        allow, deny = cfg.get("allow"), cfg.get("deny")
        if (isinstance(allow, list) and allow and all(isinstance(x, str) for x in allow)
                and isinstance(deny, list) and all(isinstance(x, str) for x in deny)):
            return {"allow": allow, "deny": deny}
    except (OSError, ValueError, AttributeError, RecursionError):
        pass
    return dict(_DEFAULT_CFG)


def model_allowed(name):
    """True for Sonnet 5.5, GPT 5.6 Terra, Opus 5.5, Fable 5.1 (and whatever else templates/models.json allows);
    False for Haiku, mini, nano, Sonnet 4.x and anything unknown, empty or not a string."""
    if not isinstance(name, str):
        return False
    n = _norm_model(name)
    if not n:
        return False
    cfg = _models_cfg()
    if any(d for d in map(_norm_model, cfg["deny"]) if d and d in n):
        return False
    return any(a for a in map(_norm_model, cfg["allow"]) if a and a in n)


# ---------------------------------------------------------------- files

def atomic_write(path, text):
    """Write text to path via a temp file in the same folder, then rename: readers never see a partial file."""
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, ".%s.%d.%s.tmp" % (os.path.basename(path), os.getpid(), os.urandom(4).hex()))
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as f:  # a plain open() keeps the user's umask
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def _expand(out, pattern):
    return sorted(p for p in glob.glob(os.path.join(out, pattern)) if os.path.isfile(p))


def artifact_hash(out, stage):
    """sha256 over the (relative path, bytes) of every artifact the stage's sign-off covers."""
    h = hashlib.sha256()
    for pat in ARTIFACTS[stage]:
        files = _expand(out, pat)
        if not files:
            h.update(("missing:" + pat).encode())
        for p in files:
            rel = os.path.relpath(p, out)
            h.update(rel.encode())
            try:
                with open(p, "rb") as f:
                    for chunk in iter(lambda: f.read(1 << 20), b""):
                        h.update(chunk)
            except OSError:
                h.update(("unreadable:" + rel).encode())
    return h.hexdigest()


def _lines(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return [ln for ln in f if ln.strip()]
    except OSError:
        return []


def _run_ids(out):
    """Run ids in normalized/runs.jsonl."""
    ids = set()
    for ln in _lines(os.path.join(out, "normalized", "runs.jsonl")):
        try:
            ids.add(str(json.loads(ln)["run_id"]))
        except (ValueError, KeyError, TypeError, RecursionError):
            pass
    return ids


def _scored_ids(out):
    """Run ids in the shard CSVs (grading/scores/*.csv)."""
    ids = set()
    for p in _expand(out, "grading/scores/*.csv"):
        try:
            with open(p, encoding="utf-8-sig", newline="") as f:
                for row in csv.DictReader(f):
                    rid = (row.get("run_id") or "").strip()
                    if rid:
                        ids.add(rid)
        except (OSError, ValueError, csv.Error):
            pass
    return ids


def available(out, stage):
    if stage == "ingest":
        return len(_lines(os.path.join(out, "normalized", "runs.jsonl")))
    if stage == "merge":
        n = 0
        for p in _expand(out, "grading/scores/*.csv"):
            n += max(0, len(_lines(p)) - 1)
        return n
    return 0


def path_for(out, stage):
    return os.path.join(out, "checks", stage + ".json")


def load(out, stage):
    """The stage's sign-off record, or None. A missing, corrupt, partial or wrong-stage file counts as missing."""
    try:
        with open(path_for(out, stage), encoding="utf-8") as f:
            rec = json.load(f)
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(rec, dict) or rec.get("stage") != stage:
        return None
    return rec


# ---------------------------------------------------------------- validation and recording

def _is_int(x):
    return isinstance(x, int) and not isinstance(x, bool)


def validate(out, rec):
    """-> list of problems (empty = acceptable)."""
    bad = []
    if not isinstance(rec, dict):
        return ["record must be a JSON object"]
    stage = rec.get("stage")
    if stage not in STAGES:
        return ["stage must be one of %s" % ", ".join(STAGES)]
    if not model_allowed(rec.get("model")):
        bad.append("model %r is below the floor (Sonnet 5.5 or GPT 5.6 Terra, or better; never Haiku); see templates/models.json"
                   % (rec.get("model"),))
    verdict = rec.get("verdict")
    if verdict not in ("pass", "fail"):
        bad.append("verdict must be 'pass' or 'fail'")
    checked, mism = rec.get("checked"), rec.get("mismatches")
    if not _is_int(checked) or checked < 0:
        bad.append("checked must be a non-negative integer")
        checked = 0
    if not _is_int(mism) or mism < 0:
        bad.append("mismatches must be a non-negative integer")
        mism = 0
    if mism > checked:
        bad.append("mismatches (%d) cannot exceed checked (%d)" % (mism, checked))
    if "notes" in rec and rec["notes"] is not None and not isinstance(rec["notes"], str):
        bad.append("notes must be a string")
    avail = available(out, stage)
    if stage in ("ingest", "merge") and not avail:
        bad.append("nothing to sign off yet: %s is missing or empty" %
                   ("normalized/runs.jsonl" if stage == "ingest" else "grading/scores/*.csv"))
    need = min(MIN_CHECKED[stage], avail) if avail else MIN_CHECKED[stage]
    if verdict != "fail" and checked < need:  # a FAIL may stop early: the first real mismatch is enough to record it
        bad.append("checked %d items but %s needs at least %d" % (checked, stage, need))
    ev = rec.get("evidence")
    if not isinstance(ev, list) or any(not isinstance(e, dict) or not str(e.get("id", "")).strip()
                                       or not str(e.get("finding", "")).strip() for e in ev):
        bad.append("evidence must be a list of {id, finding} with both filled in")
    else:
        if len(ev) < min(5, max(checked, 1)):
            bad.append("evidence needs at least %d entries (one per item you looked at, up to 5)" % min(5, max(checked, 1)))
        if len(ev) >= 2 and len({str(e["finding"]).strip() for e in ev}) < 2:
            bad.append("evidence findings are identical; write what you actually saw in each item")
        if stage in ("ingest", "merge") and avail:
            known = _run_ids(out) if stage == "ingest" else _scored_ids(out)
            where = "normalized/runs.jsonl" if stage == "ingest" else "the shard CSVs in grading/scores/"
            unknown = [str(e["id"]).strip() for e in ev if str(e["id"]).strip() not in known]
            if unknown:
                bad.append("evidence id %r is not a run id in %s%s; cite runs you actually looked at"
                           % (unknown[0], where, " (+%d more)" % (len(unknown) - 1) if len(unknown) > 1 else ""))
    if verdict == "pass" and checked and mism > MAX_MISMATCH_SHARE[stage] * checked:
        bad.append("a pass allows at most %d%% mismatches; fix the cause and re-check, or record a fail"
                   % round(100 * MAX_MISMATCH_SHARE[stage]))
    unusable = rec.get("unusable")
    if unusable not in (None, []):
        if stage != "ingest":
            bad.append("unusable is only valid on the ingest sign-off")
        elif not isinstance(unusable, list):
            bad.append("unusable must be a list of {run_id, reason}")
        else:
            ids = _run_ids(out)
            for u in unusable:
                if not isinstance(u, dict) or not str(u.get("run_id", "")).strip() or not str(u.get("reason", "")).strip():
                    bad.append("each unusable entry needs run_id and reason")
                    break
                if ids and str(u["run_id"]).strip() not in ids:
                    bad.append("unusable run_id %r is not in normalized/runs.jsonl" % u["run_id"])
                    break
    ov = rec.get("overridden")
    if ov is not None and (stage != "merge" or not _is_int(ov) or ov < 0):
        bad.append("overridden must be a non-negative integer and only belongs on the merge sign-off")
    return bad


def record(out, rec):
    """Validate, stamp (inputs_hash, recorded_at) and atomically write <out>/checks/<stage>.json. -> its path."""
    problems = validate(out, rec)
    if problems:
        raise ValueError("; ".join(problems))
    rec = dict(rec)
    rec["inputs_hash"] = artifact_hash(out, rec["stage"])
    rec["recorded_at"] = int(time.time())
    return atomic_write(path_for(out, rec["stage"]), json.dumps(rec, indent=1))


def state(out, stage):
    """missing | failed | stale | verified. A FAIL written for data that has since changed reads as stale (re-check it)."""
    rec = load(out, stage)
    if rec is None:
        return "missing"
    if rec.get("inputs_hash") != artifact_hash(out, stage):
        return "stale"
    if rec.get("verdict") != "pass":
        return "failed"
    return "verified"


def status(out):
    return {s: state(out, s) for s in STAGES}


def require(out, stages):
    """Gate: SystemExit (one line) unless every stage is verified (pass, and unchanged since it was signed off)."""
    if gates_disabled():
        return
    for s in stages:
        if s not in STAGES:
            raise ValueError("unknown stage %r" % s)
        st = state(out, s)
        if st == "verified":
            continue
        why = {"missing": "has no sign-off", "failed": "was signed off as FAIL",
               "stale": "changed after it was signed off"}[st]
        todo = ("fix the cause and re-run `plite.py run`, then have a Sonnet 5.5 / GPT 5.6 Terra (or better) model check it "
                "again" if st == "failed" else
                "have a Sonnet 5.5 / GPT 5.6 Terra (or better) model check it against the real data")
        raise SystemExit("error: stage '%s' %s. Next: %s, then run: plite.py check record --out %s --file <record.json> "
                         "(writes %s)" % (s, why, todo, out, path_for(out, s)))


def unusable_ids(out):
    """{run_id: reason}: runs kept out of grading, the CIG and the scorecard (ledger + the ingest sign-off)."""
    res = {}
    try:
        with open(os.path.join(out, UNUSABLE_LEDGER), encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                rid = (r.get("run_id") or "").strip()
                if rid:
                    res[rid] = (r.get("reason") or "").strip() or "unusable"
    except (OSError, ValueError, csv.Error):
        pass
    rec = load(out, "ingest") or {}
    lst = rec.get("unusable")
    for u in lst if isinstance(lst, list) else []:
        if isinstance(u, dict) and str(u.get("run_id") or "").strip():
            res.setdefault(str(u["run_id"]).strip(), str(u.get("reason") or "").strip() or "unusable")
    return res


# ---------------------------------------------------------------- command line

def _arg(rest, flag, default=None):
    if flag in rest and rest.index(flag) + 1 < len(rest):
        return rest[rest.index(flag) + 1]
    return default


def main(argv=None):
    argv = list(argv or [])
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0 if argv else 2
    sub, rest = argv[0], argv[1:]
    out = os.path.abspath(_arg(rest, "--out", "./procept-out"))
    if sub == "record":
        f = _arg(rest, "--file")
        if not f:
            print("error: check record needs --file record.json", file=sys.stderr)
            return 2
        try:
            with open(f, encoding="utf-8-sig") as fh:
                rec = json.load(fh)
            p = record(out, rec)
        except (OSError, ValueError, RecursionError) as e:
            print("error: %s" % e, file=sys.stderr)
            return 2
        print("recorded %s" % p)
        return 0
    if sub == "show":
        stg = _arg(rest, "--stage")
        if stg and stg not in STAGES:
            print("error: stage must be one of %s" % ", ".join(STAGES), file=sys.stderr)
            return 2
        for s in ([stg] if stg else STAGES):
            r = load(out, s)
            print("%-10s %s" % (s, "none" if r is None else json.dumps({k: r[k] for k in r if k not in ("evidence", "unusable")})))
        return 0
    print("error: unknown check subcommand %r (record|show)" % sub, file=sys.stderr)
    return 2


def status_main(argv=None):
    argv = list(argv or [])
    out = os.path.abspath(_arg(argv, "--out", "./procept-out"))
    for s, st in status(out).items():
        print("%-10s %s" % (s, st))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
