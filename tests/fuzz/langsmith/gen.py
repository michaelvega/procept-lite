"""Generate deformed LangSmith-style fixtures (synthetic only). stdlib only."""
import csv, gzip, io, json, os, random, zipfile
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "cases")
random.seed(7)

def iso(i, off=0): 
    s = 1740988800 + i * 600 + off
    import datetime
    return datetime.datetime.utcfromtimestamp(s).strftime("%Y-%m-%dT%H:%M:%S.000000")

def trace(i, tools=("search", "fetch"), fail=False, thread=True, usage="llm_output"):
    tid = "t%04d" % i
    root = {"id": tid, "trace_id": tid, "parent_run_id": None, "run_type": "chain", "name": "AgentExecutor",
            "start_time": iso(i), "end_time": iso(i, 8), "inputs": {"input": "Question %d about invoices" % i},
            "outputs": {"output": "Answer %d: done." % i}, "error": None,
            "extra": {"metadata": {"thread_id": "th%d" % (i // 3)} if thread else {}}}
    rs = [root]
    llm = {"id": tid + "-llm", "trace_id": tid, "parent_run_id": tid, "run_type": "llm", "name": "ChatModel",
           "start_time": iso(i, 1), "end_time": iso(i, 2),
           "inputs": {"messages": [[{"type": "human", "kwargs": {"content": "Question %d" % i}}]]},
           "outputs": {"generations": [[{"text": "ok"}]]}, "error": None, "extra": {}}
    if usage == "llm_output":
        llm["outputs"]["llm_output"] = {"token_usage": {"prompt_tokens": 300, "completion_tokens": 20}, "model_name": "m1"}
    elif usage == "usage_metadata":
        llm["outputs"]["generations"][0][0]["message"] = {"kwargs": {"usage_metadata": {"input_tokens": 310, "output_tokens": 22}}}
    elif usage == "extra":
        llm["extra"]["usage"] = {"input_tokens": 5, "output_tokens": 6}
    elif usage == "prompt_total":
        llm["prompt_tokens"] = 100; llm["completion_tokens"] = 10; llm["total_tokens"] = 110
    rs.append(llm)
    for k, t in enumerate(tools):
        r = {"id": "%s-tool%d" % (tid, k), "trace_id": tid, "parent_run_id": tid, "run_type": "tool", "name": t,
             "start_time": iso(i, 3 + k), "end_time": iso(i, 4 + k), "inputs": {"input": "{\"q\": \"x\"}"},
             "outputs": {"output": "result of %s" % t}, "error": None, "extra": {}}
        if fail and k == 0:
            r["error"] = "TimeoutError: timeout after 30s"; r["outputs"] = None
        rs.append(r)
    return rs

def many(n, **kw):
    out = []
    for i in range(n):
        out += trace(i, fail=(i % 4 == 0), **kw)
    return out

def w(name, data, mode="w", enc="utf-8"):
    p = os.path.join(OUT, name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    if isinstance(data, bytes):
        open(p, "wb").write(data)
    else:
        open(p, "w", encoding=enc, newline="").write(data)

def jl(rs): return "\n".join(json.dumps(r) for r in rs) + "\n"

def flat_csv(rs, delim=",", quote_bad=False):
    cols = ["id", "trace_id", "parent_run_id", "run_type", "name", "start_time", "end_time", "inputs", "outputs", "error", "extra"]
    b = io.StringIO()
    wr = csv.writer(b, delimiter=delim)
    wr.writerow(cols)
    for r in rs:
        wr.writerow([json.dumps(r.get(c)) if isinstance(r.get(c), (dict, list)) else ("" if r.get(c) is None else r.get(c)) for c in cols])
    s = b.getvalue()
    if quote_bad:
        s = s.replace('"{""input"": ""Question 3', '"{"input": "Question 3', 1) + 'stray,"unterminated quote,here\n'
    return s

def build():
    base = many(12)
    w("01_baseline.json", json.dumps(base, indent=1))
    w("02_baseline.jsonl", jl(base))
    # missing timestamps
    nt = json.loads(json.dumps(base))
    for r in nt: r.pop("start_time"); r.pop("end_time")
    w("03_no_timestamps.json", json.dumps(nt))
    ne = json.loads(json.dumps(base))
    for r in ne: r.pop("end_time")
    w("04_no_end_time.jsonl", jl(ne))
    # no ids at all
    ni = json.loads(json.dumps(base))
    for r in ni:
        for k in ("id", "trace_id", "parent_run_id"): r.pop(k, None)
    w("05_no_ids.json", json.dumps(ni))
    # parent only, no trace id, no session
    nt2 = json.loads(json.dumps(base))
    for r in nt2: r.pop("trace_id")
    w("06_no_trace_id.json", json.dumps(nt2))
    # error forms
    ef = many(8)
    forms = [{"message": "boom", "type": "ValueError"}, "Traceback (most recent call last): ...", ["e1", "e2"], True, 500, "", None, {"status": "error"}]
    k = 0
    for r in ef:
        if r["run_type"] == "tool":
            r["error"] = forms[k % len(forms)]; k += 1
    w("07_error_forms.json", json.dumps(ef))
    # errors only in output text
    eo = many(8)
    pats = ['{"status":"error","detail":"x"}', "HTTP 503 Service Unavailable", "Exception: bad", '{"isError": true}', '{"ok": false}', "request timeout", "Traceback (most recent call last):\n  File x"]
    k = 0
    for r in eo:
        if r["run_type"] == "tool":
            r["outputs"] = {"output": pats[k % len(pats)]}; k += 1
    w("08_errors_in_text.json", json.dumps(eo))
    # token variants
    tv = []
    for i, u in enumerate(["llm_output", "usage_metadata", "extra", "prompt_total", "none"]):
        tv += trace(i, usage=u)
    w("09_token_variants.json", json.dumps(tv))
    w("10_no_tokens.json", json.dumps(many(6, usage="none")))
    nt3 = many(6, usage="none")
    for r in nt3:
        r["inputs"] = None; r["outputs"] = None
    w("11_no_tokens_no_text.json", json.dumps(nt3))
    # io shapes
    sh = many(6)
    shapes = ["plain string", None, {"a": {"b": ["x", {"c": 1}]}}, [{"role": "user", "content": "hi there"}], {"messages": [{"role": "user", "content": [{"type": "text", "text": "multi part"}]}]}, 42]
    for i, r in enumerate(sh):
        r["inputs"] = shapes[i % len(shapes)]; r["outputs"] = shapes[(i + 2) % len(shapes)]
    w("12_io_shapes.json", json.dumps(sh))
    # double encoded
    de = many(6)
    for r in de:
        for c in ("inputs", "outputs", "extra"): r[c] = json.dumps(r[c])
    w("13_double_encoded.json", json.dumps(de))
    w("14_double_encoded_whole.json", json.dumps(json.dumps(base)))
    # nested child_runs
    root = trace(1)[0]; kids = trace(1)[1:]
    for kd in kids: kd.pop("parent_run_id"); kd.pop("trace_id")
    root["child_runs"] = kids
    root2 = trace(2)[0]; root2["child_runs"] = [dict(k, child_runs=[]) for k in trace(2)[1:]]
    w("15_child_runs_nested.json", json.dumps([root, root2]))
    w("16_runs_wrapper.json", json.dumps({"runs": base}))
    # truncated
    s = jl(base); w("17_truncated.jsonl", s[:-60])
    w("18_truncated_array.json", json.dumps(base)[:-40])
    # encodings
    w("19_bom.jsonl", b"\xef\xbb\xbf" + jl(base).encode())
    w("20_utf16.json", json.dumps(base).encode("utf-16"))
    w("21_gzip.jsonl.gz", gzip.compress(jl(base).encode()))
    bz = io.BytesIO()
    with zipfile.ZipFile(bz, "w") as z: z.writestr("runs.jsonl", jl(base)); z.writestr("readme.txt", "hi")
    w("22_zip.zip", bz.getvalue())
    w("23_concatenated.json", "".join(json.dumps(r) for r in base))
    w("24_concat_arrays.json", json.dumps(base[:10]) + "\n" + json.dumps(base[10:]))
    # CSV variants
    for nm, d in (("comma", ","), ("tab", "\t"), ("semi", ";"), ("pipe", "|")):
        w("25_csv_%s.csv" % nm, flat_csv(base, d))
    w("26_csv_badquotes.csv", flat_csv(base, ",", True))
    # timestamps
    for nm, f in (("ms", lambda i, o: int((1740988800 + i * 600 + o) * 1000)), ("us", lambda i, o: int((1740988800 + i * 600 + o) * 1e6)),
                  ("ns", lambda i, o: int((1740988800 + i * 600 + o) * 1e9)), ("s", lambda i, o: 1740988800 + i * 600 + o),
                  ("tz", lambda i, o: iso(i, o)[:-7] + "+02:00"), ("z", lambda i, o: iso(i, o)[:-7] + "Z")):
        rs = []
        for i in range(6):
            t = trace(i)
            for r in t:
                r["start_time"] = f(i, 0); r["end_time"] = f(i, 8)
            rs += t
        w("27_ts_%s.json" % nm, json.dumps(rs))
    # nulls / NaN
    nn = json.dumps(many(5)).replace('"error": null', '"error": NaN', 3).replace('"prompt_tokens": 300', '"prompt_tokens": NaN')
    w("28_nan.json", nn)
    allnull = many(5)
    for r in allnull:
        for k in ("name", "run_type", "inputs", "outputs", "extra", "error"): r[k] = None
    w("29_all_null_fields.json", json.dumps(allnull))
    # dup / orphan / out of order
    dd = many(8); dd = dd + dd[:7]
    dd = [r for r in dd if not r["id"].endswith("t0001")] if False else dd
    orph = [dict(r, parent_run_id="ghost-%s" % r["id"]) if r["run_type"] == "tool" else r for r in many(6)]
    random.shuffle(dd); random.shuffle(orph)
    w("30_duplicates_shuffled.json", json.dumps(dd))
    w("31_orphans_shuffled.json", json.dumps(orph))
    # huge
    hg = many(4)
    for r in hg:
        if r["run_type"] == "tool": r["outputs"] = {"output": "x" * 3_000_000}
        if r["run_type"] == "chain": r["inputs"] = {"input": "y" * 2_000_000}
    w("32_huge_fields.json", json.dumps(hg))
    # no tools
    nt4 = [r for r in many(8) if r["run_type"] != "tool"]
    w("33_no_tools.json", json.dumps(nt4))
    only_llm = [r for r in many(5) if r["run_type"] == "llm"]
    for r in only_llm: r["parent_run_id"] = None
    w("34_only_llm_runs.json", json.dumps(only_llm))
    # missing user/answer
    mu = many(6)
    for i, r in enumerate(mu):
        if r["run_type"] == "chain":
            if i % 2: r["inputs"] = {}
            else: r["outputs"] = {}
    w("35_missing_user_or_answer.json", json.dumps(mu))
    # renamed fields
    rn = []
    for r in many(8):
        rn.append({"run_uuid": r["id"], "session": r["trace_id"], "parent": r["parent_run_id"], "kind": r["run_type"], "label": r["name"],
                   "began": r["start_time"], "finished": r["end_time"], "prompt": json.dumps(r["inputs"]), "response": json.dumps(r["outputs"]), "err": r["error"]})
    w("36_renamed_fields.json", json.dumps(rn))
    w("37_unknown_schema.json", json.dumps([{"foo": i, "bar": "baz %d" % i} for i in range(5)]))
    # wrong types
    wt = many(6)
    for r in wt:
        r["start_time"] = str(r["start_time"]) if False else 12345 if r["run_type"] == "llm" else r["start_time"]
        r["run_type"] = r["run_type"].upper()
        r["extra"] = "not a dict"
        r["name"] = 123
    w("38_wrong_types.json", json.dumps(wt))
    # empty / unreadable
    w("39_empty.json", "")
    w("40_garbage.json", "this is not json at all <html>")
    w("41_empty_array.json", "[]")
    # mixed folder
    for nm in ("good.jsonl",): w("42_mixed_folder/" + nm, jl(base))
    w("42_mixed_folder/notes.txt", "hello"); w("42_mixed_folder/img.png", bytes(range(256)))
    w("42_mixed_folder/broken.json", "{{{"); w("42_mixed_folder/runs2.csv", flat_csv(base[:5]))
    # secrets
    sec = many(4)
    for r in sec:
        if r["run_type"] == "chain":
            r["inputs"] = {"input": "email me at jane.doe@example.com key sk-FAKEFAKEFAKEFAKE0000 password=hunter2"}
    sec[0]["error"] = "auth failed for token sk-FAKEFAKEFAKEFAKE0000 user bob@example.com"
    w("43_secrets.json", json.dumps(sec))
    # scale
    w("44_scale.jsonl", jl(many(1500)))
    # no thread: group by time gap
    w("45_no_session_info.json", json.dumps([r for r in many(10, thread=False)]))
    # control chars + unicode
    uc = many(4)
    uc[0]["inputs"] = {"input": "café ☃ \x00\x07 \U0001F600 </script>"}
    w("46_unicode_ctrl.json", json.dumps(uc))
    # retriever + feedback
    rt = trace(1)
    rt.append({"id": "ret1", "trace_id": "t0001", "parent_run_id": "t0001", "run_type": "retriever", "name": "kb", "start_time": iso(1, 5), "end_time": iso(1, 6),
               "inputs": {"query": "q"}, "outputs": {"documents": [{"page_content": "doc"}]}, "error": None, "extra": {}})
    w("47_retriever.json", json.dumps(rt))
    # CRLF/jsonl with blank lines and comments
    w("48_blank_lines_crlf.jsonl", "\r\n\r\n".join(json.dumps(r) for r in base) + "\r\n\r\n")


def build2():
    """Round 2: new deformations."""
    base = many(12)
    J = lambda o: json.dumps(o)
    # 49 LangSmith UI CSV export: flattened columns, inputs/outputs JSON strings, token columns
    cols = ["Name", "Run ID", "Trace ID", "Parent Run ID", "Run Type", "Start Time", "End Time", "Error", "Total Tokens", "Prompt Tokens", "Completion Tokens", "Total Cost", "Input", "Output", "Tags", "Metadata"]
    b = io.StringIO(); wr = csv.writer(b)
    wr.writerow(cols)
    for r in base:
        wr.writerow([r["name"], r["id"], r["trace_id"], r["parent_run_id"] or "", r["run_type"], r["start_time"], r["end_time"], r["error"] or "",
                     330 if r["run_type"] == "llm" else "", 300 if r["run_type"] == "llm" else "", 30 if r["run_type"] == "llm" else "", "",
                     J(r["inputs"]), J(r["outputs"]) if r["outputs"] else "", "[]", J(r["extra"].get("metadata", {}))])
    w("49_ui_csv_export.csv", b.getvalue())
    w("49b_ui_csv_excel_bom_crlf.csv", b"\xef\xbb\xbf" + b.getvalue().replace("\r\n", "\n").replace("\n", "\r\n").encode())
    # 50 dotted_order only (no parent ids)
    d = json.loads(J(base))
    for r in d:
        r["dotted_order"] = "20250303T080000000000Z" + r["trace_id"] + ("" if r["parent_run_id"] is None else ".20250303T080001000000Z" + r["id"])
        r.pop("parent_run_id"); r.pop("trace_id")
    w("50_dotted_order_only.json", J(d))
    # 51 tool calls only inside llm outputs (OpenAI tool_calls), no tool runs
    t = []
    for i in range(6):
        tr = [r for r in trace(i) if r["run_type"] != "tool"]
        tr[1]["outputs"] = {"generations": [[{"message": {"kwargs": {"content": "", "tool_calls": [{"name": "search", "args": {"q": "x"}, "id": "c1"}],
            "additional_kwargs": {"tool_calls": [{"id": "c1", "function": {"name": "search", "arguments": "{\"q\": 1}"}}]}}}}]]}
        t += tr
    w("51_toolcalls_in_llm_output.json", J(t))
    # 52 chat inputs carry full history with tool role messages (one containing error text)
    h = []
    for i in range(4):
        tr = trace(i, tools=())
        tr[1]["inputs"] = {"messages": [{"role": "user", "content": "Find invoice %d" % i}, {"role": "assistant", "content": None, "tool_calls": [{"id": "x", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "x", "content": "Error: 503 Service Unavailable" if i % 2 else "found it"}]}
        h += tr
    w("52_history_tool_messages.json", J(h))
    # 53 error literal strings
    el = many(8)
    lits = ["None", "null", "N/A", "false", "0", " ", "{}", "[]"]
    k = 0
    for r in el:
        if r["run_type"] == "tool": r["error"] = lits[k % len(lits)]; k += 1
    w("53_error_literals_not_errors.json", J(el))
    # 54 odd timestamp formats
    ts = many(6)
    fm = ["%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f+00:00", "%b %d %Y %H:%M:%S", "1740988800.5", "0001-01-01T00:00:00"]
    import datetime
    for n, r in enumerate(ts):
        f = fm[(n // 3) % len(fm)] if False else fm[n % len(fm)]
        base_dt = datetime.datetime(2025, 3, 3, 8, n)
        r["start_time"] = f if f[0].isdigit() and "%" not in f else (f if "0001" in f else base_dt.strftime(f))
        r["end_time"] = r["start_time"]
    w("54_odd_timestamp_formats.json", J(ts))
    # 55 negative durations (end before start) and year 1970
    ng = many(6)
    for r in ng:
        r["start_time"], r["end_time"] = r["end_time"], r["start_time"]
    ng[0]["start_time"] = "1970-01-01T00:00:00"
    w("55_negative_duration.json", J(ng))
    # 56 pagination wrappers across folder with overlap
    pg = "56_paged_folder/"
    w(pg + "page1.json", J({"runs": base[:16], "cursors": {"next": "abc"}}))
    w(pg + "page2.json", J({"data": base[12:], "total": 24}))
    w(pg + "page3", J({"items": base[:3]}))
    # 57 each line is an array of runs
    w("57_jsonl_of_arrays.jsonl", "\n".join(J(base[i:i + 6]) for i in range(0, 24, 6)) + "\n")
    # 58 cp1252 / invalid utf-8
    w("58_cp1252.jsonl", jl(base).replace("Answer", "Answer \u201cquoted\u201d caf\u00e9").encode("cp1252", "replace"))
    w("58b_invalid_utf8.jsonl", jl(base).encode() + b'{"id": "bad\xff\xfe", "run_type": "chain"}\n')
    # 59 NUL bytes, lone surrogates
    w("59_lone_surrogates.json", J(base).replace("Question 3 about", "Question \\ud800 3 \\u0000 about"))
    # 60 deeply nested outputs
    dn = many(3)
    deep = "x"
    for _ in range(3000): deep = {"a": deep}
    dn[2]["outputs"] = deep
    w("60_deep_nesting.json", J(dn)) if False else None
    try:
        w("60_deep_nesting.json", "[" + ",".join(J(r) for r in dn[:2]) + "," + (J(dn[2]) if False else '{"id":"d1","run_type":"chain","inputs":' + '{"a":' * 3000 + '1' + '}' * 3000 + ',"outputs":{"output":"fine"}}') + "]")
    except Exception: pass
    # 61 python-repr lines
    w("61_python_repr.txt", "\n".join(repr(r) for r in base) + "\n")
    # 62 JSON with comments and trailing commas
    w("62_comments_trailing_commas.json", "// export\n[\n" + ",\n".join(J(r) for r in base) + ",\n]\n")
    # 63 ids as ints / duplicate ids reused across traces
    ii = many(6)
    for n, r in enumerate(ii):
        r["id"] = n; r["parent_run_id"] = None if r["run_type"] == "chain" else r["parent_run_id"]
    w("63_int_ids.json", J(ii))
    dup = many(6)
    for r in dup:
        if r["run_type"] == "tool": r["id"] = "same-tool-id"
    w("63b_reused_ids.json", J(dup))
    # 64 token strings
    tk = many(4)
    for r in tk:
        if r["run_type"] == "llm":
            r["outputs"]["llm_output"]["token_usage"] = {"prompt_tokens": "1,234", "completion_tokens": "12.5", "total_tokens": "n/a"}
    tk[1]["outputs"]["llm_output"]["token_usage"] = {"prompt_tokens": -5, "completion_tokens": 10 ** 15}
    w("64_token_strings_negative.json", J(tk))
    # 65 multiline quoted CSV cells
    ml = io.StringIO(); wr = csv.writer(ml)
    wr.writerow(["id", "trace_id", "parent_run_id", "run_type", "name", "start_time", "end_time", "inputs", "outputs", "error"])
    for r in base:
        wr.writerow([r["id"], r["trace_id"], r["parent_run_id"] or "", r["run_type"], r["name"], r["start_time"], r["end_time"], "line1\nline2, \"quoted\"\n" + (r["inputs"].get("input", "") if isinstance(r["inputs"], dict) else ""), "out\nmore", r["error"] or ""])
    w("65_csv_multiline_cells.csv", ml.getvalue())
    # 66 extensionless / .log / .txt jsonl
    w("66_ext_variants/runs.log", jl(base[:8])); w("66_ext_variants/dump", jl(base[8:])); w("66_ext_variants/notes.md", "# nothing here")
    # 67 zip slip + nested zip + gz inside zip
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as z: z.writestr("inner.jsonl", jl(base[:10]))
    bz = io.BytesIO()
    with zipfile.ZipFile(bz, "w") as z:
        z.writestr("../../evil.jsonl", jl(base[10:])); z.writestr("nested.zip", inner.getvalue()); z.writestr("a.jsonl.gz", gzip.compress(jl(base[:4]).encode())); z.writestr("dir/", "")
    w("67_zip_slip_nested.zip", bz.getvalue())
    # 68 gzip truncated, gzip of garbage
    g = gzip.compress(jl(base).encode()); w("68_gzip_truncated.jsonl.gz", g[:len(g) // 2])
    # 69 tools only, no chain roots
    to = [r for r in many(6) if r["run_type"] == "tool"]
    for r in to: r["parent_run_id"] = None
    w("69_tools_only_no_roots.json", J(to))
    # 70 tool runs with null name / uppercase types
    nn = many(4)
    for r in nn:
        if r["run_type"] == "tool": r["name"] = None; r["run_type"] = "Tool"
    w("70_tool_null_name.json", J(nn))
    # 71 root error only
    re_ = many(4)
    for r in re_:
        if r["run_type"] == "chain": r["error"] = "KeyboardInterrupt"; r["outputs"] = None
    w("71_root_error_no_answer.json", J(re_))
    # 72 secrets in a filename and in error + path-like
    w("72_secret_filename_sk-FAKEFAKEFAKEFAKE0000_jane.doe@example.com.json", "{{{ not json sk-FAKEFAKEFAKEFAKE0000")
    # 73 enormous single line (20MB no newline, valid jsonl line) and 5000 tools in one trace
    big = trace(1, tools=tuple("tool%d" % (k % 7) for k in range(5000)))
    w("73_5k_tools_one_trace.json", J(big))
    # 74 feedback / reference examples mixed in
    fb = many(4)
    fb.append({"id": "fb1", "key": "correctness", "score": 1, "run_id": "t0001", "comment": "ok"})
    fb.append({"example_id": "e1", "dataset_id": "d1", "inputs": {"q": "x"}, "outputs": {"a": "y"}})
    w("74_feedback_dataset_rows_mixed.json", J(fb))
    # 75 LangGraph style: node runs with metadata langgraph_node, interrupts, Command outputs
    lg = []
    for i in range(4):
        tr = trace(i)
        for r in tr: r["extra"] = {"metadata": {"langgraph_node": r["name"], "langgraph_step": 3, "thread_id": "g%d" % (i // 2)}}
        tr[0]["outputs"] = {"messages": [{"type": "human", "content": "q"}, {"type": "ai", "content": "final answer %d" % i}]}
        tr[0]["inputs"] = {"messages": [["user", "hello %d" % i]]}
        lg += tr
    w("75_langgraph_messages_tuples.json", J(lg))
    # 76 two spellings of a schema mixed in the same file
    mx = many(3) + [{"run_uuid": "z1", "session": "zs", "kind": "chain", "label": "n", "began": iso(9), "finished": iso(9, 3), "prompt": "hi", "response": "yo"}]
    w("76_mixed_schemas_one_file.json", J(mx))
    # 77 giant 20MB single record string field in CSV
    gc = io.StringIO(); wr = csv.writer(gc); wr.writerow(["id", "run_type", "inputs", "outputs"]); wr.writerow(["g1", "chain", "q" * 20_000_000, "ok"])
    w("77_csv_20mb_cell.csv", gc.getvalue())
    # 78 only whitespace / only BOM / only newline
    w("78_only_bom.json", b"\xef\xbb\xbf"); w("78b_only_whitespace.jsonl", "\n\n   \n"); w("78c_null_json.json", "null"); w("78d_scalar.json", "42")
    # 79 array of arrays / array of strings
    w("79_array_of_arrays.json", J([[1, 2], [3]])); w("79b_array_of_json_strings.json", J([J(r) for r in base]))
    # 80 HTML error page saved as json
    w("80_html_error_page.json", "<!DOCTYPE html><html><body>403 Forbidden</body></html>")
    # 81 sensitive: data URI images / base64 blobs in inputs
    bl = many(3)
    bl[0]["inputs"] = {"input": "see", "image": "data:image/png;base64," + "QUJD" * 200000}
    w("81_base64_blob.json", J(bl))
    # 82 start only for some, end only for others
    pe = many(6)
    for n, r in enumerate(pe):
        if n % 2: r.pop("start_time")
        else: r.pop("end_time")
    w("82_partial_timestamps.json", J(pe))
    # 83 session via top-level session_id/thread + reference_example, time-gap grouping with no ids
    gp = []
    for i in range(8):
        r = trace(i, thread=False)[0]; r.pop("id"); r.pop("trace_id"); r["start_time"] = iso(0, i * (60 if i < 4 else 7200)); r["end_time"] = iso(0, i * (60 if i < 4 else 7200) + 5)
        gp.append(r)
    w("83_gap_grouping_no_ids.json", J(gp))

if __name__ == "__main__":
    build()
    build2()
    print("wrote", len(os.listdir(OUT)), "cases to", OUT)
