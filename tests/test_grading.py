import csv
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "plugins", "procept-lite", "scripts", "plite"))
import grade_prepare as gp  # noqa: E402
import grade_merge as gm  # noqa: E402

_GATE = {}


def setUpModule():  # these tests exercise the stages themselves, not the sign-off gates (test_gates.py covers those)
    _GATE["old"] = os.environ.get("PLITE_NO_GATE")
    os.environ["PLITE_NO_GATE"] = "1"


def tearDownModule():
    if _GATE.get("old") is None:
        os.environ.pop("PLITE_NO_GATE", None)
    else:
        os.environ["PLITE_NO_GATE"] = _GATE["old"]


def mkruns(spec):
    """spec: {conv: n_runs}"""
    runs = []
    t = 0
    for c, n in spec.items():
        for i in range(n):
            t += 1
            runs.append({"run_id": "%s_r%d" % (c, i), "conversation_id": c, "first_ts": "2026-01-01T00:%02d:00Z" % t,
                         "user_message": "msg %s %d" % (c, i), "tool_calls": [], "final_answer": "a"})
    return runs


def write_runs(out, runs):
    os.makedirs(os.path.join(out, "normalized"))
    with open(os.path.join(out, "normalized", "runs.jsonl"), "w") as f:
        for r in runs:
            f.write(json.dumps(r) + "\n")


def write_csv(path, rows):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(gm.COLS)
        for rid, sc in rows:
            w.writerow([rid, sc, "0.8", "why", "q", "Persona A", "" if sc == "1" else "bad thing", ""])


class TestSharding(unittest.TestCase):
    def test_conversations_stay_together(self):
        convs = gp.group_conversations(mkruns({"a": 3, "b": 4, "c": 2, "d": 5}))
        shards = gp.make_shards(convs, max_runs=6)
        for c in "abcd":
            self.assertEqual(len({i for i, s in enumerate(shards) for r in s if r["conversation_id"] == c}), 1)
        self.assertEqual(sum(len(s) for s in shards), 14)

    def test_oversize_conversation_alone(self):
        shards = gp.make_shards(gp.group_conversations(mkruns({"a": 1, "big": 9})), max_runs=3)
        self.assertEqual([len(s) for s in shards], [1, 9])

    def test_reaction_context(self):
        s = gp.group_conversations(mkruns({"a": 3}))[0]
        self.assertEqual(s[1]["next_run_id"], "a_r2")
        self.assertEqual(s[1]["next_user_message"], "msg a 2")
        self.assertEqual(s[2]["next_run_id"], None)
        self.assertEqual(s[2]["earlier_user_messages"], ["msg a 0", "msg a 1"])

    def test_audit_selection(self):
        ids = list(range(10))
        self.assertEqual(gp.audit_ids(ids, 4), [3, 7])
        self.assertEqual(gp.audit_ids(ids, 0), [])

    def test_prepare_outputs(self):
        with tempfile.TemporaryDirectory() as out:
            write_runs(out, mkruns({"a": 5, "b": 5, "c": 5}))
            est, wf = gp.prepare(out, max_runs=10, audit_every=2, audit_batch=3, product="Widget bot", price_per_mtok=1.0)
            g = os.path.join(out, "grading")
            self.assertEqual(est["shards"], 2)
            self.assertEqual(est["audit_runs"], 7)
            self.assertEqual(len(wf["audits"]), 3)
            self.assertIsNotNone(est["approx_usd"])
            brief = open(os.path.join(g, "brief.md")).read()
            self.assertIn(gm.CSV_HEADER, brief)
            self.assertIn("Widget bot", brief)
            self.assertNotIn("{{", brief)
            self.assertTrue(os.path.isdir(os.path.join(g, "scores")))
            with self.assertRaises(SystemExit):  # nothing graded yet is fine, so grade then re-prepare
                write_csv(os.path.join(g, "scores", "shard_01.csv"), [])
                gp.prepare(out)


class TestMerge(unittest.TestCase):
    def setup_out(self):
        d = tempfile.TemporaryDirectory()
        write_runs(d.name, mkruns({"a": 4, "b": 4}))
        gp.prepare(d.name, max_runs=4, audit_every=2)
        return d

    def test_clean_merge_and_audit(self):
        d = self.setup_out()
        g = os.path.join(d.name, "grading")
        idx = json.load(open(os.path.join(g, "shards_index.json")))
        for x in idx:
            write_csv(os.path.join(g, "scores", x["shard"] + ".csv"), [(r, "1") for r in x["run_ids"]])
        aidx = json.load(open(os.path.join(g, "audit_index.json")))
        for x in aidx:
            write_csv(os.path.join(g, "audit", x["shard"] + ".csv"), [(r, "1") for r in x["run_ids"]])
        s, rc = gm.merge(d.name)
        self.assertEqual(rc, 0)
        self.assertEqual(s["counts"]["good"], 8)
        self.assertEqual(s["audit"]["exact_share"], 1.0)
        self.assertEqual(s["audit"]["opposite_sign"], 0)
        self.assertEqual(len(list(csv.DictReader(open(os.path.join(d.name, "scores.csv"))))), 8)
        d.cleanup()

    def test_validation_errors(self):
        d = self.setup_out()
        g = os.path.join(d.name, "grading")
        idx = json.load(open(os.path.join(g, "shards_index.json")))
        a, b = idx
        write_csv(os.path.join(g, "scores", a["shard"] + ".csv"), [(a["run_ids"][0], "2")] + [(r, "0") for r in a["run_ids"][1:3]] + [("ghost", "1")])
        s, rc = gm.merge(d.name)
        self.assertEqual(rc, 1)
        text = " ".join(s["problems"][a["shard"]])
        self.assertIn("bad score", text)
        self.assertIn("missing run ids", text)
        self.assertIn("unexpected run ids", text)
        self.assertEqual(s["missing_shards"], [b["shard"]])
        self.assertEqual(s["rerun_shards"], sorted([a["shard"], b["shard"]]))
        d.cleanup()

    def test_bad_header_and_resume(self):
        d = self.setup_out()
        g = os.path.join(d.name, "grading")
        idx = json.load(open(os.path.join(g, "shards_index.json")))
        open(os.path.join(g, "scores", idx[0]["shard"] + ".csv"), "w").write("run_id,score\nx,1\n")
        s, rc = gm.merge(d.name)
        self.assertIn("header", s["problems"][idx[0]["shard"]][0])
        s, rc = gm.merge(d.name, resume=True)
        self.assertEqual(s["missing_shards"], [idx[1]["shard"]])
        res = json.load(open(os.path.join(g, "resume_args.json")))
        self.assertEqual([x["shard"] for x in res["shards"]], [idx[1]["shard"]])
        d.cleanup()


class TestAudit(unittest.TestCase):
    def test_opposite_signs_counted(self):
        pairs = [("1", "1")] * 20 + [("1", "-1")] * 5 + [("-1", "1")] * 10 + [("-1", "-1")] * 15 + [("0", "1")] * 3
        self.assertEqual(gm.opposite_count(pairs), 15)
        self.assertEqual(gm.opposite_count([]), 0)

    def test_agreement_is_a_plain_share(self):
        d = tempfile.TemporaryDirectory()
        write_runs(d.name, mkruns({"a": 4}))
        gp.prepare(d.name, max_runs=4, audit_every=1)
        g = os.path.join(d.name, "grading")
        idx = json.load(open(os.path.join(g, "shards_index.json")))
        write_csv(os.path.join(g, "scores", idx[0]["shard"] + ".csv"), [(r, "1") for r in idx[0]["run_ids"]])
        aidx = json.load(open(os.path.join(g, "audit_index.json")))
        write_csv(os.path.join(g, "audit", aidx[0]["shard"] + ".csv"),
                  [(r, "1" if i < 3 else "-1") for i, r in enumerate(aidx[0]["run_ids"])])
        s, rc = gm.merge(d.name)
        self.assertEqual((s["audit"]["n"], s["audit"]["exact_share"], s["audit"]["opposite_sign"]), (4, 0.75, 1))
        self.assertEqual(set(s["audit"]), {"n", "exact_share", "opposite_sign"})
        d.cleanup()


if __name__ == "__main__":
    unittest.main()
