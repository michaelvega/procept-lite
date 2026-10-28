"""normalize: agent-trace exports -> normalized/runs.jsonl (one canonical run dossier per RUN).

Usage:
  python3 plite.py normalize --src FILE_OR_DIR... [--format auto|otel|langsmith|flat] [--map mapping.json]
                              [--prices prices.json] [--noise-regex RE] [--tool-regex RE] [--out DIR]

Layers (see ingest.py, schema.py, cleaning.py): decode -> parse -> flatten -> lift provider shapes (Cloud Logging,
Bedrock, X-Ray, Jaeger, Zipkin, ADK, OpenAI/Anthropic messages ...) -> infer a mapping for unknown schemas
(<out>/mapping.auto.json) -> map -> repair -> validate. Gaps degrade the analysis instead of blocking it (no times:
latency dropped; no tokens: chars/4 estimate flagged; no error field: errors derived from result text; missing
user/answer: run marked thin) and every drop/repair/assumption is counted in <out>/cleaning_report.md.

Input formats (auto-detected per record; JSON array / JSON object / JSONL / CSV / TSV, .gz ok, dirs recursive)
  1. otel      OpenTelemetry GenAI span rows (Logfire, Pydantic AI, OTel exporters, Azure Monitor / App Insights,
               GCP Cloud Trace, AWS X-Ray / CloudWatch / AgentCore once exported as span rows). Attributes may be a
               JSON string, a dict, an OTLP key/value list, or flattened columns (gen_ai.*).
  2. langsmith LangSmith / LangChain runs export (run_type llm|tool|chain|retriever, inputs/outputs, error, ...).
  3. flat      any table, via --map mapping.json. Mapping keys (all optional, values are column names):
                 run_id, conversation_id, trace_id, span_id, parent_id,
                 timestamp, end_timestamp, timestamp_format (strptime), duration_s | duration_ms,
                 role (user|assistant|tool|system), content,
                 kind + kind_values {"tool":[..],"llm":[..],"agent":[..],"user":[..],"assistant":[..]},
                 tool_name, tool_args, tool_result, error, status + status_error_values [..],
                 input_tokens, output_tokens, model, cost_usd, agent, environment

Heuristics (all of them)
  * Dedupe: records with the same (trace_id, span_id) are one span (paginated exports repeat spans). The copy that
    has an end/duration wins over one that does not.
  * Span kind (otel): gen_ai.operation.name invoke_agent -> agent; execute_tool or any gen_ai.tool.name -> tool;
    chat/text_completion/generate_content or a model/token usage present -> llm; span names "invoke_agent*",
    "agent run" -> agent, "chat *" -> llm, "execute_tool*", "running tool" -> tool; spans with a conversation/call
    id or any gen_ai.* attribute but none of the above -> chain (kept, not analysed); everything else ("other":
    http, db, framework glue) is NOISE and dropped. --noise-regex also drops by span name; --tool-regex forces tool.
  * Run id precedence: gen_ai.agent.call.id (own, else nearest ancestor, else the single call id of the trace) >
    LangSmith trace id / root run id > (flat only, no ids) conversation + turn counter (a new turn starts at a user
    message that follows assistant/tool activity) > trace_id. The chosen source is kept in extra.run_id_source.
  * Conversation id: gen_ai.conversation.id, session.id/session_id, thread_id, LangSmith metadata
    thread_id/session_id/conversation_id. LangSmith's top-level session_id is the PROJECT id and is ignored.
    No conversation id -> conversation_id == run_id.
  * user_message = last user message of the root agent span input (history is not re-counted), else of the earliest
    span that has one. final_answer = last assistant text WITHOUT tool calls of the root agent span output (or
    final_result), else latest agent span, else latest llm span. None -> ends_without_answer.
  * Tool failure = explicit signal only: error/exception columns or events, error.type, status ERROR, level>=17,
    is_exception, success=false, LangSmith error. An empty result is NOT a failure. Cancellation (error text
    mentions cancel) is not a failure: success=false, error="cancelled", counted in extra.cancelled.
  * Tokens: sum over llm spans; if none carry usage, the largest agent-span usage (avoids double counting).
  * cost_usd: per-span cost attributes if present, else tokens x --prices (longest model-name match); null if
    neither is available. Unpriced models are listed in extra.unpriced_models.
  * Runs without a root agent span (partial export) are kept: latency = first..last span, extra.no_root = true.
  * latency_s = root agent span duration, else last end - first start.
"""
import argparse
import json
import os
import re
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (MISSING, Span, add_ingest_args, clip, find_key, iso, iter_records, jloads, last_text,  # noqa: E402
                    load_json_file, otlp_attrs, parse_duration_s, pick, redact_secrets, to_epoch, to_float, to_int,
                    to_str, to_tokens, ok_dur, is_error_literal, real_error, write_json)
from common import redact  # noqa: E402
import cleaning as CL  # noqa: E402
import schema as SC  # noqa: E402

ARGS_MAX, RESULT_MAX, UTEXT_MAX, ATEXT_MAX = 2000, 1500, 4000, 6000

# ---------------------------------------------------------------- field alias tables (also used by check_logs)
F = {
    "trace": ("trace_id", "traceId", "TraceId", "operation_Id", "traceID", "trace", "OperationId"),
    "span": ("span_id", "spanId", "SpanId", "id", "Id", "segment_id", "spanID"),
    "parent": ("parent_span_id", "parentSpanId", "ParentSpanId", "parent_id", "operation_ParentId", "parentId",
               "ParentId", "parentSpanID"),
    "name": ("span_name", "spanName", "SpanName", "displayName", "name", "Name"),
    "start": ("start_timestamp", "start_time", "startTime", "start_time_unix_nano", "startTimeUnixNano", "StartTime",
              "timestamp", "Timestamp", "time"),
    "end": ("end_timestamp", "end_time", "endTime", "end_time_unix_nano", "endTimeUnixNano", "EndTime"),
    "call": ("gen_ai.agent.call.id", "agent.call.id", "agent_call_id", "gen_ai.agent.run.id"),
    "conv": ("gen_ai.conversation.id", "conversation.id", "conversation_id", "session.id", "session_id", "thread_id",
             "gen_ai.session.id", "langfuse.session.id"),
    "op": ("gen_ai.operation.name",),
    "tool": ("gen_ai.tool.name", "tool.name", "tool_name"),
    "args": ("gen_ai.tool.call.arguments", "gen_ai.tool.arguments", "tool_arguments", "tool.arguments",
             "gen_ai.tool.input", "tool.parameters", "tool_args", "traceloop.entity.input", "input.value",
             "tool.input", "input"),
    "result": ("gen_ai.tool.call.result", "gen_ai.tool.result", "tool_response", "tool.result", "gen_ai.tool.output",
               "tool_result", "traceloop.entity.output", "output.value", "tool.output", "result", "output"),
    "in_msgs": ("gen_ai.input.messages", "gen_ai.prompt", "llm.input_messages", "input.value", "all_messages_events",
                "pydantic_ai.all_messages", "all_messages", "input", "user_message", "question"),
    "out_msgs": ("gen_ai.output.messages", "gen_ai.completion", "llm.output_messages", "output.value", "final_result",
                 "all_messages_events", "pydantic_ai.all_messages", "all_messages", "output", "final_answer",
                 "answer"),
    "itok": ("gen_ai.usage.input_tokens", "gen_ai.usage.prompt_tokens", "llm.usage.prompt_tokens",
             "llm.token_count.prompt", "gen_ai.usage.input_token_count", "input_tokens", "prompt_tokens"),
    "otok": ("gen_ai.usage.output_tokens", "gen_ai.usage.completion_tokens", "llm.usage.completion_tokens",
             "llm.token_count.completion", "gen_ai.usage.output_token_count", "output_tokens", "completion_tokens"),
    "model": ("gen_ai.request.model", "gen_ai.response.model", "llm.model_name", "model_name", "llm.request.model",
              "model"),
    "cost": ("gen_ai.usage.cost", "gen_ai.usage.cost_usd", "operation.cost", "llm.usage.cost", "cost_usd", "cost",
             "total_cost"),
    "agent": ("gen_ai.agent.name", "agent_name", "agent.name"),
    "env": ("deployment.environment.name", "deployment.environment", "service.environment", "environment",
            "sentry-environment", "deployment_environment"),
}
ATTR_COLS = ("attributes", "attrs", "customDimensions", "Properties", "properties", "span_attributes",
             "attributes_json", "annotations", "labels", "metadata")
ATTR_PREFIXES = ("attributes.", "customDimensions.", "span_attributes.", "Properties.", "properties.")
ERR_CODES = {"ERROR", "STATUS_CODE_ERROR", "2", "FAILED", "FAILURE", "FAIL", "EXCEPTION"}
STATUS_KEYS = ("status", "Status", "status_code", "statusCode", "otel.status_code", "level", "level_name",
               "is_exception", "success", "Success", "error", "error.type", "exception.type", "exception_type",
               "exception.message", "exception_message", "otel.status_description")


class Opts(object):
    def __init__(self, fmt="auto", mapping=None, prices=None, noise_regex=None, tool_regex=None):
        self.fmt = fmt
        self.mapping = mapping
        self.prices = {str(k).lower(): v for k, v in (prices or {}).items()}
        self.noise_re = re.compile(noise_regex) if noise_regex else None
        self.tool_re = re.compile(tool_regex) if tool_regex else None
        self.dur_unit = None


def opts_from_args(a):
    mapping = None
    mp = getattr(a, "mapping", None)
    if not mp and getattr(a, "out", None) and os.path.isfile(os.path.join(a.out, "mapping.json")):
        mp = os.path.join(a.out, "mapping.json")
    if mp:
        try:
            mapping = load_json_file(mp)
        except (OSError, ValueError):
            raise ValueError("mapping file %s is not valid JSON" % os.path.basename(mp))
    prices = load_json_file(a.prices) if getattr(a, "prices", None) else None
    return Opts(a.format, mapping, prices, getattr(a, "noise_regex", None), getattr(a, "tool_regex", None))


# ---------------------------------------------------------------- helpers

def _attr(a, keys):
    for k in keys:
        v = a.get(k)
        if v not in MISSING:
            return v
    return None


def _flatten_into(d, prefix, out, depth=0):
    for k, v in d.items():
        kk = prefix + k
        if isinstance(v, dict) and depth < 5 and not any("." in x for x in v):
            _flatten_into(v, kk + ".", out, depth + 1)
        out.setdefault(kk, v)


def _cancelled(err):
    return bool(err) and "cancel" in err.lower()


def _status(rec, a, events):
    """(error text or None, has_status). Explicit signals only."""
    has, typ, msg, bad = False, None, None, False
    for k in STATUS_KEYS + ("http.status_code",):
        if (rec.get(k) not in MISSING) or (a.get(k) not in MISSING):
            has = True
            break
    st = jloads(pick(rec, ("status", "Status")))
    if isinstance(st, dict):
        code, msg = st.get("code"), st.get("message") or st.get("description")
        if str(code).upper() in ERR_CODES:
            bad = True
    elif st is not None and str(st).upper() in ERR_CODES:
        bad = True
    sc = pick(rec, ("status_code", "statusCode")) or a.get("otel.status_code")
    if sc is not None and str(sc).upper() in ERR_CODES:
        bad = True
        msg = msg or a.get("otel.status_description")
    lv = pick(rec, ("level", "level_num"))
    if lv is not None and to_float(lv) is not None and to_float(lv) >= 17:
        bad = True
    if str(rec.get("level_name") or "").lower() in ("error", "fatal", "critical"):
        bad = True
    for k in ("is_exception",):
        if str(rec.get(k)).lower() == "true":
            bad = True
    if str(pick(rec, ("success", "Success")) or "").lower() == "false":
        bad = True
    ae = a.get("error")
    if ae is True or (isinstance(ae, (str, dict, list, int, float)) and ae not in MISSING and real_error(ae)):
        bad = True
        msg = msg or (None if ae is True else to_str(ae))
    hs = a.get("http.status_code") or a.get("http.response.status_code")
    if hs not in MISSING and to_int(hs) >= 500:
        bad = True
        msg = msg or "HTTP %s" % hs
    t = pick(rec, ("exception_type",)) or a.get("exception.type") or a.get("error.type")
    m = pick(rec, ("exception_message",)) or a.get("exception.message") or a.get("error.message") or msg
    e = pick(rec, ("error",)) if isinstance(pick(rec, ("error",)), (str, dict)) else None
    for ev in events or ():
        if isinstance(ev, dict) and str(ev.get("name") or "").lower() == "exception":
            ea = otlp_attrs(ev.get("attributes"))
            t = t or ea.get("exception.type")
            m = m or ea.get("exception.message")
            bad = True
    if t or (e not in MISSING and e is not None and real_error(e)):
        bad = True
    if not bad:
        return None, has
    parts = [to_str(x) for x in (t, m or e) if x]
    return clip(": ".join(parts) if parts else "error", 300), True


_NEG_RE = re.compile(r"\b(no|without|zero|0)\s+(errors?|failures?|exceptions?)\b|\berrors?\s*[:=]\s*(0|none|null|false|\[\])",
                     re.I)
_ERR_TEXT_RE = re.compile(
    r"^\W*(error|err|exception|traceback|failed|failure|fatal|critical)\b"
    r"|\b(error|exception|failed|failure|fatal)\s*[:=#]"
    r"|(?-i:\b[A-Z][A-Za-z0-9_.]*(?:Exception|Error)\b)"
    r"|\berror\s+[45]\d\d\b|\b(http|status|code|statuscode)\W{0,4}[45]\d\d\b"
    r"|\b[45]\d\d\s+(error|internal|not found|bad|unauthori[sz]ed|forbidden|service|gateway|timeout|too many)"
    r"|\b(timeout|timed out|time out|connection (refused|reset|error|aborted)|unauthori[sz]ed|forbidden|"
    r"permission denied|rate.?limit(ed)?|traceback \(most|stack ?trace|unhandled|exception raised|"
    r"internal server error|bad gateway|service unavailable|gateway timeout|access denied|not authori[sz]ed|"
    r"quota exceeded|too many requests)\b", re.I)
_BAD_STATUS = ("error", "failed", "failure", "fail", "exception", "fault", "timeout", "denied", "rejected", "cancelled_error")


def _dict_error(d, depth=0):
    if not isinstance(d, dict) or depth > 2:
        return None
    for k in ("status", "state", "outcome", "result_status"):
        v = d.get(k)
        if isinstance(v, str) and (v.lower() in _BAD_STATUS or v.lower().startswith(("error", "fail"))):
            return clip(to_str(d.get("message") or d.get("error") or d.get("detail") or v), 300)
    err = d.get("error")
    if err not in MISSING and real_error(err):
        if isinstance(err, dict):
            err = err.get("message") or err.get("code") or to_str(err)
        return clip(to_str(err) if err is not True else (d.get("message") or d.get("detail") or "error"), 300)
    for k in ("ok", "success", "succeeded"):
        if d.get(k) is False or str(d.get(k)).lower() == "false":
            return clip(to_str(d.get("message") or d.get("detail") or d.get("reason") or "%s=false" % k), 300)
    for k in ("isError", "is_error", "iserror", "failed", "hasError", "has_error"):
        if d.get(k) is True or str(d.get(k)).lower() == "true":
            return clip(to_str(d.get("message") or d.get("content") or "error"), 300)
    for k in ("statusCode", "status_code", "http_status", "httpStatus", "code", "http_code"):
        v = d.get(k)
        if v not in MISSING and not isinstance(v, (dict, list)):
            n = to_int(v)
            if 400 <= n <= 599:
                return clip(to_str(d.get("message") or d.get("detail") or "HTTP %d" % n), 300)
    for k in ("exception", "traceback", "stack_trace", "stacktrace"):
        if d.get(k) not in MISSING:
            return clip(to_str(d[k]), 300)
    if isinstance(d.get("errors"), list) and d["errors"]:
        return clip(to_str(d["errors"][0]), 300)
    for k in ("result", "data", "response", "body", "output"):
        if isinstance(d.get(k), dict):
            e = _dict_error(d[k], depth + 1)
            if e:
                return e
    return None


def _result_error(result):
    """Error text derived from a tool RESULT when no explicit error field exists: JSON objects that say they failed
    (status error, error key, ok:false, isError, HTTP 4xx/5xx) and plain text with error/exception/traceback/
    timeout/HTTP 4xx-5xx patterns. None otherwise."""
    if result is None:
        return None
    if not isinstance(result, str):
        result = to_str(result)
    t = result.strip()
    if not t:
        return None
    if t[0] in "{[":
        d = jloads(t)
        if isinstance(d, dict):
            return _dict_error(d)
        if isinstance(d, list):
            for x in d[:5]:
                e = _dict_error(x) if isinstance(x, dict) else None
                if e:
                    return e
            return None
    head = t[:1500]
    if _NEG_RE.search(head):
        return None
    m = _ERR_TEXT_RE.search(head)
    if m:
        return clip(" ".join(head.split())[:200], 300)
    return None


def _events(rec, a):
    ev = jloads(rec.get("events"))
    if ev in MISSING:
        ev = jloads(a.get("events"))
    return ev if isinstance(ev, list) else []


def _event_texts(events):
    u = x = None
    for ev in events:
        if not isinstance(ev, dict):
            continue
        n = str(ev.get("name") or ev.get("event.name") or "").lower()
        ea = otlp_attrs(ev.get("attributes"))
        body = jloads(ev.get("body", ea.get("body")))
        if n.endswith("user.message"):
            u = last_text(body if body not in MISSING else ea.get("content"), "user", "user")[0] or u
        elif n.endswith("choice"):
            msg = (body or {}).get("message") if isinstance(body, dict) else None
            x = last_text(msg if msg else (body if body not in MISSING else ea.get("message")), "assistant",
                          "assistant")[0] or x
    return u, x


def _legacy_msgs(a, prefix):
    """gen_ai.prompt.0.role / gen_ai.prompt.0.content style flattened messages."""
    items = {}
    pre = prefix + "."
    for k, v in a.items():
        if k.startswith(pre):
            parts = k[len(pre):].split(".")
            if len(parts) == 2 and parts[0].isdigit():
                items.setdefault(int(parts[0]), {})[parts[1]] = v
    return [items[i] for i in sorted(items)] if items else None


# ---------------------------------------------------------------- parsers

def parse_otel(rec, opts):
    a = {}
    for k in ATTR_COLS:
        v = rec.get(k)
        if v not in MISSING:
            a.update(otlp_attrs(v))
    res = rec.get("_resource")
    if res:
        for k, v in res.items():
            a.setdefault(k, v)
    for k, v in rec.items():
        if v in MISSING or k in ATTR_COLS:
            continue
        if "." in k or k.startswith("gen_ai"):
            for pre in ATTR_PREFIXES:
                if k.startswith(pre):
                    k = k[len(pre):]
                    break
            a.setdefault(k, v)
    if isinstance(a.get("gen_ai"), dict):
        _flatten_into(a["gen_ai"], "gen_ai.", a)

    s = Span("otel")
    nm = rec.get("displayName")
    nm = nm.get("value") if isinstance(nm, dict) else nm
    s.name = to_str(nm if nm not in MISSING else pick(rec, F["name"]))
    if s.name.startswith("projects/"):
        s.name = ""
    s.sid = to_str(pick(rec, F["span"]))
    s.tid = to_str(pick(rec, F["trace"]))
    raw_name = to_str(rec.get("name"))
    m = re.match(r"projects/[^/]+/traces/([^/]+)/spans/([^/]+)", raw_name) if raw_name.startswith("projects/") else None
    if m:
        s.tid, s.sid = s.tid or m.group(1), s.sid or m.group(2)
    if s.tid and set(s.tid) <= {"0", "-"}:
        s.tid = ""
        CL.note("degenerate_trace_id")
    s.pid = to_str(pick(rec, F["parent"])) or None
    if s.pid and set(s.pid) <= {"0"}:
        s.pid = None
    s.start = to_epoch(pick(rec, F["start"]))
    s.end = to_epoch(pick(rec, F["end"]))
    d = pick(rec, ("duration_ms", "durationMs", "DurationMs", "elapsed_ms"))
    if d is not None:
        s.dur = parse_duration_s(d, "ms")
    else:
        d = pick(rec, ("duration_ns", "durationNano", "duration_nano"))
        if d is not None:
            s.dur = parse_duration_s(d, "ns")
        else:
            d = pick(rec, ("duration", "duration_s", "duration_sec", "durationSeconds", "Duration"))
            azure = any(k in rec for k in ("operation_Id", "itemType", "customDimensions", "operation_ParentId",
                                           "OperationId", "TimeGenerated"))
            s.dur = parse_duration_s(d, "ms" if azure else (opts.dur_unit or "s"))
    s.dur = ok_dur(s.dur)
    if s.start is not None and s.end is not None and s.end >= s.start:
        s.dur = s.end - s.start
    elif s.start is not None and s.end is not None:
        CL.note("inverted_times")
        s.end = None
    if s.end is None and s.start is not None and s.dur is not None:
        s.end = s.start + s.dur
    elif s.end is None and s.start is not None:
        CL.note("missing_end")

    s.call = to_str(_attr(a, F["call"])) or None
    s.conv = to_str(_attr(a, F["conv"])) or None
    s.agent = to_str(_attr(a, F["agent"])) or None
    s.env = to_str(_attr(a, F["env"])) or None
    s.user = to_str(pick(rec, ("user_Id", "user_id", "userId", "UserId")) or _attr(a, ("user.id", "enduser.id", "user_id"))) or None
    tool = _attr(a, F["tool"])
    op = str(_attr(a, F["op"]) or "").lower()
    low = s.name.lower()
    if not op:
        if low.startswith(("invoke_agent", "invoke agent", "agent run", "create_agent", "agent.run", "agent_run",
                           "agent:", "agent ")) or low == "agent":
            op = "invoke_agent"
        elif low.startswith("execute_tool") or low.startswith("running tool") and tool:
            op = "execute_tool"
        elif low == "chat" or low.startswith(("chat ", "text_completion", "generate_content")):
            op = "chat"
    s.model = to_str(_attr(a, F["model"])) or None
    s.itok = to_tokens(_attr(a, F["itok"]))
    s.otok = to_tokens(_attr(a, F["otok"]))
    s.cost = to_float(_attr(a, F["cost"]))
    if op in ("execute_tool",) or tool:
        s.kind = "tool"
    elif op in ("invoke_agent", "create_agent", "invoke_workflow"):
        s.kind = "agent"
    elif op in ("chat", "text_completion", "generate_content", "completion", "embeddings") or s.model or s.itok:
        s.kind = "llm"
    elif s.call or s.conv or any(k.startswith("gen_ai.") for k in a):
        s.kind = "chain"
    elif not s.pid and (_attr(a, F["in_msgs"]) is not None or _attr(a, F["out_msgs"]) is not None):
        s.kind = "agent"  # a root span carrying input/output text is the request itself (Zipkin/Jaeger tags etc.)
    else:
        s.kind = "other"
    if s.kind in ("other", "chain") and low.endswith(("user.message", "gen_ai.choice", "assistant.message")):
        s.kind = "msg"  # legacy GenAI events stored as separate log records
        s.utext, s.atext = _event_texts([{"name": low, "attributes": a}])
    if opts.tool_re and s.kind != "agent" and opts.tool_re.search(s.name):
        s.kind = "tool"
    if s.kind == "tool":
        t = to_str(tool) or re.sub(r"^(execute_tool|running tool)[:\s]*", "", s.name, flags=re.I).strip() or s.name
        s.tool = t
        v = _attr(a, F["args"])
        s.args = to_str(v) if v is not None else None
        v = _attr(a, F["result"])
        s.result = to_str(v) if v is not None else None
    if s.kind in ("agent", "llm", "chain"):
        ev = _events(rec, a)
        im = _attr(a, F["in_msgs"])
        if im is None:
            im = _legacy_msgs(a, "gen_ai.prompt")
        s.msg_tools = SC.msg_tool_calls(im, _attr(a, F["out_msgs"])) or None
        s.utext = last_text(im, "user", "user")[0] if im is not None else None
        om = _attr(a, F["out_msgs"])
        if om is None:
            om = _legacy_msgs(a, "gen_ai.completion")
        s.atext = last_text(om, "assistant", "assistant")[0] if om is not None else None
        if (s.utext is None or s.atext is None) and ev:
            u, x = _event_texts(ev)
            s.utext = s.utext or u
            s.atext = s.atext or x
    ev = _events(rec, a) if rec.get("events") not in MISSING or a.get("events") not in MISSING else None
    s.err, s.has_status = _status(rec, a, ev)
    s.cancelled = _cancelled(s.err)
    return s


def parse_langsmith(rec, opts):
    s = Span("langsmith")
    rt = str(rec.get("run_type") or "").lower()
    s.sid = to_str(pick(rec, ("id", "run_id", "span_id")))
    s.pid = to_str(pick(rec, ("parent_run_id", "parent_id"))) or None
    s.tid = to_str(pick(rec, ("trace_id",))) or (s.sid if not s.pid else "")
    s.name = to_str(rec.get("name"))
    s.start = to_epoch(rec.get("start_time"))
    s.end = to_epoch(rec.get("end_time"))
    if s.start is not None and s.end is not None:
        if s.end >= s.start:
            s.dur = s.end - s.start
        else:
            CL.note("inverted_times")
            s.end = None
    inputs, outputs = jloads(rec.get("inputs")), jloads(rec.get("outputs"))
    extra = jloads(rec.get("extra")) or {}
    meta = jloads(rec.get("metadata")) or (extra.get("metadata") if isinstance(extra, dict) else None) or {}
    if not isinstance(meta, dict):
        meta = {}
    conv = pick(rec, ("thread_id", "conversation_id")) or pick(meta, ("thread_id", "conversation_id", "session_id",
                                                                       "conversation.id"))
    s.conv = to_str(conv) or None
    s.agent = to_str(meta.get("agent_name") or meta.get("agent")) or None
    s.env = to_str(meta.get("environment") or meta.get("env")) or None
    if rt == "tool" or rt == "retriever":
        s.kind = "tool"
        s.tool = s.name
        i = inputs.get("input") if isinstance(inputs, dict) and set(inputs) <= {"input"} else inputs
        s.args = to_str(i) if i is not None else None
        o = outputs.get("output") if isinstance(outputs, dict) and set(outputs) <= {"output"} else outputs
        s.result = to_str(o) if o is not None else None
    elif rt == "llm":
        s.kind = "llm"
    elif not s.pid:
        s.kind = "agent"
    else:
        s.kind = "chain"
    if opts.tool_re and s.kind != "agent" and opts.tool_re.search(s.name):
        s.kind = "tool"
        s.tool = s.name
    if s.kind in ("agent", "llm"):
        s.utext = last_text(inputs, "user", "user")[0] if inputs not in MISSING else None
        s.atext = last_text(outputs, "assistant", "assistant")[0] if outputs not in MISSING else None
        s.msg_tools = SC.msg_tool_calls(inputs, outputs) or None
    s.itok = to_tokens(pick(rec, ("prompt_tokens", "input_tokens")))
    s.otok = to_tokens(pick(rec, ("completion_tokens", "output_tokens")))
    if not (s.itok or s.otok) and s.kind == "llm":
        s.itok = to_tokens(find_key(outputs, ("prompt_tokens", "input_tokens")))
        s.otok = to_tokens(find_key(outputs, ("completion_tokens", "output_tokens")))
    if s.kind in ("llm", "agent"):
        m = find_key(extra, ("ls_model_name", "model_name", "model", "deployment_name")) if extra else None
        s.model = to_str(m or find_key(outputs, ("model_name", "model"))) or None
    s.cost = to_float(rec.get("total_cost"))
    err = rec.get("error")
    s.has_status = ("error" in rec) or ("status" in rec)
    if err not in MISSING and real_error(err):
        s.err = clip(to_str(err), 300)
    elif str(rec.get("status") or "").lower() == "error":
        s.err = "error"
    s.cancelled = _cancelled(s.err)
    return s


# ---------------------------------------------------------------- loading

def _rename(rec, fm):
    """Copy columns named by `fm` (canonical -> source path) into canonical keys."""
    out = dict(rec)
    for canon, src in fm.items():
        if not isinstance(src, str):
            continue
        v = SC.get_path(rec, src)
        if v is not None and not (isinstance(v, str) and v in MISSING):
            out[canon] = v
    return out


def _sample_duration_unit(raws):
    """Plain `duration` columns without a unit: milliseconds when the typical value is large."""
    vals = []
    for r in raws[:500]:
        if isinstance(r, dict):
            v = to_float(pick(r, ("duration", "Duration")))
            if v is not None and v > 0:
                vals.append(v)
    if not vals:
        return None
    vals.sort()
    med = vals[len(vals) // 2]
    if med >= 100:
        CL.note("unit_guess", 1, "duration column read as milliseconds (median %.0f)" % med)
        return "ms"
    return None


def _no_runs_message(stats, mapping_notes, raw_cols):
    cols = ", ".join(clip(redact(str(c)), 40) for c in list(raw_cols)[:15]) or "none"
    return ("no usable columns found. Columns seen: %s. Point the pipeline at the right fields: write mapping.json "
            "with keys user_text (the user's question), answer_text (the agent's reply) and optionally run_id, "
            "conversation_id, timestamp, tools, then re-run with --map mapping.json (see templates/mapping.example.json)."
            % cols)


def load_spans(paths, opts, keep_stats=True):
    """Parse every record. Returns (spans, stats). Noise ("other" kind or --noise-regex) is dropped and counted.
    Layers: read -> lift provider shapes -> infer mapping for unknown shapes -> parse -> dedupe."""
    CL.CR.reset()
    stats = {"records": 0, "duplicates": 0, "noise_dropped": 0, "unknown_records": 0, "formats": Counter(),
             "columns": Counter(), "attr_keys": Counter(), "bad_lines": 0, "auto_mapping": None}
    recs = list(iter_records(paths, stats))
    stats["records"] = len(recs)
    items = []
    forced = opts.fmt if opts.fmt in ("otel", "langsmith") else None
    user_map = opts.mapping if isinstance(opts.mapping, dict) else None
    for rec in recs:
        if len(stats["columns"]) < 400 and stats["records"] <= 2000 or sum(stats["columns"].values()) < 60000:
            stats["columns"].update(rec.keys())
        if forced:
            items.append((forced, rec))
        elif user_map is not None and user_map.get("format") in (None, "flat"):
            items.append(("raw", rec))
        else:
            items.extend(SC.lift(rec))
    raws = [r for k, r in items if k == "raw"]
    mapping, auto = None, False
    if raws:
        if user_map is not None:
            mapping = user_map
        else:
            mapping, notes, ok = SC.infer(raws)
            if ok:
                auto = True
                CL.note("auto_mapping", len(raws))
                for n in notes:
                    CL.CR.info(n)
            else:
                mapping = None
                stats["mapping_failed"] = _no_runs_message(stats, notes, stats["columns"])
        if mapping is not None and mapping.get("format") in ("otel", "langsmith"):
            fm = mapping.get("rename") or {}
            kind = mapping["format"]
            CL.note("renamed_columns", len(raws))
            items = [((kind, _rename(r, fm)) if k == "raw" else (k, r)) for k, r in items]
            if auto:
                stats["auto_mapping"] = mapping
            mapping = None
        elif mapping is not None and auto:
            stats["auto_mapping"] = mapping
    _sort_events(items)
    if opts.dur_unit is None:
        opts.dur_unit = _sample_duration_unit([r for k, r in items if k == "otel"])
    flat_map = mapping if mapping is not None else {}
    if flat_map.get("duration_s") and flat_map.get("duration_unit") in (None, "auto"):
        vals = sorted(v for v in (to_float(SC.get_path(r, flat_map["duration_s"])) for r in raws[:500])
                      if v is not None and v > 0)
        flat_map = dict(flat_map)
        if vals and vals[len(vals) // 2] >= 100:
            flat_map["duration_unit"] = "ms"
            CL.note("unit_guess", 1, "duration column read as milliseconds (median %.0f)" % vals[len(vals) // 2])
        else:
            flat_map["duration_unit"] = "s"
    spans, seen = [], {}
    row_sigs = set()
    ctx = SC.Ctx()
    seq_in = 0
    unit = opts.dur_unit
    for kind, rec in items:
        seq_in += 1
        if kind == "raw" and mapping is None:
            stats["unknown_records"] += 1
            stats["formats"]["unknown"] += 1
            continue
        stats["formats"][{"canon": "canon", "raw": "flat"}.get(kind, kind)] += 1
        if kind in ("raw", "canon") and isinstance(rec, dict):  # exact duplicate rows (re-exported pages)
            tcol = flat_map.get("timestamp") if kind == "raw" else "timestamp"
            if tcol and SC.get_path(rec, tcol) not in MISSING:
                try:
                    rsig = json.dumps(rec, sort_keys=True, default=str)
                except (TypeError, ValueError):
                    rsig = None
                if rsig is not None:
                    if rsig in row_sigs:
                        stats["duplicates"] += 1
                        continue
                    row_sigs.add(rsig)
        if kind == "otel":
            new = [parse_otel(rec, opts)]
            if seq_in <= 2000 and isinstance(rec.get("attributes"), dict):
                for k in rec["attributes"]:
                    stats["attr_keys"][k] += 1
        elif kind == "langsmith":
            new = [parse_langsmith(rec, opts)]
        elif kind == "canon":
            new = SC.parse_flat(rec, SC.CANON_MAP, seq_in, ctx, unit)
        else:
            new = SC.parse_flat(rec, flat_map, seq_in, ctx, unit)
        for s in new:
            if s.kind == "other" or (opts.noise_re and opts.noise_re.search(s.name or "")):
                stats["noise_dropped"] += 1
                continue
            if s.sid:
                key = (s.tid, s.sid)
                dup = None
                for j in seen.get(key, ()):
                    if spans[j].kind == s.kind:
                        if _sig(spans[j]) == _sig(s) or _sig(spans[j], True) == _sig(s, True):
                            dup = j
                            break
                if dup is not None:
                    stats["duplicates"] += 1
                    old = spans[dup]
                    if old.end is None and s.end is not None:
                        s.seq = old.seq
                        spans[dup] = s
                    continue
                if any(spans[j].kind == s.kind for j in seen.get(key, ())):
                    CL.note("id_collision")
                seen.setdefault(key, []).append(len(spans))
            s.seq = len(spans)
            spans.append(s)
    stats["duplicates"] += ctx_dups(ctx)
    CL.note("duplicate_spans", stats["duplicates"])
    CL.note("noise_spans", stats["noise_dropped"])
    CL.note("unknown_records", stats["unknown_records"])
    return spans, stats


_EV_RANK = {"user": 0, "tool_call": 1, "tool_result": 2, "llm": 2, "assistant": 3, "final": 4}


def _sort_events(items):
    """Event rows (tool call / result / user / final) that arrive out of time order are sorted by time (ties by
    logical order) so tool calls pair with their results and turns do not mix. Other items keep their place."""
    idx = [i for i, (k, r) in enumerate(items) if k == "canon" and isinstance(r, dict) and r.get("event") in _EV_RANK]
    if len(idx) < 2:
        return
    keyed = []
    for n, i in enumerate(idx):
        r = items[i][1]
        t = to_epoch(r.get("timestamp"), note=False)
        if t is None:
            return
        keyed.append((t, _EV_RANK[r["event"]], r.get("seq_in_record") or 0, n, i))
    order = sorted(keyed)
    if [k[4] for k in order] == [k[4] for k in sorted(keyed, key=lambda k: k[3])]:
        return
    moved = sum(1 for a, b in zip(sorted(idx), [k[4] for k in order]) if a != b)
    snap = [items[k[4]] for k in order]
    for pos, it in zip(sorted(idx), snap):
        items[pos] = it
    CL.note("ordering_repaired", moved)


def _sig(s, loose=False):
    """Content signature of a span: same id + same signature = a repeated record; same id + different content = reuse."""
    if loose:
        return (s.kind, s.name, s.start, to_str(s.args or s.utext or "")[:200], to_str(s.result or s.atext or "")[:200])
    return (s.kind, s.name, s.start, s.end, s.pid, to_str(s.args or s.utext or "")[:500],
            to_str(s.result or s.atext or "")[:500], s.err)


def ctx_dups(ctx):
    return getattr(ctx, "dups", 0)


def m_has_span_id(opts):
    return bool(opts.mapping and opts.mapping.get("span_id"))


# ---------------------------------------------------------------- run grouping

TURN_GAP_S = 1800.0


def resolve_runs(spans):
    """Assign span.run following the run-id precedence. Returns {run_key: (source, [spans])}."""
    by_sid = {(s.tid, s.sid): s for s in spans if s.sid}
    cnt = Counter(s.sid for s in spans if s.sid)
    by_any = {s.sid: s for s in spans if s.sid and cnt[s.sid] == 1}

    def climb(s):
        cur, call, root = s, None, s
        for _ in range(64):
            if cur.call and call is None:
                call = cur.call
            root = cur
            nxt = (by_sid.get((cur.tid, cur.pid)) or by_any.get(cur.pid)) if cur.pid else None
            if nxt is None or nxt is cur:
                break
            cur = nxt
        return call, root

    trace_calls = {}
    for s in spans:
        if s.call and s.tid:
            trace_calls.setdefault(s.tid, set()).add(s.call)
    groups = {}

    # ---- spans with no call id and no trace id: group by turn (event/flat rows) or by time containment (spans)
    orphans = [s for s in spans if not s.call and not s.tid and not (s.pid and by_any.get(s.pid))]
    turn_of = {}
    nuser = Counter(s.tid for s in spans if s.fmt == "flat" and not s.call and s.tid and s.utext and not s.pid)
    split_tids = {t for t, n in nuser.items() if n > 1}  # one trace/session id shared by several user turns
    if split_tids:
        CL.note("turns_split", len(split_tids))
        orphans = orphans + [s for s in spans if s.fmt == "flat" and not s.call and not s.pid and s.tid in split_tids]
    flat_o = [s for s in orphans if s.fmt == "flat" and not s.pid]
    buckets = {}
    for s in flat_o:
        buckets.setdefault(s.tid if s.tid in split_tids else
                           (s.conv or ("u-" + SC.shash(s.user) if s.user else "_")), []).append(s)
    for b, lst in buckets.items():
        if all(x.start is not None for x in lst):
            lst.sort(key=lambda x: (x.start, x.seq))
        n, has_user, closed, last_t = 0, False, False, None
        for s in lst:
            gap = last_t is not None and s.start is not None and s.start - last_t > TURN_GAP_S
            if (s.utext and has_user) or (closed and not s.utext and False) or closed or (gap and (has_user or closed)):
                n += 1
                has_user, closed = False, False
            if s.utext:
                has_user = True
            if s.final:
                closed = True
            if s.start is not None:
                last_t = s.start
            turn_of[s.seq] = "%s#t%d" % (b, n + 1)
    span_o = [s for s in orphans if s.fmt != "flat" or s.pid]
    span_key = {}
    if span_o:
        span_o.sort(key=lambda x: (x.start if x.start is not None else float("inf"), x.seq))
        if all(x.start is None for x in span_o):
            span_o.sort(key=lambda x: x.seq)
        cur_key, cur_end, n = None, None, 0
        for s in span_o:
            starts_new = s.kind == "agent" or cur_key is None or (
                cur_end is not None and s.start is not None and s.start > cur_end + 1.0)
            if s.kind == "agent" and cur_key is not None and cur_end is not None and s.start is not None \
                    and s.start <= cur_end:
                starts_new = False  # nested agent span
            if starts_new:
                n += 1
                cur_key = "orph%d" % n
                cur_end = s.end if s.end is not None else s.start
            else:
                if s.end is not None and (cur_end is None or s.end > cur_end):
                    cur_end = s.end
            span_key[s.seq] = cur_key
    for s in spans:
        if s.call:
            key, src = s.call, "call_id"
        else:
            call, root = climb(s)
            if call:
                key, src = call, "call_id_ancestor"
            elif s.fmt == "langsmith" and (s.tid or root.tid or root.sid):
                key, src = (s.tid or root.tid or root.sid), "langsmith_trace"
            elif s.tid in split_tids and s.seq in turn_of:
                key, src = turn_of[s.seq], "conversation_turn"
            elif s.tid and len(trace_calls.get(s.tid, ())) == 1:
                key, src = next(iter(trace_calls[s.tid])), "call_id_trace"
            elif s.tid:
                key, src = s.tid, "trace_id"
            elif s.seq in turn_of:
                key, src = turn_of[s.seq], "conversation_turn"
            elif s.seq in span_key:
                key, src = span_key[s.seq], "time_containment"
            elif s.conv:
                key, src = s.conv, "conversation"
            else:
                key, src = "span%d" % s.seq, "span"
        s.run = key
        g = groups.setdefault(key, [src, []])
        g[1].append(s)
    return groups


# ---------------------------------------------------------------- run building

def _price(prices, model, itok, otok):
    if not prices or not model:
        return None
    ml = model.lower().split("/")[-1]
    p = prices.get(ml) or prices.get(model.lower())
    if p is None:
        best = [k for k in prices if k and (k in ml or ml in k)]
        if best:
            p = prices[max(best, key=len)]
    if not isinstance(p, dict):
        return None
    return itok / 1e6 * float(p.get("input_per_mtok", 0)) + otok / 1e6 * float(p.get("output_per_mtok", 0))


def _cl(text, n, what):
    """clip + redact secrets; counts truncation and redaction in the ledger."""
    if text is None:
        return None
    text = str(text)
    if len(text) > n:
        CL.note("field_truncated", 1, what)
    out = clip(text, n)
    red = redact_secrets(out)
    if red != out:
        CL.note("secrets_redacted")
    return red


def build_run(rid, src, spans, opts):
    inf = float("inf")
    spans.sort(key=lambda s: (s.start if s.start is not None else inf, s.seq))
    core = [s for s in spans if s.kind in ("agent", "llm", "tool", "msg")]
    agents = [s for s in spans if s.kind == "agent"]
    llms = [s for s in spans if s.kind == "llm"]
    tools = [s for s in spans if s.kind == "tool"]
    sids = {s.sid for s in spans if s.sid}
    tops = [s for s in agents if not s.pid or s.pid not in sids]
    root = max(tops, key=lambda s: s.dur or 0) if tops else None
    starts = [s.start for s in (core or spans) if s.start is not None]
    ends = [(s.end if s.end is not None else s.start) for s in (core or spans)]
    ends = [e for e in ends if e is not None]
    first, last = (min(starts) if starts else None), (max(ends) if ends else None)
    if root is not None and root.dur:
        latency = root.dur
    else:
        latency = (last - first) if first is not None and last is not None else None
    if latency is not None and latency <= 0:
        latency = None
    lat_from_dur = False
    if latency is None and not starts:
        ds = [s.dur for s in tools + llms if s.dur]
        if ds:
            latency, lat_from_dur = sum(ds), True  # no timestamps: serial sum of the step durations

    user = (root.utext if root and root.utext else None) or next((s.utext for s in spans if s.utext), None)
    answer = root.atext if root and root.atext else None
    if not answer:
        for pool in (agents, llms, [s for s in spans if s.kind == "msg"]):
            cand = [s for s in pool if s.atext]
            if cand:
                answer = max(cand, key=lambda s: (s.end or s.start or 0, s.seq)).atext
                break
    thin = []
    inferred_user = inferred_answer = False
    if not user:
        alt = next((s.args for s in tools if s.args and len(s.args) > 3), None)
        if alt:
            user, inferred_user = alt, True
        thin.append("no_user_message")
    ends_without_answer = not answer
    if not answer:
        alt = next((s.result for s in reversed(tools) if s.result and not s.err), None) or \
            next((s.result for s in reversed(tools) if s.result), None)
        if alt:
            inferred_answer = True
        thin.append("no_final_answer")
    else:
        alt = None

    if not tools:
        merged, order = {}, []
        for sp in spans:
            for c in (sp.msg_tools or ()):
                key = c["id"] if c["id"] not in MISSING else (c["name"], to_str(c["args"]))
                cur = merged.get(key)
                if cur is None:
                    merged[key] = dict(c, _start=sp.start)
                    order.append(key)
                else:
                    for f in ("result", "args"):
                        if cur[f] in (None, "") and c[f] not in (None, ""):
                            cur[f] = c[f]
                    cur["error"] = cur["error"] or c["error"]
        for key in order:
            c = merged[key]
            t = Span(spans[0].fmt)
            t.kind, t.tool, t.name = "tool", c["name"], c["name"]
            t.args = to_str(c["args"]) if c["args"] is not None else None
            t.result = to_str(c["result"]) if c["result"] is not None else None
            t.err = "error" if c["error"] else None
            t.has_status = bool(c["error"])
            t.start, t.seq = c["_start"], len(spans)
            tools.append(t)
            spans = spans + [t]
        if order:
            CL.note("tool_calls_from_messages", len(order))
    calls, errors, cancelled, derived = [], [], 0, 0
    for t in tools:
        if not t.err:
            e = _result_error(t.result)
            if e:
                t.err = e
                t.has_status = True
                derived += 1
        failed = bool(t.err) and not t.cancelled
        if t.cancelled:
            cancelled += 1
        calls.append({"ts": iso(t.start), "name": t.tool or t.name,
                      "duration_s": round(t.dur, 4) if t.dur is not None else None,
                      "args": _cl(t.args or "", ARGS_MAX, "tool args"), "result": _cl(t.result or "", RESULT_MAX, "tool result"),
                      "success": not t.err, "error": ("cancelled" if t.cancelled else _cl(t.err, 300, "error")) if t.err else None})
    if derived:
        CL.note("errors_from_text", derived)
    for s in spans:
        if s.err and not s.cancelled and s.err not in errors and len(errors) < 20:
            errors.append(_cl(s.err, 200, "error"))

    models = []
    for s in llms + agents:
        if s.model and s.model not in models and (s.kind == "llm" or not llms):
            models.append(s.model)
    itok = sum(s.itok or 0 for s in llms)
    otok = sum(s.otok or 0 for s in llms)
    msgs = [s for s in spans if s.kind == "msg" and (s.itok or s.otok)]
    if not (itok or otok) and not agents and msgs:
        def _sum(vals):
            vals = [v for v in vals if v]
            return vals[0] if len(set(vals)) == 1 and len(vals) > 1 else sum(vals)  # repeated run totals count once
        itok, otok = _sum([s.itok for s in msgs]), _sum([s.otok for s in msgs])
    if not (itok or otok) and (agents or [s for s in spans if s.itok or s.otok]):
        pool_t = agents + [s for s in spans if s.kind == "msg"]
        a = max(pool_t, key=lambda s: (s.itok or 0) + (s.otok or 0)) if pool_t else None
        if a is not None:
            itok, otok = a.itok or 0, a.otok or 0
    cost, unpriced = None, []
    pool = llms if any((s.itok or s.otok or s.cost) for s in llms) else agents + [s for s in spans if s.kind == "msg"]
    for s in pool:
        c = s.cost
        if c is None and (s.itok or s.otok):
            c = _price(opts.prices, s.model, s.itok or 0, s.otok or 0)
            if c is None and opts.prices and s.model and s.model not in unpriced:
                unpriced.append(s.model)
        if c is not None:
            cost = (cost or 0.0) + c
    tokens_estimated = False
    if not (itok or otok):
        chars_in = len(user or "") + sum(len(c["args"] or "") + len(c["result"] or "") for c in calls)
        chars_out = len(answer or "") if answer else 0
        if chars_in or chars_out:
            itok, otok = (chars_in + 3) // 4, (chars_out + 3) // 4
            tokens_estimated = True
            CL.note("tokens_estimated")
            if cost is None and models:
                cost = _price(opts.prices, models[0], itok, otok)
    explicit_conv = next((s.conv for s in ([root] if root else []) + spans if s.conv), None) or \
        next((s.tid for s in ([root] if root else []) + spans if s.tid), None)
    agent = next((s.agent for s in ([root] if root else []) + spans if s.agent), None)
    if not agent and root and root.name:
        agent = re.sub(r"^(invoke_agent|agent run)\s*", "", root.name, flags=re.I).strip() or ""
    env = next((s.env for s in spans if s.env), None)
    user_id = next((s.user for s in spans if s.user), None)
    extra = {"run_id_source": src, "spans": len(spans)}
    if not root and spans[0].fmt not in ("flat",):
        extra["no_root"] = True
        CL.note("runs_without_root")
    if lat_from_dur:
        extra["latency_from_durations"] = True
        CL.note("latency_from_durations")
    if cancelled:
        extra["cancelled"] = cancelled
    if unpriced:
        extra["unpriced_models"] = unpriced
    if tokens_estimated:
        extra["tokens_estimated"] = True
    if thin:
        extra["thin"] = True
        extra["thin_reasons"] = thin
        CL.note("thin_run")
    if inferred_user:
        extra["user_message_inferred"] = True
        CL.note("user_message_inferred")
    if inferred_answer:
        extra["answer_inferred"] = True
        CL.note("answer_inferred")
    if user_id:
        extra["user_id"] = "u-" + SC.shash(user_id)
    return {
        "run_id": rid, "conversation_id": explicit_conv or "", "agent": agent or "", "environment": env or "",
        "first_ts": iso(first), "last_ts": iso(last), "latency_s": round(latency, 4) if latency is not None else None,
        "user_message": _cl(user or "", UTEXT_MAX, "user message"),
        "final_answer": _cl(answer, ATEXT_MAX, "final answer") if answer else (
            _cl(alt, ATEXT_MAX, "final answer") if inferred_answer else None),
        "ends_without_answer": ends_without_answer, "tool_calls": calls, "n_llm_calls": len(llms),
        "models": models, "input_tokens": itok, "output_tokens": otok,
        "cost_usd": round(cost, 6) if cost is not None else None,
        "n_tool_failures": sum(1 for c in calls if c["error"] and c["error"] != "cancelled"),
        "errors": errors, "extra": extra,
    }


def _synth_ids(runs, groups_src):
    """Stable hash ids for runs whose key is not a real id (no call/trace/conversation id in the data)."""
    seen = set()
    for r in runs:
        src, rid = r["extra"]["run_id_source"], r["run_id"]
        if src in ("time_containment", "span") or (src == "conversation_turn" and rid.startswith(("_#", "u-"))):
            h = "run-" + SC.shash(r["first_ts"], r["user_message"][:300], (r["final_answer"] or "")[:200],
                                   len(r["tool_calls"]))
            base, i = h, 1
            while h in seen:
                i += 1
                h = "%s-%d" % (base, i)
            r["extra"]["synthetic_run_id"] = True
            r["run_id"] = h
            CL.note("run_id_synthesized")
        seen.add(r["run_id"])


def _synth_conversations(runs):
    pend = [r for r in runs if not r["conversation_id"]]
    if not pend:
        return
    pend.sort(key=lambda r: (r["extra"].get("user_id") or "", r["first_ts"] or "~", r["run_id"]))
    prev, grp, n_in, last_t = None, 0, 0, None
    for r in pend:
        u = r["extra"].get("user_id") or ""
        t = to_epoch(r["first_ts"]) if r["first_ts"] else None
        lt = to_epoch(r["last_ts"]) if r["last_ts"] else t
        new = prev is None or u != prev or n_in >= 50 or (
            t is not None and last_t is not None and t - last_t > TURN_GAP_S)
        if new:
            grp += 1
            n_in = 0
            head = r["run_id"]
        r["conversation_id"] = "conv-" + SC.shash(u, head)
        r["extra"]["conversation_synthesized"] = True
        n_in += 1
        prev, last_t = u, (lt if lt is not None else t)
    CL.note("conversation_synthesized", len(pend))


def build_runs(spans, opts):
    """Return the list of run dossiers sorted by first_ts."""
    groups = resolve_runs(spans)
    runs = [build_run(k, g[0], g[1], opts) for k, g in groups.items()]
    runs = [r for r in runs if r["extra"]["spans"]]
    _synth_ids(runs, groups)
    _synth_conversations(runs)
    for r in runs:
        if not r["conversation_id"]:
            r["conversation_id"] = r["run_id"]
    runs.sort(key=lambda r: (r["first_ts"] or "~", r["run_id"]))
    orphan = 0
    for g in groups.values():
        sids = {x.sid for x in g[1] if x.sid}
        orphan += sum(1 for s in g[1] if s.pid and s.pid not in sids)
    CL.note("orphan_spans", orphan)
    return runs


def finalize_dimensions(runs):
    """Drop analysis dimensions the data cannot support and say so in the ledger."""
    dims = {"latency": True, "cost": True, "errors": True}
    if runs and not any(r["latency_s"] for r in runs):
        dims["latency"] = False
        CL.CR.drop_dimension("latency", "no usable timestamps or durations in the data; latency analysis is skipped "
                                        "(cost, errors and score are kept)")
    if runs and not any(r["input_tokens"] or r["output_tokens"] for r in runs) and \
            not any(r["cost_usd"] for r in runs):
        dims["cost"] = False
        CL.CR.drop_dimension("cost", "no token counts, cost values or text to estimate tokens from; cost analysis is skipped")
    elif runs and any(r["extra"].get("tokens_estimated") for r in runs):
        CL.CR.info("tokens are estimated as characters/4 for %d runs (flagged extra.tokens_estimated); cost_usd needs "
                   "model prices" % sum(1 for r in runs if r["extra"].get("tokens_estimated")))
    if runs and not any(r["tool_calls"] for r in runs):
        CL.CR.info("no tool calls in the data: the CIG has only the agent node; error analysis covers answers only")
    return dims


def run_all(paths, opts):
    spans, stats = load_spans(paths, opts)
    return build_runs(spans, opts), spans, stats


def write_outputs(out, runs, spans, stats):
    """runs.jsonl, ingest_stats.json, mapping.auto.json, cleaning_report.md."""
    os.makedirs(os.path.join(out, "normalized"), exist_ok=True)
    path = os.path.join(out, "normalized", "runs.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        for r in runs:
            f.write(json.dumps(r, ensure_ascii=False, allow_nan=False, default=str) + "\n")
    dims = finalize_dimensions(runs)
    summ = {"records": stats["records"], "spans_kept": len(spans), "duplicates_dropped": stats["duplicates"],
            "noise_dropped": stats["noise_dropped"], "unknown_records": stats["unknown_records"],
            "unreadable": stats["unknown_records"] + stats["bad_lines"],
            "runs": len(runs), "conversations": len({r["conversation_id"] for r in runs}),
            "formats": dict(stats["formats"]), "dimensions": dims}
    write_json(os.path.join(out, "normalized", "ingest_stats.json"), summ)
    if stats.get("auto_mapping"):
        am = dict(stats["auto_mapping"])
        am["_comment"] = ("scaffolded by heuristics from column names and value shapes; edit and pass with --map "
                          "to override")
        write_json(os.path.join(out, "mapping.auto.json"), am)
    return path, summ


def main(argv=None):
    p = argparse.ArgumentParser(prog="plite normalize", description="traces -> normalized/runs.jsonl")
    add_ingest_args(p)
    a = p.parse_args(argv)
    try:
        opts = opts_from_args(a)
        runs, spans, stats = run_all(a.src, opts)
    except (FileNotFoundError, ValueError) as e:
        print("error: %s" % e, file=sys.stderr)
        return 2
    except SystemExit as e:
        print("error: %s" % e, file=sys.stderr)
        return 2
    if not runs:
        CL.CR.write(a.out)
        print("error: %s" % (stats.get("mapping_failed") or
                             "no runs could be built from %d records (formats seen: %s); see %s" % (
                                 stats["records"], dict(stats["formats"]),
                                 os.path.join(a.out, "cleaning_report.md"))), file=sys.stderr)
        return 2
    path, summ = write_outputs(a.out, runs, spans, stats)
    CL.CR.write(a.out)
    print("normalized %d runs (%d conversations) from %d records -> %s" % (
        len(runs), summ["conversations"], stats["records"], path))
    print("  spans kept %d, duplicates %d, noise dropped %d, unreadable %d" % (
        len(spans), stats["duplicates"], stats["noise_dropped"], summ["unreadable"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
