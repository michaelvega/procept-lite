"""Generate deformed synthetic Logfire exports. stdlib only. Usage: python3 gen.py [outdir]"""
import copy, csv, datetime as dt, gzip, io, json, os, sys, zipfile

OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "cases")
T0 = dt.datetime(2025, 3, 1, 10, 0, 0, tzinfo=dt.timezone.utc)
TOOLS = ["lookup_order", "search_docs", "send_email"]


def iso(t):
    return t.strftime("%Y-%m-%d %H:%M:%S.%f") + "+00"


def base(nruns=6, new_conv=True, call_ids=True):
    """list of span dicts with attributes as dict (new gen_ai conventions)."""
    spans = []
    for i in range(nruns):
        t = T0 + dt.timedelta(minutes=3 * i)
        tid, root = "trace%02d" % i, "root%02d" % i
        conv = "conv-%d" % (i // 2)
        common = {"gen_ai.conversation.id": conv, "gen_ai.agent.name": "support_bot"}
        if call_ids:
            common["gen_ai.agent.call.id"] = "call-%d" % i
        a = dict(common, **{"gen_ai.operation.name": "invoke_agent"})
        q = "Where is my order %d? mail me at user%d@example.com" % (1000 + i, i)
        ans = "Order %d shipped yesterday." % (1000 + i)
        a["gen_ai.input.messages"] = [{"role": "user", "parts": [{"type": "text", "content": q}]}]
        a["gen_ai.output.messages"] = [{"role": "assistant", "parts": [{"type": "text", "content": ans}]}]
        spans.append(sp(tid, root, None, "invoke_agent support_bot", t, 5.0, a))
        la = dict(common, **{"gen_ai.operation.name": "chat", "gen_ai.request.model": "gpt-4o-2024-08-06",
                             "gen_ai.usage.input_tokens": 900 + i, "gen_ai.usage.output_tokens": 60})
        spans.append(sp(tid, "llm%02d" % i, root, "chat gpt-4o", t + dt.timedelta(seconds=.2), 1.0, la))
        for j in range(1 + i % 3):
            bad = (i % 3 == 2 and j == 0)
            ta = dict(common, **{"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": TOOLS[j % 3],
                                 "gen_ai.tool.call.arguments": {"id": 1000 + i},
                                 "gen_ai.tool.call.result": "Error: upstream timeout 504" if bad else {"status": "ok", "n": j}})
            s = sp(tid, "tool%02d_%d" % (i, j), root, "execute_tool " + TOOLS[j % 3],
                   t + dt.timedelta(seconds=1.5 + j), .5, ta)
            if bad:
                s["is_exception"] = True
                s["exception_type"] = "TimeoutError"
                s["exception_message"] = "upstream timeout"
                s["level"] = 17
            spans.append(s)
    return spans


def sp(tid, sid, pid, name, t, dur, attrs):
    return {"trace_id": tid, "span_id": sid, "parent_span_id": pid, "span_name": name,
            "start_timestamp": iso(t), "end_timestamp": iso(t + dt.timedelta(seconds=dur)), "duration": dur,
            "level": 9, "is_exception": False, "attributes": attrs}


def jl(spans, attrs_mode="dict"):
    out = []
    for s in spans:
        s = copy.deepcopy(s)
        if attrs_mode == "str":
            s["attributes"] = json.dumps(s["attributes"])
        elif attrs_mode == "double":
            s["attributes"] = json.dumps(json.dumps(s["attributes"]))
        out.append(s)
    return out


def w(name, data, mode="w", enc="utf-8"):
    os.makedirs(OUT, exist_ok=True)
    p = os.path.join(OUT, name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    if isinstance(data, bytes):
        open(p, "wb").write(data)
    else:
        open(p, "w", encoding=enc, newline="").write(data)


def lines(recs):
    return "\n".join(json.dumps(r) for r in recs) + "\n"


def flat_csv(spans, delim=",", cols_json_attr=False, flatten=True):
    rows, keys = [], []
    for s in spans:
        r = {k: v for k, v in s.items() if k != "attributes"}
        if flatten:
            for k, v in s["attributes"].items():
                r["attributes." + k] = json.dumps(v) if isinstance(v, (dict, list)) else v
        else:
            r["attributes"] = json.dumps(s["attributes"])
        rows.append(r)
        for k in r:
            if k not in keys:
                keys.append(k)
    b = io.StringIO()
    wr = csv.DictWriter(b, keys, delimiter=delim)
    wr.writeheader()
    wr.writerows(rows)
    return b.getvalue()


def otlp(v):
    if isinstance(v, bool): return {"boolValue": v}
    if isinstance(v, int): return {"intValue": str(v)}
    if isinstance(v, float): return {"doubleValue": v}
    if isinstance(v, str): return {"stringValue": v}
    return {"stringValue": json.dumps(v)}


def main():
    S = base()
    # 01 healthy baseline jsonl, attributes as JSON string
    w("01_baseline_str_attrs.jsonl", lines(jl(S, "str")))
    # 02 attributes as dict
    w("02_attrs_dict.jsonl", lines(jl(S, "dict")))
    # 03 flattened csv columns
    w("03_flat_columns.csv", flat_csv(S))
    # 04 legacy conventions: gen_ai.prompt / gen_ai.completion flattened, no input.messages
    L = copy.deepcopy(S)
    for s in L:
        a = s["attributes"]
        if "gen_ai.input.messages" in a:
            m = a.pop("gen_ai.input.messages")[0]["parts"][0]["content"]
            a["gen_ai.prompt.0.role"] = "user"; a["gen_ai.prompt.0.content"] = m
            c = a.pop("gen_ai.output.messages")[0]["parts"][0]["content"]
            a["gen_ai.completion.0.role"] = "assistant"; a["gen_ai.completion.0.content"] = c
        if "gen_ai.usage.input_tokens" in a:
            a["gen_ai.usage.prompt_tokens"] = a.pop("gen_ai.usage.input_tokens")
            a["gen_ai.usage.completion_tokens"] = a.pop("gen_ai.usage.output_tokens")
    w("04_legacy_genai.jsonl", lines(jl(L, "str")))
    # 05 missing gen_ai.agent.call.id
    w("05_no_call_id.jsonl", lines(jl(base(call_ids=False), "str")))
    # 06 no timestamps/durations at all
    N = jl(S, "str")
    for r in N:
        for k in ("start_timestamp", "end_timestamp", "duration"): r.pop(k)
    w("06_no_time.jsonl", lines(N))
    # 07 renamed time fields + epoch ns / ms numbers, no duration
    R = jl(S, "str")
    for i, r in enumerate(R):
        st = dt.datetime.strptime(r.pop("start_timestamp")[:-3], "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=dt.timezone.utc)
        en = dt.datetime.strptime(r.pop("end_timestamp")[:-3], "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=dt.timezone.utc)
        r.pop("duration")
        r["start_time_unix_nano"] = int(st.timestamp() * 1e9)
        r["end_time_unix_nano"] = int(en.timestamp() * 1e9)
        r["message"] = r.pop("span_name")
    w("07_epoch_ns_renamed.jsonl", lines(R))
    # 08 epoch ms strings and naive ISO mix
    M = jl(S, "str")
    for i, r in enumerate(M):
        st = dt.datetime.strptime(r["start_timestamp"][:-3], "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=dt.timezone.utc)
        r["start_timestamp"] = str(int(st.timestamp() * 1000)) if i % 2 else st.strftime("%Y-%m-%dT%H:%M:%S")
        r["end_timestamp"] = None if i % 3 == 0 else r["end_timestamp"]
        r["duration"] = "NaN" if i % 4 == 0 else str(r["duration"] * 1000) + "ms"
    w("08_mixed_ts_wrongtype.jsonl", lines(M))
    # 09 no tokens/cost but text exists
    T = copy.deepcopy(S)
    for s in T:
        for k in list(s["attributes"]):
            if "usage" in k: s["attributes"].pop(k)
    w("09_no_tokens.jsonl", lines(jl(T, "str")))
    # 10 no tokens and no text
    T2 = copy.deepcopy(T)
    for s in T2:
        for k in ("gen_ai.input.messages", "gen_ai.output.messages", "gen_ai.tool.call.arguments", "gen_ai.tool.call.result"):
            s["attributes"].pop(k, None)
    w("10_no_tokens_no_text.jsonl", lines(jl(T2, "str")))
    # 11 no explicit error field; errors only in tool result text
    E = jl(S, "str")
    for r in E:
        r.pop("is_exception"); r.pop("level"); r.pop("exception_type", None); r.pop("exception_message", None)
    w("11_errors_only_in_text.jsonl", lines(E))
    # 12 no tool calls at all
    NT = [s for s in S if "execute_tool" not in s["span_name"]]
    w("12_no_tools.jsonl", lines(jl(NT, "str")))
    # 13 missing user message / answer
    MU = copy.deepcopy(S)
    for s in MU:
        s["attributes"].pop("gen_ai.input.messages", None)
        if s["span_id"].endswith(("1", "3")): s["attributes"].pop("gen_ai.output.messages", None)
    w("13_no_user_no_answer.jsonl", lines(jl(MU, "str")))
    # 14 OTLP attribute lists
    O = jl(S, "dict")
    for r in O:
        r["attributes"] = [{"key": k, "value": otlp(v)} for k, v in r["attributes"].items()]
    w("14_otlp_attr_lists.jsonl", lines(O))
    # 15 double-encoded attributes
    w("15_double_encoded.jsonl", lines(jl(S, "double")))
    # 16 truncated last line + concatenated no-newline + bad line in the middle
    t = lines(jl(S, "str")).split("\n")
    t.insert(5, "{not json at all")
    w("16_truncated_garbage.jsonl", "\n".join(t)[:-60])
    # 17 JSON array vs concatenated objects
    w("17_json_array.json", json.dumps(jl(S, "dict"), indent=1))
    w("17b_concatenated.jsonl", "".join(json.dumps(r) for r in jl(S, "str")))
    # 18 BOM + CRLF
    w("18_bom_crlf.jsonl", ("﻿" + lines(jl(S, "str")).replace("\n", "\r\n")).encode("utf-8"))
    # 19 UTF-16
    w("19_utf16.jsonl", ("﻿" + lines(jl(S, "str"))).encode("utf-16"))
    # 20 gzip / zip
    w("20_gz.jsonl.gz", gzip.compress(lines(jl(S, "str")).encode()))
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("export/spans.jsonl", lines(jl(S, "str")))
    w("20b_zip.zip", b.getvalue())
    # 21 csv delimiters, bad quotes
    w("21_semicolon.csv", flat_csv(S, ";", flatten=False))
    w("21b_tab.csv", flat_csv(S, "\t"))
    w("21c_pipe.csv", flat_csv(S, "|", flatten=False))
    bad = flat_csv(S, ",", flatten=False).replace('""', '"', 3)
    w("21d_bad_quotes.csv", bad)
    # 22 duplicates, orphans, shuffled order
    D = jl(S, "str")
    import random
    rnd = random.Random(3)
    D = D + D[:8]
    for r in D[:6]: r["parent_span_id"] = "ghost" if r["parent_span_id"] else None
    rnd.shuffle(D)
    w("22_dups_orphans_shuffled.jsonl", lines(D))
    # 23 huge fields
    H = copy.deepcopy(S)
    H[0]["attributes"]["gen_ai.input.messages"][0]["parts"][0]["content"] = "lorem ipsum " * 200000
    H[2]["attributes"]["gen_ai.tool.call.result"] = "x" * 3000000
    w("23_huge_fields.jsonl", lines(jl(H, "str")))
    # 24 scale: 400 runs
    w("24_scale_400.jsonl", lines(jl(base(400), "str")))
    # 25 nulls / NaN everywhere
    NU = jl(S, "str")
    for r in NU:
        r["parent_span_id"] = r["parent_span_id"] or None
        r["span_name"] = None if r["span_id"].endswith("0") else r["span_name"]
        r["level"] = "NaN"; r["trace_id"] = r["trace_id"] if r["span_id"][-1] != "2" else None
    txt = lines(NU).replace('"duration": 5.0', '"duration": NaN').replace('"duration": 1.0', '"duration": Infinity')
    w("25_nulls_nan.jsonl", txt)
    # 26 renamed fields (Logfire UI-style exports)
    RN = []
    for s in jl(S, "dict"):
        RN.append({"traceId": s["trace_id"], "spanId": s["span_id"], "parentSpanId": s["parent_span_id"],
                   "message": s["span_name"], "startTime": s["start_timestamp"], "endTime": s["end_timestamp"],
                   "attrs": json.dumps(s["attributes"]), "exception.type": s.get("exception_type"),
                   "exception.message": s.get("exception_message")})
    w("26_renamed_fields.jsonl", lines(RN))
    # 27 exception columns only (stacktrace) with no tool result error text
    EX = jl(S, "str")
    for r in EX:
        a = json.loads(r["attributes"]); a.pop("gen_ai.tool.call.result", None); r["attributes"] = json.dumps(a)
        if r.get("is_exception"): r["exception_stacktrace"] = "Traceback (most recent call last):\n  File \"x.py\", line 1\nTimeoutError"
    w("27_exception_columns.jsonl", lines(EX))
    # 28 mixed folder
    w("28_mixed_folder/good.jsonl", lines(jl(S, "str")))
    w("28_mixed_folder/notes.txt", "hello this is not a log\n")
    w("28_mixed_folder/img.png", b"\x89PNG\r\n\x1a\n\x00\x00garbage")
    w("28_mixed_folder/empty.jsonl", "")
    w("28_mixed_folder/binary.bin", bytes(range(256)) * 10)
    # 29 events-style: gen_ai.* as span events in attributes (prompt/completion events)
    EV = jl(S, "dict")
    for r in EV:
        a = r["attributes"]
        if "gen_ai.input.messages" in a:
            q = a.pop("gen_ai.input.messages")[0]["parts"][0]["content"]
            c = a.pop("gen_ai.output.messages")[0]["parts"][0]["content"]
            r["events"] = [{"name": "gen_ai.user.message", "attributes": {"content": q}},
                           {"name": "gen_ai.choice", "attributes": {"message": json.dumps({"content": c})}}]
    w("29_events_style.jsonl", lines(EV))
    # 30 unknown schema (nothing logfire-like)
    UK = [{"foo": "run%d" % i, "bar": "question %d" % i, "baz": "answer %d" % i, "qux": i * 1.5} for i in range(6)]
    w("30_unknown_schema.jsonl", lines(UK))
    # 31 totally empty / whitespace
    w("31_empty.jsonl", "")
    w("31b_whitespace.jsonl", "  \n\n   \n")
    # 32 secrets in data
    SE = copy.deepcopy(S)
    SE[0]["attributes"]["gen_ai.input.messages"][0]["parts"][0]["content"] = "my key is sk-FAKE0000000000000000000000 and pw hunter2"
    SE[0]["attributes"]["authorization"] = "Bearer FAKE.TOKEN.VALUE"
    w("32_secrets.jsonl", lines(jl(SE, "str")))
    # 33 span_name variants + wrong types for tokens
    V = copy.deepcopy(S)
    for s in V:
        s["span_name"] = s["span_name"].replace("execute_tool ", "running tool ").replace("invoke_agent", "agent run").upper()
        a = s["attributes"]
        for k in ("gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens"):
            if k in a: a[k] = str(a[k]) + ".0"
    w("33_span_name_variants_str_tokens.jsonl", lines(jl(V, "str")))
    # 34 only llm spans (no agent root, no call id)
    LO = [s for s in base(call_ids=False) if s["parent_span_id"] is not None and "execute_tool" not in s["span_name"]]
    for s in LO: s["parent_span_id"] = None
    w("34_llm_only.jsonl", lines(jl(LO, "str")))
    # 35 missing trace ids and conv ids and timestamps shuffled (grouping by time gap)
    G = jl(S, "str")
    for r in G:
        r.pop("trace_id"); a = json.loads(r["attributes"]); a.pop("gen_ai.conversation.id"); a.pop("gen_ai.agent.call.id", None)
        r["attributes"] = json.dumps(a); r["parent_span_id"] = None
    w("35_no_ids.jsonl", lines(G))


if __name__ == "__main__":
    main()
