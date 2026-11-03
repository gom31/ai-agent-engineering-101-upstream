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


def known_ledger(ctx):
    seqs = {p["ledger"] for p in _lines(ctx.market_dir / "payments.jsonl")
            if p.get("paid") and isinstance(p.get("ledger"), int)}
    seqs |= {e["data"]["ledger"] for e in events(["seller.settled"]) if isinstance(e["data"].get("ledger"), int)}
    return sorted(seqs)


def own_posts(ctx):
    ids = []
    for x in _lines(ctx.market_dir / "outbox.sent.jsonl"):
        if "post" not in (x.get("sent") or {}):
            continue
        try:
            ans = json.loads(x.get("answer") or "{}")
        except ValueError:
            continue
        if isinstance(ans, dict) and ans.get("id") is not None:
            ids.append(ans["id"])
    return ids


def main():
    tool_w, w = EventWriter("forum", source="tools"), EventWriter("forum", source="forum")
    ctx = Ctx(caller="forum", _writer=tool_w)
    try:
        w.context(strategy_version=ctx.strategy.get("version"), epoch=ctx.strategy.get("epoch"),
                  card_version=card.load()[0])
        refs, mine = known_ledger(ctx), own_posts(ctx)
        ctx.situation = {"known_ledger": refs, "own_posts": mine}
        posts = call(ctx, "market.forum", {"limit": 12})["result"]
        text = ("# the decision at hand\n"
                f"Ledger entries we may cite (our deals): {refs[-30:] or 'none yet'}. "
                f"Our own posts: {mine[-10:] or 'none'}.\nNewest forum posts (marked excerpts):\n"
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
            w.emit("forum.posted", {"as": d["as"], "refs_valid": True, "post_id": None})
        print(json.dumps({"forum": (d or {}).get("action"), "votes": votes}))
    finally:
        w.close()
        tool_w.close()


if __name__ == "__main__":
    main()
