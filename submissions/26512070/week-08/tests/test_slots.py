"""agents.slots across real processes: never more than SLOTS calls at once, and a waiting seller goes first.

    py -m unittest discover -s tests      # from submissions/26512070/week-08
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent

WORKER = r'''
import json, sys, time
sys.path.insert(0, sys.argv[1])
from agents import slots
seat, hold, out = sys.argv[2], float(sys.argv[3]), sys.argv[4]
try:
    with slots.slot(seat, max_wait=float(sys.argv[5])):
        t = time.time()
        time.sleep(hold)
        rec = {"seat": seat, "start": t, "end": time.time()}
except slots.Busy:
    rec = {"seat": seat, "busy": True}
with open(out, "a") as f:
    f.write(json.dumps(rec) + "\n")
'''


class SlotsTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="harness-test-"))
        self.env = dict(os.environ, HARNESS_ROOT=str(self.root), HARNESS_SLOTS="2")
        self.worker = self.root / "worker.py"
        self.worker.write_text(WORKER)
        self.out = self.root / "out.jsonl"

    def start(self, seat, hold, max_wait=30):
        return subprocess.Popen([sys.executable, str(self.worker), str(HERE), seat, str(hold), str(self.out), str(max_wait)],
                                env=self.env)

    def records(self):
        return [json.loads(x) for x in self.out.read_text().splitlines()]

    def test_limit_and_priority(self):
        procs = [self.start("forum", 1.5), self.start("forum", 1.5)]      # take both slots
        time.sleep(0.8)
        procs += [self.start("forum", 0.3), self.start("analyst", 0.3)]   # wait first
        time.sleep(0.3)
        procs.append(self.start("seller", 0.3))                            # arrives last, should go first
        for p in procs:
            p.wait(timeout=60)
        recs = [r for r in self.records() if "start" in r]
        self.assertEqual(len(recs), 5)
        for r in recs:                                                     # never more than 2 at once
            overlap = sum(1 for x in recs if x["start"] < r["start"] + 0.01 < x["end"])
            self.assertLessEqual(overlap, 2, recs)
        first = min(x["start"] for x in recs)
        waited = sorted((r for r in recs if r["start"] > first + 0.5), key=lambda r: r["start"])   # the three that waited
        self.assertEqual(waited[0]["seat"], "seller", waited)

    def test_busy_after_max_wait(self):
        procs = [self.start("forum", 2.5), self.start("forum", 2.5)]
        time.sleep(0.8)
        procs.append(self.start("seller", 0.1, max_wait=0.5))
        for p in procs:
            p.wait(timeout=60)
        self.assertIn({"seat": "seller", "busy": True}, self.records())


if __name__ == "__main__":
    unittest.main()
