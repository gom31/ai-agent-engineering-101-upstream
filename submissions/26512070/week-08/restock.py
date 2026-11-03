"""Our restock, the kit's hook (Round 3): "restock": {"cmd": "py restock.py --state {state}"}.

The kit runs this every 60 s and reads the last JSON line {"buy": [{"sku", "qty"}, ...]}. Supply opens once every
20 minutes for 3 minutes (agent.md, 11/3; inventory.json "restock": open, opens_at, closes_at), and stock bought in
a window has to last until the next one. So the model is asked once per window: when the window is open or opens
within LEAD seconds (the kit waits up to 90 s per line for it). At other times nothing is bought and no model is
called. Without window information (an older market) it falls back to asking when something changed. The model
looks things up and ends with act.buy, which runs check.buy (cash floor, stock cap, units per SKU, food share).

Bankruptcy (HARNESS.md 6, user 2026-11-03): with a restock hook the kit files only when the hook asks, so
this files ("bankrupt": true) when the shop is stuck: below the kit's bankrupt line, filings left, and no
unit left to sell. Code decides it, not the model, and an urgent alert tells the user.
"""
import argparse
import json
import math
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from agents import loop  # noqa: E402
from core import card  # noqa: E402
from core import atomic, bus, paths  # noqa: E402
from core.events import EventWriter  # noqa: E402
from tools import check  # noqa: E402
from tools import market_files as mf  # noqa: E402
from tools.registry import Ctx, call  # noqa: E402

REASK_MINUTES = 20
LEAD = 75                  # plan this many seconds before a window opens at the latest (a model turn takes ~40 s)
WINDOW_MINUTES = 3
PLANS_PER_WINDOW = 4       # a plan of 10 lines takes the kit ~100 s (one supply call per 10 s); windows run 10 min (seen 11/3)
CANDIDATES = 40            # goods SKUs offered in the situation, one per demand category, so a plan needs no search
DEADLINE = 150             # the kit starts the next restock only after this one ended (and stops it at 300 s)


WINDOWS_PER_6H = 18        # supply opens about every 20 minutes
BUY_LIST_FRESH = 1200      # seconds a buy list the outer loop wrote stays good


def playbook(ctx):
    """User 11/4 02:0x (HARNESS 14): restock buys a list fixed in advance; the strategy's playbook sets its numbers
    and the strategy agent keeps changing them (delegated)."""
    pb = ((ctx.strategy.get("playbook") or {}).get("restock") or {})
    return {"mode": pb.get("mode", "rules"), "cover_windows": pb.get("cover_windows", 2),
            "market_top": pb.get("market_top", 5), "market_units": pb.get("market_units", 2),
            "max_line_share": pb.get("max_line_share", 1.0)}


def build_buy_list(ctx, now=None):
    """The buy list: our missed requests and our sales (last 6 h) turned into units per supply window, times
    cover_windows, then the market's best-selling goods we do not hold (market_units each). Most wanted first, so
    act.buy's trimming drops the least wanted. Written to memory/facts/buy_list.json for the outer side to read."""
    now = now or time.time()
    pb, lim = playbook(ctx), check._limits(ctx)
    cat = mf.catalog(ctx)
    have = {}
    for u in mf.inventory(ctx).get("units", []):
        if u.get("grade") != "expired":
            have[u["sku"]] = have.get(u["sku"], 0) + 1
    missed = call(ctx, "me.missed", {"hours": 6})
    sell = call(ctx, "market.selling", {"hours": 6})
    want = {}
    for x in (missed["result"].get("skus") or []) if missed["ok"] else []:
        if x.get("kind") == "goods":
            want[x["sku"]] = want.get(x["sku"], 0) + x.get("asked", 0)
    for x in (sell["result"].get("ours") or []) if sell["ok"] else []:
        if (cat.get(x["sku"]) or {}).get("kind") == "goods":
            want[x["sku"]] = want.get(x["sku"], 0) + x.get("sold", 0)
    lines = []
    for sku, n in sorted(want.items(), key=lambda kv: -kv[1]):
        if sku not in cat:
            continue
        target = max(1, min(lim["max_units_per_sku"], math.ceil(pb["cover_windows"] * n / WINDOWS_PER_6H)))
        lines.append({"sku": sku, "target": target, "have": have.get(sku, 0), "why": f"wanted {n} in 6 h"})
    market = [x for x in ((sell["result"].get("market") or []) if sell["ok"] else [])
              if x.get("kind") == "goods" and x["sku"] in cat and x["sku"] not in want]
    for x in market[:pb["market_top"]]:
        lines.append({"sku": x["sku"], "target": min(lim["max_units_per_sku"], pb["market_units"]),
                      "have": have.get(x["sku"], 0), "why": f"market sold {x.get('sold')} in 6 h"})
    # User 11/4 02:1x: plan within the money we may spend - each line gets what the cash and the stock cap allow
    from tools import act
    room = check.room(ctx)
    money = min(max(0, room["cash"] or 0), max(0, room["stock"]))
    cap = pb["max_line_share"] * money                   # one line may take at most this much of the window's money
    wants = [(x["sku"], max(0, min(x["target"] - x["have"],
                                   int(cap // room["worst"](x["sku"])) if room["worst"](x["sku"]) else 0) if pb["max_line_share"] < 1
                                else x["target"] - x["have"])) for x in lines]
    got = act.allocate(room, wants)
    for x, (_, q, why) in zip(lines, got):
        x["buy"], x["short"] = q, (why if q < x["target"] - x["have"] else None)
    doc = {"ts": now, "playbook": pb, "lines": lines,
           "budget": {"cash": room["cash"], "stock": round(room["stock"]), "balance": room["balance"],
                      "floor": room["floor"], "reserve": room["reserve"],
                      "planned": sum(q * room["worst"](sku) for sku, q, _ in got)}}
    atomic.write_json(paths.memory() / "facts" / "buy_list.json", doc)
    return doc


def rules_plan(ctx, now=None):
    """(plan, why, list): the buy list planned now (fresh stock and money), within the room. why: None when there
    is something to buy, 'full' when every line has its target, 'no_budget' when the money or the cap stops all."""
    doc = build_buy_list(ctx, now)
    plan = [{"sku": x["sku"], "qty": min(20, x["buy"])} for x in doc["lines"] if x["buy"] > 0][:10]
    if plan:
        check.buy(ctx, plan)
        return plan, None, doc
    lacking = [x for x in doc["lines"] if x["target"] > x["have"]]
    return [], ("no_budget" if lacking else "full"), doc


def stuck(inv):
    live = [u for u in inv.get("units") or [] if u.get("grade") != "expired"]
    return ((inv.get("balance") or 0) < (inv.get("bankrupt_line") or 0) and (inv.get("bankruptcies_left") or 0) > 0
            and not live)


def window(inv_raw, now):
    """(state, minute the window opens) from inventory.json's restock: ('open'|'soon'|'closed'|None, minute|None)."""
    r = inv_raw.get("restock") if isinstance(inv_raw.get("restock"), dict) else None
    if not r:
        return None, None
    if r.get("open"):
        start = (r.get("closes_at") or now) - WINDOW_MINUTES * 60
        return "open", int(start // 60)
    opens = r.get("opens_at")
    if opens and 0 <= opens - now <= LEAD:
        return "soon", int(opens // 60)
    return "closed", int(opens // 60) if opens else None


def signature(ctx):
    inv = call(ctx, "me.inventory", {})["result"]
    sold = call(ctx, "market.selling", {"hours": 2})["result"]["our_deals"]
    return {"units": inv["units"], "expiring": len(inv["expiring_12h"]), "sold_2h": sold,
            "balance_band": (inv["balance"] or 0) // 200_000, "strategy": ctx.strategy.get("version")}, inv


def candidates(ctx, held_skus, room, first=(), asked=()):
    """One goods SKU per goods demand category (nearest the category's median list price, and at most a tenth of the
    room so one line cannot eat it): categories named in `first` first, then the ones with the most SKUs."""
    cat = mf.catalog(ctx)
    by = {}
    for s, c in cat.items():
        if c.get("kind") == "goods" and s not in held_skus:
            by.setdefault(c.get("category"), []).append(c)
    rows = []
    for d in mf.read(ctx, "demand.json", default=[]) or []:
        if d.get("kind") != "goods" or d.get("category") not in by:
            continue
        cap = max(room / 10, 1) / 0.75                    # worst-case cost is 75% of list
        fit = [c for c in by[d["category"]] if c.get("list_price") and c["list_price"] <= cap]
        if not fit:
            continue
        med = d.get("median_price") or 0
        c = min(fit, key=lambda c: abs(c["list_price"] - med))
        rows.append({"sku": c["sku"], "name": (c.get("name") or "")[:40], "category": d.get("item"),
                     "list_price": c["list_price"], "worst_cost": round(c["list_price"] * 0.75),
                     "first": any(f and f in (d.get("category") or "") for f in first), "skus": d.get("skus") or 0})
    rows.sort(key=lambda r: (not r["first"], -r["skus"]))     # asked-for first, then the categories with most SKUs
    per = (ctx.strategy.get("restock") or {}).get("per_asked_category")
    if per:
        # User 11/3 22:06: buy what buyers need, for more deals. An order takes only certain SKUs of its category, so
        # the SKUs buyers asked us for come first (most asked first, held or not), then more SKUs of the asked-for
        # categories near the category's median price (cheapest-first picked sprays and gum from noisy categories);
        # categories nobody asked for are left out.
        demand = {x.get("item"): x for x in mf.read(ctx, "demand.json", default=[]) or []}
        top = [dict(sku=s, name=(cat[s].get("name") or "")[:40], category=(cat[s].get("category") or "").split(">")[-1],
                    list_price=cat[s].get("list_price"), worst_cost=round((cat[s].get("list_price") or 0) * 0.75),
                    first=True, skus=0)
               for s in asked if s in cat and cat[s].get("kind") == "goods"]
        extra = []
        for r in rows:
            if not r["first"]:
                continue
            d = demand.get(r["category"]) or {}
            med = d.get("median_price") or r["list_price"]
            more = sorted((c for c in by.get(d.get("category"), []) if c.get("list_price")
                           and c["sku"] not in asked), key=lambda c: abs(c["list_price"] - med))[:per]
            extra += [dict(r, sku=c["sku"], name=(c.get("name") or "")[:40], list_price=c["list_price"],
                           worst_cost=round(c["list_price"] * 0.75)) for c in more]
        rows = top + extra
    return rows[:CANDIDATES]


def situation(ctx, inv):
    lim = ctx.strategy.get("limits") or {}
    start = inv.get("start") or 5_000_000
    sell = call(ctx, "market.selling", {"hours": 6})["result"]
    held, held_skus = {}, set()
    for u in mf.inventory(ctx).get("units", []):
        if u.get("grade") != "expired":
            c = mf.catalog(ctx).get(u["sku"]) or {}
            held[c.get("category") or u["sku"]] = held.get(c.get("category") or u["sku"], 0) + 1
            held_skus.add(u["sku"])
    lines = ["# the decision at hand",
             f"Phase {ctx.strategy.get('phase')}. Balance {inv['balance']} (floor {lim.get('cash_floor')}); stock "
             f"{inv['units']} units worth {inv['stock_value']} (cap {round((lim.get('max_stock_share') or 0) * start)}); "
             f"food share {inv['food_share']} (max {lim.get('food_share_max')}); at most {lim.get('max_units_per_sku')} "
             f"per SKU. Expiring within 12 h: {inv['expiring_12h'][:8]}.",
             f"Selling in the last 6 h: ours {sell['ours'] or 'none'}; market {sell['market'] or sell['note']}.",
             "Categories we hold now (units): " + (", ".join(f"{k.split('>')[-1]} {n}" for k, n in held.items()) or "none"),
             f"Asked at our shop but not in stock (last 6 h): {call(ctx, 'me.missed', {'hours': 6})['result']}",
             "Wanted posts on the forum (marked excerpts): " + (" | ".join(
                 p["excerpt"] for q in ("구합니다", "wanted", "looking for")
                 for p in call(ctx, "market.forum", {"query": q, "limit": 4})["result"]["posts"]) or "none"),
             "", "Demand categories (categories orders may come from, not what sells; item, kind, median list price, "
             "SKUs in the catalog):"]
    for d in mf.read(ctx, "demand.json", default=[]) or []:
        lines.append(f"- {d.get('item')} ({d.get('kind')}, {d.get('median_price')}, {d.get('skus')})")
    r = mf.inventory(ctx).get("restock") or {}
    if r:
        nxt = r.get("opens_at")
        lines.append(f"\nSupply window: {'open until ' + time.strftime('%H:%M:%S', time.localtime(r['closes_at'])) if r.get('open') and r.get('closes_at') else 'opens ' + (time.strftime('%H:%M:%S', time.localtime(nxt)) if nxt else 'soon')}. "
                     "Supply opens about every 20 minutes; what you buy now has to last until the next window.")
    wf, bc = ctx.strategy.get("welcome_food") or {}, lim.get("below_cost") or {}
    if wf:
        lines.append(f"Welcome Food (strategy): each window brings food for first-time buyers (list price up to "
                     f"{bc.get('max_list_price')}, discounted only while grade {bc.get('grades')} now). What arrives at "
                     "which grade is the market's draw; market.buyer_prefs shows which food categories buyers want.")
    start_cap = round((lim.get("max_stock_share") or 0) * start)
    reserve, open_total, open_n = check.warranty_reserve(ctx)
    room = max(0, min(start_cap - (inv["stock_value"] or 0),
                      (inv["balance"] or 0) - (lim.get("cash_floor") or 0) - reserve))
    missed = call(ctx, "me.missed", {"hours": 6})["result"]
    first = [c["category"] for c in missed.get("categories") or []]
    # what buyers asked us for, then (user 11/3 22:3x: hold the market's best sellers deep) what sells: ours, then the
    # market's goods
    cat = mf.catalog(ctx)
    asked = [s["sku"] for s in missed.get("skus") or [] if s.get("sku")]
    asked += [s["sku"] for s in (sell.get("ours") or []) + (sell.get("market") or [])
              if isinstance(s, dict) and (cat.get(s.get("sku")) or {}).get("kind") == "goods" and s["sku"] not in asked]
    rows = candidates(ctx, held_skus, room, first, asked)
    lines.append(f"\nRoom to stock now: about {room} (the stock cap {start_cap} minus stock {inv['stock_value']}, and "
                 f"the balance above the floor"
                 + (f" and the warranty reserve {reserve} for {open_n} sales ({open_total}) still in warranty"
                    if reserve else "")
                 + "; costs are drawn on arrival, at most 75% of list).")
    if rows:
        lines.append("Goods candidates, one per demand category (ones buyers asked us for first; sku, list price, "
                     "worst-case cost, category, name):")
        lines += [f"- {r['sku']} {r['list_price']} {r['worst_cost']} {r['category']}: {r['name']}" for r in rows]
    lines.append("\nPick from the candidates or find others with market.sku, then act.buy a plan of up to 10 lines "
                 "(or an empty plan). This window allows a few plans in a row. The kit buys one line about every "
                 "10 s, so a line of several units (up to the per-SKU limit) stocks more in the same window.")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state")
    a = ap.parse_args()
    if a.state:
        import os
        os.environ.setdefault("MARKET_DIR", a.state)
    t0 = time.time()
    tool_w, w = EventWriter("restock", source="tools"), EventWriter("restock", source="restock")
    ctx = Ctx(caller="restock", _writer=tool_w)
    try:
        w.context(strategy_version=ctx.strategy.get("version"), epoch=ctx.strategy.get("epoch"),
                  card_version=card.load()[0])
        inv_raw = mf.inventory(ctx)
        if stuck(inv_raw):
            bus.alert("restock", "urgent", "bankrupt", "below the bankrupt line with nothing to sell: filed for "
                      "bankruptcy (the money given counts against profit)",
                      {"balance": inv_raw.get("balance"), "left": inv_raw.get("bankruptcies_left")})
            w.emit("restock.plan", {"lines": 0, "refused": None, "used_model": False, "bankrupt": True})
            print(json.dumps({"buy": [], "bankrupt": True}))
            return
        stop = (ctx.strategy.get("restock") or {}).get("stop_after")
        if stop and t0 >= stop:
            # User 11/3: the board counts unsold stock as zero profit, so stock is sold down before the round ends
            w.emit("restock.plan", {"lines": 0, "refused": None, "used_model": False, "stopped": stop})
            print(json.dumps({"buy": []}))
            return
        sig, inv = signature(ctx)
        state_path = paths.memory() / "facts" / "restock_state.json"
        last = atomic.read_json(state_path, default=None) or {}
        win, minute = window(inv_raw, t0)
        if win is not None:                              # the market's supply windows decide when
            same = last.get("window") is not None and minute is not None and abs(minute - last["window"]) <= 2
            done = same and last.get("plans_in_window", 1) >= PLANS_PER_WINDOW
            if win == "closed" or done:
                w.emit("restock.plan", {"lines": 0, "refused": None, "used_model": False,
                                        "window": win, "window_done": done})
                print(json.dumps({"buy": []}))
                return
            sig["window"] = minute
        elif last.get("sig") == sig and t0 - last.get("ts", 0) < REASK_MINUTES * 60:
            w.emit("restock.plan", {"lines": 0, "refused": None, "used_model": False})
            print(json.dumps({"buy": []}))
            return
        if playbook(ctx)["mode"] == "rules" and win == "soon":
            # 11/4 02:02: a plan made in 'soon' sent its first line 4 s before the window opened (429); the rules
            # need no lead time, so they wait for 'open' without using up the window
            w.emit("restock.plan", {"lines": 0, "refused": None, "used_model": False, "rules": True, "window": win})
            print(json.dumps({"buy": []}))
            return
        if playbook(ctx)["mode"] == "rules":
            # HARNESS 14: buy the list at once, once per window; the agent only when the list gives nothing
            plan, why, doc = rules_plan(ctx, t0)
            atomic.write_json(state_path, {"sig": sig, "ts": t0, "window": minute, "plans_in_window": PLANS_PER_WINDOW})
            w.emit("restock.plan", {"lines": len(plan), "plan": plan, "refused": None, "used_model": False,
                                    "rules": True, "why": why, "budget": doc["budget"]})
            if why == "no_budget":                       # the strategy has to know: the money is tied up
                w.emit("exec.anomaly", {"role": "restock", "kind": "no_budget", "then": "nothing bought",
                                        "situation": {"budget": doc["budget"],
                                                      "lacking": [(x["sku"], x["target"] - x["have"]) for x in doc["lines"]
                                                                  if x["target"] > x["have"]][:8]}})
            if plan or doc["lines"]:                     # the agent only when there is no list at all
                print(json.dumps({"buy": plan}))
                return
            w.emit("exec.anomaly", {"role": "restock", "kind": "no_buy_list", "then": "agent", "situation": {}})
        d, trace = loop.decide("restock", ctx, situation(ctx, inv), deadline=t0 + DEADLINE)
        plan = d["plan"] if d else []
        if d is not None:                               # a failed turn is tried again next time
            n = (last.get("plans_in_window", 0) + 1) if (minute is not None and last.get("window") is not None
                                                          and abs(minute - last["window"]) <= 2) else 1
            atomic.write_json(state_path, {"sig": sig, "ts": t0, "window": minute, "plans_in_window": n})
        refused = next((t["observation"][:200] for t in trace if "refused" in str(t["observation"])), None)
        w.emit("restock.plan", {"lines": len(plan), "plan": plan, "refused": refused, "used_model": True,
                                "why": (d or {}).get("why") or (d or {}).get("thought"),
                                "guard": None if d else "no valid plan from the model"})
        print(json.dumps({"buy": plan}))
    finally:
        w.close()
        tool_w.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"restock crashed: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        print(json.dumps({"buy": []}))
