"""Our restock, the kit's hook (Round 3): "restock": {"cmd": "py restock.py --state {state}"}.

The kit runs this every 60 s and reads the last JSON line {"buy": [{"sku", "qty"}, ...]}. The model is asked
only when something changed (stock, units about to expire, our recent sales, the balance band, the strategy)
or REASK_MINUTES passed; otherwise it buys nothing without a model call. The model looks things up and
ends with act.buy, which runs check.buy (cash floor, stock cap, units per SKU, food share).

Bankruptcy (HARNESS.md 6, user 2026-11-03): with a restock hook the kit files only when the hook asks, so
this files ("bankrupt": true) when the shop is stuck: below the kit's bankrupt line, filings left, and no
unit left to sell. Code decides it, not the model, and an urgent alert tells the user.
"""
import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from agents import loop  # noqa: E402
from core import card  # noqa: E402
from core import atomic, bus, paths  # noqa: E402
from core.events import EventWriter  # noqa: E402
from tools import market_files as mf  # noqa: E402
from tools.registry import Ctx, call  # noqa: E402

REASK_MINUTES = 20
DEADLINE = 150             # the kit starts the next restock only after this one ended (and stops it at 300 s)


def stuck(inv):
    live = [u for u in inv.get("units") or [] if u.get("grade") != "expired"]
    return ((inv.get("balance") or 0) < (inv.get("bankrupt_line") or 0) and (inv.get("bankruptcies_left") or 0) > 0
            and not live)


def signature(ctx):
    inv = call(ctx, "me.inventory", {})["result"]
    sold = call(ctx, "market.selling", {"hours": 2})["result"]["our_deals"]
    return {"units": inv["units"], "expiring": len(inv["expiring_12h"]), "sold_2h": sold,
            "balance_band": (inv["balance"] or 0) // 200_000, "strategy": ctx.strategy.get("version")}, inv


def situation(ctx, inv):
    lim = ctx.strategy.get("limits") or {}
    start = inv.get("start") or 5_000_000
    sell = call(ctx, "market.selling", {"hours": 6})["result"]
    held = {}
    for u in mf.inventory(ctx).get("units", []):
        if u.get("grade") != "expired":
            c = mf.catalog(ctx).get(u["sku"]) or {}
            held[c.get("category") or u["sku"]] = held.get(c.get("category") or u["sku"], 0) + 1
    lines = ["# the decision at hand",
             f"Phase {ctx.strategy.get('phase')}. Balance {inv['balance']} (floor {lim.get('cash_floor')}); stock "
             f"{inv['units']} units worth {inv['stock_value']} (cap {round((lim.get('max_stock_share') or 0) * start)}); "
             f"food share {inv['food_share']} (max {lim.get('food_share_max')}); at most {lim.get('max_units_per_sku')} "
             f"per SKU. Expiring within 12 h: {inv['expiring_12h'][:8]}.",
             f"Selling in the last 6 h: ours {sell['ours'] or 'none'}; market {sell['market'] or sell['note']}.",
             "Categories we hold now (units): " + (", ".join(f"{k.split('>')[-1]} {n}" for k, n in held.items()) or "none"),
             "Wanted posts on the forum (marked excerpts): " + (" | ".join(
                 p["excerpt"] for q in ("구합니다", "wanted", "looking for")
                 for p in call(ctx, "market.forum", {"query": q, "limit": 4})["result"]["posts"]) or "none"),
             "", "Demand categories (categories orders may come from, not what sells; item, kind, median list price, "
             "SKUs in the catalog):"]
    for d in mf.read(ctx, "demand.json", default=[]) or []:
        lines.append(f"- {d.get('item')} ({d.get('kind')}, {d.get('median_price')}, {d.get('skus')})")
    lines.append("\nFind SKUs with market.sku (a category word or a name), then act.buy a plan (or an empty plan).")
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
        sig, inv = signature(ctx)
        state_path = paths.memory() / "facts" / "restock_state.json"
        last = atomic.read_json(state_path, default=None) or {}
        if last.get("sig") == sig and t0 - last.get("ts", 0) < REASK_MINUTES * 60:
            w.emit("restock.plan", {"lines": 0, "refused": None, "used_model": False})
            print(json.dumps({"buy": []}))
            return
        d, trace = loop.decide("restock", ctx, situation(ctx, inv), deadline=t0 + DEADLINE)
        plan = d["plan"] if d else []
        if d is not None:                               # a failed turn is tried again next time
            atomic.write_json(state_path, {"sig": sig, "ts": t0})
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
