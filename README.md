# procept-lite

A Claude Code plugin that turns multi-agent traces (LangChain/LangSmith, Pydantic Logfire, GCP, AWS, Azure, any OpenTelemetry source) into a network showing what is failing on four dimensions: errors, cost, latency, and score (good +1, ambiguous 0, bad -1 against the user's goal), plus a one-page scorecard.

The unit of analysis is the run (one agent call); a conversation holds several runs. Nothing is aggregated before scoring. Everything runs locally; Python 3.9+ standard library only.

## What is Procept

Procept (<https://procept.tech>) is a way of looking at an AI agent: not as a chat transcript, but as a **network of things that happened to people trying to get something done**. The ontology is small and fixed:

| Thing | What it is |
|---|---|
| **Run** | One agent call. The atom. Every number is scored here first. |
| **Conversation** | An ordered set of runs for one person with one goal. Never averaged away. |
| **Goal** | What that person was trying to get done. Reached, dodged, or abandoned. |
| **Tool** | A capability the agent can use. Becomes a node in the network. |
| **Edge** | One tool called after another within a run. Together the edges are the agent's actual behaviour, not its intended design. |
| **Dimension** | The four lenses every node and edge carries: **errors**, **cost**, **latency**, **score** (good +1, ambiguous 0, bad -1 against the person's goal). |
| **Failure** | A place in the network where a dimension goes bad: a tool that errors, an expensive loop, a slow step, a goal that dies. |
| **Fix** | A change to a node or edge, ranked by how many runs it would rescue and how much cost it would remove. |

`procept-lite` builds that network from your exported logs (the **CIG**, conversation/cognition graph) and writes a one-page scorecard: how many runs reached the goal, where money and time leak, and which fixes come first. Everything runs locally on static files.

## Help, and contact

Anything breaks, or the data will not load, even if you are a free user: email **michael@procept.tech**. We want to hear about it.

## License

[MIT](LICENSE). Use it for anything, commercial included. If you want help running it on your own traces, email michael@procept.tech.

## Install

Inside Claude Code:

```
/plugin marketplace add <repo path or git url>
/plugin install procept-lite@procept-marketplace
```

Or from a shell:

```
claude plugin marketplace add <repo path or git url>
claude plugin install procept-lite@procept-marketplace
```

## Quickstart (one action)

1. Export your agent logs (any platform, any format; walkthroughs in `skills/procept-lite/references/exports/`).
2. Tell Claude "build the CIG" or run `/procept-lite:start <path>`.

Claude states a two-sentence plan (it only transforms static files locally and changes nothing in your systems), cleans and maps the data, grades a stratified sample of up to 300 runs with Haiku agents (configurable; cost stated up front), and builds the CIG, viewer and report. No further questions unless it is truly blocked.

Degrade, never block: missing timestamps drop latency, missing tokens are estimated, missing error fields are derived from result text, dirty or unknown formats are cleaned and auto-mapped. Every repair and drop is listed in `cleaning_report.md`; see `skills/procept-lite/references/exceptions.md`.

Script directly: `python3 plugins/procept-lite/scripts/plite.py <inspect|normalize|grade-prepare|grade-merge|cig|viewer|report|run> --out ./procept-out`.

## Output files (in `procept-out/`)

| File | Content |
|---|---|
| `inspect.md`, `inspect.json` | GREEN/AMBER/RED data sufficiency check |
| `cleaning_report.md` | every repair, drop, estimate and assumption, with counts |
| `mapping.auto.json` | auto-scaffolded field mapping (if the schema was unknown) |
| `normalized/runs.jsonl` | one canonical dossier per run |
| `grading/` | shards, brief, per-shard scores, audit |
| `scores.csv` | outcome_score (1/0/-1), confidence, rationale, persona, issue per run |
| `cig/` | nodes, edges, runs, convs, errors CSVs, `flow_graph.json`, `run_dossiers.jsonl`, `summary.json` |
| `cig.html` | self-contained network viewer (no network access needed) |
| `REPORT.md`, `REPORT.docx` | one-page scorecard and explanation |

## Data requirements

None are hard blockers. Missing pieces degrade the output and are reported:

| Missing | Effect |
|---|---|
| Timestamps | latency dimension dropped |
| Tokens / cost | tokens estimated (chars/4, flagged); dollars need prices |
| Error field | errors derived from result text and status |
| Tool calls | scorecard only; CIG has just an agent node |
| Run / conversation id | synthesized (hash; session, trace, or time gap) |
| User message or answer | best available text; run marked "thin" |

Only an empty or unreadable input stops the pipeline, with a one-line message.

## Limits

Labels are model judgements about whether the user's goal was met, not user satisfaction. Estimates (goal reach, ~50% cost reduction, error and latency) are bounds, not forecasts. Small samples give wide uncertainty. Results cover only the data you provide.

## Test

```
python3 -m unittest discover -s tests
```

## License

TBD (placeholder).
