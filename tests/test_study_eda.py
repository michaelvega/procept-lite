import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN = os.path.join(ROOT, "plugins", "procept-lite")
STUDY_SRC = os.path.join(PLUGIN, "study")
sys.path.insert(0, os.path.join(ROOT, "tests", "fixtures"))
sys.path.insert(0, STUDY_SRC)
os.environ["PLITE_NO_GATE"] = "1"
from make_study_out import make_out  # noqa: E402

SCRATCH = os.environ.get("TMPDIR") or tempfile.gettempdir()
BANNED = ["growthfactor", "logfire", "langsmith", "/Users/", "nan", "None", "N/A", "TODO", "unclear", "unknown", "..."]
JARGON = re.compile(r"ka" + "ppa|co" + "hen|p-value|\\bau" + "c\\b", re.I)


def run_section(study, key, raw=None):
    env = dict(os.environ)
    env.pop("PLITE_RAW", None)
    if raw:
        env["PLITE_RAW"] = raw
    p = subprocess.run([sys.executable, "-I", os.path.join(study, "sections", key + ".py")], cwd=study, env=env,
                       capture_output=True, text=True)
    return p


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class StudyEdaReward(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="study-eda-", dir=SCRATCH)
        cls.out = os.path.join(cls.tmp, "out")
        cls.meta = make_out(cls.out, n=160)
        cls.study = os.path.join(cls.out, "study")
        shutil.copytree(STUDY_SRC, cls.study, ignore=shutil.ignore_patterns("__pycache__"))
        cls.raw = os.path.join(cls.tmp, "raw.jsonl")
        with open(cls.raw, "w", encoding="utf-8") as f:
            for i, rid in enumerate(cls.meta["run_ids"]):
                if i % 4 == 3:
                    continue
                f.write(json.dumps({"id": rid, "feedback": [{"key": "correctness", "score": 1 if i % 3 else 0, "run_id": rid}],
                                    "feedback_stats": {"helpfulness": {"avg": 0.5 + (i % 5) / 10.0, "n": 1}}}) + "\n")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def build(self, raw=None):
        for k in ("eda", "reward"):
            p = run_section(self.study, k, raw)
            self.assertEqual(p.returncode, 0, p.stderr)

    def test_sections_build_and_validate(self):
        self.build()
        sec = os.path.join(self.study, "sections")
        for k in ("eda", "reward"):
            for ext in ("html", "md", "json"):
                self.assertTrue(os.path.isfile(os.path.join(sec, "%s.%s" % (k, ext))), k + "." + ext)
            json.loads(read(os.path.join(sec, k + ".json")))
        import validate_sections as vs
        lines = [l for l in vs.validate(self.out) if re.match(r"sections/(eda|reward)", l)]
        self.assertEqual(lines, [])
        import build_study
        self.assertTrue(set(["eda", "reward"]) <= set(build_study.build(self.out)["sections"]))

    def test_deterministic(self):
        self.build()
        sec = os.path.join(self.study, "sections")
        first = {f: read(os.path.join(sec, f)) for f in ("eda.html", "eda.md", "reward.html", "reward.md")}
        self.build()
        for f, t in first.items():
            self.assertEqual(t, read(os.path.join(sec, f)), f)

    def test_no_banned_strings(self):
        self.build(self.raw)
        sec = os.path.join(self.study, "sections")
        for f in ("eda.html", "eda.md", "reward.html", "reward.md"):
            text = read(os.path.join(sec, f))
            visible = re.sub(r"<style>.*?</style>", "", text, flags=re.S)
            for b in BANNED:
                pat = r"\b%s\b" % re.escape(b) if b.isalpha() else re.escape(b)
                self.assertIsNone(re.search(pat, visible, re.I if b.islower() else 0), "%s has %r" % (f, b))
            self.assertIsNone(JARGON.search(text), f)
            self.assertNotIn("<td></td>", text)
            self.assertIsNone(re.search(r"(?:fill|stroke|background|color)\s*[:=]\s*[\"']?#[0-9a-fA-F]{3,6}\b", visible), f + " hard-coded colour")

    def test_raw_scores_found_when_present(self):
        self.build(self.raw)
        j = json.loads(read(os.path.join(self.study, "sections", "reward.json")))
        names = {s["field"] for s in j["raw_scores"]}
        self.assertTrue(any("correctness" in n for n in names), names)
        self.assertTrue(any("helpfulness" in n for n in names), names)
        self.assertFalse(any(n.endswith(".n") for n in names))
        self.build()
        j = json.loads(read(os.path.join(self.study, "sections", "reward.json")))
        self.assertEqual(j["raw_scores"], [])

    def test_headline_numbers_match_data(self):
        self.build()
        import sig
        R = sig.load(self.out)
        j = json.loads(read(os.path.join(self.study, "sections", "eda.json")))
        self.assertEqual(j["runs"], len(R))
        self.assertEqual(j["good_pct"], sig.pct(sum(r["outcome"] == 1 for r in R), len(R)))
        self.assertIn("%d runs" % len(R), read(os.path.join(self.study, "sections", "eda.html")))


if __name__ == "__main__":
    unittest.main()
