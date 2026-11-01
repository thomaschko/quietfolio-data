# -*- coding: utf-8 -*-
import datetime as dt
import gzip
import csv
import json
import os
import shutil
import tempfile
import unittest

import margin_backfill as mb

DAYS = ["2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30"]


class Resp:
    def __init__(self, status, obj=None, text=None):
        self.status_code = status
        self._obj = obj
        self.text = text if text is not None else json.dumps(obj or {})

    def json(self):
        if self._obj is None:
            raise ValueError("not json")
        return self._obj


def roc(d):
    y, m, dd = d.split("-")
    return "%d/%s/%s" % (int(y) - 1911, m, dd)


class FakeSession:
    """依網址回傳假資料。block_on:網址含此字串時回 429。tpex_close_missing:這幾天上櫃收盤回空表。"""
    def __init__(self, block_on=None, tpex_close_missing=(), twse_date_override=None):
        self.headers = {}
        self.calls = []
        self.block_on = block_on
        self.tpex_close_missing = set(tpex_close_missing)
        self.twse_date_override = twse_date_override or {}

    robots = None   # {host: (status, text)};沒設 → 404

    def get(self, url, timeout=None, allow_redirects=None):
        self.calls.append(url)
        if url.endswith("/robots.txt"):
            for host, (st, txt) in (self.robots or {}).items():
                if host in url:
                    if st in (301, 302) and not allow_redirects:
                        return Resp(st, text="")
                    return Resp(200 if st in (301, 302) else st, text=txt)
            return Resp(404, text="")
        if self.block_on and self.block_on in url:
            return Resp(429, text="too many")
        if "MI_5MINS_HIST" in url:
            ym = url.split("date=")[1][:6]
            data = [[roc(d), "1", "1", "1", str(100 + i)] for i, d in enumerate(DAYS) if d.replace("-", "")[:6] == ym]
            return Resp(200, {"stat": "OK", "data": data})
        if "indexInfo/inx" in url:
            ym = url.split("date=")[1][:7].replace("/", "-")
            data = [[d.replace("-", "/"), "1", "1", "1", str(200 + i), "0"] for i, d in enumerate(DAYS) if d[:7] == ym]
            return Resp(200, {"stat": "ok", "tables": [{"fields": ["日期", "開市", "最高", "最低", "收市", "漲/跌"], "data": data}]})
        if "MI_MARGN" in url:
            d8 = url.split("date=")[1][:8]
            d = "%s-%s-%s" % (d8[:4], d8[4:6], d8[6:])
            rd = self.twse_date_override.get(d, d8)
            return Resp(200, {"stat": "OK", "date": rd, "tables": [
                {"fields": ["項目", "買進", "賣出", "現金(券)償還", "前日餘額", "今日餘額"],
                 "data": [["融資(交易單位)", "1", "1", "0", "1,000", "1,010"], ["融券(交易單位)", "0", "0", "0", "0", "0"],
                          ["融資金額(仟元)", "1", "1", "0", "50,000", "51,000"]]},
                {"fields": ["代號", "名稱", "買進", "賣出", "現金償還", "前日餘額", "今日餘額", "次一營業日限額",
                            "買進", "賣出", "現券償還", "前日餘額", "今日餘額", "次一營業日限額", "資券互抵", "註記"],
                 "data": [["2330", "台積電", "0", "0", "0", "1,000", "1,000", "9", "0", "0", "0", "5", "6", "9", "0", ""],
                          ["0050", "元大台灣50", "0", "0", "0", "10", "10", "9", "0", "0", "0", "0", "0", "9", "0", ""],
                          ["9999", "無價", "0", "0", "0", "3", "3", "9", "0", "0", "0", "0", "0", "9", "0", "OX"]]}]})
        if "MI_INDEX" in url:
            d8 = url.split("date=")[1][:8]
            return Resp(200, {"stat": "OK", "date": d8, "tables": [
                {"fields": ["指數", "收盤指數"], "data": [["加權", "1"]]},
                {"fields": ["證券代號", "證券名稱", "成交股數", "成交筆數", "成交金額", "開盤價", "最高價", "最低價", "收盤價"],
                 "data": [["2330", "台積電", "1", "1", "1", "1", "1", "1", "1,400.00"],
                          ["0050", "元大台灣50", "1", "1", "1", "1", "1", "1", "60.5"],
                          ["9999", "無價", "0", "0", "0", "--", "--", "--", "--"]]}]})
        if "margin/balance" in url:
            d = url.split("date=")[1][:10].replace("/", "-")
            return Resp(200, {"stat": "ok", "date": d.replace("-", ""), "tables": [{
                "fields": ["代號", "名稱", "前資餘額(張)", "資買", "資賣", "現償", "資餘額", "資屬證金"],
                "data": [["6488", "環球晶", "100", "0", "0", "0", "110", "0"], ["00679B", "元大美債20年", "5", "0", "0", "0", "4", "0"]],
                "summary": [["", "合計(張)", "105", "", "", "", "114", ""], ["", "融資金(仟元)", "9", "", "", "", "12,345", ""]]}]})
        if "dailyQuotes" in url:
            d = url.split("date=")[1][:10].replace("/", "-")
            data = [] if d in self.tpex_close_missing else [["6488", "環球晶", "500"], ["00679B", "元大美債20年", "27.5"]]
            return Resp(200, {"stat": "ok", "date": d.replace("-", ""), "tables": [{"fields": ["代號", "名稱", "收盤"], "data": data}]})
        return Resp(404, text="")


def make_http(sess):
    return mb.Http(session=sess, sleep=lambda s: None, clock=lambda: 0.0, gap=0)


class TestBackfill(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.today = dt.date(2026, 11, 1)

    def tearDown(self):
        shutil.rmtree(self.d)

    def rows(self, year="2026"):
        with gzip.open(os.path.join(self.d, year + ".csv.gz"), "rt", encoding="utf-8") as f:
            return list(csv.DictReader(f))

    def csv(self, name):
        with open(os.path.join(self.d, name), encoding="utf-8") as f:
            return list(csv.DictReader(f))

    def test_full_run_types_and_missing(self):
        code, log = mb.run_batch(make_http(FakeSession()), mb.Store(self.d), self.today)
        self.assertEqual(code, 10)
        rows = self.rows()
        self.assertEqual(len(rows), len(DAYS) * 5)           # 上市 3 檔 + 上櫃 2 檔
        r = [x for x in rows if x["code"] == "2330" and x["date"] == "2026-10-30"][0]
        self.assertEqual((r["market"], r["margin_lots"], r["close"]), ("twse", "1000", "1400"))
        self.assertIsInstance(float(r["margin_lots"]), float)
        miss = self.csv("missing.csv")
        self.assertEqual({(m["code"], m["reason"]) for m in miss}, {("9999", "no_close")})   # 缺價不補
        nine = [x for x in rows if x["code"] == "9999"][0]
        self.assertEqual(nine["close"], "")
        off = {(o["date"], o["market"]): o for o in self.csv("official.csv")}
        self.assertEqual(off[("2026-10-30", "twse")]["official_margin_k"], "51000")
        self.assertEqual(off[("2026-10-30", "tpex")]["official_margin_k"], "12345")
        self.assertEqual(off[("2026-10-30", "tpex")]["total_lots"], "114")
        idx = self.csv("index.csv")
        self.assertEqual(len(idx), len(DAYS) * 2)

    def test_resume_no_duplicate_requests_or_rows(self):
        mb.run_batch(make_http(FakeSession()), mb.Store(self.d), self.today, batch_days=2)
        first = len(self.rows())
        s2 = FakeSession()
        code, log = mb.run_batch(make_http(s2), mb.Store(self.d), self.today, batch_days=2)
        self.assertEqual(code, 10)
        fetched = [u for u in s2.calls if "MI_MARGN" in u]
        self.assertEqual(len(fetched), 2)                     # 只抓剩下的 2 天
        self.assertEqual(len(self.rows()), first * 2)
        s3 = FakeSession()
        code, _ = mb.run_batch(make_http(s3), mb.Store(self.d), self.today)
        self.assertEqual(code, 10)
        self.assertFalse([u for u in s3.calls if "MI_MARGN" in u or "margin/balance" in u])
        self.assertEqual(len(self.rows()), len(DAYS) * 5)     # 沒有重複列

    def test_rate_limited_stops_and_keeps_progress(self):
        code, log = mb.run_batch(make_http(FakeSession(block_on="margin/balance?response=json&date=2026/10/29")),
                                 mb.Store(self.d), self.today)
        self.assertEqual(code, 2)
        self.assertIn("429", log["stopped"])
        with open(os.path.join(self.d, "progress.json"), encoding="utf-8") as f:
            prog = json.load(f)
        self.assertIn("2026-10-30", prog["done"]["tpex"])
        self.assertIn("2026-10-29", prog["done"]["twse"])     # 同一天上市已完成的保留
        self.assertNotIn("2026-10-29", prog["done"]["tpex"])

    def test_response_date_mismatch_is_missing_day(self):
        code, _ = mb.run_batch(make_http(FakeSession(twse_date_override={"2026-10-28": "20261030"})),
                               mb.Store(self.d), self.today)
        rows = self.rows()
        self.assertFalse([r for r in rows if r["date"] == "2026-10-28" and r["market"] == "twse"])
        miss = [m for m in self.csv("missing.csv") if m["code"] == "*"]
        self.assertEqual([(m["date"], m["market"]) for m in miss], [("2026-10-28", "twse")])

    def test_whole_day_close_missing_keeps_margin(self):
        mb.run_batch(make_http(FakeSession(tpex_close_missing={"2026-10-29"})), mb.Store(self.d), self.today)
        rows = [r for r in self.rows() if r["date"] == "2026-10-29" and r["market"] == "tpex"]
        self.assertEqual({r["close"] for r in rows}, {""})
        miss = {(m["date"], m["code"], m["reason"]) for m in self.csv("missing.csv")}
        self.assertIn(("2026-10-29", "6488", "no_close"), miss)

    def test_size_limit(self):
        with open(os.path.join(self.d, "big.bin"), "wb") as f:
            f.write(b"0" * 2048)
        code, log = mb.run_batch(make_http(FakeSession()), mb.Store(self.d), self.today, max_bytes=1024)
        self.assertEqual(code, 3)

    def test_merge_dedup_same_key(self):
        p = os.path.join(self.d, "x.csv")
        mb.merge_rows(p, ["date", "market", "v"], ["date", "market"], [("d1", "twse", 1), ("d1", "twse", 2)])
        self.assertEqual(self.csv("x.csv"), [{"date": "d1", "market": "twse", "v": "2"}])

    def test_robots_redirect_followed(self):
        s = FakeSession()
        s.robots = {"www.tpex.org.tw": (301, "User-agent: *\nDisallow: /admin/\n")}
        code, _ = mb.run_batch(make_http(s), mb.Store(self.d), self.today)
        self.assertEqual(code, 10)

    def test_robots_disallow_stops(self):
        s = FakeSession()
        s.robots = {"www.tpex.org.tw": (200, "User-agent: *\nDisallow: /www/\n")}
        code, log = mb.run_batch(make_http(s), mb.Store(self.d), self.today)
        self.assertEqual(code, 2)
        self.assertIn("Disallow: /www/", log["stopped"])

    def test_num(self):
        self.assertEqual(mb.num("1,234.5"), 1234.5)
        self.assertIsNone(mb.num("--"))
        self.assertIsNone(mb.num(""))
        self.assertEqual(mb.roc_date("115/10/01"), "2026-10-01")
        self.assertEqual(mb.roc_date("2026/10/01"), "2026-10-01")


if __name__ == "__main__":
    unittest.main()
