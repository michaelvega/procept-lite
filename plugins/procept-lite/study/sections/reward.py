"""Section 2, Reward Signal. A STARTER: read it, copy it, edit it for this dataset.

Re-run: python3 -I sections/reward.py   ->  reward.html, reward.md, reward.json

Question: which quality signals can stand in for "the user got what they came for"? Every signal that exists is tested
against the outcome labels, each on its own scale (scores from different sources are never pooled):
  1. scores found in the RAW export (any judge, evaluator, rating or pass/fail field, keyed by run id). The starter scans
     common shapes (flat fields, feedback or evaluation fields, lists of {key, score}); it finds the export through
     PLITE_RAW (a file or folder), a one-line sections/../raw_path.txt, or the files listed in ../inspect.json.
     When nothing is found the section simply leaves this part out.
  2. the user's next message (next_ask). Label each reaction yourself: write sections/reactions.csv with the columns
     run_id,reaction where reaction is one of: repeats or corrects | complains | thanks or moves on | nothing to infer.
     Without that file a cue-word draft is used and written to sections/reactions.draft.csv for you to read and correct.
  3. mechanical signals from the run itself: tool_fail, unrecovered, no_answer, slow, heavy.
Plain words only; every figure shows its n.
"""
import csv
import json
import math
import os
import random
import re
import sys
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from html import escape

HERE = os.path.dirname(os.path.abspath(__file__))
STUDY = os.path.dirname(HERE)
sys.path.insert(0, STUDY)
import sig  # noqa: E402

csv.field_size_limit(sys.maxsize)
LB = ("good", "ambiguous", "bad")
COL = {"good": "var(--green)", "ambiguous": "var(--muted)", "bad": "var(--bad)"}
esc = lambda s: escape(str(s), quote=True)  # noqa: E731
short = sig.short
REACTIONS = ("repeats or corrects", "complains", "thanks or moves on", "nothing to infer")
RCOL = {"repeats or corrects": "var(--warn)", "complains": "var(--bad)", "thanks or moves on": "var(--green)", "nothing to infer": "var(--strong)"}
RABBR = {"repeats or corrects": "repeats", "complains": "complains", "thanks or moves on": "moves on", "nothing to infer": "no signal"}
MECH = [("tool_fail", "Tool failure"), ("unrecovered", "Failure that ended the run"), ("no_answer", "No answer"),
        ("slow", "Slow run"), ("heavy", "Many tool calls")]


# ------------------------------------------------------------------ the raw export
QUALITY = re.compile(r"score|rating|grade|eval|feedback|judge|verdict|pass|correct|helpful|thumb|quality|satisf", re.I)
NOT_QUALITY = re.compile(r"token|latency|cost|duration|usage|count|time|status|code|length|size|model|temperature|index|seed|port|version|(?:^|[._:])(?:n|num|total)$", re.I)
IDKEY = re.compile(r"(?:^|[._])(?:run|trace|call|session|id)(?:[._]?id)?$|\.id$|^id$", re.I)
POS, NEG = {"pass", "passed", "good", "true", "yes", "up", "thumbs_up", "correct", "positive"}, {"fail", "failed", "bad", "false", "no", "down", "thumbs_down", "incorrect", "negative"}


def raw_paths(out):
    cand = []
    for p in (os.environ.get("PLITE_RAW") or "").split(os.pathsep):
        if p.strip():
            cand.append(p.strip())
    try:
        with open(os.path.join(STUDY, "raw_path.txt"), encoding="utf-8") as f:
            cand += [l.strip() for l in f if l.strip()]
    except OSError:
        pass
    try:
        with open(os.path.join(out, "inspect.json"), encoding="utf-8") as f:
            for name in json.load(f).get("files") or []:
                name = name if isinstance(name, str) else str((name or {}).get("path") or "")
                if name:
                    for base in (os.path.dirname(out), os.getcwd(), out):
                        cand.append(name if os.path.isabs(name) else os.path.join(base, name))
    except (OSError, ValueError, AttributeError):
        pass
    files = []
    for c in cand:
        if os.path.isdir(c):
            for root, _, names in os.walk(c):
                files += [os.path.join(root, n) for n in sorted(names)]
        elif os.path.isfile(c):
            files.append(c)
    seen, res = set(), []
    for fpath in files:
        if fpath not in seen and fpath.lower().endswith((".jsonl", ".ndjson", ".json", ".csv")):
            seen.add(fpath)
            res.append(fpath)
    return res[:40]


def records(path):
    """Yield dict records from a jsonl, json or csv file (a huge json file is skipped)."""
    low = path.lower()
    try:
        if low.endswith(".csv"):
            with open(path, encoding="utf-8-sig", newline="") as f:
                for row in csv.DictReader(f):
                    yield row
        elif low.endswith((".jsonl", ".ndjson")):
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("{"):
                        try:
                            yield json.loads(line)
                        except ValueError:
                            pass
        elif os.path.getsize(path) < 200 * 1024 * 1024:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            items = d if isinstance(d, list) else next((v for v in d.values() if isinstance(v, list)), [d]) if isinstance(d, dict) else []
            for it in items:
                if isinstance(it, dict):
                    yield it
    except (OSError, ValueError, csv.Error):
        return


def as_number(v):
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        return float(v) if v == v and abs(v) != float("inf") else None
    if isinstance(v, str):
        t = v.strip().lower()
        if t in POS:
            return 1.0
        if t in NEG:
            return 0.0
        if re.fullmatch(r"-?\d+(?:\.\d+)?", t):
            return float(t)
    return None


def flatten(x, path="", depth=0):
    """Yield (path, leaf) pairs; JSON held in a string is opened when it mentions a quality-like word."""
    if depth > 7:
        return
    if isinstance(x, str):
        t = x.lstrip()
        if t[:1] in "{[" and QUALITY.search(t) and len(t) < 400000:
            try:
                yield from flatten(json.loads(t), path, depth + 1)
                return
            except ValueError:
                pass
        yield path, x
    elif isinstance(x, dict):
        key = x.get("key") if isinstance(x.get("key"), str) else x.get("name") if isinstance(x.get("name"), str) else None
        val = next((x[k] for k in ("score", "value", "rating", "avg") if k in x and as_number(x[k]) is not None), None)
        if key and val is not None and QUALITY.search(path + " " + key):
            yield "%s:%s" % (path.split(".")[0] if path else "feedback", key), val
            return
        for k, v in x.items():
            yield from flatten(v, "%s.%s" % (path, k) if path else str(k), depth + 1)
    elif isinstance(x, list):
        for v in x[:60]:
            yield from flatten(v, path, depth + 1)
    else:
        yield path, x


def field_name(path):
    if ":" in path:
        return path
    parts = [p for p in path.split(".") if not p.isdigit()]
    return ".".join(parts[-2:])


def raw_scores(R, out):
    """-> {field: {run_id: mean value}} for fields found in the raw export that cover enough runs."""
    ids = {r["run_id"] for r in R}
    got = defaultdict(lambda: defaultdict(list))
    for path in raw_paths(out):
        for rec in records(path):
            leaves = list(flatten(rec))
            hit = [(p, v) for p, v in leaves if isinstance(v, str) and v in ids]
            if not hit:
                continue
            found = {v for _, v in hit}
            if len(found) > 1:
                found = {v for p, v in hit if IDKEY.search(p)}
            if len(found) != 1:
                continue
            rid = found.pop()
            for p, v in leaves:
                if (QUALITY.search(p) and not NOT_QUALITY.search(p if ":" not in p else "")) and not isinstance(v, (dict, list)):
                    x = as_number(v)
                    if x is not None and not IDKEY.search(p):
                        got[field_name(p)][rid].append(x)
    need = max(5, int(0.05 * len(R)))
    res = {f: {r: sum(v) / len(v) for r, v in d.items()} for f, d in got.items() if len(d) >= need}
    return dict(sorted(res.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:3])


# ------------------------------------------------------------------ the user's next message
def overlap(a, b):
    x, y = set(re.findall(r"[a-z0-9']{3,}", a.lower())), set(re.findall(r"[a-z0-9']{3,}", b.lower()))
    return len(x & y) / len(x | y) if x and y else 0.0


COMPLAIN = re.compile(r"\b(wrong|useless|terrible|awful|frustrat\w*|broken|not working|doesn'?t work|didn'?t work|still (?:not|no)|never|why (?:can'?t|didn'?t|did you|won'?t)|ridiculous|waste)\b", re.I)
THANKS = re.compile(r"\b(thanks?|thx|thank you|great|perfect|awesome|got it|cool|nice|works|helpful|appreciate\w*)\b", re.I)
REPEAT = re.compile(r"^(no\b|nope|not what|i said|i asked|again|try again|instead|actually|that'?s not|that is not|still)|\b(i said|i asked|as i said|try again|not what i)\b", re.I)


def draft_reaction(r):
    t = r["next_ask"]
    if COMPLAIN.search(t):
        return "complains"
    if REPEAT.search(t.strip()) or overlap(r["first_ask"], t) >= 0.5:
        return "repeats or corrects"
    if THANKS.search(t) or overlap(r["first_ask"], t) < 0.25:
        return "thanks or moves on"
    return "repeats or corrects"


def reactions(R):
    """-> ({run_id: reaction}, source) for runs with a next message. A reactions.csv written by the reader wins."""
    have = [r for r in R if r["next_ask"].strip()]
    mine = {}
    try:
        with open(os.path.join(HERE, "reactions.csv"), encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                if (row.get("reaction") or "").strip() in REACTIONS:
                    mine[(row.get("run_id") or "").strip()] = row["reaction"].strip()
    except OSError:
        pass
    if mine:
        return {r["run_id"]: mine[r["run_id"]] for r in have if r["run_id"] in mine}, "read"
    res = {r["run_id"]: draft_reaction(r) for r in have}
    if have:
        with open(os.path.join(HERE, "reactions.draft.csv"), "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["run_id", "ask", "answer", "next_ask", "reaction"])
            for r in have[:300]:
                w.writerow([r["run_id"], short(r["first_ask"], 140), short(r["final_answer"], 140), short(r["next_ask"], 140), res[r["run_id"]]])
    return res, "draft"


# ------------------------------------------------------------------ the small statistics (plain words in the text)
def wilson(k, n, z=1.96):
    if not n:
        return 0.0, 0.0
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)


def pair_share(pos, neg):
    """Share of (better, worse) pairs where the better run scores higher (ties count half). None when a side is empty."""
    if not pos or not neg:
        return None
    s = sorted(neg)
    return sum(bisect_left(s, a) + 0.5 * (bisect_right(s, a) - bisect_left(s, a)) for a in pos) / (len(pos) * len(neg))


def pair_range(rows, score, ispos, isneg, B=400, seed=7):
    """95% range of pair_share when whole conversations are resampled (fixed seed)."""
    g = defaultdict(list)
    for r in rows:
        g[r["conversation_id"]].append(r)
    keys, rng, o = sorted(g), random.Random(seed), []
    for _ in range(B):
        s = [x for k in (keys[rng.randrange(len(keys))] for _ in keys) for x in g[k]]
        a = pair_share([score[x["run_id"]] for x in s if ispos(x)], [score[x["run_id"]] for x in s if isneg(x)])
        if a is not None:
            o.append(a)
    if len(o) < 20:
        return None
    o.sort()
    return o[int(0.025 * len(o))], o[int(0.975 * len(o)) - 1]


def verdict_flag(n1, s1, lo1, s0):
    if n1 < 5:
        return "too few runs to say"
    lift = s1 - s0
    if lo1 > s0 and lift >= 0.2:
        return "tracks"
    return "partly tracks" if lift >= 0.1 else "does not track"


def flag_stats(rows, flag):
    short_ = lambda r: r["outcome"] != 1  # noqa: E731
    f = [r for r in rows if flag(r)]
    nf = [r for r in rows if not flag(r)]
    k1 = sum(short_(r) for r in f)
    k0 = sum(short_(r) for r in nf)
    allshort = k1 + k0
    good = [r for r in rows if r["outcome"] == 1]
    s1, s0 = (k1 / len(f) if f else 0.0), (k0 / len(nf) if nf else 0.0)
    lo, hi = wilson(k1, len(f))
    return dict(n=len(rows), n_flag=len(f), n_clear=len(nf), short_flag=s1, short_clear=s0, lo=lo, hi=hi,
                base=allshort / len(rows) if rows else 0.0, catches=k1 / allshort if allshort else 0.0,
                flags_good=sum(1 for r in good if flag(r)) / len(good) if good else 0.0,
                verdict=verdict_flag(len(f), s1, lo, s0) if f else "none flagged")


# ------------------------------------------------------------------ figures
CSS = """<style>#reward{padding-top:9vh}#reward .fig{margin:2em 0}#reward .lede{margin:0 0 .5em}#reward .vd{color:var(--soft);margin:.2em 0 0;font-size:15px;max-width:64ch}
#reward svg{max-width:100%}#reward .fig figcaption b{font-size:16px;margin-bottom:6px}
#reward .al{table-layout:fixed;font-size:13px}#reward .al col.cl{width:210px}#reward .al col.cv{width:96px}#reward .al col.cn{width:64px}
#reward .al td{border-bottom:0;padding:7px 8px 7px 0;vertical-align:middle}#reward .al td.v{text-align:right;color:var(--ink);white-space:nowrap;padding-right:6px}#reward .al td.v s{text-decoration:none;color:var(--muted)}
#reward .al td.n{text-align:right;color:var(--muted);white-space:nowrap;padding-right:0;font-variant-numeric:tabular-nums}
#reward .pl{position:relative;height:20px;margin:0 6px}#reward .pl i{position:absolute;display:block}
#reward .pl .rule0{left:0;right:0;top:9px;border-top:1px solid var(--line)}#reward .pl .base{top:1px;bottom:1px;border-left:1px dashed var(--muted)}
#reward .pl .ci{top:9px;height:2px;background:var(--strong)}#reward .pl .dot{top:4px;width:12px;height:12px;margin-left:-6px;border-radius:50%;background:var(--ink)}
#reward .pl .hol{top:5px;width:10px;height:10px;margin-left:-5px;border-radius:50%;border:1.5px solid var(--muted);background:var(--bg)}
#reward .axis td{padding-top:2px}#reward .axis .pl{height:16px;border-top:1px solid var(--strong);margin-top:2px}#reward .axis .pl i{top:3px;font-style:normal;font-size:10px;color:var(--muted);white-space:nowrap}
#reward .axis .t0{left:0}#reward .axis .t5{left:50%;transform:translateX(-50%)}#reward .axis .t1{right:0}
#reward .rb{display:flex;height:20px}#reward .rb i{display:flex;align-items:center;justify-content:center;font-style:normal;font-size:10px;color:var(--bg);overflow:hidden;white-space:nowrap}
#reward .grid2{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:28px}#reward .tbl.sm td,#reward .tbl.sm th{font-size:12.5px}#reward .tbl .n{white-space:nowrap;font-variant-numeric:tabular-nums}
@media(max-width:760px){#reward .grid2{grid-template-columns:1fr}
#reward .al,#reward .al tbody,#reward .al colgroup{display:block}#reward .al col{display:none}#reward .al tr{display:grid;grid-template-columns:minmax(0,1fr) 84px 52px;column-gap:6px;padding:4px 0;border-bottom:1px solid var(--line)}
#reward .al td{padding:2px 0}#reward .al td.l{grid-column:1/-1}#reward .al td.p{grid-column:1}#reward .al td.v{grid-column:2}#reward .al td.n{grid-column:3}#reward .axis td.l,#reward .axis td.v,#reward .axis td.n{display:none}#reward .axis tr{border-bottom:0}}</style>"""


def pct(x):
    return "%d%%" % round(100 * x)


def plot_cell(items):
    """items: (kind, fraction 0..1, width) -> positioned marks inside one plot cell."""
    return '<div class="pl"><i class="rule0"></i>' + "".join(items) + "</div>"


def aligned_table(rows, axis_mid, base_label=False):
    """rows: dict(label, marks(list of html), value(html), n). One shared 0-100% axis, fixed columns."""
    t = '<div class="wrap"><table class="tbl al"><colgroup><col class="cl"><col><col class="cv"><col class="cn"></colgroup><tbody>'
    for r in rows:
        t += '<tr><td class="l">%s</td><td class="p">%s</td><td class="v">%s</td><td class="n">%s</td></tr>' % (
            esc(r["label"]), plot_cell(r["marks"]), r["value"], esc(r["n"]))
    t += '<tr class="axis"><td class="l"></td><td class="p"><div class="pl"><i class="t0">0%</i><i class="t5">' + esc(axis_mid) + '</i><i class="t1">100%</i></div></td><td class="v"></td><td class="n"></td></tr>'
    return t + "</tbody></table></div>"


def at(f):
    return "%.2f%%" % (100 * f)


def flag_chart(stats):
    rows = []
    for name, s in stats:
        if not s["n_flag"]:
            continue
        marks = ['<i class="base" style="left:%s"></i>' % at(s["base"]), '<i class="ci" style="left:%s;width:%s"></i>' % (at(s["lo"]), at(max(s["hi"] - s["lo"], 0.004))),
                 '<i class="dot" style="left:%s"></i>' % at(s["short_flag"])]
        if s["n_clear"]:
            marks.append('<i class="hol" style="left:%s"></i>' % at(s["short_clear"]))
        rows.append(dict(label=name, marks=marks, n="n=%d" % s["n_flag"],
                         value="%s%s" % (pct(s["short_flag"]), (" <s>vs %s</s>" % pct(s["short_clear"])) if s["n_clear"] else "")))
    return aligned_table(rows, "50%") if rows else ""


def pair_chart(items):
    rows = []
    for name, a, rng, npos, nneg in items:
        marks = ['<i class="base" style="left:50%"></i>']
        if rng:
            marks.append('<i class="ci" style="left:%s;width:%s"></i>' % (at(rng[0]), at(max(rng[1] - rng[0], 0.004))))
        marks.append('<i class="dot" style="left:%s"></i>' % at(a))
        rows.append(dict(label=name, marks=marks, n="%d vs %d" % (npos, nneg), value=pct(a) + ((" <s>%d-%d</s>" % (round(100 * rng[0]), round(100 * rng[1]))) if rng else "")))
    return aligned_table(rows, "50% = coin flip") if rows else ""


def dot_strip(title, by, fmt, mark=None, mark_label=""):
    """by: {label: [values]}. Dot per run, stacked where values coincide. Returns svg or ''."""
    allv = [v for l in LB for v in by.get(l, [])]
    if len(allv) < 5:
        return ""
    lo, hi = min(allv), max(allv)
    if hi <= lo:
        lo, hi = lo - 0.5, hi + 0.5
    W, x0, x1, rh = 340, 96, 322, 44
    sc = lambda v: x0 + (v - lo) / (hi - lo) * (x1 - x0)  # noqa: E731
    H = 24 + 3 * rh + 22
    s = '<svg viewBox="0 0 %d %d" role="img" aria-label="%s by outcome, n=%d"><text class="tb" x="0" y="12" style="font-size:12px">%s</text>' % (W, H, esc(title), len(allv), esc(title))
    for i, l in enumerate(LB):
        v = by.get(l, [])
        y = 24 + i * rh + rh / 2
        s += '<text class="t" x="0" y="%.0f">%s</text><text class="tm" x="0" y="%.0f">n=%d</text>' % (y - 1, l, y + 11, len(v))
        bins = defaultdict(int)
        step = (x1 - x0) / 60.0
        for x in sorted(v):
            b = int(sc(x) / step)
            k = bins[b]
            bins[b] += 1
            off = ((k + 1) // 2) * 4.4 * (1 if k % 2 else -1)
            off = max(-rh / 2 + 3, min(rh / 2 - 3, off))
            s += '<circle cx="%.1f" cy="%.1f" r="2" fill="%s" opacity=".85"/>' % (b * step + step / 2, y + off, COL[l])
    ay = 24 + 3 * rh + 4
    s += '<line x1="%d" x2="%d" y1="%d" y2="%d" stroke="var(--strong)"/>' % (x0, x1, ay, ay)
    ticks = sorted({lo, hi, (lo + hi) / 2}) if hi - lo > 1 else [lo, hi]
    for k, tk in enumerate(ticks):
        anc = "start" if k == 0 else "end" if k == len(ticks) - 1 else "middle"
        s += '<text class="tm" x="%.1f" y="%d" text-anchor="%s">%s</text>' % (sc(tk), H - 4, anc, fmt(tk))
    if mark is not None and lo <= mark <= hi:
        s += '<line x1="%.1f" x2="%.1f" y1="26" y2="%d" stroke="var(--muted)" stroke-dasharray="3 3"/>' % (sc(mark), sc(mark), ay)
    return s + "</svg>"


def num(v):
    return ("%.1f" % v).rstrip("0").rstrip(".") if abs(v - round(v)) > 1e-9 else "%d" % round(v)


def label_of(field):
    t = " ".join(re.sub(r"[_.:]+", " ", field).split())
    t = re.sub(r"^(feedback stats|feedback|evaluation|eval) ", "", t)
    return re.sub(r" (avg|score|value|mean)$", "", t) or t


# ------------------------------------------------------------------ build
def write(html, md, J):
    for ext, body in (("html", html), ("md", md), ("json", json.dumps(J, indent=1, default=str))):
        with open(os.path.join(HERE, "reward." + ext), "w", encoding="utf-8") as f:
            f.write(body)


def main():
    R = sig.load()
    N = len(R)
    out = sig.resolve_out()
    sub = "Which quality signals can stand in for the user getting what they came for?"
    if not N:
        write('<section id="reward" class="step"><h2 class="big">Reward Signal</h2><p class="sub">%s</p><p class="lede">No graded runs yet.</p></section>' % sub,
              "## Reward Signal\n\n- No graded runs yet.\n", {})
        return 0
    by = {l: [r for r in R if r["label"] == l] for l in LB}
    overall_short = sum(r["outcome"] != 1 for r in R) / N
    # signals that fire or not, each on its own scale
    flags = []  # (name, stats)
    for key, name in MECH:
        flags.append((name, flag_stats(R, lambda r, k=key: r[k]), key))
    react, rsrc = reactions(R)
    rrows = [r for r in R if r["run_id"] in react]
    if rrows:
        flags.append(("Next message repeats or complains", flag_stats(rrows, lambda r: react[r["run_id"]] in ("repeats or corrects", "complains")), "next_message"))
    # scores in the raw export
    raw = raw_scores(R, out)
    vend = []  # (field, rows, score, pair_good_bad_or_notgood)
    for field, sc_ in raw.items():
        rows = [r for r in R if r["run_id"] in sc_]
        pos = [sc_[r["run_id"]] for r in rows if r["outcome"] == 1]
        if len(pos) < 1:
            continue
        negb = [sc_[r["run_id"]] for r in rows if r["outcome"] == -1]
        use_bad = len(negb) >= 5 and len(pos) >= 5
        isneg = (lambda r: r["outcome"] == -1) if use_bad else (lambda r: r["outcome"] != 1)
        neg = negb if use_bad else [sc_[r["run_id"]] for r in rows if r["outcome"] != 1]
        a = pair_share(pos, neg)
        if a is None:
            continue
        rng = pair_range(rows, sc_, lambda r: r["outcome"] == 1, isneg) if len(pos) >= 5 and len(neg) >= 5 else None
        vals = list(sc_.values())
        top = Counter(round(v, 2) for v in vals).most_common(1)[0]
        if len(pos) < 5 or len(neg) < 5:
            v = "too few runs to say"
        elif a >= 0.8 and rng and rng[0] > 0.6:
            v = "tracks"
        elif a >= 0.65:
            v = "partly tracks"
        else:
            v = "does not track"
        vend.append(dict(field=field, label=label_of(field), rows=rows, score=sc_, a=a, rng=rng, npos=len(pos), nneg=len(neg), vs="bad" if use_bad else "not good",
                         verdict=v, top_value=top[0], top_share=top[1] / len(vals), distinct=len(set(round(x, 3) for x in vals)), n=len(vals)))
    # verdicts
    ranked = []
    for name, s, key in flags:
        if s["n_flag"]:
            ranked.append((name, s["verdict"], s["short_flag"] - s["short_clear"], s["n_flag"]))
    for v in vend:
        ranked.append(("Score " + v["label"], v["verdict"], v["a"] - 0.5, v["npos"] + v["nneg"]))
    tracks = [n for n, v, _, _ in ranked if v == "tracks"]
    partly = [n for n, v, _, _ in ranked if v == "partly tracks"]
    nots = [n for n, v, _, _ in ranked if v == "does not track"]
    j = lambda xs: ", ".join(xs[:-1]) + " and " + xs[-1] if len(xs) > 1 else (xs[0] if xs else "")  # noqa: E731
    lc = lambda s: s[0].lower() + s[1:]  # noqa: E731
    if len(tracks) >= 2:
        overall = "good"
    elif tracks or partly:
        overall = "partial"
    else:
        overall = "noise"
    parts = []
    if tracks:
        parts.append("%s track%s the outcome" % (j([lc(x) for x in tracks]), "s" if len(tracks) == 1 else ""))
    if partly:
        parts.append("%s only partly track%s it" % (j([lc(x) for x in partly]), "s" if len(partly) == 1 else ""))
    if nots:
        parts.append("%s %s not" % (j([lc(x) for x in nots]), "does" if len(nots) == 1 else "do"))
    verdict = ("Early read: " if N < 30 else "") + "Verdict: %s. %s." % (overall, (lambda t: t[0].upper() + t[1:])("; ".join(parts)) if parts else "No signal had enough runs to test")
    best = max((x for x in ranked if x[3] >= 5), key=lambda x: x[2], default=None)
    if best:
        bn, bv, lift, bnn = best
        s = next((st for nm, st, _ in flags if nm == bn), None)
        if s:
            lede = "Strongest signal, %s: runs it flags fell short %s of the time, against %s otherwise (n=%d and %d)." % (lc(bn), pct(s["short_flag"]), pct(s["short_clear"]), s["n_flag"], s["n_clear"])
        else:
            v = next(x for x in vend if "Score " + x["label"] == bn)
            lede = "Strongest signal, %s: it ranks a good run above a %s run in %s of pairs (n=%d and %d); 50%% is a coin flip." % (lc(bn.replace("Score ", "score ")), v["vs"], pct(v["a"]), v["npos"], v["nneg"])
    else:
        lede = "No signal fired often enough to test against the %d graded runs." % N
    # figures
    FN = [0]

    def fig(title, body, cap):
        FN[0] += 1
        return '<figure class="fig"><figcaption><span class="kicker">Figure</span><b>%s</b>%s</figcaption>%s</figure>' % (esc(title), esc(cap), body)

    html = CSS + '<section id="reward" class="step"><p class="kicker">Section 2 &middot; Reward Signal</p><h2 class="big">Reward Signal</h2>'
    html += '<p class="sub">%s</p><p class="lede">%s</p><p class="vd">%s</p>' % (esc(sub), esc(lede), esc(verdict))
    fc = flag_chart([(n, s) for n, s, _ in flags])
    if fc:
        html += fig("When a signal flags a run, how often did it fall short?", fc,
                    "Filled dot: flagged runs, with its 95%% range. Hollow dot: runs not flagged. Dashes: all runs, %s. n=%d." % (pct(overall_short), N))
    for v in vend:
        scores = {l: [v["score"][r["run_id"]] for r in v["rows"] if r["label"] == l] for l in LB}
        svg = dot_strip("Score: " + v["label"], scores, num)
        spread = "%s of scores are %s" % (pct(v["top_share"]), num(v["top_value"])) if v["top_share"] >= 0.5 else "%d different values" % v["distinct"]
        html += fig("Score %s by outcome" % v["label"], '<div class="grid2"><div>%s</div></div>%s' % (
            svg, pair_chart([("Good above %s" % v["vs"], v["a"], v["rng"], v["npos"], v["nneg"])])),
            "Found in the raw export for %d of %d runs; %s." % (v["n"], N, spread))
    # mechanical strips and reaction mix
    thr = sig.thresholds(R)
    lat = {l: [r["lat"] for r in by[l] if r["lat"] is not None] for l in LB}
    cal = {l: [float(r["n_calls"]) for r in by[l]] for l in LB}
    s_lat = dot_strip("Seconds per run", lat, lambda x: "%.0fs" % x, thr["slow_s"])
    s_cal = dot_strip("Tool calls per run", cal, lambda x: "%.0f" % x, thr["heavy_calls"])
    if s_lat or s_cal:
        html += fig("Where the slow and heavy cut-offs sit", '<div class="grid2"><div>%s</div><div>%s</div></div>' % (s_lat, s_cal),
                    "Dashed line: where a run counts as slow or heavy. n=%d." % N)
    if rrows:
        rr = []
        for l in LB:
            g = [r for r in rrows if r["label"] == l]
            if not g:
                continue
            c = Counter(react[r["run_id"]] for r in g)
            segs = "".join('<i style="width:%.2f%%;background:%s">%s</i>' % (100.0 * c[k] / len(g), RCOL[k],
                           ("%s %d%%" % (RABBR[k], round(100.0 * c[k] / len(g)))) if c[k] / len(g) >= 0.22 else ("%d%%" % round(100.0 * c[k] / len(g)) if c[k] / len(g) >= 0.08 else ""))
                           for k in REACTIONS if c[k])
            rr.append((l, '<div class="rb">%s</div>' % segs, "n=%d" % len(g)))
        t = '<div class="wrap"><table class="tbl al"><colgroup><col class="cl"><col><col class="cv"><col class="cn"></colgroup><tbody>'
        for l, b, nn in rr:
            t += '<tr><td class="l">after a %s run</td><td class="p">%s</td><td class="v"></td><td class="n">%s</td></tr>' % (l, b, nn)
        html += fig("What the user says next", t + "</tbody></table></div>", "Reaction to each run, by outcome; n=%d runs with a next message." % len(rrows))
    # verdict table
    tb = '<div class="wrap"><table class="tbl sm"><thead><tr><th>Signal</th><th>Flagged</th><th>Short when flagged</th><th>Short when not</th><th>Catches</th><th>Verdict</th></tr></thead><tbody>'
    for name, s, _ in flags:
        if s["n_flag"]:
            tb += '<tr><td>%s</td><td class="n">%d of %d</td><td class="n">%s</td><td class="n">%s</td><td class="n">%s of short runs</td><td>%s</td></tr>' % (
                esc(name), s["n_flag"], s["n"], pct(s["short_flag"]), pct(s["short_clear"]), pct(s["catches"]), esc(s["verdict"]))
        else:
            tb += '<tr><td>%s</td><td class="n">0 of %d</td><td class="n">none flagged</td><td class="n">%s</td><td class="n">none flagged</td><td>none flagged</td></tr>' % (esc(name), s["n"], pct(s["short_clear"]))
    for v in vend:
        tb += '<tr><td>Score %s</td><td class="n">%d of %d</td><td class="n">good above %s in %s of pairs</td><td class="n">50%% is a coin flip</td><td class="n">%d vs %d runs</td><td>%s</td></tr>' % (
            esc(v["label"]), v["n"], N, v["vs"], pct(v["a"]), v["npos"], v["nneg"], esc(v["verdict"]))
    tb += "</tbody></table></div>"
    html += fig("Verdict for each signal", tb, "Short means ambiguous or bad. n=%d graded runs." % N)
    html += "</section>"
    md = ["## Reward Signal", "", sub, "", lede, "", verdict, ""]
    for name, s, _ in flags:
        md.append("- %s: %s (flagged %d of %d; short %s when flagged, %s when not)." % (name, s["verdict"], s["n_flag"], s["n"], pct(s["short_flag"]), pct(s["short_clear"])) if s["n_flag"]
                  else "- %s: none flagged." % name)
    for v in vend:
        md.append("- Score %s: %s (good above %s in %s of pairs, %d vs %d runs)." % (v["label"], v["verdict"], v["vs"], pct(v["a"]), v["npos"], v["nneg"]))
    J = dict(runs=N, overall=overall, verdict=verdict, short_share=round(overall_short, 3), reactions_source=rsrc if rrows else "",
             signals={name: {k: (round(x, 3) if isinstance(x, float) else x) for k, x in s.items()} for name, s, _ in flags},
             raw_scores=[dict(field=v["field"], n=v["n"], pair_share=round(v["a"], 3), vs=v["vs"], verdict=v["verdict"],
                              top_value=v["top_value"], top_share=round(v["top_share"], 3)) for v in vend])
    write(html, "\n".join(md) + "\n", J)
    print("reward ok: %s, %d raw score field(s)" % (overall, len(vend)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
