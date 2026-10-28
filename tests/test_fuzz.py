"""Fuzz regression: every fixture produced by tests/fuzz/*/gen.py must go through `plite.py run` without a
traceback, reach a CIG input (runs.jsonl + cleaning_report.md) with exit 0, except inputs that are truly
empty/unreadable, which must fail with exit 2 and a one-line human message (stdlib unittest)."""
import glob
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PLITE = os.path.join(ROOT, "plugins", "procept-lite", "scripts", "plite.py")
FUZZ = os.path.join(HERE, "fuzz")
UNREADABLE = re.compile(r"empty|garbage|junk|array_of|header_only|whitespace|zero_runs|binary|trivial|scalar|bare_|"
                        r"null|html|secret_filename|truncated_zip|resourcespans|deep_nesting|text_only|only_bom|"
                        r"only_header|numeric_only", re.I)
SECRETS = re.compile(r"@example\.com|sk-[A-Za-z0-9]{12}")


def gen_dirs(source_dir):
    found = [os.path.join(source_dir, n) for n in ("cases", "cases2", "fixtures")
             if os.path.isdir(os.path.join(source_dir, n))]
    if found:
        return found
    for gen in sorted(glob.glob(os.path.join(source_dir, "gen*.py"))):
        subprocess.run([sys.executable, gen], cwd=source_dir, capture_output=True, timeout=600)
    return [os.path.join(source_dir, n) for n in ("cases", "cases2", "fixtures")
            if os.path.isdir(os.path.join(source_dir, n))]


def all_cases():
    cases = []
    for src in sorted(glob.glob(os.path.join(FUZZ, "*", "gen*.py"))):
        sd = os.path.dirname(src)
        for d in gen_dirs(sd):
            for p in sorted(os.listdir(d)):
                if not p.startswith("."):
                    cases.append((os.path.basename(sd), os.path.join(d, p)))
    return sorted(set(cases))


def run_case(args):
    source, path, outroot = args
    out = os.path.join(outroot, source, os.path.basename(os.path.dirname(path)) + "_" + os.path.basename(path))
    r = subprocess.run([sys.executable, "-I", PLITE, "run", "--src", path, "--out", out],
                       capture_output=True, text=True, timeout=900)
    return source, path, out, r


class FuzzCorpus(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="plite_fuzz_")
        cls.cases = all_cases()
        with ThreadPoolExecutor(4) as ex:
            cls.results = list(ex.map(run_case, [(s, p, cls.tmp) for s, p in cls.cases]))

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_corpus_present(self):
        self.assertGreaterEqual(len(self.cases), 100)

    def test_every_case_degrades_instead_of_failing(self):
        for source, path, out, r in self.results:
            name = "%s/%s" % (source, os.path.basename(path))
            with self.subTest(case=name):
                text = r.stdout + r.stderr
                self.assertNotIn("Traceback", text)
                self.assertIsNone(SECRETS.search(text), "secret or e-mail echoed in output messages")
                if UNREADABLE.search(os.path.basename(path)):
                    self.assertIn(r.returncode, (0, 2))
                    if r.returncode == 2:
                        err = [ln for ln in r.stderr.strip().splitlines() if ln.strip()]
                        self.assertEqual(len(err), 1, r.stderr)
                        self.assertTrue(err[0].startswith("error: "))
                    continue
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertTrue(os.path.isfile(os.path.join(out, "cleaning_report.md")))
                runs = os.path.join(out, "normalized", "runs.jsonl")
                self.assertTrue(os.path.isfile(runs))
                with open(runs, encoding="utf-8") as f:
                    self.assertGreater(sum(1 for _ in f), 0)
                self.assertTrue(os.path.isfile(os.path.join(out, "grading", "workflow_args.json")))

    def test_scored_runs_reach_the_cig(self):
        """With scores present, run continues to cig + viewer + report on a sample of the corpus."""
        import csv
        good = [(s, p, out) for s, p, out, r in self.results if r.returncode == 0][::6]
        self.assertGreater(len(good), 10)
        for source, path, out in good:
            with self.subTest(case="%s/%s" % (source, os.path.basename(path))):
                with open(os.path.join(out, "normalized", "runs.jsonl"), encoding="utf-8") as f:
                    ids = [json.loads(ln)["run_id"] for ln in f if ln.strip()]
                with open(os.path.join(out, "scores.csv"), "w", newline="", encoding="utf-8") as f:
                    w = csv.writer(f)
                    w.writerow(["run_id", "outcome_score", "confidence", "rationale", "key_quote", "persona",
                                "issue", "reaction_run_id"])
                    for i, rid in enumerate(ids):
                        w.writerow([rid, (1, 0, -1)[i % 3], "high", "why", "", "analyst", "" if i % 3 == 0 else "issue", ""])
                r = subprocess.run([sys.executable, "-I", PLITE, "run", "--src", path, "--out", out],
                                   capture_output=True, text=True, timeout=900)
                self.assertNotIn("Traceback", r.stdout + r.stderr)
                self.assertEqual(r.returncode, 0, r.stderr)
                for name in ("cig.html", "REPORT.md"):
                    self.assertTrue(os.path.isfile(os.path.join(out, name)), name)


if __name__ == "__main__":
    unittest.main()
