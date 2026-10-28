# AWS (Bedrock Agents / AgentCore / X-Ray / CloudWatch)

All commands and settings below are unverified: check vendor docs.

## Turn content capture on
- Bedrock Agents: enable "trace" on invocation (`enableTrace=true` in `InvokeAgent`) to get reasoning and tool (action group) steps in the response; store them yourself.
- Bedrock model invocation logging: Bedrock console -> Settings -> Model invocation logging, enable text prompts/responses, destination CloudWatch Logs and/or S3. This is off by default and only covers calls made after you enable it.
- AgentCore / OTel-based agents: use ADOT or an OpenTelemetry exporter with content capture on (see opentelemetry-generic.md); spans land in CloudWatch (`aws/spans` log group) (unverified).

## Export
CloudWatch Logs Insights, then export results (console: Export results -> CSV/JSON), or CLI:
```
aws logs start-query --log-group-name <group> --start-time <epoch> --end-time <epoch> \
  --query-string 'fields @timestamp, @message | sort @timestamp asc | limit 10000'
aws logs get-query-results --query-id <id> > logs.json
```
Large volumes: `aws logs create-export-task` to S3, or read the S3 invocation-log objects directly (`aws s3 sync`). X-Ray: `aws xray batch-get-traces --trace-ids ...` (5 ids per call limit unverified; segments carry little message text).

## Normalize
    python3 plugins/procept-lite/scripts/plite.py normalize --src logs.json --format auto --out procept-out
Use `--format otel` for span records; invocation logs (one JSON per model call, no tool structure) need `--map mapping.json` and probably the "derive" steps in data-gaps.md.

## Pitfalls
- Logs Insights returns at most 10,000 rows per query (unverified): split by time window.
- Model invocation logs have no run or conversation id: join on `requestId`/session id from your application logs (data-gaps.md#run-id).
- Large payloads are stored in S3 and only referenced in the log entry.
- Bedrock Agents traces are not stored unless you capture them.
