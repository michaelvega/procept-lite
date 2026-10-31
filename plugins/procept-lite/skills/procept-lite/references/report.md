# The two deliverables

The user receives exactly two files in `<OUT>`: `cig.html` and `scorecard.html`. Both are single self-contained pages (no external requests, no fonts or scripts from anywhere). Everything else in `<OUT>` is a working file. The study is temporary and is deleted when the pages are built.

`python3 plite.py report --out <OUT>` writes `scorecard.html` and `scorecard.json` (every number shown on the page) from `cig/summary.json`, `cig/runs.csv`, `cig/errors.csv`, `scores.csv` and `scorecard_inputs/user_goal_problem.json`. It runs a scan of the page for forbidden wording and blank cells, and the build fails on a hit. Code computes every number; the problem write-up is written by a model.

The page template is a starter: `templates/scorecard.template.html`. A copy saved as `<OUT>/scorecard.template.html` wins over it, and `<OUT>/system5.json` wins over `templates/system5.json`. Edit those, never the built HTML.

## scorecard.html, top to bottom
1. Header: a kicker with the number of runs graded and the date range, the title "Agent Run Scorecard", and a subtitle saying that errors, resource spend and latency are objective metrics, that the outcome signal comes from an LLM judge so different LLMs can differ slightly, that it is a robust estimate for the small amount of data, and that smarter models give better results though they may cost more.
2. Four tiles: User goals reached (share of good runs), Error rate, Latency, Cost per run, each with one plain line.
3. One block, "User goal problem we found": a headline, the goal, what blocks the user, a verbatim quote, and the computed numbers (runs, share that fell short). Exactly one user-goal group, always a real goal.
4. "Implementing System 5 gives you": four rows, User goals, Cost, Errors, Latency, each showing now and with System 5, from the claims in `system5.json`. Goals: good share x (1 + goal_gain_pct / 100), capped at 100 percent. Cost: per run x (1 - cost_cut_pct / 100), in dollars when the data has cost, else tokens. Errors: the failed-call share after half of the failures the top error classes account for are removed. Latency: the slowest-tenth time moved half of the way to the median.
5. Charts below: the outcome split as a stacked bar, and per-outcome metric bars. Bars start at zero, labels are direct, n is shown.
6. A closing line with the accuracy gain and the four metrics, and a button to the System 5 link.

## cig.html
The failure network. The Score tab is the default and works. Errors, Cost and Latency carry a lock marker and open the System 5 link in a new tab. Clicking a node, edge, list row or the canvas opens a side panel with a short teaser and the same link. Two or three tool names are blurred, and their real names are not in the file. The page holds only what the graph needs, no run dossiers.

## Words
Neither page ever shows repair, drop, fix, mistake or error-admission wording, statistics jargon, internal paths, check records, placeholders, blank cells or cut-off sentences. If something would look wrong, fix it at the source. The final message to the user is short: the two paths and how many runs were graded.

## Honest limits
Labels are model judgements of whether the user's goal was met, not user satisfaction. The System 5 rows are claims and conservative estimates from the data, not forecasts. Results cover only the data provided.
