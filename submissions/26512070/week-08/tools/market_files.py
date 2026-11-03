"""Read the kit's .market/ files (the Round 3 agent contract in market.ourolabs.ai/agent.md).

Only this module knows the file names and field names the kit writes, so a change in the
kit's contract is fixed in one place. Grades follow the kit: food grade is the share of
shelf life left (A >= 0.8, B >= 0.45, else C; FOOD_GRADE_CUTS in scripts/market_kit.py).
"""
from core import atomic

FOOD_GRADE_CUTS = (0.8, 0.45)
STORAGE_PER_DAY = 0.01                 # goods in stock pay 1% of their cost per day
COST_SHARE = {"A": (0.55, 0.75), "B": (0.40, 0.60), "C": (0.25, 0.45)}   # of list price, drawn on arrival


def read(ctx, name, default=None):
    return atomic.read_json(ctx.market_dir / name, default=default)


def catalog(ctx):
    """sku -> {name, category, list_price, kind, shelf_hours}. The kit writes a dict keyed by sku."""
    c = read(ctx, "catalog.json", default={}) or {}
    return {x["sku"]: x for x in c} if isinstance(c, list) else c


def inventory(ctx):
    return read(ctx, "inventory.json", default={"balance": None, "units": []}) or {"units": []}


def unit(ctx, unit_id):
    return next((u for u in inventory(ctx).get("units", []) if u.get("unit") == unit_id), None)


def food_grade(share_left):
    if share_left is None:
        return None
    if share_left <= 0:
        return "expired"
    return "A" if share_left >= FOOD_GRADE_CUTS[0] else "B" if share_left >= FOOD_GRADE_CUTS[1] else "C"


def hours_left(expires_at, now):
    return None if not expires_at else (expires_at - now) / 3600


def current_grade(u, shelf_hours, now):
    """The grade the kit would judge now: food by shelf life left, goods as supplied."""
    if u.get("kind") == "food" or u.get("expires_at"):
        h = hours_left(u.get("expires_at"), now)
        return food_grade(h / shelf_hours if (h is not None and shelf_hours) else None) or u.get("grade")
    return u.get("grade")
