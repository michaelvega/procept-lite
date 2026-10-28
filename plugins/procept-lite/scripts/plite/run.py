"""Chain command: inspect -> normalize -> (scores.csv present) cig -> viewer -> report."""
import importlib
import os
import sys


def _opt(argv, name, default=None):
    for i, a in enumerate(argv):
        if a == name and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return default


def _call(modname, argv):
    mod = importlib.import_module("plite." + modname)
    return int(mod.main(argv) or 0)


def main(argv=None):
    argv = list(argv or [])
    if "-h" in argv or "--help" in argv:
        print("usage: plite.py run --src FILE_OR_DIR... [--out DIR] [--format F] [--map M] [--prices P]\n"
              "Runs inspect and normalize; if <out>/scores.csv exists also cig, viewer, report.\n"
              "Otherwise stops after grade-prepare and prints the next step.")
        return 0
    out = _opt(argv, "--out", "./procept-out")
    out_args = ["--out", out]
    # strip --out from the pass-through args
    rest, skip = [], False
    for i, a in enumerate(argv):
        if skip:
            skip = False
        elif a == "--out":
            skip = True
        elif not a.startswith("--out="):
            rest.append(a)
    steps = [("check_logs", rest + out_args), ("normalize", rest + out_args)]
    for mod, args in steps:
        rc = _call(mod, args)
        if rc != 0:
            print("run: step %s failed (exit %d); fix and re-run." % (mod, rc), file=sys.stderr)
            return rc
    if not os.path.exists(os.path.join(out, "scores.csv")):
        rc = _call("grade_prepare", out_args)
        if rc != 0:
            return rc
        print("\nNext: grade the shards in %s/grading/shards with Haiku agents "
              "(see the skill, Step 2), then run: plite.py grade-merge --out %s, then plite.py run again."
              % (out, out))
        return 0
    for mod in ("build_cig", "build_viewer", "report"):
        rc = _call(mod, out_args)
        if rc != 0:
            print("run: step %s failed (exit %d)." % (mod, rc), file=sys.stderr)
            return rc
    print("\nDone: %s, %s, %s" % (os.path.join(out, "cig.html"),
                                  os.path.join(out, "REPORT.md"), os.path.join(out, "REPORT.docx")))
    return 0
