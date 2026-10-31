"""Section 1, Exploratory Data Analysis. A STARTER: read it, copy it, edit it for this dataset.

Re-run: python3 -I sections/eda.py   ->  eda.html, eda.md, eda.json (reads sig and cig/run_dossiers.jsonl only).

What it draws: one sentence on what the system does (derived from the asks and tool names), a strip of tool families,
three typical good runs and the three worst, representative failures (ask, then what happened), then small multiples:
runs per day, environments, agents, latency / tokens / tool calls split by outcome, top tools, and the mix of asks.

Everything is computed from the data. To replace a derived piece with your own judgement without touching the code, write
sections/eda_notes.json with any of:
  {"does": "one sentence, at most 40 words",
   "families": {"Family name": ["tool_a", "tool_b"]},
   "ask_types": [["Ask type name", "regex matched against the lower-cased first ask"]]}
"""
import json
import math
import os
import re
import sys
from collections import Counter, defaultdict
from html import escape

HERE = os.path.dirname(os.path.abspath(__file__))
STUDY = os.path.dirname(HERE)
sys.path.insert(0, STUDY)
import sig  # noqa: E402

LB = ("good", "ambiguous", "bad")
COL = {"good": "var(--green)", "ambiguous": "var(--muted)", "bad": "var(--bad)"}
esc = lambda s: escape(str(s), quote=True)  # noqa: E731


def fit(text, n):
    """Masked text that ends on a sentence or clause boundary, never on an ellipsis."""
    t = " ".join(sig.mask(text).split())
    if t.endswith(("\u2026", "...")):  # the source itself was cut: end on its last whole sentence or clause
        t = t.rstrip("\u2026.").rstrip()
        k = max(t.rfind(". "), t.rfind("! "), t.rfind("? "))
        if k > 20:
            t = t[:k + 1]
        else:
            k = max(t.rfind(","), t.rfind(";"))
            t = (t[:k] if k > 20 else t.rsplit(" ", 1)[0]).rstrip(" ,;:-") + "."
    if len(t) <= n:
        return t
    sents = re.split(r"(?<=[.!?])\s+", t)
    out = ""
    for x in sents:
        if out and len(out) + 1 + len(x) > n:
            break
        out = (out + " " + x).strip()
    if len(out) <= 3 * n:
        return out
    cut = out[:n]
    k = max(cut.rfind(","), cut.rfind(";"), cut.rfind(" - "))
    return (cut[:k] if k > n // 2 else cut.rsplit(" ", 1)[0]).rstrip(" ,;:-") + "."


short = fit

STOP = set("""a about above after again all also am an and any are as at be because been before being below between both but by can
could did do does doing down during each few for from further get give had has have having he her here hers him his how i if in
into is it its just let like make me more most my need no nor not now of off on once only or other our out over own please same
she should so some such than that the their them then there these they this those through to too under until up us use very was
we were what when where which while who whom why will with would you your yours tell show want see look find know got going
can't cant dont don't thanks thank yes yeah okay ok hi hello hey answer arrived order_id""".split())


# ------------------------------------------------------------------ data
def load():
    R = sig.load()
    path = os.path.join(sig.resolve_out(), "cig", "run_dossiers.jsonl")
    ids = {r["run_id"] for r in R}
    calls = defaultdict(list)
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if str(d.get("run_id")) in ids:
                    for t in d.get("tool_calls") or []:
                        if isinstance(t, dict):
                            fail = t.get("success") is False and not t.get("cancelled")
                            calls[str(d["run_id"])].append((str(t.get("name") or "unknown"), bool(fail)))
    except OSError:
        pass
    for r in R:
        r["calls"] = calls.get(r["run_id"]) or [(t, False) for t in r["tools"]]
    return R


def notes():
    try:
        with open(os.path.join(HERE, "eda_notes.json"), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def stem(w):
    return w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w


def words_of(text):
    return [stem(w) for w in re.findall(r"[a-z][a-z'-]{2,}", text.lower()) if w not in STOP and stem(w) not in STOP]


def ask_types(R, user_rules):
    """-> {run_id: name}. Rules from eda_notes.json when given, else the most common content words of the asks."""
    res, n = {}, len(R)
    if user_rules:
        rules = [(str(a), re.compile(str(b))) for a, b in user_rules]
        for r in R:
            t = r["first_ask"].lower()
            res[r["run_id"]] = next((a for a, p in rules if p.search(t)), "Other asks")
        return res
    docs = {r["run_id"]: set(words_of(r["first_ask"])) for r in R}
    left = [r for r in R if len(r["first_ask"].split()) > 3]
    for r in R:
        if len(r["first_ask"].split()) <= 3:
            res[r["run_id"]] = "Short follow-ups"
    while len(res) < n:
        df = Counter(w for r in left if r["run_id"] not in res for w in docs[r["run_id"]])
        if not df or len(df) and df.most_common(1)[0][1] < max(3, round(0.04 * n)) or len({v for v in res.values()}) >= 7:
            break
        w = sorted(df.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        for r in left:
            if r["run_id"] not in res and w in docs[r["run_id"]]:
                res[r["run_id"]] = 'Asks about "%s"' % w
    for r in R:
        res.setdefault(r["run_id"], "Other asks")
    return res


VERB_FAMILY = [
    ("Look things up", "get list fetch read query lookup check find search describe show view load retrieve track"),
    ("Change things", "create update delete set add remove apply submit send edit move drop cancel rename write post save"),
    ("Compute and compare", "run score evaluate calculate compare analyze analyse estimate rank predict forecast compute"),
]


def tool_family(name, users):
    for fam, tools in users.items():
        if name in tools:
            return fam
    first = re.split(r"[_\-.\s]|(?<=[a-z])(?=[A-Z])", name)[0].lower()
    for fam, verbs in VERB_FAMILY:
        if first in verbs.split():
            return fam
    return "Other tools"


def med(xs):
    return sig.median([x for x in xs if x is not None])


def q(v, p):
    v = sorted(v)
    return v[min(len(v) - 1, int(p * (len(v) - 1) + 0.5))]


def pick(pool, key, k, kind):
    out, seen = [], set()
    for r in sorted(pool, key=key):
        if kind[r["run_id"]] in seen or r["first_ask"][:30] in {o["first_ask"][:30] for o in out}:
            continue
        seen.add(kind[r["run_id"]])
        out.append(r)
        if len(out) == k:
            return out
    for r in sorted(pool, key=key):  # fewer distinct kinds than k: fill up
        if r not in out:
            out.append(r)
            if len(out) == k:
                break
    return out


def what_happened(r):
    t = r["rationale"] or r["final_answer"] or "Finished the run."
    return short(t, 130)


# ------------------------------------------------------------------ figures
def seg(counts, tot, width_pct=100.0, h=14):
    parts = "".join('<i style="width:%.2f%%;background:%s"></i>' % (100.0 * counts.get(l, 0) / tot, COL[l]) for l in LB) if tot else ""
    return '<span class="sb" style="width:%.2f%%;height:%dpx">%s</span>' % (width_pct, h, parts)


def rows_table(items, cols=("", "", "")):
    """items: (label, sub, bar_html, value). A table, so labels never count as prose and columns stay aligned."""
    t = '<div class="wrap"><table class="tbl rk"><colgroup><col class="c1"><col><col class="c3"></colgroup><tbody>'
    for a, b, bar, v in items:
        t += '<tr><td>%s%s</td><td class="b">%s</td><td class="v">%s</td></tr>' % (
            esc(a), ('<span class="tm2">%s</span>' % esc(b)) if b else "", bar, esc(v))
    return t + "</tbody></table></div>"


def neutral(v, mx, fail=0):
    ov = '<i class="ov" style="width:%.2f%%;background:var(--bad)"></i>' % (100.0 * fail / mx) if fail else ""
    return '<span class="sb"><i style="width:%.2f%%;background:var(--strong)"></i>%s</span>' % (100.0 * v / mx, ov)


def perday_svg(R):
    N = len(R)
    days = sorted({r["day"] for r in R if r["day"]})
    if not days:
        return ""
    from datetime import date, timedelta
    binned = len(days) > 40
    key = (lambda d: (date(int(d[:4]), int(d[5:7]), int(d[8:10])) - timedelta(days=date(int(d[:4]), int(d[5:7]), int(d[8:10])).weekday())).isoformat()) if binned else (lambda d: d)
    keys = sorted({key(d) for d in days})
    cnt = {k: Counter() for k in keys}
    for r in R:
        if r["day"]:
            cnt[key(r["day"])][r["label"]] += 1
    W, H, base, top = 340, 130, 100, 22
    mx = max(sum(c.values()) for c in cnt.values())
    bw = W / len(keys)
    s = '<svg viewBox="0 0 %d %d" role="img" aria-label="Runs per %s by outcome, n=%d"><line x1="0" y1="%d" x2="%d" y2="%d" stroke="var(--strong)"/>' % (
        W, H, "week" if binned else "day", N, base, W, base)
    step = max(1, math.ceil(len(keys) / 7)) if (len(keys) > 14 or binned) else 1
    for i, k in enumerate(keys):
        x, w, y = i * bw + bw * 0.18, bw * 0.64, base
        for l in LB:
            h = (base - top) * cnt[k][l] / mx
            y -= h
            if h > 0:
                s += '<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="%s"/>' % (x, y, w, h, COL[l])
        if bw >= 16:
            s += '<text class="t" x="%.1f" y="%.1f" text-anchor="middle">%d</text>' % (x + w / 2, y - 5, sum(cnt[k].values()))
        if i % step == 0 or i == len(keys) - 1 and step == 1:
            lab = k[8:10].lstrip("0") if (step == 1 and not binned) else k[5:]
            s += '<text class="tm" x="%.1f" y="%d" text-anchor="middle">%s</text>' % (x + w / 2, base + 15, lab)
    rng = days[0] if days[0] == days[-1] else "%s to %s" % (days[0], days[-1])
    return s + '<text class="tm" x="0" y="%d">%s</text></svg>' % (H - 2, esc(rng))


def nice_ticks(lo, hi, per_decade=True):
    out = []
    for e in range(0, 9):
        for m in (1, 3):
            v = m * 10.0 ** e
            if lo <= v <= hi:
                out.append(v)
    if lo <= 0:
        out.insert(0, 0)
    return out[:7] or [lo, hi]


def strip(R, by, metric, fmt, title):
    vals = [r[metric] for r in R if r[metric] is not None]
    if len(vals) < 3:
        return ""
    lo, hi = min(vals), max(vals)
    if hi <= lo:
        hi = lo + 1
    W, x0, x1, rh = 340, 96, 322, 28
    f = lambda v: math.log1p(max(v, 0))  # noqa: E731
    sc = lambda v: x0 + (f(v) - f(lo)) / (f(hi) - f(lo)) * (x1 - x0)  # noqa: E731
    ticks = nice_ticks(lo, hi)
    while len(ticks) > 5:
        ticks = ticks[::2]
    H = 28 + 3 * rh + 26
    s = '<svg viewBox="0 0 %d %d" role="img" aria-label="%s, by outcome, n=%d"><text class="tb" x="0" y="12" style="font-size:12px">%s</text>' % (W, H, esc(title), len(vals), esc(title))
    for i, l in enumerate(LB):
        v = [r[metric] for r in by[l] if r[metric] is not None]
        y = 40 + i * rh
        s += '<text class="t" x="0" y="%d">%s</text><text class="tm" x="0" y="%d">n=%d</text>' % (y - 1, l, y + 10, len(v))
        if not v:
            continue
        if len(v) < 8:
            for x in v:
                s += '<circle cx="%.1f" cy="%d" r="3" fill="%s"/>' % (sc(x), y, COL[l])
            continue
        s += '<line x1="%.1f" x2="%.1f" y1="%d" y2="%d" stroke="%s"/>' % (sc(q(v, .1)), sc(q(v, .9)), y, y, COL[l])
        s += '<line x1="%.1f" x2="%.1f" y1="%d" y2="%d" stroke="%s" stroke-width="5"/>' % (sc(q(v, .25)), sc(q(v, .75)), y, y, COL[l])
        m = sig.median(v)
        s += '<circle cx="%.1f" cy="%d" r="3.4" fill="var(--bg)" stroke="%s" stroke-width="1.5"/>' % (sc(m), y, COL[l])
        s += '<text class="t" x="%.1f" y="%d" text-anchor="middle">%s</text>' % (sc(m), y - 8, fmt(m))
    ay = 40 + 2 * rh + 14
    s += '<line x1="%d" x2="%d" y1="%d" y2="%d" stroke="var(--strong)"/>' % (x0, x1, ay, ay)
    for tk in ticks:
        s += '<line x1="%.1f" x2="%.1f" y1="%d" y2="%d" stroke="var(--strong)"/><text class="tm" x="%.1f" y="%d" text-anchor="middle">%s</text>' % (sc(tk), sc(tk), ay, ay + 4, sc(tk), H - 3, fmt(tk))
    return s + "</svg>"


def fs(v):
    return "%.0fs" % v if v < 100 else "%.0fs" % v


def fk(v):
    return "%.0fM" % (v / 1e6) if v >= 1e6 else "%.0fk" % (v / 1e3) if v >= 1e3 else "%.0f" % v


def fn(v):
    return "%.0f" % v


# ------------------------------------------------------------------ build
CSS = """<style>#eda{padding-top:9vh}#eda h2.big{margin-bottom:.3em}#eda .lede{margin:0 0 .6em}#eda .does{color:var(--muted);font-size:14px;max-width:70ch;margin:0 0 1.2em}
#eda .fig{margin:2em 0}#eda .fig figcaption b{font-size:16px;margin-bottom:6px}
#eda .rk{table-layout:fixed;font-size:13px}#eda .rk col.c1{width:36%}#eda .rk col.c3{width:96px}#eda .rk td{padding:6px 10px 6px 0;vertical-align:middle;border-bottom:0}
#eda .tm2{display:block;font-size:11px;color:var(--muted);line-height:1.3}#eda .rk td.v{font-size:12px;color:var(--soft);text-align:right;font-variant-numeric:tabular-nums;padding-right:0}
#eda .sb{display:flex;height:14px;position:relative}#eda .sb i{display:block;height:100%}#eda .sb i.ov{position:absolute;left:0;top:0}
#eda .grid3{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:28px;align-items:start}#eda .grid3 h4{margin:0 0 8px;font-weight:400;font-size:10px;letter-spacing:.18em;text-transform:uppercase;color:var(--muted)}
#eda .big3{font-size:40px;letter-spacing:-.05em;line-height:1;font-weight:300}
#eda svg{max-width:100%}#eda .tbl.ex td{font-size:13px;color:var(--soft)}#eda .tbl.ex td b{font-weight:400;color:var(--ink)}#eda .tbl .n{white-space:nowrap;font-variant-numeric:tabular-nums}
#eda .lg{font-size:11px;color:var(--muted);margin-top:6px}#eda .lg i{display:inline-block;width:9px;height:9px;margin:0 4px 0 10px}#eda .lg i:first-child{margin-left:0}
@media(max-width:760px){#eda .grid3{grid-template-columns:1fr}#eda .rk col.c1{width:38%}#eda .rk col.c3{width:80px}
#eda .tbl.ex,#eda .tbl.ex tbody,#eda .tbl.ex tr,#eda .tbl.ex td{display:block}#eda .tbl.ex thead{display:none}#eda .tbl.ex td{border:0;padding:3px 0}#eda .tbl.ex tr{border-bottom:1px solid var(--line);padding:9px 0}#eda .tbl.ex td.n{display:inline-block;margin-right:12px}}</style>"""
LEGEND = '<div class="lg"><i style="background:var(--green)"></i>good<i style="background:var(--muted)"></i>ambiguous<i style="background:var(--bad)"></i>bad</div>'


def run_table(rows):
    t = '<div class="wrap"><table class="tbl ex"><thead><tr><th>Ask</th><th>What happened</th><th>Size</th><th>Run</th></tr></thead><tbody>'
    for r in rows:
        size = ("%d calls" % r["n_calls"]) + (("<br>%.0fs" % r["lat"]) if r["lat"] is not None else "")
        t += '<tr><td>%s</td><td>%s</td><td class="n">%s</td><td class="n"><code>%s</code></td></tr>' % (
            esc(short(r["first_ask"], 110)), esc(what_happened(r)), size, esc(r["run_id"][:8]))
    return t + "</tbody></table></div>"


PATTERNS = [("Ended with no answer", lambda r: r["no_answer"]), ("A failed call ended the run", lambda r: r["unrecovered"]),
            ("Recovered from a failed call, still fell short", lambda r: r["tool_fail"]), ("Took too long", lambda r: r["slow"]),
            ("Piled up tool calls", lambda r: r["heavy"]), ("Answered, but fell short", lambda r: True)]


def main():
    R = load()
    N = len(R)
    if not N:
        for ext, body in (("html", '<section id="eda" class="step"><h2 class="big">Exploratory Data Analysis</h2><p class="sub">What the system does and how its runs look.</p><p class="lede">No graded runs yet.</p></section>'),
                          ("md", "## Exploratory Data Analysis\n\n- No graded runs yet.\n"), ("json", "{}")):
            with open(os.path.join(HERE, "eda." + ext), "w", encoding="utf-8") as f:
                f.write(body)
        return 0
    nt = notes()
    by = {l: [r for r in R if r["label"] == l] for l in LB}
    kind = ask_types(R, nt.get("ask_types"))
    allcalls = [(n, f) for r in R for n, f in r["calls"]]
    NT = len(allcalls)
    tc, tf = Counter(n for n, _ in allcalls), Counter(n for n, f in allcalls if f)
    users = {k: set(v) for k, v in (nt.get("families") or {}).items()}
    fam = Counter(tool_family(n, users) for n, _ in allcalls)
    famtools = {k: Counter(n for n, _ in allcalls if tool_family(n, users) == k).most_common(3) for k in fam}
    mL, mC, mT = med(r["lat"] for r in R), med(r["n_calls"] for r in R), med(r["tokens"] for r in R)
    lat_ok = any(r["lat"] is not None for r in R)

    def dist(r):
        d = abs(math.log(r["n_calls"] + 1) - math.log((mC or 0) + 1)) + abs(math.log(r["tokens"] + 1) - math.log((mT or 0) + 1))
        if lat_ok and r["lat"] is not None:
            d += abs(math.log(r["lat"] + 1) - math.log((mL or 0) + 1))
        return d
    subst = lambda r: len(r["first_ask"].split()) >= 4 and r["first_ask"].strip() != r["next_ask"].strip()  # noqa: E731
    good_pool = [r for r in by["good"] if subst(r) and r["final_answer"]] or by["good"]
    good3 = pick(good_pool, lambda r: (dist(r), r["run_id"]), 3, kind)
    bad_pool = [r for r in by["bad"] if subst(r)] or by["bad"]
    bad_pool = bad_pool + [r for r in by["ambiguous"] if subst(r)] if len(bad_pool) < 3 else bad_pool
    worst3 = pick(bad_pool, lambda r: (r["outcome"], -r["unrecovered"], -r["n_fail"], -(r["lat"] or 0), r["run_id"]), 3, kind)
    # representative failures
    short_runs = [r for r in R if r["outcome"] != 1]
    pat_of = {r["run_id"]: next(n for n, f in PATTERNS if f(r)) for r in short_runs}
    pc = Counter(pat_of.values())
    fail_rows = []
    for name, c in pc.most_common(5):
        pool = sorted([r for r in short_runs if pat_of[r["run_id"]] == name], key=lambda r: (r["outcome"], -r["conf"], r["run_id"]))
        ex = next((r for r in pool if subst(r)), pool[0])
        fail_rows.append((name, c, ex))
    # numbers
    days = sorted({r["day"] for r in R if r["day"]})
    envs = Counter(r["env"] for r in R)
    agents = Counter(r["agent"] for r in R if r["agent"])
    convs = len({r["conversation_id"] for r in R})
    good_pct = sig.pct(len(by["good"]), N)
    mlat = {l: med(r["lat"] for r in by[l]) for l in LB}
    # sentences
    wc = Counter(w for r in R if len(r["first_ask"].split()) > 3 for w in set(words_of(r["first_ask"])))
    topw = [w for w, _ in sorted(wc.items(), key=lambda kv: (-kv[1], kv[0]))[:3]]
    toptools = [n for n, _ in tc.most_common(3)]
    j = lambda xs: ", ".join(xs[:-1]) + " and " + xs[-1] if len(xs) > 1 else (xs[0] if xs else "")  # noqa: E731
    does = nt.get("does") or ("Users mostly ask about %s; the agent answers by calling %s." % (j(topw), j(toptools)) if topw and toptools else
                              "Users mostly ask about %s; the agent answers without calling tools." % j(topw) if topw else "The agent answers each ask in a single step.")
    gl, bl = mlat["good"], mlat["bad"]
    if gl and bl:
        ratio = bl / gl
        speed = "bad runs take about as long as good ones" if 0.8 <= ratio <= 1.25 else ("bad runs take %.1fx as long as good ones" % ratio if ratio > 1.25 else "bad runs finish faster than good ones")
        speed_title = speed[0].upper() + speed[1:]
    else:
        speed, speed_title = "", "Latency, calls and tokens by outcome"
    lede = "%s%d runs, %d%% good%s." % (("A typical run takes %.0fs and makes %.0f tool calls. " % (mL, mC or 0)) if (lat_ok and mL is not None) else
                                       ("A typical run makes %.0f tool calls. " % (mC or 0)), N, good_pct, ("; " + speed) if speed else "")
    if N < 30:
        lede += " With only %d runs, read these shapes as hints." % N
    sub = "What the system does and what its runs look like."
    # figures
    FN = [0]

    def fig(title, body, cap=""):
        FN[0] += 1
        return '<figure class="fig"><figcaption><span class="kicker">Figure</span><b>%s</b>%s</figcaption>%s</figure>' % (esc(title), esc(cap), body)

    fmax = max(fam.values())
    f_fam = rows_table([(k, ", ".join(t for t, _ in famtools[k]), neutral(fam[k], fmax), "%s | %d%%" % (format(fam[k], ","), sig.pct(fam[k], NT)))
                        for k in sorted(fam, key=lambda x: -fam[x])]) if NT else ""
    f_env = rows_table([(e, "%d%% of runs" % sig.pct(c, N), seg(Counter(r["label"] for r in R if r["env"] == e), c if c else 1, 100.0 * c / max(envs.values())), str(c))
                        for e, c in envs.most_common()])
    if agents:
        f_ag = rows_table([(a, "", seg(Counter(r["label"] for r in R if r["agent"] == a), c, 100.0 * c / max(agents.values())), str(c))
                           for a, c in agents.most_common(5)])
    else:
        f_ag = '<div class="big3">1</div>'
    s_lat = strip(R, by, "lat", fs, "Seconds per run") if lat_ok else ""
    s_tok = strip(R, by, "tokens", fk, "Tokens per run") if any(r["tokens"] for r in R) else ""
    s_cal = strip(R, by, "n_calls", fn, "Tool calls per run") if any(r["n_calls"] for r in R) else ""
    strips = [s for s in (s_lat, s_tok, s_cal) if s]
    top = tc.most_common(10)
    f_top = rows_table([(n, ("%d failed" % tf[n]) if tf[n] else "none failed", neutral(c, top[0][1], tf[n]), "%s | %d%% fail" % (format(c, ","), sig.pct(tf[n], c)))
                        for n, c in top]) if top else ""
    at = Counter(kind.values())
    amax = max(at.values())
    order = sorted(at, key=lambda k: (k in ("Other asks", "Short follow-ups"), -at[k], k))
    f_ask = rows_table([(k, "%d%% good" % sig.pct(sum(1 for r in R if kind[r["run_id"]] == k and r["label"] == "good"), at[k]),
                         seg(Counter(r["label"] for r in R if kind[r["run_id"]] == k), at[k], 100.0 * at[k] / amax), str(at[k])) for k in order])
    t_fail = '<div class="wrap"><table class="tbl ex"><thead><tr><th>Pattern</th><th>Runs</th><th>Example: ask, then what happened</th><th>Run</th></tr></thead><tbody>' + "".join(
        '<tr><td><b>%s</b></td><td class="n">%d</td><td>%s &rarr; %s</td><td class="n"><code>%s</code></td></tr>' % (
            esc(n), c, esc(short(r["first_ask"], 70)), esc(short(r["rationale"] or r["final_answer"] or "Fell short of the ask.", 100)), esc(r["run_id"][:8]))
        for n, c, r in fail_rows) + "</tbody></table></div>"
    html = CSS + '<section id="eda" class="step"><p class="kicker">Section 1 &middot; Exploratory Data Analysis</p><h2 class="big">Exploratory Data Analysis</h2>'
    html += '<p class="sub">%s</p><p class="lede">%s</p><p class="does">%s</p>' % (esc(sub), esc(lede), esc(does))
    if f_fam:
        html += fig("Tool calls by family", f_fam, "%s tool calls in %d runs; %d runs used none." % (format(NT, ","), N, sum(1 for r in R if r["n_calls"] == 0)))
    html += fig("Three typical good runs", run_table(good3), "Closest to the median run; n=%d good runs." % len(by["good"]))
    html += fig("Three worst runs", run_table(worst3), "Most failed calls and longest first; n=%d runs that fell short." % len(short_runs))
    html += fig("How runs fall short", t_fail, "n=%d runs that fell short; one pattern per run." % len(short_runs))
    one = ('<div><h4>Runs per day</h4>%s%s</div><div><h4>Environment</h4>%s</div><div><h4>Agents</h4>%s</div>' % (perday_svg(R), LEGEND, f_env, f_ag))
    html += fig("%d runs across %d day%s" % (N, len(days) or 1, "" if len(days) == 1 else "s"), '<div class="grid3">%s</div>' % one, "Share of runs by outcome; n=%d." % N)
    if strips:
        html += fig(speed_title, '<div class="grid3">%s</div>' % "".join("<div>%s</div>" % s for s in strips),
                    "Dot is the median, thick bar the middle half, thin line 10th to 90th percentile; n=%d." % N)
    if f_top:
        html += fig("Top tools: calls and failures", f_top, "Red marks failed calls. Top %d of %d tools; n=%s calls." % (len(top), len(tc), format(NT, ",")))
    html += fig("First ask, by outcome", f_ask + LEGEND, "n=%d runs; first message of each run, grouped by its main topic word." % N)
    html += "</section>"
    J = dict(runs=N, conversations=convs, tool_calls=NT, tools=len(tc), agents=len(agents) or 1, days=len(days), env=dict(envs),
             median_s=None if mL is None else round(mL, 1), median_calls=mC, median_tokens=None if mT is None else round(mT),
             median_s_by={l: None if mlat[l] is None else round(mlat[l], 1) for l in LB}, good_pct=good_pct,
             families=dict(fam), patterns=dict(pc), ask_types=dict(at), top_tool=top[0] if top else None,
             good_examples=[r["run_id"][:8] for r in good3], worst_examples=[r["run_id"][:8] for r in worst3])
    md = ["## Exploratory Data Analysis", "", sub, "", lede, "", does, "",
          "- %d runs, %d conversations, %s tool calls across %d tools, %d day(s)." % (N, convs, format(NT, ","), len(tc), len(days)),
          "- Good, ambiguous, bad: %d, %d, %d." % tuple(len(by[l]) for l in LB)]
    if top:
        md.append("- Busiest tool: %s, %s calls, %d%% failed." % (top[0][0], format(top[0][1], ","), sig.pct(tf[top[0][0]], top[0][1])))
    md.append("- How runs fall short: " + "; ".join("%s %d" % (n, c) for n, c, _ in fail_rows) + ".")
    md.append("- Environments: " + ", ".join("%s %d" % kv for kv in envs.most_common()) + ".")
    for ext, body in (("html", html), ("md", "\n".join(md) + "\n"), ("json", json.dumps(J, indent=1, default=str))):
        with open(os.path.join(HERE, "eda." + ext), "w", encoding="utf-8") as f:
            f.write(body)
    print("eda ok: %d runs" % N)
    return 0


if __name__ == "__main__":
    sys.exit(main())
