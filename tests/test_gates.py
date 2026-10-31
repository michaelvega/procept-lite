"""Gates: the strict flow end to end (no automap, sign-offs, unusable ledger, stale detection, overrides) plus checks.py units."""
import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLITE = os.path.join(ROOT, "plugins", "procept-lite", "scripts", "plite.py")
sys.path.insert(0, os.path.join(ROOT, "plugins", "procept-lite", "scripts"))
from plite import checks  # noqa: E402

FIXTURE = os.path.join(ROOT, "tests", "fixtures", "otel_logfire.jsonl")
COLS = ["run_id", "outcome_score", "confidence", "rationale", "key_quote", "persona", "issue", "reaction_run_id"]


_GATE = {}


def setUpModule():  # this module is about gating: switch the dev bypass off, put it back afterwards
    _GATE["old"] = os.environ.pop("PLITE_NO_GATE", None)


def tearDownModule():
    if _GATE.get("old") is not None:
        os.environ["PLITE_NO_GATE"] = _GATE["old"]


def plite(*args, **kw):
    env = {k: v for k, v in os.environ.items() if k != "PLITE_NO_GATE"}
    p = subprocess.run([sys.executable, PLITE] + list(args), capture_output=True, text=True, env=env)
    return p.returncode, p.stdout, p.stderr


def last_line(s):
    return [x for x in s.strip().splitlines() if x][-1]


def make_src(d):
    """The fixture plus one run whose user message is blank (becomes an automatic `no_ask` unusable run)."""
    lines = open(FIXTURE, encoding="utf-8").read().splitlines()
    r = json.loads(lines[0])
    a = json.loads(r["attributes"])
    a["gen_ai.agent.call.id"] = "call-6"
    a["gen_ai.input.messages"] = [{"role": "user", "parts": [{"type": "text", "content": "   "}]}]
    r.update(trace_id="t6", span_id="a6", attributes=json.dumps(a))
    p = os.path.join(d, "export.jsonl")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(lines + [json.dumps(r)]) + "\n")
    return p


def wjson(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f)


def ev(*ids):
    return [{"id": i, "finding": "checked %s against its raw rows: %d matches" % (i, n)} for n, i in enumerate(ids)]


class ModelFloor(unittest.TestCase):
    def test_floor(self):
        for ok in ("claude-sonnet-5-5", "Sonnet 5.5", "GPT 5.6 Terra", "gpt-5.6-terra", "claude-opus-5-5", "Fable 5.1"):
            self.assertTrue(checks.model_allowed(ok), ok)
        for bad in ("claude-haiku-4-5", "haiku", "gpt-5-mini", "gpt-5-nano", "claude-sonnet-4-5", "sonnet 4", "", None):
            self.assertFalse(checks.model_allowed(bad), bad)


class Strict(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.src = make_src(self.tmp)
        self.out = os.path.join(self.tmp, "out")
        self.map = os.path.join(self.out, "mapping.json")

    def run_(self):
        rc, so, se = plite("run", "--src", self.src, "--out", self.out)
        self.assertEqual(rc, 0, se)
        return last_line(so)

    def mapping(self, model):
        d = json.load(open(os.path.join(self.out, "mapping_rescue", "mapping.draft.json")))
        d = {k: v for k, v in d.items() if not k.startswith("_")}
        d["_rescue"] = {"model": model, "rows_reviewed": 16, "notes": "compared the sample with the draft"}
        wjson(self.map, d)

    def sign(self, stage, ids, **extra):
        rec = dict(stage=stage, model="claude-sonnet-5-5", verdict="pass", checked=len(ids), mismatches=0, notes="ok",
                   evidence=ev(*ids))
        rec.update(extra)
        f = os.path.join(self.tmp, stage + ".json")
        wjson(f, rec)
        rc, so, se = plite("check", "record", "--out", self.out, "--file", f)
        self.assertEqual(rc, 0, se)

    def test_flow(self):
        # (a) nothing is guessed: scaffold, then wait for a model
        self.assertTrue(self.run_().startswith("NEXT: mapping_rescue |"))
        for n in ("sample_raw.jsonl", "mapping.draft.json", "draft_preview.jsonl", "README.md"):
            self.assertTrue(os.path.exists(os.path.join(self.out, "mapping_rescue", n)), n)
        self.assertFalse(os.path.exists(os.path.join(self.out, "normalized")))
        self.assertFalse(os.path.exists(self.map))
        # direct normalize refuses too
        rc, so, se = plite("normalize", "--src", self.src, "--out", self.out)
        self.assertNotEqual(rc, 0)
        self.assertFalse(os.path.exists(os.path.join(self.out, "normalized", "runs.jsonl")))
        # (b) a small model is rejected, a floor model accepted
        self.mapping("claude-haiku-4-5")
        self.assertTrue(self.run_().startswith("NEXT: mapping_rescue |"))
        self.assertFalse(os.path.exists(os.path.join(self.out, "normalized", "runs.jsonl")))
        self.mapping("claude-sonnet-5-5")
        self.assertTrue(self.run_().startswith("NEXT: ingest_verify |"))
        runs = [json.loads(x) for x in open(os.path.join(self.out, "normalized", "runs.jsonl"))]
        ids = [r["run_id"] for r in runs]
        self.assertEqual(len(ids), 6)
        # grade-prepare is gated
        rc, so, se = plite("grade-prepare", "--out", self.out)
        self.assertNotEqual(rc, 0)
        self.assertIn("ingest", se)
        # (c) sign-off unlocks grading; blank-ask and model-flagged runs go to the ledger and into no shard
        self.sign("ingest", ids, unusable=[{"run_id": "call-5", "reason": "answer belongs to another run"}])
        self.assertTrue(self.run_().startswith("NEXT: grade |"))
        ledger = list(csv.DictReader(open(os.path.join(self.out, "normalized", "unusable.csv"))))
        self.assertEqual({(r["run_id"], r["reason"], r["source"]) for r in ledger},
                         {("call-6", "no_ask", "auto"), ("call-5", "answer belongs to another run", "model")})
        g = os.path.join(self.out, "grading")
        in_shards = set()
        for fn in os.listdir(os.path.join(g, "shards")) + [os.path.join("..", "audit", x) for x in os.listdir(os.path.join(g, "audit"))]:
            for line in open(os.path.join(g, "shards", fn)):
                in_shards.add(json.loads(line)["run_id"])
        self.assertEqual(in_shards, {"call-1", "call-2", "call-3", "call-4"})
        # (d) fake graders, merge, then the merge sign-off unlocks the CIG
        idx = json.load(open(os.path.join(g, "shards_index.json")))
        for x in idx:
            with open(os.path.join(g, "scores", x["shard"] + ".csv"), "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(COLS)
                for i, rid in enumerate(x["run_ids"]):
                    s = [1, 0, -1, 1][i % 4]
                    w.writerow([rid, s, "0.8", "reviewed the transcript", "", "order tracker", "" if s == 1 else "late answer", ""])
        for x in json.load(open(os.path.join(g, "audit_index.json"))):
            with open(os.path.join(g, "audit", x["shard"] + ".csv"), "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(COLS)
                for rid in x["run_ids"]:
                    w.writerow([rid, 1, "0.7", "second look", "", "order tracker", "", ""])
        self.assertTrue(self.run_().startswith("NEXT: merge_verify |"))
        scores = list(csv.DictReader(open(os.path.join(self.out, "scores.csv"))))
        self.assertEqual({r["run_id"] for r in scores}, {"call-1", "call-2", "call-3", "call-4"})
        rc, so, se = plite("cig", "--out", self.out)
        self.assertNotEqual(rc, 0)
        self.assertIn("merge", se)
        self.sign("merge", ["call-1", "call-2", "call-3", "call-4"])
        self.assertTrue(self.run_().startswith("NEXT: cig_verify |"))
        runs_csv = list(csv.DictReader(open(os.path.join(self.out, "cig", "runs.csv"))))
        self.assertEqual({r["run_id"] for r in runs_csv}, {"call-1", "call-2", "call-3", "call-4"})
        # (f) overrides change scores.csv without making the merge sign-off stale
        with open(os.path.join(g, "overrides.csv"), "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["run_id", "outcome_score", "rationale", "reason"])
            w.writerow(["call-1", -1, "the answer contradicts the tool result", "contradiction"])
        rc, so, se = plite("grade-merge", "--out", self.out)
        self.assertEqual(rc, 0, se)
        sc = {r["run_id"]: r["outcome_score"] for r in csv.DictReader(open(os.path.join(self.out, "scores.csv")))}
        self.assertEqual(sc["call-1"], "-1")
        self.assertEqual(json.load(open(os.path.join(g, "summary.json")))["overrides_applied"], 1)
        self.assertEqual(checks.state(self.out, "merge"), "verified")
        self.assertIn("quote_not_found", json.load(open(os.path.join(g, "summary.json"))))
        # (e) editing the mapping after sign-off makes the ingest gate stale
        self.assertEqual(checks.state(self.out, "ingest"), "verified")
        m = json.load(open(self.map))
        m["_rescue"]["notes"] = "edited afterwards"
        wjson(self.map, m)
        self.assertEqual(checks.state(self.out, "ingest"), "stale")
        with self.assertRaises(SystemExit) as cm:
            checks.require(self.out, ["ingest"])
        self.assertEqual(len(str(cm.exception).splitlines()), 1)
        self.assertIn("changed after it was signed off", str(cm.exception))


class CheckRecords(unittest.TestCase):
    def setUp(self):
        self.out = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.out, True)
        os.makedirs(os.path.join(self.out, "normalized"))
        with open(os.path.join(self.out, "normalized", "runs.jsonl"), "w") as f:
            for i in range(3):
                f.write(json.dumps({"run_id": "r%d" % i, "user_message": "hello"}) + "\n")
        wjson(os.path.join(self.out, "mapping.json"), {"_rescue": {"model": "sonnet 5.5", "rows_reviewed": 3, "notes": "x"}})

    def rec(self, **kw):
        d = dict(stage="ingest", model="gpt-5.6-terra", verdict="pass", checked=3, mismatches=0, notes="ok",
                 evidence=ev("r0", "r1", "r2"))
        d.update(kw)
        return d

    def test_record_and_stale(self):
        checks.record(self.out, self.rec())
        self.assertEqual(checks.state(self.out, "ingest"), "verified")
        with open(os.path.join(self.out, "normalized", "runs.jsonl"), "a") as f:
            f.write(json.dumps({"run_id": "r9", "user_message": "x"}) + "\n")
        self.assertEqual(checks.state(self.out, "ingest"), "stale")

    def test_rejections(self):
        for kw in ({"model": "claude-haiku-4-5"}, {"evidence": ev("r0", "r1", "ghost")}, {"checked": 1},
                   {"unusable": [{"run_id": "nope", "reason": "x"}]}, {"stage": "bogus"}, {"mismatches": 1}):
            with self.assertRaises(ValueError, msg=str(kw)):
                checks.record(self.out, self.rec(**kw))
        self.assertEqual(checks.state(self.out, "ingest"), "missing")

    def test_fail_record(self):
        checks.record(self.out, self.rec(verdict="fail", checked=1, mismatches=1, evidence=ev("r0")))
        self.assertEqual(checks.state(self.out, "ingest"), "failed")
        with self.assertRaises(SystemExit) as cm:
            checks.require(self.out, ["ingest"])
        self.assertIn("FAIL", str(cm.exception))

    def test_corrupt_record_is_missing(self):
        os.makedirs(os.path.join(self.out, "checks"))
        for junk in ("", "{not json", '["list"]', '{"stage": "ingest"'):
            with open(checks.path_for(self.out, "ingest"), "w") as f:
                f.write(junk)
            self.assertEqual(checks.state(self.out, "ingest"), "missing", junk)

    def test_unusable_ids(self):
        checks.record(self.out, self.rec(unusable=[{"run_id": "r1", "reason": "mixed up"}]))
        self.assertEqual(checks.unusable_ids(self.out), {"r1": "mixed up"})

    def test_no_temp_files_left(self):
        checks.record(self.out, self.rec())
        self.assertEqual(os.listdir(os.path.join(self.out, "checks")), ["ingest.json"])


class SecretKeyMasking(unittest.TestCase):
    def test_scaffold_masks_secret_named_keys_and_phones(self):
        from plite import scaffold
        out = scaffold.mask({"password": "hunter2-9", "api_key": "abcd1234", "input_tokens": 12,
                             "note": "call +1 415 555 0123", "n": {"token": "zzzz9999"}})
        self.assertEqual(out["password"], "[secret]")
        self.assertEqual(out["api_key"], "[secret]")
        self.assertEqual(out["n"]["token"], "[secret]")
        self.assertEqual(out["input_tokens"], 12)
        self.assertNotIn("415", out["note"])

    def test_check_help_does_not_name_the_bypass(self):
        self.assertNotIn("PLITE_NO_GATE", (checks.__doc__ or ""))


if __name__ == "__main__":
    unittest.main()
