"""run: the pipeline driver, a state machine. Idempotent: it does the next step that CODE can do, writes
<out>/status.json and ends with exactly one line

  NEXT: <step> | <instruction>

where step is one of: mapping_rescue, ingest_verify, grade, merge_verify, cig_verify, study, scorecard_verify, done.
A model (Sonnet 5.5 / GPT 5.6 Terra or better) does that step, then you run this command again.

  python3 plite.py run --src FILE_OR_DIR... [--out DIR] [--map M] [--prices P] [--format F] [--sample 300]

Order: no mapping.json -> scaffold, NEXT mapping_rescue | mapping but no (or stale) runs -> normalize + inspect, NEXT
ingest_verify | ingest not signed off -> NEXT ingest_verify | signed off and no shards -> grade-prepare, NEXT grade |
shard CSVs missing -> NEXT grade | all CSVs and no (or stale) scores.csv -> grade-merge, NEXT merge_verify | merge not
signed off -> NEXT merge_verify | signed off -> cig, NEXT cig_verify | cig signed off and no study -> study-init,
NEXT study | study signed off and the problem write-up valid -> viewer + report, NEXT scorecard_verify | scorecard
signed off -> NEXT done.
Exit 0 whenever a NEXT line is printed; 2 when the input is empty or unreadable (one-line message)."""
import argparse
import importlib
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))  # so that plite.study imports like the dispatcher does
import check_logs  # noqa: E402
import checks  # noqa: E402
import cleaning as CL  # noqa: E402
import common  # noqa: E402
import grade_merge as GM  # noqa: E402
import grade_prepare as GP  # noqa: E402
import normalize as N  # noqa: E402
import scaffold as SCAF  # noqa: E402
from common import add_ingest_args, redact  # noqa: E402

PLUGIN = os.path.abspath(os.path.join(HERE, "..", ".."))
CHECKS_DOC = os.path.join(PLUGIN, "skills", "procept-lite", "references", "ingest-checks.md")
FLOOR = "Sonnet 5.5 / GPT 5.6 Terra (or better)"
PROBLEM_KEYS = ("persona", "goal", "problem", "headline", "quote", "quote_run_id", "run_ids", "model")


# ---------------------------------------------------------------- small helpers

def _step(modname, argv):
    """Run a sibling command; never let an exception escape. -> exit code."""
    try:
        mod = importlib.import_module(modname)
        return int(mod.main(argv) or 0)
    except SystemExit as e:
        code = e.code
        if isinstance(code, str):
            print(code if code.startswith("error") else "error: %s" % code, file=sys.stderr)
            return 1
        return int(code or 0)
    except Exception as e:  # noqa: BLE001
        print("run: step %s hit an unexpected %s; see cleaning_report.md and re-run" % (modname, type(e).__name__),
              file=sys.stderr)
        return 1


def _name(x):
    """A source name that is safe to print (secrets and e-mails in file names are masked)."""
    return redact(os.path.basename(str(x).rstrip("/")) or str(x))[:80]


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError, RecursionError):
        return None


def _finish(out, step, instruction, **info):
    """Write status.json and print the one final line. Always returns 0."""
    instruction = " ".join(str(instruction).split())
    status = {"next": step, "instruction": instruction, "stages": checks.status(out), "updated": int(time.time())}
    status.update(info)
    os.makedirs(out, exist_ok=True)
    checks.atomic_write(os.path.join(out, "status.json"), json.dumps(status, indent=1))
    print("NEXT: %s | %s" % (step, instruction))
    return 0


def _summary_lines(runs, stats, rep):
    c = rep["counts"]
    cr = CL.CR
    n_drop = sum(v for k, v in cr.counts.items() if CL.KINDS.get(k, ("",))[0] == "drop")
    n_rep = sum(v for k, v in cr.counts.items() if CL.KINDS.get(k, ("",))[0] == "repair")
    n_ass = sum(v for k, v in cr.counts.items() if CL.KINDS.get(k, ("",))[0] == "assumption")
    thin = sum(1 for r in runs if r["extra"].get("thin"))
    lines = ["%d runs in %d conversations, %d tool calls (%d failed), from %d records in %d files." % (
        c["runs"], c["conversations"], c["tool_calls"], sum(r["n_tool_failures"] for r in runs), stats["records"],
        len(stats.get("files", [])))]
    if cr.dimensions:
        lines.append("Dimensions dropped: %s." % ", ".join(cr.dimensions))
    if thin:
        lines.append("%d thin runs (missing user message or answer) are kept and flagged." % thin)
    lines.append("Cleaning: %d drops, %d repairs, %d assumptions (details in cleaning_report.md)." % (n_drop, n_rep, n_ass))
    if stats.get("auto_mapping"):
        lines.append("Unknown schema was mapped by the starter heuristics (draft: mapping_rescue/mapping.draft.json).")
    return lines


# ---------------------------------------------------------------- the steps

def _scaffold(a, out):
    """Write the mapping-rescue kit. -> exit code (0 = written)."""
    return SCAF.main(["--src"] + list(a.src) + ["--out", out] + _opt_args(a))


def _opt_args(a):
    args = ["--format", a.format]
    for flag, val in (("--prices", a.prices), ("--noise-regex", a.noise_regex), ("--tool-regex", a.tool_regex)):
        if val:
            args += [flag, val]
    return args


def _kit_current(a, out):
    d = _read_json(os.path.join(out, "mapping_rescue", "mapping.draft.json"))
    return (os.path.isfile(os.path.join(out, "mapping_rescue", "README.md")) and isinstance(d, dict)
            and (d.get("_draft") or {}).get("files_sig") == N.files_sig(a.src))


def _mapping_problem(a):
    """-> (absent, problem): whether there is no mapping file at all, and the one-line reason it is not acceptable
    (None when it is, statically). Strict mode only."""
    mp = N.mapping_path_for(a)
    if not mp:
        return True, "no mapping.json yet"
    try:
        mapping = common.load_json_file(mp)
    except (OSError, ValueError):
        return False, "mapping file %s is not valid JSON" % _name(mp)
    return False, common.rescue_problem(mapping)


def _normalize(a, out, sig):
    """inspect + normalize in one pass. -> (exit code, rescue problem or None)."""
    try:
        opts = N.opts_from_args(a)
        runs, spans, stats = N.run_all(a.src, opts)
    except common.RescueError as e:
        return 0, str(e)
    except (FileNotFoundError, ValueError, SystemExit) as e:
        print("error: %s" % redact(e), file=sys.stderr)
        return 2, None
    except Exception as e:  # noqa: BLE001
        CL.CR.info("internal error %s while reading the input" % type(e).__name__)
        try:
            CL.CR.write(out)
        except OSError:
            pass
        print("error: the input could not be processed (%s); see %s" % (
            type(e).__name__, os.path.join(out, "cleaning_report.md")), file=sys.stderr)
        return 2, None
    os.makedirs(out, exist_ok=True)
    if not runs:
        CL.CR.write(out)
        skipped = sum(CL.CR.counts[k] for k in ("unreadable_file", "binary_file", "empty_file", "no_records_file",
                                                "ignored_extension"))
        if stats["records"] == 0:
            msg = ("no records found in %s (%s; see cleaning_report.md)"
                   % (", ".join(_name(x) for x in a.src),
                      "%d file(s) were empty, binary or unreadable" % skipped if skipped else
                      "nothing in it looks like JSON objects or a table"))
        else:
            msg = stats.get("mapping_failed") or "no runs could be built from %d records" % stats["records"]
        print("error: %s" % msg, file=sys.stderr)
        return 2, None
    try:
        rep = check_logs.build_report(runs, spans, stats, opts)
        check_logs.write_report(out, rep)
        N.write_outputs(out, runs, spans, stats, sig)
    except Exception as e:  # noqa: BLE001
        print("error: could not write the outputs to %s (%s)" % (out, type(e).__name__), file=sys.stderr)
        return 2, None
    summary = _summary_lines(runs, stats, rep)
    CL.CR.write(out, summary)
    for ln in summary:
        print(ln)
    return 0, None


def _shards(out):
    """-> (shard entries from shards_index.json, shards without a CSV yet)."""
    idx = _read_json(os.path.join(out, "grading", "shards_index.json"))
    if not isinstance(idx, list):
        return [], []
    todo = [x["shard"] for x in idx if not os.path.isfile(os.path.join(out, "grading", "scores", x["shard"] + ".csv"))]
    return idx, todo


def _merge_current(out):
    s = _read_json(os.path.join(out, "grading", "summary.json"))
    return (isinstance(s, dict) and os.path.isfile(os.path.join(out, "scores.csv"))
            and s.get("inputs_hash") == checks.artifact_hash(out, "merge")
            and s.get("overrides_hash") == GM.file_sha(os.path.join(out, "grading", "overrides.csv")))


def _problem_ready(out):
    d = _read_json(os.path.join(out, "scorecard_inputs", "user_goal_problem.json"))
    return isinstance(d, dict) and all(d.get(k) not in (None, "", []) for k in PROBLEM_KEYS)


def main(argv=None):
    argv = list(argv or [])
    if "-h" in argv or "--help" in argv:
        print("usage: plite.py run --src FILE_OR_DIR... [--out DIR] [--format F] [--map M] [--prices P] [--sample N]\n"
              "Does the next step code can do and ends with one line: NEXT: <step> | <instruction>. A model does that\n"
              "step (mapping rescue, checks, grading, study ...), then run the same command again until NEXT: done.")
        return 0
    p = argparse.ArgumentParser(prog="plite run", add_help=False)
    add_ingest_args(p)
    p.add_argument("--sample", type=int, default=300)
    try:
        a, _ = p.parse_known_args(argv)
    except SystemExit:
        print("error: run needs --src FILE_OR_DIR (the exported logs)", file=sys.stderr)
        return 2
    out = os.path.abspath(a.out)
    a.out = out
    gated = not checks.gates_disabled()
    out_args = ["--out", out]
    readme = os.path.join(out, "mapping_rescue", "README.md")

    def verified(stage):
        return (not gated) or checks.state(out, stage) == "verified"

    # ---- 1. the mapping comes from a model: scaffold, then wait for mapping.json
    if gated:
        absent, problem = _mapping_problem(a)
        if problem:
            if not _kit_current(a, out):
                rc = _scaffold(a, out)
                if rc != 0:
                    return rc
            if absent:
                return _finish(out, "mapping_rescue", "read %s and write %s with its _rescue block (model: Sonnet 5.5 / "
                               "GPT 5.6 Terra or better), then run this command again" %
                               (readme, os.path.join(out, "mapping.json")))
            return _finish(out, "mapping_rescue", "mapping.json is not accepted: %s; fix it (see %s), then run this "
                           "command again" % (problem, readme))

    # ---- 2. normalize + inspect when the runs are missing or stale
    sig = N.source_sig(a.src, a)
    runs_path = os.path.join(out, "normalized", "runs.jsonl")
    old = _read_json(os.path.join(out, "normalized", "ingest_stats.json")) or {}
    if not os.path.isfile(runs_path) or old.get("source_sig") != sig:
        rc, rescue = _normalize(a, out, sig)
        if rescue:
            if not _kit_current(a, out) and _scaffold(a, out) != 0:
                return 2
            return _finish(out, "mapping_rescue", "mapping.json is not accepted: %s; fix it (see %s), then run this "
                           "command again" % (rescue, readme))
        if rc != 0:
            return rc
    runs = GP.load_runs(runs_path)
    n_runs = len(runs)

    # ---- 3. the ingest sign-off
    st = checks.state(out, "ingest") if gated else "verified"
    if st == "failed":
        return _finish(out, "mapping_rescue", "the ingest check failed (see %s): fix mapping.json following %s, run "
                       "this command again, then have the new runs checked" %
                       (checks.path_for(out, "ingest"), readme), runs=n_runs)
    if st != "verified":
        need = min(checks.MIN_CHECKED["ingest"], n_runs)
        why = ("the mapping or the normalized runs changed since the last sign-off: check again" if st == "stale" else
               "check the normalized runs")
        return _finish(out, "ingest_verify", "%s: have a %s model compare at least %d of the %d runs in %s against their raw rows "
                       "using %s, list any run it cannot trust as unusable, then record it with plite.py check record "
                       "--out %s --file <record.json> (stage ingest) and run this command again" %
                       (why, FLOOR, need, n_runs, runs_path, CHECKS_DOC, out), runs=n_runs)

    # ---- 4. grading: prepare shards, wait for the CSVs, merge
    trust_scores = (not gated) and os.path.isfile(os.path.join(out, "scores.csv"))  # dev bypass: hand-written scores
    if not trust_scores:
        usable, ledger = GP.usable_split(out, runs)
        if not usable:
            GP.write_ledger(out, ledger)
            return _finish(out, "mapping_rescue", "all %d runs are unusable (no user message or marked untrustworthy; see "
                           "%s), so nothing can be graded: the mapping does not capture what users asked; redo the mapping "
                           "rescue (%s)" % (n_runs, os.path.join(out, checks.UNUSABLE_LEDGER), readme),
                           runs=n_runs, usable=0, unusable=len(ledger))
        idx, todo = _shards(out)
        if not idx or not GP.shards_current(out, a.sample):
            graded = [f for f in (os.listdir(os.path.join(out, "grading", "scores"))
                                  if os.path.isdir(os.path.join(out, "grading", "scores")) else []) if f.endswith(".csv")]
            if idx and graded:
                return _finish(out, "grade", "the runs or the unusable list changed after the shards were prepared and "
                               "graded, so those grades no longer match: re-prepare with plite.py grade-prepare --out %s "
                               "--sample %d --force (this discards the old CSVs), grade the new shards, then run this "
                               "command again" % (out, a.sample), runs=n_runs, usable=len(usable), unusable=len(ledger))
            rc = _step("grade_prepare", out_args + ["--sample", str(a.sample)])
            if rc != 0:
                return rc
            idx, todo = _shards(out)
        info = dict(runs=n_runs, usable=len(usable), unusable=len(ledger), shards=len(idx), shards_graded=len(idx) - len(todo))
        if todo:
            return _finish(out, "grade", "grade the %d remaining shard(s) of %d listed in %s with one %s reader per shard (brief: "
                           "%s), each writing its CSV into %s, then run this command again" %
                           (len(todo), len(idx), os.path.join(out, "grading", "workflow_args.json"), FLOOR,
                            os.path.join(out, "grading", "brief.md"), os.path.join(out, "grading", "scores")), **info)
        if not _merge_current(out):
            try:
                rc = GM.main(out_args)
            except SystemExit as e:
                print(e if str(e).startswith("error") else "error: %s" % e, file=sys.stderr)
                return 1
            if rc != 0:
                summ = _read_json(os.path.join(out, "grading", "summary.json")) or {}
                return _finish(out, "grade", "re-grade shard(s) %s: their CSVs have problems (see %s), then run this "
                               "command again" % (", ".join(summ.get("rerun_shards") or ["?"]),
                                                  os.path.join(out, "grading", "summary.json")), **info)
        if not verified("merge"):
            summ = _read_json(os.path.join(out, "grading", "summary.json")) or {}
            n_scored = summ.get("runs_scored") or 0
            return _finish(out, "merge_verify", "have a %s model compare at least %d of the %d scored rows in %s with their run "
                           "dossiers (%s rows have a key_quote that is not verbatim in the run: summary.json "
                           "quote_not_found), write any correction to %s (run_id,outcome_score,rationale,reason), then "
                           "record it with plite.py check record --out %s --file <record.json> (stage merge) and run "
                           "this command again" % (FLOOR, min(checks.MIN_CHECKED["merge"], n_scored), n_scored,
                                                   os.path.join(out, "scores.csv"), len(summ.get("quote_not_found") or []),
                                                   os.path.join(out, "grading", "overrides.csv"), out), **info)

    # ---- 5. the CIG
    rc = _step("build_cig", out_args)
    if rc != 0:
        return rc
    if not verified("cig"):
        return _finish(out, "cig_verify", "have a %s model check the CIG in %s (runs.csv, nodes.csv, edges.csv, summary.json) "
                       "against the run dossiers and the scores, then record it with plite.py check record --out %s "
                       "--file <record.json> (stage cig) and run this command again" %
                       (FLOOR, os.path.join(out, "cig"), out))

    # ---- 6. dev bypass: no study, build what can be built
    if not gated:
        rc = _step("build_viewer", out_args)
        if rc != 0:
            return rc
        if _step("report", out_args) != 0:
            print("run: the scorecard needs the study's problem write-up and was not built", file=sys.stderr)
        files = ["cig.html"] + (["scorecard.html"] if os.path.isfile(os.path.join(out, "scorecard.html")) else [])
        return _finish(out, "done", ", ".join(files))

    # ---- 7. the study (temporary), then the customer files
    if checks.state(out, "study") != "verified":
        if not os.path.isdir(os.path.join(out, "study")):
            try:
                study = importlib.import_module("plite.study")
            except ModuleNotFoundError as e:
                if e.name and e.name.startswith("plite"):
                    print("plite: module for command 'study-init' is missing (plite.study)", file=sys.stderr)
                    return 3
                raise
            rc = int(study.main(["init"] + out_args) or 0)
            if rc != 0:
                return rc
        return _finish(out, "study", "build the study in %s with one %s agent per section, in this order: Exploratory Data "
                       "Analysis, Reward Signal, Affinity Diagram, Empathy Map, Impact; then write %s (persona, goal, "
                       "problem, headline, quote, quote_run_id, run_ids, model) and record the study with plite.py check "
                       "record --out %s --file <record.json> (stage study); run this command again" %
                       (os.path.join(out, "study"), FLOOR,
                        os.path.join(out, "scorecard_inputs", "user_goal_problem.json"), out))
    if not _problem_ready(out):
        return _finish(out, "study", "write %s (persona, goal, problem, headline, quote, quote_run_id, run_ids, model) "
                       "from the study, then run this command again" %
                       os.path.join(out, "scorecard_inputs", "user_goal_problem.json"))
    if checks.state(out, "scorecard") == "verified":
        return _finish(out, "done", "cig.html, scorecard.html")
    rc = _step("build_viewer", out_args)
    if rc != 0:
        return rc
    if _step("report", out_args) != 0:
        return _finish(out, "study", "the scorecard was not built (see the error above): fix %s or the study outputs, then "
                       "run this command again" % os.path.join(out, "scorecard_inputs", "user_goal_problem.json"))
    return _finish(out, "scorecard_verify", "have a %s model open %s and %s and check every number and sentence against the data, "
                   "then record it with plite.py check record --out %s --file <record.json> (stage scorecard) and run "
                   "this command again" % (FLOOR, os.path.join(out, "scorecard.html"), os.path.join(out, "cig.html"), out))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
