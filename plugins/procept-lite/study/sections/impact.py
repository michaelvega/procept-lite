"""Impact. Re-run: python3 -I sections/impact.py   (run affinity.py first)

STARTER, NOT THE ANSWER. The host agent reads the affinity themes and the runs behind them, then EDITS this file: the fix list
(what to change, in which words), the effort of each (S, M or L is the analyst's judgement) and the evidence. The prose in
THEME_FIX is a generic first draft written for any agent; rewrite it from the real failures of THIS dataset. Keep the architecture:
6 to 8 ranked fixes at most, one per theme or issue worth fixing, each with the runs it touches, the runs that fell short and
are addressable, the best-case gain in good share (overlaps removed, a bound and not a forecast) and a link to its own report.

Reads sig, sections/issues.csv and sections/affinity.json. Writes impact.html, impact.md, impact.json next to this file and one
standalone report per fix to ../fix_reports/F<n>.html (shell look, at most 250 words).
"""
import csv
import html
import json
import os
import re
import sys
from collections import defaultdict

H = os.path.dirname(os.path.abspath(__file__))
STUDY = os.path.dirname(H)
sys.path.insert(0, STUDY)
sys.path.insert(0, H)
import sig  # noqa: E402
import aff_rules as A  # noqa: E402

e = html.escape
R = sig.load()
N = len(R)
BY = {r["run_id"]: r for r in R}
SHORT = {r["run_id"] for r in R if r["outcome"] != 1}
MAX_FIXES, TARGET_FIXES, REPORT_WORDS = 8, 7, 250

# Edit per dataset. effort is the analyst's judgement: S (days), M (a sprint), L (a roadmap item).
THEME_FIX = {
    A.T_DODGE: dict(title="Always finish the job, or say plainly why not", effort="S",
                    problem="The run ended empty, stalled, or handed the user elsewhere, so nothing moved toward the goal.",
                    change="Never end a turn without a result: when the last step has no answer, state what was found and what is missing. Treat a stated plan as an unfinished turn and carry on.",
                    measure="Share of runs that end without a delivered answer; it should fall toward zero."),
    A.T_GAP: dict(title="Build or announce the most-asked missing capabilities", effort="L",
                  problem="Users asked for something the product does not offer yet. Each dead end is a product-gap signal.",
                  change="Rank the missing capabilities by how often they were asked and build the top ones. Until then say so up front and offer the nearest thing that works.",
                  measure="Runs per week that hit each named gap, and their good share after launch."),
    A.T_PILE: dict(title="Stop failures from piling up on one run", effort="M",
                   problem="One run hit several failed calls, retries or long waits and then gave a thin answer or none.",
                   change="Retry a failing call once with a narrower request, cap repeats of the same call, and after two failures stop and report what is known.",
                   measure="Share of runs with three or more failed calls, and the good share of runs that had any failure."),
    A.T_WRONG: dict(title="Check every answer against the tool results", effort="M",
                    problem="An answer arrived but was wrong, partial or at odds with what the tools returned.",
                    change="Before sending, check each claim in the draft against the tool output; drop or look up anything with no source, and list any part of the ask left undone.",
                    measure="Share of sampled replies with a claim that the tool results do not back."),
    A.T_PROOF: dict(title="Report an action only after its call succeeded", effort="S",
                    problem="The reply claimed work was done that no call in the run shows.",
                    change="Say an action is done only after its tool call has succeeded; show a failure plainly when a call fails.",
                    measure="Share of 'done' claims with no matching successful call in the same run."),
    A.T_STEER: dict(title="Start the work instead of asking first", effort="S",
                    problem="The user had to repeat, correct or answer a question before any work started.",
                    change="Take a sensible default, do the work and state the assumption; ask only when the request cannot be run at all.",
                    measure="Share of runs whose first reply is a question with no tool call, and how many are followed by a repeated ask."),
}


def uid_len(ids):
    n = 8
    while len({i[:n] for i in ids}) < len(set(ids)) and n < 40:
        n += 1
    return n


RL = uid_len([r["run_id"] for r in R])


def rid(x):
    return x[:RL]


try:
    with open(os.path.join(H, "affinity.json"), encoding="utf-8") as f:
        AFF = json.load(f)
    with open(os.path.join(H, "issues.csv"), encoding="utf-8-sig", newline="") as f:
        ROWS = list(csv.DictReader(f))
except OSError as ex:
    sys.exit("impact: run affinity.py first (%s)" % ex)

by_theme, by_topic = defaultdict(set), defaultdict(set)
issue_of = defaultdict(list)
for row in ROWS:
    if row["run_id"] in BY:
        by_theme[row["theme"]].add(row["run_id"])
        by_topic[(row["theme"], row["topic"])].add(row["run_id"])
        issue_of[(row["theme"], row["run_id"])].append(row["issue"])
topics_of = defaultdict(list)
for t in AFF.get("topics", []):
    topics_of[t["theme"]].append(t["topic"])
themes = [t["theme"] for t in AFF.get("themes", [])]

# ---- the fix list: one per theme, then the largest themes split by topic until there are about seven
items = [dict(theme=th, topics=None, ids=set(by_theme[th])) for th in themes]


def splittable(it):
    if len(it["ids"]) < 6:
        return None
    tps = it["topics"] if it["topics"] is not None else topics_of[it["theme"]]
    if len(tps) < 2:
        return None
    ranked = sorted(tps, key=lambda tp: (-len(by_topic[(it["theme"], tp)]), tp))
    rest = set().union(*[by_topic[(it["theme"], tp)] for tp in ranked[1:]])
    return ranked if len(by_topic[(it["theme"], ranked[0])]) >= 3 and len(rest) >= 2 else None


while len(items) < TARGET_FIXES:
    cand = [(len(it["ids"]), k) for k, it in enumerate(items) if splittable(it)]
    if not cand:
        break
    _, k = max(cand, key=lambda t: (t[0], -t[1]))
    it = items.pop(k)
    ranked = splittable(it)
    top, rest = ranked[0], ranked[1:]
    th = it["theme"]
    items.append(dict(theme=th, topics=[top], ids=set(by_topic[(th, top)]), lead=top))
    items.append(dict(theme=th, topics=rest, ids=set().union(*[by_topic[(th, tp)] for tp in rest]), rest=True))
items = [it for it in items if len(it["ids"]) >= 2] or items
items = [it for it in items if it["ids"]]


# ---- numbers: touched, addressable, best-case gain with overlaps removed (greedy by new runs)
def mech(theme):
    return {A.T_DODGE: lambda r: r["no_answer"], A.T_PILE: lambda r: r["tool_fail"]}.get(theme)


for it in items:
    m = mech(it["theme"])
    extra = {r["run_id"] for r in R if m and m(r) and r["outcome"] == 1} if it["topics"] is None else set()
    it["touched"] = it["ids"] | extra
    it["bad"] = sum(1 for i in it["ids"] if BY[i]["label"] == "bad")
    it["amb"] = len(it["ids"]) - it["bad"]
    it["alone"] = round(100.0 * len(it["ids"]) / N, 1)
chosen, covered, left = [], set(), list(items)
while left and len(chosen) < MAX_FIXES:
    best = max(left, key=lambda it: (len(it["ids"] - covered), len(it["ids"]), it["theme"], it["topics"] or []))
    new = best["ids"] - covered
    if not new and chosen:
        break
    best["gain"] = round(100.0 * len(new) / N, 1)
    covered |= best["ids"]
    chosen.append(best)
    left.remove(best)
for k, it in enumerate(chosen, 1):
    it["id"] = "F%d" % k


def title_of(it):
    base = THEME_FIX[it["theme"]]["title"]
    if it.get("lead"):
        return "%s: %s" % (base, it["lead"][:1].lower() + it["lead"][1:])
    if it.get("rest"):
        return "%s, remaining cases" % base
    return base


def effort_of(it):
    return THEME_FIX[it["theme"]]["effort"]


def clause(text, n=140):
    t = sig.short(text, 500)
    m = re.match(r"(.+?[.!?])(\s|$)", t)
    t = m.group(1) if m else t
    if len(t) > n:
        cut = max(t.rfind(",", 0, n), t.rfind(";", 0, n))
        t = t[:cut] if cut > 40 else t[:n].rsplit(" ", 1)[0]
    t = t.strip(" ,;:-")
    return t[:1].upper() + t[1:] + ("" if t[-1:] in ".!?" else ".")


def ask_bit(r, n=70):
    t = sig.short(r["first_ask"], 400).replace('"', "")
    t = " ".join(w for w in t.split() if not re.search(r"\d{3,}|[#$@]", w)).strip(" .?!,;:-")
    return t[:n].rsplit(" ", 1)[0].strip(" .?!,;:-") if len(t) > n else t


def evidence(it, k=3):
    ids = sorted(it["ids"], key=lambda i: (BY[i]["label"] != "bad", -BY[i]["conf"], len(BY[i]["rationale"]), i))
    out, seen = [], set()
    for i in ids:
        key = clause(BY[i]["rationale"], 90)[:40]
        if key in seen:
            continue
        seen.add(key)
        out.append(i)
        if len(out) == k:
            break
    return out


def words(s):
    return len(re.findall(r"\w+", re.sub(r"<[^>]+>", " ", s)))


# ---- fix reports (standalone, shell look)
try:
    with open(os.path.join(STUDY, "shell.css"), encoding="utf-8") as f:
        CSS = f.read()
except OSError:
    CSS = ""
RP = os.path.join(STUDY, "fix_reports")
os.makedirs(RP, exist_ok=True)
for fn in os.listdir(RP):  # a re-run replaces the reports instead of leaving old ones behind
    if re.match(r"F\d+\.html$", fn):
        os.remove(os.path.join(RP, fn))
REPORT_CSS = (".rep{max-width:46rem;margin:0 auto;padding:9vh 5vw 80px}.rep h1{font-weight:300;font-size:clamp(24px,5vw,36px);letter-spacing:-.03em;line-height:1.12;margin:.2em 0 .6em}"
              ".rep h3{margin:1.6em 0 .3em}.rep p,.rep li{color:var(--soft)}.rid{font-family:var(--mono);font-size:11px;color:var(--muted)}")
for it in chosen:
    base = THEME_FIX[it["theme"]]
    scope = "the runs of this theme" if it["topics"] is None else "the runs under %s" % ("this topic" if it.get("lead") else "the remaining topics")
    problem = "%s %d runs fell short here (%d bad, %d ambiguous)." % (base["problem"], len(it["ids"]), it["bad"], it["amb"])
    ev = evidence(it)
    while True:
        lis = "".join('<li><span class="rid">%s</span> %s: %s &rarr; %s</li>' % (
            e(rid(i)), e(BY[i]["label"]), e(ask_bit(BY[i])), e(clause(BY[i]["rationale"], 110))) for i in ev)
        body = ('<p class="kicker">Fix %s &middot; effort %s (analyst judgement)</p><h1>%s</h1>'
                '<p>Touches %d runs; %d fell short and are addressable. Best case: up to %.1f points more good runs, a bound and not a forecast.</p>'
                '<h3>Problem</h3><p>%s</p><h3>Evidence</h3><ul>%s</ul><h3>Change</h3><p>%s</p><h3>How to tell it worked</h3><p>%s</p>'
                '<p><a href="../study.html#impact">Back to the study</a></p>' % (
                    it["id"], effort_of(it), e(title_of(it)), len(it["touched"]), len(it["ids"]), it["gain"], e(problem), lis,
                    e(base["change"]), e(base["measure"])))
        if words(body) <= REPORT_WORDS - 10 or len(ev) <= 1:
            break
        ev = ev[:-1]
    page = ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>%s %s</title><style>%s\n%s</style></head><body><main class="rep">%s</main></body></html>' % (it["id"], e(title_of(it)), CSS, REPORT_CSS, body))
    it["report_words"] = words(body)
    with open(os.path.join(RP, it["id"] + ".html"), "w", encoding="utf-8") as f:
        f.write(page)

# ---- json
combined = round(100.0 * len(covered) / N, 1)
J = dict(n_runs=N, n_fell_short=len(SHORT), combined_gain_pts=combined,
         fixes=[dict(id=it["id"], title=title_of(it), theme=it["theme"], runs=len(it["touched"]), addressable=len(it["ids"]), bad=it["bad"],
                     ambiguous=it["amb"], gain_pts=it["gain"], gain_alone_pts=it["alone"], effort=effort_of(it)) for it in chosen])
with open(os.path.join(H, "impact.json"), "w", encoding="utf-8") as f:
    json.dump(J, f, indent=1)

# ---- section html
if chosen:
    top = chosen[0]
    head = "%d fixes could move up to %.1f points of runs from short to good; the largest, \u201c%s\u201d, touches %d runs and could add %.1f points." % (
        len(chosen), combined, title_of(top), len(top["touched"]), top["gain"])
else:
    head = "Every run reached its goal, so no fix is needed."
mx = max([len(it["touched"]) for it in chosen] or [1])
trs = []
for it in chosen:
    w = 100.0 * len(it["ids"]) / mx
    bw = 100.0 * it["bad"] / (len(it["ids"]) or 1)
    trs.append('<tr><td data-l="Fix"><a href="fix_reports/%s.html">%s %s</a></td><td data-l="Runs touched"><b>%d</b></td>'
               '<td data-l="Short runs addressable"><b>%d</b><span class="sb" style="width:%.1f%%"><i style="width:%.1f%%;background:var(--bad)"></i>'
               '<i style="width:%.1f%%;background:var(--muted)"></i></span><small>%d bad &middot; %d ambiguous</small></td>'
               '<td data-l="Best-case gain"><b>+%.1f</b> pts</td><td data-l="Effort">%s</td></tr>' % (
                   it["id"], it["id"], e(title_of(it)), len(it["touched"]), len(it["ids"]), w, bw, 100 - bw, it["bad"], it["amb"], it["gain"], effort_of(it)))
CSS_S = """<style>
#impact{padding-top:9vh}
#impact .sb{display:flex;height:5px;background:var(--line);margin:6px 0 4px}#impact .sb i{display:block;height:100%}
#impact small{color:var(--muted);font-size:11.5px;display:block}
#impact table td{font-size:14px;color:var(--soft)}#impact td b{font-size:20px;font-weight:300;letter-spacing:-.03em;color:var(--ink)}
#impact td a{color:var(--ink);text-decoration:underline;text-decoration-color:var(--green);text-underline-offset:3px}
#impact td:nth-child(1){width:38%}
@media(max-width:700px){#impact .rs thead{display:none}#impact .rs,#impact .rs tbody,#impact .rs tr,#impact .rs td{display:block;width:100%!important}
#impact .rs tr{padding:12px 0;border-bottom:1px solid var(--line)}#impact .rs td{border:0;padding:3px 0}
#impact .rs td::before{content:attr(data-l);display:block;font-size:10px;letter-spacing:.16em;text-transform:uppercase;color:var(--muted)}}
</style>"""
fig = ('<figure class="fig"><figcaption><span class="kicker">Figure</span> Fixes ranked by new good runs they could add; gain is a best case, effort is analyst judgement (n=%d runs).</figcaption>'
       '<div class="wrap"><table class="tbl rs"><thead><tr><th>Fix (opens its report)</th><th>Runs touched</th><th>Short runs addressable</th><th>Best-case gain</th><th>Effort</th></tr></thead>'
       '<tbody>%s</tbody></table></div></figure>' % (N, "".join(trs))) if chosen else ""
page = (CSS_S + '<section id="impact" class="step">\n<p class="kicker">Section 5 &middot; Impact</p>\n<h2 class="big">Impact</h2>\n'
        '<p class="sub">Which fixes would move the most short runs to good, and at what effort?</p>\n<p class="lede">%s</p>\n%s</section>' % (e(head), fig))
with open(os.path.join(H, "impact.html"), "w", encoding="utf-8") as f:
    f.write(page)
md = "## Impact\n\n%s\n\n" % head + "".join(
    "- %s %s: touches %d runs, %d short runs addressable (%d bad, %d ambiguous), up to +%.1f points, effort %s\n" % (
        it["id"], title_of(it), len(it["touched"]), len(it["ids"]), it["bad"], it["amb"], it["gain"], effort_of(it)) for it in chosen)
md += "\nGains have overlaps removed and are a bound, not a forecast; effort is analyst judgement.\n"
with open(os.path.join(H, "impact.md"), "w", encoding="utf-8") as f:
    f.write(md)
print(head)
for it in chosen:
    print("%s %-70s touched=%3d addr=%3d gain=%4.1f effort=%s words=%d" % (it["id"], title_of(it)[:70], len(it["touched"]), len(it["ids"]), it["gain"], effort_of(it), it["report_words"]))
