# -*- coding: utf-8 -*-
import unittest

import adj_factors as A


class T(unittest.TestCase):
    def test_cash_dividend(self):
        # 現金股利 5 元(第 3 天除息):Adj Close 在除息前 = Close × 0.95
        seg = A.segments(["d1", "d2", "d3", "d4"], [95, 95, 100, 101], [100, 100, 100, 101], [0, 0, 0, 0])
        self.assertEqual(seg, [["d1", 0.95], ["d3", 1.0]])

    def test_stock_dividend(self):
        # 配股 1.1(第 3 天除權):Yahoo Close 已回溯扣配股(110 → 100),交易所原始價除權前是 110
        seg = A.segments(["d1", "d2", "d3"], [100, 100, 100], [100, 100, 100], [0, 0, 1.1])
        self.assertEqual(seg, [["d1", round(1 / 1.1, 6)], ["d3", 1.0]])
        raw_before = 110.0
        self.assertAlmostEqual(raw_before * seg[0][1], 100.0, places=3)     # 原始價 × 係數 = 還原價

    def test_sane(self):
        self.assertTrue(A.sane([["d", 0.5], ["e", 1.0]]))
        self.assertTrue(A.sane([["d", 2.0], ["e", 1.0]]))                       # 減資(股本減半)→ 舊價格 × 2
        self.assertFalse(A.sane([["d", 6.0]]))
        self.assertFalse(A.sane([]))


if __name__ == "__main__":
    unittest.main()
