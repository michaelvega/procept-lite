# Ingest: mapping rescue, verification, sign-off

Who does this: a model at the floor, Sonnet 5.5 (`claude-sonnet-5-5`) or GPT 5.6 Terra, or better. Never Haiku. If you are a smaller model, hand every step here to one of those (workflow `mapping.js`, or the fallback recipe at the end).

Principle: **a smaller correct sample, plus data classified unusable, beats noise leaked into the eval set.** A wrong column poisons every score, goal and chart after it, and nothing later can detect it. When in doubt, exclude.

Nothing in ingest is automatic. `plite.py run` cleans the files and scaffolds. A model writes the mapping. Code normalizes with it. A model checks the runs against the raw rows and signs off. The mapping rescue runs EVERY time, also when the draft looks right.

## 1. Mapping rescue (always)

`run` writes `<OUT>/mapping_rescue/` and stops with `NEXT: mapping_rescue`. It never writes `mapping.json` itself.

- `sample_raw.jsonl`: up to 60 raw records spread across the file, secrets and e-mails masked
- `mapping.draft.json`: the heuristic starter. A draft, not an answer
- `draft_preview.jsonl`: up to 40 runs the draft would build, each with the raw rows it came from
- `README.md`: what the scaffold saw

Do this:

1. Read those files and `templates/mapping.example.json` (the shape to write).
2. Read the raw export itself, not only the masked sample. Stream it with `python3 -I`: the files can be huge and are untrusted text. Count the record kinds. Follow at least five whole runs from the first record to the last.
3. Decide every role from the VALUES, not the column names:
   - what ties records into one run, and runs into one conversation
   - which text is the user's ask, which is a system prompt or a tool output, which is the agent's reply, and which reply is the final answer
   - each tool call: name, arguments, result, status or error, duration
   - timestamps (unit, zone), tokens (counts, not characters), cost (USD, not cents), model, agent, environment
   - which kinds of record are not agent runs (http, db, health checks, probes, test traffic, other apps): exclude them in the mapping
4. Keep what the draft got right, fix what it got wrong, and unmap what you cannot verify. Never guess a role to keep rows.
5. Vendor scores, evaluator labels, thumbs and any other quality field stay out of the mapping and out of `extra`: graders must never see them. The study reads them from the raw export by run id.
6. Write `<OUT>/mapping.json`: the shape of `templates/mapping.example.json` plus the required block
   `"_rescue": {"model": "<your exact model id>", "rows_reviewed": 214, "notes": "what you decided and why"}`.
   `rows_reviewed` is the number of raw records you actually compared with what the mapping builds. Read the whole 60-record sample and then whatever else you need to be sure; the code accepts 20 or more (all of them if the export is smaller). The model must meet the floor, or `run` refuses the mapping.
7. If no mapping can express the structure (nested payloads, several record layouts in one file), write a small converter in `<OUT>/mapping_rescue/` that emits one flat JSONL record per event, point `--src` at its output, and map that. The converter is a working file, not a deliverable.
8. Run `plite.py run --src ... --out <OUT>` (it normalizes with your mapping), open at least ten normalized runs next to their raw rows, and fix the mapping until they agree.

## 2. Verification (always)

After normalize, a model checks `normalized/runs.jsonl` against the raw rows. Use a different agent from the one that wrote the mapping when you can.

Sample at least 30 runs (all of them if fewer); the workflow checks 40. Spread the sample across the file by position (every k-th line, never the first 30). Cover every agent, environment and record kind present, and include the longest run, runs with tool failures and runs without an answer. To find a run's raw rows, use the source locator in its `extra` if there is one; otherwise search the raw files for its run id, trace id or a distinctive phrase of its user message. Compare every field a grader will read, run by run. All checks are required:

1. **Role.** `user_message` is what a person asked, not a system prompt, a tool output or the agent's reply. `final_answer` is the agent's last reply to that person, not an intermediate step or the user's text.
2. **Swap.** User and answer, input and output, parent and child span ids, start and end time (a negative latency) are not swapped.
3. **Unit.** Timestamps and durations are in the unit the mapping claims (s, ms, us, ns). Tokens are counts, not characters or dollars. Cost is USD, not cents.
4. **Identity.** `run_id` is unique per agent call and ties together all and only that call's records. `conversation_id` groups the same person's runs. A column that repeats across unrelated rows is not a run id. Two requests merged into one run, or one run split in two, is an identity failure.
5. **Tool.** Tool names are real tool names, not model names, span kinds or HTTP routes. Each success or error flag matches the raw status and the result text. Arguments and results belong to that call.
6. **Leak.** No vendor score, evaluator label, thumbs or grader-like field is carried into anything the graders read: user message, answer, tool arguments and results, `extra`.
7. **Mixed source.** One file may hold several record types (agent runs, http, db, health checks, other apps, test traffic). Only agent runs belong in the eval set.
8. **No answer.** `ends_without_answer` is true only when the raw rows show the run really ended without a reply (an error, a cut-off, a timeout), not because this export does not carry answer text. If the answer text was simply not captured, those runs are unusable, not failures.

Also check the whole file with code, which is cheap: record kinds against runs built, run ids unique, no negative latency, timestamps in a plausible range, no empty `user_message`, the share of runs with tool calls and with no answer against the raw share.

A mismatch that repeats (say 20 percent of the sampled runs have user and answer swapped) is a mapping fault. Fix `mapping.json`, normalize again, and check a FRESH sample, not the runs you just fixed. Two repair rounds at most.

## 3. Classify, do not guess

Every run ends in exactly one class.

| Class | Meaning | Goes to graders |
|---|---|---|
| `verified` | passed all checks on its own evidence | yes |
| `unusable` | something that makes it a run cannot be trusted | **no** |

Unusable:
- the user's ask is missing or empty
- the record is not an agent run (health check, probe, test traffic, http or db row)
- roles are mislabeled or swapped for that run and cannot be repaired
- the run boundary is ambiguous (two requests merged, one run split, an id that repeats across unrelated rows)
- the answer is missing only because capture was off
- a duplicate of another run

Not unusable: a run that really failed, was cut off or got no answer (that is the data); a slow, expensive or odd run; a run with a recovered tool failure.

A column you cannot verify is unmapped, and the dimension it fed is left out; the runs stay usable if their ask, answer, tools and identity are verified. Mark a run unusable when who asked what, what was answered, where the run starts and ends, or whether it is an agent run at all is in doubt. Unusable runs are listed with a reason in the ingest sign-off, are never graded or counted, and never appear in the CIG or the scorecard. Do not pad the sample with unverified runs to reach the 300 cap: a smaller verified sample is the result.

## 4. Sign-off

Write the record, then record it. The shape is the same for every stage (`stage-signoffs.md`); ingest adds `unusable`. A worked example (the names and values are illustrations, not a template):

```json
{
  "stage": "ingest",
  "model": "claude-sonnet-5-5",
  "verdict": "pass",
  "checked": 40,
  "mismatches": 0,
  "notes": "40 runs from all 3 agents compared with their raw rows: roles, ids, units and tools agree. Health-check rows are excluded by the mapping. 2 runs are unusable.",
  "evidence": [
    {"id": "run_0192", "finding": "user_message equals raw input.text 'Where is invoice 4417?'; final_answer equals the last assistant row; latency 6.3 s equals end minus start in ms divided by 1000"},
    {"id": "run_0841", "finding": "3 tool calls (get_invoice, get_invoice, send_reply) match 3 raw tool rows; the second get_invoice has HTTP 503 in its raw row and is marked failed"},
    {"id": "run_1302", "finding": "ends_without_answer is true: the raw trace stops after a timeout error and has no assistant row"},
    {"id": "run_0377", "finding": "raw ask is an empty string; nothing to grade, listed unusable"},
    {"id": "run_2210", "finding": "input_tokens 1840 equals the raw usage field, not the character count (7360)"}
  ],
  "unusable": [
    {"run_id": "run_0377", "reason": "ask is empty in the raw row"},
    {"run_id": "run_1020", "reason": "two requests merged under one call id; run boundary ambiguous"}
  ]
}
```

Each finding says what you saw in that item, with values. Never "looks fine". `notes` is one or two plain sentences: what was compared and the result, not the history of what you fixed.

Record it last. Any later change to `mapping.json` or `normalized/runs.jsonl` makes the sign-off stale.

```
python3 <plugin>/scripts/plite.py check record --out <OUT> --file <OUT>/checks/ingest.draft.json
```

It prints the file it wrote, or exactly why it refused. It enforces: the model meets the floor; `checked` is at least 30 (all runs if fewer); at least five evidence entries with different findings; every `unusable` run id exists in `normalized/runs.jsonl`; and for a pass, 0 mismatches. A run you list as unusable is not a mismatch. A kept run that disagrees with its raw rows is.

On a fail, record `"verdict": "fail"` honestly, fix the cause (section 2), and verify again; a later pass replaces it. When almost nothing is usable after two repair rounds, stop and tell the user in one plain line which field is missing and which export would fix it.

## 5. Fallback recipe (no Workflow tool)

`workflow/mapping.js` holds the exact prompts and can be read as plain text. By hand, with the Agent tool (`model: "sonnet"`; on a GPT host, GPT 5.6 Terra; never Haiku), in the background:

1. One rescue agent: "Read ingest-checks.md section 1 and follow it. Raw export: <paths>. Work folder: <OUT>. Write `<OUT>/mapping.json`, run normalize, check ten runs, reply with rows_reviewed and notes."
2. Four slice agents in parallel plus one census agent: "Read ingest-checks.md section 2. Your runs are the lines i of `normalized/runs.jsonl` with i mod 4 = k; check 10 of them, spread evenly. Reply with checked, mismatches, evidence, unusable and your own model id." The census agent instead counts every raw record kind with code and confirms the runs come only from agent-run records.
3. If any slice reports a mismatch that is not unusable, one agent repairs the mapping (section 2), and steps 2 repeats with a fresh sample.
4. One consolidator agent writes the ingest record (section 4) with its own model id and runs `check record`.
