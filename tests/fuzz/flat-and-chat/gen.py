"""Generate synthetic deformed flat/chat fixtures into ./cases (stdlib only)."""
import csv, gzip, io, json, os, zipfile
HERE = os.path.dirname(os.path.abspath(__file__))
C = os.path.join(HERE, "cases")
os.makedirs(C, exist_ok=True)

def w(name, data, mode=None):
    p = os.path.join(C, name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    if isinstance(data, str):
        data = data.encode("utf-8")
    open(p, "wb").write(data)

def ts(i): return "2026-03-01T10:%02d:%02dZ" % (i // 60 % 60, i % 60)

def rows_msgs(n_runs=6, cols=("run_id", "conversation_id", "role", "content", "timestamp", "input_tokens", "output_tokens")):
    out = []
    for r in range(n_runs):
        rid = "run%02d" % r
        conv = "conv%d" % (r // 2)
        out.append([rid, conv, "user", "Find invoice %d for acme" % r, ts(r * 10), 20, 0])
        out.append([rid, conv, "assistant", "Looking it up now", ts(r * 10 + 2), 0, 15])
        if r % 3 == 1:
            out.append([rid, conv, "tool", "Error: 500 internal server error", ts(r * 10 + 3), 0, 0])
        else:
            out.append([rid, conv, "tool", '{"status":"ok","rows":3}', ts(r * 10 + 3), 0, 0])
        out.append([rid, conv, "assistant", "Invoice %d total is $%d" % (r, r * 7), ts(r * 10 + 5), 0, 30])
    return list(cols), out

def csvtext(head, rows, delim=",", quoting=csv.QUOTE_MINIMAL):
    b = io.StringIO()
    wr = csv.writer(b, delimiter=delim, quoting=quoting, lineterminator="\n")
    wr.writerow(head); wr.writerows(rows)
    return b.getvalue()

h, rs = rows_msgs()
base = csvtext(h, rs)
w("01_baseline_messages.csv", base)
w("02_semicolon.csv", csvtext(h, rs, ";"))
w("03_pipe_bom.psv", "﻿" + csvtext(h, rs, "|"))
w("04_tab.tsv", csvtext(h, rs, "\t"))
w("05_utf16.csv", base.encode("utf-16"))
w("05b_utf16le_nobom.csv", base.encode("utf-16-le"))
w("06_latin1.csv", csvtext(h, [r[:3] + ["Caf\xe9 r\xe9sum\xe9 \xfcber invoice"] + r[4:] for r in rs]).encode("latin-1"))
w("07_gz.csv.gz", gzip.compress(base.encode()))
# zip of many files
b = io.BytesIO()
with zipfile.ZipFile(b, "w") as z:
    for k in range(5):
        hh, rr = rows_msgs(2)
        rr = [[x.replace("run0", "z%d_run0" % k) if isinstance(x, str) else x for x in r] for r in rr]
        z.writestr("part%d.csv" % k, csvtext(hh, rr))
    z.writestr("README.txt", "not a log\n")
    z.writestr("img.png", b"\x89PNG\r\n\x1a\n\x00\x00")
w("08_many.zip", b.getvalue())
w("09_header_line3.csv", "Exported by tool v2\n\n" + base)
w("10_units_in_names.csv", csvtext(["Run ID", "Conversation", "Role", "Message Text", "Time (UTC)", "Latency (ms)", "Prompt Tokens", "Completion Tokens", "Cost ($)"],
    [["r%d" % i, "c1", "user" if i % 2 == 0 else "assistant", "msg %d" % i, ts(i), i * 120, 10, 5, "$0.00%d" % i] for i in range(8)]))
w("11_no_runid.csv", csvtext(["role", "content", "timestamp"], [[r[2], r[3], r[4]] for r in rs]))
w("12_no_time_no_tokens.csv", csvtext(["run_id", "role", "content"], [[r[0], r[2], r[3]] for r in rs]))
w("13_no_text_no_tokens.csv", csvtext(["run_id", "role", "n"], [[r[0], r[2], i] for i, r in enumerate(rs)]))
# one row per tool call
tc = [["run%d" % (i // 3), "u%d" % (i // 3), "Q%d about refunds" % (i // 3), "lookup" if i % 2 else "search", '{"q":"x"}',
       "Traceback (most recent call last): boom" if i % 4 == 0 else "ok", ts(i), 0.5 + i / 10, "final answer %d" % (i // 3)] for i in range(12)]
w("14_tool_call_rows.csv", csvtext(["run_id", "user_id", "user_message", "tool_name", "tool_args", "tool_result", "start_time", "duration_s", "final_answer"], tc))
# wide rows
wide_head = ["session_id", "question", "answer", "tool_1_name", "tool_1_output", "tool_2_name", "tool_2_output", "total_tokens", "created_at"]
wide = [["s%d" % i, "How do I reset %d?" % i, "Go to settings", "kb_search", "HTTP 404 not found" if i % 2 else "ok", "send_mail" if i % 3 == 0 else "", "", 100 + i, 1772000000 + i * 60] for i in range(6)]
w("15_wide_rows.csv", csvtext(wide_head, wide))
# truncated / bad quotes
w("16_truncated_badquote.csv", base[:-37] + '\nrun99,c,user,"unterminated quote here,2026-03-01T10:00:00Z,1,1\nrun99,c,assistant,x')
# openai messages jsonl
def oa(i, err=False):
    m = [{"role": "system", "content": "You are helpful"}, {"role": "user", "content": "Weather in Paris %d?" % i},
         {"role": "assistant", "content": None, "tool_calls": [{"id": "call_%d" % i, "type": "function", "function": {"name": "get_weather", "arguments": '{"city":"Paris"}'}}]},
         {"role": "tool", "tool_call_id": "call_%d" % i, "content": "Error: city service timeout" if err else '{"temp":18}'},
         {"role": "assistant", "content": "It is 18C in Paris."}]
    return {"id": "chat%d" % i, "messages": m, "usage": {"prompt_tokens": 50, "completion_tokens": 9}, "created": 1772000000 + i}
w("17_openai_messages.jsonl", "\n".join(json.dumps(oa(i, i % 3 == 0)) for i in range(6)))
w("18_openai_array_nots.json", json.dumps([{"messages": o["messages"]} for o in (oa(i, i % 2 == 0) for i in range(4))]))
# legacy function_call
leg = {"messages": [{"role": "user", "content": "add 2 and 3"}, {"role": "assistant", "content": None, "function_call": {"name": "add", "arguments": "{\"a\":2,\"b\":3}"}},
       {"role": "function", "name": "add", "content": "5"}, {"role": "assistant", "content": "5"}]}
w("19_legacy_function_call.jsonl", "\n".join(json.dumps(leg) for _ in range(3)))
# anthropic blocks
def an(i, err=False):
    return {"id": "msg%d" % i, "model": "claude-x", "usage": {"input_tokens": 40, "output_tokens": 12}, "messages": [
        {"role": "user", "content": [{"type": "text", "text": "Search docs for topic %d" % i}]},
        {"role": "assistant", "content": [{"type": "text", "text": "ok"}, {"type": "tool_use", "id": "tu_%d" % i, "name": "search_docs", "input": {"q": "t"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tu_%d" % i, "content": [{"type": "text", "text": "boom exception raised"}] if err else "found 2", "is_error": err}]},
        {"role": "assistant", "content": [{"type": "text", "text": "Here are results"}]}]}
w("20_anthropic_blocks.jsonl", "\n".join(json.dumps(an(i, i % 2 == 0)) for i in range(5)))
# concatenated json no separators + truncated
w("21_concat_truncated.json", "".join(json.dumps(an(i)) for i in range(3)) + json.dumps(an(9))[:80])
# double encoded json strings in csv
w("22_double_encoded.csv", csvtext(["id", "messages"], [["d%d" % i, json.dumps(json.dumps(oa(i)["messages"]))] for i in range(3)]))
w("22b_json_in_cell.csv", csvtext(["id", "messages"], [["d%d" % i, json.dumps(oa(i)["messages"])] for i in range(3)]))
# no tool calls, only q/a
w("23_qa_only.csv", csvtext(["question", "answer"], [["What is %d+1?" % i, str(i + 1)] for i in range(5)]))
# huge field
w("24_huge_field.csv", csvtext(h, [rs[0][:3] + ["x" * 3_000_000] + rs[0][4:]] + rs[1:]))
# empty / header only / garbage
w("25_empty.csv", "")
w("26_header_only.csv", ",".join(h) + "\n")
w("27_garbage.bin", bytes(range(256)) * 20)
# mixed folder
w("28_mixed_dir/a.csv", base); w("28_mixed_dir/b.bin", b"\x00\x01\x02" * 50); w("28_mixed_dir/c.txt", "hello\n")
w("28_mixed_dir/d.jsonl", json.dumps(oa(1)) + "\n"); w("28_mixed_dir/e.pdf", b"%PDF-1.4\n\x00\x00")
# nulls NaN, secrets
nn = [list(r) for r in rs]; nn[0][3] = "NaN"; nn[1][4] = "null"; nn[2][5] = "N/A"; nn[3][1] = ""
w("29_nulls_nan.csv", csvtext(h, nn))
sec = [list(r) for r in rs]; sec[0][3] = "my email is jane.doe@example.com key sk-FAKEFAKEFAKEFAKEFAKE1234 reset"
w("30_secrets.csv", csvtext(h, sec))
# epoch variants
for unit, mul in (("s", 1), ("ms", 1000), ("us", 10**6), ("ns", 10**9)):
    w("31_epoch_%s.csv" % unit, csvtext(["run_id", "role", "content", "ts"], [[r[0], r[2], r[3], (1772000000 + i * 5) * mul] for i, r in enumerate(rs)]))
# scale
bh, br = rows_msgs(1500)
w("32_scale.csv", csvtext(bh, br))
# duplicate rows + out of order
dup = rs + rs[:5]; dup = dup[::-1]
w("33_dups_out_of_order.csv", csvtext(h, dup))
# role/content only chat, no ids
w("34_role_content_only.csv", csvtext(["role", "content"], [[r[2], r[3]] for r in rs]))
# chatgpt-style export mapping
w("35_chat_export_mapping.json", json.dumps([{"title": "t", "conversation_id": "cx1", "mapping": {"a": {"message": {"author": {"role": "user"}, "content": {"parts": ["hi there"]}, "create_time": 1772000000}}, "b": {"message": {"author": {"role": "assistant"}, "content": {"parts": ["hello"]}, "create_time": 1772000005}}}}]))

# ---- round 2 new deformations ----
w("36_crlf_bare_cr.csv", base.replace("\n", "\r\n")[:len(base)//2] + base.replace("\n", "\r")[len(base)//2:] if False else base.replace("\n", "\r"))
w("36b_crlf_mixed.csv", "".join(l + ("\r\n" if i % 2 else "\n") for i, l in enumerate(base.split("\n"))))
ml = [list(r) for r in rs]; ml[0][3] = 'line one\nline "two", with; delim | and\ttab\n\nline three'; ml[5][3] = "multi\r\nline"
w("37_multiline_quoted.csv", csvtext(h, ml, quoting=csv.QUOTE_ALL))
w("38_dup_empty_headers.csv", "run_id,role,content,content,,timestamp,\n" + "".join("%s,%s,%s,dupcol,,%s,\n" % (r[0], r[2], r[3], r[4]) for r in rs))
w("39_ragged_rows.csv", base.replace("\nrun01", ",extra1,extra2\nrun01").replace("run03,conv1,user", "run03,conv1"))
w("40_nul_padded_utf8.csv", base.replace("\n", "\x00\n").encode())
oab = []
for i in range(5):
    oab.append({"id": "batch_req_%d" % i, "custom_id": "req-%d" % i, "response": {"status_code": 200, "body": {"id": "chatcmpl-%d" % i, "created": 1772000000 + i,
        "choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [{"id": "c%d" % i, "type": "function", "function": {"name": "lookup", "arguments": "{}"}}]}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 3}}}, "request_body": {"messages": [{"role": "user", "content": "batch question %d" % i}]}})
w("41_openai_batch.jsonl", "\n".join(json.dumps(o) for o in oab))
lc = [{"thread_id": "t%d" % i, "messages": [{"type": "human", "content": "LangChain q %d" % i}, {"type": "ai", "content": "", "tool_calls": [{"name": "calc", "args": {"x": 1}, "id": "k%d" % i}]},
      {"type": "tool", "name": "calc", "tool_call_id": "k%d" % i, "content": "Error: division by zero" if i % 2 else "1", "status": "error" if i % 2 else "success"}, {"type": "ai", "content": "answer %d" % i}]} for i in range(4)]
w("42_langchain_messages.jsonl", "\n".join(json.dumps(x) for x in lc))
gm = [{"contents": [{"role": "user", "parts": [{"text": "gemini q %d" % i}]}, {"role": "model", "parts": [{"functionCall": {"name": "search", "args": {"q": "a"}}}]},
      {"role": "user", "parts": [{"functionResponse": {"name": "search", "response": {"error": "quota exceeded 429"} if i % 2 else {"result": "ok"}}}]}, {"role": "model", "parts": [{"text": "gemini a %d" % i}]}]} for i in range(4)]
w("43_gemini_contents.jsonl", "\n".join(json.dumps(x) for x in gm))
w("44_toolcalls_json_column.csv", csvtext(["session", "user_msg", "assistant_msg", "tool_calls"], [["s%d" % i, "q%d" % i, "a%d" % i,
   json.dumps([{"name": "fetch", "arguments": {"u": "x"}, "output": "HTTP 503 service unavailable" if i % 2 else "fine"}])] for i in range(5)]))
tsm = ["2026-03-01T10:00:00Z", "2026-03-01 10:00:05", "01/03/2026 10:00 AM", "2026-03-01T11:00:05+02:00", "Mar 1, 2026 10:00:20", "1772359230"]
w("45_mixed_timestamp_formats.csv", csvtext(["run_id", "role", "content", "timestamp"], [[r[0], r[2], r[3], tsm[i % 6]] for i, r in enumerate(rs)]))
syn = {"user": "Customer", "assistant": "Agent", "tool": "System"}
w("46_role_synonyms.csv", csvtext(["ticket", "speaker", "text", "time"], [[r[0], ["Customer", "Bot", "human", "gpt"][i % 4], r[3], r[4]] for i, r in enumerate(rs)]))
w("47_error_variants.csv", csvtext(["run_id", "role", "content", "tool_name", "tool_result"], [
  ["e1", "user", "go", "", ""], ["e1", "assistant", "done", "t1", '{"isError": true, "content": "x"}'],
  ["e2", "user", "go", "", ""], ["e2", "assistant", "done", "t2", '{"ok": false}'],
  ["e3", "user", "go", "", ""], ["e3", "assistant", "done", "t3", '{"status":"error","message":"m"}'],
  ["e4", "user", "go", "", ""], ["e4", "assistant", "done", "t4", "HTTP 429 Too Many Requests"],
  ["e5", "user", "go", "", ""], ["e5", "assistant", "done", "t5", "Request timed out after 30s"],
  ["e6", "user", "go", "", ""], ["e6", "assistant", "done", "t6", "all good, 0 errors found"]]))
w("48_single_column_lines.csv", "run0 user hello\nrun0 assistant hi there\nrun1 user bye\nrun1 assistant ok\n")
w("49_cp1252_smartquotes.csv", csvtext(h, [r[:3] + ["“quoted” – it’s €5"] + r[4:] for r in rs]).encode("cp1252"))
bz = io.BytesIO()
with zipfile.ZipFile(bz, "w") as z:
    z.writestr("a/b/one.csv.gz", gzip.compress(base.encode()))
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as z2: z2.writestr("deep.csv", base.replace("run0", "deep0"))
    z.writestr("nested.zip", inner.getvalue())
    z.writestr("__MACOSX/._one.csv", b"\x00\x05\x16\x07")
    z.writestr("bad.csv.gz", b"\x1f\x8b\x08corrupt")
w("50_zip_nested_gz.zip", bz.getvalue())
w("51_tsv_quoted_tabs.tsv", csvtext(h, [r[:3] + ["a\tb\tc \"q\" end"] + r[4:] for r in rs], "\t"))
w("52_duration_formats.csv", csvtext(["run_id", "role", "content", "tool_name", "duration"], [["d%d" % (i // 2), ["user", "assistant"][i % 2], "m%d" % i, "t" if i % 2 else "", ["1.2s", "350ms", "00:00:01.5", "2 min", "abc", "-5", "1e3"][i % 7]] for i in range(12)]))
w("53_thousands_cost_strings.csv", csvtext(["run_id", "role", "content", "prompt_tokens", "completion_tokens", "cost"], [["c%d" % (i // 2), ["user", "assistant"][i % 2], "m%d" % i, "1,234", "1 567", ["$0.0012", "0,0035 EUR", "free", "NaN"][i % 4]] for i in range(8)]))
w("54_bare_message_lists.jsonl", "\n".join(json.dumps(oa(i)["messages"]) for i in range(4)))
w("55_nonstring_content.jsonl", "\n".join(json.dumps({"id": "n%d" % i, "messages": [{"role": "user", "content": 12345}, {"role": "assistant", "content": {"a": [1, 2, {"b": None}]}}, {"role": "assistant", "content": [None, 3, "x"]}, {"role": "tool", "content": True}, 7, None, "stray string"]}) for i in range(3)))
w("56_lone_surrogates.jsonl", "\n".join('{"id":"u%d","messages":[{"role":"user","content":"emoji \\ud83d broken \\ude00 ok"},{"role":"assistant","content":"fine"}]}' % i for i in range(3)))
deep = "x"
for _ in range(1500): deep = {"k": deep}
w("57_deep_nesting.json", json.dumps({"id": "deep1", "messages": [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}], "meta": deep}) if False else '{"id":"deep1","messages":[{"role":"user","content":"hi"},{"role":"assistant","content":"yo"}],"meta":' + '{"k":' * 1500 + '"x"' + "}" * 1500 + "}")
w("58_no_roles_at_all.csv", csvtext(["id", "text", "when"], [["m%d" % i, "free text row %d" % i, ts(i)] for i in range(8)]))
w("59_all_dup_rows.csv", csvtext(h, rs[:4] * 50))
w("60_secrets_in_headers_and_args.csv", csvtext(["run_id", "role", "content", "api_key", "Authorization"], [[r[0], r[2], r[3] + " password=hunter2hunter2 Bearer abcdefghijklmnop1234", "sk-FAKEFAKEFAKEFAKEFAKE9999", "Bearer FAKETOKEN123456789"] for r in rs]))
w("61_many_tiny_files_dir/readme.md", "# nothing")
for k in range(60): w("61_many_tiny_files_dir/f%03d.csv" % k, csvtext(["run_id", "role", "content"], [["k%d" % k, "user", "q"], ["k%d" % k, "assistant", "a"]]))
w("62_json_lines_with_bom_each.jsonl", "".join("﻿" + json.dumps(oa(i)) + "\n" for i in range(3)))
w("63_html_export.html", "<html><body><table><tr><th>role</th><th>content</th></tr><tr><td>user</td><td>hi</td></tr></table></body></html>")
w("64_future_epoch_and_zero.csv", csvtext(["run_id", "role", "content", "ts"], [[r[0], r[2], r[3], [0, 99999999999999, -5, 1772000000][i % 4]] for i, r in enumerate(rs)]))
print("wrote", len(os.listdir(C)), "cases")
