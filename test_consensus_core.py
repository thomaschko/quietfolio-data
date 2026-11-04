# -*- coding: utf-8 -*-
import unittest

import consensus_core as cc


class TestCore(unittest.TestCase):
    def test_pctile_needs_history(self):
        self.assertIsNone(cc.pctile(5, list(range(50))))
        self.assertEqual(cc.pctile(1000, list(range(200))), 100.0)
        self.assertEqual(cc.pctile(-1, list(range(200))), 0.0)

    def test_zone_direction(self):
        self.assertEqual(cc.zone(95, +1), "hot")
        self.assertEqual(cc.zone(5, +1), "cold")
        self.assertEqual(cc.zone(95, -1), "cold")     # 反向 ETF 融資占比高 = 押空
        self.assertEqual(cc.zone(95, 0), "high")
        self.assertIsNone(cc.zone(50, +1))
        self.assertIsNone(cc.zone(None, +1))

    def test_mood_label(self):
        self.assertEqual(cc.mood_label(["hot"] * 3 + ["cold"])[0], "偏熱")
        self.assertEqual(cc.mood_label(["cold"] * 3)[0], "過度看空")
        self.assertEqual(cc.mood_label(["hot", "cold"])[0], "分歧")
        self.assertEqual(cc.mood_label([None, "high", "low"])[0], "中性")   # 只描述的指標不計

    def test_retail_and_drawdown(self):
        self.assertEqual(cc.retail_net_pct(-60, 100), 60.0)
        self.assertIsNone(cc.retail_net_pct(None, 100))
        self.assertEqual(cc.drawdown([100, 120, 108], 3), -10.0)
        self.assertIsNone(cc.drawdown([100, 108], 3))

    def test_classify_b(self):
        self.assertEqual(cc.classify_b(300, 5, 5e7), "both_up")
        self.assertEqual(cc.classify_b(-300, -5, -5e7), "both_down")
        self.assertEqual(cc.classify_b(300, 5, -5e7), "margin_up_inst_sell")
        self.assertEqual(cc.classify_b(-300, -5, 5e7), "margin_down_inst_buy")
        self.assertIsNone(cc.classify_b(150, 5, 5e7))     # 張數不夠
        self.assertIsNone(cc.classify_b(300, 2, 5e7))     # 百分比不夠
        self.assertIsNone(cc.classify_b(300, 5, 1e7))     # 法人金額不夠

    def test_passes_c(self):
        self.assertTrue(cc.passes_c(-12, 150, 4, 0.2, []))
        self.assertFalse(cc.passes_c(-8, 150, 4, 0.2, []))
        self.assertFalse(cc.passes_c(-12, 150, 4, 0.05, []))
        self.assertFalse(cc.passes_c(-12, 150, 4, 0.2, ["capital_change_suspect"]))
        self.assertFalse(cc.passes_c(None, 150, 4, 0.2, []))

    def test_notes_wording(self):
        t = "".join(cc.NOTES)
        for w in ("主力", "建議買進", "看多", "看空"):
            self.assertNotIn(w, t)


if __name__ == "__main__":
    unittest.main()
