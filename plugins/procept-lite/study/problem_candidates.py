"""Candidates for the scorecard's one user-goal problem. Usage: python3 -I problem_candidates.py [out]

Reads sections/personas.csv and sections/personas.json (written by the empathy section) and sig.load(). Writes
<out>/scorecard_inputs/problem_candidates.json: the top 3 user-goal groups by number of runs that fell short (not good),
among groups with a real, stated goal (never other/unclear), each with its stats, every run id and up to 5 verbatim
candidate quotes (<= 160 characters, complete, free of e-mail addresses, numbers and secrets, taken from first asks and
rationales), plus a skeleton of user_goal_problem.json whose narrative fields are empty for the model to fill. This
script never writes the final user_goal_problem.json: the model reads the candidates and the runs, then writes it.
"""
import csv
import json
import os
import re
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import sig  # noqa: E402
from validate_sections import MIN_GOAL_WORDS, cut_off, is_other, words  # noqa: E402

MAX_QUOTE, MIN_QUOTE, MIN_GROUP_RUNS, TOP = 160, 20, 5, 3
csv.field_size_limit(sys.maxsize)


def read_csv(path):
    try:
        with open(path, encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f))
    except OSError:
        return []


def personas(sec):
    """-> ({run_id: persona}, {lower name: entry}) from personas.csv and personas.json."""
    assign = {str(r.get("run_id") or "").strip(): str(r.get("persona") or "").strip() for r in read_csv(os.path.join(sec, "personas.csv"))}
    try:
        with open(os.path.join(sec, "personas.json"), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = []
    data = data.get("personas") if isinstance(data, dict) else data
    return assign, {str(e.get("name") or "").strip().lower(): e for e in data if isinstance(e, dict)} if isinstance(data, list) else {}


def pieces(text):
    """Verbatim, complete candidates inside a text: each line, else each sentence, kept only when it passes the filters."""
    out = []
    for line in str(text or "").splitlines():
        line = line.strip()
        if MIN_QUOTE <= len(line) <= MAX_QUOTE:
            out.append(line)
        elif len(line) > MAX_QUOTE:
            out += [m.group(0).strip() for m in re.finditer(r"[^.!?]+[.!?]", line)]
    return [p for p in out if MIN_QUOTE <= len(p) <= MAX_QUOTE and words(p) >= 4 and p == " ".join(p.split())
            and "..." not in p and "…" not in p and sig.mask(p) == p]


def norm(t):
    return re.sub(r"\W+", " ", t.lower()).strip()


def quotes(runs, first_in_conv):
    """Up to 5 candidates (3 from asks, 2 from rationales, filling from the other kind), one per run and wording."""
    short = [r for r in runs if r["outcome"] != 1]
    asks, rats = [], []
    for r in short:
        for p in pieces(r["first_ask"]):
            asks.append((0 if first_in_conv[r["run_id"]] else 1, r["outcome"], -min(words(p), 20), r["run_id"], p, "ask"))
        for p in pieces(r["rationale"]):
            rats.append((0, r["outcome"], -r["conf"], r["run_id"], p, "rationale"))
    picked, seen_t, seen_r = [], set(), set()

    def take(pool, k):
        n = 0
        for _, outcome, _, rid, p, src in sorted(pool):
            if n >= k:
                break
            if norm(p) in seen_t or rid in seen_r:
                continue
            seen_t.add(norm(p))
            seen_r.add(rid)
            picked.append({"run_id": rid, "source": src, "outcome": outcome, "text": p})
            n += 1
        return n
    a = take(asks, 3)
    b = take(rats, 2)
    if a + b < 5:
        take(asks if b < 2 else rats, 5 - a - b)
    return picked


def group_stats(name, goal, pain, runs, first_in_conv, themes):
    n = len(runs)
    c = Counter(r["label"] for r in runs)
    short = n - c["good"]
    iss = Counter()
    th = Counter()
    for r in runs:
        if r["outcome"] == 1:
            continue
        labels = themes.get(r["run_id"]) or [(r["issue_raw"], "")]
        for issue, theme in labels:
            if issue:
                iss[issue] += 1
            if theme:
                th[theme] += 1
    lat = sig.median([r["lat"] for r in runs])
    return {"persona": name, "goal": goal, "pain": pain, "n": n, "good": c["good"], "ambiguous": c["ambiguous"],
            "bad": c["bad"], "fell_short": short, "fell_short_pct": sig.pct(short, n),
            "median_latency_s": None if lat is None else round(lat, 1),
            "tool_fail_pct": sig.pct(sum(r["tool_fail"] for r in runs), n), "no_answer_pct": sig.pct(sum(r["no_answer"] for r in runs), n),
            "top_issues": [{"issue": k, "runs": v} for k, v in sorted(iss.items(), key=lambda kv: (-kv[1], kv[0]))[:5]],
            "themes": [{"theme": k, "runs": v} for k, v in sorted(th.items(), key=lambda kv: (-kv[1], kv[0]))[:5]],
            "run_ids": [r["run_id"] for r in runs], "quotes": quotes(runs, first_in_conv)}


def build(out=None):
    """-> the candidates dict (see the module docstring)."""
    out = sig.resolve_out(out)
    sec = os.path.join(out, "study", "sections")
    R = sig.load(out)
    assign, entries = personas(sec)
    themes = defaultdict(list)
    for r in read_csv(os.path.join(sec, "issues.csv")):
        if str(r.get("issue") or "").strip():
            themes[str(r.get("run_id") or "").strip()].append((str(r["issue"]).strip(), str(r.get("theme") or "").strip()))
    first = {}
    for r in R:                                   # R is in time order
        first.setdefault(r["conversation_id"], r["run_id"])
    first_in_conv = {r["run_id"]: first[r["conversation_id"]] == r["run_id"] for r in R}
    groups = defaultdict(list)
    for r in R:
        if assign.get(r["run_id"]):
            groups[assign[r["run_id"]]].append(r)
    eligible = []
    for name, runs in groups.items():
        e = entries.get(name.lower()) or {}
        goal, pain = str(e.get("goal") or "").strip(), str(e.get("pain") or "").strip()
        if is_other(name) or words(goal) < MIN_GOAL_WORDS or cut_off(goal):
            continue
        eligible.append((name, goal, pain, runs))
    pool = [g for g in eligible if len(g[3]) >= MIN_GROUP_RUNS] or eligible
    pool.sort(key=lambda g: (-sum(r["outcome"] != 1 for r in g[3]), -len(g[3]), g[0]))
    top = [group_stats(n, g, p, runs, first_in_conv, themes) for n, g, p, runs in pool[:TOP]]
    skeleton = {"persona": top[0]["persona"] if top else "", "goal": "", "problem": "", "headline": "", "quote": "",
                "quote_run_id": "", "run_ids": top[0]["run_ids"] if top else [], "model": ""}
    return {"graded_runs": len(R), "groups": top, "skeleton": skeleton}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    out = sig.resolve_out(argv[0] if argv else None)
    try:
        res = build(out)
    except (OSError, ValueError) as e:
        print("problem_candidates: %s" % e, file=sys.stderr)
        return 2
    if not res["groups"]:
        print("problem_candidates: no user-goal group has a real goal yet; finish sections/personas.csv and personas.json first",
              file=sys.stderr)
        return 1
    d = os.path.join(out, "scorecard_inputs")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "problem_candidates.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(res, f, indent=1, ensure_ascii=False)
    print("wrote %s" % path)
    for g in res["groups"]:
        print("  %s: %d of %d runs fell short (%d%%), %d quote candidates" % (g["persona"], g["fell_short"], g["n"], g["fell_short_pct"], len(g["quotes"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
