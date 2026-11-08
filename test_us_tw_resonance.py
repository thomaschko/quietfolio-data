# -*- coding: utf-8 -*-
import datetime as dt
import json
import os
import shutil
import tempfile
import unittest

import us_pr_scan as P
import us_tw_resonance as R

NOW = dt.datetime(2026, 11, 8, 12, tzinfo=dt.timezone(dt.timedelta(hours=8)))


class TestResonance(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.d, P.DAY_DIR))
        items = [{"ticker": t, "cik": i, "acc": "a%d" % i, "url": "u", "status": "ok",
                  "hits": [["光通訊/CPO|optical", 2], ["平台/網通/供應鏈|supply chain", 5]],
                  "sn": [["光通訊/CPO|optical", "optical demand"]]} for i, t in enumerate(["AAA", "BBB", "CCC"])]
        items.append({"ticker": "DDD", "cik": 9, "acc": "a9", "url": "u", "status": "ok",
                      "hits": [["平台/網通/供應鏈|supply chain", 3]], "sn": []})
        self.w(os.path.join(P.DAY_DIR, "2026-11-05.json"), P.encode_day("2026-11-05", items, "src"))
        self.w("intl_theme_baskets.json", {"themes": {"光通訊/CPO": {"tw": ["1111", "2222", "3333"]},
                                                      "AI伺服器/網通": {"tw": ["4444"]}, "PCB/CCL/載板": {"tw": []}}})
        self.w("chip_structure_full.json", {"stocks": {
            "1111": {"n": "甲", "d1": [0.5, 0, 0], "i": [0, 0, 0, 0, 3000]},
            "2222": {"n": "乙", "d1": [0.2, 0, 0], "i": [0, 0, 0, 0, 1000]},
            "3333": {"n": "丙", "d1": [-0.3, 0, 0], "i": [0, 0, 0, 0, -1000]},
            "4444": {"n": "丁", "d1": [-0.01, 0, 0], "i": [0, 0, 0, 0, 5]}}})
        self.w("consensus.json", {"lev_inst": {"lists": {"both_up": [["4444", "丁"]]}}})

    def tearDown(self):
        shutil.rmtree(self.d)

    def w(self, p, obj):
        with open(os.path.join(self.d, p), "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)

    def test_labels_and_generic_excluded(self):
        doc, n = R.build(self.d, now=NOW)
        g = {r["g"]: r for r in doc["groups"]}
        self.assertEqual(g["光通訊/CPO"]["label"], "共振")       # 3 家美股 + 台股 2 同向 > 1 反向
        self.assertEqual((g["光通訊/CPO"]["tw_up"], g["光通訊/CPO"]["tw_down"]), (2, 1))
        self.assertEqual(g["平台/網通/供應鏈"]["pr_n30"], 0)     # supply chain 是通用詞,不計入
        self.assertEqual(g["平台/網通/供應鏈"]["tw"][0][5], "up")  # 共識頁融資+法人同向買 → 同向
        self.assertEqual(g["台廠點名"]["label"], "僅美股")
        sc = [t for t in doc["terms"] if t[1] == "supply chain"][0]
        self.assertEqual((sc[2], sc[6]), (4, 1))                  # 詞表仍顯示,標為通用詞
        self.assertEqual([r["t"] for r in doc["recent"]][:1], ["AAA"])
        self.assertNotIn("DDD", [r["t"] for r in doc["recent"]])  # 只有通用詞 → 不列在最近新聞稿
        self.assertLessEqual(n, R.SIZE_LIMIT)

    def test_no_us_data(self):
        shutil.rmtree(os.path.join(self.d, P.DAY_DIR))
        doc, _ = R.build(self.d, now=NOW)
        g = {r["g"]: r for r in doc["groups"]}
        self.assertEqual(g["光通訊/CPO"]["label"], "台股先動")
        self.assertEqual(doc["recent"], [])


if __name__ == "__main__":
    unittest.main()
