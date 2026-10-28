# What is going on (plain language)

## The pieces
- **Span**: one logged step, such as one model call or one tool call.
- **Trace**: all the spans from one piece of work, stacked together.
- **Run**: one attempt by your agent to answer one request. This is our unit. One request in, one answer (or failure) out. Runs are identified by `gen_ai.agent.call.id` when present, otherwise by the trace id.
- **Conversation**: a chat that holds several runs. We score each run on its own and never average before scoring.

## Why we do not trust vendor scores
Dashboards report "success" when no exception was thrown. An agent can run cleanly and still give a useless answer, or hit a tool error and recover well. So a cheap model reads each run (the user's ask, the tool calls, the final answer) and judges it against what the user was trying to get done.

Labels: **good (+1)** the goal was met, **ambiguous (0)** unclear or partly met, **bad (-1)** the goal was missed. These are model judgements, not user satisfaction.

## The 4 dimensions
1. **Errors**: tool calls that failed, failures never recovered, runs that end with no answer.
2. **Cost**: tokens (and dollars if you give prices).
3. **Latency**: seconds per run and per tool.
4. **Score**: good / ambiguous / bad.

## What you get
- A **network picture (CIG)**: each tool is a dot, lines show which tool follows which. Switch between the four dimensions to see where failures, cost and delay pile up.
- A **one-page scorecard** (Markdown and Word): good/ambiguous/bad split, error, cost and latency numbers, the top 3 buyer goals and their main pain, and what could be reached with fixes (estimates are bounds, not forecasts).

## What it costs
Grading uses a small, cheap model, once per run.

    cost = runs x (tokens_per_run / 1,000,000) x price_per_million_tokens

Where tokens_per_run is roughly the run's text length in characters divided by 4 (typically 1,000 to 6,000), plus about 300 for the answer. Example: 1,000 runs x 3,000 tokens = 3M tokens; at a few dollars per million input tokens that is a few dollars. Check current prices with your provider. Everything else (inspect, normalize, graph, report) runs locally for free.

## Privacy
- Traces contain real user text, and sometimes names, emails and keys. Treat the files as sensitive.
- Everything except grading runs on your machine, with no network access.
- Grading sends each run's text to the model you choose. Nothing else is sent anywhere.
- To reduce exposure: filter out sensitive projects before export; strip or mask emails, phone numbers and keys from the files before normalizing (a simple regex pass is enough, see `data-gaps.md#message-content`); or export only a sample.
- Delete the output folder when you are done.
