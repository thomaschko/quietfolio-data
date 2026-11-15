# -*- coding: utf-8 -*-
import json
import os
import shutil
import tempfile
import unittest

import disloc_bt_data as D


class Resp:
    def __init__(self, code, data=None):
        self.status_code, self._d = code, data

    def json(self):
        return {"status": 200, "data": self._d}


class FakeSession:
    def __init__(self, fail=()):
        self.calls, self.fail = [], set(fail)

    def get(self, url, params=None, headers=None, timeout=None):
        ds, c = params["dataset"], params.get("data_id")
        self.calls.append((ds, c))
        if (ds, c) in self.fail:
            return Resp(500)
        if ds == "TaiwanStockInfo":
            rows = [{"stock_id": "%04d" % (1100 + i), "stock_name": "s%d" % i, "type": "twse", "industry_category": "水泥"} for i in range(1000)]
            rows += [{"stock_id": "0050", "type": "twse", "industry_category": "ETF"}, {"stock_id": "2330", "type": "twse", "industry_category": "ETF"},
                     {"stock_id": "6669", "type": "emerging", "industry_category": "x"}, {"stock_id": "2317", "type": "tpex", "industry_category": "電子"}]
            return Resp(200, rows)
        if ds == "TaiwanStockMonthRevenue":
            return Resp(200, [{"revenue_year": 2024, "revenue_month": 1, "revenue": 10}, {"revenue_year": 2024, "revenue_month": 2, "revenue": "x"}])
        if ds == "TaiwanStockFinancialStatements":
            return Resp(200, [{"date": "2024-03-31", "type": "Revenue", "value": 100}, {"date": "2024-03-31", "type": "GrossProfit", "value": 30},
                              {"date": "2024-03-31", "type": "EPS", "value": 1}])
        return Resp(200, [{"date": "2024-03-31", "type": "AcquisitionOfPropertyPlantAndEquipment", "value": -5},
                          {"date": "2024-06-30", "type": "Other", "value": 1}])


class T(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.t = [0.0]

    def tearDown(self):
        shutil.rmtree(self.d)

    def client(self, sess):
        def sleep(s):
            self.t[0] += s
        return D.Client("tok", gap=1.0, session=sess, sleep=sleep, clock=lambda: self.t[0])

    def test_parsers(self):
        self.assertEqual(D.parse_rev("1", [{"revenue_year": "2024", "revenue_month": "3", "revenue": "5"}]), [["1", 2024, 3, 5.0]])
        self.assertEqual(D.parse_fin("1", [{"date": "d", "type": "GrossProfit", "value": 2}]), [["1", "d", None, 2.0]])
        self.assertEqual(D.parse_cf("1", [{"date": "d", "type": "PurchaseOfPropertyPlantAndEquipment", "value": -1},
                                          {"date": "d", "type": "PropertyAndPlantAndEquipment", "value": -2}]), [["1", "d", -2.0]])   # 科目順序同 GAS

    def test_run_resume(self):
        sess = FakeSession(fail={("TaiwanStockCashFlowsStatement", "1101")})
        cl = self.client(sess)
        log = D.run(cl, self.d, max_minutes=0.5, clock=lambda: self.t[0], save_every=5)   # 0.5 分鐘 → 只做部分
        self.assertEqual(log["stopped"], "時間上限")
        uni = D.load_rows("universe", self.d)
        self.assertEqual(len(uni), 1001)                                           # 排除 ETF、興櫃
        self.assertIn(["2317", "", "tpex", "電子"], uni)
        prog = json.load(open(os.path.join(self.d, "progress.json")))
        self.assertEqual(prog["done"]["1101"], ["fin", "rev"])                     # cf 失敗 → 下次再試
        n1 = log["complete"]
        sess.fail.clear()
        log2 = D.run(cl, self.d, max_minutes=10 ** 6, clock=lambda: self.t[0])
        self.assertEqual((log2["complete"], log2["universe"]), (1001, 1001))
        self.assertGreater(log2["complete"], n1)
        self.assertEqual(len([c for c in sess.calls if c == ("TaiwanStockMonthRevenue", "1101")]), 1)   # 已完成的不重抓
        self.assertEqual(len([c for c in sess.calls if c == ("TaiwanStockCashFlowsStatement", "1101")]), 2)
        self.assertEqual(len(D.load_rows("rev", self.d)), 1001)
        self.assertEqual(D.run(cl, self.d)["todo_before"], 0)                      # 全部完成後直接結束

    def test_quota(self):
        class Q(FakeSession):
            def get(self, *a, **k):
                return Resp(402)
        cl = self.client(Q())
        with self.assertRaises(D.Quota):
            cl.get("TaiwanStockInfo")
        self.assertGreaterEqual(self.t[0], 1800)                                   # 每次 402 等 15 分鐘


if __name__ == "__main__":
    unittest.main()
