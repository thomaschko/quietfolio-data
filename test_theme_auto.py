# -*- coding: utf-8 -*-
import datetime as dt
import json
import os
import shutil
import tempfile
import unittest

import theme_auto as A

MAP = {"fixed": ["cpo"], "max_themes": 3, "max_stocks_per_theme": 3, "themes": {
    "cpo": {"name": "CPO", "aliases": ["CPO", "矽光子"], "coverage": ["CPO"], "baskets": [], "us_groups": ["光通訊/CPO"]},
    "memory": {"name": "記憶體", "aliases": ["HBM", "DRAM", "南亞科"], "coverage": [], "baskets": ["記憶體"], "us_groups": []},
    "robot": {"name": "機器人", "aliases": ["人形機器人"], "coverage": [], "baskets": [], "us_groups": []},
    "quiet": {"name": "安靜題材", "aliases": ["安靜"], "coverage": [], "baskets": ["x"], "us_groups": []}}}


class FakeChain:
    def __init__(self, out):
        self.out, self.calls = out, 0
        self.available = ["fake"]

    def ask_json(self, system, user):
        self.calls += 1
        return self.out, "fake"


class TestAuto(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.w("theme_auto_map.json", MAP)
        self.w("margin_share_themes.json", {"themes": {"CPO": ["3081", "3163", "4979", "6442"]}})
        self.w("intl_theme_baskets.json", {"themes": {"記憶體": {"tw": ["2408", "2344"]}, "x": {"tw": ["1101"]}}})
        self.w("daily_digest.json", {"date": "20261109",
                                     "B_preferment_themes": [{"theme": "CPO", "codes": ["9999"], "ratio": 1.5}],
                                     "D_surge_themes": [{"theme": "DRAM", "codes": ["2408"], "ratio": 2.1},
                                                        {"theme": "人形機器人", "codes": [], "ratio": 1.5},
                                                        {"theme": "金管會", "codes": [], "ratio": 3}],
                                     "G_cross_confirmed": [{"theme": "HBM", "dual_confirmed": True}],
                                     "E_new_candidates": [{"term": "南亞科", "stocks": ["2408"]}]})
        self.w("us_tw_resonance.json", {"groups": [{"g": "光通訊/CPO", "label": "共振", "pr_n30": 20, "tr_cur": 11}]})
        self.w("chip_structure_full.json", {"stocks": {}})
        self.w("consensus.json", {})

    def tearDown(self):
        shutil.rmtree(self.d)

    def w(self, p, o):
        with open(os.path.join(self.d, p), "w", encoding="utf-8") as f:
            json.dump(o, f, ensure_ascii=False)

    def test_select(self):
        th, meta = A.select_themes(self.d, dt.date(2026, 11, 9))
        ids = [t["id"] for t in th]
        self.assertEqual(ids, ["cpo", "memory", "robot"])                     # 固定 + 依熱度;沒有訊號的不選
        t = {x["id"]: x for x in th}
        self.assertEqual(set(t["memory"]["auto"]["signals"]), {"surge", "dual", "new"})
        self.assertEqual(t["memory"]["auto"]["heat"], 3)
        self.assertEqual(t["cpo"]["auto"]["heat"], 2)                          # 發酵前 + 美股共振
        self.assertEqual(len(t["cpo"]["_auto_stocks"]), 3)                     # 每題材上限
        self.assertEqual(t["cpo"]["_auto_stocks"][0][1], "My-TW-Coverage CPO")     # 沒有籃子 → Coverage 優先
        self.assertEqual(t["memory"]["_auto_stocks"][0], ("2408", "題材籃子 記憶體"))   # 有籃子 → 核心公司優先
        self.assertIn("金管會", meta["unmatched"])                              # 對不到的詞列出,不發明題材
        self.assertEqual(t["cpo"]["auto"]["streak_days"], 1)

    def test_streak(self):
        A.select_themes(self.d, dt.date(2026, 11, 6))
        dg = json.load(open(os.path.join(self.d, "daily_digest.json"), encoding="utf-8"))
        for day in ("20261106", "20261109"):                                   # 週五、下週一(週末不中斷)
            dg["date"] = day
            self.w("daily_digest.json", dg)
            th, _ = A.select_themes(self.d)
        self.assertEqual({x["id"]: x for x in th}["cpo"]["auto"]["streak_days"], 2)

    def test_match_terms(self):
        tmap = MAP["themes"]
        self.assertEqual(A.match_terms("cpo", tmap), ["cpo"])
        self.assertEqual(A.match_terms("HBM4E 供應", tmap), ["memory"])
        self.assertEqual(A.match_terms("IC", tmap), [])

    def test_ai_notes_validate_and_cache(self):
        os.makedirs(os.path.join(self.d, "prompts"))
        with open(os.path.join(self.d, "prompts", "theme_three_sentences.md"), "w", encoding="utf-8") as f:
            f.write("prompt")
        th, _ = A.select_themes(self.d, dt.date(2026, 11, 9))
        good = {"who_pays": "雲端業者", "bottleneck": "光引擎", "revenue_path": "出貨", "chain": ["雲端", "交換器", "光引擎"], "segment": "光引擎"}
        ch = FakeChain(good)
        notes = A.draft_notes(th, {}, self.d, dt.date(2026, 11, 9), chain=ch)
        self.assertEqual(notes["cpo"]["chain"], "雲端 → 交換器 → 光引擎")
        self.assertEqual(ch.calls, 3)
        ch2 = FakeChain(good)
        A.draft_notes(th, {}, self.d, dt.date(2026, 11, 10), chain=ch2)
        self.assertEqual(ch2.calls, 0)                                         # 輸入沒變、7 天內不重算
        bad = FakeChain(dict(good, who_pays="建議買進"))
        A.draft_notes(th, {}, self.d, dt.date(2026, 11, 20), chain=bad)
        notes = json.load(open(os.path.join(self.d, "theme_page", "ai_notes.json"), encoding="utf-8"))
        self.assertEqual(notes["cpo"]["who_pays"], "雲端業者")                 # 含禁用字眼 → 丟棄,保留舊稿

    def test_auto_kill_append_only(self):
        rows = A.auto_kill({"3081": "已證明", "3163": "只有故事"}, self.d, dt.date(2026, 11, 9))
        self.assertEqual([r["code"] for r in rows], ["3081", "3081"])
        self.assertTrue(all(r["version"] == 0 and r["source"] == "系統預設" for r in rows))
        rows2 = A.auto_kill({"3081": "只有故事", "3163": "開始出現"}, self.d, dt.date(2026, 11, 10))
        self.assertEqual(rows2[:2], rows)                                      # 舊列不變
        self.assertEqual(len(rows2), 4)


class TestLifecycle(unittest.TestCase):
    def px(self, closes, start=dt.date(2026, 8, 1)):
        return [(start + dt.timedelta(days=i), c) for i, c in enumerate(closes)]

    def test_stages(self):
        flat = {c: self.px([100.0] * 60) for c in "ABC"}
        idx = self.px([100.0] * 60)
        auto = {"first_seen": "2026-09-20", "days_on": 3, "heat": 2, "heat_hist": [["d1", 2], ["d2", 2]]}
        lc = A.lifecycle(auto, flat, idx, None, tracking_start="2026-09-01")
        self.assertEqual(lc["stage"], "萌芽")
        lc = A.lifecycle(auto, flat, idx, None, tracking_start="2026-09-20")     # 追蹤開始當天就有 → 不判萌芽
        self.assertEqual((lc["stage"], lc["since_tracking_start"]), ("發酵", True))
        self.assertEqual(A.lifecycle(auto, flat, idx, 95, "2026-09-01")["stage"], "擁擠")
        up = {c: self.px([100.0] * 40 + [100 + 2.0 * i for i in range(1, 21)]) for c in "ABC"}   # 近 20 日 +40%,貼近高點
        self.assertEqual(A.lifecycle(auto, up, idx, None, "2026-09-01")["stage"], "擁擠")
        down = {c: self.px([100.0] * 40 + [100 - 1.0 * i for i in range(1, 21)]) for c in "ABC"}
        cool = dict(auto, heat=1, heat_hist=[["d%d" % i, 3] for i in range(5)] + [["d9", 1]])
        lc = A.lifecycle(cool, down, idx, None, "2026-09-01")
        self.assertEqual((lc["stage"], lc["heat_trend"]), ("降溫", -2.0))
        self.assertEqual(lc["above20"], 0.0)
        self.assertEqual(A.lifecycle(auto, {"A": flat["A"]}, idx)["stage"], "資料不足")
        self.assertEqual(A.lifecycle(auto, down, idx, None, "2026-09-20")["stage"], "回檔中")   # 訊號沒減少、價格回落

    def test_heat_history_saved(self):
        d = tempfile.mkdtemp()
        try:
            for f, o in (("theme_auto_map.json", MAP), ("daily_digest.json", {"date": "20261109", "D_surge_themes": [{"theme": "CPO", "ratio": 2}]}),
                         ("margin_share_themes.json", {"themes": {"CPO": ["1", "2"]}}), ("intl_theme_baskets.json", {"themes": {}}),
                         ("us_tw_resonance.json", {}), ("chip_structure_full.json", {"stocks": {}}), ("consensus.json", {})):
                with open(os.path.join(d, f), "w", encoding="utf-8") as fh:
                    json.dump(o, fh, ensure_ascii=False)
            th, _ = A.select_themes(d, dt.date(2026, 11, 9))
            self.assertEqual(th[0]["auto"]["heat_hist"], [["2026-11-09", 1]] if isinstance(th[0]["auto"]["heat_hist"][0], list) else [("2026-11-09", 1)])
        finally:
            shutil.rmtree(d)


if __name__ == "__main__":
    unittest.main()
