# -*- coding: utf-8 -*-
"""disloc_backtest.py 單元測試(合成資料,不需網路)。"""
import unittest

import numpy as np
import pandas as pd

import disloc_backtest as B


class TestTiming(unittest.TestCase):
    def test_rev_month(self):
        self.assertEqual(B.latest_rev_month(pd.Timestamp("2024-03-01")), (2024, 1))   # 10 日前只有前兩個月
        self.assertEqual(B.latest_rev_month(pd.Timestamp("2024-03-11")), (2024, 2))
        self.assertEqual(B.latest_rev_month(pd.Timestamp("2024-01-02")), (2023, 11))

    def test_quarter(self):
        self.assertFalse(B.quarter_available("2024-03-31", pd.Timestamp("2024-05-02")))
        self.assertTrue(B.quarter_available("2024-03-31", pd.Timestamp("2024-06-03")))
        self.assertFalse(B.quarter_available("2023-12-31", pd.Timestamp("2024-03-01")))
        self.assertTrue(B.quarter_available("2023-12-31", pd.Timestamp("2024-04-01")))


class TestFundamentals(unittest.TestCase):
    def rows(self, vals, y=2022, m=1):
        out = []
        for v in vals:
            out.append((y, m, v))
            m += 1
            if m > 12:
                m, y = 1, y + 1
        return out

    def test_revenue(self):
        vals = [100] * 12 + [110, 120, 130, 140]       # 16 個月,最後 4 個月 YoY 都正
        r = B.analyze_revenue(self.rows(vals))
        self.assertEqual(r["growStreak"], 4)
        self.assertTrue(r["isHigh"])
        self.assertAlmostEqual(r["rev3mYoY"], round((120 + 130 + 140 - 300) / 300 * 100, 2))
        self.assertEqual(r["rev1mYoY"], 40.0)
        self.assertIsNone(B.analyze_revenue(self.rows([1, 2, 3])))

    def test_mops_snapshot(self):
        rows = self.rows([100] * 12 + [120, 130])     # 2022-01 … 2023-02
        ry, cy = B.mops_rev_snapshot(rows, pd.Timestamp("2023-03-15"))   # 可用到 2023-02
        self.assertEqual(ry, 30.0)
        self.assertEqual(cy, 25.0)

    def test_revenue_window(self):
        rows = self.rows([100] * 30, y=2021)
        got = B.rev_rows_at(rows, pd.Timestamp("2023-06-15"), 500)
        self.assertEqual(got[-1][:2], (2023, 5))
        self.assertTrue(len(got) <= 17)

    def test_gross_margin(self):
        rows = [("2023-03-31", 100, 20), ("2023-06-30", 100, 22), ("2023-09-30", 100, 21), ("2023-12-31", 100, 23),
                ("2024-03-31", 100, 30)]
        g = B.analyze_gross_margin(rows)
        self.assertEqual(g["gm"], 30.0)
        self.assertEqual(g["gmYoY"], 10.0)

    def test_capex(self):
        # 累計數:每年 Q1..Q4 = -10,-20,-30,-40(單季 -10);隔年翻倍 → YoY +100%
        rows = []
        for y, k in ((2022, 10), (2023, 20)):
            for q, mm in enumerate(("03-31", "06-30", "09-30", "12-31"), 1):
                rows.append((f"{y}-{mm}", -k * q))
        self.assertEqual(B.capex_yoy_history(rows, 2), [100.0, 100.0])
        self.assertIsNone(B.capex_yoy_history(rows[:6], 2))


class TestTech(unittest.TestCase):
    def test_path_a(self):
        c = [50.0] * 200 + list(np.linspace(50, 100, 20)) + list(np.linspace(100, 75, 10))   # 回檔 25%,仍在 120MA 之上
        r = B.evaluate_dislocation(c, [1000] * len(c))
        self.assertTrue(r["ok"])
        self.assertEqual(r["path"], "A")

    def test_path_b(self):
        c = list(np.linspace(50, 100, 160)) + [90] * 10 + list(np.linspace(90, 96, 10))  # 淺回檔、近 10 日漲 >5%、離 120MA >10%
        r = B.evaluate_dislocation(c, [1000] * len(c))
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["path"], "B")

    def test_rejects(self):
        c = list(np.linspace(50, 100, 150)) + list(np.linspace(100, 40, 30))
        self.assertEqual(B.evaluate_dislocation(c, [1000] * len(c))["why"], "ddOut")
        c = [100.0] * 180
        self.assertEqual(B.evaluate_dislocation(c, [1] * 180)["why"], "volLow")     # 1 張 × 100 元
        self.assertEqual(B.evaluate_dislocation(c, [1000] * 180)["why"], "momentumWeak")


class TestStats(unittest.TestCase):
    def test_verdict(self):
        self.assertEqual(B.verdict([0.01, 0.02]), "保留")
        self.assertEqual(B.verdict([-0.02, -0.01]), "建議移除")
        self.assertTrue(B.verdict([-0.01, 0.02]).startswith("未經驗證"))

    def test_summarize(self):
        tr = pd.DataFrame([dict(signal_date=d, code=str(i), path=p, net=0.01, ex_pool=0.005, ex_rand=0.004)
                           for i, (d, p) in enumerate([("2024-01-02", "A"), ("2024-01-02", "B"), ("2020-01-02", "B")])])
        s = B.summarize(tr)
        self.assertEqual(s["out_of_sample"]["B"]["net"]["n"], 1)
        self.assertEqual(s["in_sample"]["B"]["net"]["n"], 1)
        self.assertEqual(s["out_of_sample"]["B_minus_A_same_day"]["dates"], 1)


if __name__ == "__main__":
    unittest.main()
