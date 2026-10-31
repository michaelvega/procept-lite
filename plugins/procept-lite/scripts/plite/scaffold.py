"""scaffold: write the mapping-rescue kit for a raw export. Nothing here decides anything.

  python3 plite.py scaffold --src FILE_OR_DIR... [--format ..] [--prices P] [--noise-regex RE] [--tool-regex RE]
                            [--out DIR]

Writes <out>/mapping_rescue/:
  sample_raw.jsonl     <= 60 raw records spread across the WHOLE export (rare record shapes included), secrets and
                       e-mails masked; one {"loc": {"file", "record"}, "raw": {...}} per line
  mapping.draft.json   the heuristic starter (schema.py inference) for a model to read and edit
  draft_preview.jsonl  <= 40 runs the draft would build, each with its raw source locator and a masked raw excerpt
  README.md            exactly what the mapping-rescue model does next
It never writes mapping.json. A model at the floor (Sonnet 5.5 / GPT 5.6 Terra or better) compares the draft with real
rows and writes <out>/mapping.json with a _rescue block; normalize refuses to run without it.
"""
import argparse
import json
import os
import re
import sys
from bisect import bisect_right
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import SECRET_KEY_RE, add_ingest_args, clip, load_json_file, redact  # noqa: E402
from ingest import iter_records  # noqa: E402
import checks  # noqa: E402
import cleaning as CL  # noqa: E402
import normalize as N  # noqa: E402
import schema as SC  # noqa: E402

SAMPLE_MAX, PREVIEW_MAX, SHAPES_MAX, ODD_MAX = 60, 40, 15, 12
RAW_STR, RAW_ITEMS, RAW_DEPTH = 1500, 40, 12
HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.abspath(os.path.join(HERE, "..", ".."))
CHECKS_DOC = os.path.join(PLUGIN, "skills", "procept-lite", "references", "ingest-checks.md")
EXAMPLE = os.path.join(PLUGIN, "templates", "mapping.example.json")


# ---------------------------------------------------------------- masking and picking

def mask(o, _d=0):
    """A copy of a parsed value that is safe to put in front of a model: e-mails, tokens, passwords and card numbers
    masked with common.redact; very long strings and lists clipped; very deep nesting cut."""
    if isinstance(o, str):
        s = redact(o)
        return s if len(s) <= RAW_STR else s[:RAW_STR] + "...[+%d chars]" % (len(s) - RAW_STR)
    if _d >= RAW_DEPTH:
        return "<deeper levels omitted>"
    if isinstance(o, dict):
        out = {}
        for k, v in o.items():
            k = redact(k)
            while k in out:
                k += "_"
            out[k] = "[secret]" if SECRET_KEY_RE.search(k) and isinstance(v, str) and v else mask(v, _d + 1)
        return out
    if isinstance(o, list):
        items = [mask(v, _d + 1) for v in o[:RAW_ITEMS]]
        if len(o) > RAW_ITEMS:
            items.append("<+%d more items>" % (len(o) - RAW_ITEMS))
        return items
    return o


def spread(n, k):
    """k indices spread evenly over range(n), first and last included."""
    if k <= 0 or n <= 0:
        return []
    if k >= n:
        return list(range(n))
    if k == 1:
        return [0]
    return sorted({int(round(i * (n - 1) / float(k - 1))) for i in range(k)})


def pick_sample(recs, limit=SAMPLE_MAX, shapes=SHAPES_MAX):
    """Indices of the records to show: the rarest record shapes (one each, so a file that mixes agent runs with http or
    health-check rows shows both) plus an even spread over the whole export. Never just the first N."""
    n = len(recs)
    if n <= limit:
        return list(range(n))
    freq, first = Counter(), {}
    for i, r in enumerate(recs):
        sig = tuple(sorted(str(k) for k in r)) if isinstance(r, dict) else ()
        freq[sig] += 1
        first.setdefault(sig, i)
    rare = sorted(first[s] for s in sorted(freq, key=lambda s: (freq[s], first[s]))[:shapes])
    chosen = set(rare)
    chosen.update(spread(n, limit - len(rare)))
    return sorted(chosen)  # at most `shapes` + (limit - shapes) = limit


class Locator(object):
    """Record index (over everything read) -> {"file": name, "record": 1-based number within that file}."""

    def __init__(self, rec_files):
        self.files = list(rec_files or [])
        self.starts = [f[1] for f in self.files]

    def __call__(self, idx):
        j = bisect_right(self.starts, idx) - 1
        if j < 0 or idx >= self.files[j][1] + self.files[j][2]:
            return {"file": "?", "record": idx + 1}
        return {"file": self.files[j][0], "record": idx - self.files[j][1] + 1}


# ---------------------------------------------------------------- the draft

def draft_mapping(recs):
    """The heuristic starter, built with the same inference normalize used to apply directly.
    -> (draft or None, notes, detected). `draft` is None when nothing could be inferred with confidence."""
    kinds, raws = Counter(), []
    for rec in recs:
        for k, r in SC.lift(rec):
            kinds[k] += 1
            if k == "raw":
                raws.append(r)
    notes = []
    flat, ok = None, False
    if raws:
        flat, fnotes, ok = SC.infer(raws)
        notes += [str(x) for x in fnotes]
        if flat and "_notes" in flat:
            flat = {k: v for k, v in flat.items() if k != "_notes"}
    lifted = {k for k in kinds if k != "raw"}
    if raws and not ok:
        return None, notes + ["no confident mapping for %d records the parsers do not recognise" % len(raws)], dict(kinds)
    if flat and flat.get("format") in ("otel", "langsmith"):
        return flat, notes, dict(kinds)
    if not raws:
        fmt = next(iter(lifted)) if len(lifted) == 1 and next(iter(lifted)) in ("otel", "langsmith") else "auto"
        notes.append("every record is a known shape (%s); the parsers read it without column mapping" %
                     ", ".join("%s x%d" % kv for kv in sorted(kinds.items())))
        return {"format": fmt, "rename": {}}, notes, dict(kinds)
    return dict({"format": "auto" if lifted else "flat"}, **flat), notes, dict(kinds)


def _clip_run(r):
    """A run dossier shortened for reading: the mapping is what is being checked, not the whole transcript."""
    r = json.loads(json.dumps(r, default=str))
    r["user_message"] = clip(r.get("user_message") or "", 600)
    if r.get("final_answer"):
        r["final_answer"] = clip(r["final_answer"], 600)
    calls = r.get("tool_calls") or []
    for c in calls:
        c["args"], c["result"] = clip(c.get("args") or "", 240), clip(c.get("result") or "", 240)
    if len(calls) > 12:
        r["tool_calls"] = calls[:12] + [{"name": "<+%d more calls>" % (len(calls) - 12)}]
    return r


def _odd(r):
    x = r.get("extra") or {}
    return bool(x.get("thin") or x.get("user_message_inferred") or x.get("answer_inferred") or x.get("synthetic_run_id")
                or r.get("latency_s") is None or not r.get("user_message") or r.get("final_answer") is None)


def pick_runs(runs, limit=PREVIEW_MAX, odd_max=ODD_MAX):
    """Indices into `runs`: an even spread plus up to odd_max runs that look off (thin, no id, no latency), so the
    rescue model sees the awkward cases, not only the clean ones."""
    odd = [i for i, r in enumerate(runs) if _odd(r)]
    chosen = {odd[j] for j in spread(len(odd), min(odd_max, len(odd)))}
    chosen.update(spread(len(runs), limit - len(chosen)))
    return sorted(chosen)


def preview(paths, a, draft, recs, stats):
    """Run the draft over the export exactly as normalize would (nothing is kept). -> (lines, built info)."""
    mapping = {k: v for k, v in draft.items() if not k.startswith("_")}
    prices = load_json_file(a.prices) if getattr(a, "prices", None) else None
    opts = N.Opts(a.format, mapping, prices, getattr(a, "noise_regex", None), getattr(a, "tool_regex", None))
    opts.preview = True
    spans, st = N.load_spans(paths, opts, recs=recs)
    locate = {}
    runs = N.build_runs(spans, opts, locate=locate)
    where = Locator(stats.get("rec_files"))
    lines = []
    for i in pick_runs(runs):
        r = runs[i]
        src = locate.get(r["run_id"], [])
        lines.append({"run": mask(_clip_run(r)),
                      "source": {"n_records": len(src), "records": [where(x) for x in src[:8]]},
                      "raw": [mask(recs[x]) for x in src[:4]]})
    built = {"runs": len(runs),
             "run_id_sources": dict(Counter(r["extra"].get("run_id_source") for r in runs)),
             "without_user_message": sum(1 for r in runs if not (r.get("user_message") or "").strip()),
             "without_answer": sum(1 for r in runs if r.get("final_answer") is None),
             "with_tool_calls": sum(1 for r in runs if r.get("tool_calls")),
             "with_latency": sum(1 for r in runs if r.get("latency_s") is not None),
             "records_unreadable": st["unknown_records"] + st["bad_lines"]}
    return lines, built


# ---------------------------------------------------------------- README

def readme(out, n_records, n_sample, n_preview, draft_ok, built):
    need = min(20, n_records)
    doc = CHECKS_DOC if os.path.isfile(CHECKS_DOC) else "skills/procept-lite/references/ingest-checks.md"
    ex = EXAMPLE if os.path.isfile(EXAMPLE) else "templates/mapping.example.json"
    L = ["# Mapping rescue", "",
         "You are the mapping-rescue model. Floor: Sonnet 5.5 or GPT 5.6 Terra, or better; never Haiku. If you are a "
         "smaller model, stop and hand this folder to one of those.", "",
         "A wrong column or role poisons every score, persona and fix downstream, and nothing later can notice it. "
         "Better a smaller correct sample, plus runs classified unusable, than noise leaked into the eval set. "
         "Nothing here has been applied: the draft is only a starter for you to read, check against real rows and edit. "
         "The code is a starter too; if the export needs something it does not do, change the mapping (or the code) so "
         "that it does.", "",
         "## What is here", "",
         "- `sample_raw.jsonl`: %d of the %d raw records, spread across the whole export (rare record shapes "
         "included), secrets and e-mails masked. One line each: `{\"loc\": {\"file\", \"record\"}, \"raw\": {...}}`." %
         (n_sample, n_records),
         "- `mapping.draft.json`: the heuristic draft. %s" % (
             "Read its `_draft` block for what it detected." if draft_ok else
             "No confident draft could be inferred: write the mapping from the raw sample."),
         "- `draft_preview.jsonl`: %d runs the draft builds, each with `source.records` (file and record number of the "
         "raw rows it came from) and `raw` (a masked excerpt of those rows)." % n_preview, ""]
    if built:
        L += ["The draft builds %d runs: %d without a user message, %d without an answer, %d with tool calls, %d with a "
              "latency (run id sources: %s)." % (
                  built["runs"], built["without_user_message"], built["without_answer"], built["with_tool_calls"],
                  built["with_latency"], ", ".join("%s %d" % kv for kv in sorted(built["run_id_sources"].items())) or
                  "none"), ""]
    L += ["## Do this", "",
          "1. Read `sample_raw.jsonl`, then `mapping.draft.json`, then `draft_preview.jsonl`. Open the export itself for "
          "any row you need in full: `loc` / `source.records` give the file and the record number (the n-th record read "
          "from that file; for JSON lines, the n-th object).",
          "2. Compare at least %d records (all of them if there are fewer) against what the draft built. Judge real "
          "values, never column names alone:" % need,
          "   - the user message is what a person asked (not a system prompt, tool output or the agent's reply); the "
          "final answer is the agent's last reply to that person;",
          "   - run id is unique per agent call; conversation id groups one person's runs; ids are not repeated across "
          "unrelated rows;",
          "   - tool names are real tool names (not model names, span kinds or HTTP routes); success or error flags "
          "agree with the result text;",
          "   - timestamps and durations are in the unit the mapping claims; tokens are counts, cost is USD;",
          "   - nothing that looks like a vendor score, evaluator label or thumbs reaches a field the graders read; "
          "rows that are not agent runs (http, db, health checks) do not become runs.",
          "3. Edit the draft into the mapping you trust. Same shape as `%s`. `format` is `otel`, `langsmith`, `flat` "
          "(a table: values are column names or dotted paths) or `auto` (known shapes are lifted per record, the rest "
          "use the table keys). A field you cannot verify: remove the key; never best-guess a role to keep rows." % ex,
          "4. Write `%s` with the required block, replacing the numbers and words with yours:" %
          os.path.join(out, "mapping.json"),
          "   `\"_rescue\": {\"model\": \"<your own model id>\", \"rows_reviewed\": <whole number, at least %d>, "
          "\"notes\": \"<what you compared, what you changed and why>\"}`" % need,
          "   Remove the `_draft` and `_comment` blocks (they are ignored). `model` must pass the floor; `rows_reviewed` "
          "is how many raw records you really compared.",
          "5. Run `plite.py run --src <the same export> --out %s` again. It normalizes with your mapping and then asks "
          "for the next step: checking the normalized runs against their raw rows. The checks are in `%s`." % (out, doc),
          "",
          "This folder is regenerated by `plite.py scaffold`; your `mapping.json` is never touched by it.", ""]
    return "\n".join(L)


# ---------------------------------------------------------------- command

def _names(paths):
    return ", ".join(redact(os.path.basename(str(x).rstrip("/")) or str(x))[:80] for x in paths)


def build(a):
    """Read the export once, write the kit. -> dict of what was written, or raises ValueError (one-line message)."""
    CL.CR.reset()
    stats = {"files": [], "bad_lines": 0}
    recs = list(iter_records(a.src, stats))
    out = os.path.abspath(a.out)
    if not recs:
        CL.CR.write(out)
        skipped = sum(CL.CR.counts[k] for k in ("unreadable_file", "binary_file", "empty_file", "no_records_file",
                                                "ignored_extension"))
        raise ValueError("no records found in %s (%s; see cleaning_report.md)" % (
            _names(a.src), "%d file(s) were empty, binary or unreadable" % skipped if skipped else
            "nothing in it looks like JSON objects or a table"))
    mr = os.path.join(out, "mapping_rescue")
    os.makedirs(mr, exist_ok=True)
    where = Locator(stats.get("rec_files"))
    sample = [{"loc": where(i), "raw": mask(recs[i])} for i in pick_sample(recs)]
    draft, notes, detected = draft_mapping(recs)
    built, lines = None, []
    if draft is not None:
        try:
            lines, built = preview(a.src, a, draft, recs, stats)
        except Exception as e:  # noqa: BLE001 - a draft that cannot be applied is a finding for the model, not a crash
            notes.append("the draft could not be applied (%s): write the mapping from the raw sample" % type(e).__name__)
            draft = None
    cols = [redact(str(k))[:60] for k, _ in Counter(k for r in recs[:2000] for k in r).most_common(40)]
    body = {"_comment": ("starter draft from column names and value shapes; read it, check it against real rows, then "
                         "write mapping.json (see README.md in this folder). Nothing here has been applied.")}
    body.update(draft if draft is not None else {})
    body["_draft"] = {"confident": draft is not None, "records": len(recs), "detected": detected, "notes": notes,
                      "built": built or {}, "columns_seen": cols, "files_sig": N.files_sig(a.src)}
    checks.atomic_write(os.path.join(mr, "sample_raw.jsonl"),
                        "".join(json.dumps(s, ensure_ascii=False, default=str) + "\n" for s in sample))
    checks.atomic_write(os.path.join(mr, "mapping.draft.json"),
                        json.dumps(body, indent=2, ensure_ascii=False, default=str) + "\n")
    checks.atomic_write(os.path.join(mr, "draft_preview.jsonl"),
                        "".join(json.dumps(x, ensure_ascii=False, default=str) + "\n" for x in lines))
    checks.atomic_write(os.path.join(mr, "README.md"),
                        readme(out, len(recs), len(sample), len(lines), draft is not None, built))
    return {"dir": mr, "records": len(recs), "sample": len(sample), "preview": len(lines),
            "draft": draft is not None, "built": built, "files": len(stats.get("files", []))}


def main(argv=None):
    p = argparse.ArgumentParser(prog="plite scaffold", description="write the mapping-rescue kit (raw sample, draft, "
                                "preview, README) for a model to review; never writes mapping.json")
    add_ingest_args(p)
    a = p.parse_args(argv)
    try:
        res = build(a)
    except (FileNotFoundError, ValueError) as e:
        print("error: %s" % redact(e), file=sys.stderr)
        return 2
    except (MemoryError, RecursionError):
        print("error: the input is too large or too deeply nested to read", file=sys.stderr)
        return 2
    except re.error:
        print("error: --noise-regex or --tool-regex is not a valid regular expression", file=sys.stderr)
        return 2
    print("mapping rescue kit written to %s" % res["dir"])
    print("  %d raw records read from %d files: %d in sample_raw.jsonl, %d draft runs in draft_preview.jsonl%s" % (
        res["records"], res["files"], res["sample"], res["preview"],
        "" if res["draft"] else " (no confident draft; write the mapping from the raw sample)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
