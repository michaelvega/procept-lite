"""Ingest tests: check_logs + normalize on synthetic fixtures in every supported format (stdlib unittest)."""
import gzip
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "plugins", "procept-lite", "scripts", "plite"))
import check_logs  # noqa: E402
import common  # noqa: E402
import normalize as N  # noqa: E402

FX = os.path.join(ROOT, "tests", "fixtures")


def fx(name):
    return os.path.join(FX, name)


def runs_of(src, mapping=None, prices=True, fmt="auto"):
    opts = N.Opts(fmt, common.load_json_file(fx(mapping)) if mapping else None,
                  common.load_json_file(fx("prices.json")) if prices else None)
    runs, spans, stats = N.run_all([fx(src) if not os.path.isabs(src) else src], opts)
    return {r["run_id"]: r for r in runs}, spans, stats


class OtelLogfire(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runs, cls.spans, cls.stats = runs_of("otel_logfire.jsonl")

    def test_runs_and_conversations(self):
        self.assertEqual(sorted(self.runs), ["call-1", "call-2", "call-3", "call-4", "call-5"])
        conv_a = [r for r in self.runs.values() if r["conversation_id"] == "conv-A"]
        self.assertEqual(len(conv_a), 3)  # multi-turn: one conversation holds several runs
        self.assertEqual(self.runs["call-4"]["conversation_id"], "conv-B")

    def test_noise_and_duplicates(self):
        self.assertEqual(self.stats["noise_dropped"], 1)
        self.assertEqual(self.stats["duplicates"], 1)
        self.assertEqual(len(self.runs["call-1"]["tool_calls"]), 1)

    def test_user_message_is_last_turn_not_history(self):
        self.assertEqual(self.runs["call-2"]["user_message"], "Then please refund order 1234")

    def test_successful_run(self):
        r = self.runs["call-1"]
        self.assertTrue(r["final_answer"].startswith("Your order 1234 shipped"))
        self.assertFalse(r["ends_without_answer"])
        self.assertEqual((r["input_tokens"], r["output_tokens"]), (2700, 120))
        self.assertEqual(r["models"], ["gpt-4o-2024-08-06"])
        self.assertAlmostEqual(r["cost_usd"], 2700 / 1e6 * 2.5 + 120 / 1e6 * 10, places=6)
        self.assertAlmostEqual(r["latency_s"], 4.0)
        self.assertEqual(r["agent"], "support_bot")
        self.assertEqual(r["environment"], "prod")
        c = r["tool_calls"][0]
        self.assertEqual(c["name"], "lookup_order")
        self.assertIn("1234", c["args"])
        self.assertIn("shipped", c["result"])
        self.assertTrue(c["success"])

    def test_failed_tool(self):
        r = self.runs["call-2"]
        self.assertEqual(r["n_tool_failures"], 1)
        c = r["tool_calls"][0]
        self.assertFalse(c["success"])
        self.assertIn("TimeoutError", c["error"])
        self.assertTrue(r["errors"])
        self.assertEqual(c["result"], "")  # empty result alone is not what made it a failure

    def test_cancelled_is_not_failure(self):
        r = self.runs["call-3"]
        self.assertEqual(r["n_tool_failures"], 0)
        self.assertEqual(r["extra"]["cancelled"], 1)
        self.assertEqual(r["tool_calls"][0]["error"], "cancelled")

    def test_run_without_answer(self):
        r = self.runs["call-3"]
        self.assertTrue(r["ends_without_answer"])
        self.assertIsNone(r["final_answer"])

    def test_missing_root_partial_export(self):
        r = self.runs["call-5"]
        self.assertTrue(r["extra"]["no_root"])
        self.assertEqual(r["user_message"], "what are your opening hours")
        self.assertEqual(r["final_answer"], "We open at 9am.")

    def test_canonical_keys(self):
        keys = {"run_id", "conversation_id", "agent", "environment", "first_ts", "last_ts", "latency_s", "user_message",
                "final_answer", "ends_without_answer", "tool_calls", "n_llm_calls", "models", "input_tokens",
                "output_tokens", "cost_usd", "n_tool_failures", "errors", "extra"}
        for r in self.runs.values():
            self.assertEqual(set(r), keys)
            self.assertTrue(r["first_ts"].endswith("Z"))

    def test_no_prices_means_null_cost(self):
        runs, _, _ = runs_of("otel_logfire.jsonl", prices=False)
        self.assertIsNone(runs["call-1"]["cost_usd"])


class OtelAppInsightsCsv(unittest.TestCase):
    def test_flattened_columns_and_ms_duration(self):
        runs, _, stats = runs_of("otel_appinsights.csv")
        self.assertEqual(stats["formats"]["otel"], 6)
        r0, r1 = runs["ai-call-0"], runs["ai-call-1"]
        self.assertAlmostEqual(r0["latency_s"], 3.2)
        self.assertEqual(r0["tool_calls"][0]["name"], "account_api")
        self.assertTrue(r0["tool_calls"][0]["success"])
        self.assertEqual(r1["n_tool_failures"], 1)  # success=False + error.type
        self.assertTrue(r1["ends_without_answer"])
        self.assertEqual(r0["conversation_id"], "ai-conv")
        self.assertEqual(r0["extra"]["unpriced_models"], ["gpt-4.1"])


class LangSmith(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runs, cls.spans, cls.stats = runs_of("langsmith_runs.json")

    def test_runs_per_trace_and_threads(self):
        self.assertEqual(sorted(self.runs), ["ls-r1", "ls-r2", "ls-r3"])
        self.assertEqual(self.runs["ls-r1"]["conversation_id"], self.runs["ls-r2"]["conversation_id"])
        self.assertNotEqual(self.runs["ls-r1"]["conversation_id"], self.runs["ls-r3"]["conversation_id"])

    def test_content_tools_tokens(self):
        r = self.runs["ls-r1"]
        self.assertEqual(r["user_message"], "Book a table for 2 tonight")
        self.assertEqual(r["final_answer"], "Booked for 8pm.")
        self.assertEqual(r["tool_calls"][0]["name"], "book_table")
        self.assertEqual((r["input_tokens"], r["output_tokens"]), (400, 25))
        self.assertEqual(r["models"], ["gpt-4o"])
        self.assertAlmostEqual(r["latency_s"], 6.0)

    def test_failed_tool_and_empty_answer(self):
        r = self.runs["ls-r2"]
        self.assertEqual(r["n_tool_failures"], 1)
        self.assertIn("503", r["tool_calls"][0]["error"])
        self.assertTrue(r["ends_without_answer"])
        self.assertEqual(r["models"], ["gpt-4o"])  # from extra.invocation_params


class FlatTable(unittest.TestCase):
    def test_mapping_and_turn_heuristic(self):
        runs, _, _ = runs_of("flat_table.csv", mapping="flat_map.json")
        self.assertEqual(sorted(runs), ["s1#t1", "s1#t2", "s2#t1"])
        self.assertEqual({r["conversation_id"] for r in runs.values()}, {"s1", "s2"})
        t2 = runs["s1#t2"]
        self.assertEqual(t2["user_message"], "Switch me to pro")
        self.assertEqual(t2["n_tool_failures"], 1)
        self.assertEqual(t2["tool_calls"][0]["error"], "permission denied")
        self.assertTrue(runs["s2#t1"]["ends_without_answer"])
        self.assertEqual(runs["s1#t1"]["tool_calls"][0]["result"], "basic, pro")

    def test_flat_without_map_fails_clearly(self):
        opts = N.Opts("flat")
        with self.assertRaises(SystemExit):
            N.load_spans([fx("flat_table.csv")], opts)


class Readers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_json_array_gz_and_directory_dedupe(self):
        rows = [json.loads(l) for l in open(fx("otel_logfire.jsonl"))]
        with open(os.path.join(self.tmp, "a.json"), "w") as f:
            json.dump(rows[:8], f)
        with gzip.open(os.path.join(self.tmp, "b.jsonl.gz"), "wt") as f:
            for r in rows[5:]:  # overlaps with a.json: pages repeat spans
                f.write(json.dumps(r) + "\n")
        runs, _, stats = runs_of(self.tmp)
        ref, _, _ = runs_of("otel_logfire.jsonl")
        self.assertEqual(sorted(runs), sorted(ref))
        self.assertEqual(runs["call-1"]["tool_calls"], ref["call-1"]["tool_calls"])
        self.assertGreater(stats["duplicates"], 0)

    def test_otlp_resource_spans(self):
        doc = {"resourceSpans": [{"resource": {"attributes": [{"key": "deployment.environment",
                                                               "value": {"stringValue": "stg"}}]},
                                  "scopeSpans": [{"spans": [{
                                      "traceId": "T", "spanId": "S", "name": "invoke_agent x",
                                      "startTimeUnixNano": "1740823200000000000", "endTimeUnixNano": "1740823203000000000",
                                      "attributes": [
                                          {"key": "gen_ai.operation.name", "value": {"stringValue": "invoke_agent"}},
                                          {"key": "gen_ai.agent.call.id", "value": {"stringValue": "c9"}},
                                          {"key": "gen_ai.input.messages",
                                           "value": {"stringValue": "[{\"role\":\"user\",\"content\":\"yo\"}]"}}]}]}]}]}
        p = os.path.join(self.tmp, "otlp.json")
        json.dump(doc, open(p, "w"))
        runs, _, _ = runs_of(p, prices=False)
        r = runs["c9"]
        self.assertEqual((r["user_message"], r["environment"], r["latency_s"]), ("yo", "stg", 3.0))

    def test_missing_source_raises(self):
        with self.assertRaises(FileNotFoundError):
            list(common.iter_records([os.path.join(self.tmp, "nope.json")]))

    def test_time_and_text_helpers(self):
        self.assertEqual(common.to_epoch("2025-03-01 10:00:00.5+00"), common.to_epoch("2025-03-01T10:00:00.500Z"))
        self.assertEqual(common.to_epoch(1740823200000), 1740823200.0)
        self.assertEqual(common.to_epoch("2025-03-01T12:00:00+02:00"), common.to_epoch("2025-03-01T10:00:00Z"))
        self.assertEqual(common.last_text([{"role": "assistant", "content": "a"}, {"role": "user", "content": "b"}],
                                          "user", "user")[0], "b")
        self.assertIsNone(common.last_text([{"role": "assistant", "content": "x", "tool_calls": [{}]}],
                                           "assistant", "assistant")[0])
        self.assertEqual(common.redact("mail a.b@x.io now"), "mail [email] now")

    def test_noise_and_tool_regex(self):
        opts = N.Opts(noise_regex="^chat ", tool_regex="^GET ")
        spans, stats = N.load_spans([fx("otel_logfire.jsonl")], opts)
        self.assertFalse([s for s in spans if s.kind == "llm"])
        self.assertTrue([s for s in spans if s.kind == "tool" and s.name.startswith("GET")])


class CheckLogs(unittest.TestCase):
    def setUp(self):
        self.out = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.out, ignore_errors=True)

    def run_check(self, *args):
        rc = check_logs.main(list(args) + ["--out", self.out])
        self.assertEqual(rc, 0)
        return json.load(open(os.path.join(self.out, "inspect.json")))

    def test_report_shape_and_counts(self):
        rep = self.run_check("--src", fx("otel_logfire.jsonl"), "--prices", fx("prices.json"))
        self.assertIn(rep["verdict"], ("READY", "USABLE_WITH_GAPS", "NOT_READY"))
        self.assertEqual([r["id"] for r in rep["requirements"]], ["R%d" % i for i in range(1, 12)])
        for r in rep["requirements"]:
            self.assertIn(r["status"], ("GREEN", "AMBER", "RED"))
            self.assertTrue(r["fix"].startswith("references/data-gaps.md#"))
        c = rep["counts"]
        self.assertEqual((c["runs"], c["conversations"], c["tool_calls"]), (5, 2, 3))
        self.assertEqual((c["noise_spans_dropped"], c["duplicate_spans"]), (1, 1))
        st = {r["id"]: r["status"] for r in rep["requirements"]}
        self.assertEqual((st["R1"], st["R6"], st["R8"], st["R11"]), ("GREEN", "GREEN", "GREEN", "RED"))
        self.assertTrue(os.path.exists(os.path.join(self.out, "inspect.md")))
        self.assertTrue(rep["fix_list"])

    def test_examples_are_short_and_redacted(self):
        rep = self.run_check("--src", fx("otel_logfire.jsonl"))
        blob = json.dumps(rep) + open(os.path.join(self.out, "inspect.md")).read()
        self.assertNotIn("jane@example.com", blob)
        for v in rep["examples"].values():
            self.assertLessEqual(len(v), 80)

    def test_all_three_formats_readable(self):
        for src, extra in (("otel_appinsights.csv", []), ("langsmith_runs.json", []),
                           ("flat_table.csv", ["--map", fx("flat_map.json")])):
            rep = self.run_check("--src", fx(src), *extra)
            self.assertGreater(rep["counts"]["runs"], 0, src)

    def test_not_ready_when_no_user_messages(self):
        rows = [{"trace_id": "t%d" % i, "span_id": "s%d" % i, "span_name": "x", "start_timestamp": "2025-01-01T00:00:00Z",
                 "attributes": json.dumps({"gen_ai.operation.name": "chat", "gen_ai.request.model": "m"})}
                for i in range(40)]
        p = os.path.join(self.out, "bad.jsonl")
        with open(p, "w") as f:
            f.write("\n".join(json.dumps(r) for r in rows))
        rep = self.run_check("--src", p)
        self.assertEqual(rep["verdict"], "NOT_READY")
        st = {r["id"]: r["status"] for r in rep["requirements"]}
        self.assertEqual((st["R4"], st["R5"], st["R3"]), ("RED", "RED", "RED"))
        self.assertIn("message-content", rep["fix_list"][0] + rep["fix_list"][1] + rep["fix_list"][2])

    def test_unrecognised_table_points_to_mapping(self):
        p = os.path.join(self.out, "weird.csv")
        open(p, "w").write("a,b\n1,2\n")
        rep = self.run_check("--src", p)
        self.assertEqual(rep["verdict"], "NOT_READY")
        self.assertIn("--map", rep["fix_list"][0])


class Scale(unittest.TestCase):
    def test_fifty_thousand_rows_fast(self):
        import time
        tmp = tempfile.mkdtemp()
        try:
            p = os.path.join(tmp, "big.jsonl")
            with open(p, "w") as f:
                for i in range(10000):
                    base = {"trace_id": "t%d" % i, "start_timestamp": "2025-03-01T10:00:00Z", "duration": 1.0}
                    a = {"gen_ai.agent.call.id": "c%d" % i, "gen_ai.conversation.id": "v%d" % (i // 3)}
                    for j, (op, extra) in enumerate([
                            ("invoke_agent", {"gen_ai.input.messages": "q", "gen_ai.output.messages": "a"}),
                            ("chat", {"gen_ai.request.model": "m", "gen_ai.usage.input_tokens": 5}),
                            ("execute_tool", {"gen_ai.tool.name": "t"}), ("execute_tool", {"gen_ai.tool.name": "u"}),
                            ("chat", {})]):
                        r = dict(base, span_id="s%d_%d" % (i, j), parent_span_id=None if j == 0 else "s%d_0" % i,
                                 span_name=op, attributes=json.dumps(dict(a, **{"gen_ai.operation.name": op}, **extra)))
                        f.write(json.dumps(r) + "\n")
            t = time.time()
            runs, _, stats = N.run_all([p], N.Opts())
            self.assertEqual(len(runs), 10000)
            self.assertLess(time.time() - t, 30)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class MalformedRows(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_bad_rows_counted_unreadable_and_null_latency(self):
        good = {"trace_id": "t1", "span_id": "s1", "name": "agent run",
                "attributes": {"gen_ai.operation.name": "invoke_agent"}}
        p = os.path.join(self.tmp, "x.jsonl")
        with open(p, "w") as f:
            f.write(json.dumps(good) + "\n{not json\n[1, 2]\n" + json.dumps({"foo": 1}) + "\n")
        runs, spans, stats = runs_of(p)
        self.assertEqual(stats["unknown_records"] + stats["bad_lines"], 3)
        out = os.path.join(self.tmp, "out")
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(N.main(["--src", p, "--out", out]), 0)
        self.assertIn("unreadable 3", buf.getvalue())
        with open(os.path.join(out, "normalized", "ingest_stats.json")) as f:
            self.assertEqual(json.load(f)["unreadable"], 3)
        with open(os.path.join(out, "normalized", "runs.jsonl")) as f:
            self.assertIsNone(json.loads(f.readline())["latency_s"])


if __name__ == "__main__":
    unittest.main()
