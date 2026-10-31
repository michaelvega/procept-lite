import csv
import html.parser
import io
import json
import os
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "plugins", "procept-lite", "scripts"))
from plite import build_cig, build_viewer  # noqa: E402

S5 = "https://procept.tech/system5"
CTA = "Want to fix these problems? See errors, cost and latency for every node in System 5"
HOSTILE = '</script><img src=x onerror=alert(1)>"&amp;\'<!--'  # busiest tool: always visible, must be escaped
LONG = ["alpha_lookup_service", "beta_summarizer_tool", "gamma_retrieval_step", "delta_ranking_engine",
        "epsilon_formatter_x", "zeta_checker_module", "eta_planner_agent", "theta_router_node",
        "iota_validator_job", "sigma_cache_layer", "lambda_writer_unit", "mu_fetcher_client"]
SHORT = "tiny"
SECRETS = ["ASK-SECRET-7731", "ANSWER-SECRET-5512", "ARGS-SECRET-9084", "RATIONALE-SECRET-6620", "persona-secret-3391"]


def tc(name, ok=True):
    return {"ts": "2026-01-01T00:00:00Z", "name": name, "duration_s": 1.0, "args": SECRETS[2],
            "result": "r", "success": ok, "error": None if ok else "boom"}


def make_runs(n=40, extra_tools=()):
    tools = [HOSTILE] * 3 + LONG + [SHORT] + list(extra_tools)
    runs = []
    for i in range(n):
        seq = [tools[(i * 3 + k * 5) % len(tools)] for k in range(1 + i % 4)] + [HOSTILE] * (i % 2)
        runs.append({"run_id": "run-%04d" % i, "conversation_id": "conv-%d" % (i // 2), "agent": "a",
                     "environment": "", "first_ts": "2026-01-01T00:%02d:00Z" % (i % 60), "last_ts": "",
                     "latency_s": 5.0 + i, "user_message": SECRETS[0], "final_answer": SECRETS[1],
                     "ends_without_answer": False, "tool_calls": [tc(t, i % 7 != 3) for t in seq],
                     "n_llm_calls": 1, "models": [], "input_tokens": 100, "output_tokens": 50,
                     "cost_usd": None, "n_tool_failures": 0, "errors": [], "extra": {}})
    return runs


def write_workspace(d, runs):
    os.makedirs(os.path.join(d, "normalized"), exist_ok=True)
    with open(os.path.join(d, "normalized", "runs.jsonl"), "w", encoding="utf-8") as f:
        for r in runs:
            f.write(json.dumps(r) + "\n")
    with open(os.path.join(d, "scores.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["run_id", "outcome_score", "confidence", "rationale", "key_quote", "persona", "issue",
                    "reaction_run_id"])
        for i, r in enumerate(runs):
            s = (1, 0, -1)[i % 3]
            w.writerow([r["run_id"], s, "high", SECRETS[3], "", SECRETS[4], "" if s == 1 else "slow", ""])


def build(d, runs=None, system5=None):
    write_workspace(d, runs or make_runs())
    if system5 is not None:
        with open(os.path.join(d, "system5.json"), "w", encoding="utf-8") as f:
            json.dump(system5, f)
    with mock.patch.dict(os.environ, {"PLITE_NO_GATE": "1"}):
        with redirect_stdout(io.StringIO()):
            assert build_cig.main(["--out", d]) == 0
            assert build_viewer.main(["--out", d]) == 0
    with open(os.path.join(d, "cig.html"), encoding="utf-8") as f:
        return f.read()


def payload_of(page):
    m = re.search(r"var D = (\{.*?\});\n", page, re.S)
    return json.loads(m.group(1))


def flow_nodes(d):
    with open(os.path.join(d, "cig", "flow_graph.json"), encoding="utf-8") as f:
        return json.load(f)["nodes"]


class Anchors(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.anchors = []
        self._cur = None

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._cur = {"attrs": dict(attrs), "text": ""}
            self.anchors.append(self._cur)

    def handle_data(self, data):
        if self._cur is not None:
            self._cur["text"] += data

    def handle_endtag(self, tag):
        if tag == "a":
            self._cur = None


class ViewerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.d = cls.tmp.name
        cls.page = build(cls.d)
        cls.data = payload_of(cls.page)
        cls.nodes = flow_nodes(cls.d)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def hidden_pairs(self):
        return [(n, p) for n, p in zip(self.nodes, self.data["nodes"]) if p.get("hidden")]

    def test_self_contained(self):
        stripped = self.page.replace("http://www.w3.org/2000/svg", "").replace(S5, "")
        self.assertNotIn("http://", stripped)
        self.assertNotIn("https://", stripped)
        self.assertNotRegex(self.page, r"<script[^>]+src=|<link\b|@import|url\(\s*['\"]?https?:")
        self.assertNotIn("__SYSTEM5_URL__", self.page)
        self.assertNotIn("/*__DATA__*/", self.page)

    def test_two_or_three_names_blurred_and_absent_from_file(self):
        pairs = self.hidden_pairs()
        self.assertIn(len(pairs), (2, 3))
        for n, p in pairs:
            real = n["label"]
            self.assertGreaterEqual(len(real), build_viewer.MIN_BLUR_LEN)
            self.assertEqual(len(p["label"]), len(real))
            self.assertNotEqual(p["label"], real)
            for form in (real, json.dumps(real), build_viewer.safe_json(real), json.dumps(real)[1:-1], n["id"]):
                self.assertNotIn(form, self.page)
            self.assertNotIn(real.lower(), self.page.lower())
            self.assertNotIn(real.replace("_", " "), self.page)
        self.assertNotIn("tool:", self.page)  # opaque ids only
        self.assertEqual(len({p["id"] for p in self.data["nodes"]}), len(self.data["nodes"]))

    def test_never_the_busiest_and_only_long_names(self):
        tools = [n for n in self.nodes if n["kind"] == "tool"]
        busiest = max(tools, key=lambda n: (n["calls"], n["runs"]))
        self.assertEqual(busiest["label"], HOSTILE)
        labels = [p["label"] for p in self.data["nodes"]]
        self.assertIn(HOSTILE, labels)
        self.assertIn(SHORT, labels)
        for n, p in self.hidden_pairs():
            self.assertNotEqual(n["id"], busiest["id"])
            self.assertEqual(n["kind"], "tool")

    def test_selection_is_deterministic_and_order_independent(self):
        with tempfile.TemporaryDirectory() as d2:
            again = build(d2)
        self.assertEqual(again, self.page)  # byte for byte on a rerun
        ids = lambda nodes: sorted(n["id"] for n in nodes if n["id"] in build_viewer.pick_blurred(nodes))  # noqa: E731
        self.assertEqual(ids(self.nodes), ids(list(reversed(self.nodes))))
        self.assertEqual(ids(self.nodes), ids([dict(n, calls=n["calls"] + (n["label"] != HOSTILE)) for n in self.nodes]))

    def test_short_graph_blurs_nothing_it_cannot_hide(self):
        nodes = [{"id": "tool:ab", "label": "ab", "kind": "tool", "calls": 5, "runs": 5},
                 {"id": "tool:cd", "label": "cd", "kind": "tool", "calls": 3, "runs": 3}]
        self.assertEqual(build_viewer.pick_blurred(nodes), set())
        nodes.append({"id": "tool:long_name_tool", "label": "long_name_tool", "kind": "tool", "calls": 1, "runs": 1})
        nodes.append({"id": "tool:other_long_one", "label": "other_long_one", "kind": "tool", "calls": 9, "runs": 9})
        self.assertEqual(build_viewer.pick_blurred(nodes), {"tool:long_name_tool"})  # the busiest stays visible

    def test_tabs_recolour_the_graph(self):
        tabs = re.findall(r'<button type="button" class="tab[^"]*" data-dim="(\w+)"', self.page)
        self.assertEqual(sorted(tabs), ["cost", "errors", "latency", "score"])
        self.assertNotIn('class="lk"', self.page)  # no padlocks on the tabs
        self.assertRegex(self.page, r"querySelectorAll\('button\[data-dim\]'\)")
        self.assertIn("recolor()", self.page)
        # the call to action still opens System 5 in a new tab with noopener
        opens = re.findall(r"window\.open\([^)]*\)", self.page)
        self.assertTrue(opens)
        for o in opens:
            self.assertIn("noopener", o)
            self.assertIn("S5", o)
        self.assertEqual(self.data["url"], S5)

    def test_cta_is_a_link_button_to_the_url(self):
        p = Anchors()
        p.feed(self.page)
        cta = [a for a in p.anchors if a["attrs"].get("id") == "cta"]
        self.assertEqual(len(cta), 1)
        self.assertEqual(cta[0]["attrs"]["href"], S5)
        self.assertEqual(cta[0]["text"].strip(), CTA)
        self.assertIn("noopener", cta[0]["attrs"]["rel"])
        self.assertIn("wire5($('cta'))", self.page)

    def test_every_surface_opens_the_panel(self):
        for token in ("g.addEventListener('click'", "vis.addEventListener('click',pick)", "hit.addEventListener('click',pick)",
                      "tr.addEventListener('click'", "$('gwrap').addEventListener('click'"):
            self.assertIn(token, self.page)
        self.assertIn("tabindex:'0'", self.page)  # nodes and rows are focusable
        self.assertIn("ev.key==='Enter'||ev.key===' '", self.page)

    def test_no_dossier_or_per_run_data(self):
        self.assertEqual(sorted(self.data), ["conversations", "counts", "edges", "grouped", "has_scores", "nodes", "runs", "url"])
        self.assertIsInstance(self.data["runs"], int)
        for s in SECRETS + ["run-0000", "conv-0", "boom", "2026-01-01"]:
            self.assertNotIn(s, self.page)
        self.assertEqual(sorted(self.data["nodes"][1]), sorted(k for k in self.data["nodes"][1]
                                                              if k in {"id", "kind", "label", "calls", "runs", "score", "good", "bad", "hidden", "err", "lat", "cost"}))
        for e in self.data["edges"]:
            self.assertEqual(sorted(e), ["cost", "count", "err", "lat", "runs", "score", "source", "target"])

    def test_hostile_names_are_escaped(self):
        self.assertEqual(self.page.count("</script>"), 1)
        self.assertNotIn("<img src=x", self.page)
        self.assertNotIn("<!--", self.page)
        self.assertIn(HOSTILE, [p["label"] for p in self.data["nodes"]])  # round-trips through the JSON intact
        self.assertNotIn("innerHTML", self.page)  # names only ever reach the page as text

    def test_small_and_independent_of_run_count(self):
        self.assertLess(len(self.page), 100000)
        with tempfile.TemporaryDirectory() as d2:
            big = build(d2, make_runs(400))
        self.assertLess(abs(len(big) - len(self.page)), 0.1 * len(self.page))

    def test_light_and_dark_tokens(self):
        self.assertRegex(self.page, r":root\{--bg:#[0-9a-f]{6};")
        self.assertIn(':root[data-theme="light"]{--bg:', self.page)
        for tok in ("--green", "--grey", "--red"):
            self.assertGreaterEqual(self.page.count(tok + ":"), 2)
        self.assertIn('id="themeBtn"', self.page)
        self.assertIn("filter:blur(", self.page)

    def test_system5_json_override(self):
        with tempfile.TemporaryDirectory() as d2:
            page = build(d2, system5={"url": "https://example.org/s5?a=1&b=2", "accuracy_gain_pct": 123456, "extra": "LEAK-ME"})
        self.assertIn('href="https://example.org/s5?a=1&amp;b=2"', page)
        self.assertEqual(payload_of(page)["url"], "https://example.org/s5?a=1&b=2")
        self.assertNotIn(S5, page)
        self.assertNotIn("123456", page)
        self.assertNotIn("LEAK-ME", page)  # only the url is read

    def test_bad_override_falls_back(self):
        for bad in ({"url": "javascript:alert(1)"}, {"url": 5}, {"x": 1}, {"url": 'https://a.b/"onmouseover="x'}):
            with tempfile.TemporaryDirectory() as d2:
                self.assertEqual(payload_of(build(d2, system5=bad))["url"], S5)
        with tempfile.TemporaryDirectory() as d2:
            os.makedirs(d2, exist_ok=True)
            self.assertEqual(build_viewer.system5_url(d2), S5)
        with mock.patch.object(build_viewer, "SYSTEM5_TEMPLATE", os.path.join(self.d, "missing.json")):
            self.assertEqual(build_viewer.system5_url(self.d), build_viewer.DEFAULT_URL)

    def test_customer_wording(self):
        text = re.sub(r"<style.*?</style>", "", self.page, flags=re.S)
        for pat in (r"kap" + "pa", r"co" + "hen", r"\bn/a\b", r"\bunknown\b", r"\bunclear\b", r"\bTODO\b", r"\bNone\b", r"\bnan\b",
                    r"repair", r"mistake", r"\bdropped\b", r"placeholder", r"unrecovered"):
            self.assertIsNone(re.search(pat, text, re.I if pat in ("kap" + "pa", "co" + "hen", r"\bn/a\b", r"\bnan\b") else 0), pat)

    def test_tail_is_grouped_without_names(self):
        extra = ["extra_tool_number_%02d" % i for i in range(70)]
        with tempfile.TemporaryDirectory() as d2:
            page = build(d2, make_runs(150, extra))
        data = payload_of(page)
        self.assertEqual(sum(1 for n in data["nodes"] if n["kind"] == "tool"), build_viewer.MAX_TOOL_NODES)
        self.assertGreater(data["grouped"], 0)
        self.assertIn("other", [n["label"] for n in data["nodes"]])
        self.assertNotIn("other_tools", page)

    def test_gate_requires_cig_signoff(self):
        with tempfile.TemporaryDirectory() as d2:
            build(d2)
            os.remove(os.path.join(d2, "cig.html"))
            env = {k: v for k, v in os.environ.items() if k != "PLITE_NO_GATE"}
            with mock.patch.dict(os.environ, env, clear=True):
                with self.assertRaises(SystemExit):
                    build_viewer.main(["--out", d2])
            self.assertFalse(os.path.exists(os.path.join(d2, "cig.html")))

    def test_missing_cig_is_an_error(self):
        with tempfile.TemporaryDirectory() as d2, mock.patch.dict(os.environ, {"PLITE_NO_GATE": "1"}):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(build_viewer.main(["--out", d2]), 2)


if __name__ == "__main__":
    unittest.main()
