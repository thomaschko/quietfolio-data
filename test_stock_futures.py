# -*- coding: utf-8 -*-
import datetime as dt
import gzip
import os
import shutil
import tempfile
import unittest

import stock_futures as SF
import theme_page_build as B

LISTS = """<table><tr><th>股票期貨、 選擇權 商品代碼</th><th>標的證券</th><th>證券代號</th><th>簡稱</th><th>是否為 股票期貨 標的</th><th>a</th><th>b</th><th>c</th><th>d</th><th>e</th><th>f</th><th>股數</th></tr>
<tr><td>CD</td><td>台積電</td><td>2330</td><td>台積電</td><td>● 是股票期貨標的</td><td></td><td></td><td></td><td></td><td></td><td></td><td>2,000</td></tr>
<tr><td>DC</td><td>某公司</td><td>3008</td><td>某</td><td>● 是股票期貨標的</td><td></td><td></td><td></td><td></td><td></td><td></td><td>2,000</td></tr>
<tr><td>ZZ</td><td>只有選擇權</td><td>9999</td><td>x</td><td></td><td></td><td></td><td></td><td></td><td></td><td></td><td>2,000</td></tr></table>"""
HDR = "交易日期,契約,到期月份(週別),開盤價,最高價,最低價,收盤價,漲跌價,漲跌%,成交量,結算價,未沖銷契約數,最後最佳買價,最後最佳賣價,歷史最高價,歷史最低價,是否因訊息面暫停交易,交易時段,價差\n"


def row(d, c, m, oi, sess="一般"):
    return "%s,%s,%s,1,1,1,1,0,0%%,1,1,%s,1,1,1,1,,%s,\n" % (d, c, m, oi, sess)


class TestSF(unittest.TestCase):
    def test_lists(self):
        l = SF.parse_stock_lists(LISTS)
        self.assertEqual(l, {"CD": ("2330", 2000), "DC": ("3008", 2000)})

    def test_parse_sum_shares(self):
        l = SF.parse_stock_lists(LISTS)
        text = HDR + row("2026/11/11", "CDF", "202611", 100) + row("2026/11/11", "CDF", "202612", 50) \
            + row("2026/11/11", "CDF", "202611/202612", 999) + row("2026/11/11", "CDF", "202611", 7, "盤後") \
            + row("2026/11/11", "DC1", "202611", 20) + row("2026/11/11", "TXF", "202611", 5000)
        agg, kinds = SF.parse_fut_all(text, l)
        self.assertEqual(agg[("2026-11-11", "2330")], 150 * 2000)          # 價差、盤後不計
        self.assertEqual(agg[("2026-11-11", "3008")], 20 * 100)            # 小型契約每口 100 股
        self.assertEqual(kinds, {"standard": 1, "mini": 1, "other": 1})

    def test_theme_futures_crowd(self):
        d = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(d, "stock_futures"))
            rows = ["date,code,oi_shares"]
            for i in range(130):
                day = (dt.date(2026, 1, 1) + dt.timedelta(days=i)).isoformat()
                rows += ["%s,2330,%d" % (day, 1000 if i < 129 else 9000), "%s,3008,%d" % (day, 500)]
            with gzip.open(os.path.join(d, "stock_futures", "oi.csv.gz"), "wt", encoding="utf-8") as f:
                f.write("\n".join(rows) + "\n")
            out = B.theme_futures_crowd({"t": ["2330", "3008", "1101"]}, d)["t"]
            self.assertEqual((out["stock_n"], out["hi_n"]), (2, 1))
            self.assertEqual(out["pctile_median"], round((100 + 50) / 2, 1))
            self.assertEqual(B.theme_futures_crowd({"t": ["2330"]}, tempfile.mkdtemp()), "not_built")
        finally:
            shutil.rmtree(d)


if __name__ == "__main__":
    unittest.main()
