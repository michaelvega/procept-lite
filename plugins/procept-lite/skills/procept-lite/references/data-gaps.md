# What to do when data is missing

Order of preference: (1) enable capture and re-export, (2) join from another table, (3) derive heuristically (label it as derived), (4) stop. Snippets are vendor-neutral Python on a list of dicts `rows` (one per span/event); adapt field names via `--map`.

## run-id
R1. Need one id per agent attempt. Use `gen_ai.agent.call.id`, else root run id/trace id.
```python
def run_id(r): return r.get("agent_call_id") or r.get("trace_id") or r.get("request_id")
```
Derive: if only a session id and timestamps exist, start a new run at each user message (`session + turn index`). Mark `extra.run_id_derived=true`. Stop if neither exists and there is no user-message boundary.

## conversation-id
R2. Optional. Join from app logs (session/thread id) on trace id. Otherwise conversation = run id; say so in the report.

## timestamps
R3. Need start and end per span, or at least event times. Without end times, latency = last event minus first event (derived). If no timestamps at all, latency is RED: skip that dimension, never invent it.
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
Stop: if more than about 30% of runs lack the user message or the answer, grading is not meaningful. Say "not enough data".

## tool-calls
R6. Tool calls should be child spans or message parts named by tool. If only model spans exist, parse the model output for tool-call parts:
```python
calls = [p for m in msgs for p in m.get("parts", []) if p.get("type") in ("tool_call", "function_call")]
```
If the framework never traces tools, add tracing around tool functions (a decorator that logs name, args, result, error, duration). Without tool data you can still grade and score, but the network (CIG) has no nodes: stop at the scorecard and say so.

## tool-args-results
R7. Enable content capture for tools (`gen_ai.tool.call.arguments/result` or the vendor equivalent). Join results from tool-response messages by call id:
```python
results = {m["tool_call_id"]: m["content"] for m in msgs if m.get("role") == "tool"}
```
Without args/results the grader sees only tool names (lower confidence; note it).

## tool-status
R8. Need success/failure. Use span status/error fields; else derive from result text (`error`, `exception`, `timeout`, HTTP 4xx/5xx, empty result). Heuristic: flag as derived and sample 20 to check.
```python
FAIL = re.compile(r"\b(error|exception|traceback|timed? ?out|failed|40\d|50\d)\b", re.I)
```

## tokens
R9. Token counts per model call (`gen_ai.usage.*`). If missing, estimate chars/4 and mark as estimated. Dollar cost needs `--prices prices.json` (per-model input/output price per million tokens); otherwise report tokens only.

## model
R10. Model name per call (`gen_ai.request.model`). If missing, cost cannot use a per-model price: use one blended price and say so.

## volume
R11. Rule of thumb: at least 30 runs for any numbers, 200+ for persona and per-tool statistics, and tool nodes with fewer than 5 calls are noise. Fewer than 30 runs: report examples only, no percentages. Check sampling: if only errors or only sampled traces were exported, say the split is biased.

## when to stop and say "not enough data"
- No run boundary (R1) or no user message/answer text (R4, R5) in most runs.
- No way to tell the content of runs because capture is off and cannot be re-enabled.
- Fewer than 30 runs.
Say which requirement failed, what the user can switch on, and offer to re-check on a fresh export.
