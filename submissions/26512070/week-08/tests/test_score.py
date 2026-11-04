"""core.score: the board's rule - percentiles over the ranked rows only, ties half, missing as 0.

    py -m unittest discover -s tests      # from submissions/26512070/week-08
"""
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from core import score  # noqa: E402


class ScoreTest(unittest.TestCase):
    def test_buyer_pool_ties_and_missing(self):
        board = {"buyers": [
            {"name": "me", "deals": 5, "surplus": 100, "forum": 0.6, "posts": 3},
            {"name": "a", "deals": 9, "surplus": 300, "forum": 0.6, "posts": 0},
            {"name": "b", "deals": 1, "surplus": 50, "posts": 9},          # no forum value: counts as 0
            {"name": "idle", "deals": 0, "surplus": 0, "forum": None, "posts": 0}]}   # not ranked
        s = score.board_score(board, "me")["buyer"]
        self.assertEqual(s["ranked"], 3)
        self.assertEqual(s["parts"]["surplus"]["percentile"], 0.5)        # 1 below + half of itself, of 3
        self.assertEqual(s["parts"]["forum"]["percentile"], round((1 + 0.5 * 2) / 3, 3))
        self.assertEqual(s["parts"]["surplus"]["next_value"], 300)
        self.assertEqual(s["room"][0][0], "surplus")


if __name__ == "__main__":
    unittest.main()
