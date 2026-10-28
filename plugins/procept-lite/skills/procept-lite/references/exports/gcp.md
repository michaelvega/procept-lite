# Google Cloud (Vertex AI Agent Engine / ADK / Cloud Trace / Cloud Logging)

All commands and flags below are unverified: check vendor docs.

## Turn content capture on
- ADK / Agent Engine: enable tracing, and enable message-content capture via the OpenTelemetry GenAI setting `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` (value form varies by version; some versions use `true`, others `span_and_event`). Without it, spans have token counts and tool names but no text.
- Content may be written to Cloud Logging as log entries linked by trace id rather than on the span itself: export both.

## Export
Cloud Logging to a file:
```
gcloud logging read 'resource.type="aiplatform.googleapis.com/ReasoningEngine"' \
  --freshness=7d --limit=100000 --format=json > logs.json
```
(Resource type and filter vary; check what your agent writes in Logs Explorer first.)

For large volumes: Logs Explorer -> Actions -> create a sink to BigQuery or Cloud Storage, then in BigQuery:
```
bq query --format=json --max_rows=1000000 --nouse_legacy_sql \
 'SELECT * FROM `project.dataset.table` WHERE timestamp > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)' > logs.json
```
Cloud Trace spans: use the Trace API / `gcloud trace` (availability varies) or export traces through an OTLP collector to a file (see opentelemetry-generic.md).

## Normalize
    python3 plugins/procept-lite/scripts/plite.py normalize --src logs.json --format auto --out procept-out
If it is Cloud Logging JSON (`jsonPayload`, `trace`, `timestamp`), add a `--map mapping.json` pointing run id to `trace`, timestamps to `timestamp`, and text to the payload fields.

## Pitfalls
- `gcloud logging read` has default limits (use `--limit`) and Logs Explorer UI downloads cap at a small number of rows.
- `trace` is `projects/<p>/traces/<id>`: strip the prefix so spans and logs join.
- Log retention (30 days default for _Default bucket, unverified) may already have deleted old data.
- Trace sampling in Cloud Trace is often below 100%.
