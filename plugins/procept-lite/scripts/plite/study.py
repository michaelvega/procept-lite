"""study: the temporary study workspace (plite.py study-init | study-build | study-clean).

  study-init  --out DIR [--force]   copy the study starters to <out>/study (needs the ingest, merge and cig sign-offs)
  study-build --out DIR             assemble <out>/study/study.html and STUDY.md, then check the section outputs
  study-clean --out DIR             delete <out>/study (only when it carries the marker study-init wrote)

Everything in <out>/study is a STARTER for the agent to read and edit for this dataset (see study/BRIEF.md there).
The folder is temporary: the scorecard build deletes it, and nothing outside it is needed afterwards except
<out>/scorecard_inputs/. main(argv) takes argv[0] in init | build | clean.
"""
import argparse
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import checks  # noqa: E402

SRC = os.path.normpath(os.path.join(HERE, "..", "..", "study"))   # plugins/procept-lite/study
MARKER = ".procept-lite-study"
IGNORE = shutil.ignore_patterns("tests", "__pycache__", "*.pyc", ".DS_Store")


def study_dir(out):
    return os.path.join(out, "study")


def has_marker(path):
    return os.path.isdir(path) and not os.path.islink(path) and os.path.isfile(os.path.join(path, MARKER))


def init(out, force=False):
    checks.require(out, ["ingest", "merge", "cig"])
    dst = study_dir(out)
    if os.path.lexists(dst):
        if not force:
            raise SystemExit("error: %s already exists. Pass --force to start over (this discards what is in it)." % dst)
        if not has_marker(dst):
            raise SystemExit("error: %s exists but is not a procept-lite study folder (no %s marker); refusing to touch it."
                             % (dst, MARKER))
        shutil.rmtree(dst)
    shutil.copytree(SRC, dst, ignore=IGNORE)
    os.makedirs(os.path.join(dst, "sections"), exist_ok=True)
    with open(os.path.join(dst, MARKER), "w", encoding="utf-8") as f:
        f.write("procept-lite study folder (temporary). `plite.py study-clean` deletes it.\n")
    print("study folder ready: %s" % dst)
    print("next: build the five sections from %s with floor-model agents, then run: plite.py study-build --out %s"
          % (os.path.join(dst, "BRIEF.md"), out))
    return 0


def _run(script, out):
    r = subprocess.run([sys.executable, "-I", script, out], capture_output=True, text=True)
    if r.stdout:
        print(r.stdout.rstrip("\n"))
    if r.stderr:
        print(r.stderr.rstrip("\n"), file=sys.stderr)
    return r.returncode


def build(out):
    dst = study_dir(out)
    if not has_marker(dst):
        raise SystemExit("error: %s is not a study folder; run study-init first." % dst)
    rc = _run(os.path.join(dst, "build_study.py"), out)
    if rc:
        return rc
    return _run(os.path.join(dst, "validate_sections.py"), out)


def clean(out):
    dst = study_dir(out)
    if not os.path.lexists(dst):
        print("nothing to clean: %s does not exist" % dst)
        return 0
    if not has_marker(dst):
        raise SystemExit("error: %s has no %s marker (or is a link); refusing to delete it." % (dst, MARKER))
    shutil.rmtree(dst)
    print("removed %s" % dst)
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0 if argv else 2
    sub = argv[0][len("study-"):] if argv[0].startswith("study-") else argv[0]
    if sub not in ("init", "build", "clean"):
        print("error: unknown study command %r (init|build|clean)" % argv[0], file=sys.stderr)
        return 2
    p = argparse.ArgumentParser(prog="plite study-" + sub, description=__doc__.split("\n")[0])
    p.add_argument("--out", default="./procept-out", help="the output folder (default ./procept-out)")
    if sub == "init":
        p.add_argument("--force", action="store_true", help="replace an existing study folder that carries the marker")
    a = p.parse_args(argv[1:])
    out = os.path.abspath(a.out)
    if sub == "init":
        return init(out, a.force)
    return build(out) if sub == "build" else clean(out)


if __name__ == "__main__":
    sys.exit(main())
