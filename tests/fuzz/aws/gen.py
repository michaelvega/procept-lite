"""Generate deformed synthetic AWS agent-log fixtures (stdlib only). Usage: python3 gen.py"""
import csv, gzip, hashlib, io, json, os, random, zipfile
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
random.seed(7)


def hash(x):  # deterministic stand-in for builtin hash
    return int(hashlib.md5(str(x).encode()).hexdigest(), 16)

T0 = datetime(2026, 3, 2, 9, 0, 0, tzinfo=timezone.utc)
TOOLS = ["search_orders", "get_invoice", "send_email", "lookup_customer"]


def mkruns(n=8, sessions=3):
    runs = []
    for i in range(n):
        t = T0 + timedelta(minutes=3 * i)
        calls = []
        for j in range(random.choice([0, 1, 2, 3])):
            bad = (i + j) % 4 == 0
            calls.append({"name": random.choice(TOOLS), "args": {"q": "item %d" % (i * 10 + j)},
                          "result": ('{"status":"error","message":"timeout after 30s"}' if bad
                                     else '{"status":"ok","rows":%d}' % (j + 1)), "bad": bad,
                          "dur": round(random.uniform(.2, 3), 2)})
        runs.append({"id": "req-%04d" % i, "session": "sess-%d" % (i % sessions), "t": t,
                     "user": "Where is my order %d? contact user%d@example.com" % (1000 + i, i),
                     "answer": "Order %d ships tomorrow." % (1000 + i), "calls": calls,
                     "itok": 120 + i * 7, "otok": 40 + i, "dur": round(random.uniform(1, 9), 2),
                     "model": "anthropic.claude-3-haiku-20240307-v1:0"})
    return runs


def iso(t, z=True):
    return t.strftime("%Y-%m-%dT%H:%M:%S.123") + ("Z" if z else "")


def bedrock_inv(r, tokens=True, ts=True, drop_out=False, drop_in=False):
    rec = {"schemaType": "ModelInvocationLog", "schemaVersion": "1.0", "accountId": "123456789012",
           "region": "us-east-1", "requestId": r["id"], "operation": "Converse", "modelId": r["model"],
           "input": {"inputContentType": "application/json", "inputBodyJson": {"messages": [
               {"role": "user", "content": [{"text": r["user"]}]}]}},
           "output": {"outputContentType": "application/json", "outputBodyJson": {"output": {"message": {
               "role": "assistant", "content": [{"text": r["answer"]}] + [
                   {"toolUse": {"toolUseId": "tu%d" % k, "name": c["name"], "input": c["args"]}}
                   for k, c in enumerate(r["calls"])]}}, "stopReason": "end_turn"}}}
    if tokens:
        rec["input"]["inputTokenCount"] = r["itok"]
        rec["output"]["outputTokenCount"] = r["otok"]
        rec["output"]["outputBodyJson"]["usage"] = {"inputTokens": r["itok"], "outputTokens": r["otok"]}
    if ts:
        rec["timestamp"] = iso(r["t"])
    if drop_out:
        del rec["output"]
    if drop_in:
        del rec["input"]
    return rec


def agent_trace(r, ts=True):
    evs = []
    for k, c in enumerate(r["calls"]):
        evs.append({"sessionId": r["session"], "agentId": "AGT1", "eventTime": iso(r["t"] + timedelta(seconds=k)),
                    "trace": {"orchestrationTrace": {"invocationInput": {"invocationType": "ACTION_GROUP",
                              "actionGroupInvocationInput": {"actionGroupName": c["name"], "function": c["name"],
                                                             "parameters": [{"name": "q", "value": c["args"]["q"]}]}}}}})
        evs.append({"sessionId": r["session"], "agentId": "AGT1", "eventTime": iso(r["t"] + timedelta(seconds=k, milliseconds=500)),
                    "trace": {"orchestrationTrace": {"observation": {"type": "ACTION_GROUP",
                              "actionGroupInvocationOutput": {"text": c["result"]}}}}})
    evs.append({"sessionId": r["session"], "agentId": "AGT1", "eventTime": iso(r["t"] + timedelta(seconds=9)),
                "inputText": r["user"],
                "trace": {"orchestrationTrace": {"observation": {"type": "FINISH", "finalResponse": {"text": r["answer"]}}}}})
    if not ts:
        for e in evs:
            e.pop("eventTime")
    return evs


def sval(v):
    if isinstance(v, bool): return {"boolValue": v}
    if isinstance(v, int): return {"intValue": str(v)}
    if isinstance(v, float): return {"doubleValue": v}
    return {"stringValue": str(v)}


def otlp(runs, unit="ns", drop_time=False, dup=False, shuffle=False, orphan=False):
    spans = []
    def ep(t):
        s = t.timestamp()
        return str(int({"s": s, "ms": s * 1e3, "us": s * 1e6, "ns": s * 1e9}[unit]))
    for r in runs:
        tid = "%032x" % (hash(r["id"]) & (2**100 - 1))
        root = "%016x" % (abs(hash(r["id"] + "r")) & (2**60 - 1))
        t_end = r["t"] + timedelta(seconds=r["dur"])
        def sp(sid, parent, name, a, t1, t2, status=None):
            d = {"traceId": tid, "spanId": sid, "parentSpanId": parent, "name": name,
                 "attributes": [{"key": k, "value": sval(v)} for k, v in a.items()]}
            if not drop_time:
                d["startTimeUnixNano"] = ep(t1); d["endTimeUnixNano"] = ep(t2)
            if status: d["status"] = status
            return d
        spans.append(sp(root, "", "invoke_agent support", {"gen_ai.operation.name": "invoke_agent",
            "gen_ai.agent.name": "support", "session.id": r["session"], "gen_ai.prompt": r["user"],
            "gen_ai.completion": r["answer"], "gen_ai.usage.input_tokens": r["itok"],
            "gen_ai.usage.output_tokens": r["otok"], "gen_ai.request.model": r["model"]}, r["t"], t_end))
        for k, c in enumerate(r["calls"]):
            spans.append(sp("%016x" % (abs(hash(r["id"] + str(k))) & (2**60 - 1)), root, "execute_tool " + c["name"],
                {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": c["name"],
                 "gen_ai.tool.call.arguments": json.dumps(c["args"]), "gen_ai.tool.call.result": c["result"]},
                r["t"] + timedelta(seconds=k), r["t"] + timedelta(seconds=k + c["dur"]),
                {"code": 2, "message": "tool failed"} if c["bad"] else {"code": 1}))
        if orphan:
            spans.append(sp("%016x" % random.getrandbits(60), "deadbeefdeadbeef", "execute_tool ghost",
                            {"gen_ai.tool.name": "ghost"}, r["t"], t_end))
    if dup: spans += spans[:5]
    if shuffle: random.shuffle(spans)
    return {"resourceSpans": [{"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "agentcore-support"}}]},
                               "scopeSpans": [{"scope": {"name": "adot"}, "spans": spans}]}]}


def xray(runs):
    out = []
    for r in runs:
        st = r["t"].timestamp()
        out.append({"trace_id": "1-%08x-%024x" % (int(st), abs(hash(r["id"])) & (2**90 - 1)), "id": "%016x" % abs(hash(r["id"])) if False else "ab%014x" % int(st),
                    "name": "agent-lambda", "start_time": st, "end_time": st + r["dur"],
                    "annotations": {"session": r["session"], "user_message": r["user"], "final_answer": r["answer"]},
                    "subsegments": [{"id": "cd%014x" % (int(st) + k), "name": c["name"], "start_time": st + k,
                                     "end_time": st + k + c["dur"], "error": c["bad"], "fault": c["bad"],
                                     "metadata": {"input": c["args"], "output": c["result"]}}
                                    for k, c in enumerate(r["calls"])]})
    return out


def insights_rows(recs, noise=True):
    rows = []
    for rec in recs:
        rows.append({"@timestamp": (rec.get("timestamp") or iso(T0)).replace("T", " ").rstrip("Z"),
                     "@message": json.dumps(rec), "@logStream": "2026/03/02/[$LATEST]abc"})
        if noise:
            rid = rec.get("requestId", "x")
            rows.append({"@timestamp": iso(T0).replace("T", " ").rstrip("Z"), "@message": "START RequestId: %s Version: $LATEST" % rid, "@logStream": "s"})
            rows.append({"@timestamp": iso(T0).replace("T", " ").rstrip("Z"), "@message": "REPORT RequestId: %s Duration: 812.33 ms Billed Duration: 813 ms Memory Size: 512 MB Max Memory Used: 101 MB" % rid, "@logStream": "s"})
            rows.append({"@timestamp": iso(T0).replace("T", " ").rstrip("Z"), "@message": "END RequestId: %s" % rid, "@logStream": "s"})
    return rows


def csvtext(rows, delim=",", quote_bug=False, fields=None):
    fields = fields or list(rows[0].keys())
    b = io.StringIO()
    w = csv.DictWriter(b, fields, delimiter=delim, extrasaction="ignore")
    w.writeheader(); w.writerows(rows)
    s = b.getvalue()
    if quote_bug:
        s = s.replace('""', '"', 3)
    return s


def W(name, data, mode=None):
    p = os.path.join(HERE, "cases", name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    if isinstance(data, bytes): open(p, "wb").write(data)
    else: open(p, "w", encoding="utf-8", newline="").write(data)


def nd(recs): return "".join(json.dumps(x) + "\n" for x in recs)


def main():
    R = mkruns()
    inv = [bedrock_inv(r) for r in R]
    # 01 healthy insights csv bedrock
    W("01_healthy_insights_csv/export.csv", csvtext(insights_rows(inv)))
    # 02 healthy ndjson bedrock invocation logs
    W("02_healthy_bedrock_ndjson/inv.jsonl", nd(inv))
    # 03 healthy agents trace
    ev = [e for r in R for e in agent_trace(r)]
    W("03_healthy_agent_trace/trace.json", json.dumps(ev))
    # 04 healthy otlp
    W("04_healthy_otlp/spans.json", json.dumps(otlp(R)))
    # 05 healthy xray
    W("05_healthy_xray/segments.json", json.dumps(xray(R)))
    # 06 no timestamps bedrock + no timestamps agent trace
    W("06_no_timestamps/inv.jsonl", nd([bedrock_inv(r, ts=False) for r in R]))
    W("06b_no_timestamps_otlp/spans.json", json.dumps(otlp(R, drop_time=True)))
    # 07 no tokens (bedrock)
    W("07_no_tokens/inv.jsonl", nd([bedrock_inv(r, tokens=False) for r in R]))
    # 08 no text at all, no tokens
    def bare(r):
        x = bedrock_inv(r, tokens=False); x["input"] = {}; x["output"] = {}; return x
    W("08_no_text_no_tokens/inv.jsonl", nd([bare(r) for r in R]))
    # 09 renamed fields in insights csv + semicolon
    ren = []
    for rec in inv:
        ren.append({"time": rec["timestamp"], "msg": json.dumps(rec), "stream": "x"})
    W("09_renamed_semicolon/export.csv", csvtext(ren, delim=";"))
    # 10 double-encoded json in @message (json of json string)
    rows = insights_rows(inv, noise=False)
    for x in rows: x["@message"] = json.dumps(x["@message"])
    W("10_double_encoded/export.csv", csvtext(rows))
    # 11 truncated last line + bad csv quotes
    s = nd(inv); W("11_truncated_ndjson/inv.jsonl", s[:-60])
    W("11b_bad_csv_quotes/export.csv", csvtext(insights_rows(inv), quote_bug=True))
    # 12 encodings: BOM, UTF-16, gzip, zip
    W("12_encoded/bom.jsonl", b"\xef\xbb\xbf" + nd(inv).encode())
    W("12_encoded/utf16.jsonl", nd(inv).encode("utf-16"))
    W("12_encoded/inv.jsonl.gz", gzip.compress(nd(inv).encode()))
    zb = io.BytesIO()
    with zipfile.ZipFile(zb, "w") as z: z.writestr("inner/inv.jsonl", nd(inv))
    W("12b_zip/logs.zip", zb.getvalue())
    # 13 concatenated json, no newlines
    W("13_concatenated/inv.json", "".join(json.dumps(x) for x in inv))
    # 14 otlp deformed: epoch units, dup, shuffled, orphan
    for u in ("s", "ms", "us"):
        W("14_otlp_units/%s.json" % u, json.dumps(otlp(R, unit=u, dup=True, shuffle=True, orphan=True)))
    # 15 wrong types: nulls, NaN, strings for numbers
    bad = []
    for i, r in enumerate(R):
        x = bedrock_inv(r)
        x["input"]["inputTokenCount"] = ["abc", None, "12", float("nan"), -5, "1e3", {}, True][i]
        x["output"]["outputTokenCount"] = None if i % 2 else "7"
        if i == 3: x["timestamp"] = 1772442000   # epoch seconds int
        if i == 4: x["timestamp"] = "not a date"
        if i == 5: x["timestamp"] = None
        bad.append(x)
    W("15_wrong_types/inv.jsonl", nd(bad).replace("NaN", "NaN"))
    # 16 huge fields
    big = [bedrock_inv(r) for r in R[:4]]
    big[0]["input"]["inputBodyJson"]["messages"][0]["content"][0]["text"] = "lorem ipsum " * 400000
    big[1]["output"]["outputBodyJson"]["output"]["message"]["content"][0]["text"] = "x" * 3_000_000
    W("16_huge_fields/inv.jsonl", nd(big))
    # 17 scale: 3000 runs
    many = mkruns(3000, 300)
    W("17_scale/inv.jsonl", nd([bedrock_inv(r) for r in many]))
    # 18 mixed folder
    W("18_mixed_folder/inv.jsonl", nd(inv))
    W("18_mixed_folder/photo.png", bytes(random.getrandbits(8) for _ in range(500)))
    W("18_mixed_folder/notes.txt", "hello, not a log\n")
    W("18_mixed_folder/empty.json", "")
    W("18_mixed_folder/broken.json", "{not json")
    # 19 lambda noise only mixed with plain text lines
    lines = []
    for rec in inv:
        lines += ["START RequestId: %s Version: $LATEST" % rec["requestId"], json.dumps(rec),
                  "[INFO] 2026-03-02T09:00:00Z handler ok", "END RequestId: %s" % rec["requestId"],
                  "REPORT RequestId: %s Duration: 1.0 ms" % rec["requestId"]]
    W("19_lambda_noise_lines/log.txt", "\n".join(lines) + "\n")
    # 20 unknown schema: custom app log in CloudWatch
    cust = [{"@timestamp": iso(r["t"]), "@message": json.dumps({"q": r["user"], "a": r["answer"], "sid": r["session"],
            "steps": [{"fn": c["name"], "out": c["result"]} for c in r["calls"]]})} for r in R]
    W("20_unknown_schema/export.csv", csvtext(cust))
    # 21 no tool calls
    W("21_no_tools/inv.jsonl", nd([bedrock_inv(dict(r, calls=[])) for r in R]))
    # 22 missing user or answer
    miss = []
    for i, r in enumerate(R):
        x = bedrock_inv(r)
        if i % 2: x["output"]["outputBodyJson"]["output"]["message"]["content"] = []
        else: x["input"]["inputBodyJson"]["messages"] = []
        miss.append(x)
    W("22_missing_user_or_answer/inv.jsonl", nd(miss))
    # 23 missing run/request id and session id
    nid = []
    for r in R:
        x = bedrock_inv(r); x.pop("requestId"); nid.append(x)
    W("23_no_ids/inv.jsonl", nd(nid))
    # 24 agent trace without timestamps, errors only in text
    W("24_agent_trace_no_ts/trace.json", json.dumps([e for r in R for e in agent_trace(r, ts=False)]))
    # 25 empty and garbage input
    W("25_empty/empty.jsonl", "")
    W("26_garbage/garbage.bin", bytes(random.getrandbits(8) for _ in range(2000)))
    # 27 secrets in data
    sec = [bedrock_inv(r) for r in R[:3]]
    sec[0]["input"]["inputBodyJson"]["messages"][0]["content"][0]["text"] = "my key is AKIAABCDEFGHIJKLMNOP and sk-ant-api03-FAKEFAKEFAKEFAKE, mail bob@example.com"
    W("27_secrets/inv.jsonl", nd(sec))
    # 28 json array of Insights rows with ptr + tab-delimited
    W("28_tsv_pipe/export.tsv", csvtext(insights_rows(inv), delim="\t"))
    W("28_tsv_pipe/export.psv", csvtext(insights_rows(inv), delim="|"))
    # 29 xray with epoch ms strings & missing subsegment times
    xs = xray(R)
    for s in xs:
        for sub in s["subsegments"]: sub.pop("end_time")
    W("29_xray_partial/segments.json", json.dumps(xs))
    # 30 messages bedrock legacy invoke (anthropic body) with string body
    leg = []
    for r in R:
        x = bedrock_inv(r); x["operation"] = "InvokeModel"
        x["input"]["inputBodyJson"] = json.dumps({"anthropic_version": "bedrock-2023-05-31", "messages": [{"role": "user", "content": r["user"]}]})
        x["output"]["outputBodyJson"] = json.dumps({"content": [{"type": "text", "text": r["answer"]}], "usage": {"input_tokens": r["itok"], "output_tokens": r["otok"]}})
        leg.append(x)
    W("30_invokemodel_stringbody/inv.jsonl", nd(leg))


def extra():
    """Round-2 deformations (cases 31+)."""
    R = mkruns()
    inv = [bedrock_inv(r) for r in R]
    ts = lambda t: t.strftime("%Y-%m-%d %H:%M:%S.123")
    # 31 Insights GetQueryResults API shape: field/value cells
    res = {"results": [[{"field": "@timestamp", "value": ts(r["t"])},
                        {"field": "@message", "value": json.dumps(bedrock_inv(r))},
                        {"field": "@ptr", "value": "CmEKJgoi"}] for r in R], "statistics": {"recordsMatched": 8.0}, "status": "Complete"}
    W("31_insights_api_fieldvalue/results.json", json.dumps(res))
    # 32 Insights console JSON export: array of flat rows, plus empty-message rows
    rows = insights_rows(inv, noise=True) + [{"@timestamp": ts(T0), "@message": "", "@logStream": "s"}] * 3
    W("32_insights_json_array/export.json", json.dumps(rows))
    # 33 Multi-turn Converse where tool results (with error status) live in the next request's messages
    turns = []
    for r in R:
        x = bedrock_inv(r)
        msgs = [{"role": "user", "content": [{"text": r["user"]}]}]
        for k, c in enumerate(r["calls"]):
            msgs.append({"role": "assistant", "content": [{"toolUse": {"toolUseId": "tu%d" % k, "name": c["name"], "input": c["args"]}}]})
            msgs.append({"role": "user", "content": [{"toolResult": {"toolUseId": "tu%d" % k,
                         "status": "error" if c["bad"] else "success", "content": [{"text": c["result"]}]}}]})
        x["input"]["inputBodyJson"]["messages"] = msgs
        x["output"]["outputBodyJson"]["output"]["message"]["content"] = [{"text": r["answer"]}]
        turns.append(x)
    W("33_converse_toolresult_history/inv.jsonl", nd(turns))
    # 34 huge base64 image blob inside messages
    im = bedrock_inv(R[0])
    im["input"]["inputBodyJson"]["messages"][0]["content"].append({"image": {"format": "png", "source": {"bytes": "QUJD" * 2_000_000}}})
    W("34_base64_blob/inv.jsonl", nd([im] + [bedrock_inv(r) for r in R[1:]]))
    # 35 Bedrock Agents trace with failureTrace and rationale, session missing on some events
    ev = [e for r in R for e in agent_trace(r)]
    for i, e in enumerate(ev):
        if i % 5 == 0: e.pop("sessionId")
        if i % 7 == 0:
            e["trace"] = {"failureTrace": {"traceId": "t-%d" % i, "failureReason": "Lambda function returned HTTP 500 Internal Server Error"}}
    W("35_agent_trace_failure/trace.jsonl", nd(ev))
    # 36 only Lambda plain text lines (no JSON anywhere)
    ln = []
    for r in R:
        ln += ["START RequestId: %s Version: $LATEST" % r["id"], "[ERROR] Task timed out after 30.02 seconds",
               "END RequestId: %s" % r["id"], "REPORT RequestId: %s Duration: 30002.1 ms Billed Duration: 30000 ms" % r["id"]]
    W("36_lambda_text_only/log.txt", "\n".join(ln) + "\n")
    # 37 app structured logs (embedded-metric-ish) with level fields
    app = []
    for r in R:
        app.append({"timestamp": iso(r["t"]), "level": "INFO", "msg": "user_turn", "prompt": r["user"], "trace_id": r["session"]})
        for c in r["calls"]:
            app.append({"timestamp": iso(r["t"]), "level": "ERROR" if c["bad"] else "INFO", "msg": "tool", "toolName": c["name"],
                        "toolInput": c["args"], "toolOutput": c["result"], "trace_id": r["session"]})
        app.append({"timestamp": iso(r["t"]), "level": "INFO", "msg": "reply", "response": r["answer"], "trace_id": r["session"]})
    W("37_app_structured_logs/app.jsonl", nd(app))
    # 38 CSV with multi-line pretty JSON in @message, CRLF, BOM
    rws = [{"@timestamp": ts(r["t"]), "@message": json.dumps(bedrock_inv(r), indent=2), "@ptr": "p"} for r in R]
    W("38_csv_multiline_crlf_bom/export.csv", "﻿" + csvtext(rws).replace("\r\n", "\n").replace("\n", "\r\n"))
    # 39 unicode hell: emoji, RTL, NUL, lone surrogate escapes, control chars
    u = [bedrock_inv(r) for r in R]
    u[0]["input"]["inputBodyJson"]["messages"][0]["content"][0]["text"] = "café \U0001F600 ‮\u0000\u0007 \\ud800 end"
    raw = nd(u).replace("\\u0000", "\\u0000\\ud800")
    W("39_unicode_hell/inv.jsonl", raw)
    # 40 mixed timestamp formats
    mt = [bedrock_inv(r) for r in R]
    fm = ["2026-03-02T09:00:00.123456789Z", "2026-03-02T14:30:00+05:30", "Mar 2, 2026 9:09:00 AM", "1772442540123",
          "1772442720.5", "2026/03/02 09:12:00", "20260302T091500Z", "0000-00-00 00:00:00"]
    for x, f in zip(mt, fm): x["timestamp"] = f
    W("40_mixed_timestamp_formats/inv.jsonl", nd(mt))
    # 41 OTel spans with events (gen_ai.*.message) and dict attributes, string durations
    sp = []
    for r in R:
        tid = "%032x" % hash(r["id"]); rid = "%016x" % (hash(r["id"] + "r") & (2**60 - 1))
        sp.append({"trace_id": tid, "span_id": rid, "parent_span_id": None, "name": "invoke_agent", "kind": "SERVER",
                   "start_time": iso(r["t"]), "end_time": iso(r["t"] + timedelta(seconds=r["dur"])), "duration": str(r["dur"]) + "s",
                   "attributes": {"gen_ai.operation.name": "invoke_agent", "session.id": r["session"],
                                  "gen_ai.usage.input_tokens": str(r["itok"]), "gen_ai.usage.output_tokens": str(r["otok"])},
                   "events": [{"name": "gen_ai.user.message", "attributes": {"content": r["user"]}},
                              {"name": "gen_ai.choice", "attributes": {"message": json.dumps({"content": r["answer"]})}}]})
        for k, c in enumerate(r["calls"]):
            sp.append({"trace_id": tid, "span_id": "%016x" % (hash(r["id"] + str(k)) & (2**60 - 1)), "parent_span_id": rid,
                       "name": "execute_tool " + c["name"], "start_time": iso(r["t"] + timedelta(seconds=k)),
                       "end_time": iso(r["t"] + timedelta(seconds=k + c["dur"])),
                       "attributes": {"gen_ai.tool.name": c["name"], "gen_ai.tool.call.result": c["result"]},
                       "events": [{"name": "exception", "attributes": {"exception.type": "TimeoutError", "exception.message": "timeout"}}] if c["bad"] else []})
    W("41_otel_events_dict_attrs/spans.json", json.dumps(sp))
    # 42 X-Ray BatchGetTraces with Document stored as JSON string
    xs = xray(R); bt = {"Traces": []}
    for s in xs:
        bt["Traces"].append({"Id": s["trace_id"], "Duration": s["end_time"] - s["start_time"],
                             "Segments": [{"Id": s["id"], "Document": json.dumps(s)}]})
    W("42_xray_batchget_document_string/traces.json", json.dumps(bt))
    # 43 CloudWatch subscription (DATA_MESSAGE) + S3-export style "<ts> <json>" lines
    sub = {"messageType": "DATA_MESSAGE", "owner": "123456789012", "logGroup": "/aws/bedrock/modelinvocations",
           "logStream": "s", "logEvents": [{"id": str(i), "timestamp": int(r["t"].timestamp() * 1000), "message": json.dumps(bedrock_inv(r))} for i, r in enumerate(R)]}
    W("43_subscription_and_s3_lines/sub.json", json.dumps(sub))
    W("43_subscription_and_s3_lines/s3.log", "".join("%s %s\n" % (iso(r["t"]), json.dumps(bedrock_inv(r))) for r in R))
    # 44 mislabelled compression: gz named .json, plain named .gz, double gzip
    W("44_mislabelled_compression/a.json", gzip.compress(nd(inv).encode()))
    W("44_mislabelled_compression/b.jsonl.gz", nd(inv).encode())
    W("44_mislabelled_compression/c.jsonl.gz", gzip.compress(gzip.compress(nd(inv).encode())))
    # 45 verbatim duplicate records across files (retries)
    W("45_duplicate_files/a.jsonl", nd(inv)); W("45_duplicate_files/b.jsonl", nd(inv + inv[:3]))
    # 46 absurd timestamps / negative durations
    ab = otlp(R[:4]); spans = ab["resourceSpans"][0]["scopeSpans"][0]["spans"]
    for i, s in enumerate(spans):
        if i % 3 == 0: s["startTimeUnixNano"], s["endTimeUnixNano"] = s["endTimeUnixNano"], s["startTimeUnixNano"]
        if i % 5 == 0: s["startTimeUnixNano"] = "0"
        if i % 7 == 0: s["endTimeUnixNano"] = "253402300800000000000"
    W("46_absurd_times/spans.json", json.dumps(ab))
    # 47 errors only in result text variants, no status
    pats = ["Traceback (most recent call last):\n  File x\nValueError: bad", "HTTP 503 Service Unavailable", '{"isError": true}',
            '{"ok": false, "reason": "nope"}', "ThrottlingException: Rate exceeded", "AccessDeniedException", "Task timed out after 30.00 seconds", "all good, 200 OK"]
    o = otlp(mkruns()); sp = o["resourceSpans"][0]["scopeSpans"][0]["spans"]; n = 0
    for s in sp:
        for a in s["attributes"]:
            if a["key"] == "gen_ai.tool.call.result":
                a["value"] = {"stringValue": pats[n % 8]}; n += 1
        s.pop("status", None)
    W("47_error_text_patterns/spans.json", json.dumps(o))
    # 48 deeply nested JSON
    W("48_deep_nesting/deep.json", '{"a":' * 6000 + "1" + "}" * 6000)
    W("48_deep_nesting/inv.jsonl", nd(inv))
    # 49 single huge line array
    W("49_one_line_20mb/inv.json", json.dumps([bedrock_inv(r) for r in mkruns(5000, 50)]))
    # 50 ndjson with scalar/array/null junk lines
    W("50_junk_lines/inv.jsonl", "null\n42\n\"str\"\n[]\n[1,2]\n{}\n" + nd(inv) + "true\n")
    # 51 ragged CSV: duplicate header names, empty header, short/long rows
    W("51_ragged_csv/export.csv", '@timestamp,@message,@message,,x\n' + "".join(
        '%s,%s,dup,e,%s\n' % (ts(r["t"]), '"%s"' % json.dumps(bedrock_inv(r)).replace('"', '""'), "k," * (i % 3)) for i, r in enumerate(R)) + "short\n")
    # 52 latin-1 bytes and utf-16 BE without BOM
    W("52_encodings/latin1.jsonl", nd(inv).replace("ships", "shíps").encode("latin-1"))
    W("52_encodings/utf16be.jsonl", nd(inv).encode("utf-16-be"))
    # 53 key name variants (snake_case / PascalCase)
    kv = []
    for r in R:
        x = bedrock_inv(r)
        kv.append({"Timestamp": x["timestamp"], "RequestId": r["id"], "ModelId": x["modelId"], "Input": x["input"], "Output": x["output"]})
    W("53_pascal_keys/inv.jsonl", nd(kv))
    # 54 secrets in every field type; checked for echo by runall
    sc = [bedrock_inv(r) for r in R[:3]]
    sc[0]["requestId"] = "req-AKIAABCDEFGHIJKLMNOP"
    sc[1]["input"]["inputBodyJson"]["messages"][0]["content"][0]["text"] = "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.FAKE.FAKE and password=hunter2hunter2 ghp_FAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKE0000"
    W("54_secrets_in_ids/inv.jsonl", nd(sc))
    # 55 requestId reused across different conversations (Lambda retry/warm reuse)
    ru = [bedrock_inv(r) for r in R]
    for x in ru: x["requestId"] = "same-id"
    W("55_requestid_reused/inv.jsonl", nd(ru))
    # 56 shuffled agent trace events
    ev = [e for r in R for e in agent_trace(r)]; random.shuffle(ev)
    W("56_agent_trace_shuffled/trace.json", json.dumps(ev))
    # 57 zip with junk: macosx dir, nested zip, empty dir, bad member
    zb = io.BytesIO()
    with zipfile.ZipFile(zb, "w") as z:
        z.writestr("__MACOSX/._inv.jsonl", b"\x00\x05\x16\x07junk"); z.writestr("d/", ""); z.writestr("d/inv.jsonl", nd(inv))
        z.writestr("d/bad.gz", b"notgzip"); z.writestr("../../evil.jsonl", nd(inv[:1]))
        inner = io.BytesIO()
        with zipfile.ZipFile(inner, "w") as z2: z2.writestr("n.jsonl", nd(inv))
        z.writestr("nested.zip", inner.getvalue())
    W("57_zip_junk/logs.zip", zb.getvalue())
    # 58 trivial documents
    for n, c in (("ws", "  \n\t \n"), ("arr", "[]"), ("obj", "{}"), ("null", "null"), ("hdr", "@timestamp,@message\n")):
        W("58_trivial_%s/x.json" % n, c)
    # 59 tool name oddities: empty, null, huge, unicode, duplicates
    tn = [bedrock_inv(r) for r in R]
    for x in tn:
        for tu in x["output"]["outputBodyJson"]["output"]["message"]["content"]:
            if "toolUse" in tu: tu["toolUse"]["name"] = random.choice(["", None, "t" * 5000, "工具/☃", "a b\tc\nd", "../../etc"])
    W("59_tool_name_oddities/inv.jsonl", nd(tn))
    # 60 numeric strings / absurd token numbers, bool tokens, huge ints, floats
    nm = [bedrock_inv(r) for r in R]
    for x, (a, b) in zip(nm, [(1e30, 5), (10**40, "NaN"), ("Infinity", -1), (True, False), ("1,234", "5k"), (0, 0), (3.7, 2.2), ([1], {"a": 1})]):
        x["input"]["inputTokenCount"] = a; x["output"]["outputTokenCount"] = b
    W("60_absurd_numbers/inv.jsonl", nd(nm).replace("1e+30", "1e30"))
    # 61 timestamps all identical + all runs same user message (collapse risk)
    same = [bedrock_inv(dict(r, user="hi", answer="hello", t=T0)) for r in R]
    for x in same: x.pop("requestId")
    W("61_identical_no_ids/inv.jsonl", nd(same))
    # 62 symlink loop and unreadable file in folder
    d = os.path.join(HERE, "cases", "62_symlink_perm"); os.makedirs(d, exist_ok=True)
    W("62_symlink_perm/inv.jsonl", nd(inv)); W("62_symlink_perm/secret.jsonl", nd(inv))
    try:
        os.chmod(os.path.join(d, "secret.jsonl"), 0)
        lk = os.path.join(d, "loop")
        if not os.path.lexists(lk): os.symlink(".", lk)
    except OSError: pass


if __name__ == "__main__":
    main()
    extra()
