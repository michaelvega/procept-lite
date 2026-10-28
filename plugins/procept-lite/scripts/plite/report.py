"""Write the one-page REPORT.md and REPORT.docx from cig/ (+ scores.csv)."""
import argparse
import glob
import math
import os
import re
import shutil
import string
import subprocess
import sys
import tempfile

try:
    from . import estimates as est
    from . import scorecard as scm
    from .docx_writer import write_docx
except ImportError:  # imported as a top-level module
    import estimates as est
    import scorecard as scm
    from docx_writer import write_docx

MAX_BODY_WORDS = 460
PAGE_PT = (15840 - 2 * 1008) / 20.0  # printable height in points
DEFAULT_TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..",
                                "templates", "report.template.md")
FOOTER = ("Labels are reader-agent judgements of whether the user's goal was met, not user "
          "satisfaction. Estimates are bounds derived from these runs, not forecasts.")


def _p(x):
    return "%d%%" % round(x)


def _n(x):
    return "{:,}".format(int(round(x)))


def _pl(n, word, plural=None):
    """pluralize: '1 run', '2 runs'."""
    return "%s %s" % (_n(n), word if n == 1 else plural or word + "s")


def _tok(x):
    if x is None:
        return "-"
    if x >= 1e6:
        return "%.1fM" % (x / 1e6)
    return "%.1fk" % (x / 1000.0) if x >= 1000 else str(int(round(x)))


def _sec(x):
    if x is None:
        return "-"
    if x >= 3600:
        return "%.1fh" % (x / 3600.0)
    if x >= 90:
        return "%.1fm" % (x / 60.0)
    return "%.1fs" % x


def _cut(s, k):
    s = " ".join(str(s).split())
    return s if len(s) <= k else s[:max(k - 3, 1)].rstrip() + "..."


# ---------------------------------------------------------------- content
def metrics_table(sc):
    cols = [("Good", sc["by"]["good"]), ("Ambiguous", sc["by"]["ambiguous"]),
            ("Bad", sc["by"]["bad"]), ("All runs", sc["all"])]
    if not sc["graded"]:
        cols = [("All runs", sc["all"])]
    n = sc["n"]
    cost = sc["has_cost"]

    def row(label, f):
        return [label] + [f(g) if g["n"] else "-" for _, g in cols]

    rows = [["Metric"] + [c for c, _ in cols],
            row("Runs", lambda g: "%d (%s)" % (g["n"], _p(scm.pct(g["n"], n)))),
            row("Errors: runs with a tool failure", lambda g: _p(g["tool_fail_pct"])),
            row("Errors: last call failed", lambda g: _p(g["unrecovered_pct"])),
            row("Errors: ended with no answer", lambda g: _p(g["no_answer_pct"])),
            row("Cost: median tokens" + (" ($)" if cost else ""),
                lambda g: "-" if g["median_tokens"] is None else _tok(g["median_tokens"]) + (
                    " ($%.3f)" % g["median_cost"] if g["median_cost"] is not None else "")),
            row("Cost: median tool calls", lambda g: "%g" % g["median_tools"]),
            row("Latency: median", lambda g: _sec(g["median_latency"])),
            row("Latency: in slowest decile",
                lambda g: _p(g["slow_pct"]) if g["median_latency"] is not None else "-")]
    return rows


def personas_table(sc, goal_len=48, pain_len=44):
    if not sc["graded"]:
        return None
    rows = [["Buyer persona", "Runs", "Goal (common ask)", "Fell short", "Top pain"]]
    for p in sc["personas"]:
        rows.append([_cut(p["persona"], 44), str(p["n"]), _cut(p["goal"], goal_len) or "-",
                     _p(p["fell_short_pct"]), _cut(p["top_pain"], pain_len) or "-"])
    return rows


def reach_lines(sc):
    g, c, e, l = est.goals(sc), est.cost(sc), est.errors(sc), est.latency(sc)
    L = []
    if g:
        L.append("**User goals:** good share is %s now. If all %s bad or ambiguous with a "
                 "mechanical problem (tool failure, no answer, heavy or slow) were fixed it would "
                 "be at most %s; fixing half of them gives about %s."
                 % (_p(g["now"]), _pl(g["addressable"], "run"), _p(g["ceiling"]), _p(g["half"])))
    else:
        L.append("**User goals:** not graded yet, so no goal-reach bound; run the grading step.")
    if c:
        spend = "%s tokens" % _tok(c["total"])
        if c["cost_total"] is not None:
            spend += " ($%.2f)" % c["cost_total"]
        if c["graded"]:
            sup = ("A 50%% cut is supported by the data (levers reach %s)." % _p(c["union_pct"])
                   if c["supported"] else
                   "A 50%% cut is not supported: the levers reach only %s, %d points short."
                   % (_p(c["union_pct"]), round(c["gap"])))
            L.append("**Cost:** of %s, %s sit in bad or ambiguous runs, %s is excess of heavy runs "
                     "over the median run, and about %s is spent on calls after a failure. %s "
                     "Upper bound, levers counted once per run."
                     % (spend, _p(c["short_pct"]), _p(c["heavy_pct"]), _p(c["retry_pct"]), sup))
        else:
            sup = ("supported" if c["heavy_pct"] + c["retry_pct"] >= 50 else "not supported")
            L.append("**Cost:** of %s, %s is excess of heavy runs over the median and about %s is "
                     "spent after a failure; a 50%% cut is %s by these two levers (not graded yet, "
                     "so wasted-on-bad-runs is unknown)."
                     % (spend, _p(c["heavy_pct"]), _p(c["retry_pct"]), sup))
    else:
        L.append("**Cost:** no token data in the runs, so no cost bound.")
    if e:
        after = (" to %s" % ("%.1f%%" % e["after"]) if e["after"] is not None else "")
        if e["after"] is not None:
            L.append("**Errors:** %.1f%% of tool calls failed (%d of %s). Fixing the top %s "
                     "(%s) would lower this%s."
                     % (e["now"], e["fails"], _pl(e["calls"], "tool call"),
                        "error class" if len(e["top"]) == 1 else "%d error classes" % len(e["top"]),
                        ", ".join(_cut(k, 24) for k, _ in e["top"]), after))
        else:
            L.append("**Errors:** %.1f%% of tool calls failed (%d of %s); no error classes were "
                     "recorded, so no after-fix bound." % (e["now"], e["fails"], _pl(e["calls"], "tool call")))
    else:
        L.append("**Errors:** no tool calls recorded, so no error bound.")
    if l and not l["tail"]:
        L.append("**Latency:** median %s and p90 %s; there is no slow tail to bring down."
                 % (_sec(l["med"]), _sec(l["p90"])))
    elif l:
        L.append("**Latency:** median %s and p90 %s now. If the slowest decile (%s, %s of "
                 "total time) were brought down to the median, p90 would be %s and the median %s."
                 % (_sec(l["med"]), _sec(l["p90"]), _pl(l["n_slow"], "run"), _p(l["slow_share"]),
                    _sec(l["p90_after"]), _sec(l["med_after"])))
    else:
        L.append("**Latency:** no latency data, so no latency bound.")
    return L


def explanation(sc, nsent):
    a, n = sc["all"], sc["n"]
    e, l, c = est.errors(sc), est.latency(sc), est.cost(sc)
    S = []
    if sc["graded"]:
        sp = sc["split"]
        S.append("We read %s from %s: %s good, %s ambiguous and %s bad on the "
                 "user's own goal." % (_pl(n, "run"), _pl(sc["n_conversations"], "conversation"), _p(scm.pct(sp["good"], n)),
                                       _p(scm.pct(sp["ambiguous"], n)), _p(scm.pct(sp["bad"], n))))
    else:
        S.append("We read %s from %s; they are not graded yet, so goal "
                 "outcomes below are missing." % (_pl(n, "run"), _pl(sc["n_conversations"], "conversation")))
    if e:
        S.append("%d of %s failed; %s of runs ended on a failed call and %s ended "
                 "without an answer." % (e["fails"], _pl(e["calls"], "tool call"), _p(a["unrecovered_pct"]),
                                         _p(a["no_answer_pct"])))
    if sc["graded"] and sc["by"]["bad"]["n"] and sc["by"]["good"]["n"]:
        b, g = sc["by"]["bad"], sc["by"]["good"]
        if b["median_tokens"] is None or g["median_tokens"] is None:
            S.append("Bad runs use a median of %g tool calls against %g for good runs."
                     % (b["median_tools"], g["median_tools"]))
        else:
            S.append("Bad runs use a median of %s tokens and %g tool calls against %s and %g for "
                     "good runs." % (_tok(b["median_tokens"]), b["median_tools"],
                                     _tok(g["median_tokens"]), g["median_tools"]))
    if l and l["tail"]:
        S.append("Median latency is %s, but the slowest tenth of runs holds %s of all time."
                 % (_sec(l["med"]), _p(l["slow_share"])))
    if sc["personas"]:
        p = sc["personas"][0]
        s = "The largest persona, %s (%s), falls short %s of the time" % (
            _cut(p["persona"], 28), _pl(p["n"], "run"), _p(p["fell_short_pct"]))
        S.append(s + (", mostly on \"%s\"." % _cut(p["top_pain"], 30) if p["top_pain"] else "."))
    if c and c["graded"]:
        S.append("Money is mostly spent where it does not pay: %s of tokens sit in bad or "
                 "ambiguous runs." % _p(c["short_pct"]))
    return " ".join(S[:nsent])


def build_blocks(sc, level):
    """level 0..4: progressively shorter text to stay on one page."""
    nsent, nfix = [6, 5, 5, 4, 4][level], [6, 5, 4, 4, 4][level]
    goal_len, pain_len = [62, 54, 48, 42, 38][level], [56, 50, 46, 42, 38][level]
    title = "Agent Run Scorecard"
    sub = "%s, %s. Unit of analysis: the run." % (_pl(sc["n"], "run"), _pl(sc["n_conversations"], "conversation"))
    if not sc["graded"]:
        sub += " **Not graded yet**: outcome columns and personas appear after grading."
    B = [("title", title), ("p", sub), ("h2", "Scorecard")]
    sp = sc["split"]
    if sc["graded"]:
        B.append(("p", "**Outcome split:** %d good (%s), %d ambiguous (%s), %d bad (%s)%s."
                  % (sp["good"], _p(scm.pct(sp["good"], sc["n"])), sp["ambiguous"],
                     _p(scm.pct(sp["ambiguous"], sc["n"])), sp["bad"],
                     _p(scm.pct(sp["bad"], sc["n"])),
                     "; %d ungraded" % sc["ungraded"] if sc["ungraded"] else "")))
    B.append(("table", metrics_table(sc), None))
    pt = personas_table(sc, goal_len, pain_len)
    if pt:
        B.append(("table", pt, [3, 1, 4, 1.4, 3]))
    B.append(("h2", "What we can reach"))
    for line in reach_lines(sc):
        B.append(("bullet", line))
    B.append(("h2", "What the data says"))
    B.append(("p", explanation(sc, nsent)))
    B.append(("h2", "How to fix it"))
    fx = est.fixes(sc, nfix)
    for text, k in fx:
        B.append(("bullet", "%s. Runs touched: %d." % (text, k)))
    if not fx:
        B.append(("bullet", "No failure patterns found in these runs."))
    B.append(("small", FOOTER))
    return B


def body_words(blocks):
    t = 0
    for b in blocks:
        if b[0] != "table":
            t += len(re.sub(r"[*`]", "", b[1]).split())
    return t


def est_height(blocks):
    """Rough printed height in points (Calibri 10pt, 7.1in text width)."""
    h = 0.0
    cpl = 108.0
    for b in blocks:
        k = b[0]
        if k == "table":
            rows, w = b[1], b[2] or [1] * len(b[1][0])
            tot = float(sum(w))
            for r in rows:
                lines = max(math.ceil(len(str(c)) / max(cpl * 1.1 * w[i] / tot - 1, 4))
                            for i, c in enumerate(r))
                h += lines * 10.6 + 3
            h += 6
        elif k == "title":
            h += 24
        elif k == "h2":
            h += 20
        else:
            size = 9 if k == "small" else 12.3
            h += math.ceil(len(re.sub(r"[*`]", "", b[1])) / (cpl * (1.1 if k == "small" else 1)
                                                            - (3 if k == "bullet" else 0))) * size + 3
    return h


def fit(sc):
    for level in range(5):
        blocks = build_blocks(sc, level)
        if body_words(blocks) <= MAX_BODY_WORDS and est_height(blocks) <= PAGE_PT * 0.95:
            return blocks, level
    return blocks, 4


# ---------------------------------------------------------------- output
def _md_table(rows):
    esc = lambda c: str(c).replace("|", "/")
    out = ["| " + " | ".join(esc(c) for c in rows[0]) + " |",
           "|" + "|".join("---" for _ in rows[0]) + "|"]
    out += ["| " + " | ".join(esc(c) for c in r) + " |" for r in rows[1:]]
    return "\n".join(out)


def render_md(sc, blocks, template_path=None):
    def sect(name):  # blocks between h2 `name` and the next h2
        res, on = [], False
        for b in blocks:
            if b[0] == "h2":
                on = b[1] == name
                continue
            if on:
                res.append(b)
        return res

    def txt(bs):
        parts = []
        for b in bs:
            if b[0] == "small":
                continue
            parts.append(_md_table(b[1]) if b[0] == "table"
                         else "- " + b[1] if b[0] == "bullet" else b[1])
        return "\n".join(p if p.startswith("- ") else p for p in parts)

    sc_blocks = sect("Scorecard")
    tables = [b for b in sc_blocks if b[0] == "table"]
    split = [b for b in sc_blocks if b[0] == "p"]
    subtitle = blocks[1][1]
    tpl_path = template_path or DEFAULT_TEMPLATE
    try:
        with open(tpl_path, encoding="utf-8") as f:
            tpl = f.read()
    except OSError:
        tpl = ("# $title\n\n$subtitle\n\n## Scorecard\n\n$split\n\n$metrics\n\n$personas\n\n"
               "## What we can reach\n\n$reach\n\n## What the data says\n\n$explanation\n\n"
               "## How to fix it\n\n$fixes\n\n*$footer*\n")
    footer = [b for b in blocks if b[0] == "small"][0][1]
    return string.Template(tpl).safe_substitute(
        title=blocks[0][1], subtitle=subtitle, split=txt(split),
        metrics=_md_table(tables[0][1]),
        personas=("**Top buyer personas**\n\n" + _md_table(tables[1][1])) if len(tables) > 1 else "",
        reach=txt(sect("What we can reach")), explanation=txt(sect("What the data says")),
        fixes=txt(sect("How to fix it")), footer=footer)


def check_pages(docx_path, blocks):
    """Real page count via soffice when present; otherwise a word/line-height estimate."""
    exe = shutil.which("soffice") or shutil.which("libreoffice")
    for cand in ("/Applications/LibreOffice.app/Contents/MacOS/soffice",):
        exe = exe or (cand if os.path.exists(cand) else None)
    if exe:
        tmp = tempfile.mkdtemp()
        try:
            subprocess.run([exe, "--headless", "--convert-to", "pdf", "--outdir", tmp, docx_path],
                           capture_output=True, timeout=120)
            pdfs = glob.glob(os.path.join(tmp, "*.pdf"))
            if pdfs:
                with open(pdfs[0], "rb") as f:
                    pages = len(re.findall(rb"/Type\s*/Page[^s]", f.read()))
                return {"method": "soffice", "pages": pages}
        except (OSError, subprocess.SubprocessError):
            pass
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    h = est_height(blocks)
    return {"method": "estimate (no soffice: words and line heights)",
            "pages": int(math.ceil(h / PAGE_PT)), "words": body_words(blocks),
            "fill": round(h / PAGE_PT, 2)}


def build(out, template=None):
    sc = scm.compute(out)
    if not sc["n"]:
        raise SystemExit("report: no runs found in %s (run `cig` first)"
                         % os.path.join(out, "cig", "runs.csv"))
    blocks, level = fit(sc)
    md = os.path.join(out, "REPORT.md")
    dx = os.path.join(out, "REPORT.docx")
    with open(md, "w", encoding="utf-8") as f:
        f.write(render_md(sc, blocks, template))
    write_docx(dx, blocks)
    info = check_pages(dx, blocks)
    info.update(md=md, docx=dx, level=level, graded=sc["graded"])
    return info


def main(argv=None):
    ap = argparse.ArgumentParser(prog="plite report", description="Write REPORT.md and REPORT.docx.")
    ap.add_argument("--out", default="./procept-out")
    ap.add_argument("--template", default=None)
    a = ap.parse_args(argv)
    info = build(a.out, a.template)
    print("wrote %s and %s" % (info["md"], info["docx"]))
    print("pages: %s (%s)%s" % (info["pages"], info["method"],
                                "" if info["graded"] else "; not graded yet"))
    if info["pages"] > 1:
        print("warning: report may exceed one page", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
