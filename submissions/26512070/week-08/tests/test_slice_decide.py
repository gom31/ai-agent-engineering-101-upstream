"""outer.slice and outer.decide with a scripted model: summaries, a profile, a brief with options, the audit,
then the user's decision installs the next strategy version.

    py -m unittest discover -s tests      # from submissions/26512070/week-08
"""
import json
import shutil
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from core import atomic, paths  # noqa: E402
from outer import decide, slice as outer_slice  # noqa: E402
from tests.test_calc_check import Base  # noqa: E402


def j(tool, **args):
    return json.dumps({"tool": tool, "args": args})


class SliceDecideTest(Base):
    def setUp(self):
        super().setUp()
        shutil.copytree(HERE / "card", paths.root() / "card")

    def answers(self, options):
        return iter([
            j("rec.profile", name="elm", side="shop", summary="cheap, honest", interpreted=[
                {"claim": "first ask near cost", "evidence": ["ledger 10"]}]),
            j("rec.summary", summary="drinks sell", findings=[{"claim": "drinks lead", "evidence": ["live.selling"]}]),
            j("rec.summary", summary="we sold 1", findings=[{"claim": "one deal", "evidence": ["eval.unit_economics"]}]),
            j("rec.brief", score="no board yet", happened=[{"claim": "first deal", "evidence": ["ledger 11"]}],
              hypothesis="H1 too early", counterparts="elm is new", options=options,
              recommendation=options[0]["id"] if options else None),
            j("rec.audit", agrees=False, prefer="B", points=[{"claim": "A is early", "why": "only 1 deal"}]),
        ])

    def run_slice(self, options, now):
        it = self.answers(options)
        return outer_slice.run(now=now, model_call=lambda p, s, t: next(it))

    def test_slice_then_decide(self):
        import time
        opts = [{"id": "A", "change": {"phase": "profit"}, "targets": "shop profit", "expected": "+margin",
                 "risk": "fewer buyers", "reversible": True},
                {"id": "B", "change": {"context": ["keep exposure"]}, "targets": "deals", "expected": "more buyers",
                 "risk": "losses", "reversible": True}]
        now = time.mktime(time.strptime("2026-11-05 12:00", "%Y-%m-%d %H:%M"))
        out = self.run_slice(opts, now)
        self.assertTrue(all(s["ok"] for s in out["steps"].values()), out)
        pid = out["slice"]
        md = (paths.root() / "logs" / "strategy" / "briefs" / f"{pid}.md").read_text(encoding="utf-8")
        self.assertIn("### A  (recommended)", md)
        self.assertIn("agrees: False, prefers B", md)
        self.assertIn(pid, (paths.root() / "logs" / "strategy" / "PENDING.md").read_text(encoding="utf-8"))
        self.assertTrue((paths.memory() / "profiles" / "elm.json").exists())
        d = decide.decide(pid, "B", note="test")
        active = atomic.read_json(paths.memory() / "strategy" / "active.json")
        self.assertEqual((d["installed"], active["context"], active["version"]), (2, ["keep exposure"], 2))
        self.assertIn("option B", active["approved"])
        self.assertIn("(nothing)", (paths.root() / "logs" / "strategy" / "PENDING.md").read_text(encoding="utf-8"))
        with self.assertRaises(ValueError):
            decide.decide(pid, "A")

    def test_night_brief_refuses_options(self):
        import time
        opts = [{"id": "A", "change": {"phase": "profit"}, "targets": "x", "expected": "x", "risk": "x", "reversible": True},
                {"id": "B", "change": {"phase": "exposure"}, "targets": "x", "expected": "x", "risk": "x", "reversible": True}]
        now = time.mktime(time.strptime("2026-11-05 03:00", "%Y-%m-%d %H:%M"))
        it = iter(list(self.answers(opts))[:3] + [j("rec.brief", score="s", happened=[{"claim": "c", "evidence": ["e"]}],
                                                    hypothesis="h", counterparts="", options=opts, recommendation="A"),
                                                  j("rec.brief", score="s", happened=[{"claim": "c", "evidence": ["e"]}],
                                                    hypothesis="h", counterparts="", options=[]),
                                                  j("rec.audit", agrees=True, points=[])])
        out = outer_slice.run(now=now, model_call=lambda p, s, t: next(it))
        doc = atomic.read_json(paths.memory() / "strategy" / "proposals" / f"{out['slice']}.json")
        self.assertEqual((doc["status"], doc["options"]), ("report", []))

    def test_bad_option_change_is_refused(self):
        from tools.record import apply_change
        from tools.registry import Refused
        with self.assertRaises(Refused):
            apply_change(atomic.read_json(paths.memory() / "strategy" / "active.json"), {"version": 9})
        with self.assertRaises(Refused):
            apply_change(atomic.read_json(paths.memory() / "strategy" / "active.json"), {"limits": {"cash_floor": -1}})


if __name__ == "__main__":
    unittest.main()
