"""Affinity Diagram. Re-run: python3 -I sections/affinity.py

STARTER, NOT THE ANSWER. The host agent reads the runs that fell short (rationales, tool errors, asks), then EDITS this file and
aff_rules.py until the themes, topics and issue labels say what is true for THIS dataset. Keep the architecture: general theme
clusters first, then the topic diagram, then every distinct issue in one closed list; every run that fell short gets at least
one row in issues.csv; every issue and every topic belongs to exactly one theme.

Reads sig (runs, the graders' words) and cig/run_dossiers.jsonl (names of failed tool calls). Writes affinity.html, affinity.md,
affinity.json and issues.csv (run_id,issue,topic,theme) next to this file.
"""
import csv
import html
import json
import os
import re
import sys
from collections import Counter, defaultdict

H = os.path.dirname(os.path.abspath(__file__))
STUDY = os.path.dirname(H)
sys.path.insert(0, STUDY)
sys.path.insert(0, H)
import sig  # noqa: E402
import aff_rules as A  # noqa: E402

e = html.escape
R = sig.load()
OUT = sig.resolve_out()
N_ALL = len(R)
X = [r for r in R if r["outcome"] != 1]
BY = {r["run_id"]: r for r in R}
MAX_TOPICS_PER_THEME = 7


def uid_len(ids):
    n = 8
    while len({i[:n] for i in ids}) < len(set(ids)) and n < 40:
        n += 1
    return n


RL = uid_len([r["run_id"] for r in R])


def rid(x):
    return x[:RL]


def toks(s):
    out = []
    for w in re.findall(r"[a-z]+", str(s).lower()):
        if w in A.STOP or len(w) < 3:
            continue
        if len(w) > 4:
            for suf in ("ing", "ed", "es", "s"):
                if w.endswith(suf) and len(w) - len(suf) >= 3:
                    w = w[:-len(suf)]
                    break
        out.append(w)
    return out


def cap(s):
    s = " ".join(str(s).split()).strip(" .;:,-")
    return s[:1].upper() + s[1:]


def humanize(name):
    return re.sub(r"[_\-.]+", " ", str(name)).strip().lower()


# ---- failed tool calls per run (names only; text from calls is never shown)
FAILED = defaultdict(Counter)
want = {r["run_id"] for r in X}
try:
    with open(os.path.join(OUT, "cig", "run_dossiers.jsonl"), encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            d = json.loads(line)
            if d.get("run_id") in want:
                for c in A.failed_calls(d):
                    FAILED[d["run_id"]][humanize(c.get("name") or "a tool")] += 1
except OSError:
    pass


# ---- issue labels: the graders' own, cleaned, with near-duplicates merged
def clean_issue(s):
    s = re.sub(r"[^A-Za-z0-9,'/ -]+", " ", str(s or ""))
    ws = s.split()
    return cap(" ".join(ws[:9])) if ws else ""


raw_labels = Counter(clean_issue(r["issue_raw"]) for r in X if clean_issue(r["issue_raw"]))
CANON = {}
for lab, _ in raw_labels.most_common():
    ts = set(toks(lab))
    hit = None
    for c in set(CANON.values()):
        cs = set(toks(c))
        if ts and cs and len(ts & cs) / len(ts | cs) >= 0.7:
            hit = c
            break
    CANON[lab] = hit or lab


def label_for(r):
    return CANON.get(clean_issue(r["issue_raw"]), "")


# ---- primary and extra themes per run
PRIM = {r["run_id"]: A.primary_theme(r) for r in X}
votes = defaultdict(Counter)
for r in X:
    lab = label_for(r)
    if lab:
        votes[lab][PRIM[r["run_id"]]] += 1
HOME = {lab: sorted(c.items(), key=lambda kv: (-kv[1], A.THEMES.index(kv[0])))[0][0] for lab, c in votes.items()}


def rule_issue(r, theme):
    if theme == A.T_GAP:
        ph = A.ask_phrase(r)
        return "Asked for: " + ph if ph else A.RULE_LABEL[theme]
    if theme == A.T_PILE and FAILED.get(r["run_id"]):
        return "%s: %s" % (A.RULE_LABEL[theme], FAILED[r["run_id"]].most_common(1)[0][0])
    return A.RULE_LABEL[theme]


rows, seen, issue_theme = [], set(), {}


def add(r, issue, theme):
    issue = cap(issue) or A.RULE_LABEL[theme]
    if issue_theme.setdefault(issue, theme) != theme:
        issue = "%s (%s)" % (issue, A.THEME_SHORT[theme].lower())
        issue_theme.setdefault(issue, theme)
    if (r["run_id"], issue) in seen:
        return
    seen.add((r["run_id"], issue))
    rows.append([r["run_id"], issue, "", theme])


for r in X:
    th = PRIM[r["run_id"]]
    lab = label_for(r)
    add(r, lab if lab and HOME.get(lab) == th else rule_issue(r, th), th)
    for t2 in A.extra_themes(r, th):
        add(r, rule_issue(r, t2), t2)

# ---- topics: issues of one theme that share an informative word are one topic (single link)
THEME_ISSUES = defaultdict(lambda: Counter())
for rid_, issue, _, th in rows:
    THEME_ISSUES[th][issue] += 1
TOPIC_OF = {}
used_topics = {}
for th in A.THEMES:
    iss = THEME_ISSUES.get(th)
    if not iss:
        continue
    names = list(iss)
    parent = {i: i for i in names}

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    df = Counter(t for i in names for t in set(toks(i)))
    cap_df = max(2, int(0.5 * len(names))) if len(names) > 4 else len(names)
    first = {}
    for i in names:
        for t in set(toks(i)):
            if 2 <= df[t] <= min(cap_df, 5):
                if t in first:
                    parent[find(i)] = find(first[t])
                else:
                    first[t] = i
    for a in names:  # or two issues that mostly use the same words
        for b in names:
            ta, tb = set(toks(a)), set(toks(b))
            if a < b and ta and tb and len(ta & tb) / float(len(ta | tb)) >= 0.5:
                parent[find(a)] = find(b)
    groups = defaultdict(list)
    for i in names:
        groups[find(i)].append(i)
    ordered = sorted(groups.values(), key=lambda g: (-sum(iss[i] for i in g), g[0]))
    named = []
    for g in ordered:
        g.sort(key=lambda i: (-iss[i], i))
        tp = g[0]
        if tp in used_topics and used_topics[tp] != th:
            tp = "%s (%s)" % (tp, A.THEME_SHORT[th].lower())
        used_topics[tp] = th
        named.append((tp, g))
    if len(named) > MAX_TOPICS_PER_THEME:
        keep, rest = named[:MAX_TOPICS_PER_THEME - 1], named[MAX_TOPICS_PER_THEME - 1:]
        tail = "Scattered one-off cases (%s)" % A.THEME_SHORT[th].lower()
        used_topics[tail] = th
        named = keep + [(tail, [i for _, g in rest for i in g])]
    for tp, g in named:
        for i in g:
            TOPIC_OF[(th, i)] = tp
for row in rows:
    row[2] = TOPIC_OF[(row[3], row[1])]
rows.sort(key=lambda x: (A.THEMES.index(x[3]), x[2], x[1], x[0]))
assert {x[0] for x in rows} == {r["run_id"] for r in X}, "a run that fell short has no row"


# ---- aggregates
def split(ids):
    b = sum(1 for i in ids if BY[i]["label"] == "bad")
    return b, len(ids) - b


def example(ids):
    return sorted(ids, key=lambda i: (BY[i]["label"] != "bad", -BY[i]["conf"], len(BY[i]["rationale"]), i))[0]


def group(keyf):
    g = defaultdict(set)
    for row in rows:
        g[keyf(row)].add(row[0])
    return g


G_theme, G_topic, G_issue = group(lambda x: x[3]), group(lambda x: (x[3], x[2])), group(lambda x: (x[3], x[2], x[1]))
themes, topics, issues = [], [], []
for th in A.THEMES:
    ids = G_theme.get(th)
    if not ids:
        continue
    b, am = split(ids)
    themes.append(dict(theme=th, short=A.THEME_SHORT[th], runs=len(ids), bad=b, ambiguous=am, example=example(ids),
                       share_pct=sig.pct(len(ids), len(X))))
th_order = [t["theme"] for t in themes]
for (th, tp), ids in sorted(G_topic.items(), key=lambda kv: (th_order.index(kv[0][0]), -len(kv[1]), kv[0][1])):
    b, am = split(ids)
    topics.append(dict(topic=tp, theme=th, runs=len(ids), bad=b, ambiguous=am))
for (th, tp, i), ids in sorted(G_issue.items(), key=lambda kv: (th_order.index(kv[0][0]), -len(kv[1]), kv[0][2])):
    b, am = split(ids)
    issues.append(dict(issue=i, topic=tp, theme=th, runs=len(ids), bad=b, ambiguous=am, example=rid(example(ids))))
multi = sum(1 for ids in [{x[3] for x in rows if x[0] == r["run_id"]} for r in X] if len(ids) > 1)

with open(os.path.join(H, "issues.csv"), "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["run_id", "issue", "topic", "theme"])
    w.writerows(rows)

nb = sum(1 for r in X if r["label"] == "bad")
J = dict(n_runs=N_ALL, n_runs_fell_short=len(X), n_bad=nb, n_ambiguous=len(X) - nb, n_issues=len(issues), n_topics=len(topics),
         runs_in_2plus_themes=multi,
         themes=[dict(t, example=rid(t["example"])) for t in themes],
         topics=topics, issues=issues)
with open(os.path.join(H, "affinity.json"), "w", encoding="utf-8") as f:
    json.dump(J, f, indent=1)


# ---- words
def lede():
    if not X:
        return "Every run in this study reached its goal, so there is nothing to group."
    top = sorted(themes, key=lambda t: -t["runs"])
    sh = lambda t: A.THEME_SHORT[t["theme"]].lower()
    s = "%d runs fell short; the largest group, %s, holds %d (%d%%)" % (len(X), sh(top[0]), top[0]["runs"], top[0]["share_pct"])
    if len(top) > 1:
        s += ", then %s with %d" % (sh(top[1]), top[1]["runs"])
    if len(top) > 2:
        s += " and %s with %d" % (sh(top[2]), top[2]["runs"])
    return s + "."


def clause(text, n=120):
    t = sig.short(text, 400)
    m = re.match(r"(.+?[.!?])(\s|$)", t)
    t = m.group(1) if m else t
    if len(t) > n:
        t = t[:n].rsplit(" ", 1)[0].rstrip(" ,;:-") + "."
    return t


def ask_text(r, n=60):
    t = sig.short(r["first_ask"], 400).replace('"', "")
    t = " ".join(w for w in t.split() if not re.search(r"\d{3,}|[#$@]", w)).strip(" '")
    if len(t) > n:
        t = t[:n].rsplit(" ", 1)[0].rstrip(" ,;:-?!.")
    return t


def bar(b, am, scale=100.0):
    n = (b + am) or 1
    return ('<span class="sb" style="width:%.1f%%"><i style="width:%.1f%%;background:var(--bad)"></i>'
            '<i style="width:%.1f%%;background:var(--muted)"></i></span>' % (scale, 100.0 * b / n, 100.0 * am / n))


# ---- theme table (general clusters first)
def theme_table():
    mx = max(t["runs"] for t in themes) or 1
    o = ['<div class="wrap"><table class="tbl rs"><thead><tr><th>Theme</th><th>Runs</th><th>Bad / ambiguous</th><th>One example</th></tr></thead><tbody>']
    for t in themes:
        r = BY[t["example"]]
        ex = "&ldquo;%s&rdquo; &rarr; %s <span class=\"rid\">%s</span>" % (e(ask_text(r)), e(clause(r["rationale"])), e(rid(r["run_id"])))
        o.append('<tr><td data-l="Theme"><b>%s</b></td><td data-l="Runs"><b class="nn">%d</b> <small>%d%%</small></td>'
                 '<td data-l="Bad / ambiguous">%s<small>%d bad &middot; %d ambiguous</small></td><td data-l="Example">%s</td></tr>'
                 % (e(t["theme"]), t["runs"], t["share_pct"], bar(t["bad"], t["ambiguous"], 100.0 * t["runs"] / mx), t["bad"], t["ambiguous"], ex))
    return "".join(o) + "</tbody></table></div>"


# ---- topic diagram (SVG, boxes sized by runs, grouped under their theme); a wide and a narrow layout
def wrap_text(s, cpl, maxlines=3):
    words, lines, cur = s.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > cpl:
            lines.append(cur)
            cur = w
        else:
            cur = (cur + " " + w).strip()
    if cur:
        lines.append(cur)
    return lines


def topic_svg(W, cls, minw):
    gap, pad = 6, 8
    mx = max(sum(t2["runs"] for t2 in topics if t2["theme"] == t["theme"]) for t in themes) or 1
    y, parts = 4, []
    for t in themes:
        parts.append('<text class="tb" x="0" y="%d">%s</text>' % (y + 12, e(t["theme"])))
        parts.append('<text class="tm" x="0" y="%d">%d %s &#183; %d bad &#183; %d ambiguous</text>' % (y + 27, t["runs"], "run" if t["runs"] == 1 else "runs", t["bad"], t["ambiguous"]))
        y += 36
        items = [t2 for t2 in topics if t2["theme"] == t["theme"]]
        want_w = [min(W, max(minw, t2["runs"] / mx * W)) for t2 in items]
        i = 0
        while i < len(items):
            j, tot = i, 0.0
            while j < len(items) and (j == i or tot + want_w[j] + gap * (j - i) <= W):
                tot += want_w[j]
                j += 1
            f = (W - gap * (j - i - 1)) / tot
            lay = []
            for k in range(i, j):
                bw = want_w[k] * f
                lines = wrap_text(items[k]["topic"], max(8, int((bw - 2 * pad) / 5.7)))
                lay.append((k, bw, lines))
            rh = 26 + 13 * max(len(ln) for _, _, ln in lay) + 8
            x = 0.0
            for k, bw, lines in lay:
                it = items[k]
                parts.append('<rect x="%.1f" y="%d" width="%.1f" height="%d" fill="none" stroke="var(--line)"/>' % (x, y, bw, rh))
                parts.append('<text class="tb" x="%.1f" y="%d">%d</text>' % (x + pad, y + 18, it["runs"]))
                for li, ln in enumerate(lines):
                    parts.append('<text class="t" x="%.1f" y="%d">%s</text>' % (x + pad, y + 33 + 13 * li, e(ln)))
                n = it["runs"] or 1
                bwid = (bw - 2 * pad)
                parts.append('<rect x="%.1f" y="%d" width="%.1f" height="3" fill="var(--bad)"/>' % (x + pad, y + rh - 7, bwid * it["bad"] / n))
                parts.append('<rect x="%.1f" y="%d" width="%.1f" height="3" fill="var(--muted)"/>' % (x + pad + bwid * it["bad"] / n, y + rh - 7, bwid * it["ambiguous"] / n))
                x += bw + gap
            y += rh + gap
            i = j
        y += 14
    return ('<svg class="%s" viewBox="0 0 %d %d" role="img" aria-label="Topic clusters grouped by theme, box area follows runs">%s</svg>'
            % (cls, W, y, "".join(parts)))


def issue_table():
    o = ['<div class="wrap"><table class="tbl"><thead><tr><th>Issue</th><th>Runs</th><th>Bad / ambiguous</th><th>Example run</th></tr></thead><tbody>']
    cur = None
    for d in issues:
        if d["theme"] != cur:
            cur = d["theme"]
            o.append('<tr class="g"><td colspan="4">%s</td></tr>' % e(cur))
        o.append('<tr><td>%s</td><td>%d</td><td>%d / %d</td><td class="rid">%s</td></tr>' % (e(d["issue"]), d["runs"], d["bad"], d["ambiguous"], e(d["example"])))
    return "".join(o) + "</tbody></table></div>"


CSS = """<style>
#affinity{padding-top:9vh}
#affinity .sb{display:flex;height:4px;background:var(--line);margin:8px 0 4px}#affinity .sb i{display:block;height:100%}
#affinity small{color:var(--muted);font-size:11.5px}
#affinity .nn{font-size:22px;font-weight:300;letter-spacing:-.03em}
#affinity .rid{font-family:var(--mono);font-size:10.5px;color:var(--muted)}
#affinity .rs td{font-size:13px;color:var(--soft)}#affinity .rs td b{font-weight:400;color:var(--ink)}
#affinity .rs td:nth-child(1){width:20%}#affinity .rs td:nth-child(2){width:9%;white-space:nowrap}#affinity .rs td:nth-child(3){width:20%;padding-right:24px}
#affinity table.tbl tr.g td{color:var(--green);font-size:10px;letter-spacing:.16em;text-transform:uppercase;padding-top:16px;border-bottom:1px solid var(--strong)}
#affinity svg.narrow{display:none}
@media(max-width:700px){#affinity svg.wide{display:none}#affinity svg.narrow{display:block}
#affinity .rs thead{display:none}#affinity .rs,#affinity .rs tbody,#affinity .rs tr,#affinity .rs td{display:block;width:100%!important}
#affinity .rs tr{padding:12px 0;border-bottom:1px solid var(--line)}#affinity .rs td{border:0;padding:3px 0}
#affinity .rs td::before{content:attr(data-l);display:block;font-size:10px;letter-spacing:.16em;text-transform:uppercase;color:var(--muted)}}
</style>"""

if X:
    body = ('<figure class="fig"><figcaption><span class="kicker">Figure</span> Ways users were badly served; a run can sit in several themes (n=%d).</figcaption>%s</figure>'
            '<figure class="fig"><figcaption><span class="kicker">Figure</span> Topic clusters under each theme; box area follows runs (n=%d).</figcaption>%s%s</figure>'
            '<details class="dd"><summary>All %d distinct issues</summary>%s</details>'
            % (len(X), theme_table(), len(X), topic_svg(1000, "wide", 84), topic_svg(380, "narrow", 112), len(issues), issue_table()))
else:
    body = ""
HEAD = lede()
page = (CSS + '<section id="affinity" class="step">\n<p class="kicker">Section 3 &middot; Affinity Diagram</p>\n<h2 class="big">Affinity Diagram</h2>\n'
        '<p class="sub">Which kinds of failure recur, and which runs belong to each?</p>\n<p class="lede">%s</p>\n%s</section>\n' % (e(HEAD), body))
with open(os.path.join(H, "affinity.html"), "w", encoding="utf-8") as f:
    f.write(page)
md = "## Affinity Diagram\n\n%s\n\n" % HEAD
md += "".join("- %s: %d runs (%d bad, %d ambiguous)\n" % (t["theme"], t["runs"], t["bad"], t["ambiguous"]) for t in themes)
md += "\n" + "".join("- %s / %s: %d runs\n" % (t["theme"], t["topic"], t["runs"]) for t in topics)
md += "\n%d distinct issues are listed in issues.csv.\n" % len(issues)
with open(os.path.join(H, "affinity.md"), "w", encoding="utf-8") as f:
    f.write(md)
print(HEAD)
for t in themes:
    print("%4d %3d %3d  %s" % (t["runs"], t["bad"], t["ambiguous"], t["theme"]))
print("%d issues, %d topics, %d rows, %d runs in several themes" % (len(issues), len(topics), len(rows), multi))
