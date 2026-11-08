# -*- coding: utf-8 -*-
import datetime as dt
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout

import us_transcripts as U

SECRET_LINE = "Our proprietary CoWoS capacity will double, and lead times remain long."
SEGS = [
    {"speaker": "Op", "title": "Operator", "content": "Welcome. NOMATCH_SENTENCE_XYZ appears in no term."},
    {"speaker": "CEO", "title": "CEO", "content": SECRET_LINE + " We see CPO adoption. " * 2},
    {"speaker": "A", "title": "Analyst, Bank", "content": "What about HBM supply and CPO timing?"},
    {"speaker": "CEO", "title": "CEO", "content": "HBM is tight. cpo lowercase should not count."},
]
TERMS = {"先進封裝": ["CoWoS", "CPO"], "記憶體": ["HBM"], "供應鏈": ["lead time"]}


class FakeAV:
    def __init__(self, cal=None, data=None, quota_at=None):
        self.cal, self.data, self.quota_at = cal or {}, data or {}, quota_at
        self.used = 0
        self.calls = []

    def calendar(self):
        self.used += 1
        return self.cal

    def transcript(self, t, q):
        self.used += 1
        self.calls.append(t)
        if self.quota_at is not None and len(self.calls) >= self.quota_at:
            raise U.Quota("Thank you for using Alpha Vantage! rate limit")
        return self.data.get(t, [])


class TestMatch(unittest.TestCase):
    def test_sections_and_acronym_case(self):
        h = U.match_transcript(SEGS, U.compile_terms(TERMS))
        self.assertEqual(h["先進封裝|CPO"]["p"], 2)
        self.assertEqual(h["先進封裝|CPO"]["q"], 1)          # 小寫 cpo 不算
        self.assertEqual(h["記憶體|HBM"], {"p": 0, "q": 2, "s": h["記憶體|HBM"]["s"]})
        self.assertEqual(h["供應鏈|lead time"]["p"], 1)      # lead times 也算

    def test_snippet_limits(self):
        segs = [{"title": "CEO", "content": ("x " * 300 + "CoWoS ") * 10}]
        h = U.match_transcript(segs, U.compile_terms(TERMS))["先進封裝|CoWoS"]
        self.assertEqual(h["p"], 10)
        self.assertEqual(len(h["s"]), 3)
        self.assertTrue(all(len(x) <= 200 for x in h["s"]))

    def test_variants(self):
        rx = U.term_regex("800V")
        self.assertEqual(len(rx.findall("800V, 800 V, 800-V, 8000V")), 3)


class TestPriority(unittest.TestCase):
    def test_order(self):
        uni = {"AAA": {"src6": False, "baskets": ["a"], "seed": False},
               "BBB": {"src6": True, "baskets": ["a", "b"], "seed": False},
               "CCC": {"src6": True, "baskets": [], "seed": False},
               "DDD": {"src6": False, "baskets": [], "seed": True}}
        today = dt.date(2026, 11, 10)
        cal = {"DDD": "2026-11-05"}                          # 已發表財報 → 最先
        self.assertEqual(U.candidates(uni, {}, cal, today, "2026Q3"), ["DDD", "BBB", "AAA", "CCC"])
        st = {"BBB": {"2026Q3": {"status": "ok"}}, "AAA": {"2026Q3": {"status": "empty", "at": "2026-11-08"}}}
        self.assertEqual(U.candidates(uni, st, cal, today, "2026Q3"), ["DDD", "CCC"])   # 7 天內不重試 empty


class TestRun(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        with open(os.path.join(self.d, "earnings_keywords.py"), "w", encoding="utf-8") as f:
            f.write("COMPANIES = {'NVDA': 'x', 'AMD': 'y'}\nKEYWORDS = %r\n" % TERMS)
        with open(os.path.join(self.d, "intl_theme_baskets.json"), "w", encoding="utf-8") as f:
            json.dump({"themes": {"AI": {"us": ["NVDA", "MU"]}}}, f)

    def tearDown(self):
        shutil.rmtree(self.d)

    def run_(self, av, today=dt.date(2026, 11, 10), budget=20):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code, log = U.run(av=av, today=today, budget=budget, base=self.d)
        return code, log, buf.getvalue()

    def test_no_transcript_text_written_or_logged(self):
        av = FakeAV(data={"NVDA": SEGS})
        code, log, out = self.run_(av)
        self.assertEqual(code, 0)
        self.assertEqual(log["fetched"], ["NVDA"])
        for p in (U.OUT_HITS, U.OUT_STATE):
            with open(os.path.join(self.d, p), encoding="utf-8") as f:
                txt = f.read()
            self.assertNotIn("NOMATCH_SENTENCE_XYZ", txt)      # 沒命中的段落不留
            self.assertNotIn("cpo lowercase should not count. ", txt + " ")
            self.assertLessEqual(len(txt), U.SIZE_LIMIT)
        self.assertNotIn("CoWoS capacity", out)               # log 不含原文
        self.assertNotIn("NOMATCH", out)
        with open(os.path.join(self.d, U.OUT_HITS), encoding="utf-8") as f:
            doc = json.load(f)
        for h in doc["items"]["NVDA"]["2026Q3"]["hits"].values():
            self.assertTrue(len(h["s"]) <= 3 and all(len(x) <= 200 for x in h["s"]))

    def test_budget_and_quota_stop(self):
        av = FakeAV(data={"NVDA": SEGS, "MU": SEGS, "AMD": SEGS})
        code, log, _ = self.run_(av, budget=2)                # 行事曆 1 次 + 逐字稿 1 次
        self.assertEqual(len(av.calls), 1)
        av = FakeAV(data={"NVDA": SEGS}, quota_at=1)
        code, log, _ = self.run_(av)
        self.assertIn("額度", log["stopped"])

    def test_monday_and_src6_day_skip(self):
        av = FakeAV(data={"NVDA": SEGS})
        _, log, _ = self.run_(av, today=dt.date(2026, 11, 9))   # 週一
        self.assertEqual(av.used, 0)
        with open(os.path.join(self.d, "earnings_keywords.json"), "w") as f:
            json.dump({"generated_at": "2026-11-10 02:00:00"}, f)
        _, log, _ = self.run_(av)
        self.assertEqual(av.used, 0)
        self.assertIn("src6", log["stopped"])

    def test_dedup_next_day(self):
        self.run_(FakeAV(data={"NVDA": SEGS}))
        av = FakeAV(data={"NVDA": SEGS})
        self.run_(av, today=dt.date(2026, 11, 11))
        self.assertNotIn("NVDA", av.calls)                   # 這季已抓過

    def test_missing_terms_stops(self):
        with open(os.path.join(self.d, "earnings_keywords.py"), "w", encoding="utf-8") as f:
            f.write("COMPANIES = {'NVDA': 'x'}\n")
        with self.assertRaises(SystemExit):
            self.run_(FakeAV())


if __name__ == "__main__":
    unittest.main()
