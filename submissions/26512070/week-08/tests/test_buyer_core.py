"""agents.buyer_core with a scripted model and a scripted shop (no network, no model).

    py -m unittest discover -s tests      # from submissions/26512070/week-08
"""
import os
import shutil
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from agents.buyer_core import BuyerCore  # noqa: E402
from core import paths, reflex  # noqa: E402
from core.events import EventReader  # noqa: E402
from tests.test_calc_check import NOW, Base  # noqa: E402

ORDER = {"order": "o1", "item": "음료", "budget": 22000, "acceptable": ["G0011e806"], "use_within_hours": 48}
SHOPS = [{"name": "elm", "url": "http://elm", "reviews": [{"stars": 5, "review": "Deal at 18000; receipt matched."}],
          "offers": [{"sku": "G0011e806", "qty": 3}]},
         {"name": "oak", "url": "http://oak", "reviews": [], "offers": [{"sku": "G0026722f", "qty": 1}]}]


class Shop:
    """Answers each message from a script: (state, text, receipt)."""
    def __init__(self, script):
        self.script, self.sent, self.canceled = list(script), [], False

    def __call__(self, url):
        self.url = url
        return self

    def send(self, text, timeout=None):
        self.sent.append(text)
        return self.script.pop(0)

    def cancel(self):
        self.canceled = True


def receipt(price):
    return {"shop": "elm", "item": "G0011e806", "price": price, "task": "t", "sig_seller": "sig", "cert": True}


class BuyerCoreTest(Base):
    def setUp(self):
        super().setUp()
        shutil.copytree(HERE / "card", paths.root() / "card")
        os.environ["MARKET_DIR"] = str(self.market)

    def tearDown(self):
        os.environ.pop("MARKET_DIR", None)
        super().tearDown()

    def run_core(self, answers, shop, shops=SHOPS):
        it = iter(answers)
        core = BuyerCore(dict(ORDER), shops, model_call=lambda p, s, t: next(it), connect=shop, clock=lambda: NOW)
        try:
            return core.run()
        finally:
            core.close()

    def test_honest_deal_with_accept_policy(self):
        shop = Shop([("TASK_STATE_INPUT_REQUIRED", "Asking 21000.", None),
                     ("TASK_STATE_COMPLETED", "Deal at 18000.", receipt(18000))])
        out = self.run_core(['{"tool": "act.pick", "args": {"shop": "elm", "sku": "G0011e806"}}',
                             '{"tool": "act.offer", "args": {"price": 18000}}',
                             '{"tool": "act.finish", "args": {"accept": {"cert": true, "min_hours_left": 48}}}'], shop)
        self.assertEqual((out["shop"], out["receipt"]["price"], out["accept"]), ("http://elm", 18000,
                                                                                  {"cert": True, "min_hours_left": 48}))
        self.assertTrue(shop.sent[0].startswith("I want to buy"))
        kinds = [e["kind"] for e in EventReader("t", "buyer").iter_new()]
        self.assertEqual(kinds, ["buyer.order", "buyer.pick", "buyer.decision", "buyer.closed"])
        self.assertEqual(reflex.load()["shops"]["elm"]["fair"], 1)

    def test_cheating_receipt_goes_out_as_null_and_blocks(self):
        shop = Shop([("TASK_STATE_INPUT_REQUIRED", "Asking 21000.", None),
                     ("TASK_STATE_COMPLETED", "Deal.", receipt(21000))])
        out = self.run_core(['{"tool": "act.pick", "args": {"shop": "elm", "sku": "G0011e806"}}',
                             '{"tool": "act.offer", "args": {"price": 18000}}'], shop)
        self.assertIsNone(out["receipt"])
        self.assertIn("elm", reflex.blocked())
        out = self.run_core([], Shop([]))                         # next order: elm is left out, oak has nothing
        self.assertEqual(out["shop"], "")

    def test_walk_away_and_bad_model(self):
        shop = Shop([("TASK_STATE_INPUT_REQUIRED", "Asking 90000.", None)])
        out = self.run_core(["junk", "junk", "junk", '{"tool": "act.walk_away", "args": {"why": "too dear"}}'], shop)
        self.assertIsNone(out["receipt"])
        self.assertTrue(shop.canceled)
        picks = [e["data"] for e in EventReader("t", "buyer").iter_new() if e["kind"] == "buyer.pick"]
        self.assertEqual((picks[0]["shop"], picks[0]["guard"]), ("elm", "no valid pick from the model"))

    def test_offer_over_budget_is_refused_then_corrected(self):
        shop = Shop([("TASK_STATE_INPUT_REQUIRED", "Asking 25000.", None),
                     ("TASK_STATE_COMPLETED", "Deal at 22000.", receipt(22000))])
        out = self.run_core(['{"tool": "act.pick", "args": {"shop": "elm", "sku": "G0011e806"}}',
                             '{"tool": "act.offer", "args": {"price": 25000}}',
                             '{"tool": "act.offer", "args": {"price": 22000}}',
                             '{"tool": "act.finish", "args": {"accept": {}}}'], shop)
        self.assertEqual(out["receipt"]["price"], 22000)
        self.assertEqual(shop.sent[1], "22000")


if __name__ == "__main__":
    unittest.main()
