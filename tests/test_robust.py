"""Hardening tests: dirty-input layers, degradation rules, auto-scaffolded mappings, cleaning ledger (stdlib)."""
import csv
import gzip
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "plugins", "procept-lite", "scripts")
PLITE = os.path.join(SCRIPTS, "plite.py")
sys.path.insert(0, os.path.join(SCRIPTS, "plite"))
import ingest  # noqa: E402
import normalize as N  # noqa: E402


_GATE = {}


def setUpModule():  # these tests exercise the stages themselves, not the sign-off gates (test_gates.py covers those)
    _GATE["old"] = os.environ.get("PLITE_NO_GATE")
    os.environ["PLITE_NO_GATE"] = "1"


def tearDownModule():
    if _GATE.get("old") is None:
        os.environ.pop("PLITE_NO_GATE", None)
    else:
        os.environ["PLITE_NO_GATE"] = _GATE["old"]


def span_rows(n=4, with_time=True, with_tokens=True, tool_result="ok"):
    rows = []
    for i in range(n):
        base = {"trace_id": "t%d" % i}
        if with_time:
            base["start_timestamp"] = "2025-03-01T10:%02d:00Z" % i
            base["duration"] = 2.0
        a = {"gen_ai.agent.call.id": "c%d" % i, "gen_ai.operation.name": "invoke_agent",
             "gen_ai.input.messages": "question %d?" % i, "gen_ai.output.messages": "answer %d" % i}
        rows.append(dict(base, span_id="r%d" % i, span_name="invoke_agent x", attributes=json.dumps(a)))
        t = {"gen_ai.agent.call.id": "c%d" % i, "gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "lookup",
             "gen_ai.tool.call.arguments": '{"q": %d}' % i, "gen_ai.tool.call.result": tool_result}
        rows.append(dict(base, span_id="s%d" % i, parent_span_id="r%d" % i, span_name="execute_tool lookup",
                         attributes=json.dumps(t)))
        if with_tokens:
            m = {"gen_ai.agent.call.id": "c%d" % i, "gen_ai.operation.name": "chat", "gen_ai.request.model": "m",
                 "gen_ai.usage.input_tokens": 10, "gen_ai.usage.output_tokens": 5}
            rows.append(dict(base, span_id="l%d" % i, parent_span_id="r%d" % i, span_name="chat m",
                             attributes=json.dumps(m)))
    return rows


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, name, data):
        p = os.path.join(self.tmp, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data if isinstance(data, bytes) else data.encode("utf-8"))
        return p

    def cli(self, *src):
        out = os.path.join(self.tmp, "out")
        r = subprocess.run([sys.executable, "-I", PLITE, "run", "--src"] + list(src) + ["--out", out],
                           capture_output=True, text=True)
        return r, out

    def runs(self, out):
        with open(os.path.join(out, "normalized", "runs.jsonl"), encoding="utf-8") as f:
            return [json.loads(x) for x in f]

    def report(self, out):
        with open(os.path.join(out, "cleaning_report.md"), encoding="utf-8") as f:
            return f.read()


class DirtyInput(Base):
    def jsonl(self, rows):
        return "\n".join(json.dumps(r) for r in rows)

    def count(self, path):
        r, out = self.cli(path)
        self.assertEqual(r.returncode, 0, r.stderr)
        return len(self.runs(out)), self.report(out)

    def test_encodings_and_containers(self):
        text = self.jsonl(span_rows())
        variants = {
            "bom.jsonl": b"\xef\xbb\xbf" + text.encode(),
            "u16.jsonl": text.encode("utf-16"),
            "u16le.jsonl": text.encode("utf-16-le"),
            "gz.jsonl.gz": gzip.compress(text.encode()),
            "crlf.jsonl": text.replace("\n", "\r\n").encode(),
        }
        for name, data in variants.items():
            with self.subTest(name):
                n, rep = self.count(self.write(name, data))
                self.assertEqual(n, 4)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("a/spans.jsonl", text)
            z.writestr("README.txt", "hello")
        n, rep = self.count(self.write("x.zip", buf.getvalue()))
        self.assertEqual(n, 4)
        self.assertIn("zip_expanded", rep)

    def test_concatenated_truncated_and_double_encoded(self):
        rows = span_rows()
        self.assertEqual(self.count(self.write("cat.json", "".join(json.dumps(r) for r in rows)))[0], 4)
        arr = json.dumps(rows)
        n, rep = self.count(self.write("trunc.json", arr[: len(arr) // 2 + 7]))
        self.assertGreaterEqual(n, 1)
        self.assertIn("array_salvaged", rep)
        n, rep = self.count(self.write("dbl.jsonl", "\n".join(json.dumps(json.dumps(r)) for r in rows)))
        self.assertEqual(n, 4)
        self.assertIn("double_encoded", rep)
        n, rep = self.count(self.write("garb.jsonl", self.jsonl(rows) + '\n{"half": "line'))
        self.assertEqual(n, 4)
        self.assertIn("truncated_tail", rep)

    def test_delimiters_and_bad_quotes(self):
        rows = [{"run_id": "r%d" % i, "question": "why %d, really?" % i, "answer": "because; \"quoted\" | piped"}
                for i in range(5)]
        for name, d in (("c.csv", ","), ("t.tsv", "\t"), ("s.csv", ";"), ("p.csv", "|")):
            buf = io.StringIO()
            w = csv.DictWriter(buf, fieldnames=list(rows[0]), delimiter=d)
            w.writeheader()
            w.writerows(rows)
            with self.subTest(name):
                self.assertEqual(self.count(self.write(name, buf.getvalue()))[0], 5)
        bad = 'run_id,question,answer\nr1,"unterminated, oops,yes\nr2,hi,"yo ""there"\nr3,a,b,c,d\nr4,fine,ok\n'
        r, out = self.cli(self.write("bad.csv", bad))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertGreaterEqual(len(self.runs(out)), 3)

    def test_mixed_folder_ignores_junk_with_a_note(self):
        d = os.path.join(self.tmp, "mix")
        self.write("mix/good.jsonl", self.jsonl(span_rows()))
        self.write("mix/pic.png", b"\x89PNG\r\n\x1a\n\x00\x00\x00")
        self.write("mix/notes.txt", "just some notes\nno data here\n")
        self.write("mix/broken.json", "{not json at all")
        self.write("mix/blob.bin", bytes(range(256)) * 20)
        r, out = self.cli(d)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.runs(out)), 4)
        rep = self.report(out)
        self.assertIn("ignored_extension", rep)

    def test_empty_and_binary_input_fail_with_one_line(self):
        for name, data in (("e.json", b""), ("b.json", bytes(range(256)) * 40), ("n.json", b"null")):
            with self.subTest(name):
                r, out = self.cli(self.write(name, data))
                self.assertEqual(r.returncode, 2)
                self.assertEqual(len(r.stderr.strip().splitlines()), 1, r.stderr)
                self.assertNotIn("Traceback", r.stderr)
        r, _ = self.cli(os.path.join(self.tmp, "does-not-exist"))
        self.assertEqual(r.returncode, 2)
        self.assertEqual(len(r.stderr.strip().splitlines()), 1)


class Degradation(Base):
    def run_rows(self, rows, **kw):
        p = self.write("in.jsonl", "\n".join(json.dumps(r) for r in rows))
        r, out = self.cli(p)
        self.assertEqual(r.returncode, 0, r.stderr)
        return self.runs(out), self.report(out), out

    def test_no_timestamps_drops_latency_with_a_note(self):
        runs, rep, out = self.run_rows(span_rows(with_time=False))
        self.assertTrue(all(r["latency_s"] is None for r in runs))
        self.assertIn("Dimensions dropped", rep)
        self.assertIn("latency", rep)
        with open(os.path.join(out, "normalized", "ingest_stats.json")) as f:
            self.assertFalse(json.load(f)["dimensions"]["latency"])

    def test_missing_tokens_are_estimated_and_flagged(self):
        runs, rep, _ = self.run_rows(span_rows(with_tokens=False))
        for r in runs:
            self.assertTrue(r["extra"].get("tokens_estimated"))
            self.assertGreater(r["input_tokens"] + r["output_tokens"], 0)
        self.assertIn("tokens_estimated", rep)

    def test_errors_derived_from_result_text(self):
        for res in ('{"status": "error", "message": "x"}', "Error: upstream timeout 504", "HTTP 503 service unavailable",
                    "Traceback (most recent call last): boom", '{"ok": false}', '{"isError": true}',
                    '{"statusCode": 500}', "connection refused"):
            with self.subTest(res):
                runs, rep, _ = self.run_rows(span_rows(tool_result=res))
                self.assertEqual(sum(r["n_tool_failures"] for r in runs), 4)
        runs, rep, _ = self.run_rows(span_rows(tool_result="ok: 3 rows, no errors found"))
        self.assertEqual(sum(r["n_tool_failures"] for r in runs), 0)

    def test_no_tool_calls_and_thin_runs(self):
        rows = [{"run_id": "r%d" % i, "question": "q%d?" % i} for i in range(3)] + \
               [{"run_id": "r9", "answer": "orphan answer"}]
        runs, rep, _ = self.run_rows(rows)
        self.assertEqual(len(runs), 4)
        self.assertTrue(all(r["extra"].get("thin") for r in runs))
        self.assertIn("thin_run", rep)

    def test_synthesized_ids_are_stable(self):
        rows = [{"question": "what is %d?" % i, "answer": "it is %d" % i, "timestamp": 1767225600 + 60 * i}
                for i in range(5)]
        a, _, _ = self.run_rows(rows)
        b, _, _ = self.run_rows(rows)
        self.assertEqual([r["run_id"] for r in a], [r["run_id"] for r in b])
        self.assertEqual(len({r["run_id"] for r in a}), 5)
        self.assertTrue(all(r["run_id"].startswith("run-") for r in a))
        self.assertEqual(len({r["conversation_id"] for r in a}), 1)  # no conversation id: one by time gap

    def test_unknown_schema_uses_the_starter_draft(self):
        rows = [{"foo": "run%d" % i, "bar": "question number %d" % i, "baz": "answer number %d" % i, "qux": i * 1.5}
                for i in range(6)]
        runs, rep, out = self.run_rows(rows)
        self.assertEqual(len(runs), 6)
        self.assertFalse(os.path.exists(os.path.join(out, "mapping.json")))
        with open(os.path.join(out, "mapping_rescue", "mapping.draft.json")) as f:
            m = json.load(f)
        self.assertEqual((m["user_text"], m["answer_text"], m["run_id"]), ("bar", "baz", "foo"))
        self.assertIn("auto_mapping", rep)

    def test_unmappable_schema_names_the_fields(self):
        r, out = self.cli(self.write("w.csv", "alpha,beta\n1,2\n3,4\n"))
        self.assertEqual(r.returncode, 2)
        self.assertIn("alpha", r.stderr)
        self.assertIn("user_text", r.stderr)
        self.assertTrue(os.path.isfile(os.path.join(out, "cleaning_report.md")))

    def test_user_mapping_overrides_auto(self):
        rows = [{"foo": "run%d" % i, "bar": "question %d" % i, "baz": "answer %d" % i} for i in range(3)]
        p = self.write("in.jsonl", "\n".join(json.dumps(r) for r in rows))
        m = self.write("map.json", json.dumps({"run_id": "foo", "user_text": "baz", "answer_text": "bar"}))
        out = os.path.join(self.tmp, "out")
        r = subprocess.run([sys.executable, "-I", PLITE, "run", "--src", p, "--map", m, "--out", out],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.runs(out)[0]["user_message"], "answer 0")

    def test_secrets_redacted_and_not_echoed(self):
        rows = span_rows(2)
        for r in rows:
            r["attributes"] = r["attributes"].replace("question 0?", "key sk-ABCDEF1234567890SECRET password=hunter2pw")
        p = self.write("in.jsonl", "\n".join(json.dumps(r) for r in rows))
        r, out = self.cli(p)
        self.assertEqual(r.returncode, 0)
        with open(os.path.join(out, "normalized", "runs.jsonl")) as f:
            blob = f.read()
        self.assertNotIn("ABCDEF1234567890SECRET", blob)
        self.assertNotIn("hunter2pw", blob)
        self.assertNotIn("sk-ABCDEF", r.stdout + r.stderr)

    def test_scored_rerun_builds_cig_even_with_dropped_dimensions(self):
        p = self.write("in.jsonl", "\n".join(json.dumps(r) for r in span_rows(6, with_time=False, with_tokens=False)))
        r, out = self.cli(p)
        runs = self.runs(out)
        with open(os.path.join(out, "scores.csv"), "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["run_id", "outcome_score", "confidence", "rationale", "key_quote", "persona", "issue",
                        "reaction_run_id"])
            for i, x in enumerate(runs):
                w.writerow([x["run_id"], [1, 0, -1][i % 3], "high", "why", "", "analyst", "", ""])
        r, out = self.cli(p)
        self.assertEqual(r.returncode, 0, r.stderr)
        for k in ("cig/flow_graph.json", "cig.html"):
            self.assertTrue(os.path.isfile(os.path.join(out, k)), k)


class Layers(unittest.TestCase):
    def test_decode_and_json_values(self):
        self.assertEqual(ingest.decode_bytes("héllo".encode("cp1252")), "héllo")
        self.assertIsNone(ingest.decode_bytes(b"\x00\x01\x02" * 100))
        stats = {}
        vals = list(ingest.json_values('{"a":1}{"a":2}\n[{"a":3}]\nnot json\n{"a":', stats))
        self.assertEqual([v for v in vals], [{"a": 1}, {"a": 2}, [{"a": 3}]])

    def test_result_error_patterns(self):
        f = N._result_error
        self.assertTrue(f("Error: boom"))
        self.assertTrue(f('{"error": {"message": "bad"}}'))
        self.assertTrue(f("returned HTTP 404 not found"))
        self.assertFalse(f("ok"))
        self.assertFalse(f('{"status": "ok", "error": null}'))
        self.assertFalse(f("0 errors"))
        self.assertFalse(f(""))


if __name__ == "__main__":
    unittest.main()


class SampleTests(unittest.TestCase):
    def test_sample_keeps_whole_conversations_and_is_deterministic(self):
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "plugins", "procept-lite", "scripts"))
        from plite.grade_prepare import sample_runs
        runs = [{"run_id": "r%d" % i, "conversation_id": "c%d" % (i // 3), "agent": "a%d" % (i % 2)} for i in range(60)]
        s1, s2 = sample_runs(runs, 10), sample_runs(runs, 10)
        self.assertEqual(s1, s2)
        self.assertGreaterEqual(len(s1), 10)
        self.assertLess(len(s1), 60)
        for cid in {r["conversation_id"] for r in s1}:
            self.assertEqual(sum(r["conversation_id"] == cid for r in s1), 3)
        self.assertEqual(sample_runs(runs, 0), runs)
