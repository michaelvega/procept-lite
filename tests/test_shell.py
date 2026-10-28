import json
import os
import re
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN = os.path.join(ROOT, "plugins", "procept-lite")
PLITE = os.path.join(PLUGIN, "scripts", "plite.py")


def load(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return json.load(f)


class ManifestTests(unittest.TestCase):
    def test_marketplace(self):
        m = load(".claude-plugin", "marketplace.json")
        self.assertEqual(m["name"], "procept-marketplace")
        self.assertIn("owner", m)
        p = m["plugins"][0]
        self.assertEqual(p["name"], "procept-lite")
        self.assertEqual(p["source"], "./plugins/procept-lite")
        self.assertTrue(os.path.isdir(os.path.join(ROOT, p["source"])))

    def test_plugin(self):
        p = load("plugins", "procept-lite", ".claude-plugin", "plugin.json")
        self.assertEqual(p["name"], "procept-lite")
        self.assertEqual(p["version"], "0.1.0")
        self.assertTrue(p["description"])

    def test_skill_and_commands(self):
        text = open(os.path.join(PLUGIN, "skills", "procept-lite", "SKILL.md"), encoding="utf-8").read()
        self.assertTrue(text.startswith("---\nname: procept-lite\n"))
        self.assertIn("description:", text.split("---")[1])
        self.assertLess(len(text.splitlines()), 200)
        for c in ("start", "check", "grade", "build", "report"):
            cmd = open(os.path.join(PLUGIN, "commands", c + ".md"), encoding="utf-8").read()
            self.assertTrue(cmd.startswith("---\n"))
            self.assertIn("description:", cmd)


class DispatcherTests(unittest.TestCase):
    def test_help_lists_commands(self):
        r = subprocess.run([sys.executable, PLITE, "--help"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)
        for c in ("inspect", "normalize", "grade-prepare", "grade-merge", "cig", "viewer", "report", "run"):
            self.assertIn(c, r.stdout)

    def test_unknown_command(self):
        r = subprocess.run([sys.executable, PLITE, "nope"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)


class NoCouplingTests(unittest.TestCase):
    def test_no_foreign_names_or_absolute_paths(self):
        bad = re.compile(r"/Users/|/home/[a-z]|[A-Z]:\\Users", re.I)
        hits = []
        for d, _, files in os.walk(PLUGIN):
            if "__pycache__" in d:
                continue
            for f in files:
                path = os.path.join(d, f)
                try:
                    text = open(path, encoding="utf-8").read()
                except (UnicodeDecodeError, OSError):
                    continue
                if bad.search(text):
                    hits.append(os.path.relpath(path, ROOT))
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
