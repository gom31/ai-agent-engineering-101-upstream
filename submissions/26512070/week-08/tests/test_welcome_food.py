"""Welcome Food (strategy v4, user 11/3): below cost only on a buyer's first deal, only food up to a list price, at
most 10% under cost; a buyer the fingerprints name as a confirmed customer pays at least cost.

    py -m unittest discover -s tests      # from submissions/26512070/week-08
"""
import os
import shutil
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from agents.seller_core import SellerCore  # noqa: E402
from core import atomic, paths  # noqa: E402
from tests.test_calc_check import NOW, STRATEGY, Base  # noqa: E402

V4 = dict(STRATEGY, limits=dict(STRATEGY["limits"], below_cost={
    "allowed": True, "only_in_phase": "exposure", "max_loss_share_of_cost": 0.1,
    "first_deal_only": True, "kinds": ["food"], "max_list_price": 30000}))


class WelcomeFoodTest(Base):
    def setUp(self):
        super().setUp()
        atomic.write_json(paths.memory() / "strategy" / "active.json", V4)
        shutil.copytree(HERE / "card", paths.root() / "card")

    def test_price_rule(self):
        # u-food-a: food, cost 14000, list 23230; u-goods: goods; u-food-c: food, list 36900
        self.ctx.situation = {"first_time": True}
        self.assertEqual(self.ok("check.price", unit="u-food-a", price=12600)["lowest_allowed"], 12600)
        self.assertIn("lowest allowed price is 12600", self.refused("check.price", unit="u-food-a", price=12599)["hint"])
        self.assertIn("only for ['food']", self.refused("check.price", unit="u-goods", price=2800)["reason"])
        self.assertIn("up to list price 30000", self.refused("check.price", unit="u-food-c", price=9500)["reason"])
        self.ctx.situation = {"first_time": False}
        self.assertIn("a known customer", self.refused("check.price", unit="u-food-a", price=13000)["reason"])
        self.ctx.situation = {}
        self.assertIn("not known to be new", self.refused("check.price", unit="u-food-a", price=13000)["reason"])
        self.ok("check.price", unit="u-food-a", price=14000)              # at cost: always fine

    def test_seller_marks_known_customers(self):
        os.environ["MARKET_DIR"] = str(self.market)
        try:
            atomic.write_json(paths.memory() / "facts" / "fingerprints.json", {
                "participants": {"kim": {"phrases": ["i want to buy <sku> for <n>"], "deals": 3}}, "accuracy": 0.8})
            atomic.write_json(paths.memory() / "facts" / "live.json", {"customer": {"kim": {"deals_with_us": 3}}})
            seen = []
            core = SellerCore("Test Shop", Ed25519PrivateKey.generate(), clock=lambda: NOW,
                              model_call=lambda p, s, t: seen.append(p) or
                              '{"tool": "act.counter", "args": {"unit": "u-food-a", "price": 12600, "say": "12600 KRW"}}')
            core.handle("t1", "I want to buy G0011e806 for 12000")         # kim's opening: a known customer
            self.assertFalse(core.open["t1"]["first_time"])
            self.assertIn("known customer: no first-deal discount", seen[-1])
            core.handle("t2", "hello, G0026722f please")                    # nobody we know (t1 holds u-food-a)
            self.assertTrue(core.open["t2"]["first_time"])
            self.assertIn("Welcome Food", seen[-1])
            core.events.close()
            core.tool_events.close()
        finally:
            os.environ.pop("MARKET_DIR", None)


if __name__ == "__main__":
    unittest.main()
