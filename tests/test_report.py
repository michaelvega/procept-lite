import contextlib
import csv
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from html.parser import HTMLParser
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN = os.path.join(ROOT, "plugins", "procept-lite")
sys.path.insert(0, os.path.join(PLUGIN, "scripts"))

from plite import checks, estimates, report, scorecard  # noqa: E402

MINUS, ENDASH, NBSP = chr(0x2212), chr(0x2013), chr(0xA0)
KAPPA, COHEN = "ka" + "ppa", "Co" + "hen"   # removed from the product: only the lint hunts for them
RUN_COLS = ["run_id", "conversation_id", "agent", "environment", "first_ts", "latency_s",
            "input_tokens", "output_tokens", "tokens", "cost_usd", "n_tool_calls",
            "n_tool_failures", "tool_fail", "unrecovered", "no_answer", "slow", "heavy",
            "outcome_score", "label", "persona", "workflow"]
SCORE_COLS = ["run_id", "outcome_score", "confidence", "rationale", "key_quote", "persona", "issue",
              "reaction_run_id"]
LABEL = {1: "good", 0: "ambiguous", -1: "bad", None: "ungraded"}


def write_cig(out, rows, error_counts=(), dossier_text=None):
    """cig/ + scores.csv for hand-made runs. Each row: score (1, 0, -1 or None), tools, fails, tokens,
    cost (or None), latency (or None), day (YYYY-MM-DD)."""
    os.makedirs(os.path.join(out, "cig"), exist_ok=True)
    runs, scores, dossiers = [], [], []
    for i, r in enumerate(rows):
        rid = r.get("id", "r%d" % i)
        s = r.get("score")
        runs.append({"run_id": rid, "conversation_id": "c%d" % (i // 2), "agent": "", "environment": "",
                     "first_ts": "%sT10:%02d:00Z" % (r.get("day", "2026-03-04"), i % 60),
                     "latency_s": "" if r.get("latency") is None else r["latency"],
                     "input_tokens": r.get("tokens", 0), "output_tokens": 0, "tokens": r.get("tokens", 0),
                     "cost_usd": "" if r.get("cost") is None else r["cost"],
                     "n_tool_calls": r.get("tools", 0), "n_tool_failures": r.get("fails", 0),
                     "tool_fail": int(bool(r.get("fails"))), "unrecovered": 0, "no_answer": 0, "slow": 0,
                     "heavy": 0, "outcome_score": "" if s is None else s, "label": LABEL[s],
                     "persona": "grader label", "workflow": "search"})
        if s is not None:
            scores.append({"run_id": rid, "outcome_score": s, "persona": "grader label", "issue": "x"})
        ask = dossier_text or "show my open tickets for project %d"
        dossiers.append({"run_id": rid, "user_message": ask % i if "%d" in ask else ask,
                         "final_answer": "Here are your tickets.", "tool_calls": [], "errors": []})
    with open(os.path.join(out, "cig", "runs.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, RUN_COLS)
        w.writeheader()
        w.writerows(runs)
    with open(os.path.join(out, "scores.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, SCORE_COLS, restval="")
        w.writeheader()
        w.writerows(scores)
    with open(os.path.join(out, "cig", "run_dossiers.jsonl"), "w") as f:
        f.write("\n".join(json.dumps(d) for d in dossiers) + "\n")
    with open(os.path.join(out, "cig", "errors.csv"), "w", newline="") as f:
        f.write("error_class,count,runs,tool,example\n")
        for k, n in enumerate(error_counts):
            f.write("class %d,%d,1,search,example\n" % (k, n))


def make_cig(out, n=40, cost=False):
    """40 runs: i%5==0 bad, i%5==1 ambiguous, the rest good; three failed calls in bad runs, one in ambiguous
    ones; the last four runs are slow; costs in dollars only when asked."""
    rows = []
    for i in range(n):
        score = -1 if i % 5 == 0 else (0 if i % 5 == 1 else 1)
        tools = 12 if i % 10 == 0 else 3 + i % 4
        rows.append({"score": score, "tools": tools, "fails": {-1: 3, 0: 1, 1: 0}[score],
                     "tokens": 1200 + 100 * tools, "cost": (1200 + 100 * tools) * 1e-5 if cost else None,
                     "latency": 5 + i * (3 if i > n - 5 else 0.2), "day": "2026-03-%02d" % (4 + i % 3)})
    write_cig(out, rows, error_counts=(6, 3))


PROBLEM = {
    "persona": "Support lead",
    "goal": "Find every open ticket for one project and see who owns each of them.",
    "problem": "The assistant answers with tickets from other projects, so the list cannot be trusted.",
    "headline": "This support lead wants every open ticket for one project but cannot get it, because the "
                "assistant mixes in tickets from other projects.",
    "quote": "show my open tickets for project 3", "quote_run_id": "r3", "model": "claude-sonnet-5-5",
    "run_ids": ["r%d" % i for i in range(20)]}


def write_problem(out, **over):
    d = dict(PROBLEM, **over)
    d = {k: v for k, v in d.items() if v is not None}
    os.makedirs(os.path.join(out, "scorecard_inputs"), exist_ok=True)
    with open(os.path.join(out, report.PROBLEM_FILE), "w") as f:
        json.dump(d, f)


def make_study(out):
    os.makedirs(os.path.join(out, "study", "sections"))
    for rel in (".procept-lite-study", os.path.join("sections", "eda.html")):
        with open(os.path.join(out, "study", rel), "w") as f:
            f.write("x")


class Text(HTMLParser):
    """The visible text of a page, whitespace and no-break spaces folded."""

    def __init__(self, page):
        HTMLParser.__init__(self, convert_charrefs=True)
        self.parts, self.skip = [], 0
        self.feed(page)

    def handle_starttag(self, tag, attrs):
        self.skip += tag in ("style", "script")

    def handle_endtag(self, tag):
        self.skip -= tag in ("style", "script")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)

    def flat(self):
        return re.sub(r"\s+", "", "".join(self.parts))

    def spaced(self):
        return " ".join(" ".join(self.parts).replace(NBSP, " ").split())


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        env = mock.patch.dict(os.environ, {"PLITE_NO_GATE": "1"})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)

    def standard(self, **kw):
        make_cig(self.d, **kw)
        write_problem(self.d)

    def build(self, **kw):
        with open(report.build(self.d, **kw), encoding="utf-8") as f:
            return f.read()


class FormatTests(unittest.TestCase):
    def test_percent(self):
        f = report.fmt_pct
        self.assertEqual([f(0), f(0.004), f(0.05), f(3.04), f(7.2), f(10), f(47.6), f(99.7), f(100)],
                         ["0%", "<0.01%", "0.05%", "3%", "7.2%", "10%", "48%", "99.7%", "100%"])
        self.assertEqual(f(5.0), "5%")
        self.assertNotIn("100%", f(99.51))

    def test_dollars(self):
        f = report.fmt_usd
        self.assertEqual([f(1234.5), f(20.9564), f(1.5), f(0.1704), f(0.0852), f(0.0043), f(0.5), f(0)],
                         ["$1,234", "$20.96", "$1.50", "$0.17", "$0.085", "$0.0043", "$0.50", "$0"])

    def test_tokens_k_and_m(self):
        f = report.fmt_tokens
        self.assertEqual([f(950), f(9500), f(662000), f(1432857), f(123750180), f(999700)],
                         ["950", "9.5k", "662k", "1.4M", "124M", "1M"])

    def test_seconds_units(self):
        f = report.fmt_seconds
        self.assertEqual([f(0.04), f(7.2), f(10), f(79.04), f(150), f(7200)],
                         [("0.04", "s"), ("7.2", "s"), ("10", "s"), ("79", "s"), ("2.5", "min"), ("2", "h")])

    def test_delta(self):
        self.assertEqual(report.fmt_delta(28.5), "+28.5%")
        self.assertEqual(report.fmt_delta(-50.0), MINUS + "50%")
        self.assertEqual(report.fmt_delta(-5.4945), MINUS + "5.5%")
        self.assertEqual(report.fmt_delta(0.0), "no change")

    def test_dates(self):
        f = report.fmt_dates
        self.assertEqual(f("2026-10-26", "2026-10-26"), "26 Oct 2026")
        self.assertEqual(f("2026-10-26", "2026-10-27"), "26" + ENDASH + "27 Oct 2026")
        self.assertEqual(f("2026-10-28", "2026-11-03"), "28 Oct " + ENDASH + " 3 Nov 2026")
        self.assertEqual(f("2026-12-30", "2027-01-02"), "30 Dec 2026 " + ENDASH + " 2 Jan 2027")
        self.assertEqual(f("nope", "2026-10-26"), "")

    def test_shares_add_up_to_100(self):
        self.assertEqual(sum(report.round_shares([1, 1, 1])), 100)
        self.assertEqual(report.round_shares([89, 66, 32]), [48, 35, 17])
        self.assertEqual(report.round_shares([0, 0, 0]), [0, 0, 0])

    def test_plural(self):
        self.assertEqual(report.plural(1, "run"), "1 run")
        self.assertEqual(report.plural(1234, "run"), "1,234 runs")


class ClaimsMathTests(Base):
    """Every expected number below is worked out by hand in the comments."""

    def ten(self, **per_run):
        # r0..r9; 6 good, 2 ambiguous, 2 bad. 5 tool calls each (50 calls); failures 3+3+2+2 = 10 in r6..r9
        # latency 1..10 s; tokens 1000*(i+1) (mean 5500); dollars 0.1*(i+1) (mean 0.55)
        rows = []
        for i in range(10):
            rows.append({"score": 1 if i < 6 else (0 if i < 8 else -1), "tools": 5,
                         "fails": {6: 3, 7: 3, 8: 2, 9: 2}.get(i, 0), "tokens": 1000 * (i + 1),
                         "cost": 0.1 * (i + 1) if per_run.get("usd") else None, "latency": float(i + 1)})
        write_cig(self.d, rows, error_counts=per_run.get("classes", (4, 3, 2, 1)))
        return scorecard.compute(self.d), estimates.load_claims(self.d)

    def test_goals_are_relative_to_the_good_share(self):
        sc, claims = self.ten()
        g = estimates.goals(sc, claims)
        self.assertAlmostEqual(g["x"], 60.0)          # 6 of 10
        self.assertAlmostEqual(g["y"], 77.1)          # 60 * 1.285
        self.assertAlmostEqual(g["delta_pct"], 28.5)

    def test_goals_never_pass_100(self):
        rows = [{"score": 1 if i < 9 else 0, "tools": 1} for i in range(10)]
        write_cig(self.d, rows)
        g = estimates.goals(scorecard.compute(self.d), estimates.load_claims(self.d))
        self.assertAlmostEqual(g["x"], 90.0)
        self.assertEqual(g["y"], 100.0)               # 90 * 1.285 = 115.65, capped
        self.assertAlmostEqual(g["delta_pct"], 100.0 / 90.0 * 100 - 100)

    def test_cost_in_tokens_when_there_are_no_dollars(self):
        sc, claims = self.ten()
        c = estimates.cost(sc, claims)
        self.assertEqual((c["unit"], c["x"], c["y"], c["delta_pct"]), ("tokens", 5500.0, 2750.0, -50.0))

    def test_cost_in_dollars_when_costs_exist(self):
        sc, claims = self.ten(usd=True)
        c = estimates.cost(sc, claims)
        self.assertEqual(c["unit"], "usd")
        self.assertAlmostEqual(c["x"], 0.55)          # mean of 0.1 .. 1.0
        self.assertAlmostEqual(c["y"], 0.275)
        self.assertAlmostEqual(c["total"], 5.5)

    def test_cost_falls_back_to_tool_calls(self):
        write_cig(self.d, [{"score": 1, "tools": 4, "latency": 1.0}, {"score": 0, "tools": 6, "latency": 2.0}])
        sc = scorecard.compute(self.d)
        c = estimates.cost(sc, estimates.load_claims(self.d))
        self.assertEqual((c["unit"], c["x"], c["y"]), ("calls", 5.0, 2.5))

    def test_partial_dollars_below_half_the_runs_do_not_count(self):
        rows = [{"score": 1, "tools": 1, "tokens": 100, "cost": 1.0 if i < 2 else None} for i in range(10)]
        write_cig(self.d, rows)
        self.assertEqual(scorecard.compute(self.d)["unit"], "tokens")

    def test_errors_are_half_of_what_the_top_three_classes_account_for(self):
        sc, _ = self.ten()
        e = estimates.errors(sc)
        self.assertEqual((e["calls"], e["fails"], e["supported"]), (50, 10, 9.0))   # 4 + 3 + 2
        self.assertAlmostEqual(e["x"], 20.0)                                       # 10 / 50
        self.assertAlmostEqual(e["y"], 11.0)                                       # (10 - 4.5) / 50
        self.assertAlmostEqual(e["delta_pct"], -45.0)

    def test_errors_support_is_capped_at_the_failures_there_are(self):
        sc, _ = self.ten(classes=(40, 30, 20, 10))       # classes also count run-level errors: cap at 10
        e = estimates.errors(sc)
        self.assertEqual(e["supported"], 10.0)
        self.assertAlmostEqual(e["y"], 10.0)                                       # (10 - 5) / 50
        self.assertAlmostEqual(e["delta_pct"], -50.0)

    def test_errors_without_classes_promise_nothing(self):
        sc, _ = self.ten(classes=())
        e = estimates.errors(sc)
        self.assertEqual(e["y"], e["x"])

    def test_latency_moves_half_way_to_a_tail_at_the_median(self):
        sc, _ = self.ten()
        la = estimates.latency(sc)
        # latencies 1..10: median 5.5, p90 = 9 + 0.1 = 9.1; the run above p90 (10 s) taken to 5.5 gives
        # 1,2,3,4,5,5.5,6,7,8,9 whose p90 is 8 + 0.1 = 8.1; so y = 9.1 - (9.1 - 8.1) / 2 = 8.6
        self.assertAlmostEqual(la["median"], 5.5)
        self.assertAlmostEqual(la["x"], 9.1)
        self.assertAlmostEqual(la["p90_full"], 8.1)
        self.assertAlmostEqual(la["y"], 8.6)
        self.assertAlmostEqual(la["delta_pct"], (8.6 - 9.1) / 9.1 * 100)

    def test_flat_latency_has_nothing_to_remove(self):
        write_cig(self.d, [{"score": 1, "tools": 1, "latency": 7.0} for _ in range(10)])
        la = estimates.latency(scorecard.compute(self.d))
        self.assertEqual((la["x"], la["y"], la["delta_pct"]), (7.0, 7.0, 0.0))

    def test_rows_come_in_the_page_order(self):
        sc, claims = self.ten()
        rows = estimates.system5(sc, claims)
        self.assertEqual([r["key"] for r in rows], ["goals", "cost", "errors", "latency"])
        self.assertEqual([r["unit"] for r in rows], ["pct", "tokens", "pct", "s"])

    def test_missing_data_is_an_error_not_a_blank(self):
        write_cig(self.d, [{"score": 1, "tools": 0, "latency": None} for _ in range(5)])
        sc = scorecard.compute(self.d)
        for fn in (estimates.errors, estimates.latency):
            with self.assertRaises(ValueError):
                fn(sc)
        with self.assertRaises(ValueError):
            estimates.goals(dict(sc, n_graded=0), estimates.load_claims(self.d))

    def test_claims_precedence(self):
        self.assertEqual(estimates.load_claims(self.d)["accuracy_gain_pct"], 49)   # the shipped template
        with open(os.path.join(self.d, "system5.json"), "w") as f:
            json.dump({"accuracy_gain_pct": 51, "url": "https://example.test/s5", "goal_gain_pct": 10}, f)
        c = estimates.load_claims(self.d)
        self.assertEqual((c["accuracy_gain_pct"], c["url"], c["goal_gain_pct"], c["cost_cut_pct"]),
                         (51, "https://example.test/s5", 10, 50))

    def test_claims_are_validated(self):
        for bad in ({"accuracy_gain_pct": "lots"}, {"cost_cut_pct": 150}, {"url": "javascript:alert(1)"},
                    {"errors": "aggressive"}):
            with open(os.path.join(self.d, "system5.json"), "w") as f:
                json.dump(bad, f)
            with self.assertRaises(ValueError):
                estimates.load_claims(self.d)


class UsableRunsTests(Base):
    def test_unusable_runs_are_in_no_number(self):
        make_cig(self.d)
        before = scorecard.compute(self.d)
        os.makedirs(os.path.join(self.d, "normalized"))
        with open(os.path.join(self.d, "normalized", "unusable.csv"), "w") as f:
            f.write("run_id,reason,source\nr0,no_ask,auto\nr1,wrong text,model\n")   # a bad and an ambiguous run
        after = scorecard.compute(self.d)
        self.assertEqual((before["n"], after["n"]), (40, 38))
        self.assertEqual((before["split"]["bad"], after["split"]["bad"]), (8, 7))
        self.assertEqual((before["split"]["ambiguous"], after["split"]["ambiguous"]), (8, 7))
        self.assertNotIn("r0", {r["run_id"] for r in after["runs"]})
        self.assertEqual(after["all"]["calls"], before["all"]["calls"] - 12 - 4)   # r0 has 12 calls, r1 has 4

    def test_unusable_runs_cannot_be_in_the_problem_group(self):
        make_cig(self.d)
        write_problem(self.d)
        os.makedirs(os.path.join(self.d, "normalized"))
        with open(os.path.join(self.d, "normalized", "unusable.csv"), "w") as f:
            f.write("run_id,reason,source\nr7,no_ask,auto\n")
        with self.assertRaisesRegex(report.ProblemError, "not graded, usable"):
            report.validate_problem(self.d)

    def test_outcome_split_covers_graded_runs_only(self):
        rows = [{"score": 1, "tools": 2, "fails": 0, "latency": 4.0}, {"score": -1, "tools": 2, "fails": 2, "latency": 8.0},
                {"score": None, "tools": 6, "fails": 6, "latency": 40.0}]
        write_cig(self.d, rows)
        sc = scorecard.compute(self.d)
        self.assertEqual((sc["n"], sc["n_graded"]), (3, 2))
        self.assertEqual(sc["by"]["good"]["calls"] + sc["by"]["bad"]["calls"], 4)    # the ungraded run is not in it
        self.assertEqual(sc["all"]["calls"], 10)                                      # but it is in the headline
        self.assertEqual(estimates.goals(sc, estimates.load_claims(self.d))["x"], 50.0)   # 1 of 2 graded

    def test_scores_csv_wins_over_the_cig_column(self):
        make_cig(self.d)
        rows = list(csv.DictReader(open(os.path.join(self.d, "scores.csv"))))
        rows[2]["outcome_score"] = "-1"                      # r2 was good
        with open(os.path.join(self.d, "scores.csv"), "w", newline="") as f:
            w = csv.DictWriter(f, SCORE_COLS, restval="")
            w.writeheader()
            w.writerows(rows)
        self.assertEqual(scorecard.compute(self.d)["split"]["bad"], 9)

    def test_garbage_cells_do_not_become_numbers(self):
        self.assertEqual(scorecard.num("nan", 7.0), 7.0)
        self.assertEqual(scorecard.num("inf", 7.0), 7.0)
        self.assertEqual(scorecard.num("", 7.0), 7.0)
        self.assertEqual(scorecard.num("2.5"), 2.5)


class ProblemTests(Base):
    def setUp(self):
        Base.setUp(self)
        make_cig(self.d)

    def check(self, pattern, **over):
        write_problem(self.d, **over)
        with self.assertRaisesRegex(report.ProblemError, pattern):
            report.validate_problem(self.d)

    def test_valid_problem_gets_n_and_fell_short_computed_here(self):
        write_problem(self.d, run_ids=["r%d" % i for i in range(20)], n=999, fell_short_pct=1)
        p = report.validate_problem(self.d)
        # r0..r19: bad at 0,5,10,15; ambiguous at 1,6,11,16 -> 8 of 20 fell short
        self.assertEqual((p["n"], p["fell_short_n"], p["split"]), (20, 8, {"good": 12, "ambiguous": 4, "bad": 4}))
        self.assertAlmostEqual(p["fell_short_pct"], 40.0)

    def test_missing_file_says_to_run_the_study_step(self):
        with self.assertRaisesRegex(report.ProblemError, "missing.*study"):
            report.validate_problem(self.d)

    def test_invalid_json(self):
        os.makedirs(os.path.join(self.d, "scorecard_inputs"))
        with open(os.path.join(self.d, report.PROBLEM_FILE), "w") as f:
            f.write("{not json")
        with self.assertRaisesRegex(report.ProblemError, "not valid JSON"):
            report.validate_problem(self.d)

    def test_blank_fields_are_refused(self):
        for k in ("persona", "goal", "problem", "headline", "quote", "quote_run_id", "model"):
            self.check("'%s' is missing or blank" % k, **{k: "   "})
        self.check("'goal' is missing or blank", goal=None)
        self.check("'run_ids' must be a list", run_ids="r1")

    def test_cut_off_text_is_refused(self):
        self.check("does not end with", headline="This support lead wants every open ticket for one project but can't because")
        self.check("does not end with", goal="Find every open ticket for one project and see who owns")
        self.check("cut-off text", problem="The assistant answers with tickets from other projects, so the list...")
        self.check("ends on 'the'", problem="The assistant answers with tickets from other projects and then the.")
        self.check("unclosed bracket", problem="The assistant answers with tickets (from other projects, so the list is wrong.")
        self.check("has 3 words", headline="Too short here.")

    def test_junk_is_refused(self):
        self.check("too generic", persona="Other")
        self.check("unclear", persona="Unclear ask")
        self.check("filler", goal="blah blah blah blah blah blah blah blah.")
        self.check("template brackets", headline="This <b>support lead</b> wants every open ticket but cannot get one today.")
        self.check("unknown", problem="The assistant answers with unknown tickets from other projects, so the list is wrong.")
        self.check("none", goal="Find every open ticket for one project and see that none of them is lost.")

    def test_fake_quote_is_refused(self):
        self.check("not found verbatim", quote="show me everything about project 3")
        self.check("not found verbatim", quote="show my open tickets for project 4")   # real text, wrong run
        self.check("at most 160", quote="show my open tickets for project 3 " + "x " * 90)

    def test_quote_with_an_email_or_a_secret_is_refused(self):
        self.check("e-mail address or a secret", quote="mail me at ann@example.com about project 3")
        self.check("e-mail address or a secret", quote="use api_key=abcd1234efgh5678 for project 3")

    def test_quote_can_hold_any_character_and_comes_from_the_conversation_only(self):
        rows = [{"score": 1 if i % 2 else -1, "tools": 1} for i in range(8)]
        write_cig(self.d, rows, dossier_text="price > 500k & <b>rent</b> {for} project %d")
        ids = ["r%d" % i for i in range(8)]
        write_problem(self.d, run_ids=ids, quote="price > 500k & <b>rent</b> {for} project 3")
        self.assertEqual(report.validate_problem(self.d)["n"], 8)
        # the grader's own words are not part of the conversation
        with open(os.path.join(self.d, "cig", "run_dossiers.jsonl"), "w") as f:
            f.write(json.dumps({"run_id": "r3", "user_message": "x", "rationale": "the user asked about rent prices"}) + "\n")
        self.check("not found verbatim", run_ids=ids, quote="the user asked about rent prices")

    def test_unknown_run_ids_are_refused(self):
        self.check("not graded, usable runs", run_ids=["r%d" % i for i in range(19)] + ["r999"])
        self.check("must be one of", quote_run_id="r30")             # a real run, but not in the group

    def test_ungraded_runs_are_refused(self):
        rows = [{"score": 1 if i % 2 else -1, "tools": 1} for i in range(8)] + [{"score": None, "tools": 1}]
        write_cig(self.d, rows)
        self.check("not graded, usable runs", run_ids=["r%d" % i for i in range(9)], quote_run_id="r3")

    def test_too_few_runs_are_refused(self):
        self.check("at least 5", run_ids=["r0", "r1", "r2", "r3"])

    def test_tiny_datasets_may_use_all_their_runs(self):
        write_cig(self.d, [{"score": -1, "tools": 1}, {"score": 1, "tools": 1}, {"score": 0, "tools": 1}])
        write_problem(self.d, run_ids=["r0", "r1", "r2"], quote_run_id="r2", quote="show my open tickets for project 2")
        self.assertEqual(report.validate_problem(self.d)["n"], 3)

    def test_a_group_where_nothing_fell_short_is_no_problem(self):
        self.check("fell short", run_ids=["r2", "r3", "r4", "r7", "r8", "r9"], quote_run_id="r3")

    def test_model_floor(self):
        with mock.patch.dict(os.environ, {"PLITE_NO_GATE": ""}):
            for bad in ("claude-haiku-4-5", "gpt-5-mini", "tiny"):
                self.check("below the floor", model=bad)
            write_problem(self.d, model="claude-sonnet-5-5")
            self.assertEqual(report.validate_problem(self.d)["model"], "claude-sonnet-5-5")

    def test_the_error_works_for_both_kinds_of_callers(self):
        self.assertTrue(issubclass(report.ProblemError, SystemExit))
        self.assertTrue(issubclass(report.ProblemError, ValueError))

    def test_cli_validate_only(self):
        write_problem(self.d)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(report.main(["--out", self.d, "--validate-problem"]), 0)
        self.assertIn("ok: Support lead (20 runs, 40% fell short)", buf.getvalue())
        self.assertFalse(os.path.exists(os.path.join(self.d, "scorecard.html")))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            write_problem(self.d, goal=" ")
            self.assertEqual(report.main(["--out", self.d, "--validate-problem"]), 2)
        self.assertIn("'goal' is missing or blank", err.getvalue())


class LintTests(unittest.TestCase):
    PAGE = ('<html><head><style>.a{display:none}@media print{.a{text-decoration:none}}</style></head><body>'
            '<p class="pb-headline">h</p><p class="pb-goal">g</p><p class="pb-problem">p</p>%s</body></html>')

    def hits(self, extra):
        return report.lint_hits(self.PAGE % extra)

    def test_a_clean_page_passes_and_css_may_say_none(self):
        self.assertEqual(self.hits("<p>All good, $0.085 and 7.2% here.</p>"), [])
        report.lint_customer_html(self.PAGE % "<p>fine</p>")

    def test_forbidden_wording_is_found(self):
        for word in (COHEN + "'s " + KAPPA, KAPPA, "unclear ask", "Unknown", "N/A", "NaN", "None", "TODO", "placeholder text",
                     "lorem ipsum", "we repaired it", "Sorry about that", "apologies", "a mislabelled run",
                     "an unusable run", "12 runs were dropped", "cut off...", "left over {{slot}}", "a $title here",
                     "7.2000001%", "see scores.csv", "undefined"):
            self.assertTrue(self.hits("<p>%s</p>" % word), word)

    def test_hits_in_titles_labels_and_comments_count(self):
        self.assertTrue(self.hits('<svg aria-label="%s"></svg>' % KAPPA))
        self.assertTrue(self.hits('<p title="n/a">x</p>'))
        self.assertTrue(self.hits("<!-- TODO -->"))

    def test_words_that_only_contain_a_forbidden_word_pass(self):
        self.assertEqual(self.hits("<p>nonetheless, banana, finance, unknowing</p>"), [])

    def test_blank_table_cells(self):
        self.assertTrue(self.hits("<table><tr><td></td></tr></table>"))
        self.assertTrue(self.hits("<table><tr><th> &nbsp; </th></tr></table>"))
        self.assertEqual(self.hits("<table><tr><td>1</td><td><svg></svg></td></tr></table>"), [])

    def test_empty_or_missing_headline_goal_problem(self):
        page = self.PAGE.replace('<p class="pb-goal">g</p>', '<p class="pb-goal">  </p>')
        self.assertIn("empty element with class pb-goal", report.lint_hits(page % ""))
        page = self.PAGE.replace('<p class="pb-problem">p</p>', "")
        self.assertIn("missing element with class pb-problem", report.lint_hits(page % ""))

    def test_a_hit_fails_the_build(self):
        with self.assertRaises(SystemExit) as cm:
            report.lint_customer_html(self.PAGE % ("<p>%s</p>" % KAPPA))
        self.assertIn(KAPPA, str(cm.exception))

    def test_unfilled_slots_stay_visible_to_the_lint(self):
        page = report.fill("<p>{{known}} {{nope}}</p>", {"known": "ok"})
        self.assertEqual(page, "<p>ok {{nope}}</p>")
        self.assertTrue(report.lint_hits(page))


class PageTests(Base):
    def setUp(self):
        Base.setUp(self)
        self.standard(cost=True)
        self.page = self.build()
        self.text = Text(self.page)
        self.data = json.load(open(os.path.join(self.d, "scorecard.json")))

    def test_sections_in_the_agreed_order(self):
        marks = ["Errors, resource spend and latency are objective metrics.", 'class="tiles"',
                 "Implementing System 5 gives you", "Production data is", 'class="cta"',
                 "More info", "User goal problem we found", 'class="charts"']
        pos = [self.page.index(m) for m in marks]
        self.assertEqual(pos, sorted(pos))
        self.assertLess(self.page.index("<h1>"), pos[0])
        self.assertLess(self.page.index('class="kicker"'), self.page.index("<h1>"))

    def test_header_copy(self):
        self.assertIn("<h1>Agent Run Scorecard</h1>", self.page)
        self.assertIn("<title>Agent Run Scorecard</title>", self.page)
        self.assertIn("40 runs graded · 4" + ENDASH + "6 Mar 2026", self.text.spaced())
        sub = ("Errors, resource spend and latency are objective metrics. The outcome signal is derived through an "
               "LLM judge, so different LLMs can lead to slightly different outcomes and interpretations. Overall, "
               "this is a robust, scientific estimate given the small amount of data. For better results, test with "
               "smarter models, though it may cost more.")
        self.assertIn(sub, self.text.spaced())

    def test_four_tiles_with_their_numbers(self):
        self.assertEqual(self.page.count('<article class="tile'), 4)
        t = self.text.spaced()
        for label in ("User goals not reached", "Error rate", "Latency", "Cost per run"):
            self.assertIn(label, t)
        # 24 of 40 good; 3*8 + 8 = 32 failed calls of the sum of tools; see make_cig
        calls = sum(12 if i % 10 == 0 else 3 + i % 4 for i in range(40))
        self.assertIn("40% of runs did not get the user what they asked for (16 of 40 graded)", t)
        self.assertIn("%s of tool calls failed (32 of %s)" % (report.fmt_pct(3200.0 / calls), report.fmt_int(calls)), t)
        self.assertIn("is how long the slowest 1 in 10 runs take", t)

    def test_one_user_goal_problem_block(self):
        self.assertEqual(self.page.count("User goal problem we found"), 1)
        self.assertEqual(self.page.count('class="persona"'), 1)
        t = self.text.spaced()
        for k in ("persona", "headline", "goal", "problem"):
            self.assertIn(PROBLEM[k], t)
        self.assertIn("“" + PROBLEM["quote"] + "”", t)
        self.assertIn("20 runs in this group", t)
        self.assertIn("40% fell short of the goal (8 of 20)", t)

    def test_system5_has_four_rows_with_x_and_y(self):
        self.assertEqual(self.page.count('<div class="row">'), 4)
        sec = self.page[self.page.index("Implementing System 5 gives you"):self.page.index('class="charts"')]
        names = [sec.index(n) for n in ("User goals", "Cost", "Errors", "Latency")]
        self.assertEqual(names, sorted(names))
        s5 = {r["key"]: r for r in self.data["system5"]}
        self.assertEqual(s5["goals"]["x"], 60.0)
        self.assertEqual(s5["goals"]["y"], 77.1)
        self.assertEqual(s5["cost"]["delta_pct"], -50.0)
        self.assertEqual(s5["cost"]["unit"], "usd")
        flat = Text(sec).flat()
        self.assertIn("60%\u219277%", flat.replace("now", "").replace("withSystem5", ""))
        self.assertIn("+28.5%", flat)
        self.assertIn(MINUS + "50%", flat)

    def test_charts_come_after_system5_and_show_n(self):
        sec = self.page[self.page.index('class="charts"'):]
        self.assertIn('class="stack"', sec)
        self.assertEqual(sec.count('class="panel"'), 3)
        t = Text(sec).spaced()
        for s in ("Good", "Ambiguous", "Bad", "24 runs", "8 runs", "Tool calls that failed", "Typical run time",
                  "Average cost per run"):
            self.assertIn(s, t)
        self.assertLess(self.page.index("Implementing System 5 gives you"), self.page.index('class="charts"'))

    def test_closing_line_and_button(self):
        flat = self.text.flat()
        self.assertIn("Productiondatais49%moreaccurate,andwecanimproveallofthesemetrics:usergoals,cost,errorsandlatency.",
                      flat)
        self.assertRegex(self.page, r'<a class="cta" href="https://procept.tech/system5" target="_blank" rel="noopener')

    def test_removed_sections_are_gone(self):
        low = self.page.lower()
        for s in ("what the data says", "how to fix it", "top buyer", "top 3", "disclaimer", KAPPA, COHEN.lower(),
                  "reader-agent"):
            self.assertNotIn(s, low)
        for f in ("REPORT.md", "REPORT.docx"):
            self.assertFalse(os.path.exists(os.path.join(self.d, f)))
        self.assertFalse([f for f in os.listdir(self.d) if f.endswith((".docx", ".md"))])

    def test_scorecard_json_has_every_number_shown(self):
        for name, shown in self.data["shown"].items():
            self.assertIn(re.sub(r"\s+", "", shown), self.text.flat(), name)
        self.assertEqual(self.data["runs"], {"usable": 40, "graded": 40, "first_day": "2026-03-04",
                                             "last_day": "2026-03-06"})
        self.assertEqual(self.data["claims"]["accuracy_gain_pct"], 49)
        self.assertEqual(self.data["problem"]["runs"], 20)
        self.assertEqual(self.data["outcomes"]["good"], {"runs": 24, "percent": 60.0})
        self.assertEqual(sorted(self.data["by_outcome"]), ["ambiguous", "bad", "good"])
        self.assertNotIn("model", self.data["problem"])

    def test_no_external_requests(self):
        for bad in ("<link", "<script", "<img", "<iframe", "@import", "src=", "url("):
            self.assertNotIn(bad, self.page)
        self.assertEqual(re.findall(r"https?://[^\s\"'<>]+", self.page), ["https://procept.tech/system5"])
        self.assertIn("system-ui", self.page)

    def test_viewports_themes_and_print(self):
        for s in ('name="viewport"', "prefers-color-scheme:light", '[data-theme="light"]', "@media print",
                  "max-width:520px", "color-scheme"):
            self.assertIn(s, self.page)
        self.assertEqual(self.page.count("<!--"), 0)           # the template's own notes do not ship

    def test_the_page_passes_its_own_lint(self):
        self.assertEqual(report.lint_hits(self.page), [])

    def test_untrusted_text_is_escaped(self):
        rows = [{"score": 1 if i % 2 else -1, "tools": 1, "latency": 2.0 + i} for i in range(8)]
        write_cig(self.d, rows, dossier_text="price > 500k & <b>rent</b> for \"project\" %d")
        write_problem(self.d, run_ids=["r%d" % i for i in range(8)],
                      quote='price > 500k & <b>rent</b> for "project" 3')
        page = self.build()
        self.assertIn("price &gt; 500k &amp; &lt;b&gt;rent&lt;/b&gt; for &quot;project&quot; 3", page)
        self.assertNotIn("<b>rent</b>", page)
        self.assertIn('price > 500k & <b>rent</b> for "project" 3', Text(page).spaced())
        write_problem(self.d, persona="Ops <b>lead</b>")
        with self.assertRaisesRegex(report.ProblemError, "template brackets"):
            report.build(self.d)

    def test_deterministic(self):
        first = open(os.path.join(self.d, "scorecard.html"), "rb").read()
        os.remove(os.path.join(self.d, "scorecard.html"))
        report.build(self.d)
        self.assertEqual(open(os.path.join(self.d, "scorecard.html"), "rb").read(), first)


class UnitVariantTests(Base):
    def test_tokens_and_calls_label_the_cost_plainly(self):
        self.standard()
        page = self.build()
        t = Text(page).spaced()
        self.assertIn("tokens used by an average run", t)
        self.assertIn("Average tokens per run", t)
        write_cig(self.d, [{"score": 1 if i % 2 else -1, "tools": 3, "fails": 1, "latency": 3.0 + i} for i in range(8)],
                  error_counts=(2,))
        write_problem(self.d, run_ids=["r%d" % i for i in range(8)])
        page = self.build()
        self.assertIn("tool calls in an average run", Text(page).spaced())
        self.assertIn("Average tool calls per run", Text(page).spaced())

    def test_a_group_with_no_runs_still_draws_a_clean_chart(self):
        write_cig(self.d, [{"score": 1 if i % 2 else 0, "tools": 2, "fails": i % 2, "latency": 4.0 + i, "tokens": 50}
                           for i in range(8)], error_counts=(4,))
        write_problem(self.d, run_ids=["r%d" % i for i in range(8)], quote_run_id="r3",
                      quote="show my open tickets for project 3")
        page = self.build()
        self.assertIn("0 runs", page)
        self.assertEqual(report.lint_hits(page), [])

    def test_big_numbers_get_separators_and_units(self):
        rows = [{"score": 1 if i % 3 else -1, "tools": 20, "fails": 4, "tokens": 662000 + i, "latency": 150.0 + i,
                 "cost": 0.1704} for i in range(12)]
        write_cig(self.d, rows, error_counts=(30, 20, 10))
        write_problem(self.d, run_ids=["r%d" % i for i in range(12)])
        t = Text(self.build()).spaced()
        self.assertIn("$2.04 across 12 runs", t)            # 12 * 0.1704
        self.assertIn("$0.17", t)
        self.assertIn("$0.085", t)
        self.assertRegex(t, r"2\.\d ?min")


class FlowTests(Base):
    def test_gates_refuse_until_every_stage_is_signed_off(self):
        self.standard()
        with mock.patch.dict(os.environ, {"PLITE_NO_GATE": ""}):
            with self.assertRaises(SystemExit) as cm:
                report.build(self.d)
        self.assertIn("ingest", str(cm.exception))
        self.assertFalse(os.path.exists(os.path.join(self.d, "scorecard.html")))

    def test_gates_ask_for_ingest_merge_cig_and_study(self):
        self.standard()
        with mock.patch.object(report.checks, "require") as req:
            report.build(self.d)
        req.assert_called_once_with(self.d, ["ingest", "merge", "cig", "study"])

    def test_no_problem_file_no_page(self):
        make_cig(self.d)
        with self.assertRaisesRegex(SystemExit, "user_goal_problem"):
            report.build(self.d)
        self.assertFalse(os.path.exists(os.path.join(self.d, "scorecard.html")))

    def test_no_runs_and_no_grades_are_clear_errors(self):
        with self.assertRaisesRegex(SystemExit, "no usable runs"):
            report.build(self.d)
        write_cig(self.d, [{"score": None, "tools": 1} for _ in range(5)])
        with self.assertRaisesRegex(SystemExit, "no graded runs"):
            report.build(self.d)

    def test_missing_dimension_stops_the_build(self):
        write_cig(self.d, [{"score": 1 if i % 2 else -1, "tools": 2} for i in range(8)])   # no latency anywhere
        write_problem(self.d, run_ids=["r%d" % i for i in range(8)], quote_run_id="r3")
        with self.assertRaisesRegex(SystemExit, "no latency"):
            report.build(self.d)
        self.assertFalse(os.path.exists(os.path.join(self.d, "scorecard.html")))

    def test_lint_failure_writes_nothing(self):
        self.standard()
        with open(os.path.join(self.d, "scorecard.template.html"), "w") as f:
            f.write(open(os.path.join(PLUGIN, "templates", "scorecard.template.html")).read().replace(
                "</main>", "<p>{{no_such_slot}}</p></main>"))
        make_study(self.d)
        with self.assertRaisesRegex(SystemExit, "no_such_slot"):
            report.build(self.d)
        self.assertFalse(os.path.exists(os.path.join(self.d, "scorecard.html")))
        self.assertFalse(os.path.exists(os.path.join(self.d, "scorecard.json")))
        self.assertTrue(os.path.isdir(os.path.join(self.d, "study")))      # nothing is cleaned up after a failure

    def test_study_is_deleted_only_with_its_marker(self):
        self.standard()
        make_study(self.d)
        report.build(self.d)
        self.assertFalse(os.path.exists(os.path.join(self.d, "study")))
        os.makedirs(os.path.join(self.d, "study"))                         # a folder of the user's, no marker
        with open(os.path.join(self.d, "study", "mine.txt"), "w") as f:
            f.write("keep")
        report.build(self.d)
        self.assertTrue(os.path.exists(os.path.join(self.d, "study", "mine.txt")))

    def test_keep_study(self):
        self.standard()
        make_study(self.d)
        report.build(self.d, keep_study=True)
        self.assertTrue(os.path.exists(os.path.join(self.d, "study", ".procept-lite-study")))

    def test_an_edited_template_in_out_takes_precedence(self):
        self.standard()
        tpl = open(os.path.join(PLUGIN, "templates", "scorecard.template.html")).read()
        with open(os.path.join(self.d, "scorecard.template.html"), "w") as f:
            f.write(tpl.replace("See System 5", "Meet System 5"))
        self.assertIn("Meet System 5", self.build())

    def test_claims_in_out_change_the_page(self):
        self.standard()
        with open(os.path.join(self.d, "system5.json"), "w") as f:
            json.dump({"accuracy_gain_pct": 51, "url": "https://example.test/s5", "cost_cut_pct": 40}, f)
        page = self.build()
        self.assertIn("Production data is <b>51%</b> more accurate", page)
        self.assertIn('href="https://example.test/s5"', page)
        self.assertIn(MINUS + "40%", page)

    def test_every_slot_of_the_starter_is_provided(self):
        self.standard()
        sc = scorecard.compute(self.d)
        claims = estimates.load_claims(self.d)
        slots, _ = report.make_page(sc, report.validate_problem(self.d), claims, estimates.system5(sc, claims))
        tpl = open(os.path.join(PLUGIN, "templates", "scorecard.template.html")).read()
        body = re.sub(r"<!--.*?-->", "", tpl, flags=re.S)
        self.assertEqual(sorted(set(report._SLOT.findall(body)) - set(slots)), [])

    def test_main_prints_the_path_and_the_dispatcher_routes_to_it(self):
        self.standard()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(report.main(["--out", self.d]), 0)
        self.assertEqual(buf.getvalue().strip(), os.path.join(os.path.abspath(self.d), "scorecard.html"))
        r = subprocess.run([sys.executable, os.path.join(PLUGIN, "scripts", "plite.py"), "report", "--out", self.d],
                           capture_output=True, text=True, env=dict(os.environ, PLITE_NO_GATE="1"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stdout.strip().endswith("scorecard.html"))

    def test_main_reports_errors_on_stderr_with_exit_code_2(self):
        make_cig(self.d)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(report.main(["--out", self.d]), 2)
        self.assertTrue(err.getvalue().startswith("error: "))

    def test_old_report_files_and_writer_are_gone(self):
        self.assertFalse(os.path.exists(os.path.join(PLUGIN, "scripts", "plite", "docx_writer.py")))
        self.assertFalse(os.path.exists(os.path.join(PLUGIN, "templates", "report.template.md")))
        self.assertTrue(os.path.exists(os.path.join(PLUGIN, "templates", "scorecard.template.html")))
        for name in ("report.py", "scorecard.py", "estimates.py"):
            src = open(os.path.join(PLUGIN, "scripts", "plite", name)).read().lower()
            self.assertNotIn(KAPPA, src)
            self.assertNotIn("docx", src)


if __name__ == "__main__":
    unittest.main()
