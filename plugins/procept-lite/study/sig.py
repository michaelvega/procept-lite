"""Shared run table for the study sections: ONE definition of the signals, so every section agrees.

    import os, sys
    STUDY = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))    # <out>/study (sections/ lives inside it)
    sys.path.insert(0, STUDY)                                              # python3 -I does not add the script folder
    import sig
    R = sig.load()                                                          # list of run dicts, sorted by first_ts

load(out=None) returns one dict per run that is GRADED (a +1/0/-1 outcome in <out>/scores.csv) and USABLE (not listed in
<out>/normalized/unusable.csv or in the ingest sign-off's unusable list, and with a non-blank ask). Ungraded and unusable
runs never appear in any section, count or table. `out` resolves from the argument, then the PLITE_OUT environment
variable, then the parent of the folder that holds this file (the study lives in <out>/study).

Sources: <out>/cig/runs.csv (flags and totals computed by the CIG), <out>/scores.csv (outcome and the grader's words),
<out>/cig/run_dossiers.jsonl (asks, answers, tool names), <out>/normalized/unusable.csv.

Keys of a run dict:
  run_id  conversation_id  agent ("" when none)
  env        environment, grouped generically: the 4 busiest values (lower-cased), every other value "other";
             "default" when the export carries no environment
  day        "YYYY-MM-DD" of first_ts          first_ts  ISO 8601 UTC string
  lat        seconds, end to end (float) or None when the export has no timing
  tokens     input + output tokens (int, 0 if unknown)          cost_usd  float or None when the export has no prices
  n_calls    tool calls          n_fail  failed tool calls (cancelled calls are not failures)
  tool_fail  any call failed     unrecovered  a failure that mattered (the last call failed, or no answer)
  no_answer  the run ended without an answer
  slow       slowest decile of latency among the CIG's runs      heavy  top-decile tool-call count, at least 8
             (tool_fail unrecovered no_answer slow heavy are bool)
  outcome    +1 / 0 / -1 (int)      label  good | ambiguous | bad      conf  0..1 (0.0 when the grader gave none)
  rationale  the grader's one-sentence reason          key_quote  a verbatim quote the grader chose
  persona_raw, issue_raw   the grader's own persona and issue labels: raw and messy, a lead and not the final answer
  reaction_run_id          the run the grader read as the user's reaction ("" if none)
  next_ask   the user's next message in the same conversation (first ask of the next usable run) or ""
  workflow   tool names in order of first use, de-duplicated, joined by " > "      tools  every call's tool name, in order
  first_ask  the user's message that started the run          final_answer  the reply ("" if the run ended without one)

Helpers: median(xs) (ignores None), pct(part, whole), thresholds(R), mask(text), short(text, n).
Run `python3 -I sig.py [out]` for a one-screen summary.
"""
import csv
import json
import os
import re
import sys
from collections import Counter, defaultdict

try:
    csv.field_size_limit(sys.maxsize)
except OverflowError:  # pragma: no cover (Windows)
    csv.field_size_limit(2 ** 31 - 1)

HERE = os.path.dirname(os.path.abspath(__file__))
LABEL = {1: "good", 0: "ambiguous", -1: "bad"}


def resolve_out(out=None):
    """The output folder: argument, else PLITE_OUT, else the parent of the study folder."""
    return os.path.abspath(out or os.environ.get("PLITE_OUT") or os.path.dirname(HERE))


# ---------------------------------------------------------------- readers
def _missing(path, hint):
    return FileNotFoundError("%s not found (%s)" % (path, hint))


def _rows(path, hint):
    try:
        with open(path, encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f))
    except FileNotFoundError:
        raise _missing(path, hint)


def _num(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x and x not in (float("inf"), float("-inf")) else None


def _int(v, default=0):
    x = _num(v)
    return default if x is None else int(x)


def _unusable(out):
    """{run_id: reason}: the ledger plus the ingest sign-off's list (same rule the other stages use)."""
    res = {}
    try:
        with open(os.path.join(out, "normalized", "unusable.csv"), encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                if r.get("run_id"):
                    res[r["run_id"]] = r.get("reason") or "unusable"
    except OSError:
        pass
    try:
        with open(os.path.join(out, "checks", "ingest.json"), encoding="utf-8") as f:
            for u in json.load(f).get("unusable") or []:
                if isinstance(u, dict) and u.get("run_id"):
                    res.setdefault(u["run_id"], u.get("reason") or "unusable")
    except (OSError, ValueError, AttributeError):
        pass
    return res


def _scores(out):
    """{run_id: row} for rows with a valid outcome; a later row for the same run wins."""
    res = {}
    for r in _rows(os.path.join(out, "scores.csv"), "grade and merge first: scores.csv holds the graded runs"):
        x = _num(r.get("outcome_score"))
        if r.get("run_id") and x is not None and int(round(x)) in LABEL:
            r["_outcome"] = int(round(x))
            res[r["run_id"].strip()] = r
    return res


def _clean(s):
    return " ".join(str(s or "").split())


def _group_env(raw):
    names = [_clean(e).lower() or "default" for e in raw]
    top = [k for k, _ in sorted(Counter(names).items(), key=lambda kv: (-kv[1], kv[0]))[:4]]
    return [n if n in top else "other" for n in names]


# ---------------------------------------------------------------- the loader
def load(out=None):
    """-> list of run dicts (graded and usable runs only), sorted by (first_ts, run_id). See the module docstring."""
    out = resolve_out(out)
    scores = _scores(out)
    runs_csv = {r["run_id"]: r for r in _rows(os.path.join(out, "cig", "runs.csv"), "build the CIG first")}
    unusable = _unusable(out)
    chain, info = defaultdict(list), {}
    dpath = os.path.join(out, "cig", "run_dossiers.jsonl")
    try:
        fh = open(dpath, encoding="utf-8")
    except FileNotFoundError:
        raise _missing(dpath, "build the CIG first")
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            rid = str(d.get("run_id") or "")
            ask = str(d.get("user_message") or "")
            if not rid or rid in unusable or not ask.strip():
                continue
            conv = str(d.get("conversation_id") or rid)
            chain[conv].append((str(d.get("first_ts") or ""), rid, ask))
            if rid in scores and rid in runs_csv:
                tcs = [t for t in (d.get("tool_calls") or []) if isinstance(t, dict)]
                info[rid] = dict(conv=conv, agent=str(d.get("agent") or ""), ask=ask, answer=str(d.get("final_answer") or ""),
                                 tools=[str(t.get("name") or "unknown") for t in tcs], env=d.get("environment"))
    next_ask = {}
    for items in chain.values():
        items.sort()
        for a, b in zip(items, items[1:]):
            next_ask[a[1]] = b[2]
    R = []
    for rid, i in info.items():
        sc, ru = scores[rid], runs_csv[rid]
        seen, wf = set(), []
        for t in i["tools"]:
            if t not in seen:
                seen.add(t)
                wf.append(t)
        ts = ru.get("first_ts") or ""
        tokens = _int(ru.get("tokens")) or _int(ru.get("input_tokens")) + _int(ru.get("output_tokens"))
        o = sc["_outcome"]
        R.append(dict(
            run_id=rid, conversation_id=i["conv"], agent=i["agent"] or _clean(ru.get("agent")),
            env=i["env"] if i["env"] is not None else ru.get("environment"), day=ts[:10] if re.match(r"\d{4}-\d\d-\d\d", ts) else "",
            first_ts=ts, lat=_num(ru.get("latency_s")), tokens=tokens, cost_usd=_num(ru.get("cost_usd")),
            n_calls=_int(ru.get("n_tool_calls"), len(i["tools"])), n_fail=_int(ru.get("n_tool_failures")),
            tool_fail=bool(_int(ru.get("tool_fail"))), unrecovered=bool(_int(ru.get("unrecovered"))),
            no_answer=bool(_int(ru.get("no_answer"))), slow=bool(_int(ru.get("slow"))), heavy=bool(_int(ru.get("heavy"))),
            outcome=o, label=LABEL[o], conf=_num(sc.get("confidence")) or 0.0, rationale=_clean(sc.get("rationale")),
            key_quote=_clean(sc.get("key_quote")), persona_raw=_clean(sc.get("persona")), issue_raw=_clean(sc.get("issue")),
            reaction_run_id=_clean(sc.get("reaction_run_id")), next_ask=next_ask.get(rid, ""), workflow=" > ".join(wf),
            tools=i["tools"], first_ask=i["ask"], final_answer=i["answer"]))
    for r, e in zip(R, _group_env([r["env"] for r in R])):
        r["env"] = e
    R.sort(key=lambda r: (r["first_ts"], r["run_id"]))
    return R


# ---------------------------------------------------------------- small helpers shared by the sections
def median(xs):
    """Median of the numbers in xs, ignoring None; None when there are none."""
    v = sorted(x for x in xs if x is not None)
    if not v:
        return None
    m = len(v) // 2
    return v[m] if len(v) % 2 else (v[m - 1] + v[m]) / 2.0


def pct(part, whole):
    """Whole-number percentage, 0 when whole is 0."""
    return round(100.0 * part / whole) if whole else 0


def thresholds(R):
    """{'slow_s': shortest latency flagged slow, 'heavy_calls': fewest tool calls flagged heavy} (None when none)."""
    s = [r["lat"] for r in R if r["slow"] and r["lat"] is not None]
    h = [r["n_calls"] for r in R if r["heavy"]]
    return {"slow_s": round(min(s), 1) if s else None, "heavy_calls": min(h) if h else None}


_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_SECRET = re.compile(
    r"(?<![A-Za-z0-9])(?:sk|pk|rk|ghp|gho|xox[bpa])[-_][A-Za-z0-9_\-]{16,}|\bBearer\s+[A-Za-z0-9._~+/\-]{6,}=*"
    r"|\bAKIA[0-9A-Z]{16}\b|\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]*|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|(?i:\b(?:password|passwd|pwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token|token)\b[\"']?\s*[:=]\s*[\"']?)"
    r"[^\s\"',;}&]{4,}")
_PHONE = re.compile(r"(?<![\w.])(?:\+?\d{1,3}[\s.-]?)?(?:\(\d{3}\)|\d{3})[\s.-]?\d{3}[\s.-]?\d{4}(?!\w)")
_LONGNUM = re.compile(r"\b\d{9,}\b")


def mask(text):
    """Hide e-mail addresses, secrets, phone numbers and long digit strings. Use it for anything shown from a run."""
    t = _SECRET.sub("[secret]", _EMAIL.sub("[email]", str(text or "")))
    return _LONGNUM.sub("[number]", _PHONE.sub("[phone]", t))


def short(text, n=140):
    """mask(), whitespace collapsed, cut at a word boundary to at most n characters (adds an ellipsis when cut)."""
    t = _clean(mask(text))
    if len(t) <= n:
        return t
    cut = t[:n - 1].rsplit(" ", 1)[0] or t[:n - 1]
    return cut.rstrip(" ,;:-") + "\u2026"


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        R = load(argv[0] if argv else None)
    except (OSError, ValueError) as e:
        print("sig: %s" % e, file=sys.stderr)
        return 2
    c = Counter(r["label"] for r in R)
    print("%d runs, %s" % (len(R), dict(c)))
    print("days %s .. %s" % (R[0]["day"], R[-1]["day"]) if R else "no runs")
    print("thresholds", thresholds(R))
    for k in ("tool_fail", "unrecovered", "no_answer", "slow", "heavy"):
        print("%-12s %d" % (k, sum(r[k] for r in R)))
    print("env", dict(Counter(r["env"] for r in R)))
    print("median latency", median([r["lat"] for r in R]), "median tokens", median([r["tokens"] for r in R]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
