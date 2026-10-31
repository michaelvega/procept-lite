# What is going on (plain language)

## The pieces
- **Span**: one logged step, such as one model call or one tool call.
- **Trace**: all the spans from one piece of work, stacked together.
- **Run**: one attempt by your agent to answer one request. This is our unit. One request in, one answer (or failure) out. Runs are identified by `gen_ai.agent.call.id` when present, otherwise by the trace id.
- **Conversation**: a chat that holds several runs. We score each run on its own and never average before scoring.

## Why we do not trust vendor scores
Dashboards report "success" when no exception was thrown. An agent can run cleanly and still give a useless answer, or hit a tool error and recover well. So a capable model (Sonnet 5.5 or GPT 5.6 Terra minimum) reads each run (the user's ask, the tool calls, the final answer) and judges it against what the user was trying to get done.

Labels: **good (+1)** the goal was met, **ambiguous (0)** unclear or partly met, **bad (-1)** the goal was missed. These are model judgements, not user satisfaction.

## Why every step is checked
The scripts that clean and count are starters. They can misread an unusual export, and one wrong column would quietly poison everything after it. So after each stage a capable model compares the result with the real data and signs it off: how the export was mapped into runs, the grades, the graph, the final pages. Anything it cannot verify is left out instead of guessed. A smaller correct sample is better than a bigger noisy one.

## The 4 dimensions
1. **Errors**: tool calls that failed, failures never recovered, runs that end with no answer.
2. **Cost**: tokens (and dollars if you give prices).
3. **Latency**: seconds per run and per tool.
4. **Score**: good / ambiguous / bad.

## What you get
- **cig.html**, a network picture (the CIG): each tool is a dot, lines show which tool follows which. The Score view shows where runs go well and where they fail. Errors, cost and latency for every node are in System 5, linked from the page.
- **scorecard.html**, one page: how many runs reached the user's goal, the error rate, latency and cost per run, one real user goal where people fall short and what blocks them, and what implementing System 5 would change.

## What it costs
Model work happens in four places: mapping your export, checking each stage, grading each run, and a short temporary study that finds the user goal behind the scorecard. Grading is the bulk, once per run:

    cost = runs x (tokens_per_run / 1,000,000) x price_per_million_tokens

Where tokens_per_run is roughly the run's text length in characters divided by 4 (typically 1,000 to 6,000), plus about 300 for the answer. Example: 1,000 runs x 3,000 tokens = 3M tokens; at a few dollars per million input tokens that is a few dollars. The checks and the study read samples, not every run, and add to that figure. Check current prices with your provider. Everything else (clean, normalize, graph, pages) runs locally for free.

## Privacy
- Traces contain real user text, and sometimes names, emails and keys. Treat the files as sensitive.
- Everything except the model steps runs on your machine, with no network access.
- The model steps (mapping, checks, grading, study) send run text to the model you choose. Nothing else is sent anywhere.
- To reduce exposure: filter out sensitive projects before export; strip or mask emails, phone numbers and keys from the files first (a simple regex pass is enough, see `data-gaps.md#message-content`); or export only a sample.
- Delete the output folder when you are done.
