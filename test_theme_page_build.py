# -*- coding: utf-8 -*-
import datetime as dt
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout

import theme_page_build as B

TPE = dt.timezone(dt.timedelta(hours=8))
NOW = dt.datetime(2026, 11, 9, 22, 0, tzinfo=TPE)


def fake_rows(code, cumulative=False):
    """2023-01 起月營收(年增 10% 並加速)、季損益、現金流(累計)、本益比、收盤價"""
    rev, fs, cf, pe, px = [], [], [], [], []
    for i in range(46):                                   # 2023/01–2026/10
        y, m = 2023 + i // 12, i % 12 + 1
        rev.append({"revenue_year": y, "revenue_month": m, "revenue": 100.0 * (1.1 + 0.002 * i) ** (i / 12), "date": "x"})
    mon = {(r["revenue_year"], r["revenue_month"]): r["revenue"] for r in rev}
    cum_o = cum_c = 0
    acc = {}
    for k in range(14):                                   # 2023Q1–2026Q2
        y, q = 2023 + k // 4, k % 4 + 1
        d = dt.date(y, 3 * q, 30 if q in (2, 3) else 31).isoformat()
        r = sum(mon[(y, 3 * (q - 1) + j)] for j in (1, 2, 3))
        if q == 1:
            cum_o = cum_c = 0
            acc = {}
        single = {"Revenue": r, "GrossProfit": r * (0.40 + 0.005 * k), "OperatingIncome": r * (0.10 + 0.004 * k), "EPS": 1.0}
        for t_, v_ in single.items():
            acc[t_] = acc.get(t_, 0) + v_
            fs.append({"date": d, "type": t_, "value": acc[t_] if cumulative else v_})
        cum_o += r * 0.2
        cum_c += r * 0.05
        cf += [{"date": d, "type": "NetCashInflowFromOperatingActivities", "value": cum_o},
               {"date": d, "type": "PropertyAndPlantAndEquipment", "value": -cum_c}]
    day = dt.date(2023, 11, 1)
    while day <= dt.date(2026, 11, 9):
        if day.weekday() < 5:
            pe.append({"date": day.isoformat(), "PER": 15 + (day.toordinal() % 30) / 3})
            px.append({"date": day.isoformat(), "close": 100 + (day.toordinal() % 50)})
        day += dt.timedelta(days=1)
    return {"TaiwanStockMonthRevenue": rev, "TaiwanStockFinancialStatements": fs, "TaiwanStockCashFlowsStatement": cf,
            "TaiwanStockPER": pe, "TaiwanStockPrice": px}


class FakeFM:
    def __init__(self, cumulative=False, quota_after=None):
        self.used, self.calls, self.cum, self.quota_after = 0, [], cumulative, quota_after

    def get(self, ds, code, start):
        if self.quota_after is not None and self.used >= self.quota_after:
            raise B.Quota("FinMind 超過每小時額度(402)")
        self.used += 1
        self.calls.append((ds, code, start))
        rows = fake_rows(code, self.cum)[ds]
        k = "date" if ds in ("TaiwanStockPER", "TaiwanStockPrice") else None
        return [r for r in rows if not k or r[k] >= start]


class TestBuild(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.d, B.OUT_DIR))
        self.w("theme_page_themes.json", {"themes": [{"id": "cpo", "name": "CPO", "radar_terms": ["CPO"], "us_groups": ["光通訊/CPO"],
                                                      "coverage": ["CPO"], "baskets": ["光通訊/CPO"], "extra": ["2884"]}]})
        self.w("margin_share_themes.json", {"themes": {"CPO": ["3081", "3163"]}})
        self.w("intl_theme_baskets.json", {"themes": {"光通訊/CPO": {"tw": ["3163", "4979"]}}})
        self.w("margin_share_classes.json", {"twse": {"industry": {"2884": "金融保險業"}}, "tpex": {"industry": {"3081": "半導體業"}}})
        self.w("daily_digest.json", {"date": "20261108", "B_preferment_themes": [{"theme": "CPO", "codes": []}]})

    def tearDown(self):
        shutil.rmtree(self.d)

    def w(self, p, o):
        with open(os.path.join(self.d, p), "w", encoding="utf-8") as f:
            json.dump(o, f, ensure_ascii=False)

    def run_(self, fm=None, mops=None):
        idx = [(dt.date(2026, 8, 1) + dt.timedelta(days=i), 20000.0) for i in range(90)]
        with redirect_stdout(io.StringIO()):
            return B.run(base=self.d, fm=fm or FakeFM(), now=NOW, mops=mops or {}, idx_px=idx)

    def theme(self):
        with open(os.path.join(self.d, B.OUT_DIR, "cpo.json"), encoding="utf-8") as f:
            return json.load(f)

    def test_build_outputs(self):
        doc = self.run_()
        t = self.theme()
        codes = [s["code"] for s in t["stocks"]]
        self.assertEqual(sorted(codes), ["2884", "3081", "3163", "4979"])          # 去重
        s = {x["code"]: x for x in t["stocks"]}
        self.assertEqual(s["3081"]["checks"]["q1"]["state"], "g")
        self.assertEqual(s["3081"]["checks"]["q4"]["state"], "g")
        self.assertEqual(s["3081"]["checks"]["q2"]["state"], "n")                  # 沒有手動證據 → ○
        self.assertEqual(s["2884"]["checks"]["q4"]["state"], "na")                 # 金融業
        self.assertTrue(s["2884"]["checks"]["q4"]["alt"])
        self.assertEqual(s["3081"]["tier"]["tier"], "已證明")
        self.assertIsNotNone(s["3081"]["pos"])
        self.assertEqual(t["evidence"]["edgar_capex"], "not_built")
        self.assertEqual(t["evidence"]["us"], "not_built")                          # 沒有 us_tw_resonance.json
        self.assertTrue(t["evidence"]["has"])                                       # 雷達命中 CPO
        for fn in os.listdir(os.path.join(self.d, B.OUT_DIR)):
            if fn.endswith(".json"):
                with open(os.path.join(self.d, B.OUT_DIR, fn), encoding="utf-8") as f:
                    self.assertLessEqual(len(f.read()), 45000)
        self.assertEqual(doc["themes"][0]["tiers"].get("已證明"), 3)
        for w in ("買進", "賣出", "目標價", "推薦"):
            self.assertNotIn(w, json.dumps(t, ensure_ascii=False))

    def test_cache_avoids_refetch(self):
        self.run_()
        fm = FakeFM()
        self.run_(fm)
        self.assertFalse([c for c in fm.calls if c[0] in ("TaiwanStockMonthRevenue", "TaiwanStockFinancialStatements")])
        self.assertFalse([c for c in fm.calls if c[0] == "TaiwanStockPER"])        # 同一天不重抓

    def test_cumulative_income_detected(self):
        self.run_(FakeFM(cumulative=True))
        s = {x["code"]: x for x in self.theme()["stocks"]}
        self.assertTrue(s["3081"]["income_cumulative"])
        self.assertEqual(s["3081"]["checks"]["q4"]["state"], "g")                  # 換算單季後仍正確

    def test_quota_uses_cache_and_continues(self):
        self.run_(FakeFM(quota_after=3))
        with open(os.path.join(self.d, B.OUT_DIR, "index.json"), encoding="utf-8") as f:
            doc = json.load(f)
        self.assertIn("402", doc["log"]["stopped"])
        self.assertEqual(len(self.theme()["stocks"]), 4)                            # 抓不到的標資料不足,不中斷

    def test_mops_supplement_unit_check(self):
        cache = {"rev": [[2026, 7, 100.0], [2026, 8, 100.0], [2026, 9, 100.0]], "fin": []}
        ok = B.mops_supplement(cache, "1", {"1": {"year": 2026, "q": 3, "revenue": 0.3, "gross": 0.12, "op": 0.03, "eps": 1}})
        self.assertEqual(ok, "added")
        self.assertAlmostEqual(cache["fin"][0][2], 300.0)
        cache = {"rev": [[2026, 7, 100.0], [2026, 8, 100.0], [2026, 9, 100.0]], "fin": []}
        bad = B.mops_supplement(cache, "1", {"1": {"year": 2026, "q": 3, "revenue": 300.0, "gross": 1, "op": 1, "eps": 1}})
        self.assertTrue(bad.startswith("unit_mismatch"))
        self.assertEqual(cache["fin"], [])

    def test_missing_token_stops(self):
        old = os.environ.pop("FINMIND_TOKEN", None)
        try:
            with self.assertRaises(SystemExit):
                B.run(base=self.d, now=NOW, mops={}, idx_px=[])
        finally:
            if old is not None:
                os.environ["FINMIND_TOKEN"] = old

    def test_paging(self):
        big = {"id": "x", "name": "x"}
        stocks = [{"code": "%04d" % i, "pad": "z" * 3000} for i in range(40)]
        files = B.write_theme(big, {"stocks": stocks}, self.d)
        self.assertGreater(len(files), 1)
        n = 0
        for fn in files:
            with open(os.path.join(self.d, B.OUT_DIR, fn), encoding="utf-8") as f:
                txt = f.read()
            self.assertLessEqual(len(txt), 45000)
            n += len(json.loads(txt)["stocks"])
        self.assertEqual(n, 40)


if __name__ == "__main__":
    unittest.main()
