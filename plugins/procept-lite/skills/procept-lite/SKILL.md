---
name: procept-lite
description: Turn multi-agent traces into a failure network and one-page scorecard. Use for agent traces, LangSmith, LangChain, Pydantic Logfire, OpenTelemetry, GCP/AWS/Azure agent logs, agent evaluation, "why is my agent failing", grading agent runs good/ambiguous/bad, cost and latency of agents, CIG, scorecard.
---

# procept-lite

Orchestrator. Run every script as `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/plite.py <cmd> --out <OUT>`. Pick `<OUT>` once (default `./procept-out`) and reuse it.

## Rules (always)
- Unit of analysis is the RUN (one agent call). A conversation holds several runs. Never aggregate before scoring.
- Never send the user's data anywhere. Everything runs locally; graders are local subagents.
- Report what the data shows, including unflattering results. Reader-agent labels are model judgements, not user satisfaction; estimates are bounds, not forecasts.
- Graders never see vendor scores or evaluator labels (no leakage).
- Do not print secrets, tokens or emails found in traces.

## Step 0: explain and collect
1. Explain in plain words (see `references/concepts.md`): agent traces are logs of what each agent did; we turn them into a network of tools showing where runs fail on four dimensions: errors, cost, latency, score (good +1, ambiguous 0, bad -1 against the user's goal).
2. Ask which platform they use: LangChain/LangSmith, Pydantic Logfire, GCP, AWS, Azure, other OpenTelemetry source. Give the matching walkthrough: `references/exports/<platform>.md` (list the directory for names).
3. Ask where the exported files are (files or a folder). Remind them a small sample is fine to start.

## Step 1: check the data
Run `inspect --src <paths...>`. Explain the GREEN/AMBER/RED table: each row is a requirement (run id, conversation id, timestamps, user message, final answer, tool calls with args/results, tokens/cost). GREEN = ready, AMBER = works with reduced fidelity, RED = blocks.
- READY: go to Step 2.
- Not ready: walk through `references/data-gaps.md` with the user. Offer to write a `mapping.json` (field name mapping, see `references/schema.md`) and retry `inspect`/`normalize --map mapping.json` until `normalize` works. Ask for prices (`--prices prices.json`) if cost is missing and they want dollars.

## Step 2: normalize and grade
1. `normalize --src <paths...> [--map mapping.json] [--prices prices.json]`.
2. `grade-prepare` and show the printed run count and cost estimate. Ask the user for a go-ahead before spending anything.
3. Grade with Haiku agents following `references/grading-brief.md`:
   - If the Workflow tool is available, run `${CLAUDE_PLUGIN_ROOT}/workflow/grade.js` with the args JSON written by grade-prepare.
   - Else spawn Agent-tool subagents with model haiku in parallel batches, one per shard in `<OUT>/grading/shards/`, each writing its CSV to `<OUT>/grading/scores/`.
4. `grade-merge`. If shards are missing or invalid, re-run only those shards and merge again. Report the audit agreement figure honestly, even if low.

## Step 3: build the CIG
Run `cig`, then `viewer`. Explain the four dimensions and how to read the network (nodes are tools, edges are call order, colour by score; toggle dimensions).

## Step 4: report
Run `report`. It writes REPORT.md and REPORT.docx (scorecard: good/ambiguous/bad split, error/cost/latency metrics, top 3 buyer personas with goal and top pain, reachable goals, ~50% cost reduction, error and latency estimates, fix bullets). See `references/report.md`.

## Finish
Present the paths: `<OUT>/cig.html`, `<OUT>/REPORT.md`, `<OUT>/REPORT.docx`. State the honest limits: labels are model judgements on the sampled runs, estimates are bounds not forecasts, and only the data provided was examined. Offer next steps: grade more runs, add prices, fix the top issue.

Shortcut: `run --src <paths>` chains inspect, normalize and, once `scores.csv` exists, cig, viewer and report.
