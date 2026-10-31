"""Assemble the study page from the section outputs. Re-runnable: python3 -I build_study.py [out]

Reads <out>/study/sections/<key>.html (and <key>.md) for the five sections in this order: eda, reward, affinity, empathy,
impact. They are numbered 1-5 by position (a missing section is skipped and the numbers close up). Writes
<out>/study/study.html (one self-contained file: shell.css inlined, system fonts, no network) and <out>/study/STUDY.md.
The masthead needs only sig.load(). Writes the folder marker .procept-lite-study when it is missing.
"""
import os
import re
import sys
from collections import Counter
from html import escape

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import sig  # noqa: E402

MARKER = ".procept-lite-study"
SECTIONS = [("eda", "Exploratory Data Analysis", "What the system does and how runs look"),
            ("reward", "Reward Signal", "Which quality signals track real outcomes"),
            ("affinity", "Affinity Diagram", "How users were badly served, grouped by theme"),
            ("empathy", "Empathy Map", "User personas by goal, and where each is served well or badly"),
            ("impact", "Impact", "Ranked fixes and what each could change")]
FOOT = "Outcome labels are model judgements of each run against what the user asked, not a direct measure of user satisfaction."
JS = """<script>(function(){var r=document.documentElement,b=document.getElementById('th'),k='procept-study-theme';try{var s=localStorage.getItem(k);if(s)r.dataset.theme=s}catch(e){}
b.onclick=function(){var d=(r.dataset.theme||(matchMedia('(prefers-color-scheme:light)').matches?'light':'dark'))==='dark'?'light':'dark';r.dataset.theme=d;try{localStorage.setItem(k,d)}catch(e){}};
var L=[].slice.call(document.querySelectorAll('header.top nav a'));addEventListener('scroll',function(){var y=scrollY+120,c=null;L.forEach(function(a){var e=document.querySelector(a.getAttribute('href'));if(e&&e.offsetTop<=y)c=a});L.forEach(function(a){a.classList.toggle('on',a===c)})},{passive:true})})()</script>"""


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def renumber(x, label, h):
    """The section's first kicker becomes 'Section x . Label' (added when absent); figure kickers become 'Figure x.n'."""
    kick = '<p class="kicker">Section %d &middot; %s</p>' % (x, escape(label))
    h, n = re.subn(r'<(p|div) class="kicker">[^<]*</\1>', lambda m: kick, h, count=1)
    if not n:
        h = re.sub(r"(<section\b[^>]*>)", lambda m: m.group(1) + kick, h, count=1)
    it = iter(range(1, 1000))
    return re.sub(r'<span class="kicker">Figure[^<]*</span>', lambda m: '<span class="kicker">Figure %d.%d</span>' % (x, next(it)), h)


def md_body(text):
    """A section's markdown without its own leading heading (the assembler writes the numbered one)."""
    lines = text.strip().splitlines()
    if lines and lines[0].lstrip().startswith("#"):
        lines = lines[1:]
    return "\n".join(lines).strip()


def span_text(R):
    d0, d1 = (R[0]["day"], R[-1]["day"]) if R else ("", "")
    return d0 if d0 == d1 else "%s to %s" % (d0, d1) if d0 and d1 else ""


def build(out=None):
    """Write study.html and STUDY.md under <out>/study. -> {'html': path, 'md': path, 'sections': [keys present]}"""
    out = sig.resolve_out(out)
    study = os.path.join(out, "study")
    sec = os.path.join(study, "sections")
    R = sig.load(out)
    present = [(k, t, d) for k, t, d in SECTIONS if os.path.isfile(os.path.join(sec, k + ".html"))]
    css_path = os.path.join(study, "shell.css")
    css = read(css_path if os.path.isfile(css_path) else os.path.join(HERE, "shell.css"))
    n = len(R)
    c = Counter(r["label"] for r in R)
    when = span_text(R)
    kick = " &middot; ".join(x for x in ("%d runs" % n, escape(when)) if x)
    nav = "".join('<a href="#%s">%d %s</a>' % (k, i, escape(t)) for i, (k, t, _) in enumerate(present, 1))
    toc = "".join('<li><a href="#%s"><span class="tn">%d</span><span class="tt">%s</span><span class="td">%s</span></a></li>'
                  % (k, i, escape(t), escape(d)) for i, (k, t, d) in enumerate(present, 1))
    body = "".join(renumber(i, t, read(os.path.join(sec, k + ".html"))) for i, (k, t, _) in enumerate(present, 1))
    page = ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>Procept study</title><style>%s</style></head><body>'
            '<header class="top"><span class="brand">Procept study</span><nav>%s</nav><button id="th" aria-label="Toggle theme">Light / Dark</button></header>'
            '<main><section class="mast"><p class="kicker">%s</p><h1>Procept study</h1><p class="lede">Each run read against what the user asked.</p>'
            '<nav class="toc" aria-label="Contents"><p class="kicker">Contents</p><ol>%s</ol></nav></section>%s<footer class="end">%s</footer></main>%s</body></html>'
            % (css, nav, kick, toc, body, escape(FOOT), JS))
    md = "# Procept study\n\n%s\n\n" % ", ".join(x for x in (
        "%d runs" % n, when, "%d%% good, %d%% ambiguous, %d%% bad" % (sig.pct(c["good"], n), sig.pct(c["ambiguous"], n), sig.pct(c["bad"], n))) if x)
    for i, (k, t, _) in enumerate(present, 1):
        p = os.path.join(sec, k + ".md")
        md += "## %d. %s\n\n%s\n\n" % (i, t, md_body(read(p)) if os.path.isfile(p) else "")
    md += FOOT + "\n"
    os.makedirs(study, exist_ok=True)
    paths = {"html": os.path.join(study, "study.html"), "md": os.path.join(study, "STUDY.md"),
             "sections": [k for k, _, _ in present]}
    for p, text in ((paths["html"], page), (paths["md"], md)):
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
    marker = os.path.join(study, MARKER)
    if not os.path.exists(marker):
        with open(marker, "w", encoding="utf-8") as f:
            f.write("procept-lite study folder (temporary). `plite.py study-clean` deletes it.\n")
    return paths


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        p = build(argv[0] if argv else None)
    except (OSError, ValueError) as e:
        print("build_study: %s" % e, file=sys.stderr)
        return 2
    print("wrote %s (%d bytes) with %d of %d sections: %s" % (
        p["html"], os.path.getsize(p["html"]), len(p["sections"]), len(SECTIONS), ", ".join(p["sections"]) or "none"))
    print("wrote %s" % p["md"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
