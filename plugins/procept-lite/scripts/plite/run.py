"""run: the single entry point. raw export(s) -> inspect + clean + normalize + grade-prepare and, when scores exist,
cig + viewer + report. Degrades instead of blocking: every gap becomes a line in <out>/cleaning_report.md.

  python3 plite.py run --src FILE_OR_DIR... [--out DIR] [--map M] [--prices P] [--format F]

Exit 0 on success (possibly with warnings); 2 only when the input is empty/unreadable (one-line message)."""
import argparse
import importlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import check_logs  # noqa: E402
import cleaning as CL  # noqa: E402
import normalize as N  # noqa: E402
from common import add_ingest_args  # noqa: E402


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
    from common import redact
    return redact(os.path.basename(str(x).rstrip("/")) or str(x))[:80]


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
        lines.append("Unknown schema was mapped by heuristics: mapping.auto.json (edit and pass --map to override).")
    return lines


def main(argv=None):
    argv = list(argv or [])
    if "-h" in argv or "--help" in argv:
        print("usage: plite.py run --src FILE_OR_DIR... [--out DIR] [--format F] [--map M] [--prices P]\n"
              "One command: inspect + clean + normalize + grade-prepare; if <out>/scores.csv exists also cig, viewer,\n"
              "report. Bad or unknown input is repaired or scaffolded and logged in <out>/cleaning_report.md.")
        return 0
    p = argparse.ArgumentParser(prog="plite run", add_help=False)
    add_ingest_args(p)
    p.add_argument("--sample", type=int, default=300)
    try:
        a, _ = p.parse_known_args(argv)
    except SystemExit:
        print("error: run needs --src FILE_OR_DIR (the exported logs)", file=sys.stderr)
        return 2
    out = a.out
    try:
        opts = N.opts_from_args(a)
        runs, spans, stats = N.run_all(a.src, opts)
    except (FileNotFoundError, ValueError, SystemExit) as e:
        from common import redact
        print("error: %s" % redact(e), file=sys.stderr)
        return 2
    except Exception as e:  # noqa: BLE001
        CL.CR.info("internal error %s while reading the input" % type(e).__name__)
        try:
            CL.CR.write(out)
        except OSError:
            pass
        print("error: the input could not be processed (%s); see %s" % (
            type(e).__name__, os.path.join(out, "cleaning_report.md")), file=sys.stderr)
        return 2
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
        return 2
    try:
        rep = check_logs.build_report(runs, spans, stats, opts)
        check_logs.write_report(out, rep)
        N.write_outputs(out, runs, spans, stats)
    except Exception as e:  # noqa: BLE001
        print("error: could not write the outputs to %s (%s)" % (out, type(e).__name__), file=sys.stderr)
        return 2
    summary = _summary_lines(runs, stats, rep)
    CL.CR.write(out, summary)
    for ln in summary:
        print(ln)
    out_args = ["--out", out]
    if not os.path.exists(os.path.join(out, "scores.csv")):
        rc = _step("grade_prepare", out_args + ["--sample", str(a.sample)])
        if rc != 0:
            return rc
        print("\nNext: grade the shards in %s/grading/shards with Haiku agents "
              "(see the skill, Step 2), then run: plite.py grade-merge --out %s, then plite.py run again."
              % (out, out))
        return 0
    for mod in ("build_cig", "build_viewer", "report"):
        rc = _step(mod, out_args)
        if rc != 0:
            print("run: step %s failed (exit %d); the cleaned data is in %s" % (mod, rc, out), file=sys.stderr)
            return rc
    print("\nDone: %s, %s, %s" % (os.path.join(out, "cig.html"),
                                  os.path.join(out, "REPORT.md"), os.path.join(out, "REPORT.docx")))
    return 0
