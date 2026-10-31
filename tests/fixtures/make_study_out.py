"""Deterministic synthetic OUT directory for the study packages (stdlib only; no network; no plugin imports).

    import sys; sys.path.insert(0, "<repo>/tests/fixtures")
    from make_study_out import make_out
    meta = make_out(path, n=160)        # same arguments -> byte-identical files

An online outdoor-gear shop with a support assistant. FIVE user-goal groups, each asked in several wordings:
  shop     Find a product to buy               search_catalog > get_product > check_stock
  order    Track or change an order            lookup_order > track_shipment | update_order
  returns  Return an item or get a refund      lookup_order > get_policy > create_return
  deals    Check stock, codes and prices       check_stock > get_product > apply_promo
  fit      Check size, fit or compatibility    get_product > get_size_guide > search_docs
Conversations hold 1-3 runs. A later ask is a correction (after a poor run), a thank-you or a new ask, so the
user's next-message reaction exists. Outcomes are mixed; tool failures, retries, runs with no answer, a pile-up
tail, dead-end refusals and wrong answers all occur. Graders' persona labels are slightly messy on purpose.

Files written (formats follow CONTRACT.md; everything below <path>):
  normalized/runs.jsonl            every run, including the unusable and the ungraded ones
  normalized/unusable.csv          run_id,reason,source  (blank-ask runs: auto; one run the ingest check flagged: model)
  scores.csv                       graded usable runs only
  cig/runs.csv, cig/run_dossiers.jsonl   usable runs (graded and ungraded), as build_cig writes them
  cig/nodes.csv, cig/edges.csv, cig/errors.csv

make_out(path, n=160, seed=7, n_unusable=5, n_ungraded=6, with_cost=True) -> meta dict
  out          absolute path
  n            number of graded usable runs (len(sig.load(path)) == n)
  run_ids      graded usable run ids in time order
  groups       {run_id: group key} ground truth for graded usable runs
  group_names  {group key: user-goal name}
  unusable     run ids listed in normalized/unusable.csv
  ungraded     usable run ids that have no row in scores.csv
  conversations {conversation_id: [run_id, ...]} time order, usable and unusable runs
The signature and the meta keys are a stable API (other packages' tests rely on them).
"""
import csv
import json
import math
import os
import random
import re
import uuid
from datetime import datetime, timedelta, timezone
from collections import Counter, defaultdict

T0 = datetime(2026, 10, 12, 7, 0, 0, tzinfo=timezone.utc)
SCORE_COLS = ["run_id", "outcome_score", "confidence", "rationale", "key_quote", "persona", "issue", "reaction_run_id"]
RUN_COLS = ["run_id", "conversation_id", "agent", "environment", "first_ts", "latency_s", "input_tokens",
            "output_tokens", "tokens", "cost_usd", "n_tool_calls", "n_tool_failures", "tool_fail", "unrecovered",
            "no_answer", "slow", "heavy", "outcome_score", "label", "persona", "workflow"]
NODE_COLS = ["node_id", "kind", "calls", "runs", "fail_calls", "fail_rate", "median_duration_s", "p90_duration_s",
             "mean_run_tokens", "share_bad_runs", "share_good_runs", "mean_outcome"]
EDGE_COLS = ["source", "target", "count", "runs", "fail_rate_target", "mean_outcome"]
ERR_COLS = ["error_class", "count", "runs", "tool", "example"]
LABELS = {1: "good", 0: "ambiguous", -1: "bad"}

ITEMS = ["waterproof jacket", "trail runner", "two-person tent", "down sleeping bag", "hiking boot",
         "camp stove", "steel bottle", "trekking pole set", "headlamp", "daypack", "fleece pullover", "rain shell"]
BRANDS = ["Alder", "Ridge", "Summit", "Cedar", "Granite", "Juniper", "Harbor", "Basalt"]
SIZES = ["S", "M", "L", "XL", "9", "10", "11"]
COLORS = ["green", "black", "navy", "orange"]
CODES = ["WELCOME10", "SPRING15", "TRAIL20"]
STATUSES = ["shipped", "in transit", "out for delivery"]
CARRIERS = ["Northline", "Parcelway", "SwiftPost"]
HEIGHTS = ["5'6\"", "5'10\"", "6'1\"", "5'2\""]
WEIGHTS = ["140 lb", "165 lb", "185 lb", "210 lb"]
MONTHS = ["March", "June", "October", "January"]

# tool -> (args, result, typical duration in s)
TOOLS = {
    "search_catalog": (lambda s: {"query": s["item"], "max_price": s["price"]},
                       lambda s: {"results": [{"sku": s["sku%d" % i], "name": s["p%d" % i], "price": s["price%d" % i]}
                                              for i in (1, 2, 3)]}, 0.9),
    "get_product": (lambda s: {"sku": s["sku1"]},
                    lambda s: {"sku": s["sku1"], "name": s["p1"], "price": s["price1"], "sizes": ["S", "M", "L", "XL"],
                               "rating": 4.6}, 0.5),
    "check_stock": (lambda s: {"sku": s["sku1"], "size": s["size"]},
                    lambda s: {"sku": s["sku1"], "size": s["size"], "in_stock": True, "units": s["units"]}, 0.6),
    "get_size_guide": (lambda s: {"sku": s["sku1"], "height": s["height"], "weight": s["weight"]},
                       lambda s: {"recommended_size": s["size"], "fit": "true to size"}, 0.5),
    "search_docs": (lambda s: {"query": "%s with %s" % (s["item"], s["item2"])},
                    lambda s: {"hits": [{"title": "Compatibility notes", "text": "The %s is compatible with the %s."
                                         % (s["item"], s["item2"])}]}, 1.1),
    "lookup_order": (lambda s: {"order_id": str(s["order"])},
                     lambda s: {"order_id": str(s["order"]), "status": s["status"], "carrier": s["carrier"],
                                "items": [s["item"]], "total": s["price1"]}, 0.7),
    "track_shipment": (lambda s: {"order_id": str(s["order"]), "carrier": s["carrier"]},
                       lambda s: {"status": s["status"], "eta": s["eta"], "carrier": s["carrier"]}, 2.5),
    "update_order": (lambda s: {"order_id": str(s["order"]), "action": "cancel"},
                     lambda s: {"order_id": str(s["order"]), "cancelled": True, "refund_usd": s["price1"]}, 1.2),
    "get_policy": (lambda s: {"topic": "returns"},
                   lambda s: {"return_window_days": s["window"], "refund_days": s["refund_days"], "label": "prepaid"}, 0.4),
    "create_return": (lambda s: {"order_id": str(s["order"]), "item": s["item"], "reason": "does not fit"},
                      lambda s: {"rma": s["rma"], "label_emailed": True}, 1.8),
    "apply_promo": (lambda s: {"code": s["code"]},
                    lambda s: {"code": s["code"], "valid": True, "percent_off": 10}, 0.6),
}
# tool -> (error text, duration of the failed call)
ERRORS = {
    "search_catalog": ("catalog search timed out after 30 s", 30.0),
    "check_stock": ("stock service timeout after 30 s", 30.0),
    "track_shipment": ("carrier API timeout after 30 s", 30.0),
    "lookup_order": ("order service returned HTTP 500", 2.0),
    "create_return": ("return rejected: validation error 422 for order {order}", 1.1),
    "get_size_guide": ("size guide not found for product {sku1}", 0.8),
    "get_product": ("product service returned HTTP 503", 2.0),
    "search_docs": ("docs search unavailable (503)", 2.0),
    "update_order": ("order {order} already shipped; it can no longer be changed or cancelled", 0.9),
    "apply_promo": ("promo service returned HTTP 500", 2.0),
    "get_policy": ("policy service returned HTTP 503", 2.0),
}

GROUPS = {
    "shop": dict(
        name="Find a product to buy", agent="shop-assistant", labels=["gear shopper", "product finder"],
        gist="product options within their budget and size", gap="stock in the requested size was never checked",
        contra="stock is zero but the reply says in stock", thing="",
        flow={None: ["search_catalog", "get_product", "check_stock"]}, fail_tool="search_catalog", fail_k=2,
        unsup=[],
        wrong=("check_stock", lambda s: {"sku": s["sku1"], "size": s["size"], "in_stock": False, "units": 0}),
        asks=[("I need a {item} under ${price}", None), ("show me {item} options in size {size}", None),
              ("can you recommend a good {item} for a week-long trip in the mountains?", None),
              ("looking for a {item} for my brother, budget around ${price}", None),
              ("what's your lightest {item}?", None), ("do you have a {item} in {color}?", None)],
        fix=["no, I said size {size} and under ${price}"],
        kinds=dict(ok=52, recovered=8, clarify=10, partial=8, wrong=8, fail_answer=3, fail_end=4, dodge=3, pileup=4),
        answers=dict(
            ok="I found three options for a {item}: the {p1} at ${price1}, the {p2} at ${price2} and the {p3} at ${price3}. "
               "The {p1} is in stock in size {size} ({units} units left).",
            partial="Here are two {item} options that fit: the {p1} at ${price1} and the {p2} at ${price2}.",
            wrong="The {p1} is in stock in size {size} for ${price1}, so you can order it today.",
            clarify="Happy to help. What will you use it for, and what is your budget?",
            fail_answer="Sorry, I couldn't search the catalog right now. Please try again in a few minutes.",
            dodge="I can help with orders and returns. For product ideas, please browse the website."),
        issues=dict(clarify="asked for details already given", partial="stock in size never checked",
                    wrong="claimed in stock, none left", fail_end="catalog search timed out",
                    fail_answer="catalog search failed", dodge="redirected instead of searching",
                    pileup="catalog errors piled up")),
    "order": dict(
        name="Track or change an order", agent="support-assistant", labels=["order tracker", "order status checker"],
        gist="the status of their order", gap="tracking was never checked",
        contra="tracking says in transit but the reply says delivered", thing="changing the delivery address after shipping",
        flow={None: ["lookup_order", "track_shipment"], "cancel": ["lookup_order", "update_order"]},
        fail_tool="track_shipment", fail_k=3, unsup=[("lookup_order", True), ("update_order", False)],
        wrong=("track_shipment", lambda s: {"status": "in transit", "eta": s["eta"], "carrier": s["carrier"]}),
        asks=[("where is my order {order}?", None), ("has order #{order} shipped yet", None),
              ("tracking says delivered but nothing arrived, order {order}", None),
              ("when will my {item} get here? I ordered it {days} days ago", None),
              ("please cancel order {order}", "cancel"),
              ("I need to change the delivery address on order {order}", "unsupported"),
              ("can you reroute order {order} to my office instead?", "unsupported")],
        fix=["still no update on order {order}, please check again"],
        kinds=dict(ok=36, recovered=10, clarify=6, wrong=4, fail_end=14, fail_answer=10, unsupported=7, alt=3, dodge=4,
                   pileup=6),
        answers={"ok": "Order {order} is {status} with {carrier} and should arrive by {eta}.",
                 "ok:cancel": "Order {order} is cancelled and ${price1} goes back to your card within {refund_days} business days.",
                 "wrong": "Order {order} was delivered yesterday, so it should be at your door.",
                 "clarify": "Sure. Could you share the order number so I can look it up?",
                 "fail_answer": "I couldn't reach the carrier right now. Please try again later.",
                 "dodge": "Order questions are handled by our support desk. Please contact them.",
                 "unsupported": "I can't change the address on order {order} because it has already shipped.",
                 "alt": "I can't change the address on order {order} because it has shipped, but you can redirect it "
                        "with {carrier} using the link in your confirmation email.",
                 "partial": "Order {order} exists and is {status}."},
        issues=dict(clarify="asked for order number", wrong="wrong delivery status", fail_end="tracking lookup timed out",
                    fail_answer="tracking unavailable, generic apology", unsupported="address change after shipping",
                    dodge="sent to support desk", pileup="tracking errors piled up", partial="tracking never checked")),
    "returns": dict(
        name="Return an item or get a refund", agent="support-assistant", labels=["returns requester", "refund seeker"],
        gist="a started return or refund", gap="the return was never started",
        contra="the policy says 30 days but the reply says 60", thing="exchanging an item for another size",
        flow={None: ["lookup_order", "get_policy", "create_return"]}, fail_tool="create_return", fail_k=2,
        unsup=[("lookup_order", True)],
        wrong=("get_policy", lambda s: {"return_window_days": 30, "refund_days": s["refund_days"], "label": "prepaid"}),
        asks=[("I want to return the {item} I bought, it doesn't fit", None),
              ("How do I get a refund on order {order}? the {item} arrived damaged", None),
              ("start a return for order {order}", None),
              ("my {item} arrived defective, what are my options", None),
              ("can I exchange my {item} for a different size?", "unsupported"),
              ("swap my {item} for the {item2} instead please", "unsupported")],
        fix=["that didn't start the return for order {order}"],
        kinds=dict(ok=30, recovered=8, clarify=4, partial=10, wrong=4, fail_end=4, fail_answer=14, unsupported=10, alt=6,
                   dodge=8, pileup=2),
        answers=dict(
            ok="I started return {rma} for your {item}. A prepaid label is on its way to your email, and the refund "
               "posts within {refund_days} business days of arrival.",
            partial="Returns are accepted within {window} days of delivery, and your {item} is eligible.",
            wrong="You have 60 days to return it, so there is plenty of time.",
            clarify="Sure. Which item from the order would you like to return, and why?",
            fail_answer="I couldn't start the return just now. Please contact support with your order number.",
            dodge="Returns are handled by our support team. Please email them.",
            unsupported="I can only process refunds, not exchanges. Please contact support to swap sizes.",
            alt="I can't do exchanges directly, but I can start a return for a full refund and you can reorder the "
                "size you need."),
        issues=dict(clarify="asked which item to return", partial="return never started", wrong="wrong return window",
                    fail_end="order lookup failed", fail_answer="return could not be created",
                    unsupported="exchange not supported", dodge="sent to support desk", pileup="return errors piled up")),
    "deals": dict(
        name="Check stock, codes and prices", agent="shop-assistant", labels=["deal hunter", "stock and price checker"],
        gist="stock and price information", gap="stock and code validity were not checked",
        contra="the code is expired but the reply says it works", thing="price matching or holding an item",
        flow={None: ["check_stock", "get_product", "apply_promo"]}, fail_tool="check_stock", fail_k=2, unsup=[],
        wrong=("apply_promo", lambda s: {"code": s["code"], "valid": False, "reason": "expired"}),
        asks=[("is the {item} back in stock?", None), ("any discount codes for first-time buyers?", None),
              ("when will the {item} be restocked in size {size}?", None), ("apply code {code} to my cart", None),
              ("which is cheaper, the {item} or the {item2}?", None),
              ("do you price match other stores?", "unsupported"),
              ("can you hold the {item} for me until Friday?", "unsupported")],
        fix=["I asked about the {item} in size {size}, is it in stock or not?"],
        kinds=dict(ok=34, recovered=6, clarify=6, partial=10, wrong=10, fail_end=6, fail_answer=3, unsupported=14, alt=7,
                   pileup=4),
        answers=dict(
            ok="The {p1} is in stock in size {size} ({units} left) at ${price1}. Code {code} is valid and takes 10% off, "
               "so ${total} at checkout.",
            partial="The {p1} costs ${price1}.",
            wrong="Yes, code {code} works and takes 10% off your order.",
            clarify="Which size and color are you after?",
            fail_answer="I couldn't check stock right now. Please try again later.",
            unsupported="We don't offer price matching or holds.",
            alt="We don't price match, but the {p1} is ${price1} today and code {code} takes 10% off."),
        issues=dict(clarify="asked size and color", partial="stock and code not checked", wrong="claimed expired code works",
                    fail_end="stock check timed out", fail_answer="stock check failed",
                    unsupported="price match not supported", pileup="stock errors piled up")),
    "fit": dict(
        name="Check size, fit or compatibility", agent="shop-assistant", labels=["fit and sizing asker", "compatibility checker"],
        gist="a size or compatibility answer", gap="the user's own measurements were not used",
        contra="the docs say not compatible but the reply says yes", thing="",
        flow={None: ["get_product", "get_size_guide", "search_docs"]}, fail_tool="get_size_guide", fail_k=2, unsup=[],
        wrong=("search_docs", lambda s: {"hits": [{"title": "Compatibility notes",
                                                  "text": "Not compatible: the %s does not fit the %s." % (s["item"], s["item2"])}]}),
        asks=[("what size {item} should I get if I'm {height} and {weight}?", None),
              ("is the {item} true to size or does it run small?", None),
              ("I'm between sizes in the {item}, should I size up?", None),
              ("my feet are wide, will the {item} be too tight?", None),
              ("will the {item} work with the {item2}?", None)],
        fix=["that doesn't tell me which size to order"],
        kinds=dict(ok=30, recovered=5, clarify=12, partial=10, wrong=14, fail_end=7, dodge=16, pileup=6),
        answers=dict(
            ok="The size guide points to {size} for {height} and {weight}. The {p1} runs true to size and fits "
               "comfortably alongside the {item2}.",
            partial="The size guide lists S to XL, but I can't say which one fits you.",
            wrong="Yes, the {item} works with the {item2}, no problem.",
            clarify="Could you tell me your height and weight first?",
            dodge="I don't have sizing information for that product."),
        issues=dict(clarify="asked for measurements", partial="no size recommendation", wrong="wrong compatibility claim",
                    fail_end="size guide unavailable", dodge="claimed no sizing data", pileup="size guide errors piled up")),
}
GROUP_WEIGHTS = [("shop", 30), ("order", 24), ("returns", 18), ("deals", 16), ("fit", 12)]
FIX_ANY = ["no, that's not what I asked", "that didn't answer my question, can you try again?",
           "you didn't actually answer. can I talk to a person?"]
THANKS = ["thanks!", "perfect, thank you", "great, thanks a lot", "ok thanks"]
OUT_P = {"ok": ((1, 94), (0, 6)), "recovered": ((1, 85), (0, 15)), "ack": ((1, 100),), "clarify": ((0, 85), (-1, 15)),
         "partial": ((0, 80), (-1, 20)), "wrong": ((-1, 65), (0, 35)), "fail_end": ((-1, 100),),
         "fail_answer": ((-1, 60), (0, 40)), "unsupported": ((-1, 55), (0, 45)), "alt": ((1, 55), (0, 45)),
         "dodge": ((-1, 85), (0, 15)), "pileup": ((-1, 70), (0, 30))}
FALLBACK_ISSUE = {"ok": "answer thin on detail", "recovered": "needed retries, slow", "alt": "alternative only partly useful",
                  "clarify": "asked for details already given", "partial": "only part of the ask answered",
                  "wrong": "claim not backed by tool result", "dodge": "redirected instead of helping",
                  "fail_answer": "tool failed, generic apology", "unsupported": "capability not available"}
RAT = {
    ("ok", 1): ["The answer matches the tool results and gives the user {gist}.",
                "Tool results back every claim, and the reply covers {gist}.",
                "Correct and complete: the figures in the reply come straight from the {tool} result."],
    ("ok", 0): ["Correct but thin: {gist} is only partly covered.", "Plausible answer, but little of it can be verified against the tool results."],
    ("recovered", 1): ["The first {tool} call failed, the agent retried, and the final answer is correct.",
                       "A transient {tool} error was retried successfully and the user got {gist}."],
    ("recovered", 0): ["It recovered from a {tool} error, but the answer is slow and only partly covers {gist}."],
    ("ack", 1): ["A short thank-you; the agent replied politely and nothing more was asked of it."],
    ("clarify", 0): ["The agent asked a question that delays {gist}; reasonable, but no progress yet.",
                     "A clarifying question instead of acting; {gist} is still outstanding."],
    ("clarify", -1): ["Unnecessary question: the ask already had what was needed, and the user got nothing."],
    ("partial", 0): ["Only part of the ask was handled: {gap}.", "The reply stops short of {gist}: {gap}."],
    ("partial", -1): ["The reply skipped the main step: {gap}."],
    ("wrong", -1): ["The answer contradicts the {tool} result: {contra}.",
                    "A claim in the reply is not supported by the {tool} output ({contra})."],
    ("wrong", 0): ["Mostly right, but one claim is off: {contra}."],
    ("fail_end", -1): ["{tool} failed {k} times and the run ended without any answer.",
                       "No answer: every {tool} attempt errored and the agent stopped without telling the user."],
    ("fail_answer", -1): ["{tool} failed and the reply gave the user nothing usable toward {gist}.",
                          "The tool error became a generic apology; {gist} was not delivered."],
    ("fail_answer", 0): ["The reply admits the failure honestly, but {gist} was not delivered."],
    ("unsupported", -1): ["The user asked for something the assistant cannot do ({thing}) and got a flat refusal with no next step.",
                          "Dead end: {thing} is not supported and no alternative was offered."],
    ("unsupported", 0): ["The assistant could not do what was asked ({thing}); it said so clearly but offered little else."],
    ("alt", 1): ["The ask ({thing}) is not supported, but the reply offered a workable alternative."],
    ("alt", 0): ["The ask ({thing}) is not supported; the alternative offered is only partly useful."],
    ("dodge", -1): ["The agent deflected to support although its tools could have handled the ask.",
                    "Goal dodged: a canned redirect instead of {gist}."],
    ("dodge", 0): ["A redirect rather than help, though the user can find the information elsewhere."],
    ("pileup", -1): ["{k} failed calls and retries in a single run; the reply was partial and slow.",
                     "Repeated {tool} errors piled up; the user waited {lat} s for a partial answer."],
    ("pileup", 0): ["Many retries before a partial answer; {gist} arrived late and incomplete."],
}


def _pctl(vals, q):
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    pos = (len(vals) - 1) * q
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(vals) - 1)
    return vals[lo] + (vals[hi] - vals[lo]) * (pos - lo)


def _mean(vals):
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


def _cell(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, float):
        v = round(v, 4)
        return int(v) if v == int(v) and abs(v) < 1e15 else v
    return v


def _write_csv(path, cols, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(cols)
        for r in rows:
            w.writerow([_cell(r.get(c)) for c in cols])


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _uid(rng):
    return str(uuid.UUID(int=rng.getrandbits(128), version=4))


def _pick(rng, pairs):
    return rng.choices([p[0] for p in pairs], weights=[p[1] for p in pairs])[0]


def _slots(rng):
    item, item2 = rng.sample(ITEMS, 2)
    price = rng.choice([80, 100, 120, 150, 200, 250])
    s = dict(item=item, item2=item2, price=price, size=rng.choice(SIZES), color=rng.choice(COLORS),
             order=rng.randint(40000, 99999), days=rng.randint(2, 9), code=rng.choice(CODES), height=rng.choice(HEIGHTS),
             weight=rng.choice(WEIGHTS), month=rng.choice(MONTHS), units=rng.randint(2, 40), status=rng.choice(STATUSES),
             carrier=rng.choice(CARRIERS), eta="Oct %d" % rng.randint(14, 26), window=30, refund_days=rng.choice([3, 5, 7]),
             rma="RMA-%d" % rng.randint(10000, 99999))
    for i, b in enumerate(rng.sample(BRANDS, 3), 1):
        s["p%d" % i] = "%s %s" % (b, item.title())
        s["sku%d" % i] = "SKU-%04d" % rng.randint(1000, 9999)
    s["price1"] = price - rng.randint(5, 30)
    s["price2"] = price - rng.randint(0, 15)
    s["price3"] = max(20, price - rng.randint(30, 60))
    s["total"] = "%.2f" % (s["price1"] * 0.9)
    return s


def _plan(g, kind, tag, s, rng):
    """-> list of (tool, ok) in call order, for the run kind."""
    flow = g["flow"].get(tag) or g["flow"][None]
    ft = g["fail_tool"] if g["fail_tool"] in flow else flow[-1]
    if kind in ("clarify", "dodge", "ack"):
        return []
    if kind in ("ok", "wrong"):
        return [(t, True) for t in flow]
    if kind == "recovered":
        return [c for t in flow for c in (([(t, False), (t, True)]) if t == ft else [(t, True)])]
    if kind == "partial":
        return [(t, True) for t in flow[:-1]]
    if kind in ("fail_end", "fail_answer"):
        calls = []
        for t in flow:
            if t == ft:
                return calls + [(t, False)] * g["fail_k"]
            calls.append((t, True))
        return calls
    if kind == "pileup":
        calls = []
        for t in flow:
            calls += [(t, False)] * rng.randint(2, 3) + [(t, True)]
        while len(calls) < 8:
            calls.insert(len(calls) - 1, (flow[-1], False))
        return calls
    if kind in ("unsupported", "alt"):
        return list(g["unsup"])
    raise ValueError(kind)


def _first_sentence(text):
    i = text.find(". ")
    return text[:i + 1] if i > 0 else text


def _run(rng, gk, kind, ask_text, tag, s, start, conv, agent, env):
    """One canonical dossier (CONTRACT.md) plus the grader's view of it under key '_grade'."""
    g = GROUPS[gk]
    calls, t, errs = [], 0.0, []
    plan = _plan(g, kind, tag, s, rng)
    wrong_tool, wrong_fn = g["wrong"]
    for tool, ok in plan:
        args_fn, res_fn, base = TOOLS[tool]
        if ok:
            dur = round(base * rng.lognormvariate(0, 0.35), 2)
            res = wrong_fn(s) if (kind == "wrong" and tool == wrong_tool) else res_fn(s)
            c = {"name": tool, "duration_s": dur, "args": json.dumps(args_fn(s)), "result": json.dumps(res), "success": True,
                 "error": None}
        else:
            txt, dur = ERRORS[tool]
            txt = txt.format(**s)
            dur = round(dur + (rng.random() * 0.4 if dur >= 30 else rng.random()), 2)
            errs.append(txt)
            c = {"name": tool, "duration_s": dur, "args": json.dumps(args_fn(s)), "result": json.dumps({"error": txt}),
                 "success": False, "error": txt}
        t += 0.6 + rng.random() * 1.2
        c["ts"] = _iso(start + timedelta(seconds=t))
        t += dur
        calls.append(c)
    akind = "ok" if kind == "recovered" else kind
    key = akind + ":" + str(tag)
    ans = None
    if kind not in ("fail_end",):
        text = {"ack": "You're welcome! Anything else I can help with?"}.get(kind)
        if kind == "pileup":
            text = g["answers"].get("partial", g["answers"].get("ok"))
        elif text is None:
            text = g["answers"].get(key) or g["answers"].get(akind) or g["answers"]["ok"]
        ans = text.format(**s)
    llm = len(plan) + 1
    lat = round(t + sum(1.5 + rng.random() * 2.0 for _ in range(llm)), 2)
    n_fail = sum(1 for c in calls if not c["success"])
    itok = 1500 + 650 * len(calls) + 3 * len(ask_text) + rng.randint(0, 900)
    otok = 90 + 70 * len(calls) + len(ans or "") // 3 + rng.randint(0, 60)
    d = {"run_id": _uid(rng), "conversation_id": conv, "agent": agent, "environment": env, "first_ts": _iso(start),
         "last_ts": _iso(start + timedelta(seconds=lat)), "latency_s": lat, "user_message": ask_text, "final_answer": ans,
         "ends_without_answer": ans is None, "tool_calls": calls, "n_llm_calls": llm, "models": ["demo-model"],
         "input_tokens": itok, "output_tokens": otok, "cost_usd": None, "n_tool_failures": n_fail,
         "errors": sorted(set(errs)), "extra": {}}
    d["_meta"] = {"group": gk, "kind": kind, "tag": tag}
    return d


def _rationale(rng, d, kind, outcome):
    g = GROUPS[d["_meta"]["group"]]
    fails = [c for c in d["tool_calls"] if not c["success"]]
    tool = fails[0]["name"] if fails else (d["tool_calls"][-1]["name"] if d["tool_calls"] else "tool")
    if kind == "wrong":
        tool = g["wrong"][0]
    return rng.choice(RAT[(kind, outcome)]).format(gist=g["gist"], gap=g["gap"], contra=g["contra"], thing=g["thing"],
                                                   tool=tool, k=len(fails), lat=int(d["latency_s"]))


def _quote(d, kind):
    fails = [c for c in d["tool_calls"] if not c["success"]]
    if fails and kind in ("fail_end", "fail_answer", "pileup", "recovered"):
        return fails[0]["error"]
    if d["final_answer"]:
        q = _first_sentence(d["final_answer"])
        return (q if len(q) >= 15 else d["final_answer"])[:200]
    return d["user_message"].strip()[:200]


def make_out(path, n=160, seed=7, n_unusable=5, n_ungraded=6, with_cost=True):
    """Write the synthetic OUT directory under `path`; see the module docstring. -> meta dict."""
    rng = random.Random(seed)
    out = os.path.abspath(path)
    for sub in ("normalized", "cig"):
        os.makedirs(os.path.join(out, sub), exist_ok=True)
    target = n + n_ungraded
    envs = [("production", 62), ("staging", 20), ("preview-12", 6), ("preview-31", 5), ("preview-48", 4), ("dev", 3)]
    runs = []                                    # canonical dossiers incl. '_meta'
    conv_runs = defaultdict(list)
    while len(runs) < target:
        gk = _pick(rng, GROUP_WEIGHTS)
        g = GROUPS[gk]
        s = _slots(rng)
        conv = _uid(rng)
        env = _pick(rng, envs)
        agent = g["agent"]
        left = target - len(runs)
        k = min(left, _pick(rng, [(1, 62), (2, 30), (3, 8)]))
        t0 = T0 + timedelta(days=rng.randint(0, 7), seconds=rng.randint(0, 14 * 3600))
        prev = None                               # (outcome-ish, kind)
        for i in range(k):
            tag, kind = None, None
            if prev is None:
                kind = _pick(rng, list(g["kinds"].items()))
                pool = [a for a in g["asks"] if (a[1] == "unsupported") == (kind in ("unsupported", "alt"))
                        and (kind != "wrong" or g["wrong"][0] in (g["flow"].get(a[1]) or g["flow"][None]))]
                text, tag = rng.choice(pool)
                ask = text.format(**s)
            elif prev == "good":
                if rng.random() < 0.55:
                    kind, ask = "ack", rng.choice(THANKS)
                else:
                    kind = _pick(rng, [(a, b) for a, b in g["kinds"].items() if a not in ("unsupported", "alt")])
                    text, tag = rng.choice([a for a in g["asks"] if a[1] != "unsupported"])
                    ask = text.format(**s)
            else:
                if rng.random() < 0.75:
                    kind = "ok" if rng.random() < 0.6 else _pick(rng, [(a, b) for a, b in g["kinds"].items()
                                                                       if a not in ("unsupported", "alt")])
                    ask = rng.choice(g["fix"] + FIX_ANY).format(**s)
                    tag = None
                else:
                    kind = _pick(rng, [(a, b) for a, b in g["kinds"].items() if a not in ("unsupported", "alt")])
                    text, tag = rng.choice([a for a in g["asks"] if a[1] != "unsupported"])
                    ask = text.format(**s)
            d = _run(rng, gk, kind, ask, tag, s, t0, conv, agent, env)
            runs.append(d)
            conv_runs[conv].append(d)
            o = _pick(rng, OUT_P[kind])
            d["_meta"]["outcome"] = o
            prev = "good" if o == 1 else "poor"
            t0 = t0 + timedelta(seconds=d["latency_s"] + rng.randint(25, 240))
    # unusable runs: blank asks (auto) and one flagged by the ingest check (model)
    unusable = []
    for j in range(n_unusable):
        s = _slots(rng)
        conv = _uid(rng)
        t0 = T0 + timedelta(days=rng.randint(0, 7), seconds=rng.randint(0, 14 * 3600))
        blank = j < n_unusable - 1                 # the last one has a real ask but is flagged by the ingest check
        ask = ["", "   ", "\n", ""][j % 4] if blank else GROUPS["shop"]["asks"][0][0].format(**s)
        d = _run(rng, "shop", "ack" if blank else "ok", ask, None, s, t0, conv, GROUPS["shop"]["agent"], "production")
        if blank:
            d.update(tool_calls=[], n_tool_failures=0, errors=[], n_llm_calls=1, final_answer="Hi! How can I help you today?",
                     ends_without_answer=False)
        d["_meta"]["outcome"] = None
        runs.append(d)
        conv_runs[conv].append(d)
        unusable.append((d["run_id"], "no_ask" if blank else "the exporter replayed this trace twice (duplicate)",
                         "auto" if blank else "model"))
    runs.sort(key=lambda r: (r["first_ts"], r["run_id"]))
    unusable_ids = {u[0] for u in unusable}
    usable = [r for r in runs if r["run_id"] not in unusable_ids]
    # which usable runs stay ungraded
    ungraded = set(r["run_id"] for r in rng.sample(usable, n_ungraded)) if n_ungraded else set()
    if with_cost:
        for r in runs:
            r["cost_usd"] = round(r["input_tokens"] * 3e-6 + r["output_tokens"] * 15e-6, 6)
    # conversation chains (usable runs only), for reactions
    by_conv = defaultdict(list)
    for r in usable:
        by_conv[r["conversation_id"]].append(r)
    nxt = {}
    for rs in by_conv.values():
        rs.sort(key=lambda r: (r["first_ts"], r["run_id"]))
        for a, b in zip(rs, rs[1:]):
            nxt[a["run_id"]] = b["run_id"]

    # scores
    scores = {}
    for r in usable:
        if r["run_id"] in ungraded:
            continue
        m = r["_meta"]
        g = GROUPS[m["group"]]
        o = m["outcome"]
        conf = round(rng.uniform(0.85, 0.97) if o == 1 else rng.uniform(0.55, 0.8) if o == 0 else rng.uniform(0.75, 0.95), 2)
        issue = "" if o == 1 else g["issues"].get(m["kind"], FALLBACK_ISSUE.get(m["kind"], "answer thin on detail"))
        persona = g["labels"][0]
        x = rng.random()
        if x < 0.08:
            persona = g["labels"][1]
        elif x < 0.11:
            persona = ""
        scores[r["run_id"]] = dict(run_id=r["run_id"], outcome_score=o, confidence=conf,
                                   rationale=_rationale(rng, r, m["kind"], o), key_quote=_quote(r, m["kind"]),
                                   persona=persona, issue=issue, reaction_run_id=nxt.get(r["run_id"], ""))

    # CIG derivations (same rules as the real builder: failed = success false and not cancelled)
    recs = []
    for r in usable:
        rec = {k: v for k, v in r.items() if k != "_meta"}
        tcs = r["tool_calls"]
        fails = [c for c in tcs if c.get("success") is False and "cancel" not in str(c.get("error") or "").lower()]
        rec["n_tool_failures"] = len(fails)
        rec["tool_fail"] = int(bool(fails))
        rec["no_answer"] = int(bool(r["ends_without_answer"]) or not str(r["final_answer"] or "").strip())
        rec["unrecovered"] = int(bool(fails) and (bool(rec["no_answer"]) or tcs[-1].get("success") is False))
        rec["tokens"] = r["input_tokens"] + r["output_tokens"]
        rec["n_tool_calls"] = len(tcs)
        seen, wf = set(), []
        for c in tcs:
            if c["name"] not in seen:
                seen.add(c["name"])
                wf.append(c["name"])
        rec["workflow"] = " > ".join(wf)
        sc = scores.get(r["run_id"])
        if sc:
            rec.update(outcome_score=sc["outcome_score"], label=LABELS[sc["outcome_score"]], confidence=sc["confidence"],
                       rationale=sc["rationale"], key_quote=sc["key_quote"], persona=sc["persona"], issue=sc["issue"],
                       reaction_run_id=sc["reaction_run_id"])
        else:
            rec.update(outcome_score=None, label="ungraded", confidence="", rationale="", key_quote="", persona="",
                       issue="", reaction_run_id="")
        recs.append(rec)
    lat = [x["latency_s"] for x in recs]
    thr = _pctl(lat, 0.9) if len(recs) >= 10 else None
    p90 = _pctl([x["n_tool_calls"] for x in recs], 0.9)
    for x in recs:
        x["slow"] = int(thr is not None and x["latency_s"] > thr)
        x["heavy"] = int(x["n_tool_calls"] >= 8 and p90 is not None and x["n_tool_calls"] >= p90)

    # files
    with open(os.path.join(out, "normalized", "runs.jsonl"), "w", encoding="utf-8") as f:
        for r in runs:
            f.write(json.dumps({k: v for k, v in r.items() if k != "_meta"}, ensure_ascii=False) + "\n")
    _write_csv(os.path.join(out, "normalized", "unusable.csv"), ["run_id", "reason", "source"],
               [dict(run_id=a, reason=b, source=c) for a, b, c in unusable])
    _write_csv(os.path.join(out, "scores.csv"), SCORE_COLS, [scores[r["run_id"]] for r in usable if r["run_id"] in scores])
    _write_csv(os.path.join(out, "cig", "runs.csv"), RUN_COLS, recs)
    with open(os.path.join(out, "cig", "run_dossiers.jsonl"), "w", encoding="utf-8") as f:
        for x in recs:
            f.write(json.dumps(x, sort_keys=True, ensure_ascii=False) + "\n")
    _graph_files(os.path.join(out, "cig"), recs)

    graded = [r for r in usable if r["run_id"] in scores]
    return {"out": out, "n": len(graded), "run_ids": [r["run_id"] for r in graded],
            "groups": {r["run_id"]: r["_meta"]["group"] for r in graded},
            "group_names": {k: v["name"] for k, v in GROUPS.items()},
            "unusable": [u[0] for u in unusable], "ungraded": sorted(ungraded),
            "conversations": {c: [r["run_id"] for r in sorted(rs, key=lambda r: (r["first_ts"], r["run_id"]))]
                              for c, rs in sorted(conv_runs.items())}}


# ---------------------------------------------------------------- nodes / edges / errors (as build_cig writes them)
_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
_HEX = re.compile(r"\b(?:0x)?[0-9a-f]{8,}\b", re.I)
_NUM = re.compile(r"\d+(?:[.,]\d+)*")
_QUOTED = re.compile(r"(['\"`])[^'\"`]{0,80}\1")


def _error_class(text):
    t = _QUOTED.sub(" ", _HEX.sub(" ", _UUID.sub(" ", str(text or "").strip().lower())))
    t = _NUM.sub(" ", t)
    return " ".join(re.findall(r"[a-z_][a-z_\-]*", t)[:6]) or "unknown error"


def _failed(c):
    return c.get("success") is False and "cancel" not in str(c.get("error") or "").lower()


def _graph_files(cig, recs):
    calls, runs_at, e_cnt, e_runs, e_tf, e_tc = defaultdict(list), defaultdict(set), Counter(), defaultdict(set), Counter(), Counter()
    for i, r in enumerate(recs):
        seq = [("sentinel:START", None)] + [("tool:" + c["name"], c) for c in r["tool_calls"]] + \
              [("sentinel:NO_ANSWER" if r["no_answer"] else "sentinel:ANSWER", None)]
        for nid, c in seq:
            runs_at[nid].add(i)
            if c is not None:
                calls[nid].append((c["duration_s"], _failed(c)))
        for (a, _), (b, cb) in zip(seq, seq[1:]):
            e_cnt[(a, b)] += 1
            e_runs[(a, b)].add(i)
            if cb is not None:
                e_tc[(a, b)] += 1
                e_tf[(a, b)] += int(_failed(cb))

    def outcomes(idx):
        o = [recs[i]["outcome_score"] for i in idx if recs[i]["outcome_score"] is not None]
        return (_mean(o), sum(1 for x in o if x == -1) / len(o), sum(1 for x in o if x == 1) / len(o)) if o else (None, None, None)
    nodes = []
    for nid in runs_at:
        idx = sorted(runs_at[nid])
        cs = calls.get(nid, [])
        durs = [d for d, _ in cs]
        mo, sb, sg = outcomes(idx)
        tool = nid.startswith("tool:")
        nodes.append({"node_id": nid, "kind": "tool" if tool else nid.split(":")[1].lower(),
                      "calls": len(cs) if tool else len(idx), "runs": len(idx), "fail_calls": sum(1 for _, f in cs if f),
                      "fail_rate": (sum(1 for _, f in cs if f) / len(cs)) if tool and cs else None,
                      "median_duration_s": _pctl(durs, 0.5), "p90_duration_s": _pctl(durs, 0.9),
                      "mean_run_tokens": _mean([recs[i]["tokens"] for i in idx]), "share_bad_runs": sb,
                      "share_good_runs": sg, "mean_outcome": mo})
    order = {"start": 0, "tool": 1, "answer": 2, "no_answer": 3}
    nodes.sort(key=lambda n: (order[n["kind"]], -n["calls"], n["node_id"]))
    edges = [{"source": a, "target": b, "count": e_cnt[(a, b)], "runs": len(e_runs[(a, b)]),
              "fail_rate_target": (e_tf[(a, b)] / e_tc[(a, b)]) if e_tc[(a, b)] else None,
              "mean_outcome": outcomes(sorted(e_runs[(a, b)]))[0]} for a, b in e_cnt]
    edges.sort(key=lambda e: (-e["count"], e["source"], e["target"]))
    errs = {}
    for r in recs:
        for c in r["tool_calls"]:
            if _failed(c):
                txt = str(c.get("error") or c.get("result") or "").strip() or "unknown error"
                d = errs.setdefault(_error_class(txt), {"count": 0, "runs": set(), "tools": Counter(), "example": txt})
                d["count"] += 1
                d["runs"].add(r["run_id"])
                d["tools"][c["name"]] += 1
    rows = [{"error_class": k, "count": d["count"], "runs": len(d["runs"]),
             "tool": sorted(d["tools"].items(), key=lambda kv: (-kv[1], kv[0]))[0][0], "example": d["example"][:300]}
            for k, d in errs.items()]
    rows.sort(key=lambda x: (-x["count"], x["error_class"]))
    _write_csv(os.path.join(cig, "nodes.csv"), NODE_COLS, nodes)
    _write_csv(os.path.join(cig, "edges.csv"), EDGE_COLS, edges)
    _write_csv(os.path.join(cig, "errors.csv"), ERR_COLS, rows)


if __name__ == "__main__":
    import sys
    m = make_out(sys.argv[1] if len(sys.argv) > 1 else "study-fixture-out")
    print("wrote %s: %d graded runs, %d unusable, %d ungraded" % (m["out"], m["n"], len(m["unusable"]), len(m["ungraded"])))
