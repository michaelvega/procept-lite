# Exceptions and degradation

Principle: whatever the export looks like, get to a result that is correct, even if it is smaller. The pipeline cleans what it can, a model decides what the data means, and anything that cannot be verified is classified unusable and kept out. Never ask the user to fix data by hand; fix it yourself. When something is unfixable, say so in one plain line.

`<OUT>/cleaning_report.md` is the pipeline's own log of what it cleaned and assumed. It is for you: read it to understand the data. It is internal, so never read it aloud, quote it or ship it.

Exit codes: 0 = success; non-zero for an empty or unreadable input, or when a gate refuses because a stage has no sign-off, always with a one-line message and never a traceback. Secrets and e-mail addresses in data are never echoed.

## Escalation ladder
1. `plite.py run` cleans the raw files and scaffolds `<OUT>/mapping_rescue/` (a masked sample, a heuristic draft, a preview). It never writes `mapping.json` and never maps for you.
2. A floor-model agent (Sonnet 5.5 or GPT 5.6 Terra, or better; never Haiku) ALWAYS does the mapping rescue: reads the raw rows and the draft, writes `<OUT>/mapping.json` with its `_rescue` block (`ingest-checks.md`, section 1). If you are a smaller model, delegate.
3. `run` normalizes with that mapping. A floor-model agent verifies at least 30 runs against the raw rows and signs off the `ingest` stage (`ingest-checks.md`, section 2).
4. A mismatch that repeats is a mapping fault: fix the mapping, normalize again, verify a fresh sample. Two repair rounds at most.
5. Still unverifiable: unmap the column (the dimension it fed is left out) or classify the affected runs unusable. If too little is left, say in one plain line which field is missing and which export would fix it.

## Situations
| Situation | What the pipeline does | What the agent does |
|---|---|---|
| No timestamps or durations | Leaves latency out; keeps cost, errors, score | Looks for time under another name (`created`, `ts`, `time_unix_nano`) and maps it |
| Timestamps in mixed units or formats | Detects epoch s/ms/us/ns by magnitude; parses ISO with or without a zone (UTC assumed) | Sets `timestamp_format` in the mapping and checks the unit on real rows |
| No tokens or cost | Estimates tokens as chars/4, flagged as estimated; with no text either, leaves cost out | Offers `--prices prices.json` only if the user wants dollars; never required |
| No explicit error field | Derives errors from result text and status: error, exception, traceback, 4xx/5xx, timeout, `"status":"error"`, `isError`, `ok:false` | Maps `error` or `status` with `status_error_values`; the verifier checks the derived flags on real rows |
| No tool calls at all | Scorecard from the ask and the answer; the CIG has only an agent node | Checks whether tools hide inside model messages (`tool_calls`, `function_call`, `tool_use`) and maps them |
| No run id | Synthesizes a stable hash id | The verifier checks that runs are not merged or split |
| No conversation id | Uses session, thread or trace id, else user plus time gap, else one conversation | Maps the session column |
| Empty ask | The run is unusable (listed in `normalized/unusable.csv`) | Maps the right text field if the ask exists elsewhere |
| No final answer | Marks the run as ending without an answer | The verifier confirms the raw rows show a real cut-off or error; if the export just does not carry answer text, those runs are unusable |
| Wrong delimiter, bad CSV quotes | Sniffs comma, tab, semicolon, pipe; repairs quotes | Sets the delimiter in the mapping |
| BOM, UTF-16, gzip, zip | Decodes and unpacks | None |
| NDJSON vs array vs concatenated JSON; truncated last line | Detects the shape; drops the broken tail | None |
| Double-encoded JSON (JSON in a string) | Decodes repeatedly until structured | None |
| OTLP attribute lists `[{key,value:{stringValue}}]` | Flattens to plain keys | None |
| Duplicate, orphan, out-of-order spans | Dedupes by span id; orphans attach to the trace root; sorts by time | None |
| NaN, nulls, empty strings | Treated as missing | None |
| Unknown schema | The scaffold writes a heuristic draft for the agent to read | The agent writes `mapping.json` (ladder above), or a small converter in `mapping_rescue/` for structures a mapping cannot express |
| Several record types in one file | Reports the kinds it saw | The mapping excludes non-agent records; any run built from one is unusable |
| Vendor scores or evaluator labels in the export | Not carried into normalized runs; graders never see them | The study reads them from the raw export by run id, as the thing under test |
| Huge files or fields | Streams; truncates oversize fields safely | Uses the `--sample` cap; no user action |
| Mixed folder (logs, images, readmes) | Ignores unreadable or irrelevant files | None |
| Too few runs (under about 30) | Builds everything | Checks every run, and says in one plain line that the sample is small |

## Platform quirks
- **LangSmith**: runs export as flat rows with `inputs`/`outputs` as JSON strings (double-encoded), `parent_run_id`, `trace_id`, `run_type` (llm/tool/chain), `total_tokens`, `start_time`/`end_time`. Feedback and evaluator scores are present: they never reach the graders. Missing `session_id` means grouping by trace.
- **Pydantic Logfire**: `records` table export; attributes live in `attributes` as a JSON string, GenAI data in `gen_ai.*` or `events`/`all_messages`; durations in `duration` seconds, `start_timestamp` ISO. Many non-agent spans (http, db) are noise.
- **GCP**: Cloud Logging JSON (`jsonPayload`, `textPayload`, `timestamp` RFC3339, `severity`) or Cloud Trace spans (`startTime`/`endTime`, `labels`). Errors often show only as `severity >= ERROR`, which serves as the error signal. Content may be absent, so many runs may be unusable.
- **AWS**: X-Ray (`Segments` with `Document` as a string, `start_time` float epoch seconds), CloudWatch Logs (`@timestamp`, `@message` holding JSON), Bedrock invocation logs (`input.inputBodyJson`, `output.outputBodyJson`, token counts in `inputTokenCount`). Unwrap the string layers.
- **Azure**: Application Insights export, columns `timestamp`, `operation_Id`, `operation_ParentId`, `id`, `itemType`, `customDimensions` and `Properties` as JSON strings, `duration` in ms, `success` boolean (false = error). `operation_Id` is the trace id.
- **Generic OTel**: OTLP JSON `resourceSpans` > `scopeSpans` > `spans`, nanosecond string timestamps, attribute lists, `status.code` 2 = error. GenAI semconv `gen_ai.*`; older `llm.*` and `traceloop.*` names also appear.
- **Other frameworks** (CrewAI, AutoGen, OpenAI Agents, hand-rolled JSON): usually flat or nested messages. The mapping rescue reads the structure from the real rows.

## What to tell the user
Nothing about repairs, exclusions or fixes. If a dimension is left out, or most runs are unusable, say so in one plain line, for example: "This export has no timestamps, so latency is not shown." No blame, no homework, no counts of what was cleaned.
