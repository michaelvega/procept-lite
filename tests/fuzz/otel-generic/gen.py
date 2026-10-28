"""Generate deformed synthetic OTel-family fixtures. Stdlib only. Usage: python3 gen.py [outdir]"""
import gzip, io, json, os, random, sys, zipfile

OUT = os.path.join(sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "cases"))
random.seed(7)
T0 = 1767225600 * 10**9  # 2026-01-01


def kv(k, v):
    if isinstance(v, bool): return {"key": k, "value": {"boolValue": v}}
    if isinstance(v, int): return {"key": k, "value": {"intValue": str(v)}}
    if isinstance(v, float): return {"key": k, "value": {"doubleValue": v}}
    return {"key": k, "value": {"stringValue": str(v)}}


def span(tid, sid, parent, name, t, dur, attrs, status=None, events=None, times=True):
    s = {"traceId": tid, "spanId": sid, "name": name, "kind": 1, "attributes": [kv(k, v) for k, v in attrs.items()]}
    if parent: s["parentSpanId"] = parent
    if times:
        s["startTimeUnixNano"] = str(t); s["endTimeUnixNano"] = str(t + dur)
    if status is not None: s["status"] = status
    if events: s["events"] = events
    return s


def trace(i, tools=1, fail=False, tokens=True, times=True, status_style="int", prompt=True, errattr=True):
    tid = "%032x" % (0xabc000 + i); root = "%016x" % (i * 100 + 1)
    t = T0 + i * 60 * 10**9
    sp = [span(tid, root, None, "invoke_agent helper", t, 9 * 10**9,
               {"gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": "helper",
                **({"gen_ai.prompt": "Question %d: summarize invoice batch %d" % (i, i)} if prompt else {})},
               {"code": 1} if status_style == "int" else {"code": "STATUS_CODE_OK"}, times=times)]
    llm = "%016x" % (i * 100 + 2)
    a = {"gen_ai.operation.name": "chat", "gen_ai.request.model": "gpt-x", "gen_ai.completion": "Answer %d: batch total is %d dollars." % (i, i * 7)}
    if tokens: a.update({"gen_ai.usage.input_tokens": 120 + i, "gen_ai.usage.output_tokens": 40})
    sp.append(span(tid, llm, root, "chat gpt-x", t + 10**8, 2 * 10**9, a, {"code": 1}, times=times))
    for k in range(tools):
        bad = fail and k == 0
        ta = {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "lookup_invoice",
              "gen_ai.tool.call.arguments": json.dumps({"id": i}),
              "gen_ai.tool.call.result": ("Error: HTTP 503 upstream timeout" if bad else json.dumps({"ok": True, "total": i}))}
        if bad and errattr:
            st = {"code": 2, "message": "boom"} if status_style == "int" else {"code": "STATUS_CODE_ERROR", "message": "boom"}
        else:
            st = {"code": 1} if status_style == "int" else {"code": "STATUS_CODE_OK"}
        if not errattr: st = None
        sp.append(span(tid, "%016x" % (i * 100 + 3 + k), root, "execute_tool lookup_invoice", t + 3 * 10**9 + k, 10**9, ta, st, times=times))
    return sp


def otlp(spans_list):
    return {"resourceSpans": [{"resource": {"attributes": [kv("service.name", "demo-agent")]},
                               "scopeSpans": [{"scope": {"name": "demo"}, "spans": spans_list}]}]}


def allspans(n=6, **kw):
    out = []
    for i in range(n):
        out += trace(i, fail=(i % 3 == 1), **kw)
    return out


def w(name, data, mode="w", enc="utf-8"):
    p = os.path.join(OUT, name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    if isinstance(data, bytes):
        open(p, "wb").write(data)
    else:
        open(p, "w", encoding=enc, newline="").write(data)


def nd(spans):  # collector file exporter: one resourceSpans per line
    return "\n".join(json.dumps(otlp([s])) for s in spans) + "\n"


def main():
    base = allspans()
    w("01_baseline_otlp.json", json.dumps(otlp(base)))
    w("02_collector_ndjson.jsonl", nd(base))
    w("03_no_timestamps.json", json.dumps(otlp(allspans(times=False))))
    w("04_no_tokens.json", json.dumps(otlp(allspans(tokens=False))))
    w("05_no_status_text_errors_only.json", json.dumps(otlp(allspans(errattr=False))))
    w("06_string_status.json", json.dumps(otlp(allspans(status_style="str"))))
    nt = []
    for i in range(5):
        tr = trace(i, tools=0)
        nt += tr
    w("07_no_tool_calls.json", json.dumps(otlp(nt)))
    # renamed attrs (openinference / llm.* style)
    ren = []
    for s in base:
        s = json.loads(json.dumps(s))
        for a in s["attributes"]:
            a["key"] = {"gen_ai.prompt": "input.value", "gen_ai.completion": "output.value", "gen_ai.tool.name": "tool.name",
                        "gen_ai.usage.input_tokens": "llm.token_count.prompt", "gen_ai.usage.output_tokens": "llm.token_count.completion",
                        "gen_ai.operation.name": "openinference.span.kind", "gen_ai.request.model": "llm.model_name",
                        "gen_ai.tool.call.arguments": "input.value", "gen_ai.tool.call.result": "output.value"}.get(a["key"], a["key"])
        ren.append(s)
    w("08_renamed_openinference.json", json.dumps(otlp(ren)))
    # wrong types: epoch variants, ints as strings, floats, nulls, NaN
    wt = json.loads(json.dumps(otlp(base)))
    sps = wt["resourceSpans"][0]["scopeSpans"][0]["spans"]
    for j, s in enumerate(sps):
        m = j % 5
        st = int(s["startTimeUnixNano"]); en = int(s["endTimeUnixNano"])
        if m == 0: s["startTimeUnixNano"], s["endTimeUnixNano"] = st // 10**9, en // 10**9
        elif m == 1: s["startTimeUnixNano"], s["endTimeUnixNano"] = st // 10**6, en // 10**6
        elif m == 2: s["startTimeUnixNano"], s["endTimeUnixNano"] = "2026-01-01T00:00:%02dZ" % (j % 60), "2026-01-01T00:00:%02d+02:00" % (j % 60)
        elif m == 3: s["startTimeUnixNano"], s["endTimeUnixNano"] = None, None
        else: s["startTimeUnixNano"], s["endTimeUnixNano"] = st // 10**3, "garbage"
    w("09_mixed_time_formats.json", json.dumps(wt).replace('"intValue": "', '"intValue": "'))
    nan = json.dumps(otlp(base)).replace('"intValue": "120"', '"doubleValue": NaN').replace('"stringValue": "Answer 2', '"stringValue": null, "x": "Answer 2')
    w("10_nan_nulls.json", nan)
    # truncated
    t = nd(base); w("11_truncated_last_line.jsonl", t[: len(t) - 90])
    t = json.dumps(otlp(base)); w("12_truncated_array.json", t[: len(t) // 2])
    # encodings
    w("13_bom.json", b"\xef\xbb\xbf" + json.dumps(otlp(base)).encode())
    w("14_utf16.json", json.dumps(otlp(base)).encode("utf-16"))
    w("15_gzip.json.gz", gzip.compress(json.dumps(otlp(base)).encode()))
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w") as z:
        z.writestr("export/a.json", json.dumps(otlp(base[:9]))); z.writestr("export/b.jsonl", nd(base[9:]))
    w("16_zip_two_files.zip", bio.getvalue())
    w("17_double_encoded.jsonl", "\n".join(json.dumps(json.dumps(otlp([s]))) for s in base) + "\n")
    w("18_concatenated_json.json", "".join(json.dumps(otlp([s])) for s in base))
    w("19_crlf_blank_lines.jsonl", nd(base).replace("\n", "\r\n\r\n"))
    # json-array of resourceSpans
    w("20_array_of_batches.json", json.dumps([otlp([s]) for s in base]))
    # dup / orphan / out-of-order
    d = list(base); random.shuffle(d); d = d + d[:5]
    d = [s for s in d if s.get("parentSpanId") != "%016x" % 1] + []
    for s in d[:3]: s["parentSpanId"] = "ffffffffffffffff"
    w("21_dups_orphans_shuffled.json", json.dumps(otlp(d)))
    # huge fields
    hg = json.loads(json.dumps(otlp(base)))
    for s in hg["resourceSpans"][0]["scopeSpans"][0]["spans"][:4]:
        for a in s["attributes"]:
            if "stringValue" in a["value"]: a["value"]["stringValue"] = "lorem ipsum " * 400000
    w("22_huge_fields.json", json.dumps(hg))
    # events carrying prompts/completions
    ev = []
    for i in range(4):
        tid = "%032x" % (0xdef000 + i); t = T0 + i * 10**10
        ev.append(span(tid, "%016x" % (i * 10 + 1), None, "invoke_agent a", t, 5 * 10**9, {"gen_ai.operation.name": "invoke_agent"}, {"code": 1}))
        ev.append(span(tid, "%016x" % (i * 10 + 2), "%016x" % (i * 10 + 1), "chat m", t, 10**9,
                       {"gen_ai.operation.name": "chat", "gen_ai.usage.input_tokens": 50, "gen_ai.usage.output_tokens": 9}, {"code": 1},
                       events=[{"name": "gen_ai.user.message", "timeUnixNano": str(t), "attributes": [kv("content", "event prompt %d please help" % i)]},
                               {"name": "gen_ai.choice", "timeUnixNano": str(t + 5), "attributes": [kv("message", json.dumps({"content": "event answer %d" % i}))]}]))
    w("23_events_prompts.json", json.dumps(otlp(ev)))
    # jaeger
    jd = {"data": []}
    for i in range(4):
        tid = "j%03d" % i
        jd["data"].append({"traceID": tid, "processes": {"p1": {"serviceName": "agent"}}, "spans": [
            {"traceID": tid, "spanID": "s1", "operationName": "agent.run", "references": [], "startTime": 1767225600000000 + i * 10**7, "duration": 5000000, "processID": "p1",
             "tags": [{"key": "input.value", "type": "string", "value": "jaeger question %d" % i}, {"key": "output.value", "type": "string", "value": "jaeger answer %d" % i}]},
            {"traceID": tid, "spanID": "s2", "operationName": "tool.search", "references": [{"refType": "CHILD_OF", "traceID": tid, "spanID": "s1"}],
             "startTime": 1767225600000000 + i * 10**7 + 10, "duration": 1000, "processID": "p1",
             "tags": [{"key": "tool.name", "type": "string", "value": "search"}, {"key": "error", "type": "bool", "value": i % 2 == 1},
                      {"key": "output.value", "type": "string", "value": "Traceback (most recent call last): ValueError" if i % 2 else "fine"}]}]})
    w("24_jaeger.json", json.dumps(jd))
    # zipkin
    zk = []
    for i in range(4):
        tid = "%016x" % (0xaa00 + i)
        zk.append({"traceId": tid, "id": "a1", "name": "agent run", "timestamp": 1767225600000000 + i * 10**7, "duration": 4000000, "localEndpoint": {"serviceName": "agent"},
                   "tags": {"input": "zipkin question %d" % i, "output": "zipkin answer %d" % i}})
        zk.append({"traceId": tid, "id": "b1", "parentId": "a1", "name": "tool call", "timestamp": 1767225600000000 + i * 10**7 + 5, "duration": 900,
                   "tags": {"tool.name": "fetch", "error": "timeout 504" if i % 2 else "", "result": "ok"}})
    w("25_zipkin.json", json.dumps(zk))
    # missing ids
    mi = json.loads(json.dumps(otlp(base)))
    for s in mi["resourceSpans"][0]["scopeSpans"][0]["spans"]:
        s.pop("traceId", None); s.pop("spanId", None); s.pop("parentSpanId", None)
    w("26_no_ids.json", json.dumps(mi))
    # no user prompt / no answer
    np_ = allspans(prompt=False)
    for s in np_:
        s["attributes"] = [a for a in s["attributes"] if a["key"] != "gen_ai.completion"]
    w("27_no_text_at_all.json", json.dumps(otlp(np_)))
    # unknown schema: flat json with random keys
    w("28_unknown_schema.jsonl", "\n".join(json.dumps({"foo": i, "bar": "x%d" % i, "baz": [1, 2]}) for i in range(5)) + "\n")
    # empty / garbage
    w("29_empty.json", "")
    w("30_garbage.bin", bytes(random.randrange(256) for _ in range(2000)))
    # attrs as plain dict instead of kv list
    pd = json.loads(json.dumps(otlp(base)))
    for s in pd["resourceSpans"][0]["scopeSpans"][0]["spans"]:
        s["attributes"] = {a["key"]: list(a["value"].values())[0] for a in s["attributes"]}
    w("31_attrs_plain_dict.json", json.dumps(pd))
    # secrets/emails
    sec = json.dumps(otlp(base)).replace("summarize invoice", "email bob@example.com key sk-FAKE1234567890abcdef summarize invoice")
    w("32_secrets_emails.json", sec)
    # mixed folder
    for n, c in [("a.json", json.dumps(otlp(base[:6]))), ("b.jsonl", nd(base[6:])), ("notes.txt", "hello"), ("pic.png", b"\x89PNG\r\n\x1a\n\x00\x00"), ("c.json", "{not json")]:
        w(os.path.join("33_mixed_folder", n), c)
    # scale
    big = []
    for i in range(1500):
        big += trace(i, tools=random.randint(0, 4), fail=(i % 7 == 0))
    w("34_scale_1500_runs.jsonl", nd(big))
    # single huge-ish conversation w/ session attr and no trace gaps
    ses = json.loads(json.dumps(otlp(base)))
    for s in ses["resourceSpans"][0]["scopeSpans"][0]["spans"]:
        s["attributes"].append(kv("session.id", "sess-%d" % (int(s["traceId"], 16) % 2)))
    w("35_session_ids.json", json.dumps(ses))


def main2():
    """Round 2 deformations (36+)."""
    import base64
    base = allspans()
    j = json.dumps(otlp(base))
    w("36_utf16le_no_bom.json", j.encode("utf-16-le"))
    w("37_latin1_bytes.jsonl", nd(base).replace("summarize", "r\u00e9sum\u00e9 caf\u00e9").encode("latin-1", "replace") + b"\xff\xfe\x80garbage\n")
    # nested kvlist/array values, message lists in gen_ai.input.messages (new semconv)
    nm = []
    for i in range(4):
        tid = "%032x" % (0x777000 + i); t = T0 + i * 10**10
        r = "%016x" % (i * 10 + 1)
        nm.append(span(tid, r, None, "invoke_agent a", t, 5 * 10**9, {"gen_ai.operation.name": "invoke_agent"}, {"code": 1}))
        im = [{"role": "user", "parts": [{"type": "text", "content": "semconv question %d about refunds" % i}]}]
        om = [{"role": "assistant", "parts": [{"type": "tool_call", "name": "refund_lookup", "id": "c1", "arguments": {"i": i}}]},
              {"role": "assistant", "parts": [{"type": "text", "content": "semconv answer %d" % i}]}]
        a = {"gen_ai.operation.name": "chat", "gen_ai.input.messages": json.dumps(im), "gen_ai.output.messages": json.dumps(om),
             "gen_ai.usage.input_tokens": 30, "gen_ai.usage.output_tokens": 5}
        sp = span(tid, "%016x" % (i * 10 + 2), r, "chat m", t, 10**9, a, {"code": 1})
        sp["attributes"].append({"key": "nested", "value": {"kvlistValue": {"values": [kv("a", "b")]}}})
        sp["attributes"].append({"key": "arr", "value": {"arrayValue": {"values": [{"stringValue": "x"}, {"intValue": "2"}]}}})
        nm.append(sp)
    w("36b_semconv_message_lists.json", json.dumps(otlp(nm)))
    # status variants as strings / lowercase / otel.status_code attribute / no status but exception event
    sv = json.loads(j)
    for k, s_ in enumerate(sv["resourceSpans"][0]["scopeSpans"][0]["spans"]):
        st = s_.get("status", {})
        if st.get("code") == 2:
            m = k % 4
            if m == 0: s_["status"] = {"code": "2"}
            elif m == 1: s_["status"] = {"code": "Error"}
            elif m == 2: s_.pop("status"); s_["attributes"].append(kv("otel.status_code", "ERROR"))
            else:
                s_.pop("status"); s_["events"] = [{"name": "exception", "timeUnixNano": s_["startTimeUnixNano"], "attributes": [kv("exception.type", "TimeoutError"), kv("exception.message", "upstream gone")]}]
    w("37_status_variants.json", json.dumps(sv))
    # negative duration, end missing, float-second strings
    nd_ = json.loads(j)
    for k, s_ in enumerate(nd_["resourceSpans"][0]["scopeSpans"][0]["spans"]):
        st_ = int(s_["startTimeUnixNano"]); 
        if k % 3 == 0: s_["endTimeUnixNano"] = str(st_ - 10**9)
        elif k % 3 == 1: s_.pop("endTimeUnixNano")
        else: s_["startTimeUnixNano"] = "%.3f" % (st_ / 1e9); s_["endTimeUnixNano"] = "%.3f" % (int(s_["endTimeUnixNano"]) / 1e9)
    w("38_negative_missing_durations.json", json.dumps(nd_))
    # legacy instrumentationLibrarySpans + snake_case + base64 ids
    lg = json.loads(j)
    lg["resourceSpans"][0]["instrumentationLibrarySpans"] = lg["resourceSpans"][0].pop("scopeSpans")
    w("39_legacy_instrumentation_library.json", json.dumps(lg))
    sn = {"resource_spans": [{"resource": {"attributes": []}, "scope_spans": [{"spans": []}]}]}
    for s_ in json.loads(j)["resourceSpans"][0]["scopeSpans"][0]["spans"]:
        s2 = {"trace_id": base64.b64encode(bytes.fromhex(s_["traceId"])).decode(), "span_id": base64.b64encode(bytes.fromhex(s_["spanId"])).decode(),
              "name": s_["name"], "start_time_unix_nano": s_["startTimeUnixNano"], "end_time_unix_nano": s_["endTimeUnixNano"],
              "attributes": s_["attributes"], "status": s_.get("status", {})}
        if "parentSpanId" in s_: s2["parent_span_id"] = base64.b64encode(bytes.fromhex(s_["parentSpanId"])).decode()
        sn["resource_spans"][0]["scope_spans"][0]["spans"].append(s2)
    w("40_snake_case_base64_ids.json", json.dumps(sn))
    # Tempo-style batches wrapper
    w("41_tempo_batches.json", json.dumps({"batches": json.loads(j)["resourceSpans"]}))
    # envelope per line: {"time":..., "result": {resourceSpans}}
    w("42_enveloped_lines.jsonl", "\n".join(json.dumps({"time": "2026-01-01T00:00:00Z", "source": "otelcol", "result": otlp([s_])}) for s_ in base) + "\n")
    # log lines with prefix and embedded json
    w("43_prefixed_log_lines.log", "\n".join("2026-01-01T00:00:%02dZ INFO exporter " % (k % 60) + json.dumps(otlp([s_])) for k, s_ in enumerate(base)) + "\nplain line no json\n")
    # flattened csv, semicolon, one row per span
    rows = []
    for s_ in base:
        at = {a["key"]: list(a["value"].values())[0] for a in s_["attributes"]}
        rows.append({"trace_id": s_["traceId"], "span_id": s_["spanId"], "parent_span_id": s_.get("parentSpanId", ""), "name": s_["name"],
                     "start_time": s_.get("startTimeUnixNano", ""), "end_time": s_.get("endTimeUnixNano", ""),
                     "status_code": s_.get("status", {}).get("code", ""), "attributes": json.dumps(at).replace('"', '""')})
    hdr = list(rows[0])
    w("44_flat_csv_semicolon.csv", ";".join(hdr) + "\n" + "\n".join(";".join('"%s"' % r[h] if h == "attributes" else str(r[h]) for h in hdr) for r in rows) + "\n")
    # top-level junk JSON values
    w("45_json_null.json", "null")
    w("46_array_of_scalars.json", "[1, 2, null, \"x\"]")
    w("47_empty_resourcespans.json", json.dumps({"resourceSpans": []}))
    w("48_resourcespans_no_spans.json", json.dumps({"resourceSpans": [{"resource": {}, "scopeSpans": [{"spans": []}, {}]}, {}]}))
    # span id collisions across traces, single trace id for everything
    col = json.loads(j)
    for k, s_ in enumerate(col["resourceSpans"][0]["scopeSpans"][0]["spans"]):
        s_["spanId"] = "%016x" % (k % 3 + 1); 
        if "parentSpanId" in s_: s_["parentSpanId"] = "%016x" % 1
    w("49_span_id_collisions.json", json.dumps(col))
    one = json.loads(j)
    for s_ in one["resourceSpans"][0]["scopeSpans"][0]["spans"]: s_["traceId"] = "%032x" % 1
    w("50_single_trace_many_turns.json", json.dumps(one))
    # control chars, NUL, lone surrogates, very deep nesting
    cc = j.replace("summarize invoice", "summarize\\u0000 \\u0007 \\ud800 invoice")
    w("51_control_chars_surrogates.json", cc)
    deep = "[" * 3000 + "]" * 3000
    w("52_deep_nesting.json", deep)
    # truncated gzip, gzip of ndjson, double gzip
    gz = gzip.compress(nd(base).encode())
    w("53_truncated_gzip.json.gz", gz[: len(gz) - 40])
    w("54_double_gzip.jsonl.gz", gzip.compress(gz))
    # per-trace files in nested folders with junk
    for k in range(6):
        w(os.path.join("55_nested_folder", "day%d" % (k % 2), "trace_%d.json" % k), json.dumps(otlp(trace(k, fail=(k == 1)))))
    w(os.path.join("55_nested_folder", ".DS_Store"), b"\x00\x00\x00\x01Bud1")
    w(os.path.join("55_nested_folder", "day0", "empty.jsonl"), "")
    w(os.path.join("55_nested_folder", "day1", "README.md"), "# exports")
    # all error, all fail text only (no status, no tokens, no times)
    w("56_everything_missing.json", json.dumps(otlp([{"name": "x", "attributes": [kv("gen_ai.tool.name", "t"), kv("gen_ai.tool.call.result", "{\"ok\": false, \"error\": \"denied\"}")]},
                                                  {"name": "y", "attributes": [kv("gen_ai.tool.name", "t"), kv("gen_ai.tool.call.result", "HTTP 429 too many requests")]}])))
    # jaeger variants: tags dict, startTime ISO, no processes, spans-only array
    jv = []
    for i in range(3):
        jv.append({"traceID": "q%d" % i, "spanID": "a", "operationName": "agent", "startTime": "2026-01-01T00:0%d:00Z" % i, "duration": "12ms",
                   "tags": {"input.value": "jaeger dict question %d" % i, "output.value": "ans %d" % i}})
        jv.append({"traceID": "q%d" % i, "spanID": "b", "operationName": "tool.x", "references": [{"refType": "CHILD_OF", "spanID": "a"}], "startTime": "2026-01-01T00:0%d:01Z" % i, "duration": "3ms",
                   "tags": {"tool.name": "x", "otel.status_code": "ERROR", "otel.status_description": "bad"}})
    w("57_jaeger_dict_tags_iso.json", json.dumps(jv))
    # zipkin with annotations and tag error only
    w("58_zipkin_annotations.json", json.dumps([{"traceId": "%016x" % i, "id": "1", "name": "post /agent", "timestamp": "1767225600%06d" % i, "duration": 2000,
                                                  "annotations": [{"timestamp": 1767225600000000, "value": "exception: boom"}], "tags": {"http.status_code": "502", "input": "zip q %d" % i}} for i in range(4)]))
    # scale: 5000 tool spans in one run
    big = trace(0, tools=5000)
    w("59_5k_tools_one_run.jsonl", nd(big))
    # mixed epoch units within one trace plus year 1970 / 9999 values
    me = json.loads(j)
    for k, s_ in enumerate(me["resourceSpans"][0]["scopeSpans"][0]["spans"]):
        if k % 4 == 0: s_["startTimeUnixNano"] = "0"
        if k % 4 == 1: s_["endTimeUnixNano"] = "253402300800000000000"
    w("60_extreme_timestamps.json", json.dumps(me))
    # attribute keys with weird case / dots / prefix, values all numbers for text
    w("61_numeric_text_values.json", json.dumps(otlp([span("%032x" % i, "%016x" % (i+1), None, "agent", T0 + i, 10**9, {"gen_ai.prompt": 12345, "gen_ai.completion": 3.14, "gen_ai.tool.name": 7}, {"code": 1}) for i in range(4)])))


if __name__ == "__main__":
    main()
    main2()
