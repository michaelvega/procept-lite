#!/usr/bin/env python3
"""procept-lite dispatcher: python3 plite.py <command> [args]."""
import importlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

COMMANDS = {
    "inspect": ("check_logs", "check that the logs have what is needed"),
    "normalize": ("normalize", "convert raw traces to canonical runs.jsonl"),
    "grade-prepare": ("grade_prepare", "write grading shards, brief and cost estimate"),
    "grade-merge": ("grade_merge", "validate shard scores and write scores.csv"),
    "cig": ("build_cig", "build the CIG (graph csvs, flow_graph.json, dossiers)"),
    "viewer": ("build_viewer", "build the self-contained cig.html"),
    "report": ("report", "write REPORT.md and REPORT.docx"),
    "run": ("plite.run", "chain inspect, normalize, and (if scored) cig, viewer, report"),
}


def usage():
    lines = ["usage: plite.py <command> [args]  (each command accepts --out DIR)", "", "commands:"]
    for name, (_, desc) in COMMANDS.items():
        lines.append("  %-14s %s" % (name, desc))
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
    modname = COMMANDS[cmd][0]
    if "." not in modname:
        modname = "plite." + modname
    try:
        mod = importlib.import_module(modname)
    except ModuleNotFoundError as e:
        if e.name and (e.name == modname or modname.startswith(e.name)):
            print("plite: module for command %r is missing (%s)" % (cmd, modname), file=sys.stderr)
            return 3
        raise
    rc = mod.main(argv[1:])
    return int(rc or 0)


if __name__ == "__main__":
    sys.exit(main())
