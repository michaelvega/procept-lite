# What to do when data is missing

Order of preference: (1) enable capture and re-export, (2) join from another table, (3) derive it, and have the ingest verifier check the derivation on real rows (a derivation that cannot be verified means those runs are unusable), (4) stop. Snippets are vendor-neutral Python on a list of dicts `rows` (one per span/event); the field names you adapt them to go into `mapping.json`.

## run-id
R1. Need one id per agent attempt. Use `gen_ai.agent.call.id`, else root run id/trace id.
```python
def run_id(r): return r.get("agent_call_id") or r.get("trace_id") or r.get("request_id")
```
Derive: if only a session id and timestamps exist, start a new run at each user message (`session + turn index`). Mark `extra.run_id_derived=true`. Stop if neither exists and there is no user-message boundary.

## conversation-id
R2. Optional. Join from app logs (session/thread id) on trace id. Otherwise conversation = run id, and the next-message reaction signal is not available.

## timestamps
R3. Need start and end per span, or at least event times. Without end times, latency = last event minus first event (derived). If no timestamps at all, latency is RED: leave that dimension out, never invent it.
```python
from datetime import datetime
lat = (max(ts) - min(ts)).total_seconds()
```

## message-content
R4/R5. Need the user message and the final answer as text. Enable content capture (see the vendor export guide; OTel: `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT`), re-run a sample, re-export. Or join from your chat database on conversation id + time. Derive the answer as the last assistant/model output in the run; the user message as the first input. Redact first:
```python
import re
def redact(s):
    s = re.sub(r"[\w.+-]+@[\w-]+\.[\w.-]+", "[email]", s)
    return re.sub(r"\b(sk|key|tok)[-_][A-Za-z0-9_-]{16,}\b", "[secret]", s)
```
A run with no user message is unusable. A run with no answer text is a real "no answer" only if the raw rows show it ended without one; if capture was off, it is unusable. Stop: if most runs lack the user message or the answer, there is nothing correct to grade. Say in one plain line which field is missing and which setting to switch on.

## tool-calls
R6. Tool calls should be child spans or message parts named by tool. If only model spans exist, parse the model output for tool-call parts:
```python
calls = [p for m in msgs for p in m.get("parts", []) if p.get("type") in ("tool_call", "function_call")]
```
If the framework never traces tools, add tracing around tool functions (a decorator that logs name, args, result, error, duration). Without tool data you can still grade and score, but the network has only an agent node; say so in one plain line.

## tool-args-results
R7. Enable content capture for tools (`gen_ai.tool.call.arguments/result` or the vendor equivalent). Join results from tool-response messages by call id:
```python
results = {m["tool_call_id"]: m["content"] for m in msgs if m.get("role") == "tool"}
```
Without args/results the grader sees only tool names (lower confidence).

## tool-status
R8. Need success/failure. Use span status/error fields; else derive from result text (`error`, `exception`, `timeout`, HTTP 4xx/5xx, empty result). A derived flag is checked: the ingest verifier compares at least 20 derived flags with the raw result text.
```python
FAIL = re.compile(r"\b(error|exception|traceback|timed? ?out|failed|40\d|50\d)\b", re.I)
```

## tokens
R9. Token counts per model call (`gen_ai.usage.*`). If missing, estimate chars/4 and mark as estimated. Dollar cost needs `--prices prices.json` (per-model input/output price per million tokens); otherwise cost is shown in tokens.

## model
R10. Model name per call (`gen_ai.request.model`). If missing, cost cannot use a per-model price: use one blended price.

## volume
R11. Rule of thumb: at least 30 usable runs for any numbers, 200+ for per-goal and per-tool statistics, and tool nodes with fewer than 5 calls are noise. With fewer than 30 usable runs every run is checked and the numbers are wide; say so in one plain line. Check sampling: if only errors or only sampled traces were exported, the split is biased; say so in one plain line.

## flat-tables
A table (CSV or JSONL) with one row per event or span and column names we do not know. There is no format flag for it: the mapping rescue maps it (`ingest-checks.md`, section 1). `templates/mapping.example.json` shows the shape. Keys are our canonical fields, values are YOUR column names. Every key is optional; delete the ones you do not have.
- run and conversation: `run_id`, `conversation_id`; or `trace_id`, `span_id`, `parent_id` so runs can be built
- time: `timestamp` with `timestamp_format`, `end_timestamp`, or `duration_s` / `duration_ms`
- text: `role` with `content` (one message per row), or `kind` with `kind_values` saying which values mean tool, llm, agent, user and assistant rows
- tools: `tool_name`, `tool_args`, `tool_result`, `error`, `status` with `status_error_values`
- usage: `input_tokens`, `output_tokens`, `model`, `cost_usd`; also `agent`, `environment`

Decide each column from its VALUES, not its name, and exclude rows that are not agent runs. A column you cannot verify is left out.

## when to stop and say "not enough data"
- No run boundary (R1) or no user message/answer text (R4, R5) in most runs.
- No way to tell the content of runs because capture is off and cannot be re-enabled.
- Nothing usable is left after verification (no run has a verified ask and answer).
A small sample is not a reason to stop (R11). Say in one plain line which requirement failed and what the user can switch on, and offer to re-check on a fresh export.
