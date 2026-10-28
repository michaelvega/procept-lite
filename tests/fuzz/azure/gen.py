"""Generate deformed synthetic Azure (App Insights / Log Analytics / Foundry) fixtures. stdlib only."""
import csv, gzip, io, json, os, zipfile

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cases")
TOPICS = [("reset my password", "Done, check your inbox.", "account_api"),
          ("export my data", "Your export is ready.", "export_api"),
          ("what is my balance", "Your balance is 42 units.", "ledger_api"),
          ("cancel order 17", "Order 17 cancelled.", "orders_api")]


def rows(n=6, ms=True, dimkey="gen_ai", fail_every=3):
    """Return list of App Insights style row dicts (requests, dependencies, traces)."""
    out = []
    for i in range(n):
        q, a, tool = TOPICS[i % len(TOPICS)]
        op = "op%03d" % i
        t0 = 1741000000 + i * 60
        iso = lambda s: __import__("datetime").datetime.utcfromtimestamp(s).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        fail = fail_every and i % fail_every == 2
        out.append(dict(itemType="request", timestamp=iso(t0), id=op + "r", operation_Id=op, operation_ParentId="",
                        name="invoke_agent helper", duration=3200 if ms else "00:00:03.2000000", success="True",
                        customDimensions=json.dumps({"gen_ai.operation.name": "invoke_agent", "gen_ai.conversation.id": "conv%d" % (i // 3),
                                                     "gen_ai.input.messages": json.dumps([{"role": "user", "content": q}]),
                                                     "gen_ai.output.messages": json.dumps([{"role": "assistant", "content": a}])}),
                        user_Id="u%d@example.com" % (i % 2)))
        out.append(dict(itemType="dependency", timestamp=iso(t0 + 1), id=op + "l", operation_Id=op, operation_ParentId=op + "r",
                        name="chat gpt-4.1", duration=900 if ms else "00:00:00.9000000", success="True",
                        customDimensions=json.dumps({"gen_ai.operation.name": "chat", "gen_ai.request.model": "gpt-4.1",
                                                     "gen_ai.usage.input_tokens": 700, "gen_ai.usage.output_tokens": 50})))
        out.append(dict(itemType="dependency", timestamp=iso(t0 + 2), id=op + "t", operation_Id=op, operation_ParentId=op + "r",
                        name="execute_tool " + tool, duration=1500 if ms else "00:00:01.5000000", success="False" if fail else "True",
                        customDimensions=json.dumps({"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": tool,
                                                     "gen_ai.tool.call.arguments": json.dumps({"q": q}),
                                                     "gen_ai.tool.call.result": json.dumps({"status": "error", "message": "upstream timeout"} if fail else {"ok": True})})))
        if fail:
            out.append(dict(itemType="exception", timestamp=iso(t0 + 3), id=op + "e", operation_Id=op, operation_ParentId=op + "t",
                            name="TimeoutError", duration="", success="", customDimensions="{}"))
    return out


def w(name, data, mode="wb"):
    os.makedirs(OUT, exist_ok=True)
    p = os.path.join(OUT, name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    if isinstance(data, str):
        data = data.encode("utf-8")
    with open(p, "wb") as f:
        f.write(data)


def csv_text(rs, delim=",", cols=None, quote=csv.QUOTE_MINIMAL):
    cols = cols or sorted({k for r in rs for k in r}, key=lambda k: list(rs[0]).index(k) if k in rs[0] else 99)
    b = io.StringIO()
    wr = csv.DictWriter(b, cols, delimiter=delim, quoting=quote, extrasaction="ignore", lineterminator="\n")
    wr.writeheader()
    for r in rs:
        wr.writerow(r)
    return b.getvalue()


def jl(rs):
    return "\n".join(json.dumps(r) for r in rs) + "\n"


def otlp_attrs(d):
    def v(x):
        if isinstance(x, bool): return {"boolValue": x}
        if isinstance(x, int): return {"intValue": str(x)}
        if isinstance(x, float): return {"doubleValue": x}
        return {"stringValue": str(x)}
    return [{"key": k, "value": v(x)} for k, x in d.items()]


def main():
    base = rows()
    # 01 healthy csv + 02 healthy json array
    w("01_healthy.csv", csv_text(base), "w")
    w("02_healthy_array.json", json.dumps(base), "w")
    # 03 ndjson with truncated last line
    w("03_ndjson_truncated.jsonl", jl(base)[:-40], "w")
    # 04 concatenated JSON objects no separators
    w("04_concatenated.json", "".join(json.dumps(r) for r in base), "w")
    # 05 Log Analytics casing/renamed columns
    la = []
    for r in base:
        la.append({"TimeGenerated [UTC]": r["timestamp"], "ItemType": r["itemType"], "Id": r["id"], "OperationId": r["operation_Id"],
                   "ParentId": r["operation_ParentId"], "Name": r["name"], "DurationMs": r["duration"], "Success": r["success"],
                   "Properties": r["customDimensions"], "AppRoleName": "helper"})
    w("05_loganalytics_renamed.csv", csv_text(la), "w")
    # 06 no timestamps / durations
    nt = [{k: v for k, v in r.items() if k not in ("timestamp", "duration")} for r in base]
    w("06_no_time.csv", csv_text(nt), "w")
    # 07 no tokens, no success flag, errors only in result text
    nc = []
    for r in base:
        r = dict(r); r.pop("success")
        cd = json.loads(r["customDimensions"])
        cd.pop("gen_ai.usage.input_tokens", None); cd.pop("gen_ai.usage.output_tokens", None)
        r["customDimensions"] = json.dumps(cd); nc.append(r)
    w("07_no_tokens_no_success.csv", csv_text(nc), "w")
    # 08 no tool calls at all
    w("08_no_tools.csv", csv_text([r for r in base if r["itemType"] == "request"]), "w")
    # 09 double-encoded customDimensions and bad-quote CSV
    de = [dict(r, customDimensions=json.dumps(r["customDimensions"])) for r in base]
    w("09_double_encoded.json", json.dumps(de), "w")
    # 10 semicolon / tab / pipe delimiters
    w("10_semicolon.csv", csv_text(base, ";"), "w")
    w("11_tab.tsv", csv_text(base, "\t"), "w")
    w("12_pipe.csv", csv_text(base, "|"), "w")
    # 13 BOM + UTF-16
    w("13_bom_utf8.csv", b"\xef\xbb\xbf" + csv_text(base).encode())
    w("14_utf16.csv", csv_text(base).encode("utf-16"))
    # 15 gzip + 16 zip
    w("15_gz.jsonl.gz", gzip.compress(jl(base).encode()))
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("export/part1.csv", csv_text(base)); z.writestr("export/readme.txt", "hi")
    w("16_zip.zip", b.getvalue())
    # 17 OTLP attribute lists (Foundry style resourceSpans)
    spans = []
    for r in base:
        cd = json.loads(r["customDimensions"])
        t0 = 1741000000_000_000_000
        spans.append({"traceId": r["operation_Id"].ljust(32, "0"), "spanId": r["id"].ljust(16, "0"),
                      "parentSpanId": r["operation_ParentId"].ljust(16, "0") if r["operation_ParentId"] else "",
                      "name": r["name"], "startTimeUnixNano": str(t0 + len(spans) * 10**9), "endTimeUnixNano": str(t0 + len(spans) * 10**9 + 10**9),
                      "attributes": otlp_attrs(cd), "status": {"code": 2 if r["success"] == "False" else 1}})
    w("17_otlp_foundry.json", json.dumps({"resourceSpans": [{"scopeSpans": [{"spans": spans}]}]}), "w")
    # 18 epoch variants + timespan durations
    ep = []
    for i, r in enumerate(base):
        e = 1741000000 + i
        ts = [e, e * 1000, e * 10**6, e * 10**9, "%d" % e, r["timestamp"].replace("Z", "")][i % 6]
        ep.append(dict(r, timestamp=ts, duration="00:00:03.2000000" if i % 2 else r["duration"]))
    w("18_epoch_mixed.json", json.dumps(ep), "w")
    # 19 nulls, NaN, duplicates, orphans, out-of-order
    nn = [dict(r) for r in base]
    for r in nn[::4]: r["duration"] = "NaN"; r["customDimensions"] = None
    nn += nn[:5]  # dup
    nn.append(dict(base[1], id="orphan1", operation_ParentId="missing_parent", operation_Id="op_orphan"))
    nn.reverse()
    w("19_dirty_nulls_dups_orphans.json", json.dumps(nn).replace('"NaN"', 'NaN'), "w")
    # 20 huge fields
    hu = [dict(r) for r in base]
    cd = json.loads(hu[0]["customDimensions"]); cd["gen_ai.input.messages"] = json.dumps([{"role": "user", "content": "x" * 3_000_000}])
    hu[0]["customDimensions"] = json.dumps(cd)
    w("20_huge_field.json", json.dumps(hu), "w")
    # 21 mixed folder
    w("21_mixed/good.csv", csv_text(base), "w")
    w("21_mixed/junk.bin", bytes(range(256)) * 20)
    w("21_mixed/empty.json", "", "w")
    w("21_mixed/notes.txt", "meeting notes, nothing here\n", "w")
    w("21_mixed/pic.png", b"\x89PNG\r\n\x1a\n" + b"\0" * 50)
    # 22 empty / garbage only
    w("22_empty.csv", "")
    w("23_garbage.json", "{{{{ not json", "w")
    # 24 no run ids / operation ids, no conversation
    nr = [{k: v for k, v in r.items() if k not in ("operation_Id", "id", "operation_ParentId")} for r in base]
    w("24_no_ids.csv", csv_text(nr), "w")
    # 25 unknown schema (renamed everything)
    un = [{"when": r["timestamp"], "kind": r["itemType"], "what": r["name"], "blob": r["customDimensions"], "grp": r["operation_Id"]} for r in base]
    w("25_unknown_schema.csv", csv_text(un), "w")
    # 26 only trace items with message text (AppTraces)
    tr = [{"TimeGenerated": r["timestamp"], "Message": "user asked: " + json.loads(r["customDimensions"]).get("gen_ai.input.messages", "[]"),
           "OperationId": r["operation_Id"], "SeverityLevel": 1, "Type": "AppTraces"} for r in base if r["itemType"] == "request"]
    tr.append({"TimeGenerated": base[0]["timestamp"], "Message": "Traceback (most recent call last): ValueError boom", "OperationId": "op000", "SeverityLevel": 3, "Type": "AppTraces"})
    w("26_apptraces_only.csv", csv_text(tr), "w")
    # 27 wrong types: success as 1/0/yes, duration strings with units, tokens as strings with commas
    wt = []
    for i, r in enumerate(base):
        r = dict(r); r["success"] = ["1", "0", "yes", "no", "TRUE", "false"][i % 6]
        r["duration"] = ["3.2s", "3200ms", "n/a", "", "-5", "1e3"][i % 6]
        wt.append(r)
    w("27_wrong_types.csv", csv_text(wt), "w")
    # 28 scale: 3000 runs
    big = rows(3000, fail_every=7)
    w("28_scale.jsonl", jl(big), "w")
    # 29 secrets in text
    sec = [dict(r) for r in base]
    cd = json.loads(sec[0]["customDimensions"]); cd["gen_ai.input.messages"] = json.dumps([{"role": "user", "content": "my key is sk-FAKE1234567890abcdef and email bob@example.com"}])
    sec[0]["customDimensions"] = json.dumps(cd)
    w("29_secrets.csv", csv_text(sec), "w")
    # 30 header-only csv and ragged rows with bad quotes
    w("30_bad_quotes.csv", csv_text(base)[:600].replace('"{', '"{"', 1) + '\nrequest,"unterminated,quote\n', "w")

    # 31+ round-2 deformations
    w("31_crlf_cr_only.csv", csv_text(base).replace("\n", "\r"), "w")
    w("32_utf16be_json.json", json.dumps(base).encode("utf-16-be"))
    w("33_nan_inf_json.json", json.dumps([dict(r, latency="NaN") for r in base]).replace('"latency": "NaN"', '"latency": NaN, "z": -Infinity'), "w")
    w("34_wrapped_tables.json", json.dumps({"tables": [{"name": "PrimaryResult", "columns": [{"name": k, "type": "string"} for k in base[0]], "rows": [list(r.values()) for r in base]}]}), "w")
    w("35_preamble_csv.csv", "# exported by tool\n\n" + csv_text(base), "w")
    w("36_nested_zip_gz.zip", b"", "w") if False else None
    w("37_all_null_values.json", json.dumps([{k: None for k in r} for r in base]), "w")
    w("38_dup_header_cols.csv", csv_text(base).replace("\n", ",extra\n", 1), "w")


if __name__ == "__main__":
    main()
    print("wrote", OUT)
