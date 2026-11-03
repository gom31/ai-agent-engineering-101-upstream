"""Our forum agent: one turn of reading the forum, voting, and posting at most once.

    py forum.py        # the outer loop runs it (build step 5); the kit has no forum hook

The kit reads .market/forum.json from the market and sends .market/outbox.jsonl (act.post, act.vote write it).
Posts may only cite ledger entries we know (our paid deals in payments.jsonl, our settled sales).
"""
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from agents import loop  # noqa: E402
from core import card  # noqa: E402
from core import atomic  # noqa: E402
from core.events import EventWriter  # noqa: E402
from tools.evaluate import events  # noqa: E402
from tools.registry import Ctx, call  # noqa: E402

DEADLINE = 120


def _lines(path):
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def known_ledger(ctx, split=False):
    bought = {p["ledger"] for p in _lines(ctx.market_dir / "payments.jsonl")
              if p.get("paid") and isinstance(p.get("ledger"), int)}
    sold = {e["data"]["ledger"] for e in events(["seller.settled"]) if isinstance(e["data"].get("ledger"), int)}
    if split:
        return sorted(bought), sorted(sold)
    return sorted(bought | sold)


def _show(v):
    return "null" if v is None else ("true" if v is True else "false" if v is False else str(v))


def settle_line(row):
    """One ledger settle as the posts that get votes quote it (user 11/3): every field as the ledger has it."""
    d = row["data"]
    return (f"ledger #{row['seq']} settle: order {d.get('order')}, buyer {d.get('buyer')}, shop {d.get('shop')}, "
            f"item {d.get('item')}, amount {d.get('amount')}, unit {d.get('unit')}, name {d.get('name')}, "
            f"grade {_show(d.get('grade'))}, claim {_show(d.get('claim'))}, "
            f"claim_honest {_show(d.get('claim_honest'))}, warranty_hours {_show(d.get('warranty_hours'))}")


def settles(seqs):
    """The settle lines for our ledger entries, from the ledger the outer runtime collects."""
    want, out = set(seqs), {}
    for f in sorted((HERE / "memory" / "market").glob("ledger-*.jsonl")):
        for row in _lines(f):
            if row.get("seq") in want and row.get("kind") == "settle" and isinstance(row.get("data"), dict):
                out[row["seq"]] = settle_line(row)
    return out


def own_posts(ctx):
    """Our post ids and how many we wrote per side. The market answers a post with {"post": <id>} (seen 11/3)."""
    ids, sides = [], {"shop": 0, "buyer": 0}
    for x in _lines(ctx.market_dir / "outbox.sent.jsonl"):
        sent = x.get("sent") or {}
        if "post" not in sent:
            continue
        try:
            ans = json.loads(x.get("answer") or "{}")
        except ValueError:
            continue
        pid = ans.get("post", ans.get("id")) if isinstance(ans, dict) else None
        if pid is not None:
            ids.append(pid)
            sides[sent.get("as")] = sides.get(sent.get("as"), 0) + 1
    return ids, sides


def next_side(sides, buyer_refs, shop_refs):
    """The side with fewer posts writes next (both count for ALL), if that side has a settle of its own to cite."""
    want = "buyer" if sides.get("buyer", 0) <= sides.get("shop", 0) else "shop"
    have = {"buyer": buyer_refs, "shop": shop_refs}
    if have[want]:
        return want
    other = "shop" if want == "buyer" else "buyer"
    return other if have[other] else None


def main():
    tool_w, w = EventWriter("forum", source="tools"), EventWriter("forum", source="forum")
    ctx = Ctx(caller="forum", _writer=tool_w)
    try:
        w.context(strategy_version=ctx.strategy.get("version"), epoch=ctx.strategy.get("epoch"),
                  card_version=card.load()[0])
        bought, sold = known_ledger(ctx, split=True)
        refs = sorted(set(bought) | set(sold))
        mine, sides = own_posts(ctx)
        lines = settles(refs)
        side = next_side(sides, [s for s in bought if s in lines], [s for s in sold if s in lines])
        ctx.situation = {"known_ledger": refs, "own_posts": mine, "settles": lines, "post_as": side,
                         "side_refs": {"buyer": bought, "shop": sold}}
        posts = call(ctx, "market.forum", {"limit": 12})["result"]
        wf = ctx.strategy.get("welcome_food") or {}
        bc = (ctx.strategy.get("limits") or {}).get("below_cost") or {}
        inv = call(ctx, "me.inventory", {"detail": "full"})
        food = [u for u in (inv["result"].get("unit_list") or []) if u.get("kind") == "food"] if inv["ok"] else []
        r = (atomic.read_json(ctx.market_dir / "inventory.json", default={}) or {}).get("restock") or {}
        nxt = time.strftime("%H:%M", time.localtime(r["opens_at"])) if r.get("opens_at") else "unknown"
        mark_of = {"buyer": "our buyer bought", "shop": "our shop sold"}
        mark = {s: mark_of["buyer"] for s in bought} | {s: mark_of["shop"] for s in sold}
        text = ("# the decision at hand\n"
                f"Our own posts: {mine[-10:] or 'none'} (as shop {sides.get('shop', 0)}, as buyer "
                f"{sides.get('buyer', 0)}). "
                + (f"Post this turn as {side}, citing a settle {mark_of[side]}.\n"
                   if side else "No settle line to cite yet: vote, then act.skip.\n")
                + "Settle lines of our deals we may cite (newest last):\n"
                + ("\n".join(f"- ({mark.get(s)}) {lines[s]}" for s in sorted(lines)[-12:]) or "- none yet") + "\n"
                "A post states one checked fact from one settle line, in this shape: 'Settled SKU=<item> "
                "price=<amount>; grade=<grade>; warranty_hours=<warranty_hours>. One observed purchase, not a "
                "guarantee of future quality or availability.' Use only values the line shows; refs = that line's "
                "number. act.post appends the line itself. No announcements or offers without a settle line.\n"
                + (f"Welcome Food (strategy): fresh food each supply window (about every 20 min; next opens "
                   f"{nxt}), food up to list price {bc.get('max_list_price')} discounted while grade {bc.get('grades')}, "
                   f"first visit at {wf.get('share_of_cost')} of cost; food units we hold now: {len(food)}.\n" if wf else "")
                + "Newest forum posts (marked excerpts):\n"
                + "\n".join(f"- #{p['id']} {p.get('author')} ({p.get('role')}), score {p.get('score')}: {p['excerpt']}"
                            for p in posts["posts"])
                + "\n\nVote on posts that are true and useful (act.vote), then post one useful true fact with refs "
                  "(act.post) or end with act.skip.")
        d, trace = loop.decide("forum", ctx, text, deadline=time.time() + DEADLINE)
        votes = sum(1 for t in trace if '"act.vote"' in str(t.get("answer")) and '"voted"' in str(t["observation"]))
        w.emit("forum.turn", {"action": (d or {}).get("action"), "as": (d or {}).get("as"),
                              "refs": (d or {}).get("refs"), "votes": votes,
                              "why": (d or {}).get("why") or (d or {}).get("thought")})
        if d and d.get("action") == "post":
            w.emit("forum.posted", {"as": d["as"], "refs_valid": True, "post_id": None})   # the id comes back on the
            # market's answer in outbox.sent.jsonl; votes are counted by side from forum.json (forum.votes "as")
        print(json.dumps({"forum": (d or {}).get("action"), "votes": votes}))
    finally:
        w.close()
        tool_w.close()


if __name__ == "__main__":
    main()
