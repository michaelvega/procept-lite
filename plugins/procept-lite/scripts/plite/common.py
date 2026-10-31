"""Shared helpers for procept-lite ingestion (stdlib only).

Contents: tolerant record readers (JSON array / JSON object / JSONL / CSV / TSV, optionally .gz, files or
directories, streamed), timestamp parsing, text extraction from the many chat-message shapes found in
agent traces, redaction and clipping helpers.
"""
import csv
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
SECRET_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:sk|pk|rk|ghp|gho|xox[bpa])[-_][A-Za-z0-9_\-]{16,}|\bBearer\s+[A-Za-z0-9._~+/\-]{6,}=*"
    r"|\bAKIA[0-9A-Z]{16}\b|\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]*"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|(?i:\b(?:password|passwd|pwd|pw|pass|secret|api[_-]?key|apikey|access[_-]?token|auth[_-]?token|token|client[_-]?secret)\b"
    r"[\"']?\s*[:=]\s*[\"']?)[^\s\"',;}&]{4,}")


SECRET_KEY_RE = re.compile(r"(?i)pass(?:word|wd)?|pwd|secret|token|api[_-]?key|apikey|authorization|credential|private[_-]?key")
PHONE_RE = re.compile(r"(?<![\w.])\+?\d[\d ().\-]{7,}\d(?![\w.])")


CARD_RE = re.compile(r"(?<![\d.])\d(?:[ -]?\d){12,18}(?![\d.])")


def _luhn(digits):
    tot, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        tot += d
        alt = not alt
    return tot % 10 == 0


def _card_sub(m):
    digits = re.sub(r"\D", "", m.group(0))
    return "[card]" if 13 <= len(digits) <= 19 and _luhn(digits) else m.group(0)


def redact_secrets(s):
    """Replace API tokens / passwords / card numbers (not e-mails) with placeholders."""
    if not (isinstance(s, str) and s):
        return s
    return CARD_RE.sub(_card_sub, SECRET_RE.sub("[secret]", s))


def _phone_sub(m):
    return "[phone]" if 9 <= len(re.sub(r"\D", "", m.group(0))) <= 15 and re.search(r"[ ().+\-]", m.group(0)) else m.group(0)


def redact(s):
    """Replace e-mail addresses, API tokens, passwords and card numbers with placeholders."""
    return PHONE_RE.sub(_phone_sub, redact_secrets(EMAIL_RE.sub("[email]", str(s))))


# ---------------------------------------------------------------- sanitising (surrogates, control chars, depth)

_CTRL_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SURR_RE = re.compile("[\ud800-\udfff]")
MAX_STR = 400000
MAX_DEPTH = 60


def clean_text(s):
    """Remove lone surrogates / NUL / control characters; returns (text, changed)."""
    ch = False
    if _SURR_RE.search(s):
        s, ch = _SURR_RE.sub("\ufffd", s), True
    if _CTRL_RE.search(s):
        s, ch = _CTRL_RE.sub("", s), True
    return s, ch


def sanitize(o, _d=0, _st=None):
    """Return a copy of a parsed JSON value that is safe to write and process: strings cleaned, NaN/inf -> None,
    nesting deeper than MAX_DEPTH cut, giant strings clipped. Counters are noted in the cleaning ledger."""
    import cleaning as CL
    st = _st if _st is not None else {"text": 0, "deep": 0, "nan": 0, "big": 0}
    top = _st is None
    if isinstance(o, str):
        if len(o) > MAX_STR:
            o = o[:MAX_STR]
            st["big"] += 1
        o, ch = clean_text(o)
        st["text"] += ch
        r = o
    elif isinstance(o, float):
        if o != o or o in (float("inf"), float("-inf")):
            st["nan"] += 1
            r = None
        else:
            r = o
    elif isinstance(o, dict):
        if _d >= MAX_DEPTH:
            st["deep"] += 1
            r = None
        else:
            r = {}
            for k, v in o.items():
                if isinstance(k, str):
                    k, ch = clean_text(k)
                    st["text"] += ch
                r[k] = sanitize(v, _d + 1, st)
    elif isinstance(o, list):
        if _d >= MAX_DEPTH:
            st["deep"] += 1
            r = None
        else:
            r = [sanitize(v, _d + 1, st) for v in o]
    else:
        r = o
    if top:
        CL.note("text_sanitized", st["text"])
        CL.note("deep_nesting_truncated", st["deep"])
        CL.note("nan_to_null", st["nan"])
        CL.note("field_truncated", st["big"], "text over %d characters" % MAX_STR)
    return r


def safe_str(s):
    """A string that can always be encoded as UTF-8."""
    return clean_text(s)[0] if isinstance(s, str) else s


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


def _py_literal(t):
    """Python repr of a dict/list (single quotes, None/True/False) -> value, or None."""
    if len(t) > 2000000 or not (t[:1] in "{[" and t[-1:] in "}]"):
        return None
    if "'" not in t and "None" not in t and "True" not in t and "False" not in t:
        return None
    import ast
    try:
        v = ast.literal_eval(t)
    except (ValueError, SyntaxError, MemoryError, RecursionError, TypeError):
        return None
    return v if isinstance(v, (dict, list)) else None


def _pint(s):
    return int(s) if len(s) <= 30 else None


def loads_safe(t):
    """json.loads that tolerates NaN/Infinity, huge ints and deep nesting. Raises ValueError on bad JSON."""
    try:
        return json.loads(t, parse_int=_pint, parse_constant=lambda c: None)
    except RecursionError:
        raise ValueError("too deeply nested")


def jloads(v):
    """Parse v if it is a JSON (or Python-repr) object/array in a string, else return it unchanged."""
    if isinstance(v, str):
        t = v.strip()
        if t[:1] in ("{", "["):
            try:
                return loads_safe(t)
            except ValueError:
                r = _py_literal(t)
                return v if r is None else r
        if t[:2] in ('"{', '"[') and t[-1:] == '"':  # double-encoded
            try:
                inner = loads_safe(t)
            except ValueError:
                return v
            return jloads(inner) if isinstance(inner, str) else v
    return v


def to_int(v):
    try:
        if isinstance(v, str):
            v = re.sub(r"(?<=\d)[\s_,\u00a0\u202f](?=\d{3}\b)", "", v.strip()).replace(",", "")
        return int(float(v))
    except (TypeError, ValueError, OverflowError):
        return 0


TOKEN_MAX = 50000000


def to_tokens(v):
    """Token count from a number/string; negative, absurd or non-numeric values are rejected (0) and logged."""
    if v is None or v in MISSING or isinstance(v, bool):
        return 0
    n = to_int(v)
    if n < 0 or n > TOKEN_MAX:
        import cleaning as CL
        CL.note("implausible_tokens")
        return 0
    return n


DUR_MAX = 31 * 86400.0


def ok_dur(d):
    """Duration in seconds, or None (logged) when negative or longer than a month."""
    if d is None:
        return None
    if d < 0 or d > DUR_MAX:
        import cleaning as CL
        CL.note("implausible_duration")
        return None
    return d


def to_float(v):
    try:
        if isinstance(v, bool):
            return None
        if isinstance(v, str):
            v = re.sub(r"(?<=\d)[\s_,\u00a0\u202f](?=\d{3}\b)", "", v.strip().lstrip("$\u20ac\u00a3 ")).replace(",", "")
        f = float(v)
        return f if f == f and f not in (float("inf"), float("-inf")) else None
    except (TypeError, ValueError):
        return None


def write_json(path, obj):
    """Write JSON through a temp file and a rename, so a reader never sees a half-written file."""
    import checks
    checks.atomic_write(path, json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def load_json_file(path):
    with open(path, encoding="utf-8-sig") as f:
        return json.load(f)


# ---------------------------------------------------------------- strict mode: the mapping comes from a model

RESCUE_MIN_ROWS = 20


class RescueError(ValueError):
    """The mapping is missing or not accepted (no model reviewed it, or it is below the floor): one actionable line."""


def strict():
    """True unless the dev/test bypass is set: only a mapping written by a model's mapping rescue may be applied."""
    import checks
    return not checks.gates_disabled()


def no_mapping_message(out=None):
    d = out or "<out>"
    return ("no mapping.json yet: run `plite.py scaffold --src <your export> --out %s`, then have a Sonnet 5.5 / GPT 5.6 "
            "Terra model (or better) follow %s to write %s"
            % (d, os.path.join(d, "mapping_rescue", "README.md"), os.path.join(d, "mapping.json")))


def rescue_problem(mapping, n_raw=None):
    """None when `mapping` carries a valid _rescue block, else ONE line saying what is wrong and what to do.
    n_raw (the number of raw records in the export) enables the rows_reviewed >= min(20, n_raw) check."""
    import checks
    if not isinstance(mapping, dict):
        return "mapping.json must be a JSON object written by the mapping-rescue model (see mapping_rescue/README.md)"
    r = mapping.get("_rescue")
    if not isinstance(r, dict):
        return ("mapping.json has no _rescue block {\"model\": <id>, \"rows_reviewed\": <int>, \"notes\": <text>}: a model must "
                "review the raw rows and write it (see mapping_rescue/README.md)")
    model = r.get("model")
    if not checks.model_allowed(model):
        return ("_rescue.model %r is below the floor (Sonnet 5.5 or GPT 5.6 Terra, or better; never Haiku): redo the mapping "
                "rescue with an allowed model and put its id there" % (model,))
    rows = r.get("rows_reviewed")
    if isinstance(rows, bool) or not isinstance(rows, int) or rows < 0:
        return "_rescue.rows_reviewed must be a whole number: how many raw rows the model compared against the mapping"
    if n_raw is not None and rows < min(RESCUE_MIN_ROWS, n_raw):
        return ("_rescue.rows_reviewed is %d but at least %d raw rows must be reviewed (the export has %d): review more "
                "rows, then update the number" % (rows, min(RESCUE_MIN_ROWS, n_raw), n_raw))
    if r.get("notes") is not None and not isinstance(r.get("notes"), str):
        return "_rescue.notes must be text"
    return None


# ---------------------------------------------------------------- time

_TS_RE = re.compile(
    r"^\s*(\d{4})-(\d\d)-(\d\d)[T ](\d\d):(\d\d):(\d\d)(?:[.,](\d+))?\s*(Z|UTC|[+-]\d\d(?::?\d\d)?)?\s*$")


TS_MIN, TS_MAX = 946684800.0, 4102444800.0  # 2000-01-01 .. 2100-01-01


def to_epoch(v, fmt=None, note=True):
    """Seconds since epoch (float) or None; values outside 2000..2100 (epoch 0, year 1, year 9999) are missing.
    See _to_epoch for the accepted forms."""
    r = _to_epoch(v, fmt)
    if r is None:
        if note and v not in MISSING and not (isinstance(v, str) and not v.strip()) and not isinstance(v, bool):
            import cleaning as CL
            CL.note("bad_timestamp")
        return None
    if not (TS_MIN <= r <= TS_MAX):
        if note:
            import cleaning as CL
            CL.note("implausible_timestamp")
        return None
    return r


def _to_epoch(v, fmt=None):
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
        if n != n or n in (float("inf"), float("-inf")):
            return None
        a = abs(n)
        if a >= 1e11:
            import cleaning as CL
            CL.note("epoch_units_normalized")
        if a >= 1e17:
            n = n / 1e9
        elif a >= 1e14:
            n = n / 1e6
        elif a >= 1e11:
            n = n / 1e3
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
        return _loose_epoch(v)
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


_LOOSE_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d %H:%M:%S", "%m/%d/%Y %H:%M:%S",
                  "%m/%d/%Y %H:%M", "%d/%m/%Y %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%d %b %Y %H:%M:%S",
                  "%b %d, %Y %I:%M:%S %p", "%a, %d %b %Y %H:%M:%S %z", "%a %b %d %H:%M:%S %Y",
                  "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S%z", "%Y%m%dT%H%M%SZ", "%a, %d %b %Y %H:%M:%S",
                  "%d %b %Y", "%b %d %Y", "%Y.%m.%d %H:%M:%S", "%d.%m.%Y %H:%M:%S", "%d-%m-%Y %H:%M:%S",
                  "%Y-%m-%d %I:%M:%S %p", "%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y", "%Y/%m/%d")


def _loose_epoch(v):
    t = v.strip()
    if not t or len(t) > 64:
        return None
    t = re.sub(r"\s*\(?(UTC|GMT)\)?$", "", t)
    for fmt in _LOOSE_FORMATS:
        try:
            d = datetime.strptime(t, fmt)
        except ValueError:
            continue
        return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()
    try:
        d = datetime.fromisoformat(t.replace("Z", "+00:00"))
        return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()
    except ValueError:
        return None


def iso(ts):
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    except (OverflowError, ValueError, OSError):
        return None


_DUR_RE = re.compile(r"^\s*(-?\d+(?:[.,]\d+)?)\s*(ns|nanos?|nanoseconds?|us|\u00b5s|micros?|microseconds?|ms|msecs?|"
                     r"millis?|milliseconds?|s|secs?|seconds?|m|mins?|minutes?|h|hrs?|hours?)\s*$", re.I)
_DUR_MULT = {"ns": 1e-9, "us": 1e-6, "\u00b5s": 1e-6, "ms": 1e-3, "s": 1.0, "m": 60.0, "h": 3600.0}


def _dur_suffix(v):
    """'1.2s' / '350ms' / '2 min' -> seconds, else None."""
    if not isinstance(v, str):
        return None
    m = _DUR_RE.match(v)
    if not m:
        return None
    u = m.group(2).lower()
    key = "ns" if u.startswith("nano") else "us" if u.startswith(("micro", "\u00b5")) or u == "us" else \
        "ms" if u.startswith(("ms", "milli")) else "h" if u.startswith("h") else \
        "m" if u.startswith("m") else "s"
    return float(m.group(1).replace(",", ".")) * _DUR_MULT[key]


def parse_duration_s(v, unit="s"):
    """Duration in seconds from a number, numeric string, '1.2s'/'350ms'/'2 min' or 'HH:MM:SS.fff'.
    unit in s|ms|us|ns applies to bare numbers."""
    if v in MISSING:
        return None
    sfx = _dur_suffix(v)
    if sfx is not None:
        return sfx
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


_ERR_NULLS = {"", "n/a", "na", "none", "null", "nil", "false", "0", "no", "ok", "{}", "[]", "nan", "-", "--",
              "undefined", "success", "succeeded", "passed", "0.0", "false.", "no error", "no errors", "empty", "ok."}


def is_error_literal(v):
    """True for values that mean 'no error' (None, N/A, false, 0, blank, {}, [] ...)."""
    if v is None or v is False:
        return True
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v == 0 or v != v
    if isinstance(v, (dict, list)):
        return not v
    return str(v).strip().lower() in _ERR_NULLS


def real_error(v):
    """True when v is a real error value; literal 'no error' markers (N/A, false, 0, {} ...) are logged and ignored."""
    if v is None:
        return False
    if is_error_literal(v):
        if not (isinstance(v, str) and v.strip() == "") and v is not False:
            import cleaning as CL
            CL.note("error_literal_ignored")
        return False
    return True


class Span(object):
    __slots__ = ("msg_tools", "tid", "sid", "pid", "name", "start", "end", "dur", "kind", "call", "conv", "agent", "env", "tool",
                 "args", "result", "utext", "atext", "itok", "otok", "model", "cost", "err", "cancelled",
                 "has_status", "fmt", "seq", "run", "src", "user", "final", "err_derived")

    def __init__(self, fmt):
        for s in self.__slots__:
            setattr(self, s, None)
        self.fmt = fmt
        self.cancelled = False
        self.has_status = False
        self.final = False
        self.err_derived = False


# ---------------------------------------------------------------- record readers (see ingest.py)

def iter_files(paths):
    import ingest
    return ingest.iter_files(paths)


def iter_records(paths, stats=None):
    import ingest
    return ingest.iter_records(paths, stats)


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


def add_ingest_args(p):
    p.add_argument("--src", nargs="+", required=True, help="export file(s) or directories (json/jsonl/csv)")
    p.add_argument("--format", default="auto", choices=["auto", "otel", "langsmith", "flat"])
    p.add_argument("--map", dest="mapping", help="mapping.json written by the mapping rescue (default: <out>/mapping.json; "
                                                 "see templates/mapping.example.json)")
    p.add_argument("--prices", help="prices.json: model -> {input_per_mtok, output_per_mtok}")
    p.add_argument("--noise-regex", help="drop spans whose name matches this regex (in addition to non-agent spans)")
    p.add_argument("--tool-regex", help="treat spans whose name matches this regex as tool calls")
    p.add_argument("--out", default="./procept-out")
    return p
