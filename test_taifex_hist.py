# -*- coding: utf-8 -*-
import datetime as dt
import os
import shutil
import tempfile
import unittest

import requests

import taifex_hist as T

FUT_INST = ("日期,商品名稱,身份別,多方交易口數,多方交易契約金額(千元),空方交易口數,空方交易契約金額(千元),多空交易口數淨額,多空交易契約金額淨額(千元),"
            "多方未平倉口數,多方未平倉契約金額(千元),空方未平倉口數,空方未平倉契約金額(千元),多空未平倉口數淨額,多空未平倉契約金額淨額(千元)\n"
            "2026/10/05,微型臺指期貨,自營商,29600,14653025,12533,6228102,17067,8424923,21774,10879724,9114,4574713,12660,6305011\n"
            "2026/10/05,微型臺指期貨,外資及陸資,1000,1,2000,2,-1000,-1,5000,5,8000,8,-3000,-1500000\n")
OPT_INST = ("日期,商品名稱,買賣權別,身份別,買方交易口數,買方交易契約金額(千元),賣方交易口數,賣方交易契約金額(千元),交易口數買賣淨額,交易契約金額買賣淨額(千元),"
            "買方未平倉口數,買方未平倉契約金額(千元),賣方未平倉口數,賣方未平倉契約金額(千元),未平倉口數買賣淨額,未平倉契約金額買賣淨額(千元)\n"
            "2026/10/05,臺指選擇權,CALL,外資及陸資,46390,685096,42294,513495,4096,171601,29248,1936557,20486,1362861,8762,573696\n")
FUT_DATA = ("交易日期,契約,到期月份(週別),開盤價,最高價,最低價,收盤價,漲跌價,漲跌%,成交量,結算價,未沖銷契約數,最後最佳買價,最後最佳賣價,歷史最高價,歷史最低價,是否因訊息面暫停交易,交易時段,價差對單式委託成交量\n"
            "2026/10/05,TMF,202610  ,49489,50098,49460,49954,1285,2.64%,142760,49944,68259,49944,49954,50098,39800,,一般,,\n"
            "2026/10/05,TMF,202611  ,1,1,1,1,1,1%,1,1,1000,1,1,1,1,,一般,,\n"
            "2026/10/05,TMF,202610  ,48692,49490,48680,49350,681,1.40%,195275,-,-,49352,49353,49490,39800,,盤後,,\n"
            "2026/10/05,TMF,202610/202611,0,0,0,0,0,0,10,-,-,0,0,0,0,,一般,,\n")
PCR = ("日期,賣權成交量,買權成交量,買賣權成交量比率%,賣權未平倉量,買權未平倉量,買賣權未平倉量比率%\n"
       "2026/11/03,217195,271541,79.99,76050,159184,47.77,\n")
LARGE = ("日期,商品(契約),商品名稱(契約名稱),到期月份(週別),交易人類別,前五大交易人買方,前五大交易人賣方,前十大交易人買方,前十大交易人賣方,全市場未沖銷部位數\n"
         "2026/10/05,BRF    ,布蘭特原油期貨,202612  ,0,84,36,84,52,84\n"
         "2026/10/05,TX     ,臺股期貨,999999  ,0,30000,40000,45000,55000,120000\n")


class Resp:
    def __init__(self, status=200, text="", raw=None):
        self.status_code = status
        self.text = text
        self.content = raw if raw is not None else text.encode("cp950", errors="replace")


class FakeSession:
    def __init__(self, routes, robots=Resp(200, "<html>404</html>")):
        self.routes, self.robots, self.calls = routes, robots, []
        self.headers = {}

    def request(self, method, url, data=None, timeout=None):
        self.calls.append((method, url, data))
        if url.endswith("robots.txt"):
            return self.robots
        for k, v in self.routes.items():
            if k in url:
                if isinstance(v, Exception):
                    raise v
                return v(data) if callable(v) else v
        return Resp(404, "")


def ok_routes():
    return {"futContractsDateDown": Resp(200, FUT_INST), "callsAndPutsDateDown": Resp(200, OPT_INST),
            "futDataDown": Resp(200, FUT_DATA), "pcRatioDown": Resp(200, PCR), "largeTraderFutDown": Resp(200, LARGE)}


class TestParse(unittest.TestCase):
    def test_fut_inst(self):
        r = T.parse_fut_inst(FUT_INST, "TMF")
        self.assertEqual(r[1], ("2026-10-05", "TMF", "外資及陸資", "5000", "8000", "-3000", "-1500000", "-1000"))

    def test_opt_inst(self):
        self.assertEqual(T.parse_opt_inst(OPT_INST)[0], ("2026-10-05", "CALL", "外資及陸資", "29248", "20486", "8762", "573696"))

    def test_fut_oi_sums_regular_session_months_only(self):
        self.assertEqual(T.parse_fut_oi(FUT_DATA, "TMF"), [("2026-10-05", "TMF", "69259")])   # 盤後、價差列不算

    def test_pcr_and_large(self):
        self.assertEqual(T.parse_pcr(PCR)[0], ("2026-11-03", "217195", "271541", "79.99", "76050", "159184", "47.77"))
        self.assertEqual(T.parse_large(LARGE), [("2026-10-05", "TX", "999999", "0", "30000", "40000", "45000", "55000", "120000")])


class TestRun(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.d)

    def http(self, routes, **kw):
        return T.Http(session=FakeSession(routes, **kw), sleep=lambda s: None, clock=lambda: 0.0)

    def test_full_run_and_resume(self):
        h = self.http(ok_routes())
        code, log = T.run(http=h, out_dir=self.d, today=dt.date(2024, 9, 10))
        self.assertEqual(code, 0)
        self.assertEqual(log["fetched"], ["2024-08", "2024-09"])
        self.assertEqual(len(T.read_csv(os.path.join(self.d, "fut_inst.csv"))), 6)   # 同日同鍵去重:2 列 × 3 商品
        # 下個月再跑:2024-08 已完成且不是最近兩個月 → 不重抓
        h2 = self.http(ok_routes())
        code, log = T.run(http=h2, out_dir=self.d, today=dt.date(2024, 10, 3))
        self.assertEqual(log["fetched"], ["2024-09", "2024-10"])

    def test_blocked_stops(self):
        r = ok_routes()
        r["pcRatioDown"] = Resp(429, "")
        code, log = T.run(http=self.http(r), out_dir=self.d, today=dt.date(2024, 8, 20))
        self.assertEqual(code, 2)
        self.assertIn("429", log["stopped"])

    def test_html_error_page_stops(self):
        r = ok_routes()
        r["pcRatioDown"] = Resp(200, "<!DOCTYPE HTML PUBLIC><html>錯誤</html>")
        code, log = T.run(http=self.http(r), out_dir=self.d, today=dt.date(2024, 8, 20))
        self.assertEqual(code, 2)
        self.assertIn("HTML", log["stopped"])

    def test_robots_disallow(self):
        code, log = T.run(http=self.http(ok_routes(), robots=Resp(200, "User-agent: *\nDisallow: /cht/")),
                          out_dir=self.d, today=dt.date(2024, 8, 20))
        self.assertEqual(code, 2)
        self.assertIn("robots", log["stopped"])

    def test_net_error_retried_once_then_4(self):
        r = ok_routes()
        r["futContractsDateDown"] = requests.ConnectionError("cut")
        h = self.http(r)
        code, log = T.run(http=h, out_dir=self.d, today=dt.date(2024, 8, 20))
        self.assertEqual(code, 4)
        self.assertEqual(h.net_retries, 1)


if __name__ == "__main__":
    unittest.main()
