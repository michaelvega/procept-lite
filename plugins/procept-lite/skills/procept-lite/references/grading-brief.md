# Run outcome grading brief

Product: {{PRODUCT}}

THE UNIT IS THE RUN (one agent call, one `run_id`). Grade every run in your shard. Never aggregate to conversations.

## Input
Your shard is a JSONL file, one run per line. Fields: run_id, conversation_id, agent, environment, first_ts, user_message, tool_calls [{name,args,result,success,error,duration_s}] (args/results are truncated), final_answer, ends_without_answer, n_tool_failures, errors, input_tokens, output_tokens, plus context: earlier_user_messages (same conversation), next_run_id, next_user_message (the user's next message in the same conversation, or null).
Read the file in chunks with python (json lines), a few runs at a time. Some runs are large; do not dump a whole shard at once.
You are blind: you have no vendor, judge or user-satisfaction scores, and must not look for any. Do not read other shard or score files. Grade from the evidence in the run only.

## Rubric
+1 = the run delivered a correct, useful, complete answer or action for its ask (verify against the tool results).
-1 = the run failed the ask: crash, no answer, a refusal the ask did not deserve, empty/wrong/contradictory data, wrong action, hallucinated context.
 0 = ambiguous: partial, unverifiable, or just a clarifying question that was reasonable.

Reaction: use next_user_message as the user's reaction. A correction or a repeat of the ask is evidence of -1; "thanks" or moving on is evidence of +1; null means no reaction, so infer nothing from silence.
Check claims against the tool results where you can (numbers, names, counts). A failed tool call is not a failed run if the agent recovered and the final answer is right. A final answer that says it cannot do X, with a reason and a way forward, can be +1 if X really was not possible given the tools in the run.
Confidence is 0-1: how sure you are of the score.

## Persona and issue (two extra columns)
persona = a short buyer-goal label (2-4 words) naming what kind of user, with what goal, asked this (for example "analyst checking numbers", "operator running a task"; invent labels that fit THIS product's runs).
1. Before grading, skim about 20 runs of your shard and fix a list of 4-8 personas.
2. Then reuse exactly those labels for every run. Add a new label only if nothing fits. Same wording, same casing.
issue = a 2-6 word failure label (for example "tool timeout", "empty result", "wrong entity"). Leave it empty when the score is +1. Reuse similar wording across runs.

## Output
Write ONE CSV, UTF-8, header exactly:
{{CSV_HEADER}}
- one row for EVERY run in your file (count them; no skips, no extras, no duplicates), written with python's csv module so quoting is handled
- outcome_score: 1, 0 or -1
- confidence: number 0-1
- rationale: one sentence, plain words, says WHY (what in the tool results or reply)
- key_quote: verbatim, <=200 chars, from the user message, final answer or a tool result
- persona, issue: as above
- reaction_run_id: next_run_id if you used next_user_message, else empty
Then reply with the row count and the counts of +1 / 0 / -1.
Rules: no network, do not modify any source data, run python with `-I`. Ground every row in the run's own evidence. Labels are model judgements, not user satisfaction.

## Fallback recipe (no Workflow tool)
If the Workflow tool is not available, spawn the readers yourself with the Agent tool, in the background, several at once:
- one Agent per shard in `grading/workflow_args.json` (`shards[]`), with `model: "haiku"`
- prompt each: "Read <grading/brief.md> first and follow it exactly. Your file: <shard file> (<n> runs). Write your CSV to <csv path>. Reply with the counts."
- do the same for `audits[]` (audit batches are blind re-scores of a sample; the readers must not see the primary CSVs)
- when all return, run `python3 plugins/procept-lite/scripts/plite.py grade-merge --out <dir>`; re-run any shard it lists. `--resume` lists the shards still missing.
