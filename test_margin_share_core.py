# -*- coding: utf-8 -*-
import unittest

import margin_share_core as ms

NAMES = {"2330": "台積電", "2303": "聯電", "2317": "鴻海", "0050": "元大台灣50", "9999": "新股", "1101": "台泥"}
IND = {"2330": "半導體業", "2303": "半導體業", "2317": "其他電子業", "1101": "水泥工業"}
ETF = {"0050"}


class TestEst(unittest.TestCase):
    def test_units_lots_to_yuan(self):
        est, lots, exc = ms.est_values({"2330": 2}, {"2330": 1000.0})
        self.assertEqual(est["2330"], 2 * 1000 * 1000.0)   # 張 × 1000 股 × 元
        self.assertEqual(lots["2330"], 2)
        self.assertEqual(exc, {})

    def test_missing_price_excluded(self):
        est, lots, exc = ms.est_values({"2330": 5, "9999": 7}, {"2330": 10})
        self.assertNotIn("9999", est)
        self.assertEqual(exc, {"9999": 7.0})

    def test_zero_balance_counted_but_no_value(self):
        est, lots, exc = ms.est_values({"2330": 5, "1101": 0}, {"2330": 10})   # 1101 缺價也沒關係
        self.assertEqual(est["1101"], 0.0)
        total, g = ms.by_industry(est, lots, IND, ETF, NAMES)
        self.assertEqual(g["水泥工業"]["n_stocks"], 1)
        self.assertEqual(g["水泥工業"]["n_with_margin"], 0)
        self.assertEqual(g["水泥工業"]["share"], 0)
        self.assertAlmostEqual(g["半導體業"]["share"], 1.0)

    def test_none_balance_not_treated_as_zero(self):
        est, lots, exc = ms.est_values({"2330": None}, {"2330": 10})
        self.assertEqual(est, {})


class TestIndustry(unittest.TestCase):
    def setUp(self):
        self.est, self.lots, _ = ms.est_values(
            {"2330": 10, "2303": 30, "2317": 20, "0050": 5, "9999": 1},
            {"2330": 1000, "2303": 50, "2317": 200, "0050": 150, "9999": 30})

    def test_exclusive_sum_equals_total(self):
        total, g = ms.by_industry(self.est, self.lots, IND, ETF, NAMES)
        self.assertAlmostEqual(sum(r["est_value"] for r in g.values()), total)
        self.assertAlmostEqual(sum(r["share"] for r in g.values()), 1.0)
        self.assertIn("ETF", g)
        self.assertIn("未分類", g)                       # 9999 對不上產業別,不丟棄
        self.assertEqual(g["未分類"]["n_stocks"], 1)

    def test_top3_and_concentration(self):
        total, g = ms.by_industry(self.est, self.lots, IND, ETF, NAMES)
        semi = g["半導體業"]
        self.assertEqual([t["code"] for t in semi["top3"]], ["2330", "2303"])
        self.assertEqual(semi["concentration"], 1.0)
        self.assertAlmostEqual(sum(t["w"] for t in semi["top3"]), 1.0, places=3)

    def test_markets_are_separate_denominators(self):
        tw_est, tw_lots, _ = ms.est_values({"2330": 10}, {"2330": 1000})
        tp_est, tp_lots, _ = ms.est_values({"6488": 10}, {"6488": 100})
        t1, g1 = ms.by_industry(tw_est, tw_lots, IND, ETF, NAMES)
        t2, g2 = ms.by_industry(tp_est, tp_lots, {"6488": "半導體業"}, ETF, NAMES)
        self.assertAlmostEqual(g1["半導體業"]["share"], 1.0)
        self.assertAlmostEqual(g2["半導體業"]["share"], 1.0)
        self.assertNotEqual(t1, t2)


class TestTheme(unittest.TestCase):
    def test_overlap_counted_in_both_union_not_double(self):
        est, lots, _ = ms.est_values({"2330": 10, "2303": 10, "2317": 10, "0050": 10},
                                     {"2330": 100, "2303": 100, "2317": 100, "0050": 100})
        themes = {"AI": ["2330", "2317"], "晶圓": ["2330", "2303"], "含ETF": ["0050"]}
        rows, cov, unm = ms.by_theme(est, lots, themes, ETF, NAMES)
        self.assertAlmostEqual(rows["AI"]["share"], 0.5)
        self.assertAlmostEqual(rows["晶圓"]["share"], 0.5)
        self.assertEqual(rows["AI"]["n_multi"], 1)        # 2330 同時屬於兩個題材
        self.assertEqual(rows["含ETF"]["n_stocks"], 0)   # ETF 不計入題材
        self.assertAlmostEqual(cov, 0.75)                 # 2330/2303/2317,不重複計算
        self.assertAlmostEqual(unm, 0.25)                 # 0050
        self.assertGreater(rows["AI"]["share"] + rows["晶圓"]["share"], cov)


class TestHistory(unittest.TestCase):
    def test_share_change_and_insufficient(self):
        h = [("2026-10-%02d" % d, {"g": 0.10 + d / 1000}) for d in range(1, 8)]
        self.assertAlmostEqual(ms.share_change(h, "2026-10-07", "g", 5), 0.5)
        self.assertIsNone(ms.share_change(h, "2026-10-07", "g", 20))
        self.assertIsNone(ms.share_change(h, "2026-10-09", "g", 1))     # 當日不在歷史

    def test_no_lookahead(self):
        h = [("2026-10-01", {"g": 0.1}), ("2026-10-02", {"g": 0.2}), ("2026-10-03", {"g": 0.9})]
        self.assertAlmostEqual(ms.share_change(h, "2026-10-02", "g", 1), 10.0)   # 不受 10-03 影響

    def test_levels(self):
        self.assertEqual(ms.history_level(119), "none")
        self.assertEqual(ms.history_level(120), "low")
        self.assertEqual(ms.history_level(245), "ok")
        self.assertEqual(ms.history_level(490), "ok_2y")

    def test_pctile_excludes_today_and_needs_history(self):
        self.assertIsNone(ms.pctile(5, list(range(100))))
        past = list(range(200))
        self.assertEqual(ms.pctile(199.5, past), 100.0)
        self.assertEqual(ms.pctile(-1, past), 0.0)
        self.assertAlmostEqual(ms.pctile(100, past), 50.25, places=1)


class TestFlags(unittest.TestCase):
    def test_recon(self):
        self.assertAlmostEqual(ms.recon_ratio(1.7e12, 1e9), 1.7)   # 仟元 → 元
        self.assertIsNone(ms.recon_ratio(1e12, None))
        self.assertIsNone(ms.recon_off(1.7, [1.7] * 5))
        self.assertFalse(ms.recon_off(1.75, [1.7] * 30))
        self.assertTrue(ms.recon_off(2.0, [1.7] * 30))

    def test_market_flags(self):
        f, unrel = ms.market_flags("2026-10-30", "2026-10-29", {"9999": 3}, {"2330": "", "2303": "OX", "2317": "OX"})
        kinds = {x["flag"] for x in f}
        self.assertTrue(unrel)
        self.assertEqual(kinds, {"date_mismatch", "missing_price", "suspended_or_special"})
        sp = [x for x in f if x["flag"] == "suspended_or_special"][0]
        self.assertEqual(sp["by_note"], {"OX": 2})
        f, unrel = ms.market_flags("2026-10-30", "2026-10-30", {}, {})
        self.assertEqual((f, unrel), ([], False))


class TestEvents(unittest.TestCase):
    def _series(self, n):
        rets = [0.2 if i % 2 else -0.2 for i in range(n)]
        rets[50], rets[80] = -3.0, 2.5
        rets[90] = -1.0          # 落在最低 5%? 不,且 |r| < 1.5 → 不算
        return [("d%03d" % i, r) for i, r in enumerate(rets)]

    def test_event_days_require_both_conditions(self):
        ev = ms.event_days(self._series(200))
        self.assertEqual([(d, k) for d, k, _ in ev], [("d050", "down"), ("d080", "up")])

    def test_event_days_need_history(self):
        self.assertEqual(ms.event_days(self._series(100)), [])

    def test_index_returns(self):
        r = ms.index_returns([("a", 100), ("b", 102), ("c", 99.96)])
        self.assertEqual([d for d, _ in r], ["b", "c"])
        self.assertAlmostEqual(r[0][1], 2.0)
        self.assertAlmostEqual(r[1][1], -2.0)

    def test_group_changes_fwd_only_in_history(self):
        prev = {"g": {"share": 0.1, "lots": 100, "est_value": 1000}}
        cur = {"g": {"share": 0.12, "lots": 90, "est_value": 900}}
        today = ms.event_group_changes(prev, cur)
        self.assertNotIn("d_lots_5d_pct", today["g"])
        self.assertAlmostEqual(today["g"]["d_share_pp"], 2.0)
        self.assertAlmostEqual(today["g"]["d_lots_pct"], -10.0)
        self.assertEqual(today["g"]["d_lots"], -10)
        hist = ms.event_group_changes(prev, cur, fwd={"g": {"lots": 81}})
        self.assertAlmostEqual(hist["g"]["d_lots_5d_pct"], -10.0)

    def test_top_drops(self):
        r = ms.top_lot_drops({"a": 100, "b": 50, "c": 10}, {"a": 60, "b": 55, "c": 0}, {"a": "A"})
        self.assertEqual([x["code"] for x in r], ["a", "c"])
        self.assertEqual(r[0]["pct_of_prev"], 40.0)

    def test_summary_needs_10_events(self):
        ev = [("d%d" % i, "down", -2.0) for i in range(9)]
        s = ms.summarize_events(ev, {})
        self.assertEqual(s["n_events"], 9)
        self.assertIsNone(s["stats"])
        ev = [("d%d" % i, "down", -2.0) for i in range(10)]
        # g1 百分比跌最多但張數少;排序看張數:g2(−500)、g3(−200)、g1(−5);g4 增加不計
        per = {d: {"g1": {"d_lots_pct": -30.0, "d_lots": -5}, "g2": {"d_lots_pct": -1.0, "d_lots": -500},
                   "g3": {"d_lots_pct": -0.5, "d_lots": -200}, "g4": {"d_lots_pct": 1.0, "d_lots": 300}} for d, _, _ in ev}
        s = ms.summarize_events(ev, per)
        self.assertEqual(s["stats"]["down"]["median_d_lots_pct"]["g1"], -30.0)
        self.assertEqual(list(s["stats"]["down"]["top3_lot_drop_counts"]), ["g2", "g3", "g1"])
        self.assertEqual(s["stats"]["down"]["top3_lot_drop_counts"]["g2"], 10)


class TestWording(unittest.TestCase):
    def test_notes_no_banned_words(self):
        text = "".join(ms.NOTES)
        for w in ("融資金額", "過熱", "危險", "主力", "看多", "看空"):
            if w == "融資金額":
                self.assertIn("不是官方融資金額", text)
                continue
            self.assertNotIn(w, text)


if __name__ == "__main__":
    unittest.main()
