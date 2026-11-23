# -*- coding: utf-8 -*-
"""反脆弱資料管線單元測試(合成資料,不需網路)。"""
import csv
import datetime as dt
import gzip
import json
import os
import shutil
import tempfile
import unittest

import af_daily as D
import af_fin_data as F
import antifragile_fundamentals as A


def wgz(path, header, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


class TestFinData(unittest.TestCase):
    def test_stale(self):
        t = dt.date(2026, 11, 23)
        self.assertEqual(F.deadlines(t), dt.date(2026, 11, 14))
        self.assertTrue(F.stale("2026-11-10", t))
        self.assertFalse(F.stale("2026-11-15", t))
        self.assertTrue(F.stale(None, t))
        self.assertEqual(F.deadlines(dt.date(2026, 2, 1)), dt.date(2025, 11, 14))

    def test_keep_and_weekly(self):
        rows = [{"date": "2025-03-31", "type": "TotalAssets", "value": 10}, {"date": "2025-03-31", "type": "TotalAssets_per", "value": 1},
                {"date": "2025-03-31", "type": "ShorttermBorrowings", "value": 2}, {"date": "2025-03-31", "type": "Inventories", "value": 3}]
        self.assertEqual([r[2] for r in F.keep_rows("bs", "1101", rows)], ["ShorttermBorrowings", "TotalAssets"])
        per = [{"date": d, "PER": 10, "PBR": 1, "dividend_yield": 2} for d in ("2026-11-16", "2026-11-17", "2026-11-20", "2026-11-23")]
        self.assertEqual([r[1] for r in F.weekly_per("2330", per)], ["2026-11-20", "2026-11-23"])


class TestDaily(unittest.TestCase):
    def test_parsers(self):
        ins = D.parse_insider([
            {"資料年月": "11510", "公司代號": "1101", "職稱": "董事長本人", "目前持股": "1,000", "設質股數": "100"},
            {"資料年月": "11510", "公司代號": "1101", "職稱": "監察人", "目前持股": "500", "設質股數": "0"},
            {"資料年月": "11510", "公司代號": "1101", "職稱": "經理人", "目前持股": "9999", "設質股數": "0"}])
        self.assertEqual(ins[("2026-10", "1101")][:2], [1500.0, 100.0])
        tr = D.parse_transfer([{"出表日期": "1151120", "公司代號": "1540", "申報人身分": "董事本人", "姓名": "甲",
                                "預定轉讓方式及股數-轉讓方式": "一般交易", "預定轉讓總股數-自有持股": "190800"}])
        self.assertEqual(next(iter(tr))[:2], ("2026-11-20", "1540"))
        bb = D.parse_buyback([{"發言日期": "1151120", "公司代號": "2330", "主旨 ": "公告本公司董事會決議買回本公司股份"},
                              {"發言日期": "1151120", "公司代號": "2317", "主旨 ": "公告股利"}])
        self.assertEqual({x[1] for x in bb}, {"2330"})
        self.assertEqual(D.roc_date("115/11/20"), "2026-11-20")

    def test_merge_per(self):
        old = [["2330", "2026-11-16", "20", "7", "1"]]
        new = [("2330", "2026-11-17", 21.0, 7.1, 1.0), ("2330", "2026-11-23", None, 7.2, 1.1)]
        got = D.merge_per(old, new)
        self.assertEqual([r[1] for r in got], ["2026-11-17", "2026-11-23"])
        self.assertEqual(got[1][2], "")


class TestFundamentals(unittest.TestCase):
    def test_discrete_and_tail(self):
        s = [("2025-03-31", 10.0), ("2025-06-30", 25.0), ("2025-09-30", 30.0), ("2025-12-31", 50.0), ("2026-03-31", 5.0)]
        d = A.discrete(s)
        self.assertEqual([v for _, v in d], [10.0, 15.0, 5.0, 20.0, 5.0])
        self.assertEqual(A.consecutive_tail(d, 4), [15.0, 5.0, 20.0, 5.0])
        self.assertIsNone(A.consecutive_tail([("2025-03-31", 1), ("2025-09-30", 1)], 2))   # 缺一季

    def test_pctile_quantiles(self):
        p = A.pctile_map({"a": 1, "b": 2, "c": 2, "d": None})
        self.assertEqual(p["a"], 1 / 3)
        self.assertEqual(p["b"], p["c"])
        self.assertEqual(A.quantiles([0, 10], 2), [0, 5, 10])

    def setUp(self):
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.d)

    def build_fixture(self):
        d = self.d
        wgz(os.path.join(d, "bt_disloc", "universe.csv.gz"), ["code", "name", "market", "industry"],
            [["1111", "好公司", "twse", "電子零組件業"], ["2222", "高負債", "twse", "鋼鐵工業"], ["2882", "金控", "twse", "金融保險業"]])
        A_ = os.path.join(d, "af_data")
        wgz(os.path.join(A_, "company.csv.gz"), ["code", "market", "founded", "listed", "industry_code", "issued_shares"],
            [["1111", "twse", "1990-01-01", "2000-01-01", "24", "1000000"], ["2222", "twse", "2010-01-01", "2015-01-01", "10", "1000000"]])
        bs, inc, cf = [], [], []
        quarters = [f"{y}-{mm}" for y in range(2020, 2027) for mm in ("03-31", "06-30", "09-30", "12-31")][:-1]  # 到 2026Q3
        for c, liab, cash, stb in (("1111", 20, 50, 0), ("2222", 70, 5, 10)):
            for qd in quarters:
                bs += [[c, qd, "TotalAssets", 100], [c, qd, "Liabilities", liab], [c, qd, "CashAndCashEquivalents", cash]]
                if stb:
                    bs.append([c, qd, "ShorttermBorrowings", stb])
                inc += [[c, qd, "Revenue", 100], [c, qd, "GrossProfit", 30], [c, qd, "OperatingIncome", 10], [c, qd, "EPS", 1]]
                k = A.quarter_of(qd)
                cf += [[c, qd, "CashFlowsFromOperatingActivities", 20 * k], [c, qd, "PropertyAndPlantAndEquipment", -5 * k],
                       [c, qd, "InterestExpense", (1 if c == "1111" else 8) * k]]
        h = ["code", "date", "type", "value"]
        wgz(os.path.join(A_, "bs.csv.gz"), h, bs)
        wgz(os.path.join(A_, "is.csv.gz"), h, inc)
        wgz(os.path.join(A_, "cf.csv.gz"), h, cf)
        wgz(os.path.join(A_, "insider_monthly.csv.gz"), ["ym", "code", "dir_shares", "dir_pledged", "n_people"],
            [["2026-10", "1111", 200000, 0, 5], ["2026-10", "2222", 100000, 50000, 5]])
        weeks = [(dt.date(2022, 1, 7) + dt.timedelta(weeks=i)).isoformat() for i in range(200)]
        wgz(os.path.join(A_, "per.csv.gz"), ["code", "date", "per", "pbr", "dy"],
            [["1111", w, 10 + i % 10, 1.5, 3] for i, w in enumerate(weeks)])
        json.dump({"insider_transfer_since": "2026-11-23", "buyback_since": "2026-11-23"}, open(os.path.join(A_, "daily_meta.json"), "w"))

    def test_build(self):
        self.build_fixture()
        out = A.build(self.d, today=dt.date(2026, 11, 23))
        s = out["stocks"]
        self.assertEqual(s["2882"]["label"], "na")
        g, b = s["1111"], s["2222"]
        self.assertFalse(g["fragile"])
        self.assertEqual(g["metrics"]["debt_ratio"], 20.0)
        self.assertEqual(g["metrics"]["net_cash_ratio"], 50.0)
        self.assertEqual(g["metrics"]["fcf_positive_quarters"], 8)
        self.assertEqual(g["metrics"]["interest_coverage"], 10.0)
        self.assertEqual(g["metrics"]["loss_years_5y"], 0)
        self.assertEqual(g["metrics"]["insider_holding"], 20.0)
        self.assertIsNotNone(g["af_score"])
        self.assertIsNotNone(g["valuation_q"]["pe"])
        # 缺漏:持股變化、庫藏股、董監轉讓 → missing,不補零;權重重新分配,coverage < 1
        for k in ("insider_change", "buyback_12m", "insider_exit_pct"):
            self.assertIn(k, g["missing_fields"])
        self.assertLess(g["data_coverage"], 1.0)
        self.assertGreater(g["data_coverage"], 0.7)
        self.assertEqual(set(b["fragile_reasons"]), {"HIGH_DEBT", "HIGH_PLEDGE", "LOW_COVERAGE", "LIQUIDITY_GAP"})
        self.assertIsNone(b["af_score"])
        self.assertTrue(any("未滿" in n for n in out["notes"]))


if __name__ == "__main__":
    unittest.main()
