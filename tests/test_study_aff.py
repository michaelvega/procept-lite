"""Tests for the affinity, empathy and impact study starters (they run on the synthetic out from make_study_out)."""
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections import Counter, defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN = os.path.join(ROOT, "plugins", "procept-lite")
STUDY_SRC = os.path.join(PLUGIN, "study")
sys.path.insert(0, os.path.join(ROOT, "tests", "fixtures"))
sys.path.insert(0, STUDY_SRC)
os.environ["PLITE_NO_GATE"] = "1"
from make_study_out import make_out  # noqa: E402
import sig  # noqa: E402
import validate_sections as vs  # noqa: E402

SCRATCH = os.environ.get("TMPDIR") or tempfile.gettempdir()
KEYS = ("affinity", "empathy", "impact")
BANNED = re.compile(r"(?i:ka" r"ppa|co" r"hen|growthfactor|/Users/|\bunclear\b|\bunknown\b)|\bN/A\b|\bnan\b|\bNone\b|\bTODO\b")
SRC_BANNED = re.compile(r"ka" r"ppa|co" r"hen|growthfactor|/Users/", re.I)


def read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def csv_rows(p):
    with open(p, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def words(s):
    return len(re.findall(r"\w+", s))


def stage(out):
    """A copy of the study folder inside out, as study-init leaves it."""
    shutil.copytree(STUDY_SRC, os.path.join(out, "study"), ignore=shutil.ignore_patterns("__pycache__", "tests"))
    with open(os.path.join(out, "study", ".procept-lite-study"), "w") as f:
        f.write("marker\n")


def run_sections(out, keys=KEYS):
    for k in keys:
        r = subprocess.run([sys.executable, "-I", os.path.join(out, "study", "sections", k + ".py")], capture_output=True, text=True, cwd=out)
        if r.returncode != 0:
            raise AssertionError("%s failed:\n%s" % (k, r.stderr[-1500:]))


def digest(out):
    h = hashlib.sha256()
    sec = os.path.join(out, "study", "sections")
    files = [os.path.join(sec, f) for f in sorted(os.listdir(sec)) if not f.endswith((".py", ".pyc")) and os.path.isfile(os.path.join(sec, f))]
    fr = os.path.join(out, "study", "fix_reports")
    files += [os.path.join(fr, f) for f in sorted(os.listdir(fr))]
    for p in files:
        with open(p, "rb") as f:
            h.update(os.path.basename(p).encode() + f.read())
    return h.hexdigest()


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="aff-", dir=SCRATCH)
        cls.out = os.path.join(cls.tmp, "out")
        cls.meta = make_out(cls.out, n=160)
        stage(cls.out)
        run_sections(cls.out)
        cls.sec = os.path.join(cls.out, "study", "sections")
        cls.R = sig.load(cls.out)
        cls.BY = {r["run_id"]: r for r in cls.R}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)


class OutputTests(Base):
    def test_files_exist(self):
        for k in KEYS:
            for ext in ("py", "html", "md", "json"):
                self.assertTrue(os.path.isfile(os.path.join(self.sec, "%s.%s" % (k, ext))), "%s.%s" % (k, ext))
        for f in ("issues.csv", "affinity.json", "personas.csv", "personas.json", "impact.json"):
            self.assertTrue(os.path.isfile(os.path.join(self.sec, f)), f)
        for k in KEYS:
            self.assertTrue(read(os.path.join(self.sec, k + ".md")).startswith("## "))
            self.assertIn('<section id="%s" class="step">' % k, read(os.path.join(self.sec, k + ".html")))

    def test_deterministic(self):
        first = digest(self.out)
        other = os.path.join(self.tmp, "again")
        make_out(other, n=160)
        stage(other)
        run_sections(other)
        self.assertEqual(first, digest(other))

    def test_no_banned_strings(self):
        sec, fr = self.sec, os.path.join(self.out, "study", "fix_reports")
        paths = [os.path.join(sec, f) for f in os.listdir(sec) if f.endswith((".html", ".md", ".json", ".csv"))]
        paths += [os.path.join(fr, f) for f in os.listdir(fr)]
        for p in paths:
            text = read(p)
            if p.endswith(".html"):
                text = re.sub(r"<style.*?</style>", " ", text, flags=re.S)
            m = BANNED.search(text)
            self.assertIsNone(m, "%s contains %r" % (os.path.basename(p), m and m.group(0)))
        for k in KEYS + ("aff_rules",):
            m = SRC_BANNED.search(read(os.path.join(STUDY_SRC, "sections", k + ".py")))
            self.assertIsNone(m, "%s.py contains %r" % (k, m and m.group(0)))

    def test_validator_clean_for_these_sections(self):
        mine = [p for p in vs.validate(self.out) if not re.search(r"sections/(eda|reward)\b", p)]
        self.assertEqual(mine, [])

    def test_headers(self):
        for k, title in zip(KEYS, ("Affinity Diagram", "Empathy Map", "Impact")):
            h = read(os.path.join(self.sec, k + ".html"))
            self.assertIn('<h2 class="big">%s</h2>' % title, h)
            self.assertEqual(len(re.findall(r'<p class="sub">', h)), 1)
            self.assertLessEqual(words(re.search(r'<p class="sub">(.*?)</p>', h).group(1)), 18)


class AffinityTests(Base):
    def test_every_short_run_has_a_row(self):
        rows = csv_rows(os.path.join(self.sec, "issues.csv"))
        self.assertEqual(list(rows[0]), ["run_id", "issue", "topic", "theme"])
        have = {r["run_id"] for r in rows if r["issue"].strip()}
        short = {r["run_id"] for r in self.R if r["outcome"] != 1}
        self.assertEqual(have, short)
        self.assertTrue(all(r["topic"].strip() and r["theme"].strip() for r in rows))

    def test_issue_and_topic_belong_to_one_theme(self):
        rows = csv_rows(os.path.join(self.sec, "issues.csv"))
        for col in ("issue", "topic"):
            m = defaultdict(set)
            for r in rows:
                m[r[col]].add(r["theme"])
            self.assertTrue(all(len(v) == 1 for v in m.values()), col)

    def test_general_themes_come_first(self):
        h = read(os.path.join(self.sec, "affinity.html"))
        self.assertLess(h.index("Goal dodged entirely"), h.index("Topic clusters"))
        self.assertLess(h.index("Topic clusters"), h.index("distinct issues"))
        self.assertIn('<details class="dd">', h)
        self.assertNotIn("<details class=\"dd\" open", h)
        j = json.loads(read(os.path.join(self.sec, "affinity.json")))
        names = [t["theme"] for t in j["themes"]]
        self.assertIn("Goal dodged entirely", names)
        self.assertIn("Upsell or roadmap opportunity", names)
        self.assertEqual(j["n_runs_fell_short"], sum(1 for r in self.R if r["outcome"] != 1))
        self.assertEqual(sum(t["bad"] + t["ambiguous"] for t in j["themes"]) >= j["n_runs_fell_short"], True)

    def test_every_run_in_the_diagram_numbers(self):
        j = json.loads(read(os.path.join(self.sec, "affinity.json")))
        for t in j["themes"]:
            self.assertEqual(sum(x["runs"] for x in j["topics"] if x["theme"] == t["theme"]) >= t["runs"], True)


class EmpathyTests(Base):
    def personas(self, sec=None):
        sec = sec or self.sec
        return json.loads(read(os.path.join(sec, "personas.json")))["personas"]

    def check(self, sec, R):
        P = self.personas(sec)
        rows = csv_rows(os.path.join(sec, "personas.csv"))
        self.assertEqual(sorted(r["run_id"] for r in rows), sorted(r["run_id"] for r in R))
        self.assertTrue(all(r["persona"].strip() for r in rows))
        counts = Counter(r["persona"] for r in rows)
        names = {p["name"] for p in P}
        self.assertEqual(set(counts), names)
        for p in P:
            self.assertTrue(2 <= words(p["name"]) <= 4, p["name"])
            self.assertGreaterEqual(words(p["goal"]), 4)
            self.assertGreaterEqual(words(p["pain"]), 3)
            for k in ("goal", "pain", "gain"):
                self.assertTrue(p[k].strip().endswith((".", "!", "?")) and "..." not in p[k] and "…" not in p[k], p[k])
            self.assertEqual(p["n"], counts[p["name"]])
            for k in ("good_share", "fell_short_share", "med_latency_s", "tool_fail_pct", "pain", "gain"):
                self.assertIn(k, p)
        others = [p for p in P if vs.is_other(p["name"])]
        for p in others:
            self.assertLessEqual(p["n"], 0.15 * len(R))
        top = max(P, key=lambda p: p["n"])
        self.assertFalse(vs.is_other(top["name"]))
        self.assertGreaterEqual(len([p for p in P if not vs.is_other(p["name"])]), 2)
        return P

    def test_personas_on_synthetic(self):
        P = self.check(self.sec, self.R)
        by = {r["run_id"]: r["persona"] for r in csv_rows(os.path.join(self.sec, "personas.csv"))}
        for p in P:  # good share is computed from the scores, not typed
            ids = [i for i, n in by.items() if n == p["name"]]
            good = sum(1 for i in ids if self.BY[i]["outcome"] == 1)
            self.assertEqual(p["good_share"], sig.pct(good, len(ids)))
        # five hidden user goals: the starter should find a close match
        gt = self.meta["groups"]
        pure = sum(Counter(gt[i] for i, n in by.items() if n == p["name"]).most_common(1)[0][1] for p in P)
        self.assertGreaterEqual(pure / float(len(self.R)), 0.85)

    def test_no_grader_labels_still_valid(self):
        out = os.path.join(self.tmp, "nolabel")
        shutil.copytree(self.out, out)
        rows = csv_rows(os.path.join(out, "scores.csv"))
        with open(os.path.join(out, "scores.csv"), "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            for r in rows:
                r["persona"] = ""
                w.writerow(r)
        run_sections(out, ("empathy",))
        self.check(os.path.join(out, "study", "sections"), sig.load(out))

    def test_html_has_every_persona_once_in_the_table(self):
        h = read(os.path.join(self.sec, "empathy.html"))
        for p in self.personas():
            self.assertIn("<b>%s</b>" % p["name"], h)


class ImpactTests(Base):
    def test_fixes(self):
        j = json.loads(read(os.path.join(self.sec, "impact.json")))
        fx = j["fixes"]
        self.assertTrue(1 <= len(fx) <= 8)
        short = sum(1 for r in self.R if r["outcome"] != 1)
        self.assertEqual([f["id"] for f in fx], ["F%d" % i for i in range(1, len(fx) + 1)])
        gains = [f["gain_pts"] for f in fx]
        self.assertEqual(gains, sorted(gains, reverse=True))
        self.assertLessEqual(sum(gains), 100.0 * short / len(self.R) + 0.5)
        h = read(os.path.join(self.sec, "impact.html"))
        for f in fx:
            self.assertIn(f["effort"], ("S", "M", "L"))
            self.assertLessEqual(f["addressable"], f["runs"])
            self.assertEqual(f["addressable"], f["bad"] + f["ambiguous"])
            self.assertIn('href="fix_reports/%s.html"' % f["id"], h)
            page = read(os.path.join(self.out, "study", "fix_reports", f["id"] + ".html"))
            body = re.sub(r"<style.*?</style>|<title>.*?</title>", " ", page, flags=re.S)
            self.assertLessEqual(words(re.sub(r"<[^>]+>", " ", body)), 250)
            self.assertIn("--green", page)  # standalone: the shell css is inlined
            self.assertNotIn("<script", page)
            self.assertNotIn("http", page)

    def test_rerun_replaces_reports(self):
        fr = os.path.join(self.out, "study", "fix_reports")
        with open(os.path.join(fr, "F99.html"), "w") as f:
            f.write("old")
        run_sections(self.out, ("impact",))
        self.assertFalse(os.path.exists(os.path.join(fr, "F99.html")))


if __name__ == "__main__":
    unittest.main()
