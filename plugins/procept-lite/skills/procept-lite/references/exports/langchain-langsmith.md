# LangChain / LangSmith

## Make sure content is captured
- Do not set hide-inputs / hide-outputs options. Hiding is commonly done with the `LANGSMITH_HIDE_INPUTS=true` / `LANGSMITH_HIDE_OUTPUTS=true` env vars (unverified: check vendor docs). If they are set, re-run with them off, or traces will contain no user message or answer.
- Tool args and results appear as the inputs/outputs of child runs with `run_type="tool"`.
- To group runs into conversations, pass a `thread_id` (or `session_id` / `conversation_id`) in run metadata (unverified: check vendor docs).

## Export with the SDK (verified: https://docs.langchain.com/langsmith/export-traces)
```python
import json
from langsmith import Client
client = Client()   # reads LANGSMITH_API_KEY from env; never paste the key into files
with open("traces.jsonl", "w") as f:
    for r in client.list_runs(project_name="<your_project>", is_root=False):
        f.write(r.json() + "\n")   # r.json() on pydantic v1 models; use r.model_dump_json() on v2
```
- Export ALL runs of each trace (root plus children), not just roots: the tool calls live in the children. Use `trace_id`s of the roots you want, or filter by `start_time`.
- `is_root=True`, `run_type="tool"`, `error=False`, `start_time=...` and `select=[...]` exist as parameters (verified, same URL). If you use `select`, include `id, trace_id, parent_run_id, name, run_type, start_time, end_time, inputs, outputs, error, extra, total_tokens, prompt_tokens, completion_tokens, total_cost, session_id`.
- For large volumes the docs recommend Bulk Data Export (verified, same URL); setup details: unverified, check vendor docs.

## UI path
Project -> Runs table -> select runs -> export/download (exact menu labels: unverified, check vendor docs).

## Format and normalize
Output is one JSON run per line (or a JSON array) with `inputs`, `outputs`, `parent_run_id`, `trace_id`.

    python3 plugins/procept-lite/scripts/plite.py normalize --src traces.jsonl --format langsmith --out procept-out

## Pitfalls
- Rate limits on the runs query endpoint (verified, same URL); the examples use a small thread pool. Export in time windows.
- Exporting only root runs loses tool calls (red on tool-call check).
- Hidden or truncated inputs/outputs; very long values may be cut by the platform (unverified).
- Sampling: if tracing sample rate is below 1.0 you only see a subset; say so in the report.
