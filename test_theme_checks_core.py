# -*- coding: utf-8 -*-
import datetime as dt
import unittest

import theme_checks_core as T

D = dt.date


def months(start_y, start_m, values):
    out, y, m = [], start_y, start_m
    for v in values:
        out.append((y, m, v))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def accel_series(n=30, base=100.0, g0=0.05, step=0.01):
    """第一年 100 平盤;之後每月 = 12 個月前 ×(1 + 年增率),年增率每月提高 → 加速"""
    vals = [base] * 12
    for k in range(n - 12):
        vals.append(vals[-12] * (1 + g0 + step * k))
    return months(2024, 1, vals)


class TestAvailability(unittest.TestCase):
    def test_dates(self):
        self.assertEqual(T.month_avail(2026, 9), D(2026, 10, 10))
        self.assertEqual(T.month_avail(2026, 12), D(2027, 1, 10))
        self.assertEqual(T.quarter_avail(2026, 3), D(2026, 11, 14))
        self.assertEqual(T.quarter_avail(2025, 4), D(2026, 3, 31))

    def test_no_lookahead_month(self):
        rows = accel_series()
        last_y, last_m, _ = rows[-1]
        before = T.month_avail(last_y, last_m) - dt.timedelta(days=1)
        a = T.q1_revenue(rows, T.month_avail(last_y, last_m))
        b = T.q1_revenue(rows, before)
        self.assertNotEqual(a["period"], b["period"])           # 公布日前一天看不到最後一個月


class TestQ1(unittest.TestCase):
    def test_accelerating(self):
        rows = accel_series()
        c = T.q1_revenue(rows, D(2030, 1, 1))
        self.assertEqual(c["state"], "g")
        self.assertEqual(c["ev"], "事實")
        self.assertTrue(c["source"] and c["period"] and c["asof"])

    def test_decelerating(self):
        vals = [100.0] * 12 + [100 * (1 + 0.30 - 0.02 * k) for k in range(18)]
        c = T.q1_revenue(months(2024, 1, vals), D(2030, 1, 1))
        self.assertEqual(c["state"], "y")

    def test_insufficient_and_gap(self):
        self.assertEqual(T.q1_revenue(accel_series(n=15), D(2030, 1, 1))["state"], "n")
        rows = accel_series()
        rows = [r for r in rows if not (r[0] == rows[-2][0] and r[1] == rows[-2][1])]   # 缺一個月
        c = T.q1_revenue(rows, D(2030, 1, 1))
        self.assertEqual(c["state"], "n")
        self.assertIn("不插補", c["reason"])

    def test_low_base_flag(self):
        vals = [100.0] * 12 + [60.0] * 12 + [70 + 3 * k for k in range(12)]   # 去年大跌 → 今年回升
        c = T.q1_revenue(months(2023, 1, vals), D(2030, 1, 1))
        self.assertTrue(c["low_base"])


def qrows(gm_list, start=(2024, 1), rev=1000.0, opm_list=None):
    out, y, q = [], start[0], start[1]
    for i, gm in enumerate(gm_list):
        opm = (opm_list or [0.1] * len(gm_list))[i]
        out.append((y, q, {"revenue": rev * (1 + 0.05 * i), "gross": rev * (1 + 0.05 * i) * gm,
                           "op": rev * (1 + 0.05 * i) * opm}))
        y, q = (y + 1, 1) if q == 4 else (y, q + 1)
    return out


class TestQ4Q5(unittest.TestCase):
    def test_gm_rising(self):
        self.assertEqual(T.q4_gross_margin(qrows([.40, .40, .40, .41, .42, .43]), D(2030, 1, 1))["state"], "g")

    def test_gm_drop(self):
        self.assertEqual(T.q4_gross_margin(qrows([.40, .40, .40, .42, .44, .42]), D(2030, 1, 1))["state"], "y")

    def test_gm_flat_and_insufficient(self):
        self.assertEqual(T.q4_gross_margin(qrows([.40] * 6), D(2030, 1, 1))["state"], "flat")
        self.assertEqual(T.q4_gross_margin(qrows([.40] * 4), D(2030, 1, 1))["state"], "n")

    def test_financial_industry_na(self):
        c = T.q4_gross_margin(qrows([.4] * 6), D(2030, 1, 1), industry="金融保險業")
        self.assertEqual(c["state"], "na")
        self.assertTrue(c["reason"] and c["alt"])

    def test_operating_leverage(self):
        c = T.q5_operating(qrows([.4] * 5, opm_list=[.10, .10, .10, .10, .14]), D(2030, 1, 1))
        self.assertEqual(c["state"], "g")
        c = T.q5_operating(qrows([.4] * 5, opm_list=[.14, .10, .10, .10, .10]), D(2030, 1, 1))
        self.assertEqual(c["state"], "y")                              # 營收成長、營益率下降

    def test_quarter_no_lookahead(self):
        rows = qrows([.40, .40, .40, .41, .42, .43])                   # 最後一季 2025Q2
        self.assertEqual(T.q4_gross_margin(rows, D(2025, 8, 13))["period"], "2025Q1")
        self.assertEqual(T.q4_gross_margin(rows, D(2025, 8, 14))["period"], "2025Q2")


class TestQ6(unittest.TestCase):
    def test_cum_to_quarter(self):
        out = T.cum_to_quarter([(2025, 1, 10), (2025, 2, 25), (2025, 3, 45), (2025, 4, 70)])
        self.assertEqual([out[(2025, q)] for q in (1, 2, 3, 4)], [10, 15, 20, 25])
        out = T.cum_to_quarter([(2025, 1, 10), (2025, 3, 45)])        # 缺 Q2 → Q3 不能推
        self.assertIsNone(out[(2025, 3)])

    def cf(self, ocf_q, capex_q):
        rows, cum_o, cum_c = [], 0, 0
        for i, (o, c) in enumerate(zip(ocf_q, capex_q)):
            y, q = 2024 + i // 4, i % 4 + 1
            if q == 1:
                cum_o, cum_c = 0, 0
            cum_o += o
            cum_c += c
            rows.append((y, q, cum_o, -cum_c))                        # 資本支出為負數
        return rows

    def test_fcf_states(self):
        c = T.q6_fcf(self.cf([10] * 4 + [20] * 4, [5] * 8), D(2030, 1, 1))
        self.assertEqual((c["state"], c["fcf_ttm"], c["fcf_ttm_prev"]), ("g", 60, 20))
        eps = [(2024 + i // 4, i % 4 + 1, 1.0) for i in range(8)]
        c = T.q6_fcf(self.cf([10] * 8, [20] * 8), D(2030, 1, 1), eps_rows=eps)
        self.assertEqual(c["state"], "y")
        self.assertIn("EPS", c["note"])
        self.assertEqual(T.q6_fcf(self.cf([10] * 6, [5] * 6), D(2030, 1, 1))["state"], "n")   # 不足 8 季


class TestQ2Q3Q9(unittest.TestCase):
    def test_q2_needs_two_dates_same_year(self):
        asof = D(2026, 11, 9)
        self.assertEqual(T.q2_eps_revision([], asof)["state"], "n")
        one = [{"target_year": 2027, "date": "2026-10-01", "eps": 10, "source": "券商A"}]
        self.assertEqual(T.q2_eps_revision(one, asof)["state"], "n")
        mixed = one + [{"target_year": 2026, "date": "2026-11-01", "eps": 12, "source": "券商A"}]
        self.assertEqual(T.q2_eps_revision(mixed, asof)["state"], "n")      # 不同年度不比較
        up = one + [{"target_year": 2027, "date": "2026-11-01", "eps": 11, "source": "券商B"}]
        c = T.q2_eps_revision(up, asof)
        self.assertEqual((c["state"], c["ev"]), ("g", "分析師預估"))
        fut = one + [{"target_year": 2027, "date": "2026-12-01", "eps": 11, "source": "券商B"}]
        self.assertEqual(T.q2_eps_revision(fut, asof)["state"], "n")        # 未來日期不可用

    def test_q3_manual_only(self):
        self.assertEqual(T.q3_orders(None)["state"], "n")
        c = T.q3_orders({"content": "大單", "source": "法說", "date": "2026-11-01"})
        self.assertEqual(c["state"], "y")

    def pe(self, n, last):
        start = D(2024, 1, 1)
        return [(start + dt.timedelta(days=i), 10 + (i % 20)) for i in range(n)] + [(start + dt.timedelta(days=n), last)]

    def test_pe(self):
        c = T.q9_pe_percentile(self.pe(700, 29), D(2026, 1, 1))
        self.assertEqual(c["state"], "y")
        self.assertGreaterEqual(c["pct"], 80)
        self.assertEqual(T.q9_pe_percentile(self.pe(700, 11), D(2026, 1, 1))["state"], "flat")
        self.assertEqual(T.q9_pe_percentile(self.pe(700, -5), D(2026, 1, 1))["state"], "na")   # 虧損不算
        self.assertEqual(T.q9_pe_percentile(self.pe(100, 15), D(2026, 1, 1))["state"], "n")


class TestCrash(unittest.TestCase):
    def series(self, closes, start=D(2026, 1, 1)):
        return [(start + dt.timedelta(days=i), c) for i, c in enumerate(closes)]

    def test_drawdown_and_rs(self):
        idx = self.series([100] * 10 + [100 - 1.0 * k for k in range(1, 11)])     # 跌到 90(−10%)
        st = T.drawdown_state(idx)
        self.assertTrue(st["active"])
        self.assertEqual(st["peak"], 100)
        stk = self.series([50] * 10 + [50 - 0.1 * k for k in range(1, 11)])        # 只跌 2%
        rs = T.relative_strength(stk, idx, st["peak_date"])
        self.assertEqual(rs["state"], "g")
        rec = idx + [(idx[-1][0] + dt.timedelta(days=1), 98)]
        self.assertFalse(T.drawdown_state(rec)["active"])                           # 回到 97% 以上 → 結束

    def test_zero_close_is_missing(self):
        idx = self.series([100.0] * 5)
        stk = self.series([0.0] + [50.0] * 4)                               # 起點停牌(收盤 0)
        self.assertEqual(T.relative_strength(stk, idx, idx[0][0])["state"], "n")
        self.assertAlmostEqual(T.stock_drawdown(stk), 0.0)

    def test_crash_days(self):
        closes = [100 * (1.001 ** k) for k in range(100)] + [95]
        days = T.crash_days(self.series(closes))
        self.assertEqual(len(days), 1)
        self.assertLessEqual(days[0][1], -0.015)


class TestBoxesAndTiers(unittest.TestCase):
    G, Y, N, F = {"state": "g"}, {"state": "y"}, {"state": "n"}, {"state": "flat"}

    def test_four_box(self):
        self.assertEqual(T.four_box(self.G, self.G, self.F, {"pct": 50}, -0.3)["box"], 1)
        self.assertEqual(T.four_box(self.G, self.G, self.G, {"pct": 85}, -0.3)["box"], 3)
        self.assertEqual(T.four_box(self.Y, self.Y, self.G, None, -0.3)["box"], 2)
        self.assertIsNone(T.four_box(self.N, self.G, self.G, None, -0.3)["box"])      # 資料不足不當成「沒變」
        self.assertIsNone(T.four_box(self.G, self.G, self.G, None, -0.1)["box"])      # 回落未達 20%
        b = T.four_box(self.G, self.G, self.G, None, -0.3, manual_box4={"reason": "舊需求被取代"})
        self.assertEqual((b["box"], b["manual"]), (4, True))
        self.assertNotEqual(T.four_box(self.G, self.G, self.G, None, -0.3, manual_box4={})["box"], 4)   # 沒理由不能標 ④
        self.assertIn("EPS", b["note"])

    def test_box4_never_mechanical(self):
        for a in (self.G, self.Y, self.F):
            for b in (self.G, self.Y, self.F):
                self.assertNotEqual(T.four_box(a, b, self.G, {"pct": 99}, -0.5)["box"], 4)

    def test_tiers(self):
        self.assertEqual(T.tier(self.G, self.G, self.G, True)["tier"], "已證明")
        self.assertEqual(T.tier(self.G, self.F, self.Y, True)["tier"], "開始出現")
        self.assertEqual(T.tier(self.F, self.F, self.Y, True)["tier"], "只有故事")
        self.assertEqual(T.tier(self.N, self.N, self.G, True)["tier"], "資料不足")
        self.assertEqual(T.tier(self.G, self.G, self.G, True, q2=self.Y)["tier"], "開始出現")   # Q2 有證據但不是 ●
        self.assertEqual(T.tier(self.G, self.G, self.G, True, q2=self.N)["tier"], "已證明")

    def test_override_keeps_mechanical(self):
        t = T.tier(self.F, self.F, self.F, True, override={"tier": "開始出現", "reason": "法說確認新客戶"})
        self.assertEqual((t["tier"], t["mech_tier"], t["override_reason"]), ("開始出現", "只有故事", "法說確認新客戶"))
        t = T.tier(self.F, self.F, self.F, True, override={"tier": "開始出現"})          # 沒理由 → 不覆寫
        self.assertEqual(t["tier"], "只有故事")


class TestKillConditions(unittest.TestCase):
    OLD = [{"code": "2308", "version": 1, "idx": 1, "created": "2026-11-08", "metric": "rev_yoy_neg", "threshold": 2}]

    def test_append_only(self):
        ok, _ = T.check_append_only(self.OLD, self.OLD + [dict(self.OLD[0], version=2)])
        self.assertTrue(ok)
        ok, p = T.check_append_only(self.OLD, [dict(self.OLD[0], threshold=3)])
        self.assertFalse(ok)
        self.assertIn("修改", p[0])
        ok, p = T.check_append_only(self.OLD, [])
        self.assertIn("刪除", p[0])

    def test_current_and_history(self):
        rows = self.OLD + [dict(self.OLD[0], version=2, threshold=3)]
        c = T.current_conditions(rows, "2308")
        self.assertEqual((c["current"][0]["threshold"], len(c["history"])), (3, 1))

    def test_evaluate(self):
        vals = [100.0] * 12 + [110.0] * 10 + [90.0, 90.0]
        rev = months(2024, 1, vals)
        r = T.evaluate_condition({"metric": "rev_yoy_neg", "threshold": 2}, rev, [], [], D(2030, 1, 1))
        self.assertEqual(r["status"], "triggered")
        q = qrows([.45, .44, .43, .42, .41])
        r = T.evaluate_condition({"metric": "gm_down", "threshold": 2}, [], q, [], D(2030, 1, 1))
        self.assertEqual(r["status"], "triggered")
        self.assertEqual(T.evaluate_condition({"metric": "客戶下修"}, [], [], [], D(2030, 1, 1))["status"], "manual")


class TestSnapshots(unittest.TestCase):
    def test_compare(self):
        prev = {"q1": {"state": "n"}, "q4": {"state": "g"}, "q5": {"state": "flat"}, "q6": {"state": "n"}}
        cur = {"q1": {"state": "g"}, "q4": {"state": "y"}, "q5": {"state": "flat"}, "q6": {"state": "n"}}
        r = T.compare_snapshots(prev, cur)
        self.assertEqual((r["better"], r["worse"]), (1, 1))
        self.assertIn(["q1", "n", "g"], r["changes"])


class TestNoScoreNoAdvice(unittest.TestCase):
    def test_no_score_or_trade_words(self):
        with open(T.__file__, encoding="utf-8") as f:
            src = f.read()
        for w in ("目標價", "推薦", "必漲", "主力", "score"):
            self.assertNotIn(w, src)


if __name__ == "__main__":
    unittest.main()
