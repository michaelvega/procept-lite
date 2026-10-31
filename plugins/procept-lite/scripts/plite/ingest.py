"""ingest: layered, never-crashing reader. bytes -> (unpack) -> text -> (parse) -> records.

Layers
  1. unpack   gzip / bz2 / xz / zip (nested), directories (recursive), ignored extensions
  2. decode   BOM, UTF-16/32 (with or without BOM), UTF-8 with Windows-1252 fallback, binary detection
  3. parse    JSON stream (array, object, ndjson, concatenated values, truncated tails, double-encoded strings,
              JSON embedded in plain log lines) or delimited table (comma/tab/semicolon/pipe sniffed, preamble
              lines, broken quotes, ragged rows)
  4. wrap     record wrappers unwrapped (OTLP, Azure tables, GCP traces, Jaeger, CloudWatch @message, ADK events)
Every drop/repair is counted in the cleaning ledger. Unreadable files are noted and skipped, never fatal.
"""
import ast
import base64
import bz2
import codecs
import csv
import gzip
import io
import json
import lzma
import os
import re
import zipfile
import zlib
from collections import Counter

import cleaning as CL
from common import loads_safe, sanitize

IGNORED_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ico", ".svg", ".pdf", ".docx", ".xlsx", ".pptx",
               ".doc", ".xls", ".ppt", ".exe", ".dll", ".so", ".dylib", ".pyc", ".class", ".jar", ".mp3", ".mp4",
               ".mov", ".avi", ".wav", ".parquet", ".db", ".sqlite", ".ds_store", ".woff", ".woff2", ".ttf"}
TABLE_EXT = {".csv", ".tsv", ".psv", ".tab"}
MAX_MEMBER = 1 << 31

_WS = re.compile(r"[\s,\]]*")
_CW_KEYS = {"timestamp", "message", "ingestionTime", "eventId", "logStreamName", "logGroupName", "logGroup", "id",
            "extractedFields",
            "@timestamp", "@message", "@logStream", "@log", "@ptr", "@ingestionTime", "@entity"}
_WRAP = ("data", "runs", "spans", "rows", "items", "records", "results", "Records", "entries", "value", "logs",
         "logEntries", "traces", "segments", "Segments", "hits", "documents", "result", "response", "payload", "Items",
         "logEvents", "Traces", "examples", "events_list", "log_entries", "log_events")
_NO_UNWRAP = {"messages", "tools", "tool_calls", "toolCalls", "events", "children", "child_runs", "content", "parts",
              "choices", "inputs", "outputs", "input", "output", "steps", "attributes", "metadata", "usage", "errors",
              "tags", "extra", "kwargs", "generations", "history", "calls", "spans_list"}
_RS_KEYS = ("resourceSpans", "resource_spans", "batches")
_SS_KEYS = ("scopeSpans", "scope_spans", "instrumentationLibrarySpans", "instrumentation_library_spans",
            "libraryspans", "scopeSpan")
_CONTENTY = re.compile(r"user|question|prompt|answer|assistant|input|output|message|content|text|query|reply|tool",
                       re.I)
_B64 = re.compile(r"^[A-Za-z0-9+/]{11}=$|^[A-Za-z0-9+/]{22}==$|^[A-Za-z0-9+/]{12}$|^[A-Za-z0-9+/]{24}$")


def _id_hex(v):
    """OTLP protobuf-JSON carries ids as base64; convert to hex so they match other exports."""
    if isinstance(v, str) and _B64.match(v) and not re.match(r"^[0-9a-fA-F]+$", v):
        try:
            raw = base64.b64decode(v + "=" * (-len(v) % 4))
            if len(raw) in (8, 16):
                return raw.hex()
        except Exception:  # noqa: BLE001
            return v
    return v
_RECORD_HINTS = ("span_id", "spanId", "trace_id", "traceId", "run_type", "attributes", "id", "SpanId", "jsonPayload",
                 "textPayload", "protoPayload", "insertId", "messages")


# ---------------------------------------------------------------- layer 1: unpack

def _unpack(name, data, depth=0):
    """Yield (name, bytes) leaves after opening compressed containers."""
    if depth > 4:
        yield name, data
        return
    head = data[:6]
    low = name.lower()
    if head[:2] == b"\x1f\x8b":
        try:
            raw = gzip.decompress(data)
        except Exception:
            try:
                raw = zlib.decompressobj(31).decompress(data)
                if raw:
                    CL.note("gzip_partial", 1, os.path.basename(name))
            except Exception:
                raw = b""
            if not raw:
                CL.note("unreadable_file", 1, "%s (corrupt gzip)" % os.path.basename(name))
                return
        else:
            CL.note("gzip_expanded")
        inner = name[:-3] if low.endswith(".gz") else name
        for x in _unpack(inner, raw, depth + 1):
            yield x
        return
    if head[:3] == b"BZh":
        try:
            raw = bz2.decompress(data)
        except Exception:
            CL.note("unreadable_file", 1, "%s (corrupt bz2)" % os.path.basename(name))
            return
        CL.note("gzip_expanded")
        for x in _unpack(name[:-4] if low.endswith(".bz2") else name, raw, depth + 1):
            yield x
        return
    if head[:6] == b"\xfd7zXZ\x00":
        try:
            raw = lzma.decompress(data)
        except Exception:
            CL.note("unreadable_file", 1, "%s (corrupt xz)" % os.path.basename(name))
            return
        CL.note("gzip_expanded")
        for x in _unpack(name[:-3] if low.endswith(".xz") else name, raw, depth + 1):
            yield x
        return
    if head[:4] == b"PK\x03\x04" or head[:4] == b"PK\x05\x06":
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
        except Exception:
            CL.note("unreadable_file", 1, "%s (corrupt zip)" % os.path.basename(name))
            return
        CL.note("zip_expanded")
        for info in sorted(zf.infolist(), key=lambda i: i.filename):
            n = info.filename
            parts = n.replace("\\", "/").split("/")
            if info.is_dir():
                continue
            if ".." in parts or n.startswith(("/", "\\")) or (len(n) > 1 and n[1] == ":"):
                CL.note("unsafe_zip_member", 1, "member with an unsafe path in %s" % os.path.basename(name))
                continue
            if any(p.startswith(".") or p == "__MACOSX" for p in parts):
                CL.note("hidden_member_skipped")
                continue
            if info.file_size > MAX_MEMBER:
                CL.note("unreadable_file", 1, "%s (too large)" % os.path.basename(n))
                continue
            try:
                raw = zf.read(info)
            except Exception:
                CL.note("unreadable_file", 1, "%s in zip (cannot extract)" % os.path.basename(n))
                continue
            for x in _unpack(name + "!" + n, raw, depth + 1):
                yield x
        return
    yield name, data


# ---------------------------------------------------------------- layer 2: decode

def decode_bytes(data, label=""):
    """bytes -> text, or None when the content is binary. Counts repairs."""
    if data.startswith(codecs.BOM_UTF8):
        CL.note("bom_stripped")
        data = data[3:]
        enc = "utf-8"
    elif data.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        CL.note("utf16_decoded")
        try:
            return data.decode("utf-32")
        except Exception:
            return None
    elif data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        CL.note("utf16_decoded")
        try:
            return data.decode("utf-16", "replace")
        except Exception:
            return None
    else:
        enc = "utf-8"
        head = data[:8192]
        if b"\x00" in head:
            half = len(head) // 2
            odd = head[1::2].count(0)
            even = head[0::2].count(0)
            if half and odd > 0.3 * half and even < 0.1 * half:
                CL.note("utf16_decoded")
                return data.decode("utf-16-le", "replace")
            if half and even > 0.3 * half and odd < 0.1 * half:
                CL.note("utf16_decoded")
                return data.decode("utf-16-be", "replace")
            if data.count(b"\x00") > 0.1 * len(data):
                return None
            data = data.replace(b"\x00", b"")  # NUL padding inside otherwise normal text
            CL.note("nul_stripped")
    try:
        text = data.decode(enc)
    except UnicodeDecodeError:
        try:
            text = data.decode("cp1252")
        except UnicodeDecodeError:
            text = data.decode("latin-1")
        CL.note("encoding_fallback", 1, label or None)
    sample = text[:8192]
    if sample:
        bad = sum(1 for ch in sample if ord(ch) < 32 and ch not in "\t\r\n\f\v")
        if bad > 0.05 * len(sample) or "\x00" in sample:
            return None
    if "\ufeff" in text:
        CL.note("bom_stripped", text.count("\ufeff"))
        text = text.replace("\ufeff", "")
    if "\r" in text and "\n" not in text:
        text = text.replace("\r", "\n")
        CL.note("line_endings_normalized")
    return text


# ---------------------------------------------------------------- layer 3a: JSON stream

def decode_json_string(s):
    """Parse s (a string that should hold JSON) up to 3 levels. Returns the parsed object or None."""
    cur = s
    for _ in range(3):
        if not isinstance(cur, str):
            return cur if cur is not None else None
        t = cur.strip()
        if not t or t[0] not in '{["':
            return None
        try:
            cur = loads_safe(t)
        except ValueError:
            rv = py_repr(t) if t[0] in "{[" else None
            if rv is None:
                return None
            CL.note("python_repr_decoded")
            return rv
    return cur if not isinstance(cur, str) else None


def repair_truncated(text):
    """Close a JSON value that was cut off mid-way: keep everything up to the last complete element."""
    if len(text) > 60000000:
        return None
    stack, in_str, esc, safe = [], False, False, None
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if not stack:
                break
            stack.pop()
            safe = (i + 1, "".join("}" if c == "{" else "]" for c in reversed(stack)))
        elif ch == "," and stack:
            safe = (i, "".join("}" if c == "{" else "]" for c in reversed(stack)))
    if safe is None:
        return None
    try:
        return json.loads(text[:safe[0]] + safe[1])
    except ValueError:
        return None


_COL0 = re.compile(r"\n[\[{]")
_TOK = re.compile(r'"[^"\\]*(?:\\.[^"\\]*)*"|[\[\]{}]')
_DEC_STATE = {"huge": 0, "nan": 0}


def _pint(s):
    if len(s) > 30:
        _DEC_STATE["huge"] += 1
        return None
    return int(s)


def _pconst(c):
    _DEC_STATE["nan"] += 1
    return None


_DEC = json.JSONDecoder(parse_int=_pint, parse_constant=_pconst)


def cap_depth(s, limit=48):
    """Replace every container nested deeper than `limit` with null so pathological nesting cannot crash parsing."""
    out, last, depth, skipping = [], 0, 0, False
    for m in _TOK.finditer(s):
        c = m.group()[0]
        if c == '"':
            continue
        if c in "[{":
            depth += 1
            if depth == limit + 1 and not skipping:
                out.append(s[last:m.start()])
                out.append("null")
                skipping = True
        else:
            if skipping and depth == limit + 1:
                last = m.end()
                skipping = False
            depth -= 1
    if not skipping:
        out.append(s[last:])
    return "".join(out)


def py_repr(seg):
    """One Python dict/list repr (single quotes, None/True/False) -> value or None."""
    seg = seg.strip().rstrip(",")
    if len(seg) > 3000000 or seg[:1] not in "{[" or seg[-1:] not in "}]":
        return None
    try:
        v = ast.literal_eval(seg)
    except (ValueError, SyntaxError, MemoryError, RecursionError, TypeError):
        return None
    return v if isinstance(v, (dict, list)) else None


def json_values(text, stats):
    """Yield every top-level JSON value of a (possibly damaged) text."""
    dec = _DEC
    n = len(text)
    pos = 0
    nvals = 0
    salvaged = False
    while True:
        pos = _WS.match(text, pos).end()
        if pos >= n:
            break
        c = text[pos]
        if c == "}":
            pos += 1
            continue
        try:
            val, end = dec.raw_decode(text, pos)
            failed = not isinstance(val, (dict, list, str))
        except RecursionError:
            text = text[:pos] + cap_depth(text[pos:])
            n = len(text)
            CL.note("deep_nesting_truncated")
            continue
        except ValueError:
            failed = True
            if c == "[":
                if not salvaged:
                    CL.note("array_salvaged")
                    salvaged = True
                pos += 1
                continue
        if failed and c in "{[":
            e0 = text.find("\n", pos)
            rv = py_repr(text[pos:e0 if e0 != -1 else n])
            if rv is not None:
                CL.note("python_repr_decoded")
                nvals += 1
                yield rv
                pos = e0 + 1 if e0 != -1 else n
                continue
        if failed and c == "{" and nvals == 0 and not _COL0.search(text, pos + 1):
            fixed = repair_truncated(text[pos:])
            if fixed is not None:
                CL.note("array_salvaged")
                nvals += 1
                yield fixed
                break
        if failed:
            e = text.find("\n", pos)
            if e == -1 or not text[e + 1:].strip():
                CL.note("truncated_tail" if c in "{[\"" else "noise_line")
                stats["bad_lines"] = stats.get("bad_lines", 0) + 1
                break
            seg = text[pos:e]
            idx = seg.find("{")
            if idx > 0 and '"' not in seg[:idx]:
                try:
                    val, rel = dec.raw_decode(seg, idx)
                    CL.note("embedded_json")
                    nvals += 1
                    yield val
                    pos = pos + rel
                    continue
                except ValueError:
                    pass
            if c in "{[\"" or idx == 0:
                CL.note("bad_line")
            else:
                CL.note("noise_line")
            stats["bad_lines"] = stats.get("bad_lines", 0) + 1
            pos = e + 1
            continue
        nvals += 1
        yield val
        pos = end
    if nvals > 1 and text.lstrip()[:1] == "{" and "\n" not in text.strip():
        CL.note("concatenated_json")
    if _DEC_STATE["huge"]:
        CL.note("huge_number", _DEC_STATE["huge"])
    if _DEC_STATE["nan"]:
        CL.note("nan_to_null", _DEC_STATE["nan"])
    _DEC_STATE["huge"] = _DEC_STATE["nan"] = 0


# ---------------------------------------------------------------- layer 4: wrappers

def _cloudwatch(obj):
    """dict -> unwrapped dict | False (noise to drop) | None (not a CloudWatch row)."""
    if "@message" in obj:
        m = obj.get("@message")
    elif "message" in obj and set(obj) <= _CW_KEYS and isinstance(obj.get("message"), str):
        m = obj["message"]
    else:
        return None
    ts = obj.get("@timestamp") or obj.get("timestamp")
    parsed = decode_json_string(m) if isinstance(m, str) else m
    if isinstance(parsed, dict):
        rec = dict(parsed)
        if ts not in (None, "") and not any(k in rec for k in ("timestamp", "@timestamp", "time", "eventTime",
                                                             "start_time", "startTime", "ts")):
            rec["@timestamp"] = ts
        CL.note("cloudwatch_message_unwrapped")
        return rec
    return False


def _otlp_val(v):
    if isinstance(v, dict):
        for k in ("stringValue", "intValue", "doubleValue", "boolValue", "string_value", "int_value"):
            if k in v:
                x = v[k]
                return x.get("value") if isinstance(x, dict) and "value" in x else x
        if "arrayValue" in v or "kvlistValue" in v:
            return v
    return v


def _rows_of_fields(item):
    """CloudWatch Insights API row [{field, value}, ...] -> dict, else None."""
    if isinstance(item, list) and item and all(isinstance(c, dict) and "field" in c and "value" in c for c in item):
        return {c["field"]: c["value"] for c in item if c["field"] != "@ptr"}
    return None


def _columnar(obj):
    """{columns:[{name, values:[...]}]} or {columns:[names], rows:[[...]]} -> list of row dicts, else None."""
    cols = obj.get("columns")
    if not (isinstance(cols, list) and cols):
        return None
    if all(isinstance(c, dict) and isinstance(c.get("values"), list) and "name" in c for c in cols):
        n = max(len(c["values"]) for c in cols)
        return [{c["name"]: (c["values"][i] if i < len(c["values"]) else None) for c in cols} for i in range(n)]
    rows = obj.get("rows")
    if isinstance(rows, list) and rows and all(isinstance(r, list) for r in rows):
        names = [c.get("name") if isinstance(c, dict) else c for c in cols]
        return [dict(zip(names, r)) for r in rows]
    return None


def _sub_records(v, depth):
    """Records found inside one value (dict / list / JSON string), or [] when it holds no wrapped records."""
    if isinstance(v, str):
        if len(v) < 20 or v.lstrip()[:1] not in "{[":
            return []
        v = decode_json_string(v)
        if v is None:
            return []
    out = []
    if isinstance(v, dict):
        rs = list(expand(v, depth + 1))
        if rs and not (len(rs) == 1 and rs[0] is v):
            return rs
        return []
    if isinstance(v, list) and v:
        strong = False
        for it in v[:100000]:
            row = _rows_of_fields(it)
            if row is not None:
                strong = True
                out.extend(expand(row, depth + 1))
            elif isinstance(it, dict):
                rs = list(expand(it, depth + 1))
                if len(rs) != 1 or rs[0] is not it:
                    strong = True
                out.extend(rs)
            elif isinstance(it, str) and len(it) > 20 and it.lstrip()[:1] == "{":
                d = decode_json_string(it)
                if isinstance(d, dict):
                    strong = True
                    out.extend(expand(d, depth + 1))
        if not out:
            return []
        dicts = [x for x in v if isinstance(x, dict)]
        if not strong and len(dicts) >= 2:
            common = set(dicts[0])
            for d in dicts[1:50]:
                common &= set(d)
            strong = len(common) >= 2 and len(dicts) == len(v)
        return out if strong else []
    return []


def _nested(obj, depth):
    """Records hidden inside an envelope object (few scalar keys + one container holding the real rows)."""
    if depth > 4 or len(obj) > 10:
        return []
    for k, v in obj.items():
        if (isinstance(v, str) and len(v) > 160 and v.lstrip()[:1] not in "{[") or _CONTENTY.search(str(k)):
            return []
    best = []
    for k, v in obj.items():
        if k in _NO_UNWRAP or not isinstance(v, (dict, list, str)):
            continue
        rs = _sub_records(v, depth)
        if len(rs) > len(best):
            best = rs
    return best


def expand(obj, depth=0):
    """One parsed dict -> iterator of record dicts (unwrapping common export envelopes)."""
    if not isinstance(obj, dict) or depth > 8:
        return
    cw = _cloudwatch(obj)
    if cw is False:
        CL.note("noise_line")
        return
    if cw is not None:
        for r in expand(cw, depth + 1):
            yield r
        return
    if isinstance(obj.get("Document"), str) and set(obj) <= {"Id", "Document", "id"}:  # X-Ray BatchGetTraces
        d = decode_json_string(obj["Document"])
        if isinstance(d, dict):
            CL.note("wrapper_unwrapped")
            for r in expand(d, depth + 1):
                yield r
            return
    rsk = next((k for k in _RS_KEYS if isinstance(obj.get(k), list)), None)
    if rsk:
        from common import otlp_attrs
        n = 0
        for rs in obj[rsk] or []:
            if not isinstance(rs, dict):
                continue
            res = otlp_attrs((rs.get("resource") or {}).get("attributes"))
            sk = next((k for k in _SS_KEYS if isinstance(rs.get(k), list)), None)
            for ss in (rs.get(sk) if sk else None) or []:
                for sp in (ss.get("spans") if isinstance(ss, dict) else None) or []:
                    if not isinstance(sp, dict):
                        continue
                    sp = dict(sp)
                    for idk in ("traceId", "trace_id", "spanId", "span_id", "parentSpanId", "parent_span_id"):
                        if idk in sp:
                            sp[idk] = _id_hex(sp[idk])
                    sp["_resource"] = res
                    n += 1
                    yield sp
        if rsk != "resourceSpans":
            CL.note("wrapper_unwrapped")
        return
    col = _columnar(obj)
    if col is not None and "tables" not in obj:
        CL.note("columnar_transposed")
        for row in col:
            for r in expand(row, depth + 1):
                yield r
        return
    if isinstance(obj.get("tables"), list):  # Azure Log Analytics / App Insights query API
        for t in obj["tables"]:
            cols = [c.get("name") if isinstance(c, dict) else c for c in t.get("columns", [])]
            for row in t.get("rows", []):
                yield dict(zip(cols, row))
        return
    if isinstance(obj.get("traces"), list) and obj["traces"] and isinstance(obj["traces"][0], dict) \
            and "spans" in obj["traces"][0]:  # GCP Cloud Trace
        for tr in obj["traces"]:
            for sp in tr.get("spans", []):
                sp = dict(sp)
                sp.setdefault("traceId", tr.get("traceId"))
                yield sp
        return
    sp = obj.get("spans")
    if isinstance(sp, list) and sp and isinstance(sp[0], dict) and not any(k in obj for k in ("spanId", "span_id", "spanID")):
        tid = obj.get("traceID") or obj.get("traceId") or obj.get("trace_id")
        procs = obj.get("processes") if isinstance(obj.get("processes"), dict) else {}
        for s in sp:
            s = dict(s)
            if tid and not (s.get("traceID") or s.get("traceId") or s.get("trace_id")):
                s["traceID"] = tid
            if procs and s.get("processID") in procs:
                s.setdefault("_service", (procs[s["processID"]] or {}).get("serviceName"))
            yield s
        return
    ev = obj.get("events")
    if isinstance(ev, list) and ev and isinstance(ev[0], dict) and \
            any(isinstance(e, dict) and ("content" in e or "invocation_id" in e or "invocationId" in e) for e in ev[:5]) \
            and not any(h in obj for h in ("span_id", "spanId", "trace_id", "traceId", "run_type")):
        ctx = {k: v for k, v in obj.items() if k != "events" and not isinstance(v, (dict, list)) and k != "id"}
        for e in ev:
            if isinstance(e, dict):
                e = dict(e)
                for k, v in ctx.items():
                    e.setdefault(k, v)
                yield e
        return
    if not any(h in obj for h in _RECORD_HINTS):
        for k in _WRAP:
            v = obj.get(k)
            if isinstance(v, list) and v and any(isinstance(x, (dict, str)) for x in v):
                for x in v:
                    if isinstance(x, dict):
                        for y in expand(x, depth + 1):
                            yield y
                    elif isinstance(x, str):
                        d = decode_json_string(x)
                        if isinstance(d, dict):
                            for y in expand(d, depth + 1):
                                yield y
                return
        got = _nested(obj, depth)
        if got:
            CL.note("wrapper_unwrapped")
            for y in got:
                yield y
            return
    yield obj


def _emit(v, stats, depth=0):
    """A parsed JSON value -> record dicts (lists flattened, JSON-in-strings decoded)."""
    if isinstance(v, str):
        d = decode_json_string(v)
        if d is None:
            CL.note("non_record_value")
            stats["bad_lines"] = stats.get("bad_lines", 0) + 1
            return
        CL.note("double_encoded")
        v = d
    if isinstance(v, list):
        got = 0
        junk = 0
        for x in v:
            if isinstance(x, (dict, list)) or (isinstance(x, str) and decode_json_string(x) is not None):
                for r in _emit(x, stats, depth + 1):
                    got += 1
                    yield r
            else:
                junk += 1
        if junk and (not got):
            CL.note("non_record_value")
            stats["bad_lines"] = stats.get("bad_lines", 0) + 1
        elif junk:
            CL.note("non_record_value", junk)
            stats["bad_lines"] = stats.get("bad_lines", 0) + junk
        return
    if isinstance(v, dict):
        for r in expand(v):
            yield r
        return
    CL.note("non_record_value")
    stats["bad_lines"] = stats.get("bad_lines", 0) + 1


# ---------------------------------------------------------------- layer 3b: tables

def _lenient(line, d):
    try:
        r = next(csv.reader([line], delimiter=d, quoting=csv.QUOTE_NONE), [])
    except csv.Error:
        r = line.split(d)
    return [x.strip().strip('"') for x in r]


def _fit(row, W, d):
    """Too many cells (a delimiter inside a broken-quote cell): merge the surplus into the JSON/quoted cell."""
    extra = len(row) - W
    at = None
    for k, c in enumerate(row):
        t = c.lstrip()
        if t[:1] in ('"', "{", "[") or t[:2] in ('"{', '"['):
            at = k
            break
    if at is not None and row[at].lstrip()[:1] in ("{", "["):
        try:
            if isinstance(json.loads(row[at]), (dict, list)):
                return row[:W]  # the JSON cell is intact: the surplus cells are trailing junk
        except ValueError:
            pass
    if at is None or at + extra >= len(row):
        at = W - 1
    merged = d.join(row[at:at + extra + 1])
    return row[:at] + [merged] + row[at + extra + 1:]


def _split(s, d):
    if '"' not in s:
        return s.split(d)
    try:
        return next(csv.reader([s], delimiter=d), [])
    except csv.Error:
        return _lenient(s, d)


def _sniff(lines, ext):
    prefer = {".tsv": "\t", ".psv": "|", ".tab": "\t"}.get(ext)
    best, best_key = None, None
    for d in (",", "\t", ";", "|"):
        widths = [len(_split(ln, d)) for ln in lines]
        modal, cnt = Counter(widths).most_common(1)[0]
        if modal < 2:
            continue
        frac = cnt / float(len(widths))
        key = (frac >= 0.5, round(frac, 2) + (0.3 if d == prefer and frac >= 0.5 else 0), modal)
        if best_key is None or key > best_key:
            best, best_key = (d, modal, frac), key
    return best


def _numlike(s):
    s = s.strip().replace(",", "")
    try:
        float(s)
        return True
    except ValueError:
        return False


def looks_tabular(text):
    lines = [ln.rstrip("\r") for ln in text.split("\n", 60)[:60] if ln.strip()]
    if len(lines) < 2:
        return False
    s = _sniff(lines, "")
    return bool(s and s[2] >= 0.7 and s[1] >= 2)


def _csv_rows(text, d, limit=None):
    rows = []
    try:
        for r in csv.reader(io.StringIO(text), delimiter=d):
            rows.append(r)
            if limit and len(rows) >= limit:
                break
    except csv.Error:
        pass
    return rows


def _rfc_delim(text, ext):
    """Pick the delimiter by parsing the head of the file as real CSV (quoted cells may span lines)."""
    prefer = {".tsv": "\t", ".psv": "|", ".tab": "\t"}.get(ext)
    sample = text[:400000]
    if len(text) > 400000 and sample.rfind("\n") > 0:
        sample = sample[:sample.rfind("\n")]
    best, best_key = None, None
    for d in (",", "\t", ";", "|"):
        rows = [r for r in _csv_rows(sample, d, 400) if any(c.strip() for c in r)]
        if not rows:
            continue
        modal, cnt = Counter(len(r) for r in rows).most_common(1)[0]
        if modal < 2:
            continue
        frac = cnt / float(len(rows))
        key = (frac >= 0.5, round(frac, 2) + (0.3 if d == prefer and frac >= 0.5 else 0), modal)
        if best_key is None or key > best_key:
            best, best_key = d, key
    return best


def _modal_width(rows):
    cnt = Counter(len(r) for r in rows[:3000] if any(c.strip() for c in r))
    cnt.pop(0, None)
    wide = [(c, w) for w, c in cnt.items() if w >= 2]
    return max(wide)[1] if wide else (cnt.most_common(1)[0][0] if cnt else 1)


def _lines_rows(text, d):
    """Fallback: split by physical lines, repairing broken quotes line by line. -> list of cell lists."""
    lines = text.split("\n")
    out = []
    n = len(lines)
    W = None
    sample = [_split(ln.rstrip("\r"), d) for ln in lines[:200] if ln.strip()]
    if sample:
        W = Counter(len(r) for r in sample).most_common(1)[0][0]
    i = 0
    while i < n:
        ln = lines[i].rstrip("\r")
        if not ln.strip():
            i += 1
            continue
        row = None
        if ln.count('"') % 2 == 0:
            row = _split(ln, d)
            i += 1
        else:
            j, buf, ok = i + 1, ln, False
            while j < n and j - i <= 40 and len(buf) < 500000:
                buf += "\n" + lines[j].rstrip("\r")
                if buf.count('"') % 2 == 0:
                    ok = True
                    break
                j += 1
            if ok and W:
                cand = _split(buf, d)
                own = 0
                for k in range(i + 1, j + 1):
                    ck = lines[k].rstrip("\r")
                    if ck.count('"') % 2 == 0 and len(_split(ck, d)) >= max(2, W - 1):
                        own += 1
                if len(cand) == W and not own:
                    row, i = cand, j + 1
            if row is None:
                row = ("lenient", _lenient(ln, d))
                i += 1
        out.append(row)
    return out


def table_records(text, ext, stats):
    d = _rfc_delim(text, ext)
    if d is None:
        d = ","
    if d != ",":
        CL.note("csv_delimiter", 1, {"\t": "tab", ";": "semicolon", "|": "pipe"}.get(d))
    rows = [r for r in _csv_rows(text, d) if any(c.strip() for c in r)]
    W = _modal_width(rows) if rows else 1
    good = sum(1 for r in rows if len(r) == W)
    lenient = set()
    if rows and good < 0.98 * len(rows):
        alt = _lines_rows(text, d)
        alt_rows = [r[1] if isinstance(r, tuple) else r for r in alt]
        alt_len = set(i for i, r in enumerate(alt) if isinstance(r, tuple))
        alt_good = sum(1 for i, r in enumerate(alt_rows) if len(r) == W and i not in alt_len)
        if alt_good > good:
            rows, lenient = alt_rows, alt_len
            CL.note("csv_bad_quote_repaired", len(lenient))
    if not rows:
        return
    start = 0
    for i, r in enumerate(rows[:200]):
        if len(r) == W:
            start = i
            break
    if start:
        CL.note("csv_preamble_skipped", start)
    hdr = [h.strip().lstrip("\ufeff") for h in rows[start]]
    headerless = sum(1 for h in hdr if _numlike(h)) >= max(1, len(hdr) // 2)
    rows_from = start
    if headerless:
        CL.note("csv_headerless")
        hdr = ["c%d" % (i + 1) for i in range(W)]
    else:
        rows_from = start + 1
    seen = {}
    names = []
    for i, h in enumerate(hdr):
        h = h or "col%d" % (i + 1)
        if h in seen:
            seen[h] += 1
            CL.note("csv_duplicate_header", 1, "column %s repeated" % clip_name(h))
            h = "%s_%d" % (h, seen[h])
        else:
            seen[h] = 1
        names.append(h)
    W = len(names)
    for idx in range(rows_from, len(rows)):
        row = rows[idx]
        if len(row) > W:
            row = _fit(row, W, d)
            CL.note("csv_ragged_row")
        elif len(row) < W:
            nonempty = sum(1 for c in row if c.strip())
            if len(row) <= W // 2 or nonempty <= 1 and W > 2:
                CL.note("csv_junk_row")
                continue
            row = row + [""] * (W - len(row))
            CL.note("csv_ragged_row")
        yield dict(zip(names, row))


def clip_name(h):
    return h[:40]


# ---------------------------------------------------------------- one file

def _count(gen):
    n = 0
    for x in gen:
        n += 1
        yield x
    _count.last = n


def _file_records(name, data, stats):
    base = os.path.basename(name)
    ext = os.path.splitext(base.lower())[1]
    text = decode_bytes(data, base)
    if text is None:
        CL.note("binary_file", 1, base)
        stats["unreadable_files"] = stats.get("unreadable_files", 0) + 1
        return
    t = text.lstrip()
    if not t:
        CL.note("empty_file", 1, base)
        return
    got = 0
    if ext in TABLE_EXT:
        for r0 in table_records(text, ext, stats):
            for r in expand(r0):
                got += 1
                yield r
    elif t[0] in '{["':
        for v in json_values(text, stats):
            for r in _emit(v, stats):
                got += 1
                yield r
        if not got and looks_tabular(text):
            for r0 in table_records(text, ext, stats):
                for r in expand(r0):
                    got += 1
                    yield r
    else:
        for v in json_values(_noise_prefix(text), stats):
            for r in _emit(v, stats):
                got += 1
                yield r
        if not got and looks_tabular(text):
            for r0 in table_records(text, ext, stats):
                for r in expand(r0):
                    got += 1
                    yield r
    if not got:
        CL.note("no_records_file", 1, base)
        stats["unreadable_files"] = stats.get("unreadable_files", 0) + 1


def _noise_prefix(text):
    """Plain log text: let json_values handle line-by-line (it extracts embedded JSON and skips noise lines)."""
    return text


# ---------------------------------------------------------------- sources

def iter_files(paths):
    """Expand files / directories (recursive) to a sorted list of files. Raises FileNotFoundError for a missing path."""
    out = []
    for p in ([paths] if isinstance(paths, str) else list(paths)):
        p = str(p)
        if os.path.isdir(p):
            seen = set()
            for root, dirs, names in os.walk(p):
                real = os.path.realpath(root)
                if real in seen:
                    dirs[:] = []
                    continue
                seen.add(real)
                dirs[:] = sorted(d for d in dirs if not d.startswith("."))
                for n in sorted(names):
                    if not n.startswith("."):
                        out.append(os.path.join(root, n))
        elif os.path.isfile(p):
            out.append(p)
        else:
            raise FileNotFoundError("source not found: %s" % os.path.basename(p) if p else "source not found")
    return sorted(set(out))


def iter_records(paths, stats=None):
    """Yield one dict per record from every readable file under `paths`. Never raises on bad content;
    only FileNotFoundError for a path that does not exist. Counters land in `stats` and the cleaning ledger."""
    files = iter_files(paths)
    if stats is None:
        stats = {}
    stats.setdefault("files", [])
    stats.setdefault("bad_lines", 0)
    stats["files"].extend(files)
    total = 0  # records yielded so far; stats["rec_files"] = [[source name, first record index, count], ...]
    for path in files:
        base = os.path.basename(path)
        ext = os.path.splitext(base.lower())[1]
        if ext in IGNORED_EXT:
            CL.note("ignored_extension", 1, base)
            continue
        try:
            with open(path, "rb") as f:
                data = f.read()
        except Exception:
            CL.note("unreadable_file", 1, "%s (cannot open)" % base)
            continue
        if os.path.getsize(path) == 0 or not data.strip():
            CL.note("empty_file", 1, base)
            continue
        for name, blob in _unpack(path, data):
            leaf_ext = os.path.splitext(name.lower())[1]
            if leaf_ext in IGNORED_EXT:
                CL.note("ignored_extension", 1, os.path.basename(name))
                continue
            first = total
            try:
                for r in _file_records(name, blob, stats):
                    total += 1
                    yield sanitize(r)
            except (MemoryError, RecursionError):
                CL.note("unreadable_file", 1, "%s (too large or too deeply nested)" % os.path.basename(name))
            except Exception as e:  # a damaged file must never abort the run
                CL.note("unreadable_file", 1, "%s (%s)" % (os.path.basename(name), type(e).__name__))
            finally:
                if total > first:
                    stats.setdefault("rec_files", []).append([_display_name(name), first, total - first])


def _display_name(name):
    """Source name for locators: file name only (zip members as archive!member), secrets and e-mails masked."""
    from common import redact
    head, sep, member = name.partition("!")
    return redact(os.path.basename(head) + sep + member)[:120]
