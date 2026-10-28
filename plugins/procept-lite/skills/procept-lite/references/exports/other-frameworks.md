# Other frameworks and sources

Settings below are unverified: check vendor docs. Goal for each: one file with one record per step, with run/trace id, timestamps, user text, tool name/args/results, final answer.

- **OpenAI Agents SDK**: tracing is on by default and goes to the vendor dashboard; add a custom trace processor that writes spans to JSONL. Sensitive data capture is controlled by a run-config flag (unverified). Then `--format flat` or `auto`.
- **CrewAI / AutoGen / LlamaIndex**: use their OpenTelemetry or OpenInference/OpenLLMetry instrumentation, then follow opentelemetry-generic.md.
- **Langfuse / Arize Phoenix / Braintrust / Helicone / Datadog LLM Observability**: use the product's export (UI download or API) of traces with observations/spans included; keep input/output fields. Try `--format auto`; if it fails use `--map`.
- **Your own app logs**: log one JSON line per event with these keys: `run_id, conversation_id, ts, type (user|tool|llm|final), name, args, result, error, input_tokens, output_tokens, model`. This is the `flat` format.

## flat format
    python3 plugins/procept-lite/scripts/plite.py normalize --src events.jsonl --format flat --map mapping.json --out procept-out
`mapping.json` maps our field names to yours, e.g. `{"run_id": "request.id", "user_message": "msg.text"}`.

## Pitfalls
- Exports that include only LLM spans and no tool spans: see data-gaps.md#tool-calls.
- CSV files that flatten nested JSON lose structure: prefer JSON/JSONL.
