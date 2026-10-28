---
name: procept-lite
description: Turn exported agent logs into a CIG (failure network, run dossiers, viewer) and one-page scorecard in one action. Use for agent traces, LangSmith, LangChain, Pydantic Logfire, OpenTelemetry, GCP/AWS/Azure agent logs, "build the CIG", "why is my agent failing", grading agent runs good/ambiguous/bad, cost and latency of agents, scorecard.
---

# procept-lite

Principle: degrade, never block. Whatever the data looks like, reach the CIG; say plainly which dimensions are missing. Run scripts as `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/plite.py <cmd> --out <OUT>` (default `<OUT>` = `./procept-out`, reuse it).

## Rules (always)
- Unit of analysis is the RUN (one agent call); a conversation holds several runs. Never aggregate before scoring.
- Everything is local. Graders are local subagents. Never send data anywhere; never echo secrets, tokens or emails from the data.
- Report unflattering results too. Labels are model judgements, not user satisfaction; estimates are bounds, not forecasts.
- Graders never see vendor scores or evaluator labels.
- Never ask the user to fix data by hand. Never stop on a missing field.

## The one-action path
Trigger: the user has exported data and says "build the CIG" (or `/procept-lite:start <path>`). Paths = files, folders, zips, anything.

1. **Plan (max two sentences, then proceed without waiting).** Example: "I will clean and map your export into runs, grade a stratified sample of up to 300 runs with Haiku (about $X, from grade-prepare), then build the CIG, viewer and report. This only transforms static files locally and changes nothing in your systems." Get $X by running `grade-prepare --sample 300` first; state it, do not ask.
2. **Run** `run --src <paths...> --sample 300` (cap is configurable: user may say "grade 1000"). It cleans, scaffolds a mapping if needed (`<OUT>/mapping.auto.json`), normalizes, and writes `<OUT>/cleaning_report.md`.
3. **Grade** with Haiku per `references/grading-brief.md`: Workflow tool with `${CLAUDE_PLUGIN_ROOT}/workflow/grade.js` and the args JSON from grade-prepare, else background Agent-tool subagents (model haiku), one per shard in `<OUT>/grading/shards/`, fanned out in parallel, each writing its CSV to `<OUT>/grading/scores/`. Do not wait idly; fan out and collect.
4. **Merge and build:** `grade-merge` (re-run only missing or invalid shards), then `run --src <paths...>` again (it chains cig, viewer, report). Thin or ungraded runs are fine; they are marked.
5. **Finish:** present `<OUT>/cig.html`, `<OUT>/REPORT.md`, `<OUT>/REPORT.docx`, and read `<OUT>/cleaning_report.md` aloud in a few lines: what was repaired, which dimensions are missing (latency, cost, errors, tools), how many runs are "thin". State limits: model-judged labels on a sample, bounds not forecasts. Offer next steps (grade more, add prices, fix top issue).

## Questions are allowed only when
- No data yet: give the platform walkthrough, `references/exports/<platform>.md` (ask which platform: LangSmith, Logfire, GCP, AWS, Azure, other OTel).
- Truly blocked: the model-written mapping failed twice (see below) or the input is empty/unreadable. Then say exactly what is missing and which export would fix it.

## Exceptions and degradation
Full table with per-platform quirks: `references/exceptions.md`. Rules the pipeline applies itself and reports in `cleaning_report.md`:
- No timestamps: latency dropped, cost, errors, score kept. No tokens: estimated chars/4, flagged "estimated". No error field: derived from result text/status patterns. No tool calls: scorecard still built, CIG has only an agent node.
- Missing ids are synthesized; missing user message or answer: best available text, run marked "thin".
- Dirty files are repaired: BOM, UTF-16, gzip/zip, ndjson/array/concatenated JSON, truncated tail, bad CSV delimiter/quotes, double-encoded JSON, OTLP attribute lists, mixed epoch units, duplicate/orphan/out-of-order spans, huge fields truncated, unreadable files skipped with a note.
- Unknown schema: heuristic mapping to `mapping.auto.json`.

When the automatic path is not enough (error names unmapped fields, or output is clearly wrong): read `cleaning_report.md`, sample 20 rows of the raw file, write `<OUT>/mapping.json` (shape: `templates/mapping.example.json`), rerun with `--map`. Max two attempts, then ask the user the one blocking question. Tell them the mapping is only a local config file.

## References
`references/exceptions.md` (situations and fixes), `references/data-gaps.md` (what each field is for), `references/concepts.md` (plain-words explanation, use only if asked), `references/grading-brief.md`, `references/report.md`, `references/exports/`.
