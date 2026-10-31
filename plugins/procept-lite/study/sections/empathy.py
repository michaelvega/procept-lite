"""Empathy Map. Re-run: python3 -I sections/empathy.py

STARTER, NOT THE ANSWER. The host agent reads the first asks of the runs, then EDITS this file for THIS dataset: the clustering
below is only a sturdy first pass (normalised first-ask words and tool names, the graders' persona labels used as seeds and
merged by similarity). Edit the cluster logic, or override the words in PERSONA_EDITS, until every persona is a real user goal
in full words. Keep the architecture and the contract:
  - a persona is a group of RUNS defined by the user's goal (never by outcome, tool or environment);
  - name 2-4 words; goal one full sentence; pain one full sentence; never blank, never cut off;
  - runs this pass cannot place go to "Other requests" (at most 15% of the runs, never the biggest group);
  - follow-ups ("thanks", "that is wrong", "try again") take the persona of their conversation;
  - every graded usable run appears exactly once in personas.csv.

Reads sig only. Writes empathy.html, empathy.md, empathy.json, personas.csv (run_id,persona), personas.json next to this file.
"""
import csv
import html
import json
import math
import os
import re
import sys
from collections import Counter, defaultdict

H = os.path.dirname(os.path.abspath(__file__))
STUDY = os.path.dirname(H)
sys.path.insert(0, STUDY)
import sig  # noqa: E402

e = html.escape
R = sig.load()
N = len(R)
OTHER_NAME = "Other requests"
OTHER_MAX = 0.14            # leave a margin under the 15% rule
MAX_PERSONAS = 6
MERGE_SIM = 0.5             # two groups this alike (asks and tools) are one goal when there are more than 4
MIN_PLACE = 0.08            # least similarity for a run to be placed in a group
# Edit per dataset: {auto name: {"name": ..., "goal": ..., "pain": ..., "gain": ...}}. Any key left out keeps the starter wording.
PERSONA_EDITS = {}

STOP = set("a an the and or of to in on for with is are was were be been it its this that these those my your our i you we me us do does did can could would "
           "should will please just about from at as by if so how what which who where when why there their them they have has had not no yes hi hello hey get got "
           "need want like know tell let make help also any some into out up one two within around near based intent show this there data about".split())
OTHER_WORDS = {"other", "others", "unclear", "unknown", "misc", "miscellaneous", "unclassified", "uncategorized", "uncategorised",
               "undefined", "unspecified", "general", "generic", "various", "mixed"}
FOLLOW = re.compile(r"^\W*(yes|yeah|yep|ok|okay|sure|no|nope|thanks|thank you|thx|please|go ahead|proceed|continue|do it|try again|again|"
                    r"that'?s (wrong|not|incorrect)|that is (wrong|not|incorrect)|you didn'?t|never ?mind|great|perfect|got it|fine|cool)\b", re.I)


def stem(w):
    if len(w) > 4:
        for suf in ("ing", "ed", "es", "s"):
            if w.endswith(suf) and len(w) - len(suf) >= 3:
                return w[:-len(suf)]
    return w


def toks(s):
    return [stem(w) for w in re.findall(r"[a-z]+", str(s).lower()) if w not in STOP and len(w) >= 3]


def norm_label(s):
    return " ".join(re.findall(r"[a-z]+", str(s or "").lower()))


def cap(s):
    s = " ".join(str(s).split()).strip(" .;:,-")
    return s[:1].upper() + s[1:]


def uid_len(ids):
    n = 8
    while len({i[:n] for i in ids}) < len(set(ids)) and n < 40:
        n += 1
    return n


RL = uid_len([r["run_id"] for r in R])


# ---------------------------------------------------------------- vectors (asks + tool names), sparse and unit length
def is_follow(r):
    a = r["first_ask"] or ""
    return len(toks(a)) <= 1 or (bool(FOLLOW.match(a)) and len(a.split()) <= 12)


SUBST = [r for r in R if not is_follow(r)]
SUB_SET = {r["run_id"] for r in SUBST}
DF = Counter()
for r in SUBST:
    DF.update(set(["w:" + t for t in toks(r["first_ask"])] + ["t:" + t for t in set(r["tools"])]))


def vec(r):
    v = Counter(["w:" + t for t in toks(r["first_ask"])])
    for t in set(r["tools"]):
        v["t:" + t] += 1
    w = {k: c * math.log(1.0 + (len(SUBST) + 1.0) / (DF.get(k, 0) + 1.0)) for k, c in v.items()}
    n = math.sqrt(sum(x * x for x in w.values())) or 1.0
    return {k: x / n for k, x in w.items()}


def cos(a, b):
    if len(a) > len(b):
        a, b = b, a
    return sum(x * b.get(k, 0.0) for k, x in a.items())


def centroid(vs):
    c = defaultdict(float)
    for v in vs:
        for k, x in v.items():
            c[k] += x
    n = math.sqrt(sum(x * x for x in c.values())) or 1.0
    return {k: x / n for k, x in c.items()}


V = {r["run_id"]: vec(r) for r in R}
SUB_IDS = [r["run_id"] for r in SUBST]
BY = {r["run_id"]: r for r in R}


def kmeans_seeds(k, ids=None):
    """Deterministic farthest-first start, a few rounds of nearest-centre reassignment."""
    ids = sorted(ids if ids is not None else SUB_IDS)
    if not ids:
        return []
    cs = [V[ids[0]]]
    while len(cs) < min(k, len(ids)):
        far = min(ids, key=lambda i: (max(cos(V[i], c) for c in cs), i))
        cs.append(V[far])
    gs = []
    for _ in range(6):
        gs = [[] for _ in cs]
        for i in ids:
            gs[max(range(len(cs)), key=lambda j: (cos(V[i], cs[j]), -j))].append(i)
        cs = [centroid([V[i] for i in g]) if g else c for g, c in zip(gs, cs)]
    return [g for g in gs if g]


# ---------------------------------------------------------------- seeds: the graders' labels, else k-means
lab_runs = defaultdict(list)
for r in SUBST:
    k = norm_label(r["persona_raw"])
    if k:
        lab_runs[k].append(r["run_id"])
seeds = [ids for ids in lab_runs.values() if len(ids) >= 2]
if len(seeds) < 2 or sum(len(s) for s in seeds) < 0.5 * len(SUBST):
    seeds = kmeans_seeds(max(2, min(5, len(SUBST) // 8)))
groups = [list(s) for s in seeds]


def cents():
    return [centroid([V[i] for i in g]) for g in groups]


def merge_pass():
    while len(groups) > 1:
        cs_ = cents()
        best = max(((cos(cs_[i], cs_[j]), i, j) for i in range(len(cs_)) for j in range(i + 1, len(cs_))), key=lambda t: (t[0], -t[1], -t[2]))
        if len(groups) > MAX_PERSONAS or (len(groups) > 4 and best[0] >= MERGE_SIM):
            groups[best[1]] += groups.pop(best[2])
        else:
            return


merge_pass()
tiny = max(3, int(0.04 * len(SUBST)))  # very small seed groups fold into their nearest neighbour first
for g in sorted([g for g in groups if len(g) < tiny and len(groups) > 2], key=len):
    cs_ = [centroid([V[i] for i in x]) for x in groups if x is not g]
    host = [x for x in groups if x is not g]
    j = max(range(len(host)), key=lambda k: (cos(centroid([V[i] for i in g]), cs_[k]), -k))
    host[j].extend(g)
    groups.remove(g)
while groups and len(groups) < MAX_PERSONAS:  # a group holding most runs is not one goal: split it by what was asked
    big = max(groups, key=len)
    if len(big) <= 0.4 * len(SUBST) or len(big) < 12:
        break
    parts = [g for g in kmeans_seeds(min(4, MAX_PERSONAS - len(groups) + 1), big) if len(g) >= 3]
    if len(parts) < 2:
        break
    groups.remove(big)
    groups.extend(parts)
for _ in range(3):  # every substantive run moves to its nearest group
    cs_ = cents()
    if not cs_:
        break
    new = [[] for _ in cs_]
    for i in SUB_IDS:
        new[max(range(len(cs_)), key=lambda j: (cos(V[i], cs_[j]), -j))].append(i)
    groups[:] = [g for g in new if g]
merge_pass()

# ---------------------------------------------------------------- placement, small groups, the Other bucket
cs = cents()
place = {}
for gi, g in enumerate(groups):
    for i in g:
        place[i] = gi
min_size = max(3, int(0.02 * N))
small = {gi for gi, g in enumerate(groups) if len(g) < min_size}
other = []
for gi in sorted(small):
    for i in groups[gi]:
        cands = [(cos(V[i], cs[j]), j) for j in range(len(cs)) if j not in small]
        s, j = max(cands, key=lambda t: (t[0], -t[1])) if cands else (0.0, -1)
        if j >= 0 and s >= MIN_PLACE:
            place[i] = j
        else:
            place.pop(i)
            other.append(i)
for i in sorted(SUB_IDS):  # a run that sits far from its own group goes to Other
    if i in place and cos(V[i], cs[place[i]]) < MIN_PLACE:
        place.pop(i)
        other.append(i)
alive = sorted({place[i] for i in place})
remap = {gi: k for k, gi in enumerate(alive)}
place = {i: remap[g] for i, g in place.items()}
cs = [cs[g] for g in alive]
if not cs and SUB_IDS:  # nothing separable: one group for all of them, named from its words
    cs = [centroid([V[i] for i in SUB_IDS])]
    place = {i: 0 for i in SUB_IDS}
    other = []

# follow-ups take their conversation's persona (majority of its substantive runs), else their label's, else the nearest group
conv = defaultdict(list)
for r in R:
    conv[r["conversation_id"]].append(r["run_id"])
label_votes = defaultdict(Counter)
for i, g in place.items():
    k = norm_label(BY[i]["persona_raw"])
    if k:
        label_votes[k][g] += 1
for r in R:
    i = r["run_id"]
    if i in place or i in other:
        continue
    votes = Counter(place[j] for j in conv[r["conversation_id"]] if j in place)
    if not votes:
        votes = label_votes.get(norm_label(r["persona_raw"])) or Counter()
    if votes:
        place[i] = sorted(votes.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
    elif cs and r["tools"]:
        s, nj = max((cos(V[i], c), -k) for k, c in enumerate(cs))
        if s >= MIN_PLACE:
            place[i] = -nj
        else:
            other.append(i)
    else:
        other.append(i)

# keep Other under the cap and never the biggest group: the runs closest to a real group join it first
other = sorted(set(other), key=lambda i: (-max([cos(V[i], c) for c in cs] or [0.0]), i))
while cs and len(other) > OTHER_MAX * N:
    i = other.pop(0)
    place[i] = max(range(len(cs)), key=lambda j: (cos(V[i], cs[j]), -j))
if cs and other and (len(other) < 3 or len(other) >= max(Counter(place.values()).values() or [0])):
    for i in other:
        place[i] = max(range(len(cs)), key=lambda j: (cos(V[i], cs[j]), -j))
    other = []
if not cs:  # no structure at all: a single group for everything
    place = {r["run_id"]: 0 for r in R}
    other = []
members = defaultdict(list)
for i in place:
    members[place[i]].append(i)


# ---------------------------------------------------------------- words (all from the runs)
def central(texts):
    """The text most like the others (word overlap): the representative one."""
    texts = [t for t in texts if t][:120]
    if not texts:
        return ""
    sets = [set(toks(t)) or {t} for t in texts]
    sc = [sum(len(s & o) / float(len(s | o)) for o in sets) for s in sets]
    return texts[max(range(len(texts)), key=lambda k: (sc[k], -k))]


def clause(text, n=150):
    t = sig.short(text, 600)
    m = re.match(r"(.+?[.!?])(\s|$)", t)
    t = m.group(1) if m else t
    if len(t) > n:
        cut = max(t.rfind(",", 0, n), t.rfind(";", 0, n))
        t = t[:cut] if cut > 40 else t[:n].rsplit(" ", 1)[0]
    if t.count("(") > t.count(")"):  # never leave a bracket open: drop the unfinished aside
        t = t[:t.rfind("(")]
    t = t.strip(" ,;:-")
    return t[:1].upper() + t[1:] + ("" if t[-1:] in ".!?" else ".")


def ask_bit(r, n=62, cut=True):
    """The first ask in plain words (ids and numbers removed); None when it is not plain prose or does not fit without cutting."""
    t = sig.short(r["first_ask"], 400)
    if re.search(r"[{}\[\]<>=|]", t) or not re.match(r"[A-Za-z]", t):
        return ""
    t = " ".join(w for w in t.replace('"', "").split() if not re.search(r"\d|[#$@]", w)).strip(" .?!,;:-")
    if len(t) > n:
        if not cut:
            return ""
        t = t[:n].rsplit(" ", 1)[0]
    ws = t.split()
    while ws and ws[-1].lower().strip(",;:") in STOP | {"order", "number", "code", "id", "based", "within", "near", "under", "over", "below", "above", "than", "between", "after", "before", "until", "since", "during", "without", "each", "every"}:
        ws.pop()
    return " ".join(ws).strip(" .?!,;:-")


def nwords(s):
    return len(re.findall(r"\w+", s))


def label_name(ids):
    """The graders' labels for the group are messy; take the most representative clean 1-4 word one (None-safe: '' when too few)."""
    labs = []
    for i in ids:
        ws = norm_label(BY[i]["persona_raw"]).split()
        while ws and ws[-1] in STOP:
            ws.pop()
        if ws and len(ws) <= 4 and not any(w in OTHER_WORDS for w in ws):
            labs.append(" ".join(ws))
    if len(labs) < max(2, 0.15 * len(ids)):
        return ""
    best = central(labs)
    ws = best.split()
    return " ".join(ws + ["requests"] if len(ws) == 1 else ws)


def word_name(ids):
    surf, cnt = defaultdict(Counter), Counter()
    for i in ids:
        for w in re.findall(r"[a-z]+", (BY[i]["first_ask"] or "").lower()):
            if w not in STOP and len(w) >= 3:
                surf[stem(w)][w] += 1
                cnt[stem(w)] += 1
    top = sorted(cnt, key=lambda t: (-cnt[t] * math.log(1.0 + len(SUBST) / (DF.get("w:" + t, 0) + 1.0)), t))[:2]
    ws = [surf[t].most_common(1)[0][0] for t in top]
    return " ".join(ws + ["requests"]) if ws else "Core requests"


def representative(ids):
    sub = [i for i in ids if i in SUB_SET] or ids
    c = centroid([V[i] for i in sub])
    return sorted(sub, key=lambda i: (-cos(V[i], c), i))


def build_goal(ids):
    bits = []
    rep = representative(ids)
    for cut in (False, True):  # asks that fit as they are come first; a shortened one only when nothing else is left
        for i in rep:
            b = ask_bit(BY[i], cut=cut)
            if b and nwords(b) >= 3 and len(bits) < 2 and all(
                    len(set(toks(b)) & set(toks(x))) / float(len(set(toks(b)) | set(toks(x))) or 1) < 0.5 for x in bits):
                bits.append(b)
    if not bits:
        return "Get a complete, correct answer to what they ask the assistant."
    return "Get help with requests like %s." % " or ".join('"%s"' % b for b in bits)  # STARTER wording: rewrite as the real intent


def build_pain(L):
    short = [r for r in L if r["outcome"] != 1]
    if not short:
        return "No run in this group fell short, so nothing blocks these users yet.", 0
    g = defaultdict(list)
    for r in short:
        k = " ".join(sorted(set(toks(r["issue_raw"])))[:4])
        if k:
            g[k].append(r)
    if g:
        top = sorted(g.values(), key=lambda x: (-len(x), x[0]["run_id"]))[0]
        return clause(central([r["rationale"] for r in sorted(top, key=lambda r: -r["conf"])]) or top[0]["issue_raw"]), len(top)
    mech = sorted([("the run ended without any answer", [r for r in short if r["no_answer"]]),
                   ("a tool call failed before the answer was ready", [r for r in short if r["tool_fail"]])], key=lambda t: -len(t[1]))
    if mech[0][1]:
        return "Most often %s." % mech[0][0], len(mech[0][1])
    return clause(central([r["rationale"] for r in short])), len(short)


def build_gain(L):
    good = [r for r in L if r["outcome"] == 1]
    if not good:
        return "No run in this group has reached its goal yet.", 0
    return clause(central([r["rationale"] for r in sorted(good, key=lambda r: -r["conf"])])), len(good)


rows, names_used = [], set()
for g in sorted(members, key=lambda g: (-len(members[g]), g)):
    ids = members[g]
    nm = cap(label_name(ids) or word_name(ids))
    if nm.lower() in names_used:
        nm = cap(word_name(ids))
    if nm.lower() in names_used:
        extra = [w for w in word_name(ids).split() if w.lower() not in nm.lower() and w != "requests"]
        nm = cap(" ".join((nm.split() + extra[:1])[:4]))
    names_used.add(nm.lower())
    rows.append(dict(name=nm, ids=ids, other=False))
if other:
    rows.append(dict(name=OTHER_NAME, ids=other, other=True))
for row in rows:
    L = [BY[i] for i in row["ids"]]
    n = len(L)
    c = Counter(r["label"] for r in L)
    pain, pn = build_pain(L)
    gain, gn = build_gain(L)
    goal = "Get help with one-off requests that fit none of the main goals." if row["other"] else build_goal(row["ids"])
    ed = PERSONA_EDITS.get(row["name"]) or {}
    med = sig.median([r["lat"] for r in L])
    row.update(name=ed.get("name", row["name"]), goal=ed.get("goal", goal), pain=ed.get("pain", pain), gain=ed.get("gain", gain),
               n=n, good=c["good"], ambiguous=c["ambiguous"], bad=c["bad"], pain_n=pn, gain_n=gn,
               good_share=sig.pct(c["good"], n), fell_short_share=sig.pct(n - c["good"], n),
               med_latency_s=round(med) if med is not None else None,
               tool_fail_pct=sig.pct(sum(1 for r in L if r["tool_fail"]), n), share_of_runs=sig.pct(n, N))
main = [r for r in rows if not r["other"]]
assert sum(r["n"] for r in rows) == N and len({i for r in rows for i in r["ids"]}) == N

OF = {i: row["name"] for row in rows for i in row["ids"]}
with open(os.path.join(H, "personas.csv"), "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["run_id", "persona"])
    for r in R:
        w.writerow([r["run_id"], OF[r["run_id"]]])
PJ = [{k: r[k] for k in ("name", "goal", "pain", "gain", "n", "good", "ambiguous", "bad", "good_share", "fell_short_share",
                         "med_latency_s", "tool_fail_pct", "pain_n", "gain_n", "share_of_runs")} for r in rows]
with open(os.path.join(H, "personas.json"), "w", encoding="utf-8") as f:
    json.dump({"personas": PJ}, f, indent=1)

# ---------------------------------------------------------------- headline
if len(main) >= 2:
    hi, lo = max(main, key=lambda r: (r["good_share"], r["n"])), min(main, key=lambda r: (r["good_share"], -r["n"]))
    head = "%d user goals cover %d%% of the %d runs; %s runs fare best (%d%% good, n=%d) and %s worst (%d%%, n=%d)." % (
        len(main), 100 - sig.pct(len(other), N), N, hi["name"].lower(), hi["good_share"], hi["n"], lo["name"].lower(), lo["good_share"], lo["n"])
elif main:
    head = "One user goal covers the %d runs; %d%% reached it." % (N, main[0]["good_share"])
else:
    head = "No user goal could be separated in these %d runs." % N
SUB = "Who is asking, what does each group want, and where are they served well or badly?"

# ---------------------------------------------------------------- figures
COLS, CELL, GAP = 30, 8.0, 3.0
STEP = CELL + GAP
nmax = max(r["n"] for r in rows)
PER = max(1, int(math.ceil(nmax / 450.0)))


def panel(r):
    L = sorted([BY[i] for i in r["ids"]], key=lambda x: (-x["outcome"], x["first_ts"], x["run_id"]))
    cells = [L[k:k + PER] for k in range(0, len(L), PER)]
    CL = {1: "var(--green)", 0: "var(--muted)", -1: "var(--bad)"}
    rows_n = int(math.ceil(int(math.ceil(nmax / float(PER))) / float(COLS)))
    top = 62
    W, Hh = COLS * STEP, top + rows_n * STEP
    t2 = "median %ds, " % r["med_latency_s"] if r["med_latency_s"] is not None else ""
    o = ['<svg viewBox="0 0 %.0f %.0f" role="img" aria-label="%s: %d runs, %d good, %d ambiguous, %d bad">' % (W, Hh, e(r["name"]), r["n"], r["good"], r["ambiguous"], r["bad"]),
         '<text class="tb" x="0" y="14">%s</text>' % e(r["name"]),
         '<text class="t" x="0" y="33">n=%d &#183; %d%% good &#183; %d%% fell short</text>' % (r["n"], r["good_share"], r["fell_short_share"]),
         '<text class="tm" x="0" y="48">%stool failures %d%% of runs</text>' % (t2, r["tool_fail_pct"])]
    for k, grp in enumerate(cells):
        worst = min(x["outcome"] for x in grp)
        o.append('<rect x="%.1f" y="%.1f" width="%.0f" height="%.0f" fill="%s"/>' % ((k % COLS) * STEP, top + (k // COLS) * STEP, CELL, CELL, CL[worst]))
    return "".join(o) + "</svg>"


def dots():
    RH, X0, X1 = 38, 150, 440
    sx = lambda v: X0 + (X1 - X0) * v / 100.0
    hh = RH * len(rows) + 28
    o = ['<svg viewBox="0 0 560 %d" role="img" aria-label="Good share by persona">' % hh]
    for v in (0, 50, 100):
        o.append('<text class="tm" x="%.0f" y="%d" text-anchor="middle">%d%%</text>' % (sx(v), hh - 6, v))
    for k, r in enumerate(sorted(rows, key=lambda r: (-r["good_share"], r["name"]))):
        y = 20 + k * RH
        ls, cur = [], ""
        for w in r["name"].split():  # wrap the label so it never runs off the left edge
            if cur and len(cur) + 1 + len(w) > 21:
                ls.append(cur)
                cur = w
            else:
                cur = (cur + " " + w).strip()
        ls.append(cur)
        o.append('<text class="t" x="%d" y="%d" text-anchor="end">%s</text>' % (X0 - 10, y + 4 - 6 * (len(ls) - 1), "".join(
            '<tspan x="%d" dy="%d">%s</tspan>' % (X0 - 10, 0 if j == 0 else 12, e(l)) for j, l in enumerate(ls))))
        o.append('<line x1="%.1f" x2="%.1f" y1="%d" y2="%d" stroke="var(--line)"/>' % (sx(0), sx(100), y, y))
        o.append('<circle cx="%.1f" cy="%d" r="4.5" fill="var(--green)"/>' % (sx(r["good_share"]), y))
        o.append('<text class="tm" x="%.1f" y="%d">%d%% &#183; n=%d</text>' % (sx(r["good_share"]) + 10, y + 4, r["good_share"], r["n"]))
    return "".join(o) + "</svg>"


def persona_table():
    o = ['<div class="wrap"><table class="tbl rs"><thead><tr><th>Persona</th><th>Goal</th><th>Top pain</th><th>Top gain</th></tr></thead><tbody>']
    for r in rows:
        o.append('<tr><td data-l="Persona"><b>%s</b></td><td data-l="Goal">%s</td><td data-l="Top pain">%s <small>%d of %d short runs</small></td>'
                 '<td data-l="Top gain">%s <small>%d of %d good runs</small></td></tr>'
                 % (e(r["name"]), e(r["goal"]), e(r["pain"]), r["pain_n"], r["n"] - r["good"], e(r["gain"]), r["gain_n"], r["good"]))
    return "".join(o) + "</tbody></table></div>"


def walkthrough():
    o = []
    for r in rows:
        c = [BY[i] for i in r["ids"] if BY[i]["outcome"] != 1 and BY[i]["n_calls"] > 0 and len(toks(BY[i]["first_ask"])) >= 3]
        c.sort(key=lambda x: (x["label"] != "bad", -x["conf"], x["first_ts"]))
        if not c:
            continue
        x = c[0]
        tools = " > ".join(x["workflow"].split(" > ")[:6]) if x["workflow"] else "no tools"
        lat = ", %ds" % round(x["lat"]) if x["lat"] is not None else ""
        o.append('<dt>%s &middot; run %s</dt><dd>Asked: &ldquo;%s&rdquo;. The agent made %d tool calls (%d failed%s) via %s and %s. %s</dd>'
                 % (e(r["name"]), e(x["run_id"][:RL]), e(ask_bit(x, 120)), x["n_calls"], x["n_fail"], lat, e(tools),
                    "ended without an answer" if x["no_answer"] else "answered", e(clause(x["rationale"], 200))))
    return '<details class="dd"><summary>One failed run per persona</summary><dl class="wk">%s</dl></details>' % "".join(o) if o else ""


CSS = """<style>
#empathy{padding-top:9vh}
#empathy .pgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(300px,100%),1fr));gap:30px 36px;margin:1.4em 0}
#empathy .dots{max-width:560px}#empathy small{color:var(--muted);font-size:11.5px;display:block}
#empathy .rs td{font-size:13px;color:var(--soft)}#empathy .rs td b{font-weight:400;color:var(--ink)}
#empathy .rs td:nth-child(1){width:15%}#empathy .rs td:nth-child(2){width:28%}
#empathy .wk{margin:8px 0 0;font-size:13px;color:var(--soft)}
#empathy .wk dt{font-size:10px;letter-spacing:.16em;text-transform:uppercase;color:var(--muted);margin-top:12px}#empathy .wk dd{margin:2px 0 0}
@media(max-width:700px){#empathy .rs thead{display:none}#empathy .rs,#empathy .rs tbody,#empathy .rs tr,#empathy .rs td{display:block;width:100%!important}
#empathy .rs tr{padding:12px 0;border-bottom:1px solid var(--line)}#empathy .rs td{border:0;padding:3px 0}
#empathy .rs td::before{content:attr(data-l);display:block;font-size:10px;letter-spacing:.16em;text-transform:uppercase;color:var(--muted)}}
</style>"""
cap1 = "One square per run%s: green good, grey ambiguous, red bad; same scale in every panel (n=%d)." % (
    "" if PER == 1 else " group of %d (coloured by its worst run)" % PER, N)
page = (CSS + '<section id="empathy" class="step">\n<p class="kicker">Section 4 &middot; Empathy Map</p>\n<h2 class="big">Empathy Map</h2>\n'
        '<p class="sub">%s</p>\n<p class="lede">%s</p>\n'
        '<figure class="fig"><figcaption><span class="kicker">Figure</span> %s</figcaption><div class="pgrid">%s</div></figure>\n'
        '<figure class="fig"><figcaption><span class="kicker">Figure</span> What each persona wants, where it is blocked and what works (n=%d).</figcaption>%s</figure>\n'
        '<figure class="fig"><figcaption><span class="kicker">Figure</span> Good share by persona; read with the n beside each dot.</figcaption><div class="dots">%s</div></figure>\n%s</section>'
        % (e(SUB), e(head), cap1, "".join(panel(r) for r in rows), N, persona_table(), dots(), walkthrough()))
with open(os.path.join(H, "empathy.html"), "w", encoding="utf-8") as f:
    f.write(page)
md = "## Empathy Map\n\n%s\n\n%s\n\n" % (SUB, head) + "\n".join(
    "- %s (n=%d, %d%% good, %d%% fell short%s, tool failures %d%%). Goal: %s Pain: %s Gain: %s" % (
        r["name"], r["n"], r["good_share"], r["fell_short_share"], (", median %ds" % r["med_latency_s"]) if r["med_latency_s"] is not None else "",
        r["tool_fail_pct"], r["goal"], r["pain"], r["gain"]) for r in rows) + "\n"
with open(os.path.join(H, "empathy.md"), "w", encoding="utf-8") as f:
    f.write(md)
with open(os.path.join(H, "empathy.json"), "w", encoding="utf-8") as f:
    json.dump(dict(headline=head, n_runs=N, n_personas=len(main), other_share_pct=sig.pct(len(other), N), personas=PJ), f, indent=1)
print(head)
for r in rows:
    print("%-28s n=%3d good=%3d%% short=%3d%% lat=%s fail=%d%%" % (r["name"], r["n"], r["good_share"], r["fell_short_share"], r["med_latency_s"], r["tool_fail_pct"]))
