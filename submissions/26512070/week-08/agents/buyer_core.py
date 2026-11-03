"""Our buyer for one order, without the kit (market_buyer.py is the kit's hook and calls this).

What code does (deterministic): fold the kit's payment results into the reflex facts, drop blocked shops and
shops with nothing this order accepts, speak A2A, read shops' messages through calc.extract (S1), check the
receipt against what we agreed (check.receipt), and record events and the outcome. What the model decides
(agents.loop, role buyer): which shop and SKU, every offer, when to walk away, and the accept policy the
kit checks before it pays.

A receipt above what we agreed never goes out: the kit pays any signed receipt within budget, so a
cheating receipt is passed on as null and the shop is blocked (reflex).
"""
import json
import time
import uuid

from agents import loop
from core import atomic, paths
from core import card as cardmod
from core import reflex
from core.events import EventWriter
from tools import market_files as mf
from tools.calc import safe_name
from tools.registry import Ctx, call

DEADLINE = 270             # the kit stops market_buyer after 300 s
MAX_TURNS = 10
REVIEWS_SHOWN = 2
HEADERS = {"A2A-Version": "1.0"}
PRICE_FACT_EVERY = 600     # seconds between rebuilds of memory/facts/shop_prices.json


def shop_prices(shops, now=None):
    """User 11/3 23:4x: surplus first, at the shops that sell cheap. Each shop's median paid share of list price over
    the market's settles (the last two ledger days), keyed by shop URL; rebuilt from the ledger every 10 minutes."""
    now = now or time.time()
    path = paths.memory() / "facts" / "shop_prices.json"
    fact = atomic.read_json(path, default=None) or {}
    if now - fact.get("ts", 0) > PRICE_FACT_EVERY:
        cat = {}
        try:
            cat = json.loads((paths.root() / ".market" / "catalog.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        shares = {}
        for f in sorted((paths.memory() / "market").glob("ledger-*.jsonl"))[-2:]:
            for line in open(f, encoding="utf-8"):
                if '"kind": "settle"' not in line:
                    continue
                try:
                    x = json.loads(line)["data"]
                except (ValueError, KeyError):
                    continue
                lp = (cat.get(x.get("sku") or x.get("item")) or {}).get("list_price")
                if lp and x.get("amount") is not None and x.get("shop"):
                    shares.setdefault(x["shop"].rstrip("/"), []).append(x["amount"] / lp)
        fact = {"ts": now, "shops": {u: {"median_share": round(sorted(v)[len(v) // 2], 2), "deals": len(v)}
                                     for u, v in shares.items()}}
        atomic.write_json(path, fact)
    return {s["name"]: fact["shops"].get((s.get("url") or "").rstrip("/")) for s in shops}


class A2A:
    """The A2A v1.0 client: read the card, keep one task, send text."""

    def __init__(self, base, timeout=120):
        import httpx
        self.httpx, self.timeout = httpx, timeout
        card = httpx.get(base.rstrip("/") + "/.well-known/agent-card.json", headers=HEADERS, timeout=30).json()
        iface = next((i for i in card.get("supportedInterfaces", [])
                      if i.get("protocolBinding") == "JSONRPC" and str(i.get("protocolVersion")) == "1.0"), None)
        if iface is None:
            raise RuntimeError("no JSONRPC 1.0 interface in the shop's card")
        self.url, self.card, self.task_id = iface["url"], card, None

    def _rpc(self, method, params, timeout):
        body = {"jsonrpc": "2.0", "id": str(uuid.uuid4())[:8], "method": method, "params": params}
        r = self.httpx.post(self.url, json=body, headers=HEADERS, timeout=timeout).json()
        if "error" in r:
            raise RuntimeError(f"{method}: {r['error'].get('message')}")
        return r["result"].get("task", r["result"])

    def send(self, text, timeout=None):
        msg = {"role": "ROLE_USER", "messageId": str(uuid.uuid4()), "parts": [{"text": text}]}
        if self.task_id:
            msg["taskId"] = self.task_id
        t = self._rpc("SendMessage", {"message": msg}, timeout or self.timeout)
        self.task_id = t.get("id", self.task_id)
        return reply(t)

    def cancel(self):
        if self.task_id:
            try:
                self._rpc("CancelTask", {"id": self.task_id}, 30)
            except Exception:
                pass


def reply(t):
    """(state, text, receipt) from an A2A task."""
    state = (t.get("status") or {}).get("state", "")
    text = " ".join(p.get("text", "") for p in ((t.get("status") or {}).get("message") or {}).get("parts", []))
    receipt = None
    for a in t.get("artifacts") or []:
        for p in a.get("parts") or []:
            if isinstance(p.get("data"), dict) and "price" in p["data"]:
                receipt = dict(p["data"])
    if receipt and isinstance(receipt.get("price"), float) and receipt["price"].is_integer():
        receipt["price"] = int(receipt["price"])
    if receipt and "seller" in receipt and "shop" not in receipt:
        receipt["shop"] = receipt.pop("seller")
    return state, text, receipt


class BuyerCore:
    def __init__(self, order, shops, model_call=None, connect=A2A, clock=time.time):
        self.order, self.shops_in, self.model_call, self.connect, self.clock = order, shops, model_call, connect, clock
        if isinstance(order.get("budget"), float) and order["budget"].is_integer():
            order["budget"] = int(order["budget"])
        self.t0 = clock()
        self.events = EventWriter("buyer", source="buyer")
        self.tool_events = EventWriter("buyer", source="tools")
        self.ctx = Ctx(caller="buyer", clock=clock, _writer=self.tool_events)

    def deadline(self):
        return self.t0 + DEADLINE

    def close(self):
        self.events.close()
        self.tool_events.close()

    # --- the order

    def run(self):
        """The result line for the kit: {"shop", "receipt", "stars", "review", "accept"?}."""
        o, ctx = self.order, self.ctx
        strategy = ctx.strategy
        self.events.context(strategy_version=strategy.get("version"), epoch=strategy.get("epoch"),
                            card_version=cardmod.load()[0])
        reflex.read_payments(ctx.market_dir, ctx.now())
        cat = mf.catalog(ctx)
        acceptable = o.get("acceptable") or []
        kind = o.get("kind") or next((cat[s].get("kind") for s in acceptable if s in cat), None)
        self.events.emit("buyer.order", {"category": o.get("item"), "budget": o.get("budget"), "kind": kind,
                                         "use_within_hours": o.get("use_within_hours"),
                                         "acceptable_n": len(acceptable)}, order=o.get("order"))
        blocked = reflex.blocked()
        shops = {}
        for s in self.shops_in:
            skus = [x["sku"] for x in s.get("offers") or [] if x.get("qty") and (not acceptable or x["sku"] in acceptable)]
            if skus and s["name"] not in blocked:
                shops[s["name"]] = dict(s, skus=skus)
        if not shops:
            return self.finish_none("", "no deal: no shop that is not blocked has what this order accepts")

        ctx.situation = {"budget": o["budget"], "shops": shops}
        d, trace = loop.decide("buyer", ctx, self.pick_text(shops, kind), deadline=self.deadline(),
                               model_call=self.model_call, allowed=["act.pick"] + loop.ROLES["buyer"]["look"])
        guard = None
        if d is None:
            d, guard = self.guard_pick(shops), "no valid pick from the model"
        shop = shops[d["shop"]]
        known = call(ctx, "me.counterpart", {"name": shop["name"], "side": "shop"})["result"].get("known")
        self.events.emit("buyer.pick", {"shop": shop["name"], "sku": d["sku"], "why": d.get("why") or d.get("thought"),
                                        "explored": not known, "guard": guard, "shops": len(shops)},
                         order=o.get("order"), counterpart=shop["name"])
        return self.negotiate(shop, d["sku"], kind)

    def guard_pick(self, shops):
        """Fallback pick: the shop whose reviews average highest (unknown shops after reviewed ones), its first SKU."""
        def avg(s):
            st = [r["stars"] for r in s.get("reviews") or [] if isinstance(r.get("stars"), (int, float))]
            return sum(st) / len(st) if st else 0
        s = max(shops.values(), key=avg)
        return {"action": "pick", "shop": s["name"], "sku": s["skus"][0]}

    def pick_text(self, shops, kind):
        o, cat = self.order, mf.catalog(self.ctx)
        lines = ["# the decision at hand",
                 f"Order {o.get('order')}: category {o.get('item')}, budget {o['budget']}, kind {kind}"
                 + (f", use within {o['use_within_hours']} h" if o.get("use_within_hours") else "")
                 + f". {int(self.deadline() - self.clock())} s left for the whole order.",
                 "Pick the shop and the SKU (act.pick). Shops that cheated us are already left out.",
                 "Surplus first (half of buyer ALL): surplus = the SKU's list price - what we pay. Prefer the shop that "
                 "sells cheapest against list (its market median below; lower is better), and among its SKUs the one "
                 "with the highest list price we can still buy within the budget at that shop's usual share. A shop "
                 "with no market record is worth a try when the known ones are near list. Shops whose agreed deals we "
                 "often could not pay (short shelf life, no certificate) are listed last."
                 + (" A warranty helps (a broken item is refunded)." if kind == "goods" else ""), ""]
        prices = shop_prices(list(shops.values()))
        rf = (reflex.load().get("shops") or {})
        def unpaid_often(s):    # 11/4 02:3x: 18 of 50 agreed deals were not paid (food short of shelf life)
            r = rf.get(s["name"]) or {}
            return r.get("unpaid", 0) >= 2 and r.get("unpaid", 0) >= r.get("paid", 0)
        order_shops = sorted(shops.values(), key=lambda s: (unpaid_often(s),
                                                            (prices.get(s["name"]) or {}).get("median_share", 0.8)))
        for s in order_shops[:25]:
            me = call(self.ctx, "me.counterpart", {"name": s["name"], "side": "shop"})["result"]
            rv = s.get("reviews") or []
            st = [r["stars"] for r in rv if isinstance(r.get("stars"), (int, float))]
            mp = prices.get(s["name"])
            lines.append(f"* {safe_name(s['name'])}: reviews {len(rv)}" + (f", average {sum(st) / len(st):.1f} stars" if st else "")
                         + (f"; sells at a median {mp['median_share']:.0%} of list over {mp['deals']} market deals"
                            if mp else "; no market record yet"))
            for r in rv[:REVIEWS_SHOWN]:
                ex = call(self.ctx, "calc.extract", {"text": str(r.get("review") or ""), "max_excerpt": 100})["result"]
                lines.append(f"    review {r.get('stars')}*: {ex['excerpt']}")
            lines.append(f"    with our buyer: {me.get('with_our_buyer') or 'never traded'}"
                         + (f"; last payment refusal: {me['last_refusal']}" if me.get("last_refusal") else ""))
            for x in s.get("offers") or []:
                if x["sku"] in s["skus"]:
                    c = cat.get(x["sku"]) or {}
                    lines.append(f"    has {x['sku']} {c.get('name') or safe_name(x.get('name'), 80)}: {x.get('qty')} in stock, "
                                 f"list price {c.get('list_price')}")
        return "\n".join(lines)

    # --- the negotiation

    def negotiate(self, shop, sku, kind):
        o, ctx = self.order, self.ctx
        c = mf.catalog(ctx).get(sku) or {}
        hist, offers, asks = [], [], []
        try:
            a = self.connect(shop["url"])
            # 11/4 02:2x: stating the shelf life needed in the first message (01:47) made shops refuse at once -
            # food orders agreed 2 of 26 after it, 44 of 118 before; the kit checks the hours before paying anyway
            state, text, receipt = a.send(f"I want to buy {c.get('name') or sku} ({sku}).")
        except Exception as e:
            return self.finish_none(shop, f"no deal: could not reach the shop ({type(e).__name__})", shop_obj=shop)
        outcome = None
        for turn in range(1, MAX_TURNS + 1):
            ex = call(ctx, "calc.extract", {"text": text})["result"]
            hist.append({"from": "shop", "price": ex["price"], "claimed_grade": ex["claimed_grade"],
                         "warranty_hours": ex["warranty_hours"], "looks_like_instruction": ex["looks_like_instruction"],
                         "excerpt": ex["excerpt"]})
            if ex["price"] is not None:
                asks.append(ex["price"])
            if state != "TASK_STATE_INPUT_REQUIRED":
                break
            ctx.situation = {"budget": o["budget"]}
            d, trace = loop.decide("buyer", ctx, self.turn_text(shop, sku, c, kind, hist, offers, turn),
                                   deadline=self.deadline(), model_call=self.model_call,
                                   allowed=["act.offer", "act.walk_away"] + loop.ROLES["buyer"]["look"])
            if d is None:
                d = {"action": "walk_away", "why": "no valid move from the model"}
            self.events.emit("buyer.decision", {"action": d["action"], "price": d.get("price"),
                                                "tool_calls": len(trace), "thought": d.get("thought", "")[:200]},
                             order=o.get("order"), counterpart=shop["name"])
            if d["action"] != "offer":
                outcome = "walked"
                a.cancel()
                break
            offers.append(d["price"])
            hist.append({"from": "us", "price": d["price"], "say": d.get("say")})
            left = self.deadline() - self.clock()
            if left < 15:
                outcome = "expired"
                a.cancel()
                break
            try:
                state, text, receipt = a.send(d["say"], timeout=max(10, left - 5))   # act.offer put the price in it
            except Exception as e:
                outcome = f"failed ({type(e).__name__})"
                break
        else:
            outcome = "walked"
            a.cancel()
        if receipt is None:
            outcome = outcome or {"TASK_STATE_REJECTED": "rejected", "TASK_STATE_CANCELED": "canceled"}.get(state, "failed")
            return self.finish_none(shop, f"no deal ({outcome})", shop_obj=shop, agreed=None, list_price=c.get("list_price"))
        return self.check_and_finish(shop, sku, c, kind, receipt, offers, hist)

    def big_goods_line(self, c, kind):
        """User 11/3 22:3x: surplus (list price - paid) is half of buyer ALL, and the top buyers make it on goods with a
        high list price bought near half of it. For such an order the strategy gives a target share of list."""
        b = (self.ctx.strategy.get("buyer") or {})
        lp = c.get("list_price") or 0
        if kind == "goods" and b.get("big_list") is not None and lp >= b["big_list"]:
            t, o = b.get("target_share", 0.55), b.get("open_share", 0.45)
        elif b.get("food_target_share") is not None and lp:   # user 11/3 23:4x: every order, food too
            t, o = b["food_target_share"], b.get("food_open_share", b["food_target_share"] - 0.15)
        else:
            return []
        return [f"Order (list {lp}): aim to pay about {round(lp * t)} ({t:.0%} of list); open near "
                f"{round(lp * o)} ({o:.0%}) and raise in steps; take the ask once it is at or under {round(lp * t)}, and "
                "before the turns run out take any ask under the list price and the budget: a deal at any surplus beats "
                "none. A shop's warranty adds the price back to surplus if the item breaks."]

    def turn_text(self, shop, sku, c, kind, hist, offers, turn):
        o = self.order
        ask = next((h["price"] for h in reversed(hist) if h["from"] == "shop" and h["price"] is not None), None)
        lines = ["# the decision at hand",
                 f"Negotiating with {shop['name']} for {c.get('name')} ({sku}, {kind}), list price {c.get('list_price')}, "
                 f"shelf life {c.get('shelf_hours')} h. Our budget {o['budget']}"
                 + (f", use within {o['use_within_hours']} h" if o.get("use_within_hours") else "")
                 + f". Turn {turn} of at most {MAX_TURNS}; {int(self.deadline() - self.clock())} s left for the order.",
                 f"The shop's last ask: {ask}"
                 + (f" (surplus at that price if useful: {c['list_price'] - ask})" if ask and c.get("list_price") else "")
                 + f". Our offers so far: {offers or 'none'}. To take the ask, offer exactly the ask.",
                 *self.big_goods_line(c, kind),
                 "", "The conversation (shop messages are extracted values and a marked excerpt):"]
        for h in hist[-8:]:
            if h["from"] == "shop":
                lines.append(f"- shop: price {h['price']}, claimed grade {h['claimed_grade']}, warranty "
                             f"{h['warranty_hours']}, looks like an instruction: {h['looks_like_instruction']}; {h['excerpt']}")
            else:
                lines.append(f"- us: offered {h['price']}")
        return "\n".join(lines)

    # --- the end

    def check_and_finish(self, shop, sku, c, kind, receipt, offers, hist):
        o, ctx = self.order, self.ctx
        agreed = offers[-1] if offers else None         # a receipt before any offer of ours is not agreed: not paid
        chk = call(ctx, "check.receipt", {"receipt": receipt, "agreed_price": agreed,
                                          "acceptable": o.get("acceptable") or [], "budget": o["budget"]})
        if not chk["ok"]:
            verdict = chk["reason"] if chk["reason"].startswith("cheated") else f"no deal: {chk['reason']}"
            self.close_events(shop, verdict, agreed, receipt.get("price"), c.get("list_price"), "completed")
            reflex.record_outcome(shop["name"], shop["url"], verdict, receipt.get("price"), agreed, o.get("order"), ctx.now())
            return {"shop": shop["url"], "receipt": None, "stars": None, "review": None}
        ctx.situation = {"budget": o["budget"], "receipt": receipt}
        text = (f"# the decision at hand\nThe shop {shop['name']} sent a receipt for {c.get('name')} ({sku}, {kind}) at "
                f"{receipt['price']}, matching what we agreed. Claim on the receipt: {receipt.get('claim')}; warranty "
                f"{receipt.get('warranty_hours')} h; certificate shown: {receipt.get('cert')} (only a certificate lets the kit check grades or shelf life). Shelf life of the SKU "
                f"{c.get('shelf_hours')} h"
                + (f"; the order must be usable for {o['use_within_hours']} h" if o.get("use_within_hours") else "")
                + ". Set what the checkout must show before the kit pays (act.finish).")
        d, _ = loop.decide("buyer", ctx, text, deadline=self.deadline(), model_call=self.model_call,
                           allowed=["act.finish"] + loop.ROLES["buyer"]["look"])
        accept = d["accept"] if d else ({"min_hours_left": o["use_within_hours"]}
                                        if kind == "food" and o.get("use_within_hours") else {})
        self.close_events(shop, "fair", agreed, receipt["price"], c.get("list_price"), "completed", accept=accept,
                          guard=None if d else "no valid accept policy from the model")
        reflex.record_outcome(shop["name"], shop["url"], "fair", receipt["price"], agreed, o.get("order"), ctx.now())
        out = {"shop": shop["url"], "receipt": receipt, "stars": None, "review": None}
        if accept:
            out["accept"] = accept
        return out

    def finish_none(self, shop, verdict, shop_obj=None, agreed=None, list_price=None):
        name = shop_obj["name"] if shop_obj else ""
        self.close_events(shop_obj, verdict, agreed, None, list_price, verdict.split("(")[-1].rstrip(")") or "none")
        if shop_obj:
            reflex.record_outcome(name, shop_obj["url"], verdict, None, agreed, self.order.get("order"), self.ctx.now())
        return {"shop": shop_obj["url"] if shop_obj else "", "receipt": None, "stars": None, "review": None}

    def close_events(self, shop, verdict, agreed, receipt_price, list_price, outcome, accept=None, guard=None):
        self.events.emit("buyer.closed", {"outcome": outcome, "agreed": agreed, "receipt_price": receipt_price,
                                          "verdict": verdict, "list_price": list_price, "accept": accept, "guard": guard},
                         order=self.order.get("order"), **({"counterpart": shop["name"]} if shop else {}))


def load_inputs(shops_path, order_path):
    shops = json.loads(open(shops_path, encoding="utf-8").read())
    order = json.loads(open(order_path, encoding="utf-8").read()) if order_path else {}
    return order, shops
