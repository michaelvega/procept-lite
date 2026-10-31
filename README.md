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

`procept-lite` builds that network from your exported logs (the **CIG**, conversation/cognition graph) and writes a one-page scorecard: how many runs reached the goal, where money and time leak, and what System 5 gives you. Everything runs locally on static files.

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

## Quickstart

1. Export your agent logs (any platform, any format; walkthroughs in `skills/procept-lite/references/exports/`).
2. Tell Claude "build the CIG" or run `/procept-lite:start <path>`.

Claude states a two-sentence plan (it only reads static files locally and changes nothing in your systems) and runs the loop below with no further questions. At the end you get `cig.html` and `scorecard.html` and the number of runs graded.

## How it works, and why

The scripts, templates and prompts in this plugin are **starters**. The agent reads them, copies them and edits them for your dataset; what stays fixed is the architecture and the file contracts between stages: raw export, `mapping.json`, `normalized/runs.jsonl`, grading shards, `scores.csv`, `cig/`, then the two pages. Code computes numbers and validates; models judge reality and write the words.

The agent runs `plite.py run`, which does the next code step and ends with one line `NEXT: <step> | <instruction>`. A Sonnet 5.5 or GPT 5.6 Terra agent does that step, then `run` again, until `NEXT: done`. The steps:

| Step | What a model does |
|---|---|
| `mapping_rescue` | always reads your raw rows and writes the field mapping; nothing is mapped automatically |
| `ingest_verify` | compares normalized runs with their raw rows |
| `grade` | blind readers grade every run +1 / 0 / -1 against the user's goal |
| `merge_verify` | re-reads a sample and every risky grade, corrects wrong ones |
| `cig_verify` | recounts the network from the runs |
| `study` | builds a temporary study from the starters and writes the one user-goal problem |
| `scorecard_verify` | checks every number and looks at both pages |

Each verification is signed off in a small record, and a later stage refuses to run on an artifact whose sign-off is missing or out of date. Why: a starter can be wrong for one dataset, and a wrong column or a wrong grade would go straight into the scorecard. A smaller correct sample is better than a larger noisy one, so a run that cannot be verified is classified **unusable** and left out of grading and every count.

Workflows for the Workflow tool are in `plugins/procept-lite/workflow/` (`mapping.js`, `grade.js`, `verify.js`, `study.js`); each documents a fallback for hosts without it.

Script directly: `python3 plugins/procept-lite/scripts/plite.py <run|status|check|scaffold|inspect|normalize|grade-prepare|grade-merge|cig|viewer|report|study-init|study-build|study-clean> --out ./procept-out`.

## Models (required)

Use **Sonnet 5.5** (`claude-sonnet-5-5`) or **GPT 5.6 Terra** as the minimum for every step where a model judges anything: mapping, verification, grading, the study and the problem write-up. Never Haiku, and no smaller model: it mislabels runs and the errors go straight into the scorecard. A smaller host model should delegate every judgement step to one of these. Anything above them is fine.

On Claude Code the bundled workflows use Sonnet 5.5. On a GPT host, run the same briefs with GPT 5.6 Terra.

## Output (in `procept-out/`)

| File | Content |
|---|---|
| `scorecard.html` | one-page scorecard: goals reached, error rate, latency, cost per run, the user-goal problem, and what System 5 gives you |
| `cig.html` | self-contained failure network (no network access needed) |

Everything else in the folder is a working file: `mapping_rescue/`, `mapping.json`, `normalized/`, `grading/`, `scores.csv`, `cig/`, `checks/`, `scorecard_inputs/`, `scorecard.json`, `status.json`, `cleaning_report.md`. The study is temporary and is deleted when the pages are built.

## Data requirements

Nothing is a hard blocker; a missing field leaves that dimension out or marks the affected runs unusable.

| Missing | Effect |
|---|---|
| Timestamps | latency is left out |
| Tokens / cost | tokens are estimated; dollars need prices |
| Error field | errors are derived from result text and status |
| Tool calls | the network has just an agent node |
| Run / conversation id | derived from session, trace or time gap |
| User message | the run is unusable |

Only an empty or unreadable input stops the pipeline, with a one-line message.

## Limits

Labels are model judgements about whether the user's goal was met, not user satisfaction, and different models can differ slightly. The System 5 figures are claims and conservative estimates from your data, not forecasts. Small samples are noisy. By default up to 300 runs are graded. Results cover only the data you provide.

## Test

```
python3 -m unittest discover -s tests
```
