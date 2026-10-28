# One-page report

`python3 plite.py report --out DIR` reads `cig/runs.csv`, `cig/summary.json`, `cig/errors.csv`, `cig/nodes.csv`, `scores.csv` and (for the persona goal line) `cig/run_dossiers.jsonl`, then writes `REPORT.md` and `REPORT.docx` with identical content.

## Contents, in order
1. Title and scale (runs, conversations). The unit is the run; nothing is aggregated before scoring.
2. Scorecard: outcome split (good / ambiguous / bad, count and %); one table with errors (runs with a tool failure, last call failed, no answer), cost (median tokens, $ when cost data exists, median tool calls) and latency (median, share in slowest decile), each split by outcome; top 3 buyer personas by run count with goal (the most common ask, or blank if none repeats or it looks unsafe), "fell short" % (bad + ambiguous) and top pain (most frequent issue label among their bad/ambiguous runs).
3. What we can reach: four bounds, each with its derivation. User goals (good share now, ceiling if every bad/ambiguous run with a mechanical problem were fixed, half-way figure). Cost (tokens in bad/ambiguous runs, excess of heavy runs over the median, tokens spent after failures; says whether a 50% cut is supported and by how much). Errors (failed-call rate now, and if the top 3 error classes are fixed). Latency (median/p90 now, and if the slowest decile is brought to the median).
4. What the data says (4 to 6 sentences) and 4 to 6 fix bullets, each with the number of runs it touches.

Fix bullets come from rules, not free text: top failing tool and its error class (validate/constrain the input), runs with no answer (always end with an answer or say what is missing), runs with 3+ failures (stop after 2 identical failures), heavy runs (cap tool calls, plan first), slow tail (timeouts, smaller scopes), top issue labels (targeted fix). They are sorted by runs touched.

## One page
Body text is held to about 460 words plus two small tables. `report.py` retries with shorter text (fewer sentences and bullets, shorter cells) until the word budget and a line-height estimate fit. If `soffice` is installed, the docx is converted to PDF and its pages are counted; otherwise the page check is the estimate and the command says so.

## Not graded yet
If `scores.csv` is missing or empty, the report still shows errors, cost and latency for all runs, says "not graded yet", and omits the outcome split, personas and goal-reach bound.

## Honest limits
Labels are reader-agent judgements of whether the user's goal was met, not user satisfaction. Every figure under "What we can reach" is a bound derived from the runs (upper limit if the named problems were removed), not a forecast. The retry-waste lever approximates tokens after a failure as the failed-call share of a failing run's tokens, and levers are counted once per run (union).

## Docx
`docx_writer.py` uses only `zipfile` and XML strings: 0.7in margins, 10pt body, bordered tables, bold runs, bullets by hanging indent. Opens in Word and LibreOffice.
