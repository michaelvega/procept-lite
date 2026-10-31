export const meta = {
  name: 'procept-lite-grade',
  description: 'Grade every run good/ambiguous/bad (+1/0/-1) with Sonnet 5.5 readers, plus a blind audit re-score of a sample',
  whenToUse: 'plite.py run printed NEXT: grade',
  phases: [{ title: 'Score', detail: 'one reader per shard' }, { title: 'Audit', detail: 'blind re-score batches' }],
}
// ARGS: the parsed contents of <out>/grading/workflow_args.json (written by `plite.py grade-prepare`), as a JSON object:
//   { "brief": "<abs>/grading/brief.md",
//     "shards": [ { "shard": "shard_01", "n": 20, "file": "<abs>/grading/shards/shard_01.jsonl", "csv": "<abs>/grading/scores/shard_01.csv" } ],
//     "audits": [ { "shard": "audit_01", "n": 20, "file": "<abs>/grading/audit/audit_01.jsonl", "csv": "<abs>/grading/audit/audit_01.csv" } ] }
// To re-run only some shards (after `plite.py grade-merge`), pass the same object with the shards and audits arrays filtered:
// `plite.py grade-merge --resume` writes <out>/grading/resume_args.json in exactly that shape.
//
// WHAT IT DOES: one reader per shard and one per audit batch, all in parallel. Each reads the brief, grades its file blind (no
// vendor, judge or user-satisfaction scores) and writes one CSV. Model floor for every judgement step: Sonnet 5.5 (model 'sonnet'
// below) or GPT 5.6 Terra, or better; never Haiku. The scripts and the brief are starters: edit <out>/grading/brief.md for this
// dataset before running if the product needs sharper rubric wording (keep the CSV header and the persona rules).
// AFTER: `python3 <plugin>/scripts/plite.py grade-merge --out <out>`, then run `plite.py run` again (the next step is merge_verify).
// The CSV files are the result; grade-merge validates them. The one log line at the end names any reader that failed.
//
// FALLBACK (host without the Workflow tool): follow "Fallback recipe" at the end of references/grading-brief.md. In short: one
// background Agent per entry of shards[] and audits[] (model "sonnet"; a GPT 5.6 Terra host runs the same prompt with that
// model), each told: "Read <brief> first and follow it exactly. Your file: <file> (<n> runs). Write your CSV to <csv> with one
// row per run. Return the counts." Then run grade-merge and re-run what it lists.
if (!args || !args.brief || !Array.isArray(args.shards)) {
  throw new Error('args must be the contents of <out>/grading/workflow_args.json (brief, shards, audits)')
}
const OUT = { type: 'object', properties: { file: { type: 'string' }, rows: { type: 'number' }, plus: { type: 'number' }, zero: { type: 'number' }, minus: { type: 'number' } }, required: ['file', 'rows', 'plus', 'zero', 'minus'] }
const prompt = (s) => `Read ${args.brief} first and follow it exactly.\nYour file: ${s.file} (${s.n} runs).\nWrite your CSV to ${s.csv} with one row per run. Return the structured counts.`
const shards = args.shards || [], audits = args.audits || []
const jobs = [
  ...shards.map(s => () => agent(prompt(s), { label: s.shard, phase: 'Score', model: 'sonnet', schema: OUT })),
  ...audits.map(s => () => agent(prompt(s), { label: s.shard, phase: 'Audit', model: 'sonnet', schema: OUT })),
]
const res = await parallel(jobs)
const names = [...shards, ...audits].map(s => s.shard)
const failed = names.filter((_, i) => !res[i])
log(`graded: ${res.filter(Boolean).length}/${res.length} readers returned; failed: ${failed.join(', ') || 'none'}. Next: plite.py grade-merge`)
