"""Generate hostile-edge fixtures into ./cases (stdlib only, synthetic data)."""
import gzip, json, os, random, shutil, zipfile, io, csv

HERE = os.path.dirname(os.path.abspath(__file__))
CASES = os.path.join(HERE, "cases")
random.seed(7)


def span(i, run, tools=2, err=False, ts=True, tok=True):
    """Generic OTel-ish JSONL spans for one run."""
    base = 1767225600 + i * 100
    out = []
    root = {"trace_id": "t%d" % run, "span_id": "s%d_0" % run, "name": "agent run",
            "attributes": {"gen_ai.prompt": "Find invoice %d for bob@example.com" % run,
                           "gen_ai.completion": "Here is invoice %d." % run}}
    if ts:
        root["start_time"] = base; root["end_time"] = base + 30
    if tok:
        root["attributes"]["gen_ai.usage.input_tokens"] = 100
        root["attributes"]["gen_ai.usage.output_tokens"] = 40
    out.append(root)
    for k in range(tools):
        s = {"trace_id": "t%d" % run, "span_id": "s%d_%d" % (run, k + 1), "parent_span_id": "s%d_0" % run,
             "name": "tool:search", "attributes": {"tool.name": "search", "tool.args": "{\"q\":\"x\"}",
             "tool.result": "Traceback (most recent call last): boom" if err and k == 0 else "ok 3 rows"}}
        if ts:
            s["start_time"] = base + k; s["end_time"] = base + k + 1
        out.append(s)
    return out


def jl(rows):
    return "\n".join(json.dumps(r) for r in rows) + "\n"


def w(case, name, data, mode=None):
    d = os.path.join(CASES, case)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    if isinstance(data, str):
        data = data.encode("utf-8")
    with open(p, "wb") as f:
        f.write(data)
    return d


def spans(n, **kw):
    r = []
    for i in range(n):
        r += span(i, i, **kw)
    return r


def main():
    shutil.rmtree(CASES, ignore_errors=True)
    os.makedirs(CASES)
    w("01_baseline", "spans.jsonl", jl(spans(6, err=False) + span(7, 7, err=True)))
    w("02_empty_file", "empty.jsonl", b"")
    w("03_zero_runs_dir", "readme.txt", "nothing here")
    w("04_one_run", "spans.jsonl", jl(span(0, 0)))
    w("05_no_tools_no_ts", "spans.jsonl", jl(spans(5, tools=0, ts=False, tok=False)))
    w("06_all_failing", "spans.jsonl", jl(spans(5, err=True)))
    w("07_truncated", "spans.jsonl", jl(spans(4))[:-60])
    w("08_binary_garbage", "spans.jsonl", bytes(random.randrange(256) for _ in range(5000)))
    w("09_nul_emoji_rtl", "spans.jsonl", jl([dict(s, name=s["name"] + " \x00\U0001F600‮") for s in spans(3)]))
    w("10_bom_utf16", "spans.jsonl", b"\xff\xfe" + jl(spans(3)).encode("utf-16-le"))
    w("11_gzip", "spans.jsonl.gz", gzip.compress(jl(spans(4)).encode()))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("a/spans.jsonl", jl(spans(3)))
    w("12_zip", "export.zip", buf.getvalue())
    w("13_concat_json", "spans.json", "".join(json.dumps(s) for s in spans(4)))
    w("14_double_encoded", "spans.jsonl", "\n".join(json.dumps(json.dumps(s)) for s in spans(3)) + "\n")
    otlp = []
    for s in spans(3):
        a = [{"key": k, "value": ({"intValue": str(v)} if isinstance(v, int) else {"stringValue": str(v)})}
             for k, v in s["attributes"].items()]
        otlp.append({"traceId": s["trace_id"], "spanId": s["span_id"], "parentSpanId": s.get("parent_span_id", ""),
                     "name": s["name"], "startTimeUnixNano": str(int(s["start_time"] * 1e9)),
                     "endTimeUnixNano": str(int(s["end_time"] * 1e9)), "attributes": a})
    w("15_otlp_attrlist", "spans.json", json.dumps({"resourceSpans": [{"scopeSpans": [{"spans": otlp}]}]}))
    for nm, delim in (("16_csv_semicolon", ";"), ("17_csv_pipe", "|"), ("18_csv_tab", "\t")):
        sio = io.StringIO()
        cw = csv.writer(sio, delimiter=delim)
        cw.writerow(["conversation", "question", "answer", "tool", "status"])
        for i in range(5):
            cw.writerow(["c%d" % (i // 2), "what is %d" % i, "it is %d" % i, "lookup", "error" if i == 3 else "ok"])
        w(nm, "chat.csv", sio.getvalue())
    w("19_bad_csv_quotes", "chat.csv", 'id,question,answer\n1,"unterminated,"oops\n2,hi,"yo ""there"\n3,a,b,c,d\n')
    dup = spans(3)
    w("20_duplicate_ids_orphans", "spans.jsonl", jl(dup + dup + [dict(span(9, 9)[1], parent_span_id="ghost")]))
    w("21_secrets", "spans.jsonl", jl([dict(s, attributes=dict(s["attributes"], **{"gen_ai.prompt": "key sk-ABCDEF1234567890SECRET pw hunter2 mail leak@example.com"})) for s in spans(3)]))
    w("22_huge_field", "spans.jsonl", jl([dict(s, attributes=dict(s["attributes"], **{"gen_ai.completion": "A" * 2_000_000})) for s in spans(2)]))
    w("23_nan_null_types", "spans.jsonl", '{"trace_id":"a","span_id":"1","start_time":NaN,"end_time":null,"name":null,"attributes":null}\n{"trace_id":5,"span_id":[1],"start_time":"yesterday","attributes":{"gen_ai.prompt":7}}\n')
    w("24_unicode path ünï", "my spans (1).jsonl", jl(spans(3)))
    d = w("25_mixed_folder", "README.md", "# hi")
    w("25_mixed_folder", "logo.png", b"\x89PNG\r\n\x1a\n" + os.urandom(300))
    w("25_mixed_folder", "node_modules/x/index.js", "module.exports=1")
    w("25_mixed_folder", "good/spans.jsonl", jl(spans(3)))
    d = w("26_symlink_loop", "spans.jsonl", jl(spans(3)))
    try:
        os.symlink(".", os.path.join(d, "loop"))
        os.symlink("nonexistent", os.path.join(d, "dangling"))
    except OSError:
        pass
    w("27_unknown_schema", "x.jsonl", jl([{"foo": "bar %d" % i, "zip": i} for i in range(5)]))
    w("28_renamed_fields", "x.jsonl", jl([{"sessionId": "s%d" % (i // 2), "ts": "2026-01-01T00:0%d:00" % i,
        "input": "q%d" % i, "output": "a%d" % i, "toolCalls": [{"tool": "t", "output": "HTTP 500 error", "isError": True}]} for i in range(5)]))
    w("29_epoch_units", "x.jsonl", jl([{"run_id": "r%d" % i, "user": "q", "answer": "a", "timestamp": t}
        for i, t in enumerate([1767225600, 1767225600000, 1767225600000000, 1767225600000000000, "2026-01-01T00:00:00+02:00"])]))
    big = []
    for i in range(50000):
        big.append({"run_id": "r%d" % i, "conversation_id": "c%d" % (i // 5), "user": "q%d" % i, "answer": "a",
                    "start_time": 1767225600 + i, "end_time": 1767225601 + i, "tools": [{"name": "t", "result": "ok"}]})
    w("30_scale_50k", "runs.jsonl", jl(big))
    w("31_5k_tools_one_run", "run.json", json.dumps({"run_id": "one", "user": "go", "answer": "done",
        "tools": [{"name": "t%d" % (i % 7), "result": "error 503" if i % 9 == 0 else "ok", "args": "{}"} for i in range(5000)]}))

    extra()


def extra():
    """Round-2 new deformations (32+)."""
    sp = spans(4)
    # 32 CSV with embedded newlines/quotes/commas in quoted cells
    sio = io.StringIO(); cw = csv.writer(sio)
    cw.writerow(["conversation_id", "user", "assistant", "tool_name", "tool_output"])
    for i in range(4):
        cw.writerow(["c%d" % (i // 2), "line1\nline2, \"quoted\" %d" % i, "ans\r\nmulti %d" % i, "lookup", "HTTP 503 Service Unavailable" if i == 1 else "ok"])
    w("32_csv_embedded_newlines", "chat.csv", sio.getvalue())
    # 33 CSV with BOM + preamble + CRLF + trailing blank lines
    w("33_csv_bom_preamble_crlf", "export.csv", "\ufeffExported by tool v1\r\nGenerated: yesterday\r\n\r\nrun_id,question,answer\r\nr1,hi,hello\r\nr2,yo,sup\r\n\r\n\r\n")
    # 34 UTF-16-LE no BOM
    w("34_utf16le_nobom", "spans.jsonl", jl(sp).encode("utf-16-le"))
    # 35 truncated JSON array mid-object
    w("35_truncated_array", "spans.json", json.dumps(spans(4))[:-45])
    # 36 OpenAI chat messages with tool_calls
    msgs = []
    for i in range(3):
        msgs.append({"id": "chat%d" % i, "messages": [
            {"role": "user", "content": "weather in city %d?" % i},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "call_%d" % i, "type": "function", "function": {"name": "get_weather", "arguments": "{\"c\": %d}" % i}}]},
            {"role": "tool", "tool_call_id": "call_%d" % i, "content": "{\"error\": \"timeout\"}" if i == 1 else "sunny"},
            {"role": "assistant", "content": "It is sunny."}]})
    w("36_openai_messages", "chats.jsonl", jl(msgs))
    # 37 Anthropic content blocks
    ab = []
    for i in range(3):
        ab.append({"conversation_id": "a%d" % i, "messages": [
            {"role": "user", "content": [{"type": "text", "text": "find doc %d" % i}]},
            {"role": "assistant", "content": [{"type": "text", "text": "searching"}, {"type": "tool_use", "id": "tu%d" % i, "name": "search_docs", "input": {"q": "x"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tu%d" % i, "is_error": i == 2, "content": "Error: not found" if i == 2 else "found"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "done %d" % i}]}]})
    w("37_anthropic_blocks", "chats.json", json.dumps(ab))
    # 38 one conversation 3000 turns
    w("38_one_conv_3000_turns", "t.jsonl", jl([{"conversation_id": "only", "run_id": "t%d" % i, "user": "q%d" % i, "answer": "a%d" % i} for i in range(3000)]))
    # 39 duplicate run ids conflicting content across files
    w("39_dup_runid_conflict", "a.jsonl", jl([{"run_id": "same", "user": "first", "answer": "A"}]))
    w("39_dup_runid_conflict", "b.jsonl", jl([{"run_id": "same", "user": "second", "answer": "B"}, {"run_id": "same", "user": "third", "answer": "C"}]))
    # 40 weird ids
    w("40_weird_ids", "x.jsonl", jl([{"run_id": v, "user": "q%d" % i, "answer": "a"} for i, v in enumerate([None, "", "null", "None", 0, 1.5, [1], {"a": 1}, " ", "x" * 5000, "\u202e\x00", True])]))
    # 41 CR-only newlines
    w("41_cr_only_newlines", "x.jsonl", jl(sp).replace("\n", "\r"))
    # 42 deeply nested JSON
    w("42_deep_nesting", "x.json", "[" * 20000 + "]" * 20000)
    w("42_deep_nesting", "y.json", '{"run_id":"r","user":"q","answer":"a","meta":' + '{"a":' * 3000 + "1" + "}" * 3000 + "}")
    # 43 huge numbers
    w("43_huge_numbers", "x.jsonl", '{"run_id":"a","user":"q","answer":"a","input_tokens":1e400,"cost":-1e999,"start_time":1e30,"end_time":-5}\n{"run_id":"b","user":"q","answer":"a","input_tokens":' + "9" * 5000 + ',"latency":99999999999999999999}\n{"run_id":"c","user":"q","answer":"a","input_tokens":"1,234","cost":"$0.02","latency":"3.2s"}\n')
    # 44 odd timestamps
    ts = [0, -1, 253402300800, 1e12, "0000-00-00", "13/45/2026", "2026-02-30T25:61:61Z", "yesterday", "", "2026-01-01", "Thu, 01 Jan 2026 00:00:00 GMT", "2026-01-01 00:00:00.123456789"]
    w("44_odd_timestamps", "x.jsonl", jl([{"run_id": "t%d" % i, "user": "q", "answer": "a", "start_time": t, "end_time": t} for i, t in enumerate(ts)]))
    # 45 end before start
    w("45_negative_duration", "x.jsonl", jl([{"run_id": "n%d" % i, "user": "q", "answer": "a", "start_time": 1767225700, "end_time": 1767225600} for i in range(3)]))
    # 46 error fields of every shape
    errs = [{"error": False}, {"error": "false"}, {"error": 0}, {"error": ""}, {"error": None}, {"error": {"code": 500}}, {"error": ["x"]}, {"error": True}, {"status": "ok"}, {"status": 200}, {"status": 404}, {"success": False}, {"ok": False}]
    w("46_error_shapes", "x.jsonl", jl([dict(e, run_id="e%d" % i, user="q", answer="a", tools=[dict(e, name="t", result="fine")]) for i, e in enumerate(errs)]))
    # 47 html/yaml/xml/sql dressed as .json/.jsonl
    w("47_html_as_json", "page.json", "<!doctype html><html><body>Login required</body></html>")
    w("47_html_as_json", "cfg.jsonl", "key: value\nother: [1,2\n")
    w("47_html_as_json", "good.jsonl", jl(spans(2)))
    # 48 permission denied file next to good one
    d = w("48_unreadable_file", "good.jsonl", jl(spans(2)))
    p = os.path.join(d, "secret.jsonl")
    with open(p, "w") as f:
        f.write(jl(spans(2)))
    try:
        os.chmod(p, 0)
    except OSError:
        pass
    # 49 filenames with newline, long, spaces, leading dash
    w("49_odd_filenames", "-weird name.jsonl", jl(spans(2)))
    w("49_odd_filenames", "a\nb.jsonl", jl(spans(2)))
    w("49_odd_filenames", ("L" * 200) + ".jsonl", jl(spans(2)))
    # 50 hostile zips
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("../../escape.jsonl", jl(spans(2)))
        z.writestr("/abs/x.jsonl", jl(spans(2)))
        z.writestr("dir/", "")
        z.writestr("ok.jsonl", jl(spans(2)))
    w("50_zip_traversal", "e.zip", buf.getvalue())
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as z:
        z.writestr("deep.jsonl", jl(spans(2)))
    outer = io.BytesIO()
    with zipfile.ZipFile(outer, "w") as z:
        z.writestr("inner.zip", inner.getvalue())
    w("50b_nested_zip", "o.zip", outer.getvalue())
    w("50c_truncated_zip", "t.zip", buf.getvalue()[:-30])
    # 51 gzip variants
    g = gzip.compress(jl(sp).encode())
    w("51_gzip_truncated", "a.jsonl.gz", g[:-20])
    w("51b_gzip_twice", "a.jsonl.gz", gzip.compress(g))
    w("51c_gz_multi_member", "a.jsonl.gz", gzip.compress(jl(spans(2)).encode()) + gzip.compress(jl(spans(2)).encode()))
    # 52 secrets in filename and in unparseable content (messages must not echo)
    w("52_secret_filename_corrupt", "leak@example.com_sk-ABCDEF1234567890SECRET.json", b"\x00\x01 sk-ABCDEF1234567890SECRET {not json")
    # 53 lone surrogates and invalid utf-8
    w("53_lone_surrogate", "x.jsonl", '{"run_id":"s","user":"bad \\ud800 pair","answer":"ok \\udc00"}\n')
    w("53b_latin1_cp1252", "x.jsonl", ('{"run_id":"l","user":"caf\xe9 \x93quoted\x94","answer":"na\xefve"}\n').encode("latin-1"))
    # 54 each line a JSON array of spans
    w("54_array_per_line", "x.jsonl", "\n".join(json.dumps(spans(1, tools=1)[:2]) for _ in range(3)))
    # 55 only tool results, no user msg / no answer
    w("55_tools_only", "x.jsonl", jl([{"trace_id": "t", "span_id": "s%d" % i, "name": "tool", "attributes": {"tool.name": "search", "tool.result": "ok"}} for i in range(4)]))
    # 56 identical records without ids
    w("56_identical_no_ids", "x.jsonl", jl([{"user": "same q", "answer": "same a"}] * 6))
    # 57 scale: 50k-row CSV
    sio = io.StringIO(); cw = csv.writer(sio)
    cw.writerow(["session", "ts", "question", "answer", "tool", "tool_result"])
    for i in range(50000):
        cw.writerow(["s%d" % (i // 4), 1767225600 + i, "q%d" % i, "a%d" % i, "t", "error 500" if i % 17 == 0 else "ok"])
    w("57_scale_50k_csv", "m.csv", sio.getvalue())
    # 58 scale: 50k single-span traces gz
    w("58_scale_50k_gz", "m.jsonl.gz", gzip.compress(jl([{"trace_id": "t%d" % i, "span_id": "1", "name": "agent", "start_time": 1767225600 + i, "end_time": 1767225601 + i, "attributes": {"gen_ai.prompt": "q", "gen_ai.completion": "a"}} for i in range(50000)]).encode()))
    # 59 secrets/emails/phones everywhere
    w("59_pii_everywhere", "x.jsonl", jl([{"run_id": "p", "user": "my card 4111 1111 1111 1111 email a.b@example.com Authorization: Bearer abc.def.ghi", "answer": "AKIAABCDEFGHIJKLMNOP password=hunter2 ghp_0123456789abcdefghijklmnopqrstuvwxyz0"}]))
    # 60 ragged/duplicate/empty headers CSV
    w("60_csv_dup_empty_headers", "x.csv", "id,,id,question,question,answer\n1,a,2,q1,q1b,ans\n2,,,q2\n,,,,,\n")
    # 61 xlsx-like binary and sqlite header next to a good file
    w("61_binary_siblings", "a.xlsx", b"PK\x03\x04" + os.urandom(200))
    w("61_binary_siblings", "b.sqlite", b"SQLite format 3\x00" + os.urandom(500))
    w("61_binary_siblings", "good.jsonl", jl(spans(2)))
    # 62 single file src that is a bare JSON scalar / string / number
    w("62_bare_scalar", "x.json", "42")
    w("62b_bare_string", "x.json", '"hello"')
    w("62c_empty_array_obj", "x.json", "[]")
    w("62d_empty_object", "x.json", "{}")
    w("62e_array_of_nulls", "x.json", "[null,null,[],{}]")
    # 63 jsonl with huge number of tiny garbage lines around a few good
    w("63_noisy_lines", "x.jsonl", "\n".join(["garbage line"] * 500 + [json.dumps(s) for s in spans(2)] + ["}{"] * 500))
    # 64 tool result is a 2MB blob and tool args 2MB
    w("64_huge_tool_io", "x.jsonl", jl([{"run_id": "h", "user": "q", "answer": "a", "tools": [{"name": "t", "args": "B" * 2_000_000, "result": "R" * 2_000_000}]}]))


if __name__ == "__main__":
    main()
