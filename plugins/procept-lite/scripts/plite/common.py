"""Shared helpers for procept-lite ingestion (stdlib only).

Contents: tolerant record readers (JSON array / JSON object / JSONL / CSV / TSV, optionally .gz, files or
directories, streamed), timestamp parsing, text extraction from the many chat-message shapes found in
agent traces, redaction and clipping helpers.
"""
import csv
import gzip
import json
import os
import re
import sys
from datetime import datetime, timezone

try:
    csv.field_size_limit(sys.maxsize)
except OverflowError:  # pragma: no cover (Windows)
    csv.field_size_limit(2 ** 31 - 1)

MISSING = (None, "", "null", "NULL", "None", "nan", "NaN")
EXTS = (".json", ".jsonl", ".ndjson", ".csv", ".tsv")

# ---------------------------------------------------------------- small utils

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
SECRET_RE = re.compile(r"\b(?:sk|pk|rk|ghp|gho|xox[bpa])[-_][A-Za-z0-9_\-]{16,}|\bBearer\s+[A-Za-z0-9._\-]{16,}")


def redact(s):
    """Replace e-mail addresses and obvious API tokens with placeholders."""
    s = EMAIL_RE.sub("[email]", str(s))
    return SECRET_RE.sub("[secret]", s)


def clip(s, n):
    s = "" if s is None else str(s)
    return s if len(s) <= n else s[: max(0, n - 3)] + "..."


def short(s, n=80):
    """Redacted, single-line, <= n chars: the only form in which message content may be shown in reports."""
    return clip(redact(" ".join(str(s).split())), n)


def to_str(v):
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    try:
        return json.dumps(v, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(v)


def pick(d, keys):
    """First value of d[k] (k in keys) that is not a missing marker."""
    for k in keys:
        v = d.get(k)
        if v not in MISSING:
            return v
    return None


def jloads(v):
    """Parse v if it is a JSON object/array in a string, else return it unchanged."""
    if isinstance(v, str):
        t = v.lstrip()
        if t[:1] in ("{", "["):
            try:
                return json.loads(t)
            except ValueError:
                return v
    return v


def to_int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def to_float(v):
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def write_json(path, obj):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False, default=str)


def load_json_file(path):
    with open(path, encoding="utf-8-sig") as f:
        return json.load(f)


# ---------------------------------------------------------------- time

_TS_RE = re.compile(
    r"^\s*(\d{4})-(\d\d)-(\d\d)[T ](\d\d):(\d\d):(\d\d)(?:[.,](\d+))?\s*(Z|UTC|[+-]\d\d(?::?\d\d)?)?\s*$")


def to_epoch(v, fmt=None):
    """Seconds since epoch (float) or None. Accepts ISO 8601 / 'YYYY-MM-DD HH:MM:SS.ffffff+00' (naive = UTC),
    numbers in s / ms / us / ns (auto-detected by magnitude) and datetime."""
    if v in MISSING:
        return None
    if isinstance(v, datetime):
        return (v if v.tzinfo else v.replace(tzinfo=timezone.utc)).timestamp()
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)) or (isinstance(v, str) and v.strip().lstrip("-").replace(".", "", 1).isdigit()):
        n = float(v)
        a = abs(n)
        if a >= 1e17:
            return n / 1e9
        if a >= 1e14:
            return n / 1e6
        if a >= 1e11:
            return n / 1e3
        return n
    if not isinstance(v, str):
        return None
    if fmt:
        try:
            d = datetime.strptime(v.strip(), fmt)
            return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()
        except ValueError:
            return None
    m = _TS_RE.match(v)
    if not m:
        return None
    y, mo, d, h, mi, s, frac, tz = m.groups()
    try:
        base = datetime(int(y), int(mo), int(d), int(h), int(mi), int(s), tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None
    if frac:
        base += float("0." + frac)
    if tz and tz not in ("Z", "UTC"):
        sign = -1 if tz[0] == "-" else 1
        digits = tz[1:].replace(":", "")
        off = int(digits[:2]) * 3600 + (int(digits[2:4]) * 60 if len(digits) > 2 else 0)
        base -= sign * off
    return base


def iso(ts):
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_duration_s(v, unit="s"):
    """Duration in seconds from a number, numeric string or 'HH:MM:SS.fff'. unit in s|ms|us|ns."""
    if v in MISSING:
        return None
    if isinstance(v, str) and ":" in v:
        try:
            parts = [float(x) for x in v.strip().split(":")]
        except ValueError:
            return None
        sec = 0.0
        for p in parts:
            sec = sec * 60 + p
        return sec
    f = to_float(v)
    if f is None:
        return None
    return f / {"s": 1, "ms": 1e3, "us": 1e6, "ns": 1e9}[unit]


# ---------------------------------------------------------------- message / text extraction

ROLE_MAP = {"human": "user", "user": "user", "ai": "assistant", "assistant": "assistant", "model": "assistant",
            "aimessage": "assistant", "humanmessage": "user", "system": "system", "tool": "tool",
            "function": "tool", "toolmessage": "tool", "systemmessage": "system", "developer": "system"}
_TEXT_KEYS = ("output", "answer", "response", "result", "text", "final_output", "final_answer", "content",
              "message", "input", "question", "query", "prompt", "user_input", "value")
_TC_TYPES = ("tool_call", "tool_use", "function_call", "tool-call", "toolcall")


def _content_text(c):
    """(text, has_tool_call) from a message 'content' (str or list of parts)."""
    if isinstance(c, str):
        return c, False
    if isinstance(c, dict):
        c = [c]
    if not isinstance(c, list):
        return "", False
    out, tc = [], False
    for p in c:
        if isinstance(p, str):
            out.append(p)
        elif isinstance(p, dict):
            t = str(p.get("type") or "").lower()
            if t in _TC_TYPES or "tool_call" in p or "function_call" in p:
                tc = True
            elif t in ("tool_call_response", "tool_result", "tool_response", "image_url", "image", "reasoning",
                       "thinking"):
                continue
            else:
                x = p.get("text")
                if x is None:
                    x = p.get("content")
                if isinstance(x, str):
                    out.append(x)
    return "\n".join(out), tc


def _one_message(m, default_role):
    kw = m.get("kwargs") if isinstance(m.get("kwargs"), dict) else None  # LangChain serialized message
    src = kw if kw is not None else m
    role = src.get("role") or src.get("type") or m.get("role") or m.get("author")
    if kw is not None and not role and isinstance(m.get("id"), list) and m["id"]:
        role = str(m["id"][-1])
    role = ROLE_MAP.get(str(role or "").lower().replace("_", ""), "") if role else ""
    c = src.get("content")
    if c is None:
        c = src.get("parts")
    if c is None and isinstance(src.get("message"), dict):
        return _one_message(src["message"], default_role)
    if c is None:
        c = src.get("text")
    text, tc = _content_text(c)
    if (src.get("tool_calls") or src.get("function_call")
            or (isinstance(src.get("additional_kwargs"), dict)
                and (src["additional_kwargs"].get("tool_calls") or src["additional_kwargs"].get("function_call")))):
        tc = True
    if str(src.get("finish_reason") or "").lower() in ("tool_call", "tool_calls", "tool_use"):
        tc = True
    return (role or default_role, text, tc)


def messages_of(obj, default_role="", _d=0):
    """Flatten many chat shapes to [(role, text, has_tool_call)].

    Handles: plain strings (JSON strings are parsed), OpenAI/OTel GenAI message lists (content or parts[]),
    LangChain serialized messages, {"messages": [...]}, {"generations": [[{"text"|"message"}]]},
    {"prompts": [...]}, and dicts such as {"output": "..."} / {"input": "..."} (default_role applies).
    """
    if _d > 6 or obj in MISSING:
        return []
    obj = jloads(obj)
    if isinstance(obj, str):
        return [(default_role, obj, False)]
    if isinstance(obj, list):
        out = []
        for x in obj:
            if isinstance(x, list):
                out += messages_of(x, default_role, _d + 1)
            elif isinstance(x, str):
                out.append((default_role, x, False))
            elif isinstance(x, dict):
                if any(k in x for k in ("role", "content", "parts", "kwargs", "message", "text", "type")):
                    out.append(_one_message(x, default_role))
                else:
                    out += messages_of(x, default_role, _d + 1)
        return out
    if isinstance(obj, dict):
        for k in ("messages", "generations", "prompts", "history"):
            if k in obj:
                return messages_of(obj[k], default_role, _d + 1)
        if any(k in obj for k in ("role", "content", "parts", "kwargs")):
            return [_one_message(obj, default_role)]
        for k in _TEXT_KEYS:
            if k in obj and obj[k] not in MISSING:
                return messages_of(obj[k], default_role, _d + 1)
        strs = [v for v in obj.values() if isinstance(v, str) and v.strip()]
        if len(strs) == 1:
            return [(default_role, strs[0], False)]
    return []


def last_text(obj, want, default_role):
    """Text of the last message with role == want (roles missing in the data fall back to default_role).
    For want == 'assistant' a final message that contains tool calls is NOT an answer -> None.
    Returns (text or None, saw_tool_call)."""
    msgs = [m for m in messages_of(obj, default_role) if m[0] == want]
    if not msgs:
        return None, False
    if want == "user":  # tool-result messages carry the user role but no text: skip them
        msgs = [m for m in msgs if m[1].strip()] or msgs
    role, text, tc = msgs[-1]
    if want == "assistant" and tc:
        return None, True
    text = text.strip()
    return (text or None), tc


def find_key(obj, keys, depth=6):
    """Depth-limited DFS for the first non-empty value of any key in `keys` inside dicts/lists/JSON strings."""
    if depth < 0:
        return None
    obj = jloads(obj)
    if isinstance(obj, dict):
        for k in keys:
            if obj.get(k) not in MISSING:
                return obj[k]
        for v in obj.values():
            if isinstance(v, (dict, list)):
                r = find_key(v, keys, depth - 1)
                if r is not None:
                    return r
    elif isinstance(obj, list):
        for v in obj[:20]:
            r = find_key(v, keys, depth - 1)
            if r is not None:
                return r
    return None


# ---------------------------------------------------------------- record readers

def iter_files(paths):
    """Expand files / directories (recursive) to a sorted list of readable export files."""
    out = []
    for p in ([paths] if isinstance(paths, str) else list(paths)):
        p = str(p)
        if os.path.isdir(p):
            for root, _, names in os.walk(p):
                for n in sorted(names):
                    base = n[:-3] if n.lower().endswith(".gz") else n
                    if base.lower().endswith(EXTS) and not n.startswith("."):
                        out.append(os.path.join(root, n))
        elif os.path.isfile(p):
            out.append(p)
        else:
            raise FileNotFoundError("source not found: %s" % p)
    return sorted(set(out))


def _open(path):
    if path.lower().endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8-sig", errors="replace", newline="")
    return open(path, "r", encoding="utf-8-sig", errors="replace", newline="")


def _iter_array(f, buf):
    dec = json.JSONDecoder()
    pos = buf.index("[") + 1
    while True:
        while True:
            while pos < len(buf) and buf[pos] in " \t\r\n,":
                pos += 1
            if pos < len(buf):
                break
            more = f.read(1 << 22)
            if not more:
                return
            buf, pos = buf[pos:] + more, 0
        if buf[pos] == "]":
            return
        try:
            obj, end = dec.raw_decode(buf, pos)
        except ValueError:
            more = f.read(1 << 22)
            if not more:
                raise
            buf, pos = buf[pos:] + more, 0
            continue
        yield obj
        pos = end


_WRAP = ("data", "runs", "spans", "rows", "items", "records", "results", "Records", "entries", "value", "logs")
_RECORD_HINTS = ("span_id", "spanId", "trace_id", "traceId", "run_type", "attributes", "id", "SpanId")


def _otlp_val(v):
    if isinstance(v, dict):
        for k in ("stringValue", "intValue", "doubleValue", "boolValue", "string_value", "int_value"):
            if k in v:
                x = v[k]
                return x.get("value") if isinstance(x, dict) and "value" in x else x
        if "arrayValue" in v or "kvlistValue" in v:
            return v
    return v


def otlp_attrs(a):
    """Attributes as dict from: dict, OTLP list [{key, value:{stringValue}}], or GCP {attributeMap:{k:{stringValue}}}."""
    a = jloads(a)
    if isinstance(a, dict):
        if isinstance(a.get("attributeMap"), dict):
            a = a["attributeMap"]
            return {k: _otlp_val(v) for k, v in a.items()}
        return a
    if isinstance(a, list):
        return {x["key"]: _otlp_val(x.get("value")) for x in a if isinstance(x, dict) and "key" in x}
    return {}


def _expand(obj):
    if not isinstance(obj, dict):
        return
    if "resourceSpans" in obj:
        for rs in obj["resourceSpans"] or []:
            res = otlp_attrs((rs.get("resource") or {}).get("attributes"))
            for ss in rs.get("scopeSpans") or rs.get("instrumentationLibrarySpans") or []:
                for sp in ss.get("spans") or []:
                    sp = dict(sp)
                    sp["_resource"] = res
                    yield sp
        return
    if isinstance(obj.get("tables"), list):  # Azure Log Analytics / App Insights query API
        for t in obj["tables"]:
            cols = [c.get("name") if isinstance(c, dict) else c for c in t.get("columns", [])]
            for row in t.get("rows", []):
                yield dict(zip(cols, row))
        return
    if isinstance(obj.get("traces"), list):  # GCP Cloud Trace
        for tr in obj["traces"]:
            for sp in tr.get("spans", []):
                sp = dict(sp)
                sp.setdefault("traceId", tr.get("traceId"))
                yield sp
        return
    if not any(h in obj for h in _RECORD_HINTS):
        for k in _WRAP:
            if isinstance(obj.get(k), list):
                for x in obj[k]:
                    for y in _expand(x):
                        yield y
                return
    yield obj


def iter_records(paths, stats=None):
    """Yield one dict per record from every file. Auto-detects JSON array / single JSON object (with common
    wrappers: OTLP resourceSpans, Azure tables, GCP traces, {"data": [...]}) / JSONL / CSV / TSV; .gz allowed.
    Bad JSONL lines are skipped and counted in stats['bad_lines']."""
    files = iter_files(paths)
    if not files:
        raise FileNotFoundError("no .json/.jsonl/.ndjson/.csv/.tsv files found in: %s" % ", ".join(map(str, paths)))
    if stats is not None:
        stats.setdefault("files", []).extend(files)
        stats.setdefault("bad_lines", 0)
    for path in files:
        base = path.lower()[:-3] if path.lower().endswith(".gz") else path.lower()
        with _open(path) as f:
            if base.endswith((".csv", ".tsv")):
                for row in csv.DictReader(f, delimiter="\t" if base.endswith(".tsv") else ","):
                    yield row
                continue
            head = f.read(1 << 22)
            t = head.lstrip()
            if not t:
                continue
            if t[0] == "[":
                for obj in _iter_array(f, t):
                    if stats is not None and not isinstance(obj, dict):
                        stats["bad_lines"] += 1
                    for r in _expand(obj):
                        yield r
                continue
            f.seek(0)
            first = f.readline()
            try:
                json.loads(first)
                jsonl = True
            except ValueError:
                jsonl = False
            f.seek(0)
            if jsonl:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        if stats is not None:
                            stats["bad_lines"] += 1
                        continue
                    if stats is not None and not isinstance(obj, dict):
                        stats["bad_lines"] += 1
                    for r in _expand(obj):
                        yield r
            else:
                for r in _expand(json.load(f)):
                    yield r


def add_ingest_args(p):
    p.add_argument("--src", nargs="+", required=True, help="export file(s) or directories (json/jsonl/csv)")
    p.add_argument("--format", default="auto", choices=["auto", "otel", "langsmith", "flat"])
    p.add_argument("--map", dest="mapping", help="mapping.json for flat tables (see templates/mapping.example.json)")
    p.add_argument("--prices", help="prices.json: model -> {input_per_mtok, output_per_mtok}")
    p.add_argument("--noise-regex", help="drop spans whose name matches this regex (in addition to non-agent spans)")
    p.add_argument("--tool-regex", help="treat spans whose name matches this regex as tool calls")
    p.add_argument("--out", default="./procept-out")
    return p
