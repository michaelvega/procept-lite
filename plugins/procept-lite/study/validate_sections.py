"""Check the study's section outputs. Usage: python3 -I validate_sections.py [out]   (exit 1, one line per problem)

Reports: missing section files; personas with a blank, too-short or cut-off goal or pain, or a name outside 2-4 words;
a top persona called other/unclear/unknown/misc, or such a persona above 15% of the runs; personas.csv that does not
cover exactly the graded usable runs (or a persona with no personas.json entry); issues.csv missing a run that fell
short; fixes in impact.json without a fix report; statistics jargon in any text; wrong section titles; section prose
over the word budget (110 words outside tables, captions, figures and closed lists, 20% tolerance).
"""
import csv
import json
import os
import re
import sys
from collections import Counter
from html.parser import HTMLParser

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import sig  # noqa: E402
from build_study import SECTIONS  # noqa: E402

PROSE_BUDGET, TOLERANCE = 110, 1.2
MIN_GOAL_WORDS, MIN_PAIN_WORDS = 4, 3
OTHER_MAX_SHARE = 0.15
OTHER_WORDS = {"other", "others", "unclear", "unknown", "misc", "miscellaneous", "unclassified", "uncategorized",
               "uncategorised", "undefined", "unspecified", "n/a"}
# Words are assembled from pieces so this file itself stays free of them.
JARGON = re.compile(r"\b(?:%s)\b" % "|".join(
    ["ka" + "ppa", "co" + "hen", "au" + "c", "p-value", "chi-?squared?", "spearman", "pearson", "mann-whitney",
     "wilcoxon", r"confound\w*", r"calibrat\w*"]), re.I)
csv.field_size_limit(sys.maxsize)


class Page(HTMLParser):
    """Visible text of a fragment, the part that counts as prose, the first <h2 class="big"> and the ids used."""
    NOT_PROSE = {"svg", "table", "figcaption", "details"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.all, self.prose, self.ids, self.h2 = [], [], set(), None
        self._hidden, self._skip, self._in_h2, self._h2 = [], [], False, []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if a.get("id"):
            self.ids.add(a["id"])
        cls = (a.get("class") or "").split()
        if tag in ("style", "script"):
            self._hidden.append(tag)
        elif tag in self.NOT_PROSE or "kicker" in cls:
            self._skip.append(tag)
        if tag == "h2" and "big" in cls and self.h2 is None:
            self._in_h2, self._h2 = True, []

    def handle_endtag(self, tag):
        if self._hidden and self._hidden[-1] == tag:
            self._hidden.pop()
        elif self._skip and self._skip[-1] == tag:
            self._skip.pop()
        if tag == "h2" and self._in_h2:
            self._in_h2, self.h2 = False, " ".join("".join(self._h2).split())

    def handle_data(self, data):
        if self._hidden:
            return
        self.all.append(data)
        if self._in_h2:
            self._h2.append(data)
        if not self._skip:
            self.prose.append(data)


def words(text):
    return sum(1 for w in str(text).split() if re.search(r"\w", w))


def cut_off(text):
    t = str(text).strip()
    return "..." in t or "…" in t or t.count("(") != t.count(")") or t.count('"') % 2 or not re.search(r"[.!?][\"')\]]*$", t)


def is_other(name):
    return any(w in OTHER_WORDS for w in re.findall(r"[a-z/]+", str(name).lower()))


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def load_json(path, problems, label):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except OSError:
        return None
    except ValueError:
        problems.append("%s: not valid JSON" % label)
        return None


def csv_rows(path):
    try:
        with open(path, encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f))
    except OSError:
        return None


def check_personas(sec, R, problems):
    entries = load_json(os.path.join(sec, "personas.json"), problems, "sections/personas.json")
    if isinstance(entries, dict):
        entries = entries.get("personas")
    if entries is not None and not isinstance(entries, list):
        problems.append('sections/personas.json: expected a list of personas (or {"personas": [...]})')
    named = {}
    for i, e in enumerate(entries if isinstance(entries, list) else [], 1):
        if not isinstance(e, dict) or not str(e.get("name") or "").strip():
            problems.append("sections/personas.json: entry %d has no name" % i)
            continue
        name = str(e["name"]).strip()
        named[name.lower()] = e
        if not 2 <= words(name) <= 4:
            problems.append('sections/personas.json: persona "%s" needs a name of 2-4 words (has %d)' % (name, words(name)))
        for key, need in (("goal", MIN_GOAL_WORDS), ("pain", MIN_PAIN_WORDS)):
            v = str(e.get(key) or "").strip()
            if not v:
                problems.append('sections/personas.json: persona "%s" has a blank %s' % (name, key))
            elif words(v) < need:
                problems.append('sections/personas.json: persona "%s" has a too-short %s ("%s"); write it in full words' % (name, key, v))
            elif cut_off(v):
                problems.append('sections/personas.json: persona "%s" has a %s that looks cut off (ends "%s")' % (name, key, v[-30:]))
    rows = csv_rows(os.path.join(sec, "personas.csv"))
    if rows is None or R is None:
        return
    valid = {r["run_id"] for r in R}
    ids = [str(r.get("run_id") or "").strip() for r in rows]
    cnt = Counter(ids)
    missing = sorted(valid - set(ids))
    extra = sorted(set(ids) - valid)
    if missing:
        problems.append("sections/personas.csv: %d graded run(s) have no persona (e.g. %s)" % (len(missing), missing[0][:8]))
    if extra:
        problems.append("sections/personas.csv: %d run id(s) are not graded usable runs (e.g. %s)" % (len(extra), extra[0][:8]))
    if any(c > 1 for c in cnt.values()):
        problems.append("sections/personas.csv: %d run id(s) appear more than once" % sum(1 for c in cnt.values() if c > 1))
    use = [(str(r.get("run_id") or "").strip(), str(r.get("persona") or "").strip()) for r in rows]
    use = [(i, p) for i, p in use if i in valid]
    if any(not p for _, p in use):
        problems.append("sections/personas.csv: %d row(s) have a blank persona" % sum(1 for _, p in use if not p))
    by = Counter(p for _, p in use if p)
    total = len(valid) or 1
    if by and is_other(by.most_common(1)[0][0]):
        problems.append('sections/personas.csv: the biggest group is "%s"; the top persona must be a real user goal' % by.most_common(1)[0][0])
    for p, c in sorted(by.items()):
        if is_other(p) and c / total > OTHER_MAX_SHARE:
            problems.append('sections/personas.csv: "%s" covers %d%% of the runs (limit %d%%)' % (p, round(100.0 * c / total), round(100 * OTHER_MAX_SHARE)))
        if p.lower() not in named and isinstance(entries, list):
            problems.append('sections/personas.csv: persona "%s" has no entry in personas.json' % p)


def check_issues(sec, R, problems):
    rows = csv_rows(os.path.join(sec, "issues.csv"))
    if rows is None or R is None:
        return
    if rows and not {"run_id", "issue"} <= set(rows[0]):
        problems.append("sections/issues.csv: needs the columns run_id, issue, topic, theme")
        return
    have = {str(r.get("run_id") or "").strip() for r in rows if str(r.get("issue") or "").strip()}
    short = sorted(r["run_id"] for r in R if r["outcome"] != 1)
    miss = [i for i in short if i not in have]
    if miss:
        problems.append("sections/issues.csv: %d run(s) that fell short have no issue (e.g. %s)" % (len(miss), miss[0][:8]))
    valid = {r["run_id"] for r in R}
    extra = sorted(i for i in {str(r.get("run_id") or "").strip() for r in rows} if i not in valid)
    if extra:
        problems.append("sections/issues.csv: %d run id(s) are not graded usable runs (e.g. %s)" % (len(extra), extra[0][:8]))


def check_fixes(study, sec, problems):
    data = load_json(os.path.join(sec, "impact.json"), problems, "sections/impact.json")
    if data is None:
        return
    fixes = data.get("fixes") if isinstance(data, dict) else None
    if not isinstance(fixes, list) or not fixes:
        problems.append('sections/impact.json: needs a "fixes" list (id, title, runs, ...)')
        return
    for f in fixes:
        fid = str(f.get("id") or "").strip() if isinstance(f, dict) else ""
        if not fid or not os.path.isfile(os.path.join(study, "fix_reports", fid + ".html")):
            problems.append("fix_reports/%s.html: missing (every fix in impact.json has its own report)" % (fid or "?"))


def validate(out=None):
    """-> list of problem lines (empty when the sections are fine)."""
    out = sig.resolve_out(out)
    study = os.path.join(out, "study")
    sec = os.path.join(study, "sections")
    problems = []
    try:
        R = sig.load(out)
    except (OSError, ValueError) as e:
        R = None
        problems.append("cannot read the graded runs (%s)" % e)
    needs = {"affinity": ["issues.csv"], "empathy": ["personas.csv", "personas.json"]}
    present = []
    for key, title, _ in SECTIONS:
        files = ["%s.%s" % (key, x) for x in ("py", "html", "md", "json")] + needs.get(key, [])
        gone = [f for f in files if not os.path.isfile(os.path.join(sec, f))]
        if gone:
            problems.append("sections/%s: missing %s" % (key, ", ".join(gone)))
        if "%s.html" % key not in gone:
            present.append((key, title))
    if os.path.isfile(os.path.join(sec, "personas.json")) or os.path.isfile(os.path.join(sec, "personas.csv")):
        check_personas(sec, R, problems)
    check_issues(sec, R, problems)
    if os.path.isfile(os.path.join(sec, "impact.json")):
        check_fixes(study, sec, problems)
    for key, title in present:
        p = Page()
        p.feed(read(os.path.join(sec, key + ".html")))
        p.close()
        rel = "sections/%s.html" % key
        if p.h2 is None:
            problems.append('%s: no <h2 class="big"> title (expected "%s")' % (rel, title))
        elif p.h2.lower() != title.lower():
            problems.append('%s: the title reads "%s", expected "%s"' % (rel, p.h2, title))
        if key not in p.ids:
            problems.append('%s: no element with id="%s" (the contents links need it)' % (rel, key))
        n = words(" ".join(p.prose))
        if n > PROSE_BUDGET * TOLERANCE:
            problems.append("%s: %d words of prose outside tables, captions and figures (budget %d, limit %d)"
                            % (rel, n, PROSE_BUDGET, int(PROSE_BUDGET * TOLERANCE)))
    texts = []
    for key, _ in present:
        texts.append(("sections/%s.html" % key, " ".join(_text(os.path.join(sec, key + ".html")))))
        if os.path.isfile(os.path.join(sec, key + ".md")):
            texts.append(("sections/%s.md" % key, read(os.path.join(sec, key + ".md"))))
    pj = load_json(os.path.join(sec, "personas.json"), [], "")
    pj = pj.get("personas") if isinstance(pj, dict) else pj
    for e in pj if isinstance(pj, list) else []:
        if isinstance(e, dict):
            texts.append(("sections/personas.json", " ".join(str(e.get(k) or "") for k in ("name", "goal", "pain", "gain"))))
    fr = os.path.join(study, "fix_reports")
    for f in sorted(os.listdir(fr)) if os.path.isdir(fr) else []:
        if f.endswith(".html"):
            texts.append(("fix_reports/" + f, " ".join(_text(os.path.join(fr, f)))))
    for label, text in texts:
        m = JARGON.search(text)
        if m:
            problems.append('%s: statistics jargon ("%s"); say it in plain words' % (label, m.group(0)))
    return problems


def _text(path):
    p = Page()
    p.feed(read(path))
    p.close()
    return p.all


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    problems = validate(argv[0] if argv else None)
    for line in problems:
        print(line)
    if not problems:
        print("ok: all sections check out")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
