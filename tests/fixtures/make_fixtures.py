"""Regenerates the synthetic ingest fixtures in this directory (stdlib only, deterministic).
Run: python3 tests/fixtures/make_fixtures.py     (the generated files are committed; tests do not need this)"""
import csv
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))


def ts(minute, sec=0.0):
    s = int(sec)
    return "2025-03-01 10:%02d:%02d.%06d+00" % (minute, s, int((sec - s) * 1e6))


def otel_rows():
    rows = []

    def span(trace, sid, pid, name, t0, dur, attrs, **kw):
        r = {"trace_id": trace, "span_id": sid, "parent_span_id": pid, "span_name": name,
             "start_timestamp": ts(*t0), "end_timestamp": ts(t0[0], t0[1] + dur), "duration": dur,
             "level": 9, "is_exception": False, "attributes": json.dumps(attrs)}
        r.update(kw)
        rows.append(r)
        return r

    def user(t):
        return [{"role": "user", "parts": [{"type": "text", "content": t}]}]

    def answer(t):
        return [{"role": "assistant", "parts": [{"type": "text", "content": t}], "finish_reason": "stop"}]

    base = {"gen_ai.conversation.id": "conv-A"}
    # run 1: success with one tool, conversation A turn 1
    a1 = dict(base, **{"gen_ai.agent.call.id": "call-1", "gen_ai.agent.name": "support_bot"})
    span("t1", "a1", None, "invoke_agent support_bot", (0, 0), 4.0, dict(
        a1, **{"gen_ai.operation.name": "invoke_agent",
               "gen_ai.input.messages": user("Where is my order 1234? Contact me at jane@example.com"),
               "gen_ai.output.messages": answer("Your order 1234 shipped yesterday."),
               "deployment.environment.name": "prod"}))
    span("t1", "l1", "a1", "chat gpt-4o", (0, 0.2), 1.0, dict(
        a1, **{"gen_ai.operation.name": "chat", "gen_ai.request.model": "gpt-4o-2024-08-06",
               "gen_ai.usage.input_tokens": 1200, "gen_ai.usage.output_tokens": 80}))
    tool1 = span("t1", "x1", "a1", "execute_tool lookup_order", (0, 1.3), 0.5, dict(
        a1, **{"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "lookup_order",
               "gen_ai.tool.call.arguments": {"order_id": "1234"},
               "gen_ai.tool.call.result": {"status": "shipped"}}))
    rows.append(dict(tool1))  # duplicate row (paginated export)
    span("t1", "l1b", "a1", "chat gpt-4o", (0, 2.0), 1.5, dict(
        a1, **{"gen_ai.operation.name": "chat", "gen_ai.request.model": "gpt-4o-2024-08-06",
               "gen_ai.usage.input_tokens": 1500, "gen_ai.usage.output_tokens": 40}))
    span("t1", "h1", "a1", "GET /health", (0, 0.1), 0.01, {"http.method": "GET"})  # noise

    # run 2: same conversation, tool fails with an exception, agent still answers
    a2 = dict(base, **{"gen_ai.agent.call.id": "call-2", "gen_ai.agent.name": "support_bot"})
    span("t2", "a2", None, "invoke_agent support_bot", (5, 0), 6.0, dict(
        a2, **{"gen_ai.operation.name": "invoke_agent",
               "gen_ai.input.messages": [{"role": "user", "content": "Where is my order 1234?"},
                                         {"role": "assistant", "content": "Shipped yesterday."},
                                         {"role": "user", "content": "Then please refund order 1234"}],
               "gen_ai.output.messages": answer("I could not process the refund, a human will follow up.")}))
    span("t2", "l2", "a2", "chat gpt-4o", (5, 0.2), 1.0, dict(
        a2, **{"gen_ai.operation.name": "chat", "gen_ai.request.model": "gpt-4o",
               "gen_ai.usage.input_tokens": 2000, "gen_ai.usage.output_tokens": 60}))
    span("t2", "x2", "a2", "execute_tool refund_order", (5, 1.5), 2.0, dict(
        a2, **{"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "refund_order",
               "gen_ai.tool.call.arguments": {"order_id": "1234"}}),
        level=17, is_exception=True, exception_type="TimeoutError", exception_message="refund api timed out")

    # run 3: no answer; one tool call cancelled (not a failure)
    a3 = dict(base, **{"gen_ai.agent.call.id": "call-3", "gen_ai.agent.name": "support_bot"})
    span("t3", "a3", None, "invoke_agent support_bot", (10, 0), 30.0, dict(
        a3, **{"gen_ai.operation.name": "invoke_agent",
               "gen_ai.input.messages": user("Cancel everything and email me the invoice")}))
    span("t3", "l3", "a3", "chat gpt-4o", (10, 0.2), 1.0, dict(
        a3, **{"gen_ai.operation.name": "chat", "gen_ai.request.model": "gpt-4o",
               "gen_ai.usage.input_tokens": 900, "gen_ai.usage.output_tokens": 30}))
    span("t3", "x3", "a3", "execute_tool send_invoice", (10, 2.0), 28.0, dict(
        a3, **{"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "send_invoice",
               "gen_ai.tool.call.arguments": {"to": "x"}}),
        level=17, is_exception=True, exception_type="CancelledError", exception_message="cancelled")

    # run 4: other conversation, no tools
    a4 = {"gen_ai.conversation.id": "conv-B", "gen_ai.agent.call.id": "call-4"}
    span("t4", "a4", None, "invoke_agent support_bot", (15, 0), 2.0, dict(
        a4, **{"gen_ai.operation.name": "invoke_agent", "gen_ai.input.messages": user("hi"),
               "gen_ai.output.messages": answer("Hello! How can I help?")}))
    span("t4", "l4", "a4", "chat gpt-4o-mini", (15, 0.1), 1.5, dict(
        a4, **{"gen_ai.operation.name": "chat", "gen_ai.request.model": "gpt-4o-mini",
               "gen_ai.usage.input_tokens": 100, "gen_ai.usage.output_tokens": 10}))

    # run 5: partial export, root span missing (parent not present)
    a5 = {"gen_ai.conversation.id": "conv-B", "gen_ai.agent.call.id": "call-5"}
    span("t5", "l5", "missing-root", "chat gpt-4o-mini", (20, 0), 1.0, dict(
        a5, **{"gen_ai.operation.name": "chat", "gen_ai.request.model": "gpt-4o-mini",
               "gen_ai.usage.input_tokens": 300, "gen_ai.usage.output_tokens": 20,
               "gen_ai.input.messages": user("what are your opening hours"),
               "gen_ai.output.messages": answer("We open at 9am.")}))
    return rows


def appinsights_rows():
    cols = ["operation_Id", "id", "operation_ParentId", "name", "timestamp", "duration", "success",
            "gen_ai.operation.name", "gen_ai.agent.call.id", "gen_ai.conversation.id", "gen_ai.tool.name",
            "gen_ai.tool.call.arguments", "gen_ai.tool.call.result", "gen_ai.request.model",
            "gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens", "gen_ai.input.messages",
            "gen_ai.output.messages", "error.type"]
    rows = []

    def add(**kw):
        rows.append({c: kw.get(c.replace(".", "_"), kw.get(c, "")) for c in cols})
    for i, (ok, q, ans) in enumerate([(True, "reset my password", "Done, check your inbox."),
                                      (False, "export my data", "")]):
        op, root = "az%d" % i, "az%d-root" % i
        add(operation_Id=op, id=root, name="invoke_agent helper", timestamp="2025-03-02T09:0%d:00.000Z" % i,
            duration="3200", success="True", **{"gen_ai.operation.name": "invoke_agent",
                                                "gen_ai.agent.call.id": "ai-call-%d" % i,
                                                "gen_ai.conversation.id": "ai-conv",
                                                "gen_ai.input.messages": json.dumps([{"role": "user", "content": q}]),
                                                "gen_ai.output.messages": json.dumps(
                                                    [{"role": "assistant", "content": ans}]) if ans else ""})
        add(operation_Id=op, id=root + "-llm", operation_ParentId=root, name="chat gpt-4.1",
            timestamp="2025-03-02T09:0%d:00.200Z" % i, duration="900", success="True",
            **{"gen_ai.operation.name": "chat", "gen_ai.request.model": "gpt-4.1", "gen_ai.usage.input_tokens": "700",
               "gen_ai.usage.output_tokens": "50", "gen_ai.agent.call.id": "ai-call-%d" % i})
        add(operation_Id=op, id=root + "-tool", operation_ParentId=root, name="execute_tool account_api",
            timestamp="2025-03-02T09:0%d:01.200Z" % i, duration="1500", success="True" if ok else "False",
            **{"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "account_api",
               "gen_ai.tool.call.arguments": '{"action": "%s"}' % q.split()[0],
               "gen_ai.tool.call.result": '{"ok": true}' if ok else "", "error.type": "" if ok else "HTTP500",
               "gen_ai.agent.call.id": "ai-call-%d" % i})
    return cols, rows


def langsmith_runs():
    def run(rid, trace, parent, rtype, name, t0, t1, inputs, outputs=None, error=None, meta=None, **kw):
        r = {"id": rid, "trace_id": trace, "parent_run_id": parent, "run_type": rtype, "name": name,
             "start_time": "2025-03-03T08:%02d:%02d.000000" % t0, "end_time": "2025-03-03T08:%02d:%02d.000000" % t1,
             "inputs": inputs, "outputs": outputs, "error": error,
             "extra": {"metadata": meta or {}, "invocation_params": {}}}
        r.update(kw)
        return r
    m = {"thread_id": "thread-1"}
    out = [
        run("ls-r1", "ls-r1", None, "chain", "AgentExecutor", (0, 0), (0, 6), {"input": "Book a table for 2 tonight"},
            {"output": "Booked for 8pm."}, meta=m),
        run("ls-r1-llm", "ls-r1", "ls-r1", "llm", "ChatOpenAI", (0, 1), (0, 2),
            {"messages": [[{"lc": 1, "type": "constructor", "id": ["langchain", "schema", "messages", "HumanMessage"],
                            "kwargs": {"content": "Book a table for 2 tonight", "type": "human"}}]]},
            {"generations": [[{"text": "", "message": {"kwargs": {"content": "", "tool_calls": [{"name": "book"}]}}}]],
             "llm_output": {"token_usage": {"prompt_tokens": 400, "completion_tokens": 25}, "model_name": "gpt-4o"}},
            meta=m),
        run("ls-r1-tool", "ls-r1", "ls-r1", "tool", "book_table", (0, 3), (0, 4), {"input": "{'party': 2}"},
            {"output": "confirmed"}, meta=m),
        run("ls-r2", "ls-r2", None, "chain", "AgentExecutor", (2, 0), (2, 9), {"input": "Actually make it for 4"},
            {"output": ""}, meta=m),
        run("ls-r2-llm", "ls-r2", "ls-r2", "llm", "ChatOpenAI", (2, 1), (2, 2), {"messages": [[]]}, {"generations": [[]]},
            meta=m, prompt_tokens=500, completion_tokens=30, extra={"metadata": m, "invocation_params": {"model": "gpt-4o"}}),
        run("ls-r2-tool", "ls-r2", "ls-r2", "tool", "book_table", (2, 3), (2, 8), {"input": "{'party': 4}"}, None,
            error="HTTPError: 503 service unavailable", meta=m),
        run("ls-r3", "ls-r3", None, "chain", "AgentExecutor", (5, 0), (5, 2), {"input": "thanks"}, {"output": "You're welcome!"},
            meta={"thread_id": "thread-2"}),
    ]
    return out


def flat_table():
    cols = ["session", "when", "speaker", "text", "tool", "tool_in", "tool_out", "tool_error", "model", "tok_in", "tok_out"]
    rows = [
        ["s1", "2025-03-04 12:00:00", "user", "How do I change my plan?", "", "", "", "", "", "", ""],
        ["s1", "2025-03-04 12:00:02", "tool", "", "get_plans", "{}", "basic, pro", "", "", "", ""],
        ["s1", "2025-03-04 12:00:03", "assistant", "You can switch to pro in settings.", "", "", "", "", "m1", "300", "20"],
        ["s1", "2025-03-04 12:05:00", "user", "Switch me to pro", "", "", "", "", "", "", ""],
        ["s1", "2025-03-04 12:05:02", "tool", "", "change_plan", '{"plan": "pro"}', "", "permission denied", "", "", ""],
        ["s1", "2025-03-04 12:05:03", "assistant", "Sorry, I could not change it.", "", "", "", "", "m1", "400", "15"],
        ["s2", "2025-03-04 13:00:00", "user", "hello", "", "", "", "", "", "", ""],
    ]
    mapping = {"conversation_id": "session", "timestamp": "when", "role": "speaker", "content": "text",
               "tool_name": "tool", "tool_args": "tool_in", "tool_result": "tool_out", "error": "tool_error",
               "model": "model", "input_tokens": "tok_in", "output_tokens": "tok_out"}
    return cols, rows, mapping


def main():
    with open(os.path.join(HERE, "otel_logfire.jsonl"), "w") as f:
        for r in otel_rows():
            f.write(json.dumps(r) + "\n")
    cols, rows = appinsights_rows()
    with open(os.path.join(HERE, "otel_appinsights.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    with open(os.path.join(HERE, "langsmith_runs.json"), "w") as f:
        json.dump(langsmith_runs(), f, indent=1)
    cols, rows, mapping = flat_table()
    with open(os.path.join(HERE, "flat_table.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        w.writerows(rows)
    with open(os.path.join(HERE, "flat_map.json"), "w") as f:
        json.dump(mapping, f, indent=1)
    with open(os.path.join(HERE, "prices.json"), "w") as f:
        json.dump({"gpt-4o": {"input_per_mtok": 2.5, "output_per_mtok": 10.0},
                   "gpt-4o-mini": {"input_per_mtok": 0.15, "output_per_mtok": 0.6},
                   "m1": {"input_per_mtok": 1.0, "output_per_mtok": 2.0}}, f, indent=1)


if __name__ == "__main__":
    main()
