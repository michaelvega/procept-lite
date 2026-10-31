"""scorecard.html + scorecard.json: the customer-facing page of a run.

  python3 plite.py report --out DIR [--keep-study] [--validate-problem]

Flow: gates (ingest, merge, cig, study) -> validate_problem -> numbers (scorecard.py) -> System 5 rows
(estimates.py) -> fill the template -> lint_customer_html (a hit fails the build) -> write -> delete <out>/study.

The words come from the template and from <out>/scorecard_inputs/user_goal_problem.json (written by a model that
read the runs); this file only computes numbers, draws the bars and refuses anything that would look wrong.
A copy of the template at <out>/scorecard.template.html takes precedence over templates/scorecard.template.html.
"""
import argparse
import datetime
import html
import json
import math
import os
import re
import shutil
import sys
from collections import Counter
from html.parser import HTMLParser

try:
    from . import checks
    from . import estimates as est
    from . import scorecard as scm
    from .common import redact
except ImportError:  # imported as a top-level module
    import checks
    import estimates as est
    import scorecard as scm
    from common import redact

TITLE = "Agent Run Scorecard"
HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, "..", "..", "templates", "scorecard.template.html")
PROBLEM_FILE = os.path.join("scorecard_inputs", "user_goal_problem.json")
STUDY_MARKER = ".procept-lite-study"
GATES = ["ingest", "merge", "cig", "study"]
NB = "\u00a0"
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
CLS = {"good": "good", "ambiguous": "amb", "bad": "bad"}
NAME = {"good": "Good", "ambiguous": "Ambiguous", "bad": "Bad"}
E = lambda s: html.escape(str(s), quote=True)  # noqa: E731


# ------------------------------------------------------------ numbers as text
def fmt_int(n):
    return "{:,}".format(int(round(n)))


def plural(n, word):
    return "%s %s%s" % (fmt_int(n), word, "" if int(round(n)) == 1 else "s")


def _trim(s):
    return s.rstrip("0").rstrip(".") if "." in s else s


def fmt_pct(v):
    """7.2%, 48%, 0.05%: one decimal below 10, whole numbers above; never rounds up to 100%."""
    if v <= 0:
        return "0%"
    if v < 0.01:
        return "<0.01%"
    if v < 0.1:
        return "%.2f%%" % v
    if v < 10 or 99.5 <= v < 100:
        return _trim("%.1f" % v) + "%"
    return "%d%%" % round(v)


def fmt_usd(v):
    """$20.96, $0.17, $0.085: two significant digits below a dollar."""
    if v <= 0:
        return "$0"
    if v >= 100:
        return "$" + fmt_int(v)
    if v >= 1:
        return "$%.2f" % v
    if v < 0.00005:
        return "<$0.0001"
    return "$%.*f" % (1 - int(math.floor(math.log10(v))), v)


def fmt_tokens(v):
    """Tokens as k or M: 662k, 9.5k, 1.4M."""
    if v < 1000:
        return "%d" % round(v)
    if v < 999500:
        k = v / 1e3
        return _trim("%.1f" % k) + "k" if k < 10 else "%dk" % round(k)
    m = v / 1e6
    return _trim("%.1f" % m) + "M" if m < 10 else "%dM" % round(m)


def fmt_num(v):
    return _trim("%.1f" % v)


def fmt_calls(v):
    return fmt_num(v) if v < 10 else "%d" % round(v)


def fmt_seconds(s):
    """-> (number, unit): 7.2 s, 79 s, 2.4 min, 1.6 h."""
    if s < 90:
        v, u = s, "s"
    elif s < 5400:
        v, u = s / 60.0, "min"
    else:
        v, u = s / 3600.0, "h"
    return (_trim("%.2f" % v) if v < 0.1 else _trim("%.1f" % v) if v < 10 else "%d" % round(v)), u


def fmt_delta(d):
    """+28.5%, minus 50%; 'no change' when there is nothing to show."""
    if abs(d) < 0.05:
        return "no change"
    return ("+" if d > 0 else "\u2212") + _trim("%.1f" % abs(d)) + "%"


def fmt_dates(first, last):
    """26-27 Oct 2026, 28 Oct - 3 Nov 2026, ... ('' when a date cannot be read)."""
    try:
        a, b = datetime.date.fromisoformat(first), datetime.date.fromisoformat(last)
    except ValueError:
        return ""
    day = lambda x: "%d %s" % (x.day, MONTHS[x.month - 1])  # noqa: E731
    if a == b:
        return "%s %d" % (day(a), a.year)
    if (a.year, a.month) == (b.year, b.month):
        return "%d\u2013%d %s %d" % (a.day, b.day, MONTHS[a.month - 1], a.year)
    if a.year == b.year:
        return "%s \u2013 %s %d" % (day(a), day(b), a.year)
    return "%s %d \u2013 %s %d" % (day(a), a.year, day(b), b.year)


def show(unit, v):
    """-> (plain text, html) of `v` in `unit` (pct, usd, tokens, calls, s)."""
    if unit == "s":
        n, u = fmt_seconds(v)
        return n + NB + u, E(n) + "<small>" + E(u) + "</small>"
    t = {"pct": fmt_pct, "usd": fmt_usd, "tokens": fmt_tokens, "calls": fmt_calls}[unit](v)
    return t, E(t)


def round_shares(counts):
    """Whole-number percentages of `counts` that add up to 100 (largest remainder)."""
    total = sum(counts)
    if not total:
        return [0] * len(counts)
    raw = [100.0 * c / total for c in counts]
    base = [int(v) for v in raw]
    for i in sorted(range(len(raw)), key=lambda i: (base[i] - raw[i], i))[:100 - sum(base)]:
        base[i] += 1
    return base


# ------------------------------------------------------------ the one user-goal problem
class ProblemError(SystemExit, ValueError):
    """One line telling the host what to fix. A SystemExit and a ValueError, so either kind of caller works."""


_FIELD_HINT = {
    "persona": "a 2-4 word label for who this user is", "goal": "one full sentence: what this user is trying to get done",
    "problem": "one full sentence: what blocks them", "headline": "one salient sentence about this buyer and what stops them",
    "quote": "a verbatim quote of at most 160 characters from the run", "quote_run_id": "the run the quote comes from",
    "model": "the id of the model that wrote this file"}
_WORDS = {"persona": (1, 6), "headline": (8, 45), "goal": (5, 60), "problem": (5, 60), "quote": (3, 40)}
_DANGLING = set("a an the and or but nor of to without because which who whose whom if than as into onto via per "
                "their its my our your".split())  # words a finished sentence cannot end on
_GENERIC_PERSONAS = {"other", "others", "misc", "miscellaneous", "general", "generic", "user", "users", "customer",
                     "customers", "buyer", "buyers", "ungrouped", "unlabelled", "unlabeled", "unspecified"}
_END = re.compile(r"[.!?][\"'\u201d\u2019)\]]*$")

# the two statistics names are written so that they never appear spelled out in the source
_BANNED = [(re.compile(p, re.I), label) for p, label in (
    (r"ka{1}p{2}a", "stats jargon"), (r"co(?:h)en", "stats jargon"),
    (r"p-value|confidence interval|standard deviation|chi-?square", "stats jargon"),
    (r"\bunclear\b", "unclear"), (r"\bunknown\b", "unknown"), (r"\bn/a\b", "n/a"), (r"\bnan\b", "nan"),
    (r"\bnone\b", "none"), (r"\bundefined\b", "undefined"), (r"\binf(?:inity)?\b", "infinity"),
    (r"\bto-?do\b|\btbd\b|\bfixme\b", "todo"), (r"placeholder|lorem|ipsum", "placeholder"),
    (r"\brepair|\bmistak|\bmislabel|\b(?:we|were|was|been)\s+(?:dropped|fixed|corrected|removed|excluded|discarded)\b"
     r"|\b(?:dropped|removed|excluded|discarded)\s+\d", "repair wording"),
    (r"\bsorry\b|apolog", "apology"), (r"\bunusable\b", "unusable"),
    (r"/Us" r"ers/|/home/|/private/|/tmp/|\b[A-Za-z]:\\|\.jsonl?\b|\.csv\b|\bsign-?off\b|inputs_hash|\bPLITE_", "internal path"),
    (r"\.\.\.|\u2026", "cut-off text"),
    (r"\{\{|\}\}|\$\{|%\([A-Za-z_]+\)s|\$[A-Za-z_]{2,}", "unrendered placeholder"),
    (r"(?<![$\d.])\d+\.\d{4,}|[-\u2212]0(?:\.0+)?%", "untidy number"))]


def banned_hits(text):
    """[(label, context)] for every forbidden wording in `text` (one per label)."""
    res = []
    for rx, label in _BANNED:
        m = rx.search(text)
        if m:
            res.append((label, text[max(0, m.start() - 25):m.end() + 25].strip()))
    return res


def _fail(msg):
    raise ProblemError("%s: %s" % (PROBLEM_FILE, msg))


def _words_ok(field, text):
    lo, hi = _WORDS[field]
    n = len(text.split())
    if not lo <= n <= hi:
        _fail("'%s' has %d words; it needs %d to %d (%s)" % (field, n, lo, hi, _FIELD_HINT[field]))


def _text_ok(field, text, sentence, brackets=True):
    """Reject blank-looking, cut-off or junk text; `sentence` fields must be one complete sentence."""
    _words_ok(field, text)
    hits = banned_hits(text)
    if hits:
        _fail("'%s' contains %s (%r); rewrite it without" % (field, hits[0][0], hits[0][1]))
    if brackets and re.search(r"[<>{}]", text):
        _fail("'%s' contains template brackets; write plain text" % field)
    letters = sum(ch.isalpha() for ch in text)
    words = [w.lower() for w in text.split()]
    if letters < 0.6 * len(text.replace(" ", "")) or len(set(words)) < min(len(words), max(3, len(words) // 2)):
        _fail("'%s' reads like filler, not a real sentence; write it from the evidence" % field)
    if not sentence:
        return
    if not _END.search(text):
        _fail("'%s' does not end with . ! or ? (cut off?); write the full sentence" % field)
    last = re.findall(r"[\w']+", text)[-1].lower()
    if last in _DANGLING:
        _fail("'%s' ends on '%s' (cut off?); write the full sentence" % (field, last))
    if text.count("(") != text.count(")") or text.count('"') % 2 or text.count("\u201c") != text.count("\u201d"):
        _fail("'%s' has an unclosed bracket or quote (cut off?); write the full sentence" % field)


def _dossier_text(out, run_id):
    """What was said in a run (ask, answer, tool inputs and outputs, errors), or None if the run has no dossier."""
    needle = json.dumps(run_id)[1:-1]
    for rel in (os.path.join("cig", "run_dossiers.jsonl"), os.path.join("normalized", "runs.jsonl")):
        path = os.path.join(out, rel)
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8") as f:
            for line in f:
                if needle not in line:
                    continue
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                if isinstance(o, dict) and str(o.get("run_id")) == run_id:
                    parts = [o.get("user_message"), o.get("final_answer")]
                    for tc in o.get("tool_calls") or []:
                        if isinstance(tc, dict):
                            parts += [tc.get("args"), tc.get("result"), tc.get("error")]
                    parts += o.get("errors") or []
                    return " ".join("\n".join(p for p in parts if isinstance(p, str)).split())
    return None


def validate_problem(out, runs=None):
    """Check scorecard_inputs/user_goal_problem.json against the data and return it with n and the fell-short share
    computed here. Raises ProblemError (one line saying what to fix)."""
    path = os.path.join(out, PROBLEM_FILE)
    if not os.path.isfile(path):
        _fail("is missing. Run the study step (study-init, then the study's last step that writes the user-goal "
              "problem), then build the scorecard again")
    try:
        with open(path, encoding="utf-8-sig") as f:
            d = json.load(f)
    except (OSError, ValueError):
        _fail("is not valid JSON; the study's problem step must write it again")
    if not isinstance(d, dict):
        _fail("must be a JSON object with persona, goal, problem, headline, quote, quote_run_id, run_ids and model")
    f = {}
    for k, hint in _FIELD_HINT.items():
        v = d.get(k)
        if not isinstance(v, str) or not v.strip():
            _fail("'%s' is missing or blank; write %s" % (k, hint))
        f[k] = " ".join(v.split())
    _text_ok("persona", f["persona"], False)
    if f["persona"].lower() in _GENERIC_PERSONAS:
        _fail("'persona' is too generic; name the user-goal group (e.g. what they are there to do)")
    for k in ("headline", "goal", "problem"):
        _text_ok(k, f[k], True)
    _text_ok("quote", f["quote"], False, brackets=False)
    if len(f["quote"]) > 160:
        _fail("'quote' is %d characters; it must be at most 160" % len(f["quote"]))
    if redact(f["quote"]) != f["quote"]:
        _fail("'quote' contains an e-mail address or a secret; pick another verbatim quote")
    if not checks.gates_disabled() and not checks.model_allowed(f["model"]):
        _fail("'model' %r is below the floor (Sonnet 5.5 or GPT 5.6 Terra, or better; never Haiku)" % f["model"])
    ids = d.get("run_ids")
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        _fail("'run_ids' must be a list of run id strings (every graded run in this user-goal group)")
    ids = list(dict.fromkeys(i.strip() for i in ids if i.strip()))
    label = {r["run_id"]: r["label"] for r in (scm.load_runs(out) if runs is None else runs)
             if r["label"] in scm.LABELS}
    stray = [i for i in ids if i not in label]
    if stray:
        _fail("'run_ids' has %d id(s) that are not graded, usable runs of this dataset (e.g. %s)"
              % (len(stray), redact(stray[0])[:60]))
    need = min(5, len(label))
    if len(ids) < need:
        _fail("'run_ids' lists %d run(s); a user-goal group needs at least %d" % (len(ids), need))
    if f["quote_run_id"] not in ids:
        _fail("'quote_run_id' must be one of 'run_ids'")
    said = _dossier_text(out, f["quote_run_id"])
    if said is None:
        _fail("the run in 'quote_run_id' has no dossier to check the quote against")
    if f["quote"] not in said:
        _fail("'quote' is not found verbatim in the run in 'quote_run_id'; copy it exactly from that run")
    split = {l: sum(1 for i in ids if label[i] == l) for l in scm.LABELS}
    short = split["ambiguous"] + split["bad"]
    if not short:
        _fail("no run in 'run_ids' fell short of the goal; use the group with the most runs that fell short")
    return dict(f, run_ids=ids, n=len(ids), split=split, fell_short_n=short,
                fell_short_pct=scm.pct(short, len(ids)))


# ------------------------------------------------------------ the page
def shares(split):
    """Whole-number percentage per outcome, adding up to 100."""
    return dict(zip(scm.LABELS, round_shares([split[l] for l in scm.LABELS])))


def stack(split, mini=False):
    """One stacked bar of good / ambiguous / bad runs; with a percentage inside the wide segments."""
    total, share = sum(split.values()), shares(split)
    segs = "".join(
        '<i class="%s" style="width:%.2f%%">%s</i>' % (
            CLS[l], 100.0 * split[l] / total, "" if mini or split[l] * 11 < total else "%d%%" % share[l])
        for l in scm.LABELS if split[l])
    aria = "%s of %s" % (", ".join("%s %s" % (fmt_int(split[l]), l) for l in scm.LABELS), plural(total, "run"))
    return '<div class="stack%s" role="img" aria-label="%s">%s</div>' % (" mini" if mini else "", E(aria), segs)


def chart_split(split):
    items, share = [], shares(split)
    for l in scm.LABELS:
        s = "<1%" if split[l] and not share[l] else "%d%%" % share[l]
        items.append('<li class="%s"><b>%s</b>%s <span>%s</span></li>'
                     % (CLS[l], E(s), NAME[l], E(plural(split[l], "run"))))
    return stack(split) + '<ul class="legend">%s</ul>' % "".join(items)


def chart_metrics(sc):
    """Three small multiples (errors, run time, cost per run), each with one bar per outcome, from zero."""
    unit = sc["unit"]
    cost_name = {"usd": "Average cost per run", "tokens": "Average tokens per run",
                 "calls": "Average tool calls per run"}[unit]
    panels = (("Tool calls that failed", "error_pct", "pct", "no tool calls"),
              ("Typical run time", "latency_median", "s", "no timings"),
              (cost_name, "cost_mean", unit, "not recorded"))
    out = []
    for title, key, u, missing in panels:
        top = max([sc["by"][l][key] for l in scm.LABELS if sc["by"][l][key] is not None] + [0])
        rows = []
        for l in scm.LABELS:
            g, v = sc["by"][l], sc["by"][l][key]
            if v is None:
                bar, val = "", '<b class="val dim">%s</b>' % (E(missing) if g["n"] else "0 runs")
            else:
                bar = '<i class="%s" style="width:%.2f%%"></i>' % (CLS[l], 100.0 * v / top if top else 0)
                val = '<b class="val">%s</b>' % show(u, v)[1]
            rows.append('<li class="mrow"><span class="grp">%s<em>%s</em></span><span class="track">%s</span>%s</li>'
                        % (NAME[l], E(plural(g["n"], "run")), bar, val))
        out.append('<div class="panel"><h3>%s</h3><ul>%s</ul></div>' % (E(title), "".join(rows)))
    return '<div class="panels">%s</div>' % "".join(out)


def _r(o):
    """Floats rounded for scorecard.json."""
    if isinstance(o, float):
        return round(o, 4)
    if isinstance(o, dict):
        return {k: _r(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_r(v) for v in o]
    return o


def make_page(sc, problem, claims, rows):
    """-> (slots, data): ready-to-insert HTML per template slot, and scorecard.json (every number shown)."""
    R = {r["key"]: r for r in rows}
    g, c, e, l = R["goals"], R["cost"], R["errors"], R["latency"]
    unit = c["unit"]
    slots, shown = {}, {}

    def put(name, text, markup=None):
        shown[name] = text
        slots[name] = E(text) if markup is None else markup

    def put_value(name, u, v):
        text, markup = show(u, v)
        put(name, text, markup)

    for r in rows:
        put_value(r["key"] + "_x", r["unit"], r["x"])
        put_value(r["key"] + "_y", r["unit"], r["y"])
        put(r["key"] + "_delta", fmt_delta(r["delta_pct"]))
    put_value("goals_now", "pct", round(100 - g["x"], 1))
    put("goals_missed", fmt_int(g["graded"] - g["good"]))
    put("goals_graded", fmt_int(g["graded"]))
    put_value("errors_now", "pct", e["x"])
    put("errors_failed", fmt_int(e["fails"]))
    put("errors_calls", fmt_int(e["calls"]))
    put_value("latency_now", "s", l["x"])
    put("latency_median", NB.join(fmt_seconds(l["median"])))
    put_value("cost_now", unit, c["x"])
    total = {"usd": fmt_usd, "tokens": fmt_tokens, "calls": fmt_int}[unit](c["total"])
    put("cost_total", total)
    put("n_runs", plural(c["n"], "run"))
    put("cost_caption", {"usd": "average cost of one run (%s across %s).",
                         "tokens": "tokens used by an average run (%s across %s).",
                         "calls": "tool calls in an average run (%s across %s)."}[unit] % (total, shown["n_runs"]))
    kicker = "%s graded" % plural(sc["n_graded"], "run")
    if sc["dates"] and fmt_dates(*sc["dates"]):
        kicker += " \u00b7 " + fmt_dates(*sc["dates"])
    put("kicker", kicker)
    put("title", TITLE)
    for k in ("persona", "headline", "goal", "problem", "quote"):
        put(k, problem[k])
    put("problem_n", fmt_int(problem["n"]))
    put("problem_short_n", fmt_int(problem["fell_short_n"]))
    put_value("problem_short_pct", "pct", problem["fell_short_pct"])
    split = problem["split"]
    slots["problem_bar"] = stack(split, mini=True) + '<p class="note">%s</p>' % E(
        " \u00b7 ".join("%s %s" % (fmt_int(split[x]), x) for x in scm.LABELS))
    slots["chart_split"] = chart_split(sc["split"])
    slots["chart_metrics"] = chart_metrics(sc)
    put("accuracy_gain_pct", fmt_num(claims["accuracy_gain_pct"]))
    slots["url"] = E(claims["url"])
    first, last = sc["dates"] or ("", "")
    data = _r({
        "title": TITLE,
        "runs": {"usable": sc["n"], "graded": sc["n_graded"], "first_day": first, "last_day": last},
        "tiles": {
            "goals": {"not_reached_percent": round(100 - g["x"], 1), "not_reached": g["graded"] - g["good"], "graded": g["graded"]},
            "errors": {"percent": e["x"], "failed_calls": e["fails"], "tool_calls": e["calls"],
                       "definition": "failed tool calls divided by all tool calls"},
            "latency": {"slowest_tenth_s": l["x"], "median_s": l["median"]},
            "cost": {"unit": unit, "per_run": c["x"], "total": c["total"], "runs": c["n"]}},
        "problem": {k: problem[k] for k in ("persona", "headline", "goal", "problem", "quote")} | {
            "runs": problem["n"], "fell_short": problem["fell_short_n"],
            "fell_short_pct": problem["fell_short_pct"], "outcomes": problem["split"]},
        "system5": rows,
        "outcomes": {k: {"runs": sc["split"][k], "percent": scm.pct(sc["split"][k], sc["n_graded"])}
                     for k in scm.LABELS},
        "by_outcome": {k: {"runs": v["n"], "error_pct": v["error_pct"], "median_latency_s": v["latency_median"],
                           "cost_per_run": v["cost_mean"]} for k, v in sc["by"].items()},
        "claims": claims,
        "shown": shown})
    return slots, data


_SLOT = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")


def load_template(out):
    for p in (os.path.join(out, "scorecard.template.html"), TEMPLATE):
        if os.path.isfile(p):
            with open(p, encoding="utf-8") as f:
                return f.read()
    raise SystemExit("error: templates/scorecard.template.html is missing from the plugin")


def fill(template, slots):
    """Drop the template's own comments, then fill every known {{slot}}; unknown ones stay (and fail the lint)."""
    t = re.sub(r"<!--.*?-->", "", template, flags=re.S)
    t = _SLOT.sub(lambda m: slots.get(m.group(1), m.group(0)), t)
    return re.sub(r"\n{3,}", "\n\n", t)


# ------------------------------------------------------------ lint
REQUIRED = ("pb-headline", "pb-goal", "pb-problem")
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


class LintError(SystemExit, ValueError):
    pass


class _Scan(HTMLParser):
    """Visible text (no <style> or <script>), titles and labels, comments, and the text of the required elements."""

    def __init__(self, required):
        HTMLParser.__init__(self, convert_charrefs=True)
        self.required, self.text, self.found, self.stack, self.skip, self.blank = required, [], {}, [], 0, 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        self.text += [a[k] for k in ("title", "alt", "aria-label") if a.get(k)]
        if self.stack and tag != "br":
            self.stack[-1]["kids"] += 1
        if tag in ("style", "script"):
            self.skip += 1
        if tag not in _VOID:
            self.stack.append({"tag": tag, "cls": (a.get("class") or "").split(), "text": [], "kids": 0})

    def handle_endtag(self, tag):
        if tag in ("style", "script"):
            self.skip = max(0, self.skip - 1)
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i]["tag"] == tag:
                for el in self.stack[i:]:
                    txt = "".join(el["text"]).replace("\xa0", " ").strip()
                    for c in el["cls"]:
                        if c in self.required:
                            self.found.setdefault(c, []).append(txt)
                    if el["tag"] in ("td", "th") and not txt and not el["kids"]:
                        self.blank += 1
                del self.stack[i:]
                break

    def handle_data(self, data):
        if not self.skip:
            self.text.append(data)
            for el in self.stack:
                el["text"].append(data)

    def handle_comment(self, data):
        self.text.append(data)


def lint_hits(html_text, required=REQUIRED):
    """Everything on the page that a customer must not see (list of short strings; empty = clean)."""
    hits = []
    for m in _SLOT.finditer(html_text):
        hits.append("unrendered placeholder: {{%s}}" % m.group(1))
    scan = _Scan(required)
    scan.feed(html_text)
    scan.close()
    text = " ".join(" ".join(scan.text).split())
    hits += ["%s: ...%s..." % hit for hit in banned_hits(text)]
    if scan.blank:
        hits.append("blank table cell (%d)" % scan.blank)
    for c in required:
        if c not in scan.found:
            hits.append("missing element with class %s" % c)
        elif not all(scan.found[c]):
            hits.append("empty element with class %s" % c)
    return hits


def lint_customer_html(html_text, required=REQUIRED):
    """Fail the build (LintError, a SystemExit) when the page holds forbidden wording, a blank cell, an unfilled slot or
    an empty headline, goal or problem."""
    hits = lint_hits(html_text, required)
    if hits:
        raise LintError("error: scorecard.html would show text a customer must not see: %s. Fix the source (the "
                        "problem file, the template or the data) and build again." % "; ".join(hits[:6]))


# ------------------------------------------------------------ build
def _write(path, text):
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(path + ".tmp", path)


def clear_study(out):
    """Delete the temporary <out>/study, only when it carries the marker study-init wrote."""
    d = os.path.join(out, "study")
    if os.path.isfile(os.path.join(d, STUDY_MARKER)) and not os.path.islink(d):
        shutil.rmtree(d)


def build(out, keep_study=False):
    """gates -> validate_problem -> numbers -> page -> lint -> write -> delete the study. Returns scorecard.html."""
    checks.require(out, GATES)
    sc = scm.compute(out)
    if not sc["n"]:
        raise SystemExit("error: no usable runs in %s (build the CIG first)" % os.path.join(out, "cig", "runs.csv"))
    if not sc["n_graded"]:
        raise SystemExit("error: no graded runs (scores.csv has no outcome for any run); finish grading first")
    problem = validate_problem(out, sc["runs"])
    try:
        claims = est.load_claims(out)
        rows = est.system5(sc, claims)
    except ValueError as e:
        raise SystemExit("error: %s" % e)
    slots, data = make_page(sc, problem, claims, rows)
    page = fill(load_template(out), slots)
    lint_customer_html(page)
    _write(os.path.join(out, "scorecard.json"), json.dumps(data, indent=1, ensure_ascii=False) + "\n")
    path = os.path.join(out, "scorecard.html")
    _write(path, page)
    if not keep_study:
        clear_study(out)
    return path


def main(argv=None):
    ap = argparse.ArgumentParser(prog="plite report", description="Write scorecard.html and scorecard.json.")
    ap.add_argument("--out", default="./procept-out")
    ap.add_argument("--keep-study", action="store_true", help="keep <out>/study after the build")
    ap.add_argument("--validate-problem", action="store_true",
                    help="only check scorecard_inputs/user_goal_problem.json")
    a = ap.parse_args(argv)
    try:
        if a.validate_problem:
            p = validate_problem(a.out)
            print("ok: %s (%s, %d%% fell short)" % (p["persona"], plural(p["n"], "run"), round(p["fell_short_pct"])))
        else:
            print(os.path.abspath(build(a.out, a.keep_study)))
    except SystemExit as e:
        if not isinstance(e.code, str):
            raise
        print(e.code if e.code.startswith("error") else "error: " + e.code, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
