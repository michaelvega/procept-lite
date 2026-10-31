import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN = os.path.join(ROOT, "plugins", "procept-lite")
sys.path.insert(0, os.path.join(ROOT, "tests", "fixtures"))
sys.path.insert(0, os.path.join(PLUGIN, "scripts", "plite"))
sys.path.insert(0, os.path.join(PLUGIN, "study"))
os.environ["PLITE_NO_GATE"] = "1"
from make_study_out import make_out  # noqa: E402
import sig  # noqa: E402
import study as study_cli  # noqa: E402
import validate_sections as vs  # noqa: E402

SCRATCH = os.environ.get("TMPDIR") or tempfile.gettempdir()
KEYS = ["eda", "reward", "affinity", "empathy", "impact"]
TITLES = ["Exploratory Data Analysis", "Reward Signal", "Affinity Diagram", "Empathy Map", "Impact"]


def section_html(key, title, extra=""):
    return '<section id="%s"><h2 class="big">%s</h2><p>Short note.</p>%s</section>' % (key, title, extra)


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="study-", dir=SCRATCH)
        cls.out = os.path.join(cls.tmp, "out")
        cls.meta = make_out(cls.out, n=160)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def fresh(self):
        d = tempfile.mkdtemp(prefix="work-", dir=self.tmp)
        out = os.path.join(d, "out")
        shutil.copytree(self.out, out)
        return out

    def populate(self, out):
        """Write valid section files for a copy of the fixture."""
        R = sig.load(out)
        sec = os.path.join(out, "study", "sections")
        os.makedirs(sec, exist_ok=True)
        for k, t in zip(KEYS, TITLES):
            for ext, body in (("py", "# starter\n"), ("md", "# %s\n" % t), ("json", "{}"), ("html", section_html(k, t))):
                with open(os.path.join(sec, "%s.%s" % (k, ext)), "w") as f:
                    f.write(body)
        groups = sorted({self.meta["groups"][r["run_id"]] for r in R})
        names = {g: "%s seeker" % self.meta["group_names"][g].split()[0] for g in groups}
        with open(os.path.join(sec, "personas.csv"), "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["run_id", "persona"])
            for r in R:
                w.writerow([r["run_id"], names[self.meta["groups"][r["run_id"]]]])
        with open(os.path.join(sec, "personas.json"), "w") as f:
            json.dump([{"name": n, "goal": "Get this job done without waiting.", "pain": "Answers miss the point."}
                       for n in names.values()], f)
        with open(os.path.join(sec, "issues.csv"), "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["run_id", "issue", "topic", "theme"])
            for r in R:
                if r["outcome"] != 1:
                    w.writerow([r["run_id"], "wrong answer", "t", "delivered-but-wrong"])
        with open(os.path.join(sec, "impact.json"), "w") as f:
            json.dump({"fixes": [{"id": "f1", "title": "x", "runs": 3}]}, f)
        os.makedirs(os.path.join(out, "study", "fix_reports"), exist_ok=True)
        with open(os.path.join(out, "study", "fix_reports", "f1.html"), "w") as f:
            f.write("<p>report</p>")
        return R, sec


class SigTests(Base):
    def test_load(self):
        R = sig.load(self.out)
        self.assertEqual(len(R), self.meta["n"])
        self.assertEqual([r["first_ts"] for r in R], sorted(r["first_ts"] for r in R))
        ids = {r["run_id"] for r in R}
        for u in self.meta["unusable"] + self.meta["ungraded"]:
            self.assertNotIn(u, ids)
        for k in ("run_id conversation_id agent env day first_ts lat tokens cost_usd n_calls n_fail tool_fail unrecovered "
                  "no_answer slow heavy outcome label conf rationale key_quote persona_raw issue_raw reaction_run_id "
                  "next_ask workflow tools first_ask final_answer").split():
            self.assertIn(k, R[0])
        self.assertTrue(all(r["outcome"] in (1, 0, -1) for r in R))
        self.assertLessEqual(len({r["env"] for r in R}), 5)
        self.assertTrue(any(r["next_ask"] for r in R))

    def test_env_var_and_missing(self):
        os.environ["PLITE_OUT"] = self.out
        try:
            self.assertEqual(len(sig.load()), self.meta["n"])
        finally:
            del os.environ["PLITE_OUT"]
        with self.assertRaises(OSError):
            sig.load(os.path.join(self.tmp, "nope"))

    def test_deterministic(self):
        other = os.path.join(self.tmp, "again")
        make_out(other, n=160)
        for rel in ("scores.csv", "cig/runs.csv", "cig/run_dossiers.jsonl"):
            self.assertEqual(open(os.path.join(self.out, rel)).read(), open(os.path.join(other, rel)).read())


class CliTests(Base):
    def test_init_build_clean(self):
        out = self.fresh()
        self.assertEqual(study_cli.main(["init", "--out", out]), 0)
        st = os.path.join(out, "study")
        self.assertTrue(os.path.isfile(os.path.join(st, ".procept-lite-study")))
        self.assertTrue(os.path.isfile(os.path.join(st, "sig.py")))
        self.assertFalse(os.path.exists(os.path.join(st, "tests")))
        with self.assertRaises(SystemExit):
            study_cli.main(["init", "--out", out])
        self.assertEqual(study_cli.main(["init", "--out", out, "--force"]), 0)
        self.populate(out)
        self.assertEqual(study_cli.main(["build", "--out", out]), 0)
        html = open(os.path.join(st, "study.html"), encoding="utf-8").read()
        self.assertIn("Exploratory Data Analysis", html)
        self.assertNotIn("http://", html.replace("http://www.w3.org", ""))
        self.assertTrue(os.path.isfile(os.path.join(st, "STUDY.md")))
        self.assertEqual(study_cli.main(["clean", "--out", out]), 0)
        self.assertFalse(os.path.exists(st))

    def test_marker_safety(self):
        out = self.fresh()
        st = os.path.join(out, "study")
        os.makedirs(st)
        open(os.path.join(st, "mine.txt"), "w").write("keep")
        with self.assertRaises(SystemExit):
            study_cli.main(["clean", "--out", out])
        with self.assertRaises(SystemExit):
            study_cli.main(["init", "--out", out, "--force"])
        self.assertTrue(os.path.isfile(os.path.join(st, "mine.txt")))

    def test_gate(self):
        out = self.fresh()
        env = dict(os.environ)
        env.pop("PLITE_NO_GATE")
        r = subprocess.run([sys.executable, os.path.join(PLUGIN, "scripts", "plite.py"), "study-init", "--out", out],
                           capture_output=True, text=True, env=env)
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(os.path.exists(os.path.join(out, "study")))

    def test_build_orders_and_skips(self):
        out = self.fresh()
        study_cli.main(["init", "--out", out])
        sec = os.path.join(out, "study", "sections")
        for k, t in (("impact", "Impact"), ("eda", "Exploratory Data Analysis")):
            open(os.path.join(sec, k + ".html"), "w").write(section_html(k, t))
        subprocess.run([sys.executable, "-I", os.path.join(out, "study", "build_study.py"), out], check=True, capture_output=True)
        html = open(os.path.join(out, "study", "study.html"), encoding="utf-8").read()
        self.assertLess(html.index("Exploratory Data Analysis"), html.rindex("Impact"))
        self.assertNotIn("Reward Signal", html)


class ValidateTests(Base):
    def setUp(self):
        self.out = self.fresh()
        self.R, self.sec = self.populate(self.out)

    def problems(self):
        return vs.validate(self.out)

    def has(self, text):
        self.assertTrue(any(text in p for p in self.problems()), "%r not in %r" % (text, self.problems()))

    def edit_personas(self, fn):
        p = os.path.join(self.sec, "personas.json")
        d = json.load(open(p))
        fn(d)
        json.dump(d, open(p, "w"))

    def test_clean(self):
        self.assertEqual(self.problems(), [])

    def test_missing_section(self):
        os.remove(os.path.join(self.sec, "reward.html"))
        self.has("reward")

    def test_blank_and_short(self):
        self.edit_personas(lambda d: d[0].update(goal="", pain="ok"))
        self.has("blank goal")
        self.has("too-short pain")

    def test_cut_off(self):
        self.edit_personas(lambda d: d[0].update(goal="Find a product to buy and then..."))
        self.has("cut off")
        self.edit_personas(lambda d: d[0].update(goal="Find a product to buy and compa"))
        self.has("cut off")

    def test_top_other(self):
        rows = list(csv.DictReader(open(os.path.join(self.sec, "personas.csv"))))
        top = max(set(r["persona"] for r in rows), key=lambda p: sum(r["persona"] == p for r in rows))
        for r in rows:
            if r["persona"] == top:
                r["persona"] = "Other users"
        with open(os.path.join(self.sec, "personas.csv"), "w", newline="") as f:
            w = csv.DictWriter(f, ["run_id", "persona"])
            w.writeheader()
            w.writerows(rows)
        self.has("biggest group")
        self.has("limit 15%")

    def test_csv_coverage(self):
        p = os.path.join(self.sec, "personas.csv")
        lines = open(p).read().splitlines()
        open(p, "w").write("\n".join(lines[:-3]) + "\n")
        self.has("have no persona")

    def test_issues_missing(self):
        p = os.path.join(self.sec, "issues.csv")
        lines = open(p).read().splitlines()
        open(p, "w").write("\n".join(lines[:-2]) + "\n")
        self.has("have no issue")

    def test_jargon(self):
        bad = "Co" + "hen's ka" + "ppa and AU" + "C"
        open(os.path.join(self.sec, "eda.html"), "w").write(section_html("eda", TITLES[0], "<p>%s</p>" % bad))
        self.has("jargon")

    def test_prose_budget(self):
        open(os.path.join(self.sec, "eda.html"), "w").write(
            section_html("eda", TITLES[0], "<p>%s</p>" % " ".join(["word"] * 200)))
        self.has("budget")
        open(os.path.join(self.sec, "eda.html"), "w").write(
            section_html("eda", TITLES[0], "<table><tr><td>%s</td></tr></table>" % " ".join(["word"] * 300)))
        self.assertEqual(self.problems(), [])

    def test_cli_exit_code(self):
        v = os.path.join(PLUGIN, "study", "validate_sections.py")
        self.assertEqual(subprocess.run([sys.executable, "-I", v, self.out]).returncode, 0)
        os.remove(os.path.join(self.sec, "impact.html"))
        self.assertEqual(subprocess.run([sys.executable, "-I", v, self.out], capture_output=True).returncode, 1)


class CandidatesTests(Base):
    def test_candidates(self):
        out = self.fresh()
        self.populate(out)
        pc = os.path.join(PLUGIN, "study", "problem_candidates.py")
        r = subprocess.run([sys.executable, "-I", pc, out], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        d = json.load(open(os.path.join(out, "scorecard_inputs", "problem_candidates.json")))
        self.assertFalse(os.path.exists(os.path.join(out, "scorecard_inputs", "user_goal_problem.json")))
        self.assertIn("skeleton", json.dumps(d).lower())
        self.assertLessEqual(len(json.dumps(d)), 200000)


class CutOffText(unittest.TestCase):
    def test_unbalanced_brackets_and_quotes_are_cut_off(self):
        self.assertTrue(vs.cut_off("Clarifying questions (which stores."))
        self.assertTrue(vs.cut_off('Get help with "a request.'))
        self.assertFalse(vs.cut_off("Clarifying questions (which stores) block them."))


if __name__ == "__main__":
    unittest.main()
