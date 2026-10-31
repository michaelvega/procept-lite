"""End to end, strict (gates ON): export -> mapping rescue -> ingest -> grading -> merge -> cig -> study -> pages -> done.
Also: skipping any single sign-off blocks the next stage with a clear message."""
import csv
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import make_e2e_out as E  # noqa: E402

_OLD = {}


def setUpModule():
    _OLD["gate"] = os.environ.pop("PLITE_NO_GATE", None)


def tearDownModule():
    if _OLD.get("gate") is not None:
        os.environ["PLITE_NO_GATE"] = _OLD["gate"]


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.out = os.path.join(self.tmp, "out")
        self.f = E.Flow(self.out, os.path.join(self.tmp, "work"), n=64)

    def nxt(self, step):
        line = self.f.run()
        self.assertTrue(line.startswith("NEXT: %s |" % step), line)

    def blocked(self, *args, word):
        rc, so, se = self.f.plite(*args)
        self.assertNotEqual(rc, 0, so)
        self.assertIn(word, se)
        self.assertTrue(se.strip())

    def test_full_flow(self):
        f, out = self.f, self.out
        self.nxt("mapping_rescue")
        self.assertFalse(os.path.exists(os.path.join(out, "normalized")))
        f.write_mapping("claude-haiku-4-5")
        self.nxt("mapping_rescue")
        f.write_mapping()
        self.nxt("ingest_verify")
        self.blocked("grade-prepare", "--out", out, word="ingest")
        f.sign_ingest()
        self.nxt("grade")
        f.write_shards()
        self.nxt("merge_verify")
        self.blocked("cig", "--out", out, word="merge")
        ids = f.scored_ids()
        self.assertGreaterEqual(len(ids), 60)
        f.sign("merge", ids, checked=45)
        self.nxt("cig_verify")
        self.blocked("viewer", "--out", out, word="cig")
        f.sign("cig", ["runs.csv", "nodes.csv", "edges.csv", "summary.json", "errors.csv"])
        self.nxt("study")
        self.assertTrue(os.path.isdir(os.path.join(out, "study")))
        self.blocked("report", "--out", out, word="study")
        f.run_starters()
        for k in ("eda", "reward", "affinity", "empathy", "impact"):
            self.assertTrue(os.path.isfile(os.path.join(out, "study", "sections", k + ".html")), k)
        f.write_problem()
        self.nxt("study")          # problem written but the study is not signed off
        f.sign_study()
        self.nxt("scorecard_verify")
        for n in ("scorecard.html", "cig.html", "scorecard.json"):
            self.assertTrue(os.path.isfile(os.path.join(out, n)), n)
        self.assertFalse(os.path.exists(os.path.join(out, "study")))
        page = open(os.path.join(out, "scorecard.html"), encoding="utf-8").read()
        sys.path.insert(0, os.path.join(E.ROOT, "plugins", "procept-lite", "scripts"))
        from plite import report
        report.lint_customer_html(page)          # raises on a hit
        cig = open(os.path.join(out, "cig.html"), encoding="utf-8").read().lower()
        for bad in ("ka" + "ppa", "co" + "hen", "todo", "/users/"):
            self.assertNotIn(bad, cig)
        f.sign("scorecard", ["scorecard.html", "cig.html", "scorecard.json", "system5.json", "summary.json"], checked=5)
        self.nxt("done")
        rc, so, se = f.plite("status", "--out", out)
        self.assertEqual(rc, 0)
        self.assertNotIn("missing", so)
        self.assertNotIn("stale", so)

    def test_skipped_signoffs_block(self):
        """Each stage refuses to start without the sign-off before it."""
        f, out = self.f, self.out
        f.run()
        f.write_mapping()
        f.run()
        self.blocked("grade-prepare", "--out", out, word="ingest")
        self.nxt("ingest_verify")                  # run itself stops here too
        self.assertFalse(os.path.exists(os.path.join(out, "grading", "shards_index.json")))
        f.sign_ingest()
        f.run()
        f.write_shards()
        f.run()
        self.assertFalse(os.path.exists(os.path.join(out, "cig")))
        self.blocked("cig", "--out", out, word="merge")
        f.sign("merge", f.scored_ids(), checked=45)
        f.run()
        self.blocked("viewer", "--out", out, word="cig")
        self.assertFalse(os.path.exists(os.path.join(out, "scorecard.html")))
        f.sign("cig", ["runs.csv", "nodes.csv", "edges.csv", "summary.json", "errors.csv"])
        f.run()
        f.run_starters()
        f.write_problem()
        self.blocked("report", "--out", out, word="study")
        self.assertFalse(os.path.exists(os.path.join(out, "scorecard.html")))
        self.nxt("study")


if __name__ == "__main__":
    unittest.main()
