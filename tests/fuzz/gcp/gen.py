#!/usr/bin/env python3
"""Generate deformed synthetic GCP-style agent log fixtures (stdlib only)."""
import csv, gzip, io, json, os, random, zipfile, datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "fixtures")
T0 = dt.datetime(2026, 3, 1, 9, 0, 0, tzinfo=dt.timezone.utc)
TOOLS = ["search_docs", "get_order", "send_email_stub", "calc_refund"]


def iso(s, z=True):
    t = (T0 + dt.timedelta(seconds=s)).strftime("%Y-%m-%dT%H:%M:%S.%f")
    return t + ("Z" if z else "")


def scenario(n_runs=6):
    """list of runs: trace_id, session, user msg, answer, tool list, tokens."""
    runs = []
    for i in range(n_runs):
        tools = []
        for j in range(i % 3 + (1 if i % 4 == 3 else 0)):
            bad = (i % 4 == 3 and j == 0)
            tools.append({"name": TOOLS[(i + j) % 4], "args": {"q": "item %d" % i},
                          "result": "Error 503: upstream timeout" if bad else "ok: 3 rows for item %d" % i,
                          "start": i * 100 + 5 + j * 4, "end": i * 100 + 7 + j * 4})
        runs.append({"i": i, "trace": "%032x" % (0xabc000 + i), "session": "sess-%d" % (i // 2),
                     "user": "Where is order %d? contact me at user%d@example.com" % (1000 + i, i),
                     "answer": "Order %d ships tomorrow." % (1000 + i), "tools": tools,
                     "t0": i * 100, "t1": i * 100 + 20, "tin": 120 + i * 10, "tout": 40 + i})
    return runs


def logging_entries(runs, payload="json", sev_err=True):
    e = []
    for r in runs:
        base = {"logName": "projects/demo-proj/logs/agent", "resource": {"type": "aiplatform.googleapis.com/ReasoningEngine",
                "labels": {"reasoning_engine_id": "re-1"}}, "trace": "projects/demo-proj/traces/" + r["trace"],
                "labels": {"session_id": r["session"]}}
        e.append(dict(base, timestamp=iso(r["t0"]), severity="INFO", insertId="u%d" % r["i"],
                      jsonPayload={"event": "user_message", "text": r["user"]}))
        for k, t in enumerate(r["tools"]):
            e.append(dict(base, timestamp=iso(t["start"]), severity="INFO", insertId="t%d_%d" % (r["i"], k),
                          jsonPayload={"event": "tool_call", "tool": t["name"], "args": t["args"]}))
            bad = "Error" in t["result"]
            e.append(dict(base, timestamp=iso(t["end"]), severity="ERROR" if bad and sev_err else "INFO",
                          insertId="tr%d_%d" % (r["i"], k),
                          jsonPayload={"event": "tool_result", "tool": t["name"], "result": t["result"]}))
        e.append(dict(base, timestamp=iso(r["t1"]), severity="INFO", insertId="a%d" % r["i"],
                      jsonPayload={"event": "final_answer", "text": r["answer"],
                                   "usage": {"input_tokens": r["tin"], "output_tokens": r["tout"]}}))
    return e


def trace_spans(runs, attr_style="plain"):
    spans = []
    for r in runs:
        root = "r%015x" % r["i"]
        def A(d):
            if attr_style == "otlp":
                return [{"key": k, "value": {"stringValue": str(v)} if not isinstance(v, int) else {"intValue": str(v)}}
                        for k, v in d.items()]
            return d
        spans.append({"traceId": r["trace"], "spanId": root, "name": "invoke_agent demo",
                      "startTime": iso(r["t0"]), "endTime": iso(r["t1"]),
                      "labels" if attr_style == "plain" else "attributes": A({
                          "gen_ai.operation.name": "invoke_agent", "gen_ai.conversation.id": r["session"],
                          "gen_ai.prompt": r["user"], "gen_ai.completion": r["answer"],
                          "gen_ai.usage.input_tokens": r["tin"], "gen_ai.usage.output_tokens": r["tout"]})})
        for k, t in enumerate(r["tools"]):
            bad = "Error" in t["result"]
            sp = {"traceId": r["trace"], "spanId": "s%03d%012x" % (k, r["i"]), "parentSpanId": root,
                  "name": "execute_tool " + t["name"], "startTime": iso(t["start"]), "endTime": iso(t["end"]),
                  "labels" if attr_style == "plain" else "attributes": A({
                      "gen_ai.tool.name": t["name"], "gen_ai.tool.call.arguments": json.dumps(t["args"]),
                      "gen_ai.tool.call.result": t["result"]})}
            if bad:
                sp["status"] = {"code": 2, "message": t["result"]}
            spans.append(sp)
    return spans


def bq_rows(runs):
    rows = []
    for r in runs:
        rows.append({"insertId": "b%d" % r["i"], "timestamp": iso(r["t0"], False).replace("T", " ") + " UTC",
                     "severity": "ERROR" if any("Error" in t["result"] for t in r["tools"]) else "INFO",
                     "trace": "projects/demo/traces/" + r["trace"],
                     "labels": json.dumps({"session_id": r["session"]}),
                     "jsonPayload": json.dumps({"user_message": r["user"], "final_answer": r["answer"],
                                                "tool_calls": [{"name": t["name"], "args": t["args"], "result": t["result"]} for t in r["tools"]],
                                                "usage": {"input_tokens": r["tin"], "output_tokens": r["tout"]}})})
    return rows


def w(name, data, mode="w", enc="utf-8"):
    p = os.path.join(OUT, name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    if isinstance(data, bytes):
        open(p, "wb").write(data)
    else:
        open(p, mode, encoding=enc, newline="").write(data)


def nd(items):
    return "\n".join(json.dumps(x) for x in items) + "\n"


def csv_text(rows, delim=",", quote=csv.QUOTE_MINIMAL):
    b = io.StringIO()
    wr = csv.DictWriter(b, fieldnames=list(rows[0].keys()), delimiter=delim, quoting=quote)
    wr.writeheader(); wr.writerows(rows)
    return b.getvalue()


def main():
    random.seed(7)
    runs = scenario()
    L = logging_entries(runs)
    # 01 healthy baselines
    w("01_baseline_logging.ndjson", nd(L))
    w("01b_baseline_trace.json", json.dumps(trace_spans(runs), indent=1))
    w("01c_baseline_bq.csv", csv_text(bq_rows(runs)))
    # 02 missing timestamps everywhere
    L2 = [{k: v for k, v in e.items() if k != "timestamp"} for e in L]
    w("02_no_timestamps.ndjson", nd(L2))
    # 03 no token/usage field, text present
    L3 = json.loads(json.dumps(L))
    for e in L3:
        e["jsonPayload"].pop("usage", None)
    w("03_no_tokens.ndjson", nd(L3))
    # 04 no tokens and no text at all (tool names only)
    L4 = []
    for e in L:
        p = e["jsonPayload"]
        L4.append(dict(e, jsonPayload={"event": p["event"], **({"tool": p["tool"]} if "tool" in p else {})}))
    w("04_no_tokens_no_text.ndjson", nd(L4))
    # 05 no explicit error field: severity all INFO, error only in text
    w("05_errors_only_in_text.ndjson", nd(logging_entries(runs, sev_err=False)))
    # 06 no tool calls at all
    w("06_no_tools.ndjson", nd(logging_entries([dict(r, tools=[]) for r in runs])))
    # 07 renamed fields (textPayload / protoPayload style)
    L7 = []
    for e in L:
        p = e["jsonPayload"]
        n = {k: v for k, v in e.items() if k != "jsonPayload"}
        n["protoPayload"] = {"@type": "type.googleapis.com/google.cloud.audit.AuditLog",
                             "methodName": p["event"], "request": {"content": p.get("text") or p.get("args"), "toolName": p.get("tool")},
                             "response": {"content": p.get("result")}}
        L7.append(n)
    w("07_protopayload_renamed.ndjson", nd(L7))
    L7b = [dict({k: v for k, v in e.items() if k != "jsonPayload"},
                textPayload="%s %s" % (e["jsonPayload"]["event"], json.dumps({k: v for k, v in e["jsonPayload"].items() if k != "event"})))
           for e in L]
    w("07b_textpayload_only.ndjson", nd(L7b))
    # 08 wrong types: tokens as strings, NaN, null, timestamps epoch ms / ns / weird
    L8 = json.loads(json.dumps(L))
    for n, e in enumerate(L8):
        m = n % 4
        e["timestamp"] = [int((T0.timestamp() + 5 * n) * 1000), str(int((T0.timestamp() + 5 * n) * 1e9)),
                          iso(5 * n, False), None][m]
        u = e["jsonPayload"].get("usage")
        if u:
            u["input_tokens"] = "%d" % u["input_tokens"] if m % 2 else None
            u["output_tokens"] = "NaN"
    txt = nd(L8).replace('"output_tokens": "NaN"', '"output_tokens": NaN')
    w("08_wrong_types.ndjson", txt)
    # 09 truncated last line + garbage line
    t = nd(L)
    w("09_truncated.ndjson", t[: len(t) - 60] + "\n")
    w("09b_garbage_lines.ndjson", t.replace("\n", "\n<<<not json>>>\n", 3))
    # 10 encoded: BOM, utf-16, gzip, zip
    w("10_bom.ndjson", b"\xef\xbb\xbf" + nd(L).encode())
    w("10b_utf16.ndjson", nd(L).encode("utf-16"))
    w("10c_logs.ndjson.gz", gzip.compress(nd(L).encode()))
    zb = io.BytesIO()
    with zipfile.ZipFile(zb, "w") as z:
        z.writestr("export/logs-0001.json", nd(L)); z.writestr("export/readme.txt", "hello")
    w("10d_logs.zip", zb.getvalue())
    # 11 layouts: JSON array, concatenated pretty JSON, wrapped {"entries": [...]}
    w("11_array.json", json.dumps(L))
    w("11b_concatenated.json", "".join(json.dumps(e, indent=2) + "\n" for e in L))
    w("11c_wrapped_entries.json", json.dumps({"entries": L, "nextPageToken": "x"}))
    # 12 double-encoded JSON payloads, and OTLP attribute lists
    L12 = [dict(e, jsonPayload=json.dumps(e["jsonPayload"])) for e in L]
    w("12_double_encoded.ndjson", nd(L12))
    w("12b_whole_line_double_encoded.ndjson", "\n".join(json.dumps(json.dumps(e)) for e in L) + "\n")
    w("12c_trace_otlp_attrs.json", json.dumps(trace_spans(runs, "otlp")))
    # 13 BigQuery sink CSV variants
    rows = bq_rows(runs)
    w("13_bq_semicolon.csv", csv_text(rows, ";"))
    w("13b_bq_tab.tsv", csv_text(rows, "\t"))
    w("13c_bq_pipe_badquotes.csv", csv_text(rows, "|", csv.QUOTE_NONE if False else csv.QUOTE_MINIMAL).replace('""', '"', 4))
    w("13d_bq_nested_struct.ndjson", nd([{"insertId": r["insertId"], "timestamp": r["timestamp"], "severity": r["severity"],
                                         "trace": r["trace"], "labels": json.loads(r["labels"]),
                                         "jsonPayload": json.loads(r["jsonPayload"])} for r in rows]))
    # 14 trace spans: duplicates, orphans, out-of-order, no root
    S = trace_spans(runs)
    S14 = S + S[:5]
    random.shuffle(S14)
    S14.append({"traceId": "f" * 32, "spanId": "orph1", "parentSpanId": "missing", "name": "execute_tool search_docs",
                "startTime": iso(900), "endTime": iso(901)})
    w("14_dup_orphan_shuffled.json", json.dumps(S14))
    w("14b_trace_no_root.json", json.dumps([s for s in S if "parentSpanId" in s]))
    # 15 missing ids: no insertId/trace/session
    L15 = [{k: v for k, v in e.items() if k not in ("insertId", "trace", "labels")} for e in L]
    w("15_no_ids.ndjson", nd(L15))
    # 16 missing user msg / missing final answer
    L16 = [e for e in L if e["jsonPayload"]["event"] != "user_message"]
    w("16_no_user_message.ndjson", nd(L16))
    L16b = [e for e in L if e["jsonPayload"]["event"] != "final_answer"]
    w("16b_no_final_answer.ndjson", nd(L16b))
    # 17 huge fields
    L17 = json.loads(json.dumps(L))
    L17[1]["jsonPayload"]["text"] = "lorem ipsum " * 400000
    L17[3]["jsonPayload"]["args"] = {"blob": "x" * 3000000}
    w("17_huge_fields.ndjson", nd(L17))
    # 18 scale: 3000 runs
    big = scenario(3000)
    w("18_scale_3000runs.ndjson", nd(logging_entries(big)))
    # 19 mixed folder
    d = "19_mixed_folder/"
    w(d + "a.ndjson", nd(L[:20])); w(d + "b.ndjson", nd(L[20:]))
    w(d + "image.png", b"\x89PNG\r\n\x1a\n\x00\x00garbage")
    w(d + "notes.txt", "meeting notes, nothing here")
    w(d + "empty.json", "")
    w(d + "broken.json.gz", b"\x1f\x8b\x08\x00not really gzip")
    # 20 Vertex ADK event list (author/content/parts, function_call/response)
    ev = []
    for r in runs:
        ev.append({"id": "e%du" % r["i"], "invocation_id": "inv-%d" % r["i"], "author": "user", "timestamp": T0.timestamp() + r["t0"],
                   "content": {"role": "user", "parts": [{"text": r["user"]}]}})
        for t in r["tools"]:
            ev.append({"id": "e%d%s" % (r["i"], t["name"]), "invocation_id": "inv-%d" % r["i"], "author": "agent", "timestamp": T0.timestamp() + t["start"],
                       "content": {"role": "model", "parts": [{"function_call": {"name": t["name"], "args": t["args"]}}]}})
            ev.append({"id": "e%dr%s" % (r["i"], t["name"]), "invocation_id": "inv-%d" % r["i"], "author": "agent", "timestamp": T0.timestamp() + t["end"],
                       "content": {"role": "user", "parts": [{"function_response": {"name": t["name"], "response": {"result": t["result"]}}}]}})
        ev.append({"id": "e%df" % r["i"], "invocation_id": "inv-%d" % r["i"], "author": "agent", "timestamp": T0.timestamp() + r["t1"],
                   "content": {"role": "model", "parts": [{"text": r["answer"]}]},
                   "usage_metadata": {"prompt_token_count": r["tin"], "candidates_token_count": r["tout"]}})
    w("20_adk_events.json", json.dumps({"session_id": "adk-s1", "events": ev}))
    w("20b_adk_events_no_ts_no_usage.ndjson", nd([{k: v for k, v in e.items() if k not in ("timestamp", "usage_metadata")} for e in ev]))
    # 21 unknown schema (custom keys) and totally empty / unreadable
    w("21_unknown_schema.ndjson", nd([{"ts_x": iso(r["t0"]), "who": "u1", "ask": r["user"], "reply": r["answer"],
                                      "steps": [{"fn": t["name"], "out": t["result"]} for t in r["tools"]]} for r in runs]))
    w("21b_empty_file.json", "")
    w("21c_binary_junk.json", bytes(random.randrange(256) for _ in range(500)))
    w("21d_just_array_of_strings.json", json.dumps(["a", "b", 3, None]))
    # 22 secrets in data
    L22 = json.loads(json.dumps(L))
    L22[0]["jsonPayload"]["text"] = "my key is sk-FAKEFAKEFAKEFAKE1234567890 and Bearer abcdefFAKEtoken.xyz"
    L22[1]["jsonPayload"]["args"] = {"password": "hunter2-fake", "api_key": "AIzaFAKEFAKEFAKEFAKEFAKEFAKEFAKE123"}
    w("22_secrets.ndjson", nd(L22))
    # 23 errors as ok:false / isError / status string / traceback in result
    L23 = json.loads(json.dumps(logging_entries(runs, sev_err=False)))
    forms = ['{"ok": false, "detail": "denied"}', '{"isError": true}', '{"status":"error"}',
             "Traceback (most recent call last):\n  File \"x.py\", line 1\nValueError: bad", "HTTP 429 Too Many Requests"]
    k = 0
    for e in L23:
        if e["jsonPayload"].get("event") == "tool_result":
            e["jsonPayload"]["result"] = forms[k % len(forms)]; k += 1
    w("23_error_forms.ndjson", nd(L23))
    # 24 conversation id only derivable by time gap (no session, no trace)
    L24 = [{k: v for k, v in e.items() if k not in ("trace", "labels")} for e in L]
    w("24_conv_by_gap.ndjson", nd(L24))
    # 25 NEW: error only via httpRequest.status / protoPayload.status.code, tool_result text neutral
    L25 = json.loads(json.dumps(logging_entries(runs, sev_err=False)))
    for e in L25:
        p = e["jsonPayload"]
        if p.get("event") == "tool_result" and "Error" in p["result"]:
            p["result"] = "upstream unavailable"; e["httpRequest"] = {"status": 503}
    w("25_http_status_error.ndjson", nd(L25))
    # 26 CRLF line endings, NUL bytes, lone surrogates escaped, tabs in text
    t26 = nd(L).replace("\n", "\r\n").replace("Order", "Or\\u0000der")
    w("26_crlf_nul.ndjson", t26)
    # 27 timestamps as mixed units in one file (s, ms, us, ns) plus timestamp object {seconds,nanos}
    L27 = json.loads(json.dumps(L))
    for n, e in enumerate(L27):
        b = T0.timestamp() + 3 * n
        e["timestamp"] = [b, int(b * 1e3), int(b * 1e6), {"seconds": int(b), "nanos": 5}][n % 4]
    w("27_mixed_epoch_units.ndjson", nd(L27))
    # 28 severity as numeric / lowercase / DEFAULT, and labels as list of k/v pairs
    L28 = json.loads(json.dumps(logging_entries(runs)))
    for n, e in enumerate(L28):
        e["severity"] = [500, "error", "DEFAULT", "Critical"][n % 4] if e["severity"] == "ERROR" else e["severity"]
        e["labels"] = [{"key": "session_id", "value": e["labels"]["session_id"]}]
    w("28_severity_forms_list_labels.ndjson", nd(L28))
    # 29 Cloud Logging export with entries nested under "logEntries" inside a double-encoded string
    w("29_wrapped_string.json", json.dumps({"logEntries": json.dumps(L)}))
    # 30 payload keys in different case / camelCase (userMessage, finalAnswer, toolCalls)
    L30 = []
    for r in runs:
        L30.append({"insertId": "c%d" % r["i"], "timestamp": iso(r["t0"]), "trace": "projects/p/traces/" + r["trace"],
                    "jsonPayload": {"UserMessage": r["user"], "FinalAnswer": r["answer"],
                                    "ToolCalls": [{"Name": t["name"], "Args": t["args"], "Result": t["result"]} for t in r["tools"]]}})
    w("30_camel_pascal_keys.ndjson", nd(L30))
    # 31 leading junk / preamble before JSON and trailing comma array
    w("31_preamble_trailing_comma.json", "Exported by tool v1\n" + json.dumps(L, indent=0).rstrip("]").rstrip() + ",\n]")
    # 32 only user messages (no tools, no answers, no tokens)
    w("32_user_only.ndjson", nd([e for e in L if e["jsonPayload"]["event"] == "user_message"]))
    print("wrote", len(os.listdir(OUT)), "entries to", OUT)


if __name__ == "__main__":
    main()
