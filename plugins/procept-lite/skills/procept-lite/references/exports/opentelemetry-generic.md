# Any OpenTelemetry source

The GenAI semantic conventions now live at https://github.com/open-telemetry/semantic-conventions-genai (verified: the old docs page says it moved; I could not read the attribute list there, so names below are from memory).

## Attributes we look for (unverified: check the repo above)
- Run: `gen_ai.agent.call.id` (else trace id). Agent: `gen_ai.agent.name`.
- Conversation: `gen_ai.conversation.id` (fallbacks: `session.id`, `thread_id`).
- Model/tokens: `gen_ai.request.model`, `gen_ai.response.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`.
- Tools: `gen_ai.tool.name`, `gen_ai.tool.call.arguments`, `gen_ai.tool.call.result` (older: `gen_ai.tool.call.id`).
- Messages: `gen_ai.input.messages`, `gen_ai.output.messages` (older: span events `gen_ai.user.message`, `gen_ai.choice`, or `gen_ai.prompt.N.*` attributes).
- Errors: span status `ERROR`, `error.type`, exception events.

## Turn content capture on
Message content is OFF by default in most instrumentations. The usual switch is the env var `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true` (unverified: check vendor docs); some libraries have their own flag (e.g. OpenLLMetry `TRACELOOP_TRACE_CONTENT=true`, OpenInference hide-* options) (unverified). Re-run a sample after changing it.

## Get spans out as files
- OTLP file exporter / Collector `file` exporter writes JSON lines (unverified: check collector docs). Config sketch:
```yaml
exporters:
  file:
    path: ./spans.jsonl
service:
  pipelines:
    traces: {receivers: [otlp], exporters: [file]}
```
- Or export from your backend (Jaeger, Tempo, Honeycomb, Datadog, etc.) as JSON/CSV with one row per span, keeping attributes and parent ids.

## Normalize
    python3 plugins/procept-lite/scripts/plite.py normalize --src spans.jsonl --format otel --out procept-out
Use `--format auto` if unsure, `--map mapping.json` for custom attribute names.

## Pitfalls
- Head sampling drops traces; tail sampling may keep only errors, which biases the picture. State this in the report.
- Attribute value size limits truncate long messages (default limits: unverified).
- Spans split across files: pass the whole directory to `--src` so children find their parents.
