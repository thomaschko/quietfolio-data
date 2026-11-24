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
        step = {"1d": 1, "1wk": 7, "1mo": 30}[iv]
        n = {"1d": 600, "1wk": 520, "1mo": 120}[iv]
        t0 = int(dt.datetime(2016, 1, 4, tzinfo=dt.timezone.utc).timestamp())
        ts = [t0 + i * step * 86400 for i in range(n)]
        cl = [100 + 10 * math.sin(i / 9.0) + (5 if sym == "^SOX" else 0) for i in range(n)]
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


if __name__ == "__main__":
    unittest.main()
