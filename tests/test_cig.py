import csv
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "plugins", "procept-lite", "scripts"))
from plite import build_cig, build_viewer, cig_query  # noqa: E402


_GATE = {}


def setUpModule():  # these tests exercise the stages themselves, not the sign-off gates (test_gates.py covers those)
    _GATE["old"] = os.environ.get("PLITE_NO_GATE")
    os.environ["PLITE_NO_GATE"] = "1"


def tearDownModule():
    if _GATE.get("old") is None:
        os.environ.pop("PLITE_NO_GATE", None)
    else:
        os.environ["PLITE_NO_GATE"] = _GATE["old"]


def tc(name, dur=1.0, ok=True, err=None):
    return {"ts": "2026-01-01T00:00:00Z", "name": name, "duration_s": dur, "args": "{}",
            "result": "r", "success": ok, "error": err}


def run(rid, tools, answer="done", lat=10.0, inp=100, out=50, conv=None):
    return {"run_id": rid, "conversation_id": conv or rid, "agent": "a", "environment": "",
            "first_ts": "2026-01-01T00:%02d:00Z" % (int(rid[1:]) % 60), "last_ts": "",
            "latency_s": lat, "user_message": "hello </script><b>", "final_answer": answer,
            "ends_without_answer": answer is None, "tool_calls": tools, "n_llm_calls": 1,
            "models": [], "input_tokens": inp, "output_tokens": out, "cost_usd": None,
            "n_tool_failures": 0, "errors": [], "extra": {}}


def fixture():
    runs = [
        run("r1", [tc("search", 1), tc("fetch", 2)], lat=5, conv="c1"),
        run("r2", [tc("search", 3, False, "timeout after 30 s for id 123")], lat=20, conv="c1"),
        run("r3", [tc("search", 1), tc("search", 1), tc("fetch", 4, False, "timeout after 99 s for id 456")],
            answer=None, lat=40, inp=1000, conv="c2"),
        run("r4", [], lat=2, conv="c3"),
    ]
    scores = {"r1": 1, "r2": 0, "r3": -1}
    return runs, scores


def write_workspace(d, runs, scores=None):
    os.makedirs(os.path.join(d, "normalized"), exist_ok=True)
    with open(os.path.join(d, "normalized", "runs.jsonl"), "w") as f:
        for r in runs:
            f.write(json.dumps(r) + "\n")
    if scores is not None:
        with open(os.path.join(d, "scores.csv"), "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["run_id", "outcome_score", "confidence", "rationale", "key_quote", "persona",
                        "issue", "reaction_run_id"])
            for k, v in scores.items():
                w.writerow([k, v, "high", "why", "", "analyst", "" if v == 1 else "timeouts", ""])


def rt(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def rj(path):
    return json.loads(rt(path))


def rd(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


class CigTests(unittest.TestCase):
    def build(self, d, scored=True):
        runs, scores = fixture()
        write_workspace(d, runs, scores if scored else None)
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(build_cig.main(["--out", d]), 0)
        return os.path.join(d, "cig")

    def test_files_and_graph_counts(self):
        with tempfile.TemporaryDirectory() as d:
            cig = self.build(d)
            for n in ["runs.csv", "nodes.csv", "edges.csv", "convs.csv", "errors.csv",
                      "flow_graph.json", "run_dossiers.jsonl", "summary.json"]:
                self.assertTrue(os.path.exists(os.path.join(cig, n)), n)
            nodes = {r["node_id"]: r for r in rd(os.path.join(cig, "nodes.csv"))}
            self.assertEqual(nodes["tool:search"]["calls"], "4")
            self.assertEqual(nodes["tool:search"]["runs"], "3")
            self.assertEqual(nodes["tool:search"]["fail_calls"], "1")
            self.assertAlmostEqual(float(nodes["tool:search"]["fail_rate"]), 0.25)
            self.assertAlmostEqual(float(nodes["tool:fetch"]["fail_rate"]), 0.5)
            self.assertEqual(nodes["sentinel:START"]["runs"], "4")
            self.assertEqual(nodes["sentinel:NO_ANSWER"]["runs"], "1")
            edges = {(r["source"], r["target"]): r for r in rd(os.path.join(cig, "edges.csv"))}
            self.assertEqual(edges[("tool:search", "tool:search")]["count"], "1")
            self.assertEqual(edges[("sentinel:START", "sentinel:ANSWER")]["count"], "1")  # 0-tool run
            self.assertEqual(edges[("tool:search", "tool:fetch")]["count"], "2")
            self.assertAlmostEqual(float(edges[("tool:search", "tool:fetch")]["fail_rate_target"]), 0.5)
            self.assertAlmostEqual(float(edges[("tool:search", "tool:fetch")]["mean_outcome"]), 0.0)

    def test_dimension_math(self):
        with tempfile.TemporaryDirectory() as d:
            cig = self.build(d)
            runs = {r["run_id"]: r for r in rd(os.path.join(cig, "runs.csv"))}
            self.assertEqual(runs["r1"]["label"], "good")
            self.assertEqual(runs["r2"]["label"], "ambiguous")
            self.assertEqual(runs["r3"]["label"], "bad")
            self.assertEqual(runs["r2"]["tool_fail"], "1")
            self.assertEqual(runs["r2"]["unrecovered"], "1")
            self.assertEqual(runs["r3"]["no_answer"], "1")
            self.assertEqual(runs["r1"]["tokens"], "150")
            self.assertEqual(runs["r1"]["workflow"], "search > fetch")
            self.assertEqual(runs["r3"]["workflow"], "search > fetch")
            self.assertEqual(runs["r4"]["n_tool_calls"], "0")
            s = rj(os.path.join(cig, "summary.json"))
            self.assertEqual(s["counts"], {"good": 1, "ambiguous": 1, "bad": 1, "ungraded": 1})
            self.assertEqual(s["n_conversations"], 3)
            self.assertEqual(s["errors"]["tool_fail_runs"], 2)
            self.assertEqual(s["errors"]["failed_calls"], 2)
            self.assertAlmostEqual(s["mean_outcome"], 0.0)
            self.assertEqual(s["latency"]["median_s"], 12.5)
            self.assertEqual(s["cost"]["total_tokens"], 150 * 3 + 1050)
            errs = rd(os.path.join(cig, "errors.csv"))
            self.assertEqual(len(errs), 1)  # both timeouts collapse to one class
            self.assertEqual(errs[0]["count"], "2")
            self.assertEqual(errs[0]["error_class"], "timeout after s for id")
            convs = {r["conversation_id"]: r for r in rd(os.path.join(cig, "convs.csv"))}
            self.assertEqual(convs["c1"]["n_runs"], "2")
            self.assertEqual(convs["c1"]["worst_outcome"], "0")

    def test_error_class(self):
        a = build_cig.error_class("HTTP 500 for user 8f14e45f-ceea-467a-9a36-dedd4bea2543 'bob'")
        b = build_cig.error_class("HTTP 503 for user 11111111-2222-3333-4444-555555555555 'alice'")
        self.assertEqual(a, b)

    def test_deterministic(self):
        with tempfile.TemporaryDirectory() as d1, tempfile.TemporaryDirectory() as d2:
            c1, c2 = self.build(d1), self.build(d2)
            for n in sorted(os.listdir(c1)):
                self.assertEqual(rt(os.path.join(c1, n)), rt(os.path.join(c2, n)), n)

    def test_no_scores(self):
        with tempfile.TemporaryDirectory() as d:
            cig = self.build(d, scored=False)
            s = rj(os.path.join(cig, "summary.json"))
            self.assertFalse(s["has_scores"])
            self.assertEqual(s["counts"]["ungraded"], 4)
            nodes = rd(os.path.join(cig, "nodes.csv"))
            self.assertTrue(all(n["mean_outcome"] == "" for n in nodes))
            html_path = os.path.join(d, "cig.html")
            with redirect_stdout(io.StringIO()):
                self.assertEqual(build_viewer.main(["--out", d]), 0)
            self.assertTrue(os.path.exists(html_path))

    def test_single_run_and_zero_tools(self):
        with tempfile.TemporaryDirectory() as d:
            write_workspace(d, [run("r1", [], lat=1)])
            with redirect_stdout(io.StringIO()):
                self.assertEqual(build_cig.main(["--out", d]), 0)
            edges = rd(os.path.join(d, "cig", "edges.csv"))
            self.assertEqual([(e["source"], e["target"]) for e in edges],
                             [("sentinel:START", "sentinel:ANSWER")])
            s = rj(os.path.join(d, "cig", "summary.json"))
            self.assertEqual(s["n_tools"], 0)

    def test_viewer_self_contained_and_escaped(self):
        with tempfile.TemporaryDirectory() as d:
            self.build(d)
            old = os.environ.get("PLITE_NO_GATE")
            os.environ["PLITE_NO_GATE"] = "1"
            try:
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(build_viewer.main(["--out", d]), 0)
            finally:
                if old is None:
                    os.environ.pop("PLITE_NO_GATE", None)
                else:
                    os.environ["PLITE_NO_GATE"] = old
            html = rt(os.path.join(d, "cig.html"))
            self.assertEqual(html.count("</script>"), 1)  # tool names and user text are escaped
            bare = html.replace("http://www.w3.org/2000/svg", "").replace(build_viewer.DEFAULT_URL, "")
            self.assertNotIn("http://", bare)
            self.assertNotIn("https://", bare)  # the System 5 link is the only address in the page
            self.assertNotIn("/*__DATA__*/", html)
            self.assertNotIn("hello", html)  # no per-run text is embedded

    def test_tool_cap_groups_tail(self):
        runs = [run("r%d" % i, [tc("t%d" % i)]) for i in range(1, 80)]
        recs = build_cig.prepare(runs, {})
        grouped, tail = build_viewer.group_tail(recs)
        self.assertEqual(len(tail), 79 - (build_viewer.MAX_TOOL_NODES - 1))
        nodes, _ = build_cig.build_graph(grouped)
        tools = [n for n in nodes if n["kind"] == "tool"]
        self.assertEqual(len(tools), build_viewer.MAX_TOOL_NODES)
        self.assertIn("tool:other", [n["node_id"] for n in tools])

    def test_query_cli(self):
        with tempfile.TemporaryDirectory() as d:
            self.build(d)
            for argv in (["summary"], ["runs", "--label", "bad"], ["runs", "--tool", "fetch", "--min-latency", "10"],
                         ["run", "r3"], ["tools"], ["errors"]):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    self.assertEqual(cig_query.main(["--out", d] + argv), 0, argv)
                self.assertTrue(buf.getvalue().strip())
            buf = io.StringIO()
            with redirect_stdout(buf):
                cig_query.main(["--out", d, "runs", "--label", "bad"])
            self.assertIn("r3", buf.getvalue())
            self.assertNotIn("r1 ", buf.getvalue().split("\n", 1)[1])


if __name__ == "__main__":
    unittest.main()
