# Study brief (read fully, then build ONE section)

You build one section of a study of THIS dataset: five sections about how an agent's runs served its users. Everything in this folder is a STARTER. Read it, copy it, edit it, replace it. A `sections/<key>.py` starter is a first draft of the code, not the answer. The words, personas, themes and fixes are yours and must come from the runs in front of you: no sentence in a section is a fill-in-the-blank template. Code computes numbers and checks; you judge what is true and write it down.

Model floor: whenever you judge anything (name a persona, pick a theme, read a reaction, rank a fix) you must be Sonnet 5.5 or GPT 5.6 Terra, or better. Never hand a judgement to a smaller model.

Work only inside this study folder (`<out>/study/`): your own `sections/<key>.*` files plus the companion files your section owns (listed below). Never edit `sig.py`, `shell.css`, `build_study.py`, `validate_sections.py`, `problem_candidates.py` or another section's files; if one is wrong for this data, say so in your return. No network. Run Python as `python3 -I`. Never print or copy secrets or e-mail addresses.

```
study/  BRIEF.md  sig.py  shell.css  build_study.py  validate_sections.py  problem_candidates.py
        sections/      K.py starters and your outputs K.html K.md K.json (+ companions)
        fix_reports/   F<n>.html, written by the impact section
```

## The signal: use sig.py, never recompute

```python
import os, sys
STUDY = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # this folder; sections/ is inside it
sys.path.insert(0, STUDY)                                             # python3 -I does not add the script's folder
import sig
R = sig.load()                                                        # one dict per graded, usable run, in time order
```
The keys are listed in the docstring of `sig.py`; read it first. For a full run (every tool call with arguments and results) read `cig/run_dossiers.jsonl` in the folder above, by run id.
- PRIMARY signal: `outcome` / `label`: good (+1), ambiguous (0), bad (-1). A model read each run against what the user asked. "Fell short" means ambiguous or bad.
- SECONDARY signals are mechanical: `tool_fail`, `unrecovered`, `no_answer`, `slow`, `heavy`.
- Any other quality score (vendor or evaluator scores, ratings) is the thing under TEST, never the reference.
- The labels are model judgements, not user satisfaction. The page footer says so once; do not repeat it.
- `sig.load()` already left out the runs that were not graded and the runs that cannot be trusted. Never add them back, count them or mention them.
- `persona_raw` and `issue_raw` are the graders' own labels: leads to check against the asks, not answers.
- Show anything taken from a run with `sig.short(text, n)` (it masks e-mails, numbers and secrets).
- Read at least 30 real runs (asks, answers, tool results), spread across outcomes, before you write a word. If the data does not support something a starter assumes, change the starter.

## Style (hard)

- Look: the shell. `shell.css` holds the tokens and classes; build the page once (`python3 -I build_study.py`) and look at it. Cyber-brutalist: black page, green accent, thin rules, no cards or shadows. Colours by meaning only: good `var(--green)`, ambiguous `var(--muted)`, bad `var(--bad)`, `var(--warn)` rarely. Always CSS variables, never hard-coded colours (light mode must work). Readable at 390 px and 1280 px, both themes, no horizontal page scroll (put wide tables in `<div class="wrap">`).
- Tufte: high data-ink, direct labels instead of legends, small multiples, sparklines, dot and strip plots, unit charts; no gridlines, boxes, 3D, pies, gauges or decorative colour. Bars start at zero. One claim per figure. Show the n on every figure.
- Words are the main constraint. The prose of a section is at most 110 words. One headline sentence (the finding, with its number), captions of at most 15 words, no paragraphs, no jargon. Long lists go in `<details class="dd">` closed by default. The validator counts every word outside tables, captions, SVG and closed `<details>`, so chart labels belong in SVG text or table cells.
- Every number in a sentence is computed by the script and formatted into the text, so a re-run keeps words and figures in agreement. Never type a figure by hand.
- Header contract, exactly this at the top of the fragment:
  ```html
  <section id="K" class="step">
  <p class="kicker">Section n · Name</p>   <!-- build_study renumbers it -->
  <h2 class="big">Name</h2>                <!-- the exact title: Exploratory Data Analysis | Reward Signal | Affinity Diagram | Empathy Map | Impact -->
  <p class="sub">One line of at most 18 words: the question this section answers.</p>
  <p class="lede">The finding in one or two sentences, with the numbers.</p>
  ```
  Figures are `<figure class="fig"><figcaption><span class="kicker">Figure</span> caption</figcaption> ... </figure>`; the assembler writes `Figure n.m`. Put one `<style>` block in the fragment with every selector prefixed by `#K`. No scripts needed; no external fonts, images or requests.

## Honesty and privacy

- Report what the data shows, including when it is unflattering or inconclusive. Never overclaim; small groups get `n=` and a hedge word.
- No e-mails, person names, addresses or phone numbers. Verbatim user quotes are at most 140 characters. Run ids appear as their first 8 characters.
- No data plumbing in any text: nothing about export size, coverage, dropped, repaired or excluded rows, parsers or sign-offs. If something looks off in the data, fix it in code at the source and do not mention it.
- No statistics jargon anywhere: no agreement coefficients, area-under-curve scores, p-values, calibration or confounding talk. When a statistic helps, say what it means in plain words with its numbers ("a good run scored above a bad run in 70% of pairs, n=131 and 17"). The validator rejects the jargon.
- No placeholders: no "N/A", "nan", "None", "TODO", no blank table cells, no sentence cut off with "...".

## Personas and their goals and pains (the empathy section; every section that names groups of users follows it)

- A persona is a group of runs defined by the USER GOAL: what the user was trying to get done. Not by outcome, tool, agent or environment.
- Name: 2 to 4 words that say who is asking or what for. Illustration from another domain: "Invoice chaser".
- Goal: a real user intent in full words, one sentence ending with a period; concrete (what, about what), never blank, never cut off, never a category word like "misc". Illustration: "Get the overdue invoices of last quarter chased without writing each reminder by hand."
- Pain: one sentence ending with a period naming what most often blocks this persona, taken from the failures of its runs; concrete, never blank, never "errors". Gain: what works for them, same form (optional).
- No persona called other, unclear, unknown or misc may cover more than 15% of the runs, and none may be the biggest persona. Split, merge or re-read until every run lands in a real goal.
- Follow-ups ("thanks", "no, that is wrong", "try again") have no goal of their own: give them the persona of their conversation (use `conversation_id`).
- Every graded run appears exactly once in `personas.csv`; every persona in it has a `personas.json` entry.

## The five sections (page order; each name is the exact title)

**1. eda, Exploratory Data Analysis.** Question: what does the system do and what do its runs look like? Findings a product manager can act on, not data plumbing: what the system does (one tight sentence of at most 40 words, from the asks and the tool names) with a strip of tool families (you decide the families from the tool names); the 3 most typical good runs and the 3 worst, one line each (ask, then what happened); the most representative failures; then classic summary stats as small multiples: runs per day, environments (`env`), agents, latency, tokens and tool-call distributions split by outcome, top tools with their failure rate, the mix of ask types.

**2. reward, Reward Signal.** Question: which quality signals can stand in for "the user got what they came for"? Test every signal that exists for this data against our outcome labels, each on its own scale (never pool scores from different sources):
- Scores in the RAW export. If the raw export carries any judge, evaluator, rating or pass/fail field, extract it by run id with a small script (the graders never saw it). The host agent gives you the export's path; `inspect.json` lists its files and `mapping_rescue/sample_raw.jsonl` shows its record shapes. Say what it is, how many runs have it, how spread out it is (is nearly every score the same?) and whether it separates good runs from bad ones. If the export carries no such field, leave this out without comment.
- The user's next message (`next_ask`). Read the pairs yourself and label each reaction (repeats or corrects the ask, complains, thanks or moves on, nothing to infer) instead of trusting keyword rules; sample if there are more than about 300. Report how often each reaction follows good, ambiguous and bad runs.
- Mechanical signals: how often each of `tool_fail`, `unrecovered`, `no_answer`, `slow`, `heavy` flags a run that fell short, and how often it flags a good run.
Plain words only; each figure answers one question ("when this signal flags a run, how often did it fall short?") with the n shown. Close with a one-line verdict per signal: it tracks, partly tracks, or does not track the outcome, whichever the data says.

**3. affinity, Affinity Diagram.** Question: which kinds of failure recur and which runs belong to each? Build it bottom-up from the grader rationales and tool-error text of every run that fell short.
(a) First, the GENERAL THEME CLUSTERS: the experience-level ways a user was badly served, 4 to 6, each with its run count and a one-line example; they are not the issue list again. Include each of these when the data supports it: goal dodged entirely (the agent sidestepped, refused, stalled or ended with no answer, so the user got nothing toward the goal); upsell or roadmap opportunity (the user asked for a capability or data the product does not have yet, a dead end that is a product-gap signal: name the capability asked for); pile-up (one run hit many problems at once: several failed calls, retries, slow, a partial answer); delivered but wrong or incomplete. Add the others the data shows; never force a theme it does not show.
(b) Then the topic clusters, drawn as a diagram: boxes sized by runs, grouped under their theme.
(c) Last, every distinct issue in ONE closed `<details class="dd">`: issue, runs, bad/ambiguous split, one example run id.
Companions: `sections/issues.csv` with the columns `run_id,issue,topic,theme` has at least one row (non-blank issue) for EVERY run that fell short; each issue maps to exactly one theme; a run that belongs to several themes gets one row per theme. `sections/affinity.json` is the section's json (theme counts, issue counts).

**4. empathy, Empathy Map.** Question: who is asking, what does each group want, where are they served well or badly? 4 to 6 personas by user goal (rules above), derived from the first asks. Per persona: a unit chart of its runs (one cell per run, coloured good, ambiguous or bad, same scale in every panel), n, good share, median latency, tool-failure %, top pain, top gain, and optionally a closed `<details class="dd">` walkthrough of one conversation where it failed (the ask, what the agent did, what went wrong; short). Companions: `sections/personas.csv` (`run_id,persona`) and `sections/personas.json` as `{"personas": [{"name", "goal", "pain", "gain", "n", "good_share"}]}`, with n and good_share computed by code.

**5. impact, Impact.** Question: which fixes would move the most short runs to good, and at what effort? Ranked fixes, one per theme or issue worth fixing (6 to 8 at most; fewer if the data supports fewer). A table or dot plot: fix, runs it touches, runs that fell short and are addressable, best-case gain in good share in percentage points (overlaps removed; a bound, not a forecast), effort S, M or L (analyst judgement, labelled as such). Each fix links to its own report `fix_reports/F<n>.html`: a standalone page in the shell look (inline the contents of `shell.css`), at most 250 words: the problem, evidence with run ids, the change, how to measure that it worked. Reads `sections/issues.csv` and `sections/affinity.json`. Companion: `sections/impact.json` as `{"fixes": [{"id": "F1", "title", "runs", "bad", "ambiguous", "gain_pts", "effort"}]}`.

## Output contract

For your key K write `sections/K.py`, re-runnable with `python3 -I sections/K.py` (it reads `sig`, plus the other sections' files only where listed above), producing `sections/K.html` (the fragment), `sections/K.md` (a brief plain-text version, bullets, starting with `## Title`) and `sections/K.json` (the headline numbers used in the text), plus your companions.

Dependency order: eda, reward, affinity and empathy are independent and can be built at the same time. impact needs the affinity files, so it starts after affinity. Then the problem write-up (below) runs last, once every section validates.

## Check your work (do not claim done from code alone)

1. `python3 -I sections/K.py`, then `python3 -I validate_sections.py`. Your lines must be gone (other sections may still be missing; ignore their lines). Then `python3 -I build_study.py` assembles `study.html` (it skips missing sections).
2. Screenshot your section with headless Chrome at 1280 px and at 390 px, dark and light, and LOOK at the images: nothing clipped or overlapping, text readable, nothing running off the right edge. Fix what you see. Put every screenshot and scratch copy in your session's scratch folder, never in `<out>` or a repository, and delete them when done. Recipe:
   - Make a scratch copy of `study.html`; add `data-theme="dark"` (or `"light"`) to its `<html>` tag; replace `NNvh` with `NNpx` (otherwise the page is thousands of pixels of blank); add `<style>.mast,section.step:not(#K){display:none}</style>` before `</head>` to see only your section.
   - `"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless --disable-gpu --hide-scrollbars --window-size=1280,2400 --screenshot=<scratch>/K-1280-dark.png file://<scratch>/copy.html` (use the Chrome you have on other systems).
   - Headless Chrome will not render narrower than about 500 px, so for 390 px wrap the copy in a page with `<iframe src="file://<scratch>/copy.html" style="width:390px;height:2400px;border:0">` and shoot that page at `--window-size=520,2400`.

## Not your job: the problem write-up

When all five sections validate, a separate model step runs `python3 -I problem_candidates.py` and writes `<out>/scorecard_inputs/user_goal_problem.json` from the candidates it finds: the one user-goal group where the most runs fell short, told as a specific, evidence-based sentence of the form "this user wants to do X but cannot because Y" (an example of tone, never a template). Section agents never write or edit anything in `scorecard_inputs/`. Your personas feed it, so make each goal and pain specific.

## Return

Files written, 3 headline numbers, anything surprising or a limit you hit. At most 120 words.
