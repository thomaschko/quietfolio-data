# -*- coding: utf-8 -*-
"""mood_intl(國際 MACD,v2026-11-25-mood-intl)單元測試:合成資料,不需網路。"""
import datetime as dt
import math
import unittest

import consensus as C
import mood_intl as M


def series(n, start="2016-01-04", step=7, fn=lambda i: 100 + 10 * math.sin(i / 9.0)):
    d0 = dt.date.fromisoformat(start)
    return [((d0 + dt.timedelta(days=i * step)).isoformat(), fn(i)) for i in range(n)]


class Resp:
    def __init__(self, status, payload):
        self.status_code, self._p = status, payload

    def json(self):
        return self._p


class FakeSession:
    def __init__(self, fail=()):
        self.fail, self.calls = set(fail), []

    def get(self, url, headers=None, timeout=None):
        self.calls.append(url)
        sym = url.split("/chart/")[1].split("?")[0].replace("%5E", "^")
        iv = url.split("interval=")[1].split("&")[0]
        if (sym, iv) in self.fail:
            return Resp(503, {})
        rng = url.split("range=")[1].split("&")[0]
        step = {"1d": 1, "1wk": 7, "1mo": 30}[iv]
        n = {"1d": 600, "1wk": 520, "1mo": 120}[iv]
        t0 = int(dt.datetime(2016, 1, 4, tzinfo=dt.timezone.utc).timestamp())
        ts = [t0 + i * step * 86400 for i in range(n)]
        cl = [100 + 10 * math.sin(i / 9.0) + (5 if sym == "^SOX" else 0) for i in range(n)]
        if rng != "10y":                     # 短範圍:多一根最新的(模擬 10 年查詢缺最新一天)
            ts, cl = ts[-3:] + [ts[-1] + step * 86400], cl[-3:] + [cl[-1] + 1]
        elif iv == "1d":
            ts, cl = ts, cl
        return Resp(200, {"chart": {"result": [{"meta": {"gmtoffset": 28800}, "timestamp": ts,
                                                "indicators": {"quote": [{"close": cl}]}}]}})


class TestCalc(unittest.TestCase):
    def test_ema_macd(self):
        self.assertEqual(M.ema([1, 2, 3], 3), [1, 1.5, 2.25])
        dif, dea, hist = M.macd([10.0] * 50)
        self.assertAlmostEqual(dif[-1], 0)
        self.assertAlmostEqual(hist[-1], 0)

    def test_states(self):
        self.assertEqual(M.state_of(1, 2, 1), "BULL_EXPAND")
        self.assertEqual(M.state_of(1, 1, 2), "BULL_FADE")
        self.assertEqual(M.state_of(1, -1, -2), "BULL_FADE")       # DIF>0 但 HIST ≤ 0
        self.assertEqual(M.state_of(-1, -1, -2), "BEAR_FADE")
        self.assertEqual(M.state_of(-1, -3, -2), "BEAR_EXPAND")

    def test_dedupe_period(self):
        rows = [("2026-11-09", 1.0), ("2026-11-16", 2.0), ("2026-11-23", 3.0), ("2026-11-25", 4.0)]
        self.assertEqual(M.dedupe_period(rows, "1wk"), [("2026-11-09", 1.0), ("2026-11-16", 2.0), ("2026-11-23", 4.0)])
        self.assertEqual(M.dedupe_period([("2026-10-01", 1.0), ("2026-11-01", 2.0), ("2026-11-25", 3.0)], "1mo"),
                         [("2026-10-01", 1.0), ("2026-11-01", 3.0)])

    def test_normalize_dates(self):
        self.assertEqual(M.normalize_dates([("2026-11-24", 1.0)], "1wk"), [("2026-11-23", 1.0)])   # 週二 → 週一
        self.assertEqual(M.normalize_dates([("2026-11-24", 1.0)], "1mo"), [("2026-11-01", 1.0)])

    def test_short_range_adds_latest_bar(self):
        rows, _ = M.fetch_chart(FakeSession(), "^TWII", "1d", sleep=lambda s: None)
        self.assertEqual(len(rows), 601)
        self.assertEqual(rows[-1][0], (dt.date(2016, 1, 4) + dt.timedelta(days=600)).isoformat())

    def test_latest_from_meta_when_close_null(self):
        class S:
            def get(self, url, headers=None, timeout=None):
                t0 = int(dt.datetime(2026, 11, 20, 0, 0, tzinfo=dt.timezone.utc).timestamp())
                return Resp(200, {"chart": {"result": [{"meta": {"gmtoffset": 32400, "regularMarketPrice": 5006.03,
                                                                  "regularMarketTime": t0 + 4 * 86400 + 6 * 3600},
                                                         "timestamp": [t0, t0 + 3 * 86400, t0 + 4 * 86400],
                                                         "indicators": {"quote": [{"close": [4800.0, 4708.54, None]}]}}]}})
        rows, _ = M._fetch_range(S(), "^KS11", "1d", "1mo", sleep=lambda s: None)
        self.assertEqual(rows[-1], ("2026-11-24", 5006.03))
        self.assertEqual(rows[-2], ("2026-11-23", 4708.54))

    def test_rolling_zone_no_lookahead(self):
        dp = [0.0] * 35 + [float(i % 10) for i in range(150)] + [50.0]
        z = M.rolling_zones(dp, "1wk")
        self.assertEqual(z[-1][1], "hot")
        self.assertEqual(z[-1][0], 100.0)
        self.assertEqual(z[40], (None, None))        # 歷史不足 120 → 不給
        dp2 = dp[:-1] + [-5.0]
        self.assertEqual(M.rolling_zones(dp2, "1wk")[-1][1], "cold")

    def test_event_stats_first_day_only(self):
        dates = [d for d, _ in series(30)]
        closes = [100.0 + i for i in range(30)]
        zones = [(None, None)] * 5 + [(95, "hot")] * 3 + [(None, None)] * 22
        tw = {M.week_key(d): 200.0 + i for i, d in enumerate(dates)}
        st, recent = M.event_stats(dates, closes, zones, tw, M.week_key, "1wk")
        self.assertEqual(st["hot"]["n"], 1)                       # 連續 3 根極端只算第一根
        self.assertAlmostEqual(st["hot"]["avgRet4"], round((109 / 105 - 1) * 100, 2))
        self.assertTrue(st["hot"]["lowSample"])
        self.assertEqual(st["cold"]["n"], 0)
        self.assertEqual(recent, [{"date": dates[5], "zone": "hot"}])

    def test_provisional(self):
        now = dt.datetime(2026, 11, 25, 16, 0)                    # 週三 16:00
        self.assertTrue(M.is_provisional("1wk", "2026-11-23", now, 1330))
        self.assertFalse(M.is_provisional("1wk", "2026-11-16", now, 1330))
        self.assertFalse(M.is_provisional("1d", "2026-11-25", now, 1330))     # 已收盤
        self.assertTrue(M.is_provisional("1d", "2026-11-25", now.replace(hour=10), 1330))
        self.assertTrue(M.is_provisional("1mo", "2026-11-01", now, 1330))
        self.assertTrue(M.is_provisional("1wk", "2026-11-27", dt.datetime(2026, 11, 27, 10, 0), 1600))   # 週五盤中

    def test_votes(self):
        today = dt.date(2026, 11, 25)
        mk = lambda z, d="2026-11-23": {"intervals": {"1wk": {"ok": True, "date": d, "zone": z}}}
        b = {"^TWII": mk("cold"), "^SOX": mk("hot"), "^GSPC": mk("hot"), "^N225": mk(None), "^KS11": mk(None)}
        v = M.votes(b, today)
        self.assertEqual(v["twMacd"], "cold")
        self.assertIsNone(v["intlMacd"])                          # 只有 2 個同邊 → 不計票
        b["^N225"] = mk("hot")
        self.assertEqual(M.votes(b, today)["intlMacd"], "hot")
        b["^TWII"] = mk("cold", "2026-11-02")                      # 超過 10 天 → 不投票
        v = M.votes(b, today)
        self.assertIsNone(v["twMacd"])
        self.assertEqual(v["twMacd_note"], "過期")


class TestBuild(unittest.TestCase):
    def test_build_and_fetch_failure_uses_prev(self):
        now = dt.datetime(2026, 11, 25, 9, 0, tzinfo=dt.timezone.utc)
        o = M.build(session=FakeSession(), now_utc=now, sleep=lambda s: None)
        w = o["symbols"]["^TWII"]["intervals"]["1wk"]
        self.assertTrue(w["ok"])
        self.assertEqual(len(w["spark"]), 52)
        self.assertIn(w["state"], M.STATE_ZH)
        self.assertIn("lowSample", o["symbols"]["^TWII"]["intervals"]["1mo"])
        o2 = M.build(session=FakeSession(fail={("^KS11", "1wk")}), prev=o, now_utc=now, sleep=lambda s: None)
        k = o2["symbols"]["^KS11"]["intervals"]["1wk"]
        self.assertEqual(k["fetch_failed"], "HTTP 503")
        self.assertEqual(k["date"], o["symbols"]["^KS11"]["intervals"]["1wk"]["date"])
        o3 = M.build(session=FakeSession(fail={("^KS11", "1wk")}), prev=None, now_utc=now, sleep=lambda s: None)
        self.assertFalse(o3["symbols"]["^KS11"]["intervals"]["1wk"]["ok"])

    def test_consensus_adds_votes(self):
        m = {"label": "中性", "hot": 0, "cold": 0, "indicators": [
            {"key": "a", "label": "A", "dir": 1, "zone": "cold"}, {"key": "b", "label": "B", "dir": 1, "zone": "cold"},
            {"key": "c", "label": "C", "dir": 0, "zone": "low"}]}
        mi = {"votes": {"twMacd": "cold", "intlMacd": None}, "symbols": {}}
        C.add_intl_macd(m, base="/nonexistent", builder=lambda prev=None: mi)
        self.assertEqual(m["label"], "過度看空")                   # 原本 2 項 + 台股 MACD 1 項 = 3
        self.assertEqual(m["cold"], 3)
        self.assertEqual([x["key"] for x in m["vote_items"]], ["a", "b", "tw_macd", "intl_macd"])

    def test_consensus_survives_builder_error(self):
        m = {"label": "中性", "hot": 0, "cold": 0, "indicators": []}
        C.add_intl_macd(m, base="/nonexistent", builder=lambda prev=None: (_ for _ in ()).throw(RuntimeError("x")))
        self.assertNotIn("macd", m)
        self.assertEqual(m["label"], "中性")


class TestAuditFixes(unittest.TestCase):
    """2026-11-25 審查 F1–F8"""

    def test_us_composite_four_cases(self):
        self.assertEqual(M.us_composite("hot", "hot"), "hot")       # 同邊
        self.assertEqual(M.us_composite("cold", None), "cold")      # 一邊極端、另一邊正常
        self.assertIsNone(M.us_composite("hot", "cold"))           # 方向相反
        self.assertIsNone(M.us_composite(None, None))

    def test_votes_rules(self):
        today = dt.date(2026, 11, 25)
        mk = lambda z, d="2026-11-16", **kw: dict({"intervals": {"1wk": dict({"ok": True, "date": "2026-11-23", "zone": "hot",
                                                                              "lastClosed": {"date": d, "zone": z}}, **kw)}})
        b = {"^TWII": mk("cold"), "^SOX": mk("cold"), "^GSPC": mk(None), "^N225": mk(None), "^KS11": mk("cold")}
        v = M.votes(b, today)
        self.assertEqual(v["twMacd"], "cold")                      # 用已收定那根(cold),不是本週即時(hot)
        self.assertEqual(v["intlMacd"], "cold")                    # 美(合成 cold)+ 韓 cold = 2
        b["^KS11"] = mk("cold", fetch_failed="HTTP 503")
        v = M.votes(b, today)
        self.assertIsNone(v["intlMacd"])                           # F8:抓取失敗不投票
        self.assertEqual(v["intlMacd_detail"]["^KS11"]["note"], "抓取失敗")
        b["^TWII"]["suspect"] = [{"date": "2026-11-18", "chg": 9.0}]
        v = M.votes(b, today)
        self.assertIsNone(v["twMacd"])                             # F7:投票那週有可疑資料
        self.assertEqual(v["twMacd_note"], "資料可疑")

    def test_trim_intraday(self):
        # 美東 10:20(台北 23:20):今天的日線是盤中價 → 丟掉;週線最後一根改用前一日收盤
        off = -5 * 3600
        now = dt.datetime(2026, 11, 25, 15, 20, tzinfo=dt.timezone.utc)
        raw = {("^SOX", "1d"): ([("2026-11-23", 100.0), ("2026-11-24", 101.0), ("2026-11-25", 120.0)], off),
               ("^SOX", "1wk"): ([("2026-11-16", 90.0), ("2026-11-23", 120.0)], off),
               ("^SOX", "1mo"): ([("2026-10-01", 80.0), ("2026-11-01", 120.0)], off)}
        self.assertTrue(M.trim_intraday(raw, "^SOX", now, 1600))
        self.assertEqual(raw[("^SOX", "1d")][0][-1], ("2026-11-24", 101.0))
        self.assertEqual(raw[("^SOX", "1wk")][0][-1], ("2026-11-23", 101.0))
        self.assertEqual(raw[("^SOX", "1mo")][0][-1], ("2026-11-01", 101.0))
        # 收盤後不修剪
        raw2 = {("^SOX", "1d"): ([("2026-11-25", 120.0)], off)}
        self.assertFalse(M.trim_intraday(raw2, "^SOX", now.replace(hour=22), 1600))
        # 新的一週只有今天盤中 → 週線最後一根丟掉
        raw3 = {("^SOX", "1d"): ([("2026-11-27", 100.0), ("2026-11-30", 130.0)], off),
                ("^SOX", "1wk"): ([("2026-11-23", 100.0), ("2026-11-30", 130.0)], off)}
        self.assertTrue(M.trim_intraday(raw3, "^SOX", dt.datetime(2026, 11, 30, 15, 0, tzinfo=dt.timezone.utc), 1600))
        self.assertEqual(raw3[("^SOX", "1wk")][0], [("2026-11-23", 100.0)])

    def test_suspect_days(self):
        rows = [("2026-11-20", 100.0), ("2026-11-23", 100.0), ("2026-11-24", 109.0), ("2026-11-25", 110.0)]
        self.assertEqual(M.suspect_days(rows), [{"date": "2026-11-24", "chg": 9.0}])

    def test_tw_window_lag(self):
        keys = ["2026-11-20", "2026-11-23", "2026-11-24"]
        closes = [100.0, 110.0, 121.0]
        self.assertAlmostEqual(M.tw_window(keys, closes, "2026-11-20", 1, lag=False), 0.10)
        self.assertAlmostEqual(M.tw_window(keys, closes, "2026-11-20", 1, lag=True), 121 / 110 - 1)   # 從下一個台股交易日起算
        self.assertIsNone(M.tw_window(keys, closes, "2026-11-24", 1, lag=True))

    def test_base_and_last_closed(self):
        rows = series(300)
        now = dt.datetime(2030, 1, 1)
        a = M.analyze(rows, "1wk", rows, now, 1330)
        self.assertIn("base", a["extremeStats"])
        self.assertEqual(a["extremeStats"]["base"]["n4"], 300 - 35 - 4)
        self.assertEqual(a["lastClosed"]["date"], rows[-1][0])           # 已收定 → 最後一根
        prov_now = dt.date.fromisoformat(rows[-1][0])
        b = M.analyze(rows, "1wk", rows, dt.datetime(prov_now.year, prov_now.month, prov_now.day, 9, 0), 1330)
        self.assertTrue(b["provisional"])
        self.assertEqual(b["lastClosed"]["date"], rows[-2][0])           # 未收定 → 前一根

    def test_tw_macd_merged_with_drawdown(self):
        m = {"label": "中性", "hot": 0, "cold": 0, "indicators": [
            {"key": "drawdown", "label": "距高點", "dir": 1, "zone": "cold"},
            {"key": "breadth5", "label": "上漲家數", "dir": 1, "zone": "cold"}]}
        mi = {"votes": {"twMacd": "cold", "intlMacd": None}, "symbols": {}}
        C.add_intl_macd(m, base="/nonexistent", builder=lambda prev=None: mi)
        self.assertEqual(m["cold"], 2)                             # 台股 MACD 與距高點同向 → 合併,只算 2 項
        self.assertEqual(m["label"], "中性")
        self.assertEqual(m["vote_items"][-2]["merged_with"], "drawdown")


if __name__ == "__main__":
    unittest.main()
