"""STARTER rules for the affinity section. The host agent is expected to READ the runs that fell short and then EDIT this file
(themes, wordings, patterns, issue labels) so it fits THIS dataset. Nothing here is the answer; it is a first draft that follows
the architecture: every run that fell short gets one primary theme, may get extra rows in other themes, and every issue label
belongs to exactly one theme.

A rule looks at one run `r` (a sig.load() dict), the lower-cased text of the grader's rationale and issue label, and the run's
failed tool calls, and says yes or no. Themes with no runs are dropped by affinity.py, so a theme the data does not show is
never forced onto the page.
"""
import re

T_DODGE = "Goal dodged entirely"
T_GAP = "Upsell or roadmap opportunity"
T_PILE = "Pile-up: many things went wrong at once"
T_WRONG = "Delivered, but wrong or incomplete"
T_PROOF = "Said it was done, but no proof"
T_STEER = "Made the user steer"
THEMES = [T_DODGE, T_GAP, T_PILE, T_WRONG, T_PROOF, T_STEER]
THEME_SHORT = {T_DODGE: "Goal dodged", T_GAP: "Roadmap opportunity", T_PILE: "Pile-up", T_WRONG: "Wrong or incomplete",
               T_PROOF: "Done, but no proof", T_STEER: "User had to steer"}


def rx(pattern):
    c = re.compile(pattern, re.I)
    return lambda s: bool(c.search(s or ""))


# Words in the grader's rationale or issue label that point at each theme. Edit per dataset.
DODGE_WORDS = rx(r"no (final )?answer|empty (final )?(reply|answer)|ended without|stall|refus|declin|gave up|giving up|did not (do|complete|perform|act|start)"
                 r"|only a (plan|promise)|promised|instead of (acting|doing|running|starting|searching|trying)|sent (the user )?to|redirect|hand(ed)? ?off|support desk|human agent|nothing (was )?(done|delivered)")
GAP_WORDS = rx(r"not supported|unsupported (feature|capability|request|action)|cannot (do|be done|support|offer|handle)|can.?t (do|support|offer|handle)|does not support|no way to|no such|not available|lacks?\b|missing (feature|capability|data)|not (yet )?offered|no tool for|out of scope|capability")
PROOF_WORDS = rx(r"no tool call|without (a|any) tool call|claimed|claims|says it|said it|not backed|unverif|ungrounded|unsupported claim|no (evidence|proof|source)|never (checked|verified)|not checked|asserted")
STEER_WORDS = rx(r"clarif|asked (which|what|whether|for|the user)|asks? (which|what|whether|for)|asking (which|what|for)|repeat|re-?ask|asked again|correct(ed|s|ion)|had to (say|ask|repeat)")
WRONG_WORDS = rx(r"wrong|incorrect|inaccurate|misreport|mismatch|contradict|does not match|partial|incomplete|thin|only (part|some|the top)|left out|missing|outdated|stale|not what")
FAIL_WORDS = rx(r"fail|error|timed? ?out|time-?out|cancel|unavailable|rejected|exception|limit")

# One generic issue label per theme, used when the grader gave no label or the grader's label already belongs to another theme.
RULE_LABEL = {
    T_DODGE: "Run ended without doing the job",
    T_GAP: "Asked for something the product does not offer",
    T_PILE: "Several calls failed or dragged on one run",
    T_WRONG: "Answer delivered but wrong or partial",
    T_PROOF: "Reply claimed more than the calls show",
    T_STEER: "User had to repeat or correct",
}
RULE_TOPIC = {
    T_DODGE: "Nothing delivered",
    T_GAP: "Capability asked for, not offered",
    T_PILE: "Failures piled up",
    T_WRONG: "Wrong or partial answer",
    T_PROOF: "Claim without a call",
    T_STEER: "User steered the agent",
}
STOP = set("a an the and or of to in on for with is are was were be been it its this that these those my your our i you we me us do does did can could would should will "
           "please just about from at as by if so how what which who where when why there their them they have has had not no yes".split())


def text_of(r):
    return ("%s %s" % (r.get("rationale") or "", r.get("issue_raw") or "")).lower()


def failed_calls(dossier):
    return [c for c in (dossier or {}).get("tool_calls") or [] if c.get("success") is False or c.get("error")]


def mechanical_pile(r):
    """Several things at once on one run: three or more failed calls, or any failed call on a slow or call-heavy run."""
    return r["n_fail"] >= 3 or (r["n_fail"] >= 1 and (r["slow"] or r["heavy"]))


def primary_theme(r):
    """First matching theme wins. Order: dodge, gap, pile-up, no proof, steer, wrong (the default)."""
    t = text_of(r)
    if r["no_answer"] or DODGE_WORDS(t):
        return T_DODGE
    if GAP_WORDS(t):
        return T_GAP
    if mechanical_pile(r) or re.search(r"piled up|pile-up|retries|retried|several (failed|errors)", t):
        return T_PILE
    if PROOF_WORDS(t) and r["n_calls"] == 0:
        return T_PROOF
    if STEER_WORDS(t):
        return T_STEER
    return T_WRONG


def extra_themes(r, primary):
    """Other themes this run also belongs to (it then gets one more row in issues.csv per theme)."""
    t = text_of(r)
    out = []
    if primary != T_PILE and mechanical_pile(r):
        out.append(T_PILE)
    if primary != T_DODGE and (r["no_answer"] or (r["unrecovered"] and FAIL_WORDS(t) and DODGE_WORDS(t))):
        out.append(T_DODGE)
    if primary != T_GAP and GAP_WORDS(t) and GAP_WORDS(r.get("final_answer") or ""):
        out.append(T_GAP)
    return out


def ask_phrase(r, n=6):
    """A short, plain phrase for what was asked (first content words of the first ask), for 'Asked for: ...' labels."""
    ws = [w for w in re.findall(r"[a-z][a-z'-]+", (r.get("first_ask") or "").lower()) if w not in STOP]
    ws = ws[:n]
    while ws and ws[-1] in ("under", "over", "below", "above", "than", "between", "after", "before", "within", "around", "near", "each", "every", "instead", "based"):
        ws.pop()
    return " ".join(ws)
