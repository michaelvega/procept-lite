# Azure (Azure AI Foundry agents / Application Insights / Log Analytics)

All commands and settings below are unverified: check vendor docs.

## Turn content capture on
- Foundry / Azure AI Projects tracing: connect an Application Insights resource to the project, enable tracing, and set content recording on (commonly the env var `AZURE_TRACING_GEN_AI_CONTENT_RECORDING_ENABLED=true`, or the OTel GenAI content variable, see opentelemetry-generic.md). Off by default: without it you get no message text or tool arguments.
- Instrumenting with `azure-monitor-opentelemetry` sends spans to Application Insights.

## Export
Log Analytics / Application Insights, Logs blade, KQL (table names: `AppDependencies`, `AppTraces`, `AppRequests` in workspace-based resources):
```
AppDependencies
| where TimeGenerated > ago(7d)
| project TimeGenerated, OperationId, Id, ParentId, Name, DurationMs, Success, Properties
| order by TimeGenerated asc
```
Then Export -> CSV, or CLI:
```
az monitor log-analytics query -w <workspace-id> --analytics-query "<KQL>" -o json > logs.json
```
Large volumes: Log Analytics data export rule to a storage account, or the Query API with paging.

## Normalize
    python3 plugins/procept-lite/scripts/plite.py normalize --src logs.json --format auto --out procept-out
Mapping hints: `OperationId` is the trace id, `Id` the span id, `ParentId` the parent, `Properties`/`customDimensions` holds the `gen_ai.*` attributes. Pass `--map mapping.json` if columns are renamed.

## Pitfalls
- Portal result limit around 30,000 rows and query API limits (unverified): split by time window.
- Application Insights adaptive sampling may be on by default for some SDKs: check `Itemcount` / sampling rate.
- `Properties` values can be truncated (long strings cut at a fixed size, unverified).
- Content may sit in `AppTraces` events rather than on the dependency row: export both and join on `OperationId`.
