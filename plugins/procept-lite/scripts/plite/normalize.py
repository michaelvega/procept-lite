"""normalize: agent-trace exports -> normalized/runs.jsonl (one canonical run dossier per RUN).

Usage:
  python3 plite.py normalize --src FILE_OR_DIR... [--format auto|otel|langsmith|flat] [--map mapping.json]
                              [--prices prices.json] [--noise-regex RE] [--tool-regex RE] [--out DIR]

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
from common import (MISSING, add_ingest_args, clip, find_key, iso, iter_records, jloads, last_text,  # noqa: E402
                    load_json_file, otlp_attrs, parse_duration_s, pick, to_epoch, to_float, to_int,
                    to_str, write_json)

ARGS_MAX, RESULT_MAX, UTEXT_MAX, ATEXT_MAX = 2000, 1500, 4000, 6000

# ---------------------------------------------------------------- field alias tables (also used by check_logs)
F = {
    "trace": ("trace_id", "traceId", "TraceId", "operation_Id", "traceID", "trace"),
    "span": ("span_id", "spanId", "SpanId", "id", "Id", "segment_id"),
    "parent": ("parent_span_id", "parentSpanId", "ParentSpanId", "parent_id", "operation_ParentId", "parentId",
               "ParentId"),
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
             "gen_ai.tool.input", "tool.parameters", "tool_args", "traceloop.entity.input"),
    "result": ("gen_ai.tool.call.result", "gen_ai.tool.result", "tool_response", "tool.result", "gen_ai.tool.output",
               "tool_result", "traceloop.entity.output"),
    "in_msgs": ("gen_ai.input.messages", "gen_ai.prompt", "llm.input_messages", "input.value", "all_messages_events",
                "pydantic_ai.all_messages", "all_messages"),
    "out_msgs": ("gen_ai.output.messages", "gen_ai.completion", "llm.output_messages", "output.value", "final_result",
                 "all_messages_events", "pydantic_ai.all_messages", "all_messages"),
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


class Span(object):
    __slots__ = ("tid", "sid", "pid", "name", "start", "end", "dur", "kind", "call", "conv", "agent", "env", "tool",
                 "args", "result", "utext", "atext", "itok", "otok", "model", "cost", "err", "cancelled",
                 "has_status", "fmt", "seq", "run", "src")

    def __init__(self, fmt):
        for s in self.__slots__:
            setattr(self, s, None)
        self.fmt = fmt
        self.cancelled = False
        self.has_status = False


class Opts(object):
    def __init__(self, fmt="auto", mapping=None, prices=None, noise_regex=None, tool_regex=None):
        self.fmt = fmt
        self.mapping = mapping
        self.prices = {str(k).lower(): v for k, v in (prices or {}).items()}
        self.noise_re = re.compile(noise_regex) if noise_regex else None
        self.tool_re = re.compile(tool_regex) if tool_regex else None


def opts_from_args(a):
    mapping = load_json_file(a.mapping) if getattr(a, "mapping", None) else None
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
    for k in STATUS_KEYS:
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
    t = pick(rec, ("exception_type",)) or a.get("exception.type") or a.get("error.type")
    m = pick(rec, ("exception_message",)) or a.get("exception.message") or a.get("error.message") or msg
    e = pick(rec, ("error",)) if isinstance(pick(rec, ("error",)), (str, dict)) else None
    for ev in events or ():
        if isinstance(ev, dict) and str(ev.get("name") or "").lower() == "exception":
            ea = otlp_attrs(ev.get("attributes"))
            t = t or ea.get("exception.type")
            m = m or ea.get("exception.message")
            bad = True
    if t or e:
        bad = True
    if not bad:
        return None, has
    parts = [to_str(x) for x in (t, m or e) if x]
    return clip(": ".join(parts) if parts else "error", 300), True


def _result_error(result):
    """Error text when a tool RESULT is a JSON object that itself says it failed ({"status":"error"} or a
    non-empty "error" key); the span status stays OK in many agent frameworks. None otherwise."""
    if not isinstance(result, str) or not result.lstrip().startswith("{"):
        return None
    d = jloads(result)
    if not isinstance(d, dict):
        return None
    err = d.get("error")
    if str(d.get("status") or "").lower() in ("error", "failed", "failure") or (err and not isinstance(err, bool)):
        if isinstance(err, dict):
            err = err.get("message") or err.get("code") or to_str(err)
        return clip(to_str(err) if err else "tool result status error", 300)
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
            s.dur = parse_duration_s(d, "ms" if "operation_Id" in rec else "s")
    if s.dur is None and s.start is not None and s.end is not None:
        s.dur = max(0.0, s.end - s.start)
    if s.end is None and s.start is not None and s.dur is not None:
        s.end = s.start + s.dur

    s.call = to_str(_attr(a, F["call"])) or None
    s.conv = to_str(_attr(a, F["conv"])) or None
    s.agent = to_str(_attr(a, F["agent"])) or None
    s.env = to_str(_attr(a, F["env"])) or None
    tool = _attr(a, F["tool"])
    op = str(_attr(a, F["op"]) or "").lower()
    low = s.name.lower()
    if not op:
        if low.startswith(("invoke_agent", "invoke agent", "agent run", "create_agent")):
            op = "invoke_agent"
        elif low.startswith("execute_tool") or low.startswith("running tool") and tool:
            op = "execute_tool"
        elif low == "chat" or low.startswith(("chat ", "text_completion", "generate_content")):
            op = "chat"
    s.model = to_str(_attr(a, F["model"])) or None
    s.itok = to_int(_attr(a, F["itok"]))
    s.otok = to_int(_attr(a, F["otok"]))
    s.cost = to_float(_attr(a, F["cost"]))
    if op in ("execute_tool",) or tool:
        s.kind = "tool"
    elif op in ("invoke_agent", "create_agent", "invoke_workflow"):
        s.kind = "agent"
    elif op in ("chat", "text_completion", "generate_content", "completion", "embeddings") or s.model or s.itok:
        s.kind = "llm"
    elif s.call or s.conv or any(k.startswith("gen_ai.") for k in a):
        s.kind = "chain"
    else:
        s.kind = "other"
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
        s.dur = max(0.0, s.end - s.start)
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
    s.itok = to_int(pick(rec, ("prompt_tokens", "input_tokens")))
    s.otok = to_int(pick(rec, ("completion_tokens", "output_tokens")))
    if not (s.itok or s.otok) and s.kind == "llm":
        s.itok = to_int(find_key(outputs, ("prompt_tokens", "input_tokens")))
        s.otok = to_int(find_key(outputs, ("completion_tokens", "output_tokens")))
    if s.kind in ("llm", "agent"):
        m = find_key(extra, ("ls_model_name", "model_name", "model", "deployment_name")) if extra else None
        s.model = to_str(m or find_key(outputs, ("model_name", "model"))) or None
    s.cost = to_float(rec.get("total_cost"))
    err = rec.get("error")
    s.has_status = ("error" in rec) or ("status" in rec)
    if err not in MISSING and err is not False:
        s.err = clip(to_str(err), 300)
    elif str(rec.get("status") or "").lower() == "error":
        s.err = "error"
    s.cancelled = _cancelled(s.err)
    return s


def parse_flat(rec, opts, seq):
    m = opts.mapping or {}

    def g(key):
        col = m.get(key)
        v = rec.get(col) if col else None
        return None if v in MISSING else v

    s = Span("flat")
    s.sid = to_str(g("span_id")) or ("r%d" % seq)
    s.tid = to_str(g("trace_id"))
    s.pid = to_str(g("parent_id")) or None
    s.call = to_str(g("run_id")) or None
    s.conv = to_str(g("conversation_id")) or None
    s.start = to_epoch(g("timestamp"), m.get("timestamp_format"))
    s.end = to_epoch(g("end_timestamp"), m.get("timestamp_format"))
    d = g("duration_s")
    s.dur = parse_duration_s(d, "s") if d is not None else parse_duration_s(g("duration_ms"), "ms")
    if s.dur is None and s.start is not None and s.end is not None:
        s.dur = max(0.0, s.end - s.start)
    if s.end is None and s.start is not None and s.dur is not None:
        s.end = s.start + s.dur
    s.agent, s.env = to_str(g("agent")) or None, to_str(g("environment")) or None
    s.model = to_str(g("model")) or None
    s.itok, s.otok, s.cost = to_int(g("input_tokens")), to_int(g("output_tokens")), to_float(g("cost_usd"))
    role = str(g("role") or "").lower()
    role = {"human": "user", "ai": "assistant", "model": "assistant", "bot": "assistant"}.get(role, role)
    content = g("content")
    kv = {k: [str(x).lower() for x in v] for k, v in (m.get("kind_values") or {}).items()}
    kcol = str(g("kind") or "").lower()
    kind = None
    for k, vals in kv.items():
        if kcol and kcol in vals:
            kind = k
    if kind in ("user", "assistant"):
        role, kind = kind, None
    tool = g("tool_name")
    if kind == "tool" or tool is not None:
        s.kind = "tool"
        s.tool = to_str(tool) or s.name or "tool"
        s.name = s.tool
        s.args = to_str(g("tool_args")) if g("tool_args") is not None else None
        s.result = to_str(g("tool_result")) if g("tool_result") is not None else (
            to_str(content) if role == "tool" and content is not None else None)
    elif kind == "agent":
        s.kind = "agent"
    elif kind == "llm" or s.model or s.itok:
        s.kind = "llm"
    elif role in ("user", "assistant", "tool", "system"):
        s.kind = "msg"
    else:
        s.kind = "other"
    if content is not None and s.kind != "tool":
        if role == "user":
            s.utext = to_str(content)
        elif role == "assistant":
            s.atext = to_str(content).strip() or None
    s.name = s.name or s.kind
    ecol, scol = m.get("error"), m.get("status")
    s.has_status = bool((ecol and ecol in rec) or (scol and scol in rec))
    e = g("error")
    if e is not None and str(e).lower() not in ("false", "0", "ok", "none", "success"):
        s.err = clip(to_str(e) if str(e).lower() not in ("true", "1") else "error", 300)
    st = g("status")
    if st is not None and str(st).lower() in [x.lower() for x in (m.get("status_error_values") or
                                                                 ["error", "failed", "failure", "exception", "fail"])]:
        s.err = s.err or clip(to_str(st), 300)
    s.cancelled = _cancelled(s.err)
    return s


def detect_format(rec, opts):
    if opts.fmt != "auto":
        return opts.fmt
    if opts.mapping:
        return "flat"
    if "run_type" in rec or "parent_run_id" in rec or "dotted_order" in rec:
        return "langsmith"
    if any(k in rec for k in ("span_id", "spanId", "SpanId", "trace_id", "traceId", "operation_Id", "attributes",
                              "customDimensions", "Properties", "span_name", "displayName")) \
            or any(k.startswith("gen_ai") for k in rec):
        return "otel"
    return "unknown"


# ---------------------------------------------------------------- loading

def load_spans(paths, opts, keep_stats=True):
    """Parse every record. Returns (spans, stats). Noise ("other" kind or --noise-regex) is dropped and counted."""
    stats = {"records": 0, "duplicates": 0, "noise_dropped": 0, "unknown_records": 0, "formats": Counter(),
             "columns": Counter(), "attr_keys": Counter(), "bad_lines": 0}
    spans, seen = [], {}
    for rec in iter_records(paths, stats):
        stats["records"] += 1
        if stats["records"] <= 2000:
            stats["columns"].update(rec.keys())
        fmt = detect_format(rec, opts)
        stats["formats"][fmt] += 1
        if fmt == "unknown":
            stats["unknown_records"] += 1
            continue
        if fmt == "otel":
            s = parse_otel(rec, opts)
        elif fmt == "langsmith":
            s = parse_langsmith(rec, opts)
        else:
            if not opts.mapping:
                raise SystemExit("flat format needs --map mapping.json (see templates/mapping.example.json)")
            s = parse_flat(rec, opts, stats["records"])
        if stats["records"] <= 2000 and fmt == "otel":
            for k in rec.get("attributes") if isinstance(rec.get("attributes"), dict) else ():
                stats["attr_keys"][k] += 1
        if s.kind == "other" or (opts.noise_re and opts.noise_re.search(s.name or "")):
            stats["noise_dropped"] += 1
            continue
        if s.sid and fmt != "flat" or (fmt == "flat" and m_has_span_id(opts)):
            key = (s.tid, s.sid)
            j = seen.get(key)
            if j is not None:
                stats["duplicates"] += 1
                old = spans[j]
                if old.end is None and s.end is not None:
                    s.seq = old.seq
                    spans[j] = s
                continue
            seen[key] = len(spans)
        s.seq = len(spans)
        spans.append(s)
    return spans, stats


def m_has_span_id(opts):
    return bool(opts.mapping and opts.mapping.get("span_id"))


# ---------------------------------------------------------------- run grouping

def resolve_runs(spans):
    """Assign span.run following the run-id precedence. Returns {run_key: (source, [spans])}."""
    by_sid = {s.sid: s for s in spans if s.sid}
    call_memo, root_memo = {}, {}

    def climb(s):
        chain, cur, call, root = [], s, None, s
        for _ in range(64):
            if cur.call and call is None:
                call = cur.call
            root = cur
            chain.append(cur)
            nxt = by_sid.get(cur.pid) if cur.pid else None
            if nxt is None or nxt is cur:
                break
            cur = nxt
        return call, root

    trace_calls = {}
    for s in spans:
        if s.call and s.tid:
            trace_calls.setdefault(s.tid, set()).add(s.call)
    groups = {}
    turn_state = {}
    flat_orphans = [s for s in spans if s.fmt == "flat" and not s.call and not s.tid]
    flat_orphans.sort(key=lambda s: (s.conv or "", s.start if s.start is not None else float("inf"), s.seq))
    turn_of = {}
    for s in flat_orphans:
        c = s.conv or "_"
        st = turn_state.setdefault(c, {"n": 0, "active": False})
        if s.utext and st["active"]:
            st["n"] += 1
            st["active"] = False
        if s.atext or s.kind == "tool":
            st["active"] = True
        turn_of[s.seq] = "%s#t%d" % (c, st["n"] + 1)
    for s in spans:
        if s.call:
            key, src = s.call, "call_id"
        else:
            call, root = climb(s)
            if call:
                key, src = call, "call_id_ancestor"
            elif s.fmt == "langsmith":
                key, src = (s.tid or root.sid), "langsmith_trace"
            elif s.tid and len(trace_calls.get(s.tid, ())) == 1:
                key, src = next(iter(trace_calls[s.tid])), "call_id_trace"
            elif s.seq in turn_of and s.conv:
                key, src = turn_of[s.seq], "conversation_turn"
            elif s.tid:
                key, src = s.tid, "trace_id"
            elif s.conv:
                key, src = s.conv, "conversation"
            else:
                key, src = root.sid or ("span%d" % s.seq), "span"
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

    user = (root.utext if root and root.utext else None) or next((s.utext for s in spans if s.utext), None)
    answer = root.atext if root and root.atext else None
    if not answer:
        for pool in (agents, llms, [s for s in spans if s.kind == "msg"]):
            cand = [s for s in pool if s.atext]
            if cand:
                answer = max(cand, key=lambda s: (s.end or s.start or 0, s.seq)).atext
                break

    calls, errors, cancelled = [], [], 0
    for t in tools:
        if not t.err:
            t.err = _result_error(t.result)
        failed = bool(t.err) and not t.cancelled
        if t.cancelled:
            cancelled += 1
        calls.append({"ts": iso(t.start), "name": t.tool or t.name, "duration_s": round(t.dur, 4) if t.dur is not None else None,
                      "args": clip(t.args or "", ARGS_MAX), "result": clip(t.result or "", RESULT_MAX),
                      "success": not t.err, "error": ("cancelled" if t.cancelled else t.err) if t.err else None})
    for s in spans:
        if s.err and not s.cancelled and s.err not in errors and len(errors) < 20:
            errors.append(clip(s.err, 200))

    models = []
    for s in llms + agents:
        if s.model and s.model not in models and (s.kind == "llm" or not llms):
            models.append(s.model)
    itok = sum(s.itok or 0 for s in llms)
    otok = sum(s.otok or 0 for s in llms)
    if not (itok or otok) and agents:
        a = max(agents, key=lambda s: (s.itok or 0) + (s.otok or 0))
        itok, otok = a.itok or 0, a.otok or 0
    cost, unpriced = None, []
    pool = llms if any((s.itok or s.otok or s.cost) for s in llms) else agents
    for s in pool:
        c = s.cost
        if c is None and (s.itok or s.otok):
            c = _price(opts.prices, s.model, s.itok or 0, s.otok or 0)
            if c is None and opts.prices and s.model and s.model not in unpriced:
                unpriced.append(s.model)
        if c is not None:
            cost = (cost or 0.0) + c
    conv = next((s.conv for s in ([root] if root else []) + spans if s.conv), None) or rid
    agent = next((s.agent for s in ([root] if root else []) + spans if s.agent), None)
    if not agent and root and root.name:
        agent = re.sub(r"^(invoke_agent|agent run)\s*", "", root.name, flags=re.I).strip() or ""
    env = next((s.env for s in spans if s.env), None)
    extra = {"run_id_source": src, "spans": len(spans)}
    if not root and spans[0].fmt != "flat":
        extra["no_root"] = True
    if cancelled:
        extra["cancelled"] = cancelled
    if unpriced:
        extra["unpriced_models"] = unpriced
    return {
        "run_id": rid, "conversation_id": conv, "agent": agent or "", "environment": env or "",
        "first_ts": iso(first), "last_ts": iso(last), "latency_s": round(latency, 4) if latency is not None else None,
        "user_message": clip(user or "", UTEXT_MAX), "final_answer": clip(answer, ATEXT_MAX) if answer else None,
        "ends_without_answer": not answer, "tool_calls": calls, "n_llm_calls": len(llms), "models": models,
        "input_tokens": itok, "output_tokens": otok, "cost_usd": round(cost, 6) if cost is not None else None,
        "n_tool_failures": sum(1 for c in calls if c["error"] and c["error"] != "cancelled"),
        "errors": errors, "extra": extra,
    }


def build_runs(spans, opts):
    """Return the list of run dossiers sorted by first_ts."""
    runs = [build_run(k, g[0], g[1], opts) for k, g in resolve_runs(spans).items()]
    runs = [r for r in runs if r["extra"]["spans"]]
    runs.sort(key=lambda r: (r["first_ts"] or "~", r["run_id"]))
    return runs


def run_all(paths, opts):
    spans, stats = load_spans(paths, opts)
    return build_runs(spans, opts), spans, stats


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
    if not runs:
        print("error: no runs could be built from %d records (formats seen: %s). Run `inspect` first; for a flat "
              "table pass --map." % (stats["records"], dict(stats["formats"])), file=sys.stderr)
        return 2
    os.makedirs(os.path.join(a.out, "normalized"), exist_ok=True)
    path = os.path.join(a.out, "normalized", "runs.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        for r in runs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    summ = {"records": stats["records"], "spans_kept": len(spans), "duplicates_dropped": stats["duplicates"],
            "noise_dropped": stats["noise_dropped"], "unknown_records": stats["unknown_records"],
            "unreadable": stats["unknown_records"] + stats["bad_lines"],
            "runs": len(runs), "conversations": len({r["conversation_id"] for r in runs}),
            "formats": dict(stats["formats"])}
    write_json(os.path.join(a.out, "normalized", "ingest_stats.json"), summ)
    print("normalized %d runs (%d conversations) from %d records -> %s" % (
        len(runs), summ["conversations"], stats["records"], path))
    print("  spans kept %d, duplicates %d, noise dropped %d, unreadable %d" % (
        len(spans), stats["duplicates"], stats["noise_dropped"], summ["unreadable"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
