"""core.card, core.strategy and memory.card_section on the real card in card/.

    py -m unittest discover -s tests      # from submissions/26512070/week-08
"""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from core import atomic, card, paths, strategy  # noqa: E402
from tools import query  # noqa: E402,F401
from tools.registry import Ctx, call  # noqa: E402

STRATEGY_V1 = atomic.read_json(HERE / "card" / "strategy_v1.json")


class Base(unittest.TestCase):
    def setUp(self):
        self.old = os.environ.get("HARNESS_ROOT")
        root = Path(tempfile.mkdtemp(prefix="harness-test-"))
        shutil.copytree(HERE / "card", root / "card")
        os.environ["HARNESS_ROOT"] = str(root)

    def tearDown(self):
        if self.old is None:
            os.environ.pop("HARNESS_ROOT", None)
        else:
            os.environ["HARNESS_ROOT"] = self.old


class CardTest(Base):
    def test_sections_and_version(self):
        v, s = card.load()
        self.assertEqual(v, 1)
        for k in ("core: the world", "core: the goal", "intent", "role: seller", "role: audit", "appendix: rules"):
            self.assertIn(k, s)

    def test_assemble_order_and_frames(self):
        v, text = card.assemble("seller", "executor", priorities=["exposure first"])
        self.assertLess(text.index("# core: the world"), text.index("# your role: seller"))
        self.assertLess(text.index("# your role: seller"), text.index("# intent"))
        self.assertTrue(text.rstrip().endswith("- exposure first"))           # the changing part comes last
        self.assertNotIn("appendix", text)
        _, with_limits = card.assemble("seller", priorities=["exposure first"],
                                       limits=STRATEGY_V1["limits"])
        self.assertIn("cash floor (KRW): 1500000", with_limits)
        self.assertIn("below cost: allowed in phase exposure", with_limits)
        self.assertLess(with_limits.index("# risk limits"), with_limits.index("# current priorities"))
        _, audit = card.assemble("audit", "auditor")
        self.assertIn("claims to verify", audit)
        with self.assertRaises(KeyError):
            card.assemble("cashier")

    def test_card_section_tool(self):
        ctx = Ctx(caller="test")
        self.assertIn("bankrupt", call(ctx, "memory.card_section", {"name": "appendix: rules"})["result"]["text"])
        self.assertFalse(call(ctx, "memory.card_section", {"name": "appendix: nope"})["ok"])


class StrategyTest(Base):
    def test_draft_is_refused_then_installed_when_approved(self):
        src = paths.root() / "card" / "strategy_v1.json"
        with self.assertRaises(ValueError):
            strategy.install(src)                                             # still a draft
        s = strategy.install(src, approved="user 2026-11-03 test")
        self.assertEqual(atomic.read_json(paths.memory() / "strategy" / "active.json")["version"], 1)
        self.assertTrue((paths.memory() / "strategy" / "history" / "v1.json").exists())
        with self.assertRaises(ValueError):
            strategy.install(src, approved="again")                           # not newer than v1


if __name__ == "__main__":
    unittest.main()
