---
description: Do the grading steps: grade runs, then verify the merge (Sonnet 5.5 or GPT 5.6 Terra agents)
---

Use the procept-lite skill, steps `grade` and `merge_verify`. Run `plite.py run` until it prints `NEXT: grade`, state the cost estimate in one line and proceed: `workflow/grade.js` with `grading/workflow_args.json` (fallback `references/grading-brief.md`), then `plite.py run`, then `verify.js` with stage merge. Model floor Sonnet 5.5 or GPT 5.6 Terra, never Haiku. Output dir: `./procept-out` unless the user names one. Arguments from the user: $ARGUMENTS
