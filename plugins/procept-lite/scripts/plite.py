#!/usr/bin/env python3
"""procept-lite dispatcher: python3 plite.py <command> [args]."""
import importlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# command -> (module, description[, function name[, arguments put in front of the command's own]])
COMMANDS = {
    "inspect": ("check_logs", "check that the logs have what is needed"),
    "normalize": ("normalize", "convert raw traces to canonical runs.jsonl (needs the rescued mapping.json)"),
    "grade-prepare": ("grade_prepare", "write grading shards, brief and cost estimate (needs the ingest sign-off)"),
    "grade-merge": ("grade_merge", "validate shard scores and write scores.csv"),
    "cig": ("build_cig", "build the CIG (graph csvs, flow_graph.json, dossiers)"),
    "viewer": ("build_viewer", "build the self-contained cig.html"),
    "report": ("report", "build the customer scorecard (scorecard.html)"),
    "run": ("plite.run", "do the next step of the pipeline and print NEXT: <step> | <instruction>"),
    "scaffold": ("plite.scaffold", "write the mapping-rescue kit (raw sample, draft mapping, preview, README)"),
    "check": ("plite.checks", "record or show a stage sign-off written by a verifying model"),
    "status": ("plite.checks", "show every stage as verified, stale, failed or missing", "status_main"),
    "study-init": ("plite.study", "copy the study starters to <out>/study", "main", ["init"]),
    "study-build": ("plite.study", "assemble <out>/study/study.html from the section outputs", "main", ["build"]),
    "study-clean": ("plite.study", "delete <out>/study", "main", ["clean"]),
}


def usage():
    lines = ["usage: plite.py <command> [args]  (each command accepts --out DIR)", "", "commands:"]
    for name, spec in COMMANDS.items():
        lines.append("  %-14s %s" % (name, spec[1]))
    return "\n".join(lines)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(usage())
        return 0 if argv else 2
    cmd = argv[0]
    if cmd not in COMMANDS:
        print("plite: unknown command %r\n\n%s" % (cmd, usage()), file=sys.stderr)
        return 2
    spec = COMMANDS[cmd]
    modname = spec[0]
    func = spec[2] if len(spec) > 2 else "main"
    front = list(spec[3]) if len(spec) > 3 else []
    if "." not in modname:
        modname = "plite." + modname
    try:
        mod = importlib.import_module(modname)
    except ModuleNotFoundError as e:
        if e.name and (e.name == modname or modname.startswith(e.name)):
            print("plite: module for command %r is missing (%s)" % (cmd, modname), file=sys.stderr)
            return 3
        raise
    rc = getattr(mod, func)(front + argv[1:])
    return int(rc or 0)


if __name__ == "__main__":
    sys.exit(main())
