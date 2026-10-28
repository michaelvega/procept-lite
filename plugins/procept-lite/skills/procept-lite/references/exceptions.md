# Exceptions and degradation

Principle: whatever it takes to get the CIG. Degrade, never block. Say plainly which dimensions are missing. Never ask the user to fix data by hand; fix it yourself. Every repair, drop and assumption is counted in `<OUT>/cleaning_report.md`; read it before presenting results.

Exit codes: 0 = success (possibly with warnings); nonzero only for empty or unreadable input, with a one-line message. Never a traceback. Secrets and emails in data are never echoed.

## Escalation ladder
1. Pipeline auto-cleans and auto-maps (`mapping.auto.json`).
2. If it errors naming fields, or `cleaning_report.md` shows most runs "thin" or zero tools where tools clearly exist: read a 20-row sample of the raw file (head, or `python3 -I` one-liner; do not print secrets), write `<OUT>/mapping.json` following `templates/mapping.example.json`, rerun with `--map`.
3. Second attempt allowed if the first changed things. After two failed attempts, ask the user one question naming the exact missing fields, or for a different export.

## Situations
| Situation | Pipeline does automatically | If not enough, the skill does |
|---|---|---|
| No timestamps or durations | Drops latency; keeps cost, errors, score; notes it in cleaning_report | Check for time under another name (`created`, `ts`, `time_unix_nano`); map it |
| Timestamps in mixed units or formats | Detects epoch s/ms/us/ns by magnitude; parses ISO with or without tz (assume UTC, logged) | Set `timestamp_format` in mapping |
| No tokens or cost | Estimates tokens as chars/4, flagged "estimated"; if no text either, drops cost | Offer `--prices prices.json` only if they want dollars; never required |
| No explicit error field | Derives errors from result text and status: error, exception, traceback, 4xx/5xx, timeout, `"status":"error"`, `isError`, `ok:false` | Map `error`/`status` + `status_error_values` |
| No tool calls at all | Scorecard from user message and answer; CIG has only an agent node | Check whether tools hide inside model messages (`tool_calls`, `function_call`, `tool_use`) and map |
| No run id | Synthesizes stable hash id | None |
| No conversation id | Uses session/thread/trace id, else user + time gap, else one conversation | Map the session column |
| Missing user message or final answer | Uses best available text, marks run "thin", grades accordingly | Map the correct text fields; if over ~30% thin, say so in the report |
| Wrong delimiter, bad CSV quotes | Sniffs comma, tab, semicolon, pipe; repairs quotes | Set delimiter in mapping |
| BOM, UTF-16, gzip, zip | Decodes and unpacks | None |
| NDJSON vs array vs concatenated JSON; truncated last line | Detects shape; drops the broken tail, counts it | None |
| Double-encoded JSON (JSON in a string) | Decodes repeatedly until structured | None |
| OTLP attribute lists `[{key,value:{stringValue}}]` | Flattens to plain keys | None |
| Duplicate, orphan, out-of-order spans | Dedupes by span id; orphans attach to the trace root; sorts by time | None |
| NaN, nulls, empty strings | Treated as missing | None |
| Unknown schema | Heuristic mapping by column name and value shape, written to `mapping.auto.json` | Model writes `mapping.json` (ladder above) |
| Huge files or fields | Streams; truncates oversize fields safely (counted) | Use `--sample` cap; no user action |
| Mixed folder (logs, images, readmes) | Ignores unreadable or irrelevant files, lists them | None |
| Partial runs (cut off, no final answer) | Kept, marked "thin" | None |
| Secrets, emails in data | Never echoed in messages; graders told not to repeat them | Do not quote them in your own summary |
| Too few runs (under ~30) | Builds everything; report gives examples, no percentages | State it |

## Platform quirks
- **LangSmith**: runs export as flat rows with `inputs`/`outputs` as JSON strings (double-encoded), `parent_run_id`, `trace_id`, `run_type` (llm/tool/chain), `total_tokens`, `start_time`/`end_time`. Feedback and evaluator scores are present: do not pass them to graders. Missing `session_id` means grouping by trace.
- **Pydantic Logfire**: `records` table export; attributes live in `attributes` as JSON string, GenAI data in `gen_ai.*` or `events`/`all_messages`; durations in `duration` seconds, `start_timestamp` ISO. Many non-agent spans (http, db): treated as noise.
- **GCP**: Cloud Logging JSON (`jsonPayload`, `textPayload`, `timestamp` RFC3339, `severity`) or Cloud Trace spans (`startTime`/`endTime`, `labels`). Errors often only in `severity >= ERROR`; used as the error signal. Content may be absent: expect thin runs.
- **AWS**: X-Ray (`Segments` with `Document` as a string, `start_time` float epoch seconds), CloudWatch Logs (`@timestamp`, `@message` holding JSON), Bedrock invocation logs (`input.inputBodyJson`, `output.outputBodyJson`, token counts in `inputTokenCount`). Unwrap the string layers.
- **Azure**: Application Insights export, columns `timestamp`, `operation_Id`, `operation_ParentId`, `id`, `itemType`, `customDimensions` and `Properties` as JSON strings, `duration` in ms, `success` boolean (false = error). `operation_Id` is the trace id.
- **Generic OTel**: OTLP JSON `resourceSpans` > `scopeSpans` > `spans`, nanosecond string timestamps, attribute lists, `status.code` 2 = error. GenAI semconv `gen_ai.*`; older `llm.*`/`traceloop.*` names also seen.
- **Other frameworks** (CrewAI, AutoGen, OpenAI Agents, hand-rolled JSON): usually flat or nested messages; rely on auto-mapping, then model-written mapping.

## What to tell the user
Short, plain: which dimensions are present, which were dropped or estimated, count of repaired/skipped items, count of thin runs. Example: "Latency is missing (no timestamps), so the CIG covers cost, errors and score. 4 files were skipped as unreadable; 12 runs are thin." No blame, no homework.
