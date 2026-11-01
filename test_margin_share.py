# -*- coding: utf-8 -*-
import datetime as dt
import os
import shutil
import tempfile
import unittest

import margin_backfill as mb
import margin_share as M

ISIN_HTML = """<table>
<tr><td>有價證券代號及名稱</td><td>ISIN</td><td>上市日</td><td>市場別</td><td>產業別</td><td>CFI</td><td>備註</td></tr>
<tr><td><b> 股票</b></td><td></td><td></td><td></td><td></td><td></td><td></td></tr>
<tr><td>2330　台積電</td><td>TW0002330008</td><td>1994/09/05</td><td>上市</td><td>半導體業</td><td>ESVUFR</td><td></td></tr>
<tr><td>1101　台泥</td><td>TW0001101004</td><td>1962/02/09</td><td>上市</td><td>水泥工業</td><td>ESVUFR</td><td></td></tr>
<tr><td><b> ETF</b></td><td></td><td></td><td></td><td></td><td></td><td></td></tr>
<tr><td>0050　元大台灣50</td><td>TW0000050004</td><td>2003/06/30</td><td>上市</td><td></td><td>CEOGEU</td><td></td></tr>
</table>"""


class FakeHttp(mb.Http):
    def __init__(self, responses):
        super().__init__(sleep=lambda s: None, clock=lambda: 0.0)
        self.responses = responses
        self.urls = []

    def json(self, url):
        self.urls.append(url)
        for k, v in self.responses.items():
            if k in url:
                if isinstance(v, Exception):
                    raise v
                return v
        raise mb.Blocked("unexpected " + url)


def md_doc(date="20261030", tpex=True):
    d = {"date": date, "twse": {"2330": [10, 9, 0, ""], "1101": [5, 5, 0, "OX"], "0050": [2, 2, 0, ""]},
         "twse_summary": {"fields": ["項目", "買進", "賣出", "現金(券)償還", "前日餘額", "今日餘額"],
                          "data": [["融資(交易單位)", "1", "1", "0", "17", "17"], ["融資金額(仟元)", "1", "1", "0", "9", "9,000"]]},
         "names": {"2330": "台積電", "1101": "台泥", "0050": "元大台灣50"}}
    if tpex:
        d["tpex"] = {"6488": [4, 4, 0, ""]}
        d["tpex_date"] = date
    return d


def twse_close(date="20261030"):
    return {"date": date, "tables": [{"fields": ["證券代號", "收盤價"], "data": [["2330", "1,000"], ["1101", "30"], ["0050", "100"]]}]}


def tpex_close(date="20261030"):
    return {"date": date, "tables": [{"fields": ["代號", "收盤"], "data": [["6488", "500"]]}]}


def tpex_margin(date="20261030"):
    return {"date": date, "tables": [{"fields": ["代號", "名稱", "資餘額"], "data": [["6488", "x", "4"]],
                                      "summary": [["合計(張)", "", "4"], ["融資金(仟元)", "", "1,000"]]}]}


class TestParse(unittest.TestCase):
    def test_isin(self):
        ind, etf, names = M.classes_from_isin(M.parse_isin(ISIN_HTML))
        self.assertEqual(ind, {"2330": "半導體業", "1101": "水泥工業"})
        self.assertEqual(etf, ["0050"])
        self.assertEqual(names["2330"], "台積電")

    def test_theme_md(self):
        t = "## 上游\n- **2330 台積電** (x)\n- **3661 世芯-KY** (y)\n- **2330 台積電** again"
        self.assertEqual(M.parse_theme_md(t), ["2330", "3661"])


class TestIngest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.d)

    def test_ingest_ok_and_idempotent(self):
        http = FakeHttp({"MI_INDEX": twse_close(), "margin/balance": tpex_margin(), "dailyQuotes": tpex_close()})
        store = mb.Store(self.d)
        log = M.ingest_daily(http, store, md_doc())
        self.assertEqual((log["twse"], log["tpex"]), ("ok", "ok"))
        rows = mb.read_csv(os.path.join(self.d, "2026.csv.gz"), gz=True)
        self.assertEqual(len(rows), 4)
        off = {r["market"]: r for r in mb.read_csv(os.path.join(self.d, "official.csv"))}
        self.assertEqual(off["twse"]["official_margin_k"], "9000")
        self.assertEqual(off["tpex"]["official_margin_k"], "1000")
        log = M.ingest_daily(http, mb.Store(self.d), md_doc())
        self.assertEqual(log["twse"], "already")

    def test_close_not_ready_is_not_written(self):
        http = FakeHttp({"MI_INDEX": twse_close("20261029"), "margin/balance": tpex_margin(), "dailyQuotes": tpex_close()})
        store = mb.Store(self.d)
        log = M.ingest_daily(http, store, md_doc())
        self.assertEqual(log["twse"], "no_close_yet")
        self.assertFalse(store.done("twse", "2026-10-30"))
        self.assertTrue(store.done("tpex", "2026-10-30"))

    def test_tpex_date_mismatch_skipped(self):
        doc = md_doc()
        doc["tpex_date"] = "20261029"
        log = M.ingest_daily(FakeHttp({"MI_INDEX": twse_close()}), mb.Store(self.d), doc)
        self.assertNotIn("tpex", log)

    def test_blocked_stops(self):
        log = M.ingest_daily(FakeHttp({"MI_INDEX": mb.Blocked("HTTP 429")}), mb.Store(self.d), md_doc())
        self.assertTrue(log["twse"].startswith("stopped"))
        self.assertNotIn("tpex", log)


class TestRun(unittest.TestCase):
    """小型合成歷史:150 個交易日,檢查三個輸出檔與保留舊檔"""

    def setUp(self):
        self.cwd = os.getcwd()
        self.d = tempfile.mkdtemp()
        os.chdir(self.d)
        os.makedirs("margin_hist")
        os.makedirs("margin_daily")
        dates = [(dt.date(2026, 1, 1) + dt.timedelta(days=i)).isoformat() for i in range(150)]
        rows, idx, off, done = [], [], [], {"twse": {}, "tpex": {}}
        for i, d in enumerate(dates):
            rows += [(d, "twse", "2330", 100 + i, 1000), (d, "twse", "1101", 50, 30), (d, "twse", "0050", 20, 100),
                     (d, "tpex", "6488", 10, 500)]
            ret = -3.0 if i in (60, 100) else (0.3 if i % 2 else -0.3)
            idx += [(d, m, 100 * (1 + ret / 100) ** 1) for m in ("twse", "tpex")]
            off += [(d, "twse", 170, 100000), (d, "tpex", 10, 3000)]
            done["twse"][d] = done["tpex"][d] = "ok"
        # 指數改成累乘,讓 index_returns 還原出上面的報酬
        lvl, idx2 = {"twse": 100.0, "tpex": 100.0}, []
        for i, d in enumerate(dates):
            ret = -3.0 if i in (60, 100) else (0.3 if i % 2 else -0.3)
            for m in lvl:
                lvl[m] *= 1 + ret / 100
                idx2.append((d, m, round(lvl[m], 4)))
        mb.write_csv("margin_hist/2026.csv.gz", ["date", "market", "code", "margin_lots", "close"], rows, gz=True)
        mb.write_csv("margin_hist/index.csv", ["date", "market", "close"], idx2)
        mb.write_csv("margin_hist/official.csv", ["date", "market", "total_lots", "official_margin_k"], off)
        mb._write_json("margin_hist/progress.json", {"done": done, "batches": [], "index_months": []})
        cls = {"asof": "2026-11-01", "source": "test",
               "twse": {"industry": {"2330": "半導體業", "1101": "水泥工業"}, "etf": ["0050"], "names": {"2330": "台積電"}},
               "tpex": {"industry": {"6488": "半導體業"}, "etf": [], "names": {}}}
        mb._write_json(M.CLASS_CACHE, cls)
        mb._write_json(M.THEME_CACHE, {"themes": {"AI": ["2330", "6488"]}, "source": "test"})
        self.http = FakeHttp({})

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.d)

    def test_outputs(self):
        code, log = M.run(http=self.http, today=dt.date(2026, 11, 1), gap_fill=0)
        self.assertEqual(code, 0)
        main = mb._read_json(M.OUT, None)
        tw = main["markets"]["twse"]
        self.assertEqual(tw["history_level"], "low")
        self.assertAlmostEqual(sum(r["share_pct"] for r in tw["industry"]), 100, places=1)
        self.assertEqual({r["name"] for r in tw["industry"]}, {"半導體業", "水泥工業", "ETF"})
        semi = [r for r in tw["industry"] if r["name"] == "半導體業"][0]
        self.assertGreater(semi["d5_pp"], 0)
        self.assertIsNotNone(semi["share_pctile"])
        self.assertEqual(tw["recon_ratio"], 2.525)     # (249×1000×1000 + 50×1000×30 + 20×1000×100) ÷ (100000 仟元)
        ev = mb._read_json(M.EV_OUT, None)["markets"]["twse"]
        self.assertEqual([e["date"] for e in ev["events"]], ["2026-03-02", "2026-04-11"])
        self.assertIsNone(ev["stats"])          # 事件數 < 10
        hist = mb._read_json(M.HIST_OUT, None)
        self.assertEqual(hist["step_days"], 1)
        self.assertEqual(len(hist["markets"]["twse"]["dates"]), 150)
        for s in (M.OUT, M.HIST_OUT, M.EV_OUT):
            with open(s, encoding="utf-8") as f:
                self.assertLessEqual(len(f.read()), M.SIZE_LIMIT)
        with open(M.OUT, encoding="utf-8") as f:
            text = f.read()
        for w in ("過熱", "危險", "主力", "看多", "看空"):
            self.assertNotIn(w, text)

    def test_oversize_keeps_old_files(self):
        for p in (M.OUT, M.HIST_OUT, M.EV_OUT):
            with open(p, "w") as f:
                f.write("OLD")
        old = M.SIZE_LIMIT
        M.SIZE_LIMIT = 100
        try:
            code, log = M.run(http=self.http, today=dt.date(2026, 11, 1), gap_fill=0)
        finally:
            M.SIZE_LIMIT = old
        self.assertEqual(code, 1)
        for p in (M.OUT, M.HIST_OUT, M.EV_OUT):
            with open(p) as f:
                self.assertEqual(f.read(), "OLD")


if __name__ == "__main__":
    unittest.main()
