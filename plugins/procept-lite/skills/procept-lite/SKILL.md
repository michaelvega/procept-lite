---
name: procept-lite
description: Turn exported agent logs (LangSmith, LangChain, Pydantic Logfire, OpenTelemetry, GCP, AWS, Azure) into cig.html, a failure network over errors, cost, latency and score, plus scorecard.html, a one-page scorecard, in one action, with every stage checked against the real data by a smart model. Use for agent traces, "build the CIG", "why is my agent failing", grading agent runs good/ambiguous/bad, cost and latency of agents, scorecard.
---

# procept-lite

Scripts run as `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/plite.py <cmd> --out <OUT>` (`<OUT>` defaults to `./procept-out`; reuse it). Below, `plite.py` means that command.

## How it works
- The scripts, templates and prompts in this plugin are **starters**: read them, copy what you need, edit it for this dataset. The architecture and the file contracts stay: raw export, `mapping.json`, `normalized/runs.jsonl`, grading shards, `scores.csv`, `cig/`, a temporary study, then `cig.html` and `scorecard.html`. If a starter must change for this data, copy the plugin folder into `<OUT>/work/` and run every command from the copy, so all steps agree.
- Code computes numbers and validates. Models judge reality and write words. Each stage is checked against the real data by a smart model and signed off before the next stage may use it.
- **Model floor** for every judgement step (mapping rescue, verification, grading, study, problem write-up): Sonnet 5.5 (`claude-sonnet-5-5`) or GPT 5.6 Terra, or better. Never Haiku. If you are a smaller model, delegate each judgement step to one of those: Workflow tool or Agent tool with `model: "sonnet"`, in the background and in parallel (a GPT host uses GPT 5.6 Terra).
- The unit is the RUN (one agent call); a conversation holds several runs. Never aggregate before scoring.
- The user gets exactly two files, `cig.html` and `scorecard.html`. The study is temporary and is deleted.

## Rules
- Everything is local. Never send data anywhere. Never echo secrets, tokens or emails from the data. Graders never see vendor scores or evaluator labels.
- Correct and small beats big and noisy. A smaller verified sample, with the rest classified unusable, is better than noise in the eval set. Never pad it.
- Customer-facing files never contain repair, drop, fix, mistake or error-admission wording, statistics jargon, internal paths, check records, placeholders or blank cells. If something would look wrong, fix it at the source; never annotate it.
- Show what the data shows, flattering or not. Code computes every number; never adjust one by hand.
- If you find a mistake of your own, fix it, verify again and do not mention it again. If something is unfixable or unverifiable, say so in ONE plain line.
- Never ask the user to fix data by hand. Never stop on a missing field: unmap it (that dimension is left out) or classify the affected runs unusable.

## The loop
Plan in at most two sentences, then proceed without waiting. Example: "I will map your export into runs, check every stage against the real data, grade up to 300 runs and build cig.html and scorecard.html. It all runs locally on static files and changes nothing in your systems."

`plite.py run --src <paths...> --out <OUT> [--sample 300]` (paths: files, folders, zips; the user may ask for a bigger sample). It does the next code step it can and ends with ONE line `NEXT: <step> | <instruction>` (also in `<OUT>/status.json`). Do that step with a floor-model agent (below), then run the same command again, until `NEXT: done`. `plite.py status --out <OUT>` shows each sign-off as verified, stale, failed or missing.

A sign-off is a JSON record, written with `plite.py check record --out <OUT> --file rec.json`:
`{"stage": "ingest|merge|cig|study|scorecard", "model": "<your own exact model id>", "verdict": "pass|fail", "checked": 40, "mismatches": 0, "notes": "one or two plain sentences", "evidence": [{"id": "<run id or item>", "finding": "what you saw; 5 or more, all different"}], "unusable": [{"run_id": "...", "reason": "..."}] (ingest only), "overridden": 0 (merge only)}`.
The code refuses a record that is below the floor, too small, or has too many mismatches. Any later change to what it covers makes it stale, so record last. Sampling, evidence, pass and fail per stage: `references/stage-signoffs.md`.

## The NEXT steps
Workflows are plain scripts in `${CLAUDE_PLUGIN_ROOT}/workflow/`, run with the Workflow tool (`model: sonnet` inside). Every one takes `out` and `root: "${CLAUDE_PLUGIN_ROOT}"`. With no Workflow tool, follow the fallback recipe named for each step.

**mapping_rescue** (no `mapping.json`; `run` scaffolded `<OUT>/mapping_rescue/`)
- Do: `mapping.js` with `{out, src: [<the paths>], root}`; it also covers `ingest_verify`. Fallback: `references/ingest-checks.md`, section 5.
- Check: ALWAYS, even if the draft looks right. The agent reads the scaffold and the raw rows themselves and decides every field's role from values, not column names.
- Writes: `<OUT>/mapping.json` (shape of `templates/mapping.example.json`) with `"_rescue": {"model", "rows_reviewed", "notes"}`.
- If it fails: `run` refuses without a valid mapping and says why; fix the mapping and run again.

**ingest_verify** (mapping applied, `normalized/runs.jsonl` built, not signed off)
- Do: the same workflow; add `only: "verify"` when `mapping.json` already exists.
- Check: at least 30 normalized runs (all if fewer), spread across the file, against their raw rows: roles, swaps, units, identity, tools, leaks, mixed sources, real no-answers (`references/ingest-checks.md`).
- Records: stage `ingest`, with `unusable` listing every run that cannot be trusted. Those are never graded or counted.
- If it fails: fix the mapping, normalize again, check a fresh sample; two repair rounds at most. What still cannot be verified becomes unusable. If almost nothing is usable, say so in one plain line and stop.

**grade** (`grade-prepare` wrote shards and `<OUT>/grading/workflow_args.json`)
- Do: read `<OUT>/grading/cost_estimate.json`, state the estimate in one line, proceed. `grade.js` with the contents of `workflow_args.json` as args. Fallback: `references/grading-brief.md`.
- Check: blind readers grade every run from its own evidence (`references/grading-brief.md`). Persona labels are provisional goal labels; the study re-derives the final personas.
- Records: nothing; `merge_verify` signs off. If it fails: `grade-merge` names invalid or missing shards; re-run only those (`grade-merge --resume` writes the args).

**merge_verify** (`grade-merge` wrote `scores.csv`)
- Do: `verify.js` with `{out, stage: "merge", root}`. Fallback: `references/stage-signoffs.md`, last section.
- Check: at least 40 graded runs (all if fewer) plus every risky row (quote not found, audit disagreement, low confidence), against the run's own tool results and answer.
- Records: stage `merge`. Each wrong score goes to `<OUT>/grading/overrides.csv`, then `grade-merge` applies it; `overridden` counts them.
- If it fails (more than 15 percent wrong): the grading itself is off. Fix `<OUT>/grading/brief.md`, re-run the affected shards, merge, verify a fresh sample.

**cig_verify** (`cig/` built)
- Do: `verify.js` with `{out, stage: "cig", root}`.
- Check: tools, edges and run rows recounted independently from the runs and scores; node names are real tools; no unusable run inside; summary, flow graph and CSVs agree.
- Records: stage `cig`. If it fails: fix the cause (the mapping, or a starter), run `plite.py run`, verify again.

**study** (`study-init` copied the starters to `<OUT>/study/`; temporary)
- Do: `study.js` with `{out, root}`. Agents edit the starter sections for this data (Exploratory Data Analysis, Reward Signal, Affinity Diagram, Empathy Map, Impact), then one agent writes `<OUT>/scorecard_inputs/user_goal_problem.json`, validated by `plite.py report --validate-problem`. Fallback: `references/stage-signoffs.md`, last section.
- Check: numbers recomputed from `cig/` and `scores.csv`, quotes verbatim, screenshots looked at, the problem's runs read and found to share one real goal.
- Records: stage `study`. If it fails: rebuild the failing section from its starter, rewrite the problem until it validates, verify again.

**scorecard_verify** (`run` built both pages and deleted the study)
- Do: `verify.js` with `{out, stage: "scorecard", root}`.
- Check: every number on the page against `scorecard.json` and `cig/summary.json`; both pages opened in headless Chrome at 1280px and 390px; no forbidden wording; the `cig.html` tabs, panel and blurred names behave as `references/stage-signoffs.md` says.
- Records: stage `scorecard`. If it fails: fix at the source (the data, the problem file, `<OUT>/system5.json`, a copy of the template saved as `<OUT>/scorecard.template.html`), run `plite.py run`, verify again. Never patch the HTML by hand.

## Finish (`NEXT: done`)
Reply in two or three lines: the paths of `<OUT>/cig.html` and `<OUT>/scorecard.html`, and how many runs were graded. Nothing about repairs, exclusions, drops, fixes, sign-offs or models. Add one plain line only if something is unfixable or unverifiable, for example a dimension this export does not carry. No homework.

## Questions are allowed only when
- There is no data yet: ask which platform (LangSmith, Logfire, GCP, AWS, Azure, other OTel) and give `references/exports/<platform>.md`.
- Truly blocked: the input is empty or unreadable, or almost nothing survives verification. Say in one line what is missing and which export would fix it.

## References
`references/stage-signoffs.md` (what each verifier checks, the record, pass and fail), `ingest-checks.md` (mapping rescue, checks, unusable), `grading-brief.md`, `exceptions.md` (odd exports), `data-gaps.md` (what each field is for), `report.md` (the two deliverables), `concepts.md` (plain-words explanation; only if asked), `exports/` (platform walkthroughs).
