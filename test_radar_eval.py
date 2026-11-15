# -*- coding: utf-8 -*-
import unittest

import radar_eval as R


def c(t, d, x):
    return {"detection_type": t, "flagged_date": d, "excess_pct": x}


class T(unittest.TestCase):
    def test_insufficient(self):
        L = [c("theme_cluster", "d1", 5), c("keyword_surge", "d1", 1), c("keyword_surge", "d2", 0)]
        r = R.evaluate(L)["theme_cluster"]
        self.assertEqual((r["paired_dates"], r["status"], r["mean_diff_pp"]), (1, "樣本不足", 4.0))

    def test_better_and_dual_counts(self):
        L = []
        for i in range(30):
            d = "d%02d" % i
            L += [c("dual", d, 3 + i % 3), c("keyword_surge", d, 0), c("keyword_surge", d, 2)]
        r = R.evaluate(L)
        self.assertEqual(r["theme_cluster"]["status"], "較好")               # dual 併入兩種規則
        self.assertEqual(r["source_diversity"]["paired_dates"], 30)
        self.assertGreater(r["theme_cluster"]["ci95_pp"][0], 0)

    def test_no_difference(self):
        L = []
        for i in range(40):
            d = "d%02d" % i
            L += [c("source_diversity", d, (-1) ** i * 5), c("keyword_surge", d, 0)]
        self.assertEqual(R.evaluate(L)["source_diversity"]["status"], "沒有差別的證據")


if __name__ == "__main__":
    unittest.main()
