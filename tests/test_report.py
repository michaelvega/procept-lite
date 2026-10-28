import csv
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import unittest
import xml.dom.minidom
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "plugins", "procept-lite", "scripts"))

from plite import report, scorecard, estimates, docx_writer  # noqa: E402

RUN_COLS = ["run_id", "conversation_id", "agent", "environment", "first_ts", "latency_s",
            "input_tokens", "output_tokens", "tokens", "cost_usd", "n_tool_calls",
            "n_tool_failures", "tool_fail", "unrecovered", "no_answer", "slow", "heavy",
            "outcome_score", "label", "persona", "workflow"]


def make_cig(out, graded=True, n=40):
    rnd = random.Random(3)
    os.makedirs(os.path.join(out, "cig"))
    rows, scores, dossiers = [], [], []
    for i in range(n):
        bad = i % 5 == 0
        amb = i % 5 == 1
        tools = 12 if i % 10 == 0 else rnd.randint(1, 4)
        fails = 3 if bad else (1 if amb else 0)
        score = -1 if bad else (0 if amb else 1)
        persona = ["Power user", "New user", "Ops manager"][i % 3]
        rows.append({"run_id": "r%d" % i, "conversation_id": "c%d" % (i // 2), "latency_s": 5 + i * (3 if i > 35 else 0.2),
                     "input_tokens": 1000 + 100 * tools, "output_tokens": 200, "tokens": 1200 + 100 * tools,
                     "cost_usd": "", "n_tool_calls": tools, "n_tool_failures": fails,
                     "tool_fail": int(fails > 0), "unrecovered": int(bad), "no_answer": int(bad and i % 2 == 0),
                     "slow": int(i > 35), "heavy": int(tools >= 8),
                     "outcome_score": score if graded else "", "label": "ungraded", "persona": persona,
                     "workflow": "search > fetch"})
        scores.append({"run_id": "r%d" % i, "outcome_score": score, "persona": persona,
                       "issue": "" if score == 1 else "wrong result"})
        dossiers.append({"run_id": "r%d" % i, "user_message": "show my open tickets"})
    with open(os.path.join(out, "cig", "runs.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, RUN_COLS)
        w.writeheader()
        w.writerows(rows)
    with open(os.path.join(out, "cig", "run_dossiers.jsonl"), "w") as f:
        f.write("\n".join(json.dumps(d) for d in dossiers))
    if graded:
        with open(os.path.join(out, "scores.csv"), "w", newline="") as f:
            w = csv.DictWriter(f, ["run_id", "outcome_score", "confidence", "rationale", "key_quote",
                                   "persona", "issue", "reaction_run_id"], restval="")
            w.writeheader()
            w.writerows(scores)
    with open(os.path.join(out, "cig", "errors.csv"), "w", newline="") as f:
        f.write("error_class,count,runs,tool,example\ntimeout,6,4,fetch,x\nbad_args,3,2,search,y\n")
    with open(os.path.join(out, "cig", "nodes.csv"), "w", newline="") as f:
        f.write("node_id,kind,calls,runs,fail_calls,fail_rate\ntool:fetch,tool,50,30,9,0.18\ntool:search,tool,60,40,2,0.03\n")
    with open(os.path.join(out, "cig", "summary.json"), "w") as f:
        json.dump({}, f)


class EdgeCaseTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def _rewrite(self, **fields):
        p = os.path.join(self.d, "cig", "runs.csv")
        rows = list(csv.DictReader(open(p)))
        for r in rows:
            r.update(fields)
        with open(p, "w", newline="") as f:
            w = csv.DictWriter(f, RUN_COLS)
            w.writeheader()
            w.writerows(rows)

    def test_equal_latency_has_no_slow_tail(self):
        make_cig(self.d)
        self._rewrite(latency_s="7", slow="")
        sc = scorecard.compute(self.d)
        self.assertEqual(sum(r["slow"] for r in sc["runs"]), 0)
        self.assertEqual(sc["all"]["slow_pct"], 0)
        self.assertFalse(estimates.latency(sc)["tail"])
        self.assertFalse(any("slow tail" in t for t, _ in estimates.fixes(sc)))
        md = open(report.build(self.d)["md"]).read()
        self.assertIn("no slow tail", md)
        self.assertNotIn("100%", md.split("## What we can reach")[0].split("Latency: in slowest decile")[1])

    def test_missing_latency_renders_dash(self):
        make_cig(self.d)
        self._rewrite(latency_s="", slow="")
        sc = scorecard.compute(self.d)
        self.assertIsNone(estimates.latency(sc))
        md = open(report.build(self.d)["md"]).read()
        self.assertIn("no latency data", md)
        self.assertNotIn("0.0s", md)
        self.assertRegex(md, r"Latency: median \| -")

    def test_missing_tokens_renders_dash(self):
        make_cig(self.d)
        self._rewrite(tokens="0", input_tokens="0", output_tokens="0")
        md = open(report.build(self.d)["md"]).read()
        self.assertRegex(md, r"Cost: median tokens \| -")
        self.assertNotIn("tokens and", md)

    def test_pluralize(self):
        self.assertEqual(report._pl(1, "run"), "1 run")
        self.assertEqual(report._pl(2, "run"), "2 runs")
        self.assertEqual(report._pl(1, "error class", "error classes"), "1 error class")
        self.assertEqual(report._pl(3, "error class", "error classes"), "3 error classes")


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_graded_report(self):
        make_cig(self.d)
        info = report.build(self.d)
        md = open(info["md"]).read()  # noqa
        for s in ("Outcome split", "What we can reach", "Power user", "50% cut", "not forecasts"):
            self.assertIn(s, md)
        self.assertLessEqual(info["pages"], 1)
        sc = scorecard.compute(self.d)
        self.assertEqual(sum(sc["split"].values()), 40)
        self.assertEqual(len(sc["personas"]), 3)
        g = estimates.goals(sc)
        self.assertLessEqual(g["now"], g["half"])
        self.assertLessEqual(g["half"], g["ceiling"])
        self.assertLessEqual(report.body_words(report.fit(sc)[0]), report.MAX_BODY_WORDS)

    def test_docx_valid(self):
        make_cig(self.d)
        info = report.build(self.d)
        with zipfile.ZipFile(info["docx"]) as z:
            self.assertIsNone(z.testzip())
            for name in z.namelist():
                xml.dom.minidom.parseString(z.read(name))
            doc = z.read("word/document.xml").decode()
        self.assertIn('w:left="1008"', doc)
        self.assertIn("<w:tbl>", doc)
        self.assertIn("<w:b/>", doc)
        if shutil.which("textutil"):
            r = subprocess.run(["textutil", "-convert", "txt", "-stdout", info["docx"]],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0)
            self.assertIn("Scorecard", r.stdout)

    def test_ungraded(self):
        make_cig(self.d, graded=False)
        info = report.build(self.d)
        md = open(info["md"]).read()  # noqa
        self.assertIn("not graded yet", md.lower())
        self.assertFalse(info["graded"])

    def test_no_runs_errors(self):
        with self.assertRaises(SystemExit):
            report.build(self.d)

    def test_docx_writer_escapes(self):
        p = os.path.join(self.d, "x.docx")
        docx_writer.write_docx(p, [("title", "A & <B>"), ("p", "**bold** \x01 text"),
                                   ("bullet", "b"), ("table", [["a", "b"], ["1", "2"]], None)])
        with zipfile.ZipFile(p) as z:
            xml.dom.minidom.parseString(z.read("word/document.xml"))

    def test_main_cli(self):
        make_cig(self.d)
        self.assertEqual(report.main(["--out", self.d]), 0)
        self.assertTrue(os.path.exists(os.path.join(self.d, "REPORT.docx")))


if __name__ == "__main__":
    unittest.main()
