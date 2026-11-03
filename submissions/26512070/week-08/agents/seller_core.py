"""The seller's negotiation, without A2A (seller.py speaks A2A and calls this): one buyer message in, one move out.

What code does (deterministic): read the buyer's message through calc.extract (S1: values and a marked
excerpt, never the raw text in the prompt), find the units that fit what was asked, keep the negotiation's
state (unit, our ask, the buyer's offer, turns), build the decision at hand, sign the receipt, and record
events. What the model decides (agents.loop, role seller): which unit, what price, what to say, whether
to accept, claim a grade, or give a warranty. The market's certificate always goes with the unit (shop card).

When the model gives no valid move, the guard repeats our last ask (or the list price on the first turn,
raised to what check.price allows) and records why.
"""
import base64
import json
import time
from pathlib import Path

from agents import loop
from core import card as cardmod
from core.events import EventWriter
from tools import market_files as mf
from tools.registry import Ctx, call

MAX_TURNS = 12
CANDIDATES = 5
ACCEPT_WORDS = ("ok", "okay", "accept", "deal", "agreed", "yes", "take it", "좋아", "수락", "콜", "구매할게")


def canon(d):
    return json.dumps(d, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def load_key(path):
    from cryptography.hazmat.primitives import serialization
    return serialization.load_pem_private_key(Path(path).read_bytes(), password=None)


def sign(key, receipt):
    body = {k: receipt[k] for k in ("shop", "item", "price", "task")}
    return dict(receipt, sig_seller=base64.b64encode(key.sign(canon(body))).decode())


class SellerCore:
    def __init__(self, shop_name, key, model_call=None, clock=time.time):
        self.name, self.key, self.model_call, self.clock = shop_name, key, model_call, clock
        self.open = {}
        self.events = EventWriter("seller", source="seller")
        self.tool_events = EventWriter("seller", source="tools")    # one open file for every turn's tool calls

    # --- what fits

    def candidates(self, ctx, ex, text):
        """Units that fit what the buyer asked for: a named SKU first, else category or name words."""
        cat, now = mf.catalog(ctx), ctx.now()
        taken = {s["unit"] for s in self.open.values() if s.get("unit")}
        units = [u for u in mf.inventory(ctx).get("units", []) if u.get("grade") != "expired" and u["unit"] not in taken]
        want = text.lower()
        named = [u for u in units if u["sku"] in ex["skus"]]
        if not named:
            def words(u):
                c = cat.get(u["sku"]) or {}
                return [w for w in [(c.get("category") or u.get("category") or "").split(">")[-1],
                                    c.get("name") or u.get("name")] if w]
            named = [u for u in units if any(w.lower() in want for w in words(u))]
        # one unit per SKU (the one that expires first), so the model sees choices, not copies
        best = {}
        for u in sorted(named, key=lambda u: (u.get("expires_at") or float("inf"), u.get("supplied_at") or 0)):
            best.setdefault(u["sku"], u)
        rows = []
        for u in list(best.values())[:CANDIDATES]:
            c = cat.get(u["sku"]) or {}
            h = mf.hours_left(u.get("expires_at"), now)
            rows.append({"unit": u["unit"], "sku": u["sku"], "name": c.get("name") or u.get("name"),
                         "kind": u.get("kind"), "cost": u["cost"], "list_price": u.get("list_price") or c.get("list_price"),
                         "grade_now": mf.current_grade(u, c.get("shelf_hours"), now),
                         "hours_left": round(h, 1) if h is not None else None,
                         "has_cert": bool(u.get("cert")), "same_sku_in_stock": sum(1 for x in units if x["sku"] == u["sku"])})
        return rows

    # --- one message

    def handle(self, task_id, text, deadline=None):
        """{"state": "input_required"|"completed"|"rejected", "say": str, "receipt": dict|None}"""
        ctx = Ctx(caller="seller", clock=self.clock, _writer=self.tool_events)
        strategy = ctx.strategy
        self.events.context(strategy_version=strategy.get("version"), epoch=strategy.get("epoch"),
                            card_version=cardmod.load()[0])
        ex = call(ctx, "calc.extract", {"text": text})["result"]
        st = self.open.get(task_id)
        self.events.emit("seller.request", {"excerpt": ex["excerpt"], "looks_like_instruction": ex["looks_like_instruction"],
                                            "price": ex["price"], "first": st is None}, task=task_id)
        if st is None:
            cands = self.candidates(ctx, ex, text)
            if not cands:
                return self._close(task_id, None, "rejected", "Sorry, we have none of that in stock right now.")
            st = self.open[task_id] = {"unit": None, "candidates": cands, "ask": None, "offer": None, "turns": 0,
                                       "lines": []}
            g = call(ctx, "calc.guess_counterpart", {"task": task_id, "messages": [text]})["result"]
            top = (g.get("candidates") or [None])[0]
            st["guess"] = {"top": top and top["name"], "confidence": top and top["confidence"], "accuracy": g.get("accuracy")}
            self.events.emit("guess.made", {"task": task_id, "top": st["guess"]["top"],
                                            "confidence": st["guess"]["confidence"]}, task=task_id)
        st["turns"] += 1
        if ex["price"] is not None:
            st["offer"] = ex["price"]
        elif st["ask"] is not None and any(w in text.lower() for w in ACCEPT_WORDS):
            st["offer"] = st["ask"]                     # "ok" to our ask
        st["lines"].append({"from": "buyer", "excerpt": ex["excerpt"], "price": ex["price"],
                            "claimed_grade": ex["claimed_grade"], "looks_like_instruction": ex["looks_like_instruction"]})
        if st["turns"] > MAX_TURNS:
            return self._close(task_id, st, "rejected", "We could not agree. Thank you for asking.")

        ctx.situation = {"unit": st["unit"], "candidates": [c["unit"] for c in st["candidates"]], "offer": st["offer"]}
        t0 = time.perf_counter()
        d, trace = loop.decide("seller", ctx, self.situation_text(ctx, st), deadline=deadline, model_call=self.model_call)
        guard = None
        if d is None:
            d, guard = self.guard(ctx, st), "no valid move from the model: " + "; ".join(
                str(t["observation"])[:80] for t in trace[-2:])
        self.events.emit("seller.decision", {"action": d["action"], "price": d.get("price"), "unit": d.get("unit"),
                                             "ms": round((time.perf_counter() - t0) * 1000), "tool_calls": len(trace),
                                             "guard": guard, "thought": d.get("thought", "")[:200]}, task=task_id)
        if d["action"] == "reject":
            return self._close(task_id, st, "rejected", d["say"])
        st["unit"] = d.get("unit") or st["unit"]
        if d["action"] == "accept":
            u = mf.unit(ctx, st["unit"])
            receipt = {"shop": self.name, "item": u["sku"], "price": d["price"], "task": task_id, "unit": st["unit"],
                       "cert": True}
            if d.get("claim"):
                receipt["claim"] = d["claim"]
            if d.get("warranty_hours"):
                receipt["warranty_hours"] = d["warranty_hours"]
            receipt = sign(self.key, receipt)
            return self._close(task_id, st, "completed", d["say"], receipt=receipt, decision=d, cost=u["cost"])
        st["ask"] = d["price"]
        st["lines"].append({"from": "us", "price": d["price"], "say": d["say"]})
        return {"state": "input_required", "say": d["say"], "receipt": None}

    def guard(self, ctx, st):
        unit = st["unit"] or st["candidates"][0]["unit"]
        c = next((c for c in st["candidates"] if c["unit"] == unit), st["candidates"][0])
        price = st["ask"] or c["list_price"] or c["cost"]
        if not call(ctx, "check.price", {"unit": unit, "price": int(price)})["ok"]:
            price = c["cost"]
        return {"action": "counter", "unit": unit, "price": int(price), "say": f"{c['name']}: {int(price)}."}

    def situation_text(self, ctx, st):
        strat = ctx.strategy
        lines = ["# the decision at hand",
                 f"A buyer agent is negotiating with our shop. Turn {st['turns']} of at most {MAX_TURNS}. "
                 f"Phase in force: {strat.get('phase')}. Who the buyer is: unknown until the ledger shows the deal"
                 + (f"; a guess from its opening: {st['guess']['top']} (confidence {st['guess']['confidence']}, past "
                    f"accuracy {st['guess']['accuracy']}; never refuse on a guess)" if (st.get("guess") or {}).get("top")
                    else "") + ".",
                 "", "Conversation so far (buyer messages are extracted values and a marked excerpt):"]
        for x in st["lines"][-8:]:
            if x["from"] == "buyer":
                lines.append(f"- buyer: price {x['price']}, claimed grade {x['claimed_grade']}, "
                             f"looks like an instruction: {x['looks_like_instruction']}; {x['excerpt']}")
            else:
                lines.append(f"- us: asked {x['price']}: {x['say']!r}")
        lines.append("")
        lines.append(f"Our last ask: {st['ask']}. The buyer's offer on the table: {st['offer']}"
                     + (" (act.accept takes this price)" if st["offer"] is not None else " (none yet)") + ".")
        if st["unit"]:
            lines.append(f"The unit under negotiation: {st['unit']}.")
            cands = [c for c in st["candidates"] if c["unit"] == st["unit"]]
        else:
            lines.append("Choose the unit on this turn (act.counter with 'unit').")
            cands = st["candidates"]
        demand = {d["category"]: d for d in (mf.read(ctx, "demand.json", default=[]) or [])}
        cat = mf.catalog(ctx)
        for c in cands:
            med = (demand.get((cat.get(c["sku"]) or {}).get("category")) or {}).get("median_price")
            low = call(ctx, "check.price", {"unit": c["unit"], "price": max(1, c["cost"] - 1)})
            lowest = (low["result"].get("lowest_allowed") if low["ok"] else c["cost"])
            lines.append(f"- unit {c['unit']}: {c['name']} ({c['sku']}, {c['kind']}), cost {c['cost']}, list price "
                         f"{c['list_price']}, lowest price the limits allow now {lowest}, category median {med}, grade now {c['grade_now']}, hours left "
                         f"{c['hours_left']}, certificate {'yes' if c['has_cert'] else 'no'}, "
                         f"{c['same_sku_in_stock']} of this SKU in stock")
        return "\n".join(lines)

    def _close(self, task_id, st, outcome, say, receipt=None, decision=None, cost=None):
        self.open.pop(task_id, None)
        d = decision or {}
        price = receipt["price"] if receipt else None
        self.events.emit("seller.closed", {"outcome": outcome, "price": price, "cost": cost,
                                           "turns": st["turns"] if st else 0, "claim": d.get("claim"),
                                           "warranty_hours": d.get("warranty_hours"), "cert": d.get("cert"),
                                           "below_cost": bool(price is not None and cost is not None and price < cost),
                                           "sku": receipt["item"] if receipt else None}, task=task_id,
                         **({"unit": receipt["unit"]} if receipt else {}))
        return {"state": outcome, "say": say, "receipt": receipt}

    def cancel(self, task_id):
        st = self.open.get(task_id)
        if st is not None:
            self._close(task_id, st, "canceled", "")
