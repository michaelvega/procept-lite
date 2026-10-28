"""schema: provider-shape lifting, heuristic mapping inference and the generic table/event/turn parser.

  lift(rec)            one raw record -> [("otel"|"langsmith"|"canon"|"raw", record)]  (provider shapes are converted
                       to a canonical flat record; unknown shapes stay "raw")
  infer(records)       dataset-level: scaffold a mapping for "raw" records by column names, then value shapes
  parse_flat(...)      canonical/mapped record -> Spans (turn rows, event rows, tool rows, tool lists, wide rows)

The mapping vocabulary is the flat mapping of templates/mapping.example.json plus: user_text, answer_text, tools +
tool_item, wide_tools, event + event_values, total_tokens, user_id, duration_unit, format/rename (span tables).
Values are column names or dotted paths (jsonPayload.text, labels.session_id).
"""
import hashlib
import json
import re
from collections import Counter

from common import (MISSING, Span, clip, jloads, otlp_attrs, parse_duration_s, to_epoch, to_float, to_int,
                    to_str, to_tokens, ok_dur, is_error_literal, real_error)
import cleaning as CL

# ---------------------------------------------------------------- names and paths


def norm(name):
    s = re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", str(name)).lower()
    return re.sub(r"[^a-z0-9]", "", s)


_UNIT_MAP = {"ms": "ms", "msec": "ms", "millis": "ms", "milliseconds": "ms", "s": "s", "sec": "s", "secs": "s",
             "seconds": "s", "us": "us", "usec": "us", "ns": "ns"}
_UNIT_RE = re.compile(r"(?:[\s_\-\.]|(?<=[a-z]))(ms|msec|millis|milliseconds|s|sec|secs|seconds|us|usec|ns)$|"
                      r"(?<=[a-z])(Ms|Msec|Millis|Sec|Secs|Seconds|Us|Ns)$")


def split_unit(name):
    """'Latency (ms)' / duration_ms / DurationMs / elapsed_seconds -> (normalised stem, unit or None)."""
    raw = str(name).strip()
    m = re.search(r"[\(\[]\s*(ms|msec|millis|milliseconds|s|sec|secs|seconds|us|ns)\s*[\)\]]\s*$", raw.lower())
    if m:
        return norm(raw), _UNIT_MAP.get(m.group(1))
    m = _UNIT_RE.search(raw)
    if m and len(raw) > len(m.group(0)) + 2:
        stem = norm(raw[:m.start()])
        if stem in V["duration"] or stem in V["timestamp"] or stem in ("duration", "latency", "elapsed", "time"):
            return stem, _UNIT_MAP.get((m.group(1) or m.group(2)).lower())
    return norm(raw), None


def name_unit(name):
    return split_unit(name)[1]


def get_path(rec, path):
    """rec[path] or a dotted traversal (JSON strings on the way are decoded). None when absent."""
    if not isinstance(path, str):
        return None
    if isinstance(rec, dict) and path in rec:
        return rec[path]
    parts = path.split(".")
    cur, i = rec, 0
    while i < len(parts):
        if isinstance(cur, str):
            cur = jloads(cur)
        if isinstance(cur, list) and parts[i].isdigit() and int(parts[i]) < len(cur):
            cur, i = cur[int(parts[i])], i + 1
            continue
        if not isinstance(cur, dict):
            return None
        for j in range(len(parts), i, -1):
            k = ".".join(parts[i:j])
            if k in cur:
                cur, i = cur[k], j
                break
        else:
            return None
    return cur


def first_of(d, keys):
    """First non-missing value of d[k] for k in keys (case/underscore-insensitive on miss)."""
    if not isinstance(d, dict):
        return None
    for k in keys:
        v = d.get(k)
        if v not in MISSING and v != []:
            return v
    idx = {norm(k): k for k in d}
    for k in keys:
        kk = idx.get(norm(k))
        if kk is not None and d[kk] not in MISSING and d[kk] != []:
            return d[kk]
    return None


def shash(*parts):
    h = hashlib.sha1("|".join(to_str(p) for p in parts).encode("utf-8", "replace")).hexdigest()
    return h[:12]


# ---------------------------------------------------------------- vocabularies

V = {
    "run_id": ["runid", "run", "requestid", "reqid", "invocationid", "turnid", "executionid", "execid", "callid",
               "agentcallid", "interactionid", "exchangeid", "qaid", "caseid", "rid", "messageid", "chatid", "ticket",
               "ticketid", "ticketno", "ticketnumber", "incident", "incidentid", "taskid", "jobid", "txnid",
               "transactionid", "reference", "ref", "requestuuid", "uuid4"],
    "trace_id": ["traceid", "trace", "operationid", "correlationid", "otraceid", "traceidhex", "grp", "group"],
    "conversation_id": ["conversationid", "conversation", "convid", "conv", "sessionid", "session", "threadid",
                        "thread", "dialogid", "dialogueid", "sid", "usersessionid", "sessionkey", "chat"],
    "parent_id": ["parentspanid", "parentid", "parent", "parentrunid", "operationparentid", "parenteventid",
                  "parentuuid"],
    "span_id": ["spanid", "eventid", "insertid", "segmentid", "rowid", "uuid", "id", "recordid", "msgid"],
    "timestamp": ["timestamp", "time", "ts", "tsx", "datetime", "date", "createdat", "created", "createtime",
                  "started", "startedat", "starttime", "start", "began", "begin", "when", "eventtime",
                  "timegenerated", "utc", "occurredat", "loggedat", "logtime", "t", "startedtimestamp",
                  "starttimestamp", "createdtime", "creationtime", "at"],
    "end_timestamp": ["endtime", "endedat", "finishedat", "finished", "end", "endtimestamp", "stoptime",
                      "completedat", "completed", "endts", "endedtimestamp"],
    "duration": ["duration", "latency", "elapsed", "took", "responsetime", "timetaken", "runtime", "elapsedtime",
                 "durationsec", "totallatency"],
    "role": ["role", "speaker", "sender", "author", "from", "participant", "party", "messagerole", "actor"],
    "event": ["event", "eventtype", "eventname", "kind", "type", "action", "step", "phase", "stage", "methodname",
              "operation", "category", "messagetype", "evt", "steptype", "itemtype"],
    "content": ["content", "text", "message", "messagetext", "msg", "body", "utterance", "value", "payload",
                "line", "contents", "messagecontent", "data"],
    "user_text": ["usermessage", "userinput", "userquery", "userquestion", "usertext", "userprompt", "question",
                  "ask", "asked", "prompt", "query", "humaninput", "human", "inputtext", "input", "request",
                  "requesttext", "q", "instruction", "customermessage", "usermsg", "user", "inputmessage",
                  "inputmessages", "utterance"],
    "answer_text": ["finalanswer", "finalresponse", "answer", "reply", "agentresponse", "assistantresponse",
                    "airesponse", "modelresponse", "botresponse", "assistant", "bot", "output", "outputtext",
                    "completion", "response", "a", "generated", "responsetext", "agentanswer", "agentreply",
                    "outputmessage", "outputmessages", "result", "assistantmsg", "assistantmessage", "assistanttext",
                    "botmsg", "botmessage", "agentmsg", "agentmessage", "aimsg", "aimessage", "assistantreply",
                    "assistantcontent", "systemresponse", "replytext", "responsemessage", "answertext"],
    "tool_name": ["toolname", "tool", "function", "functionname", "fn", "toolcall", "toolused", "tooltype",
                  "actionname", "toolid"],
    "tool_args": ["toolargs", "toolarguments", "toolinput", "arguments", "args", "params", "parameters",
                  "toolparams", "functionargs", "toolparameters", "toolin"],
    "tool_result": ["toolresult", "tooloutput", "toolresponse", "observation", "toolobservation",
                    "functionresult", "result", "out", "output", "toolreturn", "toolout", "toolresultcontent"],
    "error": ["error", "err", "errormessage", "errormsg", "exception", "iserror", "failed", "failure", "errortype",
              "errordetail", "exceptionmessage", "toolerror", "errors"],
    "status": ["status", "statuscode", "state", "outcome", "success", "ok", "severity", "level", "httpstatus",
               "resultstatus", "loglevel", "levelname"],
    "input_tokens": ["inputtokens", "prompttokens", "inputtokencount", "prompttokencount", "tokensin",
                     "promptnumtokens", "intokens", "inputtoken", "prompttoken", "tokenin", "inputtokenscount",
                     "usagepromptokens"],
    "output_tokens": ["outputtokens", "completiontokens", "outputtokencount", "candidatestokencount", "tokensout",
                      "outtokens", "completiontoken", "outputtoken", "responsetokens", "tokenout",
                      "candidatestokens"],
    "total_tokens": ["totaltokens", "tokens", "tokencount", "totaltokencount", "tokenusage", "numtokens",
                     "ntokens"],
    "model": ["model", "modelid", "modelname", "llm", "engine", "deployment", "modelversion", "llmmodel"],
    "cost_usd": ["cost", "costusd", "usd", "totalcost", "price", "spend", "costdollars", "estimatedcost", "costdollar"],
    "agent": ["agent", "agentname", "app", "service", "servicename", "appname", "approlename", "botname"],
    "environment": ["environment", "env", "deploymentenvironment", "stage"],
    "user_id": ["userid", "uid", "username", "user", "customer", "customerid", "who", "email", "useremail",
                "account", "accountid", "userid"],
    "tools": ["toolcalls", "steps", "tools", "toolcallsmade", "actions", "functioncalls", "tooluses", "calls",
              "toolinvocations", "toolsused", "intermediatesteps", "subsegments", "actionsused"],
}
ROLE_VALUES = {"user", "human", "assistant", "ai", "bot", "model", "system", "tool", "function", "agent",
               "customer", "client", "gpt", "developer", "chatbot", "operator", "support", "tool_call"}

EVENT_VOCAB = {
    "user": {"usermessage", "user", "human", "userinput", "question", "request", "prompt", "input", "query",
             "messagein", "userquery", "utterance", "ask", "usermsg", "humanmessage", "usertext", "inputmessage"},
    "final": {"finalanswer", "answer", "finalresponse", "final", "reply", "agentresponse", "agentreply",
              "modelresponse", "finish", "end", "done", "agentfinish", "response", "assistantfinal"},
    "assistant": {"assistant", "assistantmessage", "ai", "bot", "output", "completion", "aimessage", "model",
                  "agent", "message", "botmessage", "assistantreply"},
    "tool_call": {"toolcall", "tooluse", "functioncall", "toolstart", "toolinvocation", "action", "tool",
                  "invoketool", "call", "toolrequest", "toolinput", "calltool", "toolcalled", "actionstart",
                  "toolexecution", "executetool"},
    "tool_result": {"toolresult", "toolresponse", "toolend", "functionresponse", "observation", "tooloutput",
                    "result", "toolreturn", "functionresult", "toolfinish", "toolfinished", "actionresult",
                    "toolcompleted", "toolerror", "actionend"},
    "llm": {"llmcall", "llm", "modelcall", "chat", "generate", "llmresponse", "llmstart", "llmend", "modelrequest",
            "completioncall"},
}


def classify_event(v):
    """Event/kind/type cell -> user|final|assistant|tool_call|tool_result|llm|None."""
    n = norm(v)
    if not n:
        return None
    for k, vs in EVENT_VOCAB.items():
        if n in vs:
            return k
    if "tool" in n or "function" in n or "action" in n:
        if any(x in n for x in ("result", "response", "output", "end", "return", "observ", "finish", "error")):
            return "tool_result"
        return "tool_call"
    if "user" in n or "human" in n or "question" in n:
        return "user"
    if "final" in n or "answer" in n or "reply" in n:
        return "final"
    if "assistant" in n or "response" in n or n.startswith("ai"):
        return "assistant"
    if "llm" in n or "model" in n:
        return "llm"
    return None


def norm_role(v):
    r = str(v or "").strip().lower()
    return {"human": "user", "customer": "user", "client": "user", "ai": "assistant", "model": "assistant",
            "bot": "assistant", "gpt": "assistant", "chatbot": "assistant", "agent": "assistant",
            "function": "tool", "operator": "assistant", "support": "assistant", "developer": "system",
            "humanmessage": "user", "aimessage": "assistant", "toolmessage": "tool"}.get(r, r)


# ---------------------------------------------------------------- text-shaped helpers

def text_of(v, role_hint=None, _d=0):
    """Best-effort plain text of a message-ish value (string, content blocks, parts, dict)."""
    if _d > 12:
        return ""
    v = jloads(v)
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, (int, float, bool)):
        return str(v)
    if isinstance(v, list):
        out = []
        for p in v:
            t = text_of(p, None, _d + 1)
            if t:
                out.append(t)
        return "\n".join(out)
    if isinstance(v, dict):
        ty = str(v.get("type") or "").lower()
        if ty in ("tool_use", "tool_call", "tool_result", "image", "image_url", "thinking", "reasoning") or \
                any(k in v for k in ("toolUse", "toolResult", "function_call", "functionCall", "function_response",
                                     "functionResponse")):
            return ""
        for k in ("text", "content", "parts", "message", "output", "value", "answer"):
            if k in v and v[k] not in MISSING:
                return text_of(v[k], None, _d + 1)
        return ""
    return ""


_NAMEKEYS = ("name", "tool", "tool_name", "toolName", "fn", "function", "function_name", "action", "tool_id")
_ARGKEYS = ("args", "arguments", "input", "inputs", "params", "parameters", "tool_input", "tool_args",
            "toolInput", "action_input", "request")
_RESKEYS = ("result", "output", "out", "response", "observation", "tool_output", "tool_result", "toolOutput",
            "return", "returns", "answer", "content", "text", "body")
_ERRKEYS = ("error", "err", "exception", "is_error", "isError", "failed", "error_message", "errorMessage")


def tool_item(it):
    """A tool-call-ish dict -> {name,args,result,error,start,end,duration,id} (or None)."""
    it = jloads(it)
    if not isinstance(it, dict):
        return None
    fn = it.get("function") if isinstance(it.get("function"), dict) else None
    if fn is not None:
        name, args = fn.get("name"), fn.get("arguments")
    else:
        name = first_of(it, _NAMEKEYS)
        if isinstance(name, dict):
            name = name.get("name")
        args = first_of(it, _ARGKEYS)
    if name in MISSING:
        return None
    res = first_of(it, _RESKEYS)
    err = None
    for k in _ERRKEYS:
        e = it.get(k)
        if e not in MISSING and real_error(e):
            err = "error" if e is True or str(e).lower() == "true" else to_str(e)
            break
    st = str(it.get("status") or "").lower()
    if not err and st in ("error", "failed", "failure", "fail", "exception", "timeout"):
        err = st
    if not err:
        for k in ("status", "statusCode", "status_code", "http_status", "httpStatus"):
            v = it.get(k)
            if v not in MISSING and not isinstance(v, (dict, list, bool)) and 400 <= to_int(v) <= 599:
                err = "HTTP %d" % to_int(v)
                break
    if not err:
        for k in ("success", "ok", "succeeded"):
            if it.get(k) is False or str(it.get(k)).strip().lower() == "false":
                err = "%s=false" % k
                break
    return {"name": to_str(name), "args": args, "result": res, "error": err, "id": it.get("id") or it.get("call_id"),
            "start": it.get("start") or it.get("start_time") or it.get("timestamp") or it.get("ts"),
            "end": it.get("end") or it.get("end_time"),
            "duration": it.get("duration_s") or it.get("duration") or it.get("latency")}


# ---------------------------------------------------------------- chat-message lifting

def _msg_items(m, idx):
    """One chat message dict -> ordered items: ('text',role,str,idx) ('call',id,name,args) ('result',id,name,text,err)."""
    items = []
    role = norm_role(m.get("role") or (m.get("author") or {}).get("role") if isinstance(m.get("author"), dict)
                     else m.get("role") or m.get("author") or m.get("type") or "")
    c = m.get("content")
    if c is None:
        c = m.get("parts")
    if c is None and isinstance(m.get("content"), dict):
        c = m["content"]
    if isinstance(c, dict) and "parts" in c:
        c = c["parts"]
    if isinstance(c, str):
        c = jloads(c) if c.strip()[:1] in ("[",) else c
    blocks = c if isinstance(c, list) else ([c] if isinstance(c, dict) else [])
    text_parts = []
    if isinstance(c, str):
        text_parts.append(c)
    elif isinstance(c, (int, float)):
        text_parts.append(to_str(c))
    for b in blocks:
        if isinstance(b, str):
            text_parts.append(b)
            continue
        if isinstance(b, (int, float)) and not isinstance(b, bool):
            text_parts.append(to_str(b))
            continue
        if not isinstance(b, dict):
            continue
        ty = str(b.get("type") or "").lower()
        tu = b.get("toolUse") or b.get("tool_use") if not ty == "tool_use" else b
        tr = b.get("toolResult") or b.get("tool_result") if ty != "tool_result" else b
        fc = b.get("function_call") or b.get("functionCall")
        fr = b.get("function_response") or b.get("functionResponse")
        if isinstance(tu, dict) and (ty == "tool_use" or "name" in tu):
            items.append(("call", tu.get("id") or tu.get("toolUseId"), tu.get("name"),
                          tu.get("input") if tu.get("input") is not None else tu.get("arguments"), idx))
        elif ty in ("tool_call", "function_call") and (b.get("name") or isinstance(b.get("function"), dict)):
            f = b.get("function") if isinstance(b.get("function"), dict) else b
            items.append(("call", b.get("id"), f.get("name"), f.get("arguments") or f.get("args"), idx))
        elif isinstance(fc, dict):
            items.append(("call", fc.get("id"), fc.get("name"), fc.get("args") or fc.get("arguments"), idx))
        elif isinstance(tr, dict) and (ty == "tool_result" or "toolUseId" in tr or "tool_use_id" in tr):
            err = tr.get("is_error") or tr.get("isError") or str(tr.get("status") or "").lower() == "error"
            items.append(("result", tr.get("tool_use_id") or tr.get("toolUseId"), None,
                          text_of(tr.get("content")), bool(err), idx))
        elif isinstance(fr, dict):
            r = fr.get("response")
            items.append(("result", fr.get("id"), fr.get("name"), r if isinstance(r, str) else to_str(r), False, idx))
        else:
            t = text_of(b)
            if not t and not any(k in b for k in ("type", "text", "content", "parts")):
                t = to_str(b)  # structured content with no text field: keep it as JSON text
            if t:
                text_parts.append(t)
    for tc in (m.get("tool_calls") or (m.get("additional_kwargs") or {}).get("tool_calls") or []):
        if isinstance(tc, dict):
            f = tc.get("function") if isinstance(tc.get("function"), dict) else tc
            items.append(("call", tc.get("id"), f.get("name"), f.get("arguments") if f.get("arguments") is not None
                          else f.get("args"), idx))
    fc = m.get("function_call") or (m.get("additional_kwargs") or {}).get("function_call")
    if isinstance(fc, dict) and fc.get("name"):
        items.append(("call", None, fc.get("name"), fc.get("arguments"), idx))
    if role in ("tool", "function"):
        t = "\n".join(text_parts)
        items.insert(0, ("result", m.get("tool_call_id") or m.get("tool_use_id"), m.get("name"), t,
                         bool(m.get("is_error")) or str(m.get("status") or "").lower() == "error", idx))
        return items
    t = "\n".join(x for x in text_parts if x and x.strip()).strip()
    if t:
        items.insert(0, ("text", role or "", t, idx))
    return items


_LC_TYPES = {"human", "ai", "tool", "system", "function", "humanmessage", "aimessage", "toolmessage",
             "systemmessage", "assistant", "user", "chat", "generic"}


def _looks_msg(d):
    if not isinstance(d, dict):
        return False
    has_role = "role" in d or (isinstance(d.get("author"), dict) and "role" in d["author"]) or \
        (isinstance(d.get("type"), str) and d["type"].lower().replace("_", "") in _LC_TYPES)
    return has_role and any(k in d for k in ("content", "parts", "tool_calls", "function_call", "text"))


def find_messages(rec, depth=0, path=""):
    """DFS for chat-message lists / single messages inside a record. -> ordered list of message dicts."""
    out = []
    if depth > 6:
        return out
    if isinstance(rec, str):
        s = rec.strip()
        if (s[:1] in ("{", "[") or s[:2] in ('"[', '"{')) and len(s) < 3000000:
            d = jloads(s)
            if not isinstance(d, str):
                return find_messages(d, depth + 1, path)
        return out
    if isinstance(rec, list):
        msgs = [x for x in rec if _looks_msg(x)]
        if msgs and len(msgs) >= max(1, len(rec) // 2):
            return msgs
        for x in rec[:50]:
            out += find_messages(x, depth + 1, path)
        return out
    if isinstance(rec, dict):
        if _looks_msg(rec):
            return [rec]
        if isinstance(rec.get("mapping"), dict):  # ChatGPT export
            nodes = []
            for k, n in rec["mapping"].items():
                msg = n.get("message") if isinstance(n, dict) else None
                if isinstance(msg, dict):
                    au = msg.get("author") or {}
                    c = msg.get("content")
                    parts = c.get("parts") if isinstance(c, dict) else c
                    nodes.append((msg.get("create_time") or 0, {"role": au.get("role"), "content": text_of(parts)}))
            nodes.sort(key=lambda x: x[0] or 0)
            return [m for _, m in nodes]
        for k, v in rec.items():
            if isinstance(v, (dict, list)) or (isinstance(v, str) and (v[:1] in ("{", "[") or v[:2] in ('"[', '"{'))):
                out += find_messages(v, depth + 1, k)
    return out


def _find_usage(rec):
    it = ot = None
    model = None

    def walk(d, depth=0):
        nonlocal it, ot, model
        if depth > 6:
            return
        d = jloads(d)
        if isinstance(d, dict):
            for k, v in d.items():
                n = norm(k)
                if n in V["input_tokens"] or n in ("inputtokens", "prompttokens") and not isinstance(v, (dict, list)):
                    it = it if it else to_tokens(v) or it
                elif n in V["output_tokens"] and not isinstance(v, (dict, list)):
                    ot = ot if ot else to_tokens(v) or ot
                elif n in ("model", "modelid", "modelname") and isinstance(v, str) and not model:
                    model = v
                elif isinstance(v, (dict, list)) or (isinstance(v, str) and v[:1] in ("{", "[")):
                    walk(v, depth + 1)
        elif isinstance(d, list):
            for x in d[:30]:
                walk(x, depth + 1)
    walk(rec)
    return it, ot, model


def lift_chat(rec):
    """A record that holds a chat transcript (OpenAI / Anthropic / Bedrock / Gemini / ChatGPT export) -> canon turn."""
    msgs = find_messages(rec)
    if not msgs:
        return None
    items = []
    for i, m in enumerate(msgs):
        items += _msg_items(m, i)
    user = None
    ans = None
    ans_idx = -1
    calls, by_id = [], {}
    last_call_msg = -1
    for it in items:
        if it[0] == "text":
            _, role, text, idx = it
            if role == "user":
                user = text
            elif role in ("assistant", ""):
                ans, ans_idx = text, idx
        elif it[0] == "call":
            _, cid, name, args, idx = it
            c = {"name": to_str(name) or "tool", "args": args, "result": None, "error": None, "id": cid}
            calls.append(c)
            if cid:
                by_id[cid] = c
            last_call_msg = idx
        elif it[0] == "result":
            _, cid, name, text, err, idx = it
            c = by_id.get(cid) if cid else None
            if c is None:
                c = next((x for x in calls if x["result"] is None and (not name or x["name"] == name)), None)
            if c is None:
                c = {"name": to_str(name) or "tool", "args": None, "result": None, "error": None, "id": cid}
                calls.append(c)
            c["result"] = text
            if err:
                c["error"] = "error"
            last_call_msg = idx if last_call_msg < 0 else last_call_msg
    if ans is not None and last_call_msg > ans_idx and any(c["result"] is not None for c in calls):
        ans = None
    it_, ot_, model = _find_usage({k: v for k, v in rec.items() if norm(k) != "messages"}) if isinstance(rec, dict) else (None, None, None)
    out = {"user_text": user, "answer_text": ans, "tools": calls or None, "input_tokens": it_, "output_tokens": ot_,
           "model": model}
    if isinstance(rec, dict):
        out.update(_envelope(rec))
    return out


_TS_KEYS = ("timestamp", "@timestamp", "created_at", "createdAt", "created", "time", "ts", "eventTime", "event_time",
            "start_time", "startTime", "date", "datetime", "create_time", "timeGenerated", "TimeGenerated")
_RUN_KEYS = ("run_id", "runId", "requestId", "request_id", "custom_id", "invocation_id", "invocationId", "id", "chat_id",
             "turn_id", "execution_id", "message_id", "call_id")
_CONV_KEYS = ("conversation_id", "conversationId", "session_id", "sessionId", "thread_id", "threadId", "session",
              "conversation", "sid", "chat_id", "dialog_id")
_USER_KEYS = ("user_id", "userId", "user", "uid", "customer_id", "account_id", "principal", "identity")


def _envelope(rec):
    ts = first_of(rec, _TS_KEYS)
    out = {}
    if ts is not None and not isinstance(ts, (dict, list)):
        out["timestamp"] = ts
    r = first_of(rec, _RUN_KEYS)
    if r is not None and not isinstance(r, (dict, list)):
        out["run_id"] = r
    c = first_of(rec, _CONV_KEYS)
    if c is not None and not isinstance(c, (dict, list)):
        out["conversation_id"] = c
    u = first_of(rec, _USER_KEYS)
    if isinstance(u, (str, int)) and not isinstance(u, bool):
        out["user_id"] = u
    return out


# ---------------------------------------------------------------- tool calls hidden in message structures

def msg_tool_calls(*objs):
    """Tool calls (with results when present) found anywhere inside chat-message structures: assistant tool_calls /
    function_call / tool_use / tool_call parts and the matching tool-role / tool_result / tool_call_response messages.
    Used only when a trace has no tool spans. -> [{name,args,result,error,id}]"""
    calls, by_id = [], {}

    def add(cid, name, args):
        if name in MISSING or name is None or isinstance(name, (dict, list)):
            return None
        key = cid if cid not in MISSING else (to_str(name), to_str(args))
        if key in by_id:
            return by_id[key]
        c = {"name": to_str(name), "args": args, "result": None, "error": None, "id": cid}
        by_id[key] = c
        calls.append(c)
        return c

    def attach(cid, name, text, err):
        c = by_id.get(cid) if cid not in MISSING else None
        if c is None:
            c = next((x for x in calls if x["result"] is None and (not name or x["name"] == name)), None)
        if c is None:
            c = add(cid, name or "tool", None)
        if c is not None:
            if text not in (None, ""):
                c["result"] = text
            if err:
                c["error"] = "error"

    def walk(o, d=0):
        if d > 9:
            return
        if isinstance(o, str):
            s = o.strip()
            if s[:1] not in ("{", "[") or len(s) > 2000000:
                return
            o = jloads(s)
            if isinstance(o, str):
                return
        if isinstance(o, list):
            for x in o[:300]:
                walk(x, d + 1)
        elif isinstance(o, dict):
            tcs = o.get("tool_calls")
            if isinstance(tcs, list):
                for tc in tcs:
                    if isinstance(tc, dict):
                        f = tc.get("function") if isinstance(tc.get("function"), dict) else tc
                        add(tc.get("id"), f.get("name"), f.get("arguments") if f.get("arguments") is not None
                            else f.get("args"))
            fc = o.get("function_call")
            if isinstance(fc, dict):
                add(None, fc.get("name"), fc.get("arguments"))
            ty = str(o.get("type") or "").lower().replace("-", "_")
            if ty in ("tool_use", "tool_call", "function_call") and o.get("name"):
                add(o.get("id"), o.get("name"), o.get("input") if o.get("input") is not None else
                    (o.get("arguments") if o.get("arguments") is not None else o.get("args")))
            elif ty in ("tool_result", "tool_call_response", "tool_response"):
                r = o.get("content") if o.get("content") is not None else o.get("result", o.get("response"))
                attach(o.get("tool_use_id") or o.get("id") or o.get("tool_call_id"), o.get("name"),
                       r if isinstance(r, str) else to_str(r), bool(o.get("is_error") or o.get("isError")))
            role = norm_role(o.get("role") or "")
            if role == "tool" or ty in ("tool", "toolmessage"):
                attach(o.get("tool_call_id") or o.get("tool_use_id"), o.get("name"), text_of(o.get("content")),
                       bool(o.get("is_error")) or str(o.get("status") or "").lower() == "error")
            for k, v in o.items():
                if isinstance(v, (dict, list)) or (isinstance(v, str) and v[:1] in ("{", "[")):
                    walk(v, d + 1)

    for ob in objs:
        if ob not in MISSING:
            walk(ob)
    return calls


# ---------------------------------------------------------------- LangSmith shapes

_FEEDBACK_KEYS = {"id", "key", "score", "value", "run_id", "comment", "created_at", "modified_at", "feedback_source",
                  "feedback_source_type", "trace_id", "correction", "session_id", "extra", "feedback_config",
                  "start_time", "end_time", "comparative_experiment_id", "source_info", "user_id"}


def non_run_row(rec):
    """LangSmith feedback / dataset-example rows that ride along with runs."""
    if "run_type" in rec or "parent_run_id" in rec or "dotted_order" in rec:
        return False
    if "key" in rec and ("score" in rec or "value" in rec) and set(rec) <= _FEEDBACK_KEYS:
        return True
    if ("example_id" in rec or "dataset_id" in rec) and not ({"start_time", "end_time"} & set(rec)):
        return True
    return False


def lift_langsmith(rec, parent=None, depth=0):
    """A LangSmith run -> [("langsmith", run)] with nested child_runs flattened and ids from dotted_order."""
    rec = dict(rec)
    kids = rec.pop("child_runs", None)
    do = rec.get("dotted_order")
    if isinstance(do, str) and do and (rec.get("trace_id") in MISSING or rec.get("parent_run_id") in MISSING):
        segs = [x.split("Z", 1)[1] if "Z" in x else x for x in do.split(".")]
        segs = [x for x in segs if x]
        if segs:
            used = False
            if rec.get("trace_id") in MISSING:
                rec["trace_id"] = segs[0]
                used = True
            if rec.get("parent_run_id") in MISSING and len(segs) > 1:
                rec["parent_run_id"] = segs[-2]
                used = True
            if rec.get("id") in MISSING:
                rec["id"] = segs[-1]
            if used:
                CL.note("hierarchy_from_dotted_order")
    if parent is not None:
        if rec.get("trace_id") in MISSING:
            rec["trace_id"] = parent.get("trace_id") or parent.get("id")
        if rec.get("parent_run_id") in MISSING:
            rec["parent_run_id"] = parent.get("id")
    out = [("langsmith", rec)]
    kids = jloads(kids)
    if isinstance(kids, list) and depth < 12:
        for k in kids:
            if isinstance(k, dict):
                CL.note("children_flattened")
                out.extend(lift_langsmith(k, rec, depth + 1))
    return out


# ---------------------------------------------------------------- provider lifts

def _sev_error(sev):
    return str(sev or "").upper() in ("ERROR", "CRITICAL", "ALERT", "EMERGENCY")


def _event_rec(base, event, **kw):
    r = dict(base)
    r["event"] = event
    r.update({k: v for k, v in kw.items() if v is not None})
    return r


def lift_gcp(rec):
    tr = rec.get("trace")
    tid = to_str(tr).rsplit("/", 1)[-1] if tr not in MISSING else None
    labels = jloads(rec.get("labels"))
    labels = labels if isinstance(labels, dict) else {}
    conv = None
    for k, v in labels.items():
        if norm(k) in V["conversation_id"] and v not in MISSING:
            conv = v
            break
    base = {"trace_id": tid, "conversation_id": conv,
            "timestamp": rec.get("timestamp") or rec.get("receiveTimestamp"), "span_id": rec.get("insertId")}
    sev_err = "severity ERROR" if _sev_error(rec.get("severity")) else None
    hr = jloads(rec.get("httpRequest"))
    if isinstance(hr, dict) and 400 <= to_int(hr.get("status")) <= 599:
        sev_err = sev_err or "HTTP %d" % to_int(hr.get("status"))
    jp, pp, tp = jloads(rec.get("jsonPayload")), jloads(rec.get("protoPayload")), rec.get("textPayload")
    if isinstance(tp, str) and not isinstance(jp, dict):
        m = re.match(r"^\s*([A-Za-z_][\w.\-]*)\s+(\{.*\})\s*$", tp, re.S)
        if m:
            d = jloads(m.group(2))
            if isinstance(d, dict):
                jp = dict(d)
                jp.setdefault("event", m.group(1))
        else:
            d = jloads(tp)
            if isinstance(d, dict):
                jp = d
    if isinstance(jp, dict):
        ev = first_of(jp, ("event", "event_type", "type", "kind", "action", "name"))
        if ev is not None and classify_event(ev):
            kind = classify_event(ev)
            usage = jloads(jp.get("usage") or jp.get("usage_metadata") or jp.get("usageMetadata")) or {}
            it_, ot_, mdl = _find_usage(usage) if usage else (None, None, None)
            return [("canon", _event_rec(
                base, kind, content=first_of(jp, ("text", "content", "message", "body", "output", "answer")),
                tool_name=first_of(jp, ("tool", "tool_name", "toolName", "function", "name")) if "tool" in kind else None,
                tool_args=first_of(jp, ("args", "arguments", "input", "params")),
                tool_result=first_of(jp, ("result", "output", "response", "observation")),
                error=first_of(jp, ("error", "exception", "error_message")) or sev_err,
                input_tokens=it_ or first_of(jp, ("input_tokens", "prompt_tokens")),
                output_tokens=ot_ or first_of(jp, ("output_tokens", "completion_tokens")),
                model=mdl or first_of(jp, ("model", "model_name"))))]
        merged = dict(base)
        for k, v in jp.items():
            merged[k] = v
        if sev_err:
            merged["error"] = merged.get("error") or sev_err
        return [("raw", merged)]
    if isinstance(pp, dict):
        ev = pp.get("methodName") or pp.get("method") or ""
        kind = classify_event(ev)
        req, resp = jloads(pp.get("request")), jloads(pp.get("response"))
        req = req if isinstance(req, dict) else {}
        resp = resp if isinstance(resp, dict) else {}
        if kind:
            tool = req.get("toolName") or resp.get("toolName")
            return [("canon", _event_rec(
                base, kind, tool_name=tool if "tool" in kind else None,
                content=req.get("content") if kind in ("user", "final", "assistant") else None,
                tool_args=req.get("content") if kind == "tool_call" else None,
                tool_result=resp.get("content") if kind == "tool_result" else None, error=sev_err))]
        merged = dict(base)
        merged.update({"method": ev, "request": req, "response": resp})
        return [("raw", merged)]
    if isinstance(tp, str) and tp.strip():
        r = dict(base)
        r["text"] = tp
        return [("raw", r)]
    return [("raw", dict(base))]


def _body_text(body):
    """Text of a model request/response body in any of the provider shapes Bedrock passes through."""
    body = jloads(body)
    txt = text_of(body)
    if txt:
        return txt
    if isinstance(body, dict):
        for k in ("outputText", "generation", "completion", "generated_text", "text", "prompt", "inputText"):
            v = body.get(k)
            if isinstance(v, str) and v.strip():
                return v
        for path in (("output", "message", "content"), ("choices", 0, "message", "content"),
                     ("results", 0, "outputText"), ("generations", 0, "text"), ("messages", -1, "content")):
            cur = body
            for p in path:
                try:
                    cur = cur[p]
                except (KeyError, IndexError, TypeError):
                    cur = None
                    break
            r = text_of(cur) if cur is not None else ""
            if r:
                return r
    return ""


def lift_bedrock_invocation(rec):
    inp, out = jloads(rec.get("input")), jloads(rec.get("output"))
    inp = inp if isinstance(inp, dict) else {}
    out = out if isinstance(out, dict) else {}
    t = lift_chat({"input": inp.get("inputBodyJson"), "output": out.get("outputBodyJson")})
    if t is None:
        t = {"user_text": None, "answer_text": None}
        txt = text_of(out.get("outputBodyJson"))
        t["answer_text"] = txt or None
        t["user_text"] = text_of(inp.get("inputBodyJson")) or None
    t = dict(t)
    if not t.get("answer_text"):
        t["answer_text"] = _body_text(out.get("outputBodyJson")) or None
    if not t.get("user_text"):
        t["user_text"] = _body_text(inp.get("inputBodyJson")) or None
    t["run_id"] = rec.get("requestId") or rec.get("request_id")
    t["timestamp"] = rec.get("timestamp")
    t["model"] = rec.get("modelId") or t.get("model")
    it_ = inp.get("inputTokenCount")
    ot_ = out.get("outputTokenCount")
    t["input_tokens"] = it_ if it_ not in MISSING else t.get("input_tokens")
    t["output_tokens"] = ot_ if ot_ not in MISSING else t.get("output_tokens")
    for k in ("sessionId", "conversationId", "session_id"):
        if rec.get(k):
            t["conversation_id"] = rec[k]
    ident = rec.get("identity")
    if isinstance(ident, dict) and ident.get("arn"):
        t["user_id"] = ident["arn"]
    err = rec.get("errorCode") or rec.get("error")
    if err:
        t["error"] = to_str(err)
    return [("canon", t)]


def lift_bedrock_agent(rec):
    tr = jloads(rec.get("trace"))
    if not isinstance(tr, dict):
        return None
    base = {"conversation_id": rec.get("sessionId") or rec.get("session_id"), "timestamp": rec.get("eventTime"),
            "trace_id": None, "span_id": None, "run_id": None}
    evs = []
    if rec.get("inputText"):
        evs.append(_event_rec(base, "user", content=rec["inputText"]))
    for _, part in tr.items():
        if not isinstance(part, dict):
            continue
        ii = part.get("invocationInput")
        if isinstance(ii, dict):
            ag = ii.get("actionGroupInvocationInput") or {}
            kb = ii.get("knowledgeBaseLookupInput") or {}
            name = ag.get("function") or ag.get("actionGroupName") or ag.get("apiPath") or \
                ("knowledge_base" if kb else ii.get("invocationType") or "tool")
            params = ag.get("parameters")
            args = {p.get("name"): p.get("value") for p in params if isinstance(p, dict)} \
                if isinstance(params, list) else (kb.get("text") or ag.get("requestBody"))
            evs.append(_event_rec(base, "tool_call", tool_name=name, tool_args=args))
        ob = part.get("observation")
        if isinstance(ob, dict):
            ty = str(ob.get("type") or "").upper()
            if ty == "FINISH" or isinstance(ob.get("finalResponse"), dict):
                evs.append(_event_rec(base, "final", content=(ob.get("finalResponse") or {}).get("text")))
            else:
                ag = ob.get("actionGroupInvocationOutput") or {}
                kb = ob.get("knowledgeBaseLookupOutput")
                res = ag.get("text") if ag else to_str(kb) if kb else ob.get("repromptResponse", {}).get("text") \
                    if isinstance(ob.get("repromptResponse"), dict) else None
                evs.append(_event_rec(base, "tool_result", tool_name=None, tool_result=res,
                                      error="agent failure" if ty == "ASK_USER_FAILURE" else None))
        mo = part.get("modelInvocationOutput")
        if isinstance(mo, dict):
            usage = ((mo.get("metadata") or {}).get("usage")) or {}
            if usage:
                evs.append(_event_rec(base, "llm", input_tokens=usage.get("inputTokens"),
                                      output_tokens=usage.get("outputTokens")))
    if not evs:
        return []
    for i, e in enumerate(evs):
        e["seq_in_record"] = i
    return [("canon", e) for e in evs]


def _xray_flatten(seg, out):
    for sub in seg.get("subsegments") or []:
        if isinstance(sub, dict):
            out.append(sub)
            _xray_flatten(sub, out)


def lift_xray(rec):
    ann = first_of(rec, ("annotations",)) or {}
    meta = first_of(rec, ("metadata",)) or {}
    ann = ann if isinstance(ann, dict) else {}
    meta = meta if isinstance(meta, dict) else {}
    pool = dict(meta)
    pool.update(ann)
    user = first_of(pool, V["user_text"] + ["user_message", "userMessage"])
    ans = first_of(pool, V["answer_text"] + ["final_answer", "finalAnswer"])
    conv = first_of(pool, V["conversation_id"] + ["session"])
    subs = []
    _xray_flatten(rec, subs)
    tools = []
    for s in subs:
        sm = s.get("metadata") if isinstance(s.get("metadata"), dict) else {}
        sa = s.get("annotations") if isinstance(s.get("annotations"), dict) else {}
        d = dict(sm)
        d.update(sa)
        http = (s.get("http") or {}).get("response") or {} if isinstance(s.get("http"), dict) else {}
        err = None
        if s.get("error") or s.get("fault") or s.get("throttle"):
            cause = s.get("cause")
            err = "error"
            if isinstance(cause, dict) and cause.get("exceptions"):
                try:
                    err = to_str(cause["exceptions"][0].get("message") or "error")
                except Exception:
                    err = "error"
        elif isinstance(http.get("status"), int) and http["status"] >= 400:
            err = "HTTP %d" % http["status"]
        tools.append({"name": to_str(s.get("name")), "args": first_of(d, _ARGKEYS), "result": first_of(d, _RESKEYS),
                      "error": err, "start": s.get("start_time"), "end": s.get("end_time")})
    t = {"run_id": rec.get("trace_id") or rec.get("id"), "conversation_id": conv, "user_text": user,
         "answer_text": ans, "tools": tools or None, "timestamp": rec.get("start_time"),
         "end_timestamp": rec.get("end_time"), "span_id": rec.get("id")}
    err = rec.get("error") or rec.get("fault")
    if err and not tools:
        t["error"] = "error"
    return [("canon", t)]


def lift_adk(rec):
    c = jloads(rec.get("content"))
    c = c if isinstance(c, dict) else {}
    parts = c.get("parts") if isinstance(c.get("parts"), list) else []
    author = str(rec.get("author") or c.get("role") or "").lower()
    base = {"run_id": rec.get("invocation_id") or rec.get("invocationId"),
            "conversation_id": rec.get("session_id") or rec.get("sessionId"),
            "timestamp": rec.get("timestamp"), "span_id": rec.get("id"),
            "user_id": rec.get("user_id") or rec.get("userId")}
    um = jloads(rec.get("usage_metadata") or rec.get("usageMetadata")) or {}
    evs = []
    for i, p in enumerate(parts):
        if not isinstance(p, dict):
            continue
        fc, fr = p.get("function_call") or p.get("functionCall"), p.get("function_response") or p.get("functionResponse")
        if isinstance(fc, dict):
            evs.append(_event_rec(base, "tool_call", tool_name=fc.get("name"), tool_args=fc.get("args")))
        elif isinstance(fr, dict):
            resp = fr.get("response")
            if isinstance(resp, dict) and set(resp) == {"result"}:
                resp = resp["result"]
            evs.append(_event_rec(base, "tool_result", tool_name=fr.get("name"), tool_result=resp))
        elif p.get("text"):
            if author == "user" or c.get("role") == "user" and author in ("user", ""):
                evs.append(_event_rec(base, "user", content=p["text"]))
            else:
                evs.append(_event_rec(base, "final", content=p["text"],
                                      input_tokens=um.get("prompt_token_count") or um.get("promptTokenCount"),
                                      output_tokens=um.get("candidates_token_count") or um.get("candidatesTokenCount")))
    if not evs:
        return []
    out = []
    for i, e in enumerate(evs):
        e = dict(e)
        e["span_id"] = "%s#%d" % (base["span_id"], i) if base["span_id"] and len(evs) > 1 else base["span_id"]
        out.append(("canon", e))
    return out


def _tags(tags):
    if isinstance(tags, list):
        d = {}
        for t in tags:
            if isinstance(t, dict) and "key" in t:
                d[t["key"]] = t.get("value")
        return d
    return dict(tags) if isinstance(tags, dict) else {}


def lift_jaeger(rec):
    refs = rec.get("references") or []
    parent = rec.get("parentSpanID") or next((r.get("spanID") for r in refs if isinstance(r, dict)
                                              and str(r.get("refType")).upper() in ("CHILD_OF", "FOLLOWS_FROM")), None)
    attrs = _tags(rec.get("tags"))
    out = {"trace_id": rec.get("traceID"), "span_id": rec.get("spanID"), "parent_span_id": parent,
           "span_name": rec.get("operationName"), "attributes": attrs}
    st = to_float(rec.get("startTime"))
    if st is not None:
        out["start_timestamp"] = to_epoch(rec.get("startTime"))
        d = to_float(rec.get("duration"))
        if d is not None:
            out["duration"] = d / 1e6
    err = attrs.get("error")
    if err is True or str(err).lower() == "true":
        out["status"] = {"code": "ERROR", "message": to_str(attrs.get("error.message") or attrs.get("message") or "error")}
    if rec.get("_service"):
        attrs.setdefault("service.name", rec["_service"])
    return [("otel", out)]


def lift_zipkin(rec):
    attrs = _tags(rec.get("tags"))
    out = {"trace_id": rec.get("traceId"), "span_id": rec.get("id"), "parent_span_id": rec.get("parentId"),
           "span_name": rec.get("name"), "attributes": attrs}
    if rec.get("timestamp") not in MISSING:
        out["start_timestamp"] = to_epoch(rec.get("timestamp"))
    d = to_float(rec.get("duration"))
    if d is not None:
        out["duration"] = d / 1e6
    ep = rec.get("localEndpoint")
    if isinstance(ep, dict) and ep.get("serviceName"):
        attrs.setdefault("service.name", ep["serviceName"])
    return [("otel", out)]


_OTEL_ATTR_COLS = ("attributes", "attrs", "customDimensions", "Properties", "properties", "span_attributes",
                   "attributes_json")


def is_otel_like(rec):
    if any(k in rec for k in ("span_id", "spanId", "SpanId", "operation_Id", "attributes", "customDimensions",
                              "Properties", "span_name", "displayName", "spanName", "span_attributes", "attrs",
                              "startTimeUnixNano", "start_time_unix_nano")):
        return True
    return any(isinstance(k, str) and (k.startswith("gen_ai") or k.startswith("llm.")) for k in rec)


def unwrap_payload(rec):
    """{time, stream, msg: '<json object>'} -> the JSON object plus the scalar columns (CloudWatch-style exports)."""
    if len(rec) > 8:
        return None
    big = [(k, v) for k, v in rec.items() if isinstance(v, str) and len(v) > 40 and v.lstrip()[:1] == "{"]
    if len(big) != 1:
        return None
    k, v = big[0]
    d = jloads(v)
    if not isinstance(d, dict) or len(d) < 2 or any(str(x).startswith(("gen_ai", "llm.")) for x in d):
        return None
    out = dict(d)
    for kk, vv in rec.items():
        if kk != k and not isinstance(vv, (dict, list)):
            out.setdefault(kk, vv)
    return out


def lift(rec, _depth=0):
    """Raw record -> list of (kind, record)."""
    try:
        if "ModelId" in rec or ("RequestId" in rec and isinstance(rec.get("Input"), dict)
                                and isinstance(rec.get("Output"), dict)):
            rec = {(k[0].lower() + k[1:] if isinstance(k, str) and k[:1].isupper() else k): v for k, v in rec.items()}
            CL.note("renamed_columns")
        if non_run_row(rec):
            CL.note("non_run_row")
            return []
        if "run_type" in rec or "parent_run_id" in rec or "dotted_order" in rec:
            return lift_langsmith(rec)
        if any(k in rec for k in ("jsonPayload", "textPayload", "protoPayload")) or \
                ("logName" in rec and "insertId" in rec):
            CL.note("format_lifted", 1, "cloud-logging")
            return lift_gcp(rec)
        if rec.get("schemaType") == "ModelInvocationLog" or \
                ("modelId" in rec and isinstance(jloads(rec.get("input")), dict)
                 and ("inputBodyJson" in jloads(rec.get("input")) or "inputBodyS3Path" in jloads(rec.get("input")))):
            CL.note("format_lifted", 1, "bedrock-invocation")
            return lift_bedrock_invocation(rec)
        tr = jloads(rec.get("trace"))
        if isinstance(tr, dict) and any(k.endswith("Trace") for k in tr) and \
                ("sessionId" in rec or "agentId" in rec or "eventTime" in rec):
            out = lift_bedrock_agent(rec)
            if out is not None:
                CL.note("format_lifted", 1, "bedrock-agent-trace")
                return out
        if "subsegments" in rec or ("trace_id" in rec and isinstance(rec.get("start_time"), (int, float))
                                    and str(rec.get("trace_id")).startswith("1-")):
            CL.note("format_lifted", 1, "x-ray")
            return lift_xray(rec)
        if ("invocation_id" in rec or "invocationId" in rec) and isinstance(jloads(rec.get("content")), dict) and \
                "parts" in jloads(rec.get("content")):
            CL.note("format_lifted", 1, "adk-events")
            return lift_adk(rec)
        if "spanID" in rec and "operationName" in rec:
            CL.note("format_lifted", 1, "jaeger")
            return lift_jaeger(rec)
        if "traceId" in rec and "id" in rec and "spanId" not in rec and any(k in rec for k in ("localEndpoint", "tags", "annotations", "kind")) \
                and "attributes" not in rec:
            CL.note("format_lifted", 1, "zipkin")
            return lift_zipkin(rec)
        if is_otel_like(rec) and not any(k in rec for k in ("messages", "contents")):
            return [("otel", rec)]
        if any(k in rec for k in ("messages", "contents", "mapping", "conversation", "chat", "transcript", "history")):
            t = lift_chat(rec)
            if t is not None and (t.get("user_text") or t.get("answer_text") or t.get("tools")):
                CL.note("format_lifted", 1, "chat-messages")
                return [("canon", t)]
        if not _looks_msg(rec) and any(isinstance(v, (dict, list)) for v in rec.values()):
            nested = find_messages(rec)
            if len(nested) >= 2 and any(norm_role(m.get("role") or m.get("type")) == "user" for m in nested):
                t = lift_chat(rec)
                if t is not None and (t.get("user_text") or t.get("answer_text") or t.get("tools")):
                    CL.note("format_lifted", 1, "chat-messages")
                    return [("canon", t)]
    except Exception:
        pass
    if _depth == 0:
        merged = unwrap_payload(rec)
        if merged is not None:
            CL.note("cloudwatch_message_unwrapped")
            return lift(merged, 1)
    return [("raw", rec)]


# ---------------------------------------------------------------- profile and inference

class Col(object):
    __slots__ = ("path", "n", "strs", "nums", "lists", "dicts", "distinct", "len_sum", "spaces", "qmarks", "ts_ok",
                 "samples", "json_dicts", "genai", "role_ok", "ev_ok", "ev_vals", "tool_lists", "numeric_like",
                 "name_like", "idlike", "ts_num", "dur_ok")

    def __init__(self, path):
        self.path = path
        for s in self.__slots__[1:]:
            setattr(self, s, 0)
        self.distinct = set()
        self.samples = []
        self.ev_vals = Counter()


def _walk_paths(rec, prefix="", depth=0, out=None):
    out = {} if out is None else out
    for k, v in rec.items():
        p = prefix + str(k)
        if isinstance(v, str) and v[:1] in ("{",) and depth < 3 and len(v) < 200000:
            d = jloads(v)
            if isinstance(d, dict):
                out[p] = v
                _walk_paths(d, p + ".", depth + 1, out)
                continue
        if isinstance(v, dict) and depth < 3:
            out[p] = v
            _walk_paths(v, p + ".", depth + 1, out)
        else:
            out[p] = v
    return out


def profile(records, limit=400):
    step = max(1, len(records) // limit)
    sample = records[::step][:limit]
    cols = {}
    for rec in sample:
        for p, v in _walk_paths(rec).items():
            c = cols.get(p)
            if c is None:
                c = cols[p] = Col(p)
            if v in MISSING:
                continue
            c.n += 1
            if isinstance(v, str):
                s = v.strip()
                c.strs += 1
                c.len_sum += len(s)
                if " " in s:
                    c.spaces += 1
                if "?" in s:
                    c.qmarks += 1
                if len(c.distinct) < 2000:
                    c.distinct.add(s[:200])
                if s[:1] == "{":
                    d = jloads(s)
                    if isinstance(d, dict):
                        c.json_dicts += 1
                        if any(str(k).startswith(("gen_ai", "llm.", "openinference", "traceloop")) for k in d):
                            c.genai += 1
                if s[:1] == "[" and isinstance(jloads(s), list):
                    c.lists += 1
                    items = jloads(s)
                    if items and isinstance(items[0], dict) and tool_item(items[0]):
                        c.tool_lists += 1
                if len(s) > 5 and re.search(r"\d{4}-\d\d-\d\d|\d\d?/\d\d?/\d{4}", s) and to_epoch(s, note=False) is not None:
                    c.ts_ok += 1
                elif re.fullmatch(r"\d{9,19}(\.\d+)?", s) and to_epoch(s, note=False) is not None:
                    c.ts_ok += 1
                    c.ts_num += 1
                if re.match(r"^[A-Za-z0-9_\-:.]{1,64}$", s) and re.search(r"\d", s):
                    c.idlike += 1
                if to_float(s) is not None:
                    c.numeric_like += 1
                elif len(s) < 24 and parse_duration_s(s) is not None and re.search(r"\d", s):
                    c.dur_ok += 1
                ev = classify_event(s)
                if ev:
                    c.ev_ok += 1
                    c.ev_vals[ev] += 1
                if norm(s) in ROLE_VALUES:
                    c.role_ok += 1
            elif isinstance(v, bool):
                c.nums += 1
            elif isinstance(v, (int, float)):
                c.nums += 1
                c.numeric_like += 1
                if len(c.distinct) < 2000:
                    c.distinct.add(v)
                if 1e8 <= abs(v) <= 6e18 and to_epoch(v, note=False) is not None:
                    c.ts_ok += 1
                    c.ts_num += 1
            elif isinstance(v, list):
                c.lists += 1
                if v and isinstance(v[0], dict) and tool_item(v[0]):
                    c.tool_lists += 1
            elif isinstance(v, dict):
                c.dicts += 1
            if len(c.samples) < 3:
                c.samples.append(v)
    return cols, len(sample)


def _texty(c):
    return c.strs and c.strs >= 0.6 * c.n and (c.len_sum / c.strs >= 9 or c.spaces >= 0.5 * c.strs) \
        and c.json_dicts < 0.5 * c.strs


def _idy(c):
    return c.n and c.strs + c.nums >= 0.8 * c.n and c.json_dicts == 0 and (c.len_sum / max(c.strs, 1) <= 80) \
        and not (c.spaces > 0.3 * max(c.strs, 1)) and c.dicts == 0 and c.lists == 0


def infer(records):
    """-> (mapping, notes, confident). mapping is the flat mapping (+ optional format for span tables)."""
    cols, n = profile(records)
    notes = []
    if not cols:
        return None, ["no columns"], False
    byname = {}
    for p, c in cols.items():
        last = p.rsplit(".", 1)[-1]
        nm = norm(last)
        byname.setdefault(nm, []).append(c)
        sn = split_unit(last)[0]
        if sn != nm:
            byname.setdefault(sn, []).append(c)
    used = set()
    m = {}

    def cands(role):
        res = []
        for pri, name in enumerate(V.get(role, [])):
            for c in byname.get(name, []):
                if c.path not in used and c.n >= 0.05 * n:
                    # prefer top-level over nested at equal priority
                    res.append((pri, c.path.count("."), c))
        res.sort(key=lambda x: (x[0], x[1]))
        return [c for _, _, c in res]

    def take(role, test=None, key=None):
        for c in cands(role):
            if test is None or test(c):
                used.add(c.path)
                m[key or role] = c.path
                return c
        return None

    # identifiers and time
    take("run_id", _idy)
    take("trace_id", _idy)
    take("conversation_id", _idy)
    take("parent_id", _idy)
    ts = take("timestamp", lambda c: c.ts_ok >= 0.25 * c.n)
    if ts is None:
        # any column that parses as a timestamp
        best = [c for c in cols.values() if c.path not in used and c.ts_ok - c.ts_num >= 0.8 * max(c.n, 1) and c.n >= 0.5 * n]
        if best:
            best.sort(key=lambda c: (c.path.count("."), -c.ts_ok))
            used.add(best[0].path)
            m["timestamp"] = best[0].path
            notes.append("timestamp column chosen by value shape: %s" % best[0].path)
    take("end_timestamp", lambda c: c.ts_ok >= 0.25 * c.n)
    dur = take("duration", lambda c: c.numeric_like + c.dur_ok >= 0.6 * c.n)
    if dur:
        u = name_unit(dur.path.rsplit(".", 1)[-1])
        vals = [to_float(x) for x in dur.samples if to_float(x) is not None]
        m["duration_unit"] = u or "auto"
        m["duration_s"] = m.pop("duration")
    # wide tool columns: tool_1_name, tool_1_output ...
    wide = {}
    for p, c in cols.items():
        mm = re.match(r"^(?:tool|function|action)[_\- ]?(\d+)[_\- ]?(name|output|result|args|input|arguments|status|error)$",
                      p.rsplit(".", 1)[-1], re.I)
        if mm:
            wide.setdefault(int(mm.group(1)), {})[
                {"output": "result", "input": "args", "arguments": "args"}.get(mm.group(2).lower(), mm.group(2).lower())] = p
    if wide:
        m["wide_tools"] = [wide[i] for i in sorted(wide) if "name" in wide[i]]
        for d in m["wide_tools"]:
            used.update(d.values())
    # tools list column
    tl = [c for c in cols.values() if c.path not in used and c.tool_lists >= 0.3 * max(c.n, 1)]
    tl.sort(key=lambda c: (norm(c.path.rsplit(".", 1)[-1]) not in V["tools"], c.path.count(".")))
    if tl:
        m["tools"] = tl[0].path
        used.add(tl[0].path)
    # role / event
    role = take("role", lambda c: c.role_ok >= 0.7 * max(c.strs, 1) and c.strs >= 0.7 * c.n)
    ev = take("event", lambda c: c.ev_ok >= 0.6 * max(c.strs, 1) and c.strs >= 0.7 * c.n and len(c.ev_vals) >= 1
              and c.role_ok < 0.7 * max(c.strs, 1))
    if ev:
        m["kind"] = m.pop("event")
        kv = {}
        for c in [cols[m["kind"]]]:
            for k in c.ev_vals:
                kv.setdefault(k, [])
        vals = {}
        for rec in records[:2000]:
            v = get_path(rec, m["kind"])
            if v not in MISSING and isinstance(v, str):
                k = classify_event(v)
                if k:
                    vals.setdefault(k, set()).add(v)
        m["kind_values"] = {k: sorted(v) for k, v in vals.items()}
    # span/event id
    take("span_id", lambda c: _idy(c) and len(c.distinct) >= 0.9 * c.n)
    # tool name/args/result
    tn = take("tool_name", lambda c: c.strs >= 0.5 * c.n and (c.len_sum / max(c.strs, 1)) <= 80 and c.json_dicts == 0)
    if tn is None and ev and "tool_call" in (m.get("kind_values") or {}):
        pass
    if tn:
        take("tool_args")
        take("tool_result")
    else:
        take("tool_args", lambda c: bool(m.get("tools")) is False and False)
    # texts
    content = None
    if role or ev:
        content = take("content", lambda c: c.strs >= 0.3 * c.n)
    ut = take("user_text", lambda c: c.strs >= 0.5 * c.n and c.numeric_like < 0.5 * c.n and c.json_dicts == 0
              and not (norm(c.path.rsplit(".", 1)[-1]) == "user" and not _texty(c)))
    at = take("answer_text", lambda c: c.strs >= 0.3 * c.n and not (
        c.numeric_like >= 0.9 * c.n and norm(c.path.rsplit(".", 1)[-1]) not in (
            "answer", "finalanswer", "reply", "response", "completion", "assistant", "agentresponse", "airesponse")))
    if not ut and at and not (role or ev):
        c = next((x for x in cols.values() if x.path not in used and norm(x.path.rsplit(".", 1)[-1]) == "user"
                  and x.strs >= 0.5 * x.n), None)
        if c is not None:
            m["user_text"] = c.path
            used.add(c.path)
            ut = c
    # shape fallback for text columns
    if not (role or ev) and not (ut and at):
        free = [c for c in cols.values() if c.path not in used and _texty(c) and c.path.count(".") == 0
                and not c.idlike >= 0.9 * c.n and len(c.distinct) >= max(2, 0.3 * min(n, c.n))]
        free.sort(key=lambda c: (c.qmarks == 0, list(cols).index(c.path)))
        if not ut and free:
            c = free.pop(0)
            m["user_text"] = c.path
            used.add(c.path)
            notes.append("user message column chosen by value shape: %s" % c.path)
            ut = c
        if not at and free:
            c = free.pop(0)
            m["answer_text"] = c.path
            used.add(c.path)
            notes.append("answer column chosen by value shape: %s" % c.path)
            at = c
        if not ut and not at:
            lone = [c for c in cols.values() if c.path not in used and c.strs >= 0.5 * c.n and c.json_dicts == 0
                    and c.dicts == 0 and c.lists == 0 and c.numeric_like < 0.5 * c.n and c.ts_ok < 0.5 * c.n
                    and c.path.count(".") == 0]
            if lone:
                m["user_text"] = lone[0].path
                used.add(lone[0].path)
                notes.append("only text-like column %s taken as the user message" % lone[0].path)
                ut = lone[0]
    # identifiers by shape when still absent
    if "run_id" not in m and "trace_id" not in m:
        ids = [c for c in cols.values() if c.path not in used and _idy(c) and c.idlike >= 0.9 * c.n
               and c.path.count(".") == 0 and c.n >= 0.8 * n and len(c.distinct) >= 0.5 * n
               and c.ts_ok < 0.5 * c.n and c.numeric_like < 0.5 * c.n]
        if ids and (ut or at):
            m["run_id"] = ids[0].path
            used.add(ids[0].path)
            notes.append("run id column chosen by value shape: %s" % ids[0].path)
    # remaining
    take("error")
    st = take("status")
    if st and st.path:
        vals = {str(v).lower() for rec in records[:2000] for v in [get_path(rec, st.path)] if v not in MISSING}
        errv = [x for x in vals if re.search(r"err|fail|exception|fatal|crit|timeout|denied|\b[45]\d\d\b", x)]
        if norm(st.path.rsplit(".", 1)[-1]) in ("success", "ok"):
            m["status_error_values"] = ["false", "0", "no"]
        elif errv:
            m["status_error_values"] = sorted(errv)
        else:
            m.pop("status", None)
    take("input_tokens", lambda c: c.numeric_like >= 0.5 * c.n)
    take("output_tokens", lambda c: c.numeric_like >= 0.5 * c.n)
    if "input_tokens" not in m and "output_tokens" not in m:
        take("total_tokens", lambda c: c.numeric_like >= 0.5 * c.n)
    take("model", lambda c: c.strs >= 0.5 * c.n)
    take("cost_usd", lambda c: c.numeric_like >= 0.5 * c.n)
    take("agent", lambda c: c.strs >= 0.5 * c.n and c.json_dicts == 0)
    take("environment", lambda c: c.strs >= 0.5 * c.n)
    take("user_id", lambda c: c.strs + c.nums >= 0.5 * c.n and not _texty(c))

    # span table (id + parent + attribute blob / kind) -> rename to otel/langsmith
    blob = [c for c in cols.values() if c.genai >= 0.3 * max(c.n, 1)]
    if blob:
        fm = {"attributes": blob[0].path}
        name_c = None
        for c in cols.values():
            if c.path == blob[0].path or c.path.count("."):
                continue
            nm = norm(c.path)
            if nm in ("name", "what", "spanname", "operationname", "label", "displayname", "message", "title") \
                    and c.strs >= 0.5 * c.n:
                name_c = c
                break
        if name_c is None:
            cand = [c for c in cols.values() if c.path != blob[0].path and c.strs >= 0.8 * c.n and
                    c.spaces >= 0.5 * c.strs and not _texty(c) and c.path.count(".") == 0]
            name_c = cand[0] if cand else None
        if name_c:
            fm["span_name"] = name_c.path
        for k, role_ in (("trace_id", "trace_id"), ("span_id", "span_id"), ("parent_span_id", "parent_id"),
                         ("start_timestamp", "timestamp"), ("end_timestamp", "end_timestamp")):
            if m.get(role_):
                fm[k] = m[role_]
        if "trace_id" not in fm:
            tcs = [c for c in cols.values() if c.path not in fm.values() and c.idlike >= 0.9 * c.n
                   and c.spaces == 0 and c.path.count(".") == 0 and 0.01 < len(c.distinct) / max(c.n, 1) < 0.9
                   and c.ts_ok < 0.5 * c.n]
            if tcs:
                fm["trace_id"] = tcs[0].path
        if "start_timestamp" not in fm:
            tc = [c for c in cols.values() if c.ts_ok >= 0.6 * max(c.n, 1)]
            if tc:
                fm["start_timestamp"] = tc[0].path
        if m.get("duration_s"):
            fm["duration"] = m["duration_s"]
        notes.append("span table with attribute column %s: renamed to the canonical OpenTelemetry fields" % blob[0].path)
        return {"format": "otel", "rename": fm, "_notes": notes}, notes, True
    # langsmith-like renamed: id + parent + kind(chain/llm/tool) + inputs/outputs
    kindc = [c for c in cols.values() if c.strs >= 0.8 * c.n and
             {str(x).lower() for x in c.distinct} & {"llm", "tool", "chain", "retriever", "agent"} and len(c.distinct) <= 12]
    parentc = take("parent_id") if "parent_id" not in m else None
    if kindc and ("parent_id" in m or parentc):
        fm = {}
        idc = m.get("span_id") or next((c.path for c in cols.values() if c.idlike >= 0.9 * c.n and len(c.distinct) >= 0.9 * n
                                        and c.path not in (m.get("parent_id"),)), None)
        if idc:
            fm["id"] = idc
        fm["parent_run_id"] = m["parent_id"]
        fm["run_type"] = kindc[0].path
        sess = m.get("trace_id") or m.get("conversation_id")
        if sess:
            fm["trace_id"] = sess
        for k, role_ in (("start_time", "timestamp"), ("end_time", "end_timestamp"), ("error", "error"),
                         ("prompt_tokens", "input_tokens"), ("completion_tokens", "output_tokens"),
                         ("total_cost", "cost_usd")):
            if m.get(role_):
                fm[k] = m[role_]
        mc = next((c for c in cols.values() if c.path.count(".") == 0 and norm(c.path) in ("metadata", "extra", "meta")),
                  None)
        if mc is not None:
            fm["metadata"] = mc.path
        used_vals = set(fm.values())
        textc = [c for c in cols.values() if c.path not in used_vals and c.json_dicts + c.strs >= 0.5 * c.n]
        nm_c = next((c for c in textc if norm(c.path) in ("name", "label", "title", "what", "displayname")), None)
        if nm_c:
            fm["name"] = nm_c.path
        inc = next((c for c in textc if norm(c.path) in V["user_text"] or norm(c.path) in ("prompt", "input", "inputs")), None)
        outc = next((c for c in textc if norm(c.path) in V["answer_text"] or norm(c.path) in ("response", "output", "outputs")), None)
        if inc:
            fm["inputs"] = inc.path
        if outc:
            fm["outputs"] = outc.path
        if "error" not in fm:
            ec = next((c for c in cols.values() if norm(c.path) in ("err", "error", "exception")), None)
            if ec:
                fm["error"] = ec.path
        notes.append("run table with renamed columns mapped to the LangSmith fields")
        return {"format": "langsmith", "rename": fm, "_notes": notes}, notes, True

    ok = bool(m.get("user_text") or m.get("answer_text") or m.get("content") or m.get("tools") or m.get("tool_name")
              or m.get("wide_tools") or m.get("role") or m.get("kind"))
    if ut and not m.get("kind") and not m.get("role"):
        pass
    return m, notes, ok


# ---------------------------------------------------------------- applying a flat mapping

CANON_MAP = {k: k for k in ("run_id", "conversation_id", "trace_id", "span_id", "parent_id", "timestamp",
                            "end_timestamp", "content", "tool_name", "tool_args", "tool_result", "error",
                            "input_tokens", "output_tokens", "model", "cost_usd", "agent", "environment",
                            "user_text", "answer_text", "tools", "user_id", "total_tokens", "duration_s")}
CANON_MAP["kind"] = "event"
CANON_MAP["kind_values"] = {"user": ["user"], "final": ["final"], "assistant": ["assistant"],
                            "tool_call": ["tool_call"], "tool_result": ["tool_result"], "llm": ["llm"]}


class Ctx(object):
    def __init__(self):
        self.turns = set()
        self.open = {}
        self.dedup = set()
        self.run_turns = {}


def _bad(cell):
    return not real_error(cell)


def _new_tool(rec_spans, ctx, name, args, result, err, start, end, dur, base, fmt="flat"):
    s = Span(fmt)
    s.kind = "tool"
    s.tool = to_str(name) or "tool"
    s.name = s.tool
    s.args = to_str(args) if args not in MISSING and args is not None else None
    s.result = to_str(result) if result not in MISSING and result is not None else None
    s.start = to_epoch(start) if start not in MISSING else base.get("start")
    s.end = to_epoch(end) if end not in MISSING else None
    d = parse_duration_s(dur, "s") if dur not in MISSING and dur is not None else None
    s.dur = d if d is not None else (max(0.0, s.end - s.start) if s.end is not None and s.start is not None else None)
    if s.end is None and s.start is not None and s.dur is not None:
        s.end = s.start + s.dur
    if err not in MISSING and err is not None and not _bad(err):
        s.err = clip(to_str(err) if str(err).lower() not in ("true", "1") else "error", 300)
    s.has_status = err is not None or False
    s.call, s.tid, s.conv, s.pid, s.user = base["call"], base["tid"], base["conv"], base["sid"], base["user"]
    s.agent, s.env = base["agent"], base["env"]
    rec_spans.append(s)
    return s


def parse_flat(rec, m, seq, ctx, opts_unit=None):
    """Canonical/mapped record -> [Span]."""

    def g(key):
        col = m.get(key)
        if not col or not isinstance(col, str):
            return None
        v = get_path(rec, col)
        if v is None or (isinstance(v, str) and v in MISSING) or (not isinstance(v, (dict, list)) and v in MISSING):
            return None
        return v

    spans = []
    fmt_ts = m.get("timestamp_format")
    tv = g("timestamp")
    start = to_epoch(tv, fmt_ts)
    if tv is not None and start is None:
        CL.note("bad_timestamp")
    ev_ = g("end_timestamp")
    end = to_epoch(ev_, fmt_ts)
    unit = m.get("duration_unit")
    if unit in (None, "auto"):
        unit = opts_unit or "s"
    dv = g("duration_s")
    if dv is not None:
        dur = parse_duration_s(dv, unit if unit in ("s", "ms", "us", "ns") else "s")
    else:
        dur = parse_duration_s(g("duration_ms"), "ms")
    dur = ok_dur(dur)
    if dur is None and start is not None and end is not None:
        if end >= start:
            dur = end - start
        else:
            CL.note("inverted_times")
            end = None
    if end is None and start is not None and dur is not None:
        end = start + dur
    sid = to_str(g("span_id")) or None
    base = {"call": to_str(g("run_id")) or None, "tid": to_str(g("trace_id")) or None,
            "conv": to_str(g("conversation_id")) or None, "sid": to_str(g("parent_id")) or None,
            "user": to_str(g("user_id")) or None, "agent": to_str(g("agent")) or None,
            "env": to_str(g("environment")) or None, "start": start}
    model = to_str(g("model")) or None
    itok, otok = to_tokens(g("input_tokens")), to_tokens(g("output_tokens"))
    if not (itok or otok) and g("total_tokens") is not None:
        itok = to_tokens(g("total_tokens"))
    cost = to_float(g("cost_usd"))
    err_cell = g("error")
    err = None
    if err_cell is not None and not _bad(err_cell):
        err = clip(to_str(err_cell) if str(err_cell).lower() not in ("true", "1") else "error", 300)
    st = g("status")
    if st is not None and not err:
        bad_vals = [x.lower() for x in (m.get("status_error_values") or
                                        ["error", "failed", "failure", "exception", "fail"])]
        if str(st).lower() in bad_vals:
            err = clip(to_str(st), 300)
    has_status = bool(err_cell is not None or st is not None)
    role = norm_role(g("role"))
    content = g("content")
    kind_cell = g("kind")
    kv = {k: [str(x).lower() for x in v] for k, v in (m.get("kind_values") or {}).items()}
    ek = None
    if kind_cell is not None:
        kc = str(kind_cell).lower()
        for k, vals in kv.items():
            if kc in vals:
                ek = k
        if ek is None and not kv:
            ek = classify_event(kind_cell)
        if ek is None and kv:
            ek = classify_event(kind_cell)
    ek = {"tool": "tool_row", "agent": "agent", "llm": "llm"}.get(ek, ek)
    if ek == "assistant" and role == "":
        role = "assistant"
    if ek == "user":
        role = "user"
    if ek in ("final", "assistant"):
        role = "assistant"
    tool = g("tool_name")
    ut, at = g("user_text"), g("answer_text")
    tools_cell = g("tools")
    tools = jloads(tools_cell) if tools_cell is not None else None
    tools = tools if isinstance(tools, list) else None
    wide = m.get("wide_tools") or []

    group = (base["call"] or base["tid"] or base["conv"] or "_")

    def mk_text_span(kind, utext, atext, final=False):
        s = Span("flat")
        s.kind = kind
        s.sid = sid
        s.tid, s.call, s.conv, s.user = base["tid"], base["call"], base["conv"], base["user"]
        s.start, s.end, s.dur = start, end, dur
        s.agent, s.env, s.model = base["agent"], base["env"], model
        s.itok, s.otok, s.cost = itok, otok, cost
        s.utext = to_str(utext).strip() or None if utext is not None else None
        s.atext = to_str(atext).strip() or None if atext is not None else None
        s.final = final
        s.err, s.has_status = (err if kind != "tool" else None), has_status
        s.name = kind
        return s

    is_event = ek in ("user", "final", "assistant", "tool_call", "tool_result", "tool_row", "llm", "agent") or \
        bool(role)
    # --- turn text columns
    if (ut is not None or at is not None) and not is_event:
        sig = to_str(tools_cell)[:3000] + "|".join(to_str(get_path(rec, w.get("name"))) + to_str(get_path(rec, w.get("result")))
                                                    for w in wide if isinstance(w, dict))
        kt = tv if (tool is None and not tools and not wide) else None
        key = (group, to_str(ut)[:200], to_str(at)[:200], sig, kt)
        dup = key in ctx.turns and (group != "_" or tv is not None)
        if dup and (tools or wide or tool is None):
            ctx.dups = getattr(ctx, "dups", 0) + 1
            return []
        if not dup or tool is None:
            ctx.turns.add(key)
            if base["call"]:
                owned = ctx.run_turns.setdefault(base["call"], {})
                tk = (to_str(ut)[:200], to_str(at)[:200])
                if tk not in owned:
                    owned[tk] = base["call"] if not owned else "%s#%d" % (base["call"], len(owned) + 1)
                    if len(owned) > 1:
                        CL.note("duplicate_ids_split")
                base["call"] = owned[tk]
            s = mk_text_span("agent", ut, at, final=at is not None)
            s.pid = None
            spans.append(s)
            parent = s
            if base["call"] is None and base["tid"] is None:
                s.call = "run-" + shash(base["conv"], tv, to_str(ut)[:300], to_str(at)[:300], seq if tv is None else "")
                base["call"] = s.call
                CL.note("run_id_synthesized")
    elif (ut is not None or at is not None) and is_event and role != "tool":
        # event rows that also carry run-level text columns: record them once per run
        key = (group, to_str(ut)[:200], to_str(at)[:200])
        if key not in ctx.turns:
            ctx.turns.add(key)
            s = mk_text_span("agent", ut, at, final=True)
            spans.append(s)
    # --- tool lists / wide tools
    tool_calls_made = []
    if tools:
        for it in tools:
            ti = tool_item(it)
            if ti:
                _new_tool(spans, ctx, ti["name"], ti["args"], ti["result"], ti["error"], ti["start"], ti["end"],
                          ti["duration"], dict(base, call=base["call"] or (spans[0].call if spans else None)))
    for w in wide:
        nm = get_path(rec, w.get("name"))
        if nm in MISSING or nm is None:
            continue
        _new_tool(spans, ctx, nm, get_path(rec, w.get("args")) if w.get("args") else None,
                  get_path(rec, w.get("result")) if w.get("result") else None,
                  get_path(rec, w.get("error")) if w.get("error") else None, None, None, None,
                  dict(base, call=base["call"] or (spans[0].call if spans else None)))
    # --- single row tool / event rows
    if ek in ("tool_call", "tool_result", "tool_row") or role == "tool" or (tool is not None and not is_event) or \
            (tool is not None and ek not in ("user", "final", "assistant", "llm", "agent")):
        res = g("tool_result")
        if res is None and role == "tool" and content is not None:
            res = content
        if res is None and ek == "tool_result":
            res = content
        args = g("tool_args")
        if args is None and ek == "tool_call" and content is not None:
            args = content
        if ek == "tool_result":
            lst = ctx.open.get(group) or []
            tgt = None
            for t in reversed(lst):
                if t.result is None and (not tool or t.tool == to_str(tool)):
                    tgt = t
                    break
            if tgt is None:
                for t in reversed(lst):
                    if t.result is None:
                        tgt = t
                        break
            if tgt is not None:
                tgt.result = to_str(res) if res is not None else tgt.result
                if err and not tgt.err:
                    tgt.err = err
                tgt.has_status = tgt.has_status or has_status
                if start is not None:
                    tgt.end = start
                    if tgt.start is not None:
                        tgt.dur = max(0.0, start - tgt.start)
                return spans
        s = _new_tool(spans, ctx, tool if tool is not None else (content if role == "tool" and False else "tool"),
                      args, res, err, tv, ev_ if ev_ is not None else None, dur, dict(base))
        s.start, s.end, s.dur = start, end, dur
        s.sid = sid
        s.pid = None
        s.has_status = has_status
        s.itok, s.otok, s.model = itok, otok, model
        if ek == "tool_call":
            ctx.open.setdefault(group, []).append(s)
        if role == "assistant" and content is not None and ek in (None, "assistant"):
            spans.append(mk_text_span("msg", None, content))
    elif ek in ("user", "final", "assistant") or role in ("user", "assistant", "system"):
        text = content if content is not None else (ut if role == "user" else at)
        s = mk_text_span("msg", text if role == "user" else None, text if role == "assistant" else None,
                         final=(ek == "final"))
        if role == "system":
            s.utext = s.atext = None
        spans.append(s)
    elif ek in ("llm", "agent") or model or itok or otok:
        s = mk_text_span("llm" if ek != "agent" else "agent", None, content if ek == "agent" else None)
        if ek == "llm" or (model or itok or otok):
            s.kind = "llm"
        spans.append(s)
    elif not spans and (content is not None):
        spans.append(mk_text_span("msg", content, None))
    for s in spans:
        if s.sid is None and s.kind != "tool":
            s.sid = sid
    if sid and spans:
        spans[0].sid = sid
    for s in spans:
        s.src = seq
    return spans
