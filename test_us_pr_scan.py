# -*- coding: utf-8 -*-
"""edgar_http / us_universe / us_pr_scan 測試(不連網)"""
import datetime as dt
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout

import requests

import edgar_http as eh
import us_pr_scan as P
import us_universe as UV

TERMS = {"先進封裝": ["CoWoS", "CPO"], "記憶體": ["HBM"]}
PR_BODY = ("<html><body><p>Record revenue. Demand for CoWoS and HBM remains strong; HBM supply is tight.</p>"
           "<p>We do not count cpo in lowercase. SECRET_FULL_TEXT_MARKER " + "filler " * 60 + "</p></body></html>")
IDX_HDR = "Form Type   Company Name   CIK   Date Filed   File Name\n" + "-" * 80 + "\n"


class Resp:
    def __init__(self, code=200, text="", obj=None):
        self.status_code, self.text = code, text if obj is None else json.dumps(obj)
        self.content = self.text.encode()
        self._obj = obj

    def json(self):
        return self._obj if self._obj is not None else json.loads(self.text)


class FakeSession:
    def __init__(self, seq):
        self.seq = list(seq)
        self.urls = []
        self.headers = []

    def get(self, url, headers=None, timeout=None):
        self.urls.append(url)
        self.headers.append(headers)
        x = self.seq.pop(0) if self.seq else Resp(200, "")
        if isinstance(x, Exception):
            raise x
        return x


class FakeEdgar:
    """url → Resp;未登錄的 → 404"""
    def __init__(self, routes):
        self.routes = routes
        self.urls = []
        self.count = 0

    def get(self, url):
        self.urls.append(url)
        self.count += 1
        r = self.routes.get(url)
        if isinstance(r, Exception):
            raise r
        return r or Resp(404, "")


class TestEdgarHttp(unittest.TestCase):
    def test_missing_ua(self):
        old = os.environ.pop("EDGAR_USER_AGENT", None)
        try:
            with self.assertRaises(eh.MissingUA):
                eh.Edgar()
        finally:
            if old is not None:
                os.environ["EDGAR_USER_AGENT"] = old

    def test_backoff_then_abort(self):
        s = FakeSession([Resp(200, "")] + [Resp(403)] * 3)     # robots.txt 後連續 403
        sleeps = []
        e = eh.Edgar(ua="t x", session=s, sleep=sleeps.append, clock=lambda: 0)
        with self.assertRaises(eh.EdgarAbort):
            e.get("https://www.sec.gov/a")
        self.assertIn(1, sleeps)
        self.assertIn(2, sleeps)

    def test_recover_and_rate_gap(self):
        s = FakeSession([Resp(200, ""), Resp(429), Resp(200, "ok"), Resp(200, "ok2")])
        sleeps = []
        e = eh.Edgar(ua="t x", session=s, sleep=sleeps.append, clock=lambda: 0)
        self.assertEqual(e.get("https://www.sec.gov/a").text, "ok")
        self.assertEqual(e.get("https://www.sec.gov/b").text, "ok2")
        self.assertEqual(e.fail, 0)
        self.assertIn(eh.MIN_GAP, sleeps)                        # 時鐘不動 → 每次都等滿間隔
        self.assertTrue(all(h["User-Agent"] == "t x" for h in s.headers))

    def test_connection_errors_abort(self):
        s = FakeSession([Resp(200, "")] + [requests.ConnectionError("x")] * 3)
        e = eh.Edgar(ua="t x", session=s, sleep=lambda x: None, clock=lambda: 0)
        with self.assertRaises(eh.EdgarAbort):
            e.get("https://www.sec.gov/a")

    def test_robots_disallow(self):
        s = FakeSession([Resp(200, "User-agent: *\nDisallow: /private/\n")])
        e = eh.Edgar(ua="t x", session=s, sleep=lambda x: None, clock=lambda: 0)
        with self.assertRaises(eh.EdgarAbort):
            e.get("https://www.sec.gov/private/x")


class TestUniverse(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        with open(os.path.join(self.d, "earnings_keywords.py"), "w", encoding="utf-8") as f:
            f.write("COMPANIES = {'ASX': 'adr'}\nKEYWORDS = {}\n")
        with open(os.path.join(self.d, "us_seed_tickers.txt"), "w", encoding="utf-8") as f:
            f.write("ZZZ  # seed\n")

    def tearDown(self):
        shutil.rmtree(self.d)

    def test_build(self):
        tick = {"fields": ["cik", "name", "ticker", "exchange"],
                "data": [[1, "Chip Co", "CHP", "Nasdaq"], [2, "Bank", "BNK", "NYSE"], [3, "ADR Co", "ASX", "OTC"],
                         [4, "Seed Co", "ZZZ", "NYSE"], [5, "Otc Chip", "OCH", "OTC"]]}
        subs = {1: ("3674", "SEMICONDUCTORS & RELATED DEVICES"), 2: ("6022", "STATE COMMERCIAL BANKS"),
                3: ("3674", "SEMICONDUCTORS & RELATED DEVICES"), 4: ("7372", "SERVICES-PREPACKAGED SOFTWARE")}
        routes = {UV.TICKERS_URL: Resp(obj=tick)}
        for c, (sic, desc) in subs.items():
            routes[UV.SUB_URL % c] = Resp(obj={"sic": sic, "sicDescription": desc})
        ed = FakeEdgar(routes)
        res = UV.build(ed, base=self.d, now=dt.datetime(2026, 11, 8, tzinfo=dt.timezone.utc))
        got = {r[1]: r[5] for r in res["companies"]}
        self.assertEqual(got, {"CHP": "sic", "ASX": "core", "ZZZ": "seed"})   # OTC 晶片(OCH)不收,銀行不收
        self.assertEqual(res["sic_report"]["3674"]["n"], 1)
        self.assertEqual(res["sic_report"]["3674"]["desc"], "SEMICONDUCTORS & RELATED DEVICES")
        self.assertIsNone(res["sic_report"]["3827"]["desc"])
        n1 = len(ed.urls)
        UV.build(ed, base=self.d, now=dt.datetime(2026, 11, 9, tzinfo=dt.timezone.utc))
        self.assertEqual(len(ed.urls) - n1, 1)                   # 快取:第二次只抓代號清單

    def test_time_budget_resume(self):
        tick = {"fields": ["cik", "name", "ticker", "exchange"], "data": [[i, "C%d" % i, "T%d" % i, "NYSE"] for i in range(1, 6)]}
        routes = {UV.TICKERS_URL: Resp(obj=tick)}
        for i in range(1, 6):
            routes[UV.SUB_URL % i] = Resp(obj={"sic": "3674", "sicDescription": "S"})
        t = [0]

        def clock():
            t[0] += 20 * 60                                      # 每次 20 分鐘 → 第 2 家就超過 40 分鐘
            return t[0]
        res = UV.build(FakeEdgar(routes), base=self.d, clock=clock)
        self.assertTrue(res["stopped"])
        self.assertGreater(res["pending_sic_lookup"], 0)
        res = UV.build(FakeEdgar(routes), base=self.d)
        self.assertEqual(res["pending_sic_lookup"], 0)
        self.assertEqual(res["n"], 5)


def idx_line(form, name, cik, date, acc):
    return "%-12s%-62s%-12d%-12s%s\n" % (form, name, cik, date, "edgar/data/%d/%s.txt" % (cik, acc))


def filing_index(cik, acc, docs):
    rows = "".join('<tr><td>%d</td><td>d</td><td><a href="/Archives/edgar/data/%d/%s/%s">%s</a></td><td>%s</td><td>1</td></tr>'
                   % (i + 1, cik, acc.replace("-", ""), n, n, t) for i, (n, t) in enumerate(docs))
    return '<table class="tableFile"><tr><th>Seq</th><th>Description</th><th>Document</th><th>Type</th><th>Size</th></tr>%s</table>' % rows


class TestScan(unittest.TestCase):
    NOW = dt.datetime(2026, 11, 7, 3, 30, tzinfo=dt.timezone.utc)   # 週六;昨天 11/6 週五

    def setUp(self):
        self.d = tempfile.mkdtemp()
        with open(os.path.join(self.d, "earnings_keywords.py"), "w", encoding="utf-8") as f:
            f.write("COMPANIES = {}\nKEYWORDS = %r\n" % TERMS)
        with open(os.path.join(self.d, P.UNIVERSE), "w", encoding="utf-8") as f:
            json.dump({"companies": [[100, "AAA", "A Co", "3674", "Nasdaq", "sic"], [200, "BBB", "B Co", "3674", "NYSE", "sic"]]}, f)
        with open(os.path.join(self.d, P.STATE), "w", encoding="utf-8") as f:
            json.dump({"watermark": "2026-11-05", "processed": {}, "backlog": [], "runs": []}, f)

    def tearDown(self):
        shutil.rmtree(self.d)

    def routes(self):
        a1, a2, b1 = "0000000100-26-000001", "0000000100-26-000002", "0000000200-26-000009"
        idx = IDX_HDR + idx_line("8-K", "A Co", 100, "20261106", a1) + idx_line("8-K", "A Co", 100, "20261106", a2) \
            + idx_line("8-K", "B Co", 200, "20261106", b1) + idx_line("8-K", "Other", 999, "20261106", "0000000999-26-000001") \
            + idx_line("10-Q", "A Co", 100, "20261106", "0000000100-26-000003")
        r = {P.IDX_URL % (2026, 4, "20261106"): Resp(200, idx),
             P.SUB_URL % 100: Resp(obj={"filings": {"recent": {"accessionNumber": [a2, a1], "items": ["5.02", "2.02,9.01"]}}}),
             P.SUB_URL % 200: Resp(obj={"filings": {"recent": {"accessionNumber": [b1], "items": ["2.02,9.01"]}}}),
             P.FILING_INDEX % (100, a1.replace("-", ""), a1): Resp(200, filing_index(100, a1, [("a8k.htm", "8-K"), ("q3pr.htm", "EX-99.1")])),
             P.FILING_INDEX % (200, b1.replace("-", ""), b1): Resp(200, filing_index(200, b1, [("b8k.htm", "8-K"), ("pr.pdf", "EX-99.1")])),
             P.DOC_URL % (100, a1.replace("-", ""), "q3pr.htm"): Resp(200, PR_BODY)}
        return r, (a1, a2, b1)

    def run_(self, ed, now=None, clock=None):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code, log = P.run(edgar=ed, base=self.d, now=now or self.NOW, **({"clock": clock} if clock else {}))
        return code, log, buf.getvalue()

    def out(self):
        txt = ""
        for fn in sorted(os.listdir(os.path.join(self.d, P.DAY_DIR))):
            with open(os.path.join(self.d, P.DAY_DIR, fn), encoding="utf-8") as f:
                txt += f.read()
        with open(os.path.join(self.d, P.OUT), encoding="utf-8") as f:
            return txt + f.read()

    def items(self):
        out = []
        for fn in sorted(os.listdir(os.path.join(self.d, P.DAY_DIR))):
            with open(os.path.join(self.d, P.DAY_DIR, fn), encoding="utf-8") as f:
                out += P.decode_day(json.load(f))
        return out

    def summary(self):
        with open(os.path.join(self.d, P.OUT), encoding="utf-8") as f:
            return json.load(f)

    def state(self):
        with open(os.path.join(self.d, P.STATE), encoding="utf-8") as f:
            return json.load(f)

    def test_scan_202_only_and_no_fulltext(self):
        routes, (a1, a2, b1) = self.routes()
        ed = FakeEdgar(routes)
        code, log, printed = self.run_(ed)
        self.assertEqual(code, 0)
        accs = {x["acc"]: x for x in self.items()}
        self.assertEqual(set(accs), {a1, b1})                    # a2 是 5.02 → 不收;CIK 999 與 10-Q 不看
        hits = {h[0]: h for h in accs[a1]["hits"]}
        self.assertEqual(hits["記憶體|HBM"][1], 2)
        self.assertEqual(hits["先進封裝|CoWoS"][1], 1)
        self.assertNotIn("先進封裝|CPO", hits)                   # 小寫 cpo 不算
        sn = accs[a1]["sn"]
        self.assertEqual([t for t, _ in sn], ["記憶體|HBM", "先進封裝|CoWoS"])   # 次數多的詞優先,每份最多 2 段
        self.assertTrue(all(len(x) <= 200 for _, x in sn))
        agg = {r["term"]: r for r in self.summary()["aggregate"]}
        self.assertEqual((agg["記憶體|HBM"]["n30"], agg["記憶體|HBM"]["tickers30"]), (1, ["AAA"]))
        self.assertEqual(self.summary()["days"], [["2026-11-06", 2]])
        self.assertEqual(accs[b1]["status"], "unparsed")         # PDF
        self.assertNotIn("filler filler filler filler filler filler filler filler filler", self.out())
        self.assertNotIn("SECRET_FULL_TEXT_MARKER", printed)      # log 只有計數
        self.assertEqual(self.state()["watermark"], "2026-11-06")
        self.assertEqual(sum(1 for u in ed.urls if "submissions" in u), 2)   # 每家 submissions 只抓一次

    def test_dedupe_rerun(self):
        routes, _ = self.routes()
        self.run_(FakeEdgar(routes))
        st = self.state()
        st["watermark"] = "2026-11-05"                           # 模擬重讀同一天
        with open(os.path.join(self.d, P.STATE), "w", encoding="utf-8") as f:
            json.dump(st, f)
        ed = FakeEdgar(routes)
        self.run_(ed)
        self.assertFalse([u for u in ed.urls if "submissions" in u])
        self.assertEqual(len(self.items()), 2)

    def test_unpublished_index_keeps_watermark(self):
        routes, _ = self.routes()
        del routes[P.IDX_URL % (2026, 4, "20261106")]
        code, log, _ = self.run_(FakeEdgar(routes))
        self.assertEqual(self.state()["watermark"], "2026-11-05")

    def test_time_budget_backlog_then_resume(self):
        routes, (a1, a2, b1) = self.routes()
        t = [0]

        def clock():
            t[0] += 10 * 60
            return t[0]
        code, log, _ = self.run_(FakeEdgar(routes), clock=clock)
        self.assertTrue(self.state()["backlog"])
        self.assertIn("時間預算", log["stopped"])
        self.run_(FakeEdgar(routes))
        self.assertEqual(self.state()["backlog"], [])
        self.assertEqual({x["acc"] for x in self.items()}, {a1, b1})

    def test_abort_keeps_backlog(self):
        routes, (a1, a2, b1) = self.routes()
        routes[P.SUB_URL % 100] = eh.EdgarAbort("EDGAR 連續 3 次 HTTP 403")
        code, log, _ = self.run_(FakeEdgar(routes))
        self.assertIn("EDGAR 停止", log["stopped"])
        self.assertTrue(any(p[1] == a1 for p in self.state()["backlog"]))

    def test_snippets_prefer_specific_terms_and_groups(self):
        hits = [["客戶capex/需求|capital expenditure", 9, ["capex text"]], ["光通訊/CPO|optical", 3, ["optical a"]],
                ["光通訊/CPO|laser", 2, ["laser b"]], ["記憶體|HBM", 1, ["hbm c"]]]
        hs, sn = P.compact(hits)
        self.assertEqual([x[0] for x in sn], ["光通訊/CPO|optical", "記憶體|HBM", "光通訊/CPO|laser"])   # 通用詞最後

    def test_day_file_size_cap(self):
        items = [{"ticker": "T%d" % i, "cik": i, "acc": "x%05d" % i, "url": "u", "status": "ok",
                  "hits": [["g|w%d" % k, 3] for k in range(5)], "sn": [["g|w0", "y" * 200], ["g|w1", "y" * 200]]}
                 for i in range(120)]
        doc, note = P.fit_day({"date": "2026-11-05", "items": items})
        self.assertLessEqual(len(P._dumps(doc)), P.SIZE_LIMIT)
        self.assertEqual(note, "one_snippet")
        self.assertEqual(len(doc["items"]), 120)                 # 不刪資料

    def test_migrate_old_single_file_and_expire(self):
        with open(os.path.join(self.d, P.OUT), "w", encoding="utf-8") as f:
            json.dump({"items": [{"filed": "2026-11-04", "ticker": "OLD", "cik": 100, "acc": "old-1", "url": "u",
                                  "status": "ok", "hits": [["記憶體|HBM", 2, []]]}]}, f)
        os.makedirs(os.path.join(self.d, P.DAY_DIR))
        with open(os.path.join(self.d, P.DAY_DIR, "2026-07-01.json"), "w") as f:
            json.dump({"items": []}, f)
        routes, _ = self.routes()
        self.run_(FakeEdgar(routes))
        self.assertFalse(os.path.exists(os.path.join(self.d, P.DAY_DIR, "2026-07-01.json")))   # 超過 90 天刪除
        self.assertIn("OLD", {x["ticker"] for x in self.items()})
        self.assertNotIn("items", self.summary())

    def test_aggregate_windows(self):
        days = {"2026-11-01": [{"ticker": "A", "hits": [["t", 2]]}], "2026-09-20": [{"ticker": "B", "hits": [["t", 1]]}],
                "2026-08-20": [{"ticker": "C", "hits": [["t", 1]]}]}
        r = P.aggregate(days, dt.date(2026, 11, 7))[0]
        self.assertEqual((r["n30"], r["prev30"], r["n90"], r["tickers30"]), (1, 1, 3, ["A"]))

    def test_missing_terms_stops(self):
        with open(os.path.join(self.d, "earnings_keywords.py"), "w", encoding="utf-8") as f:
            f.write("COMPANIES = {}\n")
        with self.assertRaises(SystemExit):
            self.run_(FakeEdgar({}))

    def test_bilingual_terms_preferred(self):
        with open(os.path.join(self.d, "theme_terms_bilingual.json"), "w", encoding="utf-8") as f:
            json.dump([{"id": "hbm", "en": ["HBM"], "zh": ["高頻寬記憶體"]}], f)
        terms, src = P.load_terms(self.d)
        self.assertEqual((terms, src), ({"hbm": ["HBM"]}, "theme_terms_bilingual.json"))


if __name__ == "__main__":
    unittest.main()
