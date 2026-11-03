"""The outer runtime's collection (HARNESS.md 2.3): the market's ledger and board into our records and facts.

    py -m outer.collect once          # one round: ledger, broken goods, facts (and the board when due)
    py -m outer.collect once --from 2113000

Every round:
1. ledger: the lines after our cursor, 500 at a time, kept as they are in memory/market/ledger-<day>.jsonl.
   The cursor moves only after the lines are written, so a crash reads them again (at-least-once; the
   folds below skip a seq they already counted).
2. our sales: a settled line for our shop becomes seller.settled (price, the unit's cost from the seller's
   own seller.closed for that task, the buyer's name) and guess.confirmed; a line for our buyer is marked.
3. broken goods (me.match_broken): each new .market/broken.jsonl line is matched to a purchase of ours with
   the same item name within 6 hours before; one match is recorded, several stay candidates.
4. facts: memory/facts/live.json (selling in the last hours, customers, shops, participants), rebuilt from a
   small rolling state (memory/facts/collect_state.json), never from a rescan of the whole ledger.
5. board (every BOARD_EVERY): our shop, buyer and Players rows with each part's percentile, in
   memory/facts/scoreboard.json and one line per snapshot in memory/market/board-ours.jsonl.

Round 3 may hide fields of other participants' deals (prices); a missing field is "unknown", never 0.
"""
import json
import sys
import time
from pathlib import Path

from core import atomic, paths
from core.events import EventWriter
from tools.evaluate import events

MARKET = "https://market.ourolabs.ai"
PAGE = 500
WINDOW_HOURS = 48
BOARD_EVERY = 600
BROKEN_HOURS = 6
SETTLED_KINDS = ("result", "settle", "settled", "deal")


def config():
    c = json.loads((paths.root() / "config" / "shop.json").read_text(encoding="utf-8"))
    return {"card_name": c["name"], "join_name": c.get("join_name"), "github": c.get("github"), "market": c.get("market", MARKET)}


# --- reading a ledger line whatever Round 3 shows of it

def deal_of(row):
    """{seq, ts, buyer, shop_url, shop_card, sku, price, stars, task, order} for a settled line, else None."""
    if row.get("kind") not in SETTLED_KINDS:
        return None
    d = row.get("data") or {}
    r = d.get("receipt") if isinstance(d.get("receipt"), dict) else {}
    if not r and not d.get("item") and not d.get("sku"):
        return None                                 # a result with no deal
    mr = d.get("mandate_receipt") if isinstance(d.get("mandate_receipt"), dict) else {}
    return {"seq": row.get("seq"), "ts": row.get("ts"), "buyer": d.get("buyer"), "shop_url": d.get("shop"),
            "shop_card": r.get("shop") or d.get("shop_name"), "sku": r.get("item") or d.get("sku") or d.get("item") or mr.get("item"),
            "price": r.get("price", d.get("price")), "stars": d.get("stars"), "task": r.get("task") or d.get("task"),
            "order": d.get("order"), "unit": mr.get("unit") or d.get("unit")}


# --- the rolling state behind live.json

def empty_state():
    return {"hours": {}, "customers": {}, "shops": {}, "participants": {}, "last_seq": None, "broken_offset": 0,
            "board_ts": 0, "our_shop_urls": []}


def fold(state, row, me, catalog):
    """Count one ledger line into the state. Returns the deal (or None). Skips seqs already counted."""
    seq = row.get("seq")
    if state["last_seq"] is not None and seq is not None and seq <= state["last_seq"]:
        return None
    if seq is not None:
        state["last_seq"] = seq
    d = row.get("data") or {}
    ts = row.get("ts") or time.time()
    if row.get("kind") == "join" and d.get("name"):
        p = state["participants"].setdefault(d["name"], {"first_seen": ts, "roles": []})
        if d.get("role") and d["role"] not in p["roles"]:
            p["roles"].append(d["role"])
        if d.get("name") == me["join_name"] and d.get("role") == "shop" and d.get("url"):
            if d["url"] not in state["our_shop_urls"]:
                state["our_shop_urls"].append(d["url"])
    deal = deal_of(row)
    if deal is None:
        return None
    hour = str(int(ts // 3600 * 3600))
    h = state["hours"].setdefault(hour, {"deals": 0, "skus": {}, "cats": {}})
    h["deals"] += 1
    if deal["sku"]:
        h["skus"][deal["sku"]] = h["skus"].get(deal["sku"], 0) + 1
        cat = (catalog.get(deal["sku"]) or {}).get("category")
        if cat:
            h["cats"][cat] = h["cats"].get(cat, 0) + 1
    shop_key = deal["shop_card"] or deal["shop_url"]
    if shop_key:
        s = state["shops"].setdefault(shop_key, {"deals_seen": 0, "stars": [], "last": None, "url": deal["shop_url"]})
        s["deals_seen"] += 1
        s["last"] = ts
        if isinstance(deal["stars"], (int, float)):
            s["stars"] = (s["stars"] + [deal["stars"]])[-50:]
    ours = is_ours(deal, me, state)
    if ours and deal["buyer"]:
        c = state["customers"].setdefault(deal["buyer"], {"deals": 0, "first": ts, "last": None, "skus": {}, "paid": 0})
        c["deals"] += 1
        c["last"] = ts
        if deal["price"] is not None:
            c["paid"] += deal["price"]
        if deal["sku"]:
            c["skus"][deal["sku"]] = c["skus"].get(deal["sku"], 0) + 1
    deal["ours"] = ours
    deal["our_buyer"] = bool(me["join_name"] and deal["buyer"] == me["join_name"])
    return deal


def is_ours(deal, me, state):
    return bool((deal["shop_card"] and deal["shop_card"] == me["card_name"])
                or (deal["shop_url"] and deal["shop_url"] in state["our_shop_urls"]))


def prune(state, now):
    keep = now - WINDOW_HOURS * 3600
    state["hours"] = {k: v for k, v in state["hours"].items() if int(k) >= keep}


def live(state, catalog, now, hours=6):
    since = now - hours * 3600
    skus, cats, deals = {}, {}, 0
    for k, h in state["hours"].items():
        if int(k) + 3600 <= since:
            continue
        deals += h["deals"]
        for s, n in h["skus"].items():
            skus[s] = skus.get(s, 0) + n
        for c, n in h["cats"].items():
            cats[c] = cats.get(c, 0) + n
    top = sorted(skus.items(), key=lambda x: -x[1])[:50]
    return {
        "updated": now, "window_hours": hours,
        "selling": {"deals": deals, "updated": now,
                    "skus": [{"sku": s, "name": (catalog.get(s) or {}).get("name"), "kind": (catalog.get(s) or {}).get("kind"),
                              "sold": n} for s, n in top],
                    "categories": [{"category": c, "sold": n} for c, n in sorted(cats.items(), key=lambda x: -x[1])[:30]]},
        "sku": {s: {"sold_6h": n} for s, n in top},
        "customer": {n: {"deals_with_us": c["deals"], "first": c["first"], "last": c["last"], "paid_total": c["paid"],
                         "top_skus": sorted(c["skus"], key=lambda s: -c["skus"][s])[:5]} for n, c in state["customers"].items()},
        "shop": {n: {"deals_seen": s["deals_seen"], "last": s["last"],
                     "stars_avg": round(sum(s["stars"]) / len(s["stars"]), 2) if s["stars"] else None}
                 for n, s in state["shops"].items()},
        "participants": {"count": len(state["participants"]),
                         "new_6h": sorted(n for n, p in state["participants"].items() if p["first_seen"] >= since)},
    }


# --- our own records

def seller_costs():
    """task -> (cost, unit) from our seller's closed negotiations."""
    return {e["keys"].get("task"): (e["data"].get("cost"), e["keys"].get("unit"))
            for e in events(["seller.closed"]) if e["data"].get("outcome") == "completed" and e["keys"].get("task")}


def match_broken(lines, purchases, hours=BROKEN_HOURS):
    """[(news, [candidate purchases])]: purchases of ours with the same item name, up to `hours` before it broke."""
    out = []
    for n in lines:
        cands = [p for p in purchases
                 if p.get("name") and p["name"] == n.get("name") and 0 <= n.get("broke_at", 0) - p["ts"] <= hours * 3600]
        out.append((n, cands))
    return out


# --- the market

class Market:
    def __init__(self, base):
        import httpx
        self.base, self.c = base.rstrip("/"), httpx.Client(timeout=90)

    def get(self, path, tries=4, **params):
        last = None
        for i in range(tries):
            try:
                r = self.c.get(self.base + path, params=params)
                if r.status_code == 200:
                    return r.json()
                last = f"{r.status_code}"
            except Exception as e:                       # network trouble or a 502 page: try again, then skip
                last = type(e).__name__
            time.sleep(2 * (i + 1))
        raise RuntimeError(f"{path}: {last}")


def percentiles(rows, mine, fields):
    """For each numeric field, our percentile among rows that have it (higher is better)."""
    out = {}
    for f in fields:
        vals = [r[f] for r in rows if isinstance(r.get(f), (int, float))]
        v = mine.get(f) if mine else None
        if isinstance(v, (int, float)) and vals:
            out[f] = round(100 * sum(1 for x in vals if x <= v) / len(vals), 1)
    return out


def board_snapshot(board, me, now):
    def find(rows, key):
        return next((r for r in rows or [] if r.get("name") == key), None)
    shop = find(board.get("shops"), me["join_name"])
    buyer = find(board.get("buyers"), me["join_name"])
    player = next((p for p in board.get("players") or []
                   if (p.get("owner") or {}).get("login") == me["github"] or p.get("login") == me["github"]), None)
    num = lambda rows: sorted({k for r in rows or [] for k, v in r.items() if isinstance(v, (int, float)) and k not in ("joined",)})
    return {"ts": now, "round": board.get("round"), "totals": board.get("totals"),
            "shop": shop, "buyer": buyer, "player": player,
            "shop_percentiles": percentiles(board.get("shops"), shop, num(board.get("shops"))),
            "buyer_percentiles": percentiles(board.get("buyers"), buyer, num(board.get("buyers"))),
            "player_percentiles": percentiles(board.get("players"), player, num(board.get("players"))),
            "counts": {k: len(board.get(k) or []) for k in ("shops", "buyers", "players")}}


class Collector:
    def __init__(self, market=None, clock=time.time):
        self.me = config()
        self.market = market or Market(self.me["market"])
        self.clock = clock
        self.state_path = paths.memory() / "facts" / "collect_state.json"
        self.cursor_path = paths.memory() / "facts" / "ledger_cursor.json"
        self.w = EventWriter("collect", source="outer")
        self.store = atomic.Appender()

    def close(self):
        self.w.close()
        self.store.close()

    def catalog(self):
        md = Path(__import__("os").environ.get("MARKET_DIR") or paths.root() / ".market")
        c = atomic.read_json(md / "catalog.json", default={}) or {}
        return {x["sku"]: x for x in c} if isinstance(c, list) else c

    def round(self, start=None):
        now = self.clock()
        state = atomic.read_json(self.state_path, default=None) or empty_state()
        cur = atomic.read_json(self.cursor_path, default=None)
        if cur is None:
            if start is None:                            # first run: start from now, like the kit
                last = self.market.get("/api/ledger", after=-1)
                start = last[-1]["seq"] if last else 0
            cur = {"seq": start}
        cat, costs = self.catalog(), None
        n_rows, ours = 0, 0
        while True:
            rows = self.market.get("/api/ledger", after=cur["seq"])
            if not rows:
                break
            day = time.strftime("%Y%m%d", time.localtime(now))
            for row in rows:
                self.store.append(paths.memory() / "market" / f"ledger-{day}.jsonl", json.dumps(row, ensure_ascii=False))
                deal = fold(state, row, self.me, cat)
                if deal and deal["ours"]:
                    costs = costs if costs is not None else seller_costs()
                    cost, unit = costs.get(deal["task"], (None, None))
                    self.w.emit("seller.settled", {"price": deal["price"], "cost": cost, "ledger": deal["seq"],
                                                   "sku": deal["sku"], "stars": deal["stars"]},
                                task=deal["task"], ledger_seq=deal["seq"], **({"counterpart": deal["buyer"]} if deal["buyer"] else {}),
                                **({"unit": unit} if unit else {}))
                    if deal["buyer"] and deal["task"]:
                        self.w.emit("guess.confirmed", {"task": deal["task"], "actual": deal["buyer"]}, task=deal["task"])
                    ours += 1
                if deal and deal["our_buyer"]:
                    self.w.emit("buyer.settled_seen", {"shop": deal["shop_card"], "price": deal["price"], "sku": deal["sku"]},
                                ledger_seq=deal["seq"], **({"order": deal["order"]} if deal["order"] else {}))
            cur = {"seq": rows[-1]["seq"]}
            atomic.write_json(self.cursor_path, cur)     # after the lines are kept
            n_rows += len(rows)
            if len(rows) < PAGE:
                break
        broken = self.broken(state, cat)
        prune(state, now)
        atomic.write_json(self.state_path, state)
        atomic.write_json(paths.memory() / "facts" / "live.json", live(state, cat, now))
        board = None
        if now - state.get("board_ts", 0) >= BOARD_EVERY:
            try:
                snap = board_snapshot(self.market.get("/api/board"), self.me, now)
                atomic.write_json(paths.memory() / "facts" / "scoreboard.json", snap)
                self.store.append(paths.memory() / "market" / "board-ours.jsonl", json.dumps(
                    {k: snap[k] for k in ("ts", "shop", "buyer", "player", "shop_percentiles", "buyer_percentiles",
                                          "player_percentiles")}, ensure_ascii=False))
                state["board_ts"] = now
                atomic.write_json(self.state_path, state)
                board = True
            except RuntimeError as e:
                board = f"skipped: {e}"
        out = {"rows": n_rows, "our_sales": ours, "broken": broken, "cursor": cur["seq"], "board": board}
        self.w.emit("collect.round", out)
        return out

    def broken(self, state, cat):
        md = Path(__import__("os").environ.get("MARKET_DIR") or paths.root() / ".market")
        f = md / "broken.jsonl"
        if not f.exists():
            return 0
        with open(f, "rb") as fh:
            fh.seek(state.get("broken_offset", 0))
            chunk = fh.read()
        done = chunk.rfind(b"\n") + 1
        news = []
        for raw in chunk[:done].splitlines():
            try:
                news.append(json.loads(raw.decode("utf-8")))
            except (UnicodeDecodeError, ValueError):
                continue
        state["broken_offset"] = state.get("broken_offset", 0) + done
        if not news:
            return 0
        purchases = []
        for e in events(["buyer.received"]):
            purchases.append({"ts": e["ts"], "order": e["keys"].get("order"), "price": e["data"].get("price"),
                              "name": e["data"].get("name"), "sku": e["data"].get("sku")})
        for n, cands in match_broken(news, purchases):
            self.w.emit("buyer.broken", {"name": n.get("name"), "broke_at": n.get("broke_at"), "matched": len(cands) == 1,
                                         "candidates": [c["order"] for c in cands],
                                         "price": cands[0]["price"] if len(cands) == 1 else None},
                        **({"order": cands[0]["order"]} if len(cands) == 1 and cands[0]["order"] else {}))
        return len(news)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    o = sub.add_parser("once")
    o.add_argument("--from", dest="start", type=int)
    a = ap.parse_args()
    c = Collector()
    try:
        print(json.dumps(c.round(a.start)))
    finally:
        c.close()
        sys.stdout.flush()
