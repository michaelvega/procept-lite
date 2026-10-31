"""Synthetic export plus a driver for the strict end-to-end flow (stdlib only).

    python3 tests/make_e2e_out.py OUT [--keep-study] [--runs 72]

builds OUT from scratch: a tracing export of an outdoor-gear shop assistant (four user-goal groups, real-looking
asks, tool names, tool results and failures), then walks the strict pipeline stage by stage with fake graders and
hand-written sign-offs, and leaves cig.html and scorecard.html in OUT. With --keep-study the study folder
(the starter sections it built) stays in OUT/study. tests/test_flow.py imports Flow to assert every seam.
"""
import csv
import json
import os
import random
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLITE = os.path.join(ROOT, "plugins", "procept-lite", "scripts", "plite.py")
PRICES = os.path.join(ROOT, "tests", "fixtures", "prices.json")
MODEL = "claude-sonnet-5-5"
COLS = ["run_id", "outcome_score", "confidence", "rationale", "key_quote", "persona", "issue", "reaction_run_id"]

ITEMS = ["rain shell", "trail runner", "two-person tent", "down sleeping bag", "hiking boot", "camp stove",
         "steel bottle", "trekking pole set", "headlamp", "daypack", "fleece pullover", "wool base layer"]
BRANDS = ["Alder", "Ridge", "Summit", "Cedar", "Granite", "Juniper"]

ASKS = {
    "track": ["Where is my parcel? I ordered the %(b)s %(i)s last week and nothing has come.",
              "Can you tell me when the %(i)s I ordered will get here?",
              "My tracking page has said in transit for days. Where is my %(b)s %(i)s now?",
              "Has my order shipped yet? I paid for express delivery on the %(i)s.",
              "I need to change the delivery address on the %(i)s I just bought."],
    "returns": ["I want to send back the %(b)s %(i)s, it is too small. How do I get a refund?",
                "Please start a return for the %(i)s. It arrived with a broken zip.",
                "How do I return the %(b)s %(i)s and get my money back to my card?",
                "The %(i)s is not what I expected. I would like a refund label.",
                "I bought the wrong %(i)s as a gift. Can I exchange it for a different one?"],
    "shop": ["I am looking for a %(i)s under a hundred dollars that works for cold weather.",
             "Which %(b)s %(i)s do you recommend for a weekend trip?",
             "Do you have the %(i)s in stock in green? I need it before the weekend.",
             "What is the lightest %(i)s you sell? Weight matters more than price.",
             "Show me a cheap %(i)s for a beginner."],
    "fit": ["I am tall with broad shoulders. What size %(i)s should I get in the %(b)s line?",
            "Does the %(b)s %(i)s run small? I normally wear a medium.",
            "Will the %(i)s fit over a thick winter jacket?",
            "What size %(i)s fits a narrow foot? My last pair gave me blisters.",
            "Is the %(i)s compatible with the %(b)s pack I already own?"],
}
PERSONA = {"track": "Order tracking", "returns": "Returns and refunds", "shop": "Product search", "fit": "Size and fit"}
GOAL = {"track": "find out where an order is and when it arrives",
        "returns": "send an item back and get the money returned",
        "shop": "find the right product for a trip and budget",
        "fit": "work out which size or fit to buy"}
TOOLS = {"track": ["lookup_order", "track_shipment"], "returns": ["lookup_order", "get_return_policy", "create_return"],
         "shop": ["search_catalog", "get_product", "check_stock"], "fit": ["get_product", "get_size_guide", "search_docs"]}
RESULT = {"lookup_order": {"status": "shipped", "carrier": "Northline", "items": 1},
          "track_shipment": {"last_scan": "regional hub", "eta_days": 2},
          "get_return_policy": {"window_days": 30, "free_label": True},
          "create_return": {"return_id": "RET-48213", "label_url": "issued"},
          "search_catalog": {"results": [{"name": "Ridge shell", "price": 89}, {"name": "Alder shell", "price": 94}]},
          "get_product": {"name": "Ridge shell", "sizes": ["S", "M", "L", "XL"], "weight_g": 410},
          "check_stock": {"in_stock": True, "units": 12},
          "get_size_guide": {"fit": "true to size", "note": "size up for layers"},
          "search_docs": {"hits": 3, "top": "Sizing guide for layering"}}
GOOD_ANS = {"track": "Your order has shipped with Northline and the last scan was at the regional hub. It should arrive in about two days.",
            "returns": "I have started your return. A free prepaid label has been issued and you have 30 days to post the item back.",
            "shop": "The Ridge shell at 89 dollars is the best match for cold weather and a modest budget. It is in stock in all sizes.",
            "fit": "The line runs true to size. Go one size up if you plan to wear layers underneath."}
WEAK_ANS = {"track": "Your order is on its way. Please check the carrier site for more detail.",
            "returns": "You can return items within 30 days. Please visit the returns page to continue.",
            "shop": "We have many products that might suit you. Browse the catalog to compare options.",
            "fit": "Sizing varies by product. Please see the size chart on the product page."}
FAIL_ANS = {"track": "I could not reach the carrier right now. Please try again later.",
            "returns": "I was unable to start your return because the returns system did not respond. Please try again later."}
FOLLOW = [("thanks", "Thanks, that helps."), ("again", "That did not answer my question. Can you try again?"),
          ("thanks", "Great, thank you."), ("again", "Still nothing. Please just start it.")]


def ts(t):
    return t.strftime("%Y-%m-%d %H:%M:%S.%f+00")


def make_export(path, n=72, seed=11):
    """Write a Logfire-style JSONL export with `n` graded-looking runs plus one blank-ask run and one mixed-up run.
    Returns truth: {call_id: {"group", "score", "ask", "failed", "answer"}} (only for the runs a grader would see)."""
    rnd = random.Random(seed)
    t0 = datetime(2026, 10, 12, 7, 0, 0, tzinfo=timezone.utc)
    rows, truth = [], {}
    groups = ["returns"] * 5 + ["track"] * 4 + ["shop"] * 4 + ["fit"] * 3
    counter = [0]

    def span(trace, sid, parent, name, start, dur, attrs, exc=None):
        r = {"trace_id": trace, "span_id": sid, "parent_span_id": parent, "span_name": name,
             "start_timestamp": ts(start), "end_timestamp": ts(start + timedelta(seconds=dur)), "duration": round(dur, 3),
             "level": 17 if exc else 9, "is_exception": bool(exc), "attributes": json.dumps(attrs)}
        if exc:
            r["exception_type"], r["exception_message"] = exc
        rows.append(r)

    def one_run(conv, group, ask, start, kind, blank=False):
        counter[0] += 1
        cid, tr = "call-%d" % counter[0], "trace-%d" % counter[0]
        base = {"gen_ai.conversation.id": conv, "gen_ai.agent.call.id": cid, "gen_ai.agent.name": "shop_assistant"}
        tools = TOOLS[group]
        failed, t, answer = False, start + timedelta(seconds=0.3), None
        nin = rnd.randint(900, 2400)
        slow = rnd.random() < 0.1
        calls = []
        for k, name in enumerate([] if ask.strip() == "" else tools):
            dur = rnd.uniform(0.4, 1.4) * (9 if slow and k == len(tools) - 1 else 1)
            bad = (group == "returns" and name == "create_return" and kind != "good") or \
                  (group == "track" and name == "track_shipment" and kind == "fail")
            if bad:
                span(tr, "x%d_%d" % (counter[0], k), "a%d" % counter[0], "execute_tool " + name, t, dur,
                     dict(base, **{"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": name,
                                   "gen_ai.tool.call.arguments": {"order": "recent"}}), ("TimeoutError", "upstream timed out"))
                t += timedelta(seconds=dur + 0.2)
                failed = True
                if kind == "recovered":
                    span(tr, "x%d_%dr" % (counter[0], k), "a%d" % counter[0], "execute_tool " + name, t, 0.8,
                         dict(base, **{"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": name,
                                       "gen_ai.tool.call.arguments": {"order": "recent"},
                                       "gen_ai.tool.call.result": RESULT[name]}))
                    t += timedelta(seconds=1.0)
                    failed = False
            else:
                span(tr, "x%d_%d" % (counter[0], k), "a%d" % counter[0], "execute_tool " + name, t, dur,
                     dict(base, **{"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": name,
                                   "gen_ai.tool.call.arguments": ({"sku": "recent"} if blank else {"query": ask[:30]}),
                                   "gen_ai.tool.call.result": RESULT[name]}))
                t += timedelta(seconds=dur + 0.2)
            calls.append(name)
            span(tr, "l%d_%d" % (counter[0], k), "a%d" % counter[0], "chat gpt-4o", t, rnd.uniform(0.8, 1.8),
                 dict(base, **{"gen_ai.operation.name": "chat", "gen_ai.request.model": "gpt-4o-2024-08-06",
                               "gen_ai.usage.input_tokens": nin + k * 500, "gen_ai.usage.output_tokens": rnd.randint(40, 160)}))
            t += timedelta(seconds=1.6)
        if kind in ("good", "recovered"):
            answer, score = GOOD_ANS[group], 1
        elif kind == "weak":
            answer, score = WEAK_ANS[group], 0
        else:
            answer, score = (FAIL_ANS.get(group) if kind == "fail" else None), -1
            if kind == "silent":
                answer = None
        attrs = dict(base, **{"gen_ai.operation.name": "invoke_agent", "deployment.environment.name": "prod",
                              "gen_ai.input.messages": [{"role": "user", "parts": [{"type": "text", "content": ask}]}]})
        if answer:
            attrs["gen_ai.output.messages"] = [{"role": "assistant", "parts": [{"type": "text", "content": answer}],
                                                "finish_reason": "stop"}]
        span(tr, "a%d" % counter[0], None, "invoke_agent shop_assistant", start, (t - start).total_seconds() + 0.5, attrs)
        if not blank:
            truth[cid] = {"group": group, "score": score, "ask": ask, "failed": failed, "answer": answer or ""}
        return cid, t

    clock = t0
    made = 0
    while made < n:
        g = rnd.choice(groups)
        conv = "conv-%03d" % (made + 1)
        ask = rnd.choice(ASKS[g]) % {"i": rnd.choice(ITEMS), "b": rnd.choice(BRANDS)}
        r = rnd.random()
        if g == "returns":
            kind = "good" if r < 0.3 else "recovered" if r < 0.4 else "fail" if r < 0.8 else "silent" if r < 0.9 else "weak"
        elif g == "track":
            kind = "good" if r < 0.55 else "weak" if r < 0.8 else "fail" if r < 0.92 else "silent"
        elif g == "shop":
            kind = "good" if r < 0.65 else "weak" if r < 0.95 else "silent"
        else:
            kind = "good" if r < 0.5 else "weak" if r < 0.95 else "silent"
        clock += timedelta(minutes=rnd.randint(7, 55))
        cid, t = one_run(conv, g, ask, clock, kind)
        made += 1
        if kind != "good" and made < n and rnd.random() < 0.5:   # the user comes back and presses
            clock = t + timedelta(minutes=2)
            cid2, t = one_run(conv, g, FOLLOW[1][1], clock, "weak" if rnd.random() < 0.5 else "good")
            made += 1
    # one blank ask (automatic no_ask) and one run another check will flag
    clock += timedelta(minutes=30)
    one_run("conv-blank", "shop", "   ", clock, "good", blank=True)
    clock += timedelta(minutes=30)
    flagged, _ = one_run("conv-mixed", "shop", "Which pack suits a long day hike in the rain?", clock, "good", blank=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return truth, flagged


def ev(ids, what):
    return [{"id": i, "finding": "%s %s: value in the artifact matches the data (case %d)" % (what, i, k)}
            for k, i in enumerate(ids)]


class Flow:
    """Drives `plite.py` in a subprocess with the gates ON."""

    def __init__(self, out, work, n=72):
        self.out, self.work = out, work
        os.makedirs(work, exist_ok=True)
        self.src = os.path.join(work, "export.jsonl")
        self.truth, self.flagged = make_export(self.src, n)
        self.env = {k: v for k, v in os.environ.items() if k != "PLITE_NO_GATE"}

    # --- plumbing
    def plite(self, *args):
        p = subprocess.run([sys.executable, PLITE] + list(args), capture_output=True, text=True, env=self.env)
        return p.returncode, p.stdout, p.stderr

    def run(self):
        rc, so, se = self.plite("run", "--src", self.src, "--out", self.out, "--prices", PRICES)
        if rc != 0:
            raise RuntimeError("run failed: %s %s" % (so, se))
        return [x for x in so.strip().splitlines() if x][-1]

    def sign(self, stage, ids, checked=None, **extra):
        rec = dict(stage=stage, model=MODEL, verdict="pass", checked=checked or len(ids), mismatches=0,
                   notes="compared the artifact with the data; every item agrees", evidence=ev(ids[:8], stage))
        rec.update(extra)
        f = os.path.join(self.work, stage + ".record.json")
        with open(f, "w") as fh:
            json.dump(rec, fh)
        rc, so, se = self.plite("check", "record", "--out", self.out, "--file", f)
        if rc != 0:
            raise RuntimeError("check record %s: %s %s" % (stage, so, se))

    # --- stage by stage
    def write_mapping(self, model=MODEL):
        d = json.load(open(os.path.join(self.out, "mapping_rescue", "mapping.draft.json")))
        d = {k: v for k, v in d.items() if not k.startswith("_")}
        d["_rescue"] = {"model": model, "rows_reviewed": 40, "notes": "read the sample against the draft; fields line up"}
        with open(os.path.join(self.out, "mapping.json"), "w") as f:
            json.dump(d, f)

    def run_ids(self):
        return [json.loads(x)["run_id"] for x in open(os.path.join(self.out, "normalized", "runs.jsonl"))]

    def sign_ingest(self):
        ids = self.run_ids()
        self.sign("ingest", ids, checked=min(len(ids), 40),
                  unusable=[{"run_id": self.flagged, "reason": "the answer belongs to another run"}])

    def write_shards(self):
        g = os.path.join(self.out, "grading")
        for x in json.load(open(os.path.join(g, "shards_index.json"))):
            with open(os.path.join(g, "scores", x["shard"] + ".csv"), "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(COLS)
                for rid in x["run_ids"]:
                    t = self.truth[rid]
                    s = t["score"]
                    quote = t["ask"] if s != 1 else ""
                    w.writerow([rid, s, "0.85", "the answer %s what was asked" % ("matches" if s == 1 else "misses"),
                                quote, PERSONA[t["group"]], "" if s == 1 else ("tool failed" if t["failed"] else "vague answer"), ""])
        for x in json.load(open(os.path.join(g, "audit_index.json"))):
            with open(os.path.join(g, "audit", x["shard"] + ".csv"), "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(COLS)
                for rid in x["run_ids"]:
                    w.writerow([rid, self.truth[rid]["score"], "0.8", "second look", "", PERSONA[self.truth[rid]["group"]], "", ""])

    def scored_ids(self):
        return [r["run_id"] for r in csv.DictReader(open(os.path.join(self.out, "scores.csv")))]

    def run_starters(self):
        """Run every starter section on the real out dir, then write the personas and problem from the data."""
        for k in ("eda", "reward", "affinity", "empathy", "impact"):
            p = subprocess.run([sys.executable, "-I", os.path.join(self.out, "study", "sections", k + ".py")],
                               capture_output=True, text=True, cwd=self.out)
            if p.returncode != 0:
                raise RuntimeError("section %s failed: %s" % (k, p.stderr[-600:]))

    def write_problem(self):
        short = {}
        for rid in self.scored_ids():
            t = self.truth[rid]
            short.setdefault(t["group"], []).append(rid)
        grp = max(short, key=lambda g: sum(self.truth[r]["score"] != 1 for r in short[g]))
        ids = short[grp]
        qid = next(r for r in ids if self.truth[r]["score"] != 1 and len(self.truth[r]["ask"]) <= 150)
        d = {"persona": PERSONA[grp],
             "goal": "These shoppers want to %s without having to chase anyone." % GOAL[grp],
             "problem": "The returns system times out before a label is issued, and the assistant then stops without a next step.",
             "headline": "This shopper wants to send an item back and get a refund but can't because the return step keeps timing out.",
             "quote": self.truth[qid]["ask"], "quote_run_id": qid, "run_ids": ids, "model": MODEL}
        os.makedirs(os.path.join(self.out, "scorecard_inputs"), exist_ok=True)
        with open(os.path.join(self.out, "scorecard_inputs", "user_goal_problem.json"), "w") as f:
            json.dump(d, f)
        return d

    def sign_study(self):
        self.sign("study", ["eda", "reward", "affinity", "empathy", "impact"], checked=5)


def build(out, keep_study=False, n=72):
    out = os.path.abspath(out)
    if os.path.exists(out):
        if not os.listdir(out) or os.path.exists(os.path.join(out, "status.json")):
            shutil.rmtree(out)
        else:
            raise SystemExit("refusing to replace %s: not an earlier e2e output" % out)
    work = out + "-work"
    shutil.rmtree(work, ignore_errors=True)
    fl = Flow(out, work, n)

    def want(step):
        line = fl.run()
        if not line.startswith("NEXT: %s |" % step):
            raise SystemExit("expected NEXT: %s, got: %s" % (step, line))

    want("mapping_rescue")
    fl.write_mapping()
    want("ingest_verify")
    fl.sign_ingest()
    want("grade")
    fl.write_shards()
    want("merge_verify")
    fl.sign("merge", fl.scored_ids(), checked=min(len(fl.scored_ids()), 45))
    want("cig_verify")
    fl.sign("cig", ["runs.csv", "nodes.csv", "edges.csv", "summary.json", "errors.csv"])
    want("study")
    fl.run_starters()
    fl.write_problem()
    fl.sign_study()
    if keep_study:   # run's own report step deletes the study; build the pages by hand and keep it
        for cmd in (("viewer",), ("report", "--keep-study")):
            rc, so, se = fl.plite(cmd[0], "--out", out, *cmd[1:])
            if rc != 0:
                raise SystemExit("%s failed: %s %s" % (cmd[0], so, se))
        fl.sign("scorecard", ["scorecard.html", "cig.html", "scorecard.json", "system5.json", "summary.json"], checked=5)
        line = fl.run()
    else:
        want("scorecard_verify")
        fl.sign("scorecard", ["scorecard.html", "cig.html", "scorecard.json", "system5.json", "summary.json"], checked=5)
        line = fl.run()
    shutil.rmtree(work, ignore_errors=True)
    if not line.startswith("NEXT: done |"):
        raise SystemExit("expected done, got: " + line)
    print(line)
    return out


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        raise SystemExit(__doc__)
    runs = int(args[args.index("--runs") + 1]) if "--runs" in args else 72
    build(args[0], "--keep-study" in args, runs)
