// procept-lite parallel grading (Workflow tool script). One Haiku reader per shard plus blind audit batches, all in parallel.
// Prepare first:  python3 plugins/procept-lite/scripts/plite.py grade-prepare --out DIR
// That writes DIR/grading/workflow_args.json, shaped like:
//   { "brief": "<abs>/grading/brief.md",
//     "shards": [ { "shard": "shard_01", "n": 20, "file": "<abs>/grading/shards/shard_01.jsonl", "csv": "<abs>/grading/scores/shard_01.csv" } ],
//     "audits": [ { "shard": "audit_01", "n": 20, "file": "<abs>/grading/audit/audit_01.jsonl", "csv": "<abs>/grading/audit/audit_01.csv" } ] }
// Run this script with args = the parsed contents of that JSON file.
// To re-run only some shards (after `grade-merge`), pass the same object with the shards/audits arrays filtered
// (`grade-merge --resume` writes grading/resume_args.json in exactly this shape).
// Afterwards: python3 plugins/procept-lite/scripts/plite.py grade-merge --out DIR
export const meta = {
  name: 'procept-lite-grade',
  description: 'Grade every run good/ambiguous/bad (+1/0/-1) with cheap readers, plus a blind audit re-score of a sample',
  phases: [{ title: 'Score', detail: 'one reader per shard' }, { title: 'Audit', detail: 'blind re-score batches' }],
}
const OUT = { type: 'object', properties: { file: { type: 'string' }, rows: { type: 'number' }, plus: { type: 'number' }, zero: { type: 'number' }, minus: { type: 'number' } }, required: ['file', 'rows', 'plus', 'zero', 'minus'] }
const prompt = (s) => `Read ${args.brief} first and follow it exactly.\nYour file: ${s.file} (${s.n} runs).\nWrite your CSV to ${s.csv} with one row per run. Return the structured counts.`
const shards = args.shards || [], audits = args.audits || []
const jobs = [
  ...shards.map(s => () => agent(prompt(s), { label: s.shard, phase: 'Score', model: 'haiku', schema: OUT })),
  ...audits.map(s => () => agent(prompt(s), { label: s.shard, phase: 'Audit', model: 'haiku', schema: OUT })),
]
const res = await parallel(jobs)
const names = [...shards, ...audits].map(s => s.shard)
const failed = names.filter((_, i) => !res[i])
log(`${res.filter(Boolean).length}/${res.length} readers returned; failed: ${failed.join(', ') || 'none'}`)
return { results: res.map((r, i) => r ? { shard: names[i], ...r } : { shard: names[i], failed: true }) }
