# procept-lite

A Claude Code plugin that turns multi-agent traces (LangChain/LangSmith, Pydantic Logfire, GCP, AWS, Azure, any OpenTelemetry source) into a network showing what is failing on four dimensions: errors, cost, latency, and score (good +1, ambiguous 0, bad -1 against the user's goal), plus a one-page scorecard.

The unit of analysis is the run (one agent call); a conversation holds several runs. Nothing is aggregated before scoring. Everything runs locally; Python 3.9+ standard library only.

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

## Quickstart

1. Run `/procept-lite:start` (or just ask "why is my agent failing?" with your trace export at hand).
2. The skill explains the idea, tells you how to export traces from your platform, and checks the files (`/procept-lite:check`).
3. It estimates grading cost and asks before spending, then grades runs with Haiku agents (`/procept-lite:grade`).
4. It builds the network (`/procept-lite:build`) and the scorecard (`/procept-lite:report`).

Script directly: `python3 plugins/procept-lite/scripts/plite.py <inspect|normalize|grade-prepare|grade-merge|cig|viewer|report|run> --out ./procept-out`.

## Output files (in `procept-out/`)

| File | Content |
|---|---|
| `inspect.md`, `inspect.json` | GREEN/AMBER/RED data sufficiency check |
| `normalized/runs.jsonl` | one canonical dossier per run |
| `grading/` | shards, brief, per-shard scores, audit |
| `scores.csv` | outcome_score (1/0/-1), confidence, rationale, persona, issue per run |
| `cig/` | nodes, edges, runs, convs, errors CSVs, `flow_graph.json`, `run_dossiers.jsonl`, `summary.json` |
| `cig.html` | self-contained network viewer (no network access needed) |
| `REPORT.md`, `REPORT.docx` | one-page scorecard and explanation |

## Data requirements

| Need | Used for | If missing |
|---|---|---|
| Run id (agent call id or root trace id) | unit of analysis | derived from trace id, lower fidelity |
| Conversation / session / thread id | grouping runs | each run treated as its own conversation |
| Timestamps | latency, ordering | latency unavailable |
| User message and final answer | grading | RED, cannot grade |
| Tool calls with args and results | network, errors | AMBER, no tool graph |
| Tokens and/or prices | cost | cost reported in tokens only |

## Limits

Labels are model judgements about whether the user's goal was met, not user satisfaction. Estimates (goal reach, ~50% cost reduction, error and latency) are bounds, not forecasts. Small samples give wide uncertainty. Results cover only the data you provide.

## Test

```
python3 -m unittest discover -s tests
```

## License

TBD (placeholder).
