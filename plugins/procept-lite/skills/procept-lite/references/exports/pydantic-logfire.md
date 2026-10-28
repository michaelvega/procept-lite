# Pydantic Logfire (Pydantic AI and other OTel apps)

## Instrument (verified: https://pydantic.dev/docs/logfire/integrations/llms/pydanticai/)
```python
import logfire
logfire.configure()              # must run before the next line
logfire.instrument_pydantic_ai() # call exactly once
```
## Make sure content is captured
- Whether message content and tool args/results are recorded is controlled by an instrumentation option (commonly `include_content`) (unverified: check vendor docs). If your export has spans but no message text, turn it on and re-run a sample.
- Conversation grouping: pass a conversation/session id as an attribute (`gen_ai.conversation.id` where supported) (unverified: check vendor docs).

## Export
- UI: Explore (SQL) page -> run a query -> download results as CSV/JSON (exact buttons: unverified, check vendor docs).
- SQL against the `records` table (table and column names: unverified, check vendor docs):
```sql
SELECT start_timestamp, end_timestamp, trace_id, span_id, parent_span_id,
       span_name, message, attributes, is_exception, exception_message
FROM records
WHERE start_timestamp > now() - interval '7 days'
ORDER BY start_timestamp
```
- Keep the `attributes` column: it holds the `gen_ai.*` fields. Programmatic read API with a read token exists (unverified: check vendor docs; the Logfire query-API docs URL I tried returned 404).

## Format and normalize
CSV or JSON rows with `attributes` as a JSON string/object.

    python3 plugins/procept-lite/scripts/plite.py normalize --src logfire.csv --format otel --out procept-out

If columns have custom names, add `--map mapping.json` (see data-gaps.md).

## Pitfalls
- Row limits on UI/API downloads (unverified): export in day-sized windows and count rows.
- Sampling configured in `logfire.configure` drops traces; tail data is missing.
- Attributes truncated or scrubbed: Logfire scrubs values that look like secrets, which may replace text with `[Scrubbed due to ...]`. Check for it in message text (unverified).
