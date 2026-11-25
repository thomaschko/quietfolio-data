# -*- coding: utf-8 -*-
import datetime as dt
import json
import os

import pytest

import mood_leverage as L

D = dt.date


def test_parse_finra():
    rows = [["Year-Month", "Debit Balances in Customers' Securities Margin Accounts", "Free Credit"],
            ["2026-10", 1448360, 210290], ["2026-09", "1,480,054", 215328], ["note", None, None]]
    out = L.parse_finra(rows)
    assert out == [{"month": "2026-09", "debit_musd": 1480054.0}, {"month": "2026-10", "debit_musd": 1448360.0}]
    with pytest.raises(ValueError):
        L.parse_finra([["Year-Month", "Something else"]])


def test_parse_jpx_history_mtsk_layout():
    # mtsk.xls:日期為 Excel 序號,「合計」在第 9 欄
    hdr = ["月日", "委託", "", "", "", "自己", "", "", "", "合計   Total", "", "", "", "社内対当"]
    rows = [["信用取引現在高"], hdr, ["[東京・名古屋 Tokyo"],
            [46283.0, 1, 2, 3, 4, 5, 6, 7, 8, 481794.0, 884462.0, 3497919.0, 6455988.0, 1],
            ["", ""], ["[東京 Tokyo"], [46283.0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 9, 9, 9, 9]]
    out = L.parse_jpx_history(rows, "20260925_mtsk.xls")
    assert out == [{"date": "2026-09-18", "sell_kshs": 481794.0, "sell_myen": 884462.0, "buy_kshs": 3497919.0,
                    "buy_myen": 6455988.0, "src": "20260925_mtsk.xls"}]


def test_parse_jpx_history_xlsx_layout():
    hdr = ["月日", "制度信用取引", None, None, None, None, None, None, None, "一般信用取引", None, None, None, "合計 Total"]
    rows = [hdr, ["[東京・名古屋 Tokyo"],
            [dt.datetime(2026, 9, 25), 79560, 108176, 1788, 2136, 228944, 462841, 1644350, 3556056, 236636, 476220,
             1857331, 2905644, 545141, 1047237, 3503469, 6463836]]
    out = L.parse_jpx_history(rows, "x.xlsx")
    assert out[0]["date"] == "2026-09-25" and out[0]["buy_myen"] == 6463836 and out[0]["sell_kshs"] == 545141


def test_parse_jpx_weekly():
    rows = [[None, dt.datetime(2026, 11, 13)], [None, dt.datetime(2026, 11, 13), dt.datetime(2026, 11, 13)],
            [None, dt.datetime(2026, 11, 17)], [],
            [None, None, None, None, "二市場 Tokyo&Nagoya", None, None, None, "東京 Tokyo"],
            [None, "制度信用取引残高", "自己", "株数Shs.", 79690.0, 981.0, 1595.0, -1041.0],
            [None, "信用取引残高合計\nTotal", None, "株数Shs.", 356858.0, 24439.0, 3396354.0, -64338.0, 1, 1, 1, 1],
            [None, None, None, "金額Val.", 660000.0, 46513.0, 5768159.0, -112836.0, 1, 1, 1, 1]]
    r = L.parse_jpx_weekly(rows, "20261113_mtcurrent.xlsx")
    assert r == {"date": "2026-11-13", "sell_kshs": 356858.0, "sell_myen": 660000.0, "buy_kshs": 3396354.0,
                 "buy_myen": 5768159.0, "src": "20261113_mtcurrent.xlsx"}


def test_parse_jpx_mcap_both_sheets():
    new = [["株式時価総額"], [None, None, None, None, None, "(百万円)"],
           ["月末", "プライム", "スタンダード", "グロース", "TOKYO PRO", "合計\nTotal"],
           [dt.datetime(2026, 10, 30), 1302763824, 32779080, 10082607, 373948, 1345999459]]
    old = [["月末", "一部", "二部", "マザーズ", "PRO", "JQS", "JQG", "合計\nTotal"],
           [dt.datetime(2022, 3, 31), 708523421, 5418959, 6855816, 121518, 9182979, 257564, 730360259]]
    out = L.parse_jpx_mcap([new, old])
    assert out == [{"month_end": "2022-03-31", "mcap_myen": 730360259.0},
                   {"month_end": "2026-10-30", "mcap_myen": 1345999459.0}]


def test_parse_kofia_checks_sum():
    j = {"ds1": [{"TMPV1": "20261123", "TMPV2": 25942655, "TMPV3": 20055285, "TMPV4": 5887370},
                 {"TMPV1": "20261120", "TMPV2": 25957908, "TMPV3": 20095547, "TMPV4": 5862361}]}
    out = L.parse_kofia(j)
    assert [r["date"] for r in out] == ["2026-11-20", "2026-11-23"]
    bad = {"ds1": [{"TMPV1": "20261123", "TMPV2": 999, "TMPV3": 1, "TMPV4": 1}]}
    with pytest.raises(ValueError):
        L.parse_kofia(bad)


def test_yoy_series_uses_last_point_before_one_year():
    pts = [(D(2025, 11, 21), 100.0), (D(2025, 11, 24), 110.0), (D(2026, 11, 24), 121.0)]
    assert L.yoy_series(pts) == [(D(2026, 11, 24), 10.0)]   # 2025-11-24 = 365 天前
    # 一年前沒有接近的資料 → 不算
    assert L.yoy_series([(D(2025, 1, 1), 1.0), (D(2026, 11, 24), 2.0)]) == []


def test_month_end():
    assert L.month_end("2026-02") == D(2026, 2, 28) and L.month_end("2026-12") == D(2026, 12, 31)


def test_jp_ratio_uses_previous_month_end():
    jp = [{"date": "2026-11-13", "buy_myen": "5768159"}, {"date": "2026-10-30", "buy_myen": "6505886"}]
    mc = [{"month_end": "2026-09-30", "mcap_myen": "1396539007"}, {"month_end": "2026-10-30", "mcap_myen": "1345999459"}]
    out = L.jp_ratio_series(sorted(jp, key=lambda r: r["date"]), mc)
    assert out[0][2] == D(2026, 10, 30) and out[1][2] == D(2026, 10, 30)
    assert out[1][1] == round(5768159 / 1345999459 * 100, 4)


def _series(n, start, step_days, last):
    s = [(start + dt.timedelta(days=i * step_days), float(i % 50)) for i in range(n)]
    s[-1] = (s[-1][0], last)
    return s


def test_card_zone_stale_and_fetch_failed():
    s = _series(300, D(2021, 1, 1), 7, 1000.0)       # 最後一筆遠高於歷史 → 分位 100 → hot
    today = s[-1][0] + dt.timedelta(days=3)
    c = L.card("JP", s, today, today - dt.timedelta(days=1), None)
    assert c["ok"] and c["pctl"] == 100.0 and c["zone"] == "hot" and c["vote"] == "hot"
    # 第一次抓到已超過 10 天 → 過期、不投票
    c = L.card("JP", s, today, today - dt.timedelta(days=11), None)
    assert c["stale"] and c["vote"] is None and "資料過期" in c["note"]
    # 本次抓取失敗 → 資料暫缺、不投票(數值保留供顯示)
    c = L.card("JP", s, today, today, "HTTP 500")
    assert c.get("fetch_failed") and c["vote"] is None and "資料暫缺" in c["note"]
    # 沒資料
    c = L.card("KR", [], today, None, "timeout")
    assert not c["ok"] and "資料暫缺" in c["note"]


def test_card_window_10y_and_min_hist():
    s = _series(100, D(2020, 1, 1), 7, 1000.0)
    c = L.card("JP", s, s[-1][0], s[-1][0], None)
    assert c["pctl"] is None and c["vote"] is None and "歷史不足" in c["note"]


def _write(base, key, rows):
    L.write_csv(str(base), key, rows)


def test_compute_votes_two_of_three(tmp_path):
    today = D(2026, 11, 25)
    seen = "2026-11-24"
    # US:月資料 2014-01 ~ 2026-10,最後一個月年增率最高
    us, v = [], 100.0
    m = D(2014, 1, 1)
    while m <= D(2026, 10, 1):
        v *= 1.002
        us.append({"month": m.strftime("%Y-%m"), "debit_musd": round(v, 3), "first_seen": "2026-11-01"})
        m = D(m.year + (m.month == 12), m.month % 12 + 1, 1)
    us[-1]["debit_musd"] = us[-13]["debit_musd"] * 1.6
    us[-1]["first_seen"] = "2026-11-18"
    _write(tmp_path, "US", us)
    # KR:日資料,最後一天年增率最高
    kr, d = [], D(2016, 1, 4)
    while d <= D(2026, 11, 24):
        if d.weekday() < 5:
            kr.append({"date": d.isoformat(), "credit_mkrw": 1000 + (d - D(2016, 1, 4)).days % 40, "kospi_mkrw": 0,
                       "kosdaq_mkrw": 0, "first_seen": "2026-01-01"})
        d += dt.timedelta(days=1)
    kr[-1]["credit_mkrw"] = 5000
    kr[-1]["first_seen"] = seen
    _write(tmp_path, "KR", kr)
    # JP:沒有市值 → 暫缺
    _write(tmp_path, "JP", [{"date": "2026-11-13", "sell_kshs": 1, "sell_myen": 1, "buy_kshs": 3, "buy_myen": 3,
                             "src": "x", "first_seen": seen}])
    _write(tmp_path, "JP_MCAP", [])
    out = L.compute(str(tmp_path), today, status={})
    assert out["US_FINRA"]["vote"] == "hot" and out["KR"]["vote"] == "hot"
    assert out["JP"]["ok"] is False and out["JP"]["creditRatio"] == 3.0
    assert out["votes"]["intlLeverage"] == "hot" and out["votes"]["valid"] == 2
    # 韓國抓取失敗 → 只剩美國有效 → 無方向
    out = L.compute(str(tmp_path), today, status={"KR": "timeout"})
    assert out["KR"]["vote"] is None and out["votes"]["intlLeverage"] is None and out["votes"]["valid"] == 1
    assert "無方向" in out["votes"]["note"]


def test_merge_keeps_first_seen_and_warns():
    old = [{"month": "2026-09", "debit_musd": "100", "first_seen": "2026-10-20"}]
    warn = []
    rows, n = L.merge(old, [{"month": "2026-09", "debit_musd": 110.0}, {"month": "2026-10", "debit_musd": 120.0}],
                      "month", D(2026, 11, 20), warn, "debit_musd")
    assert n == 1 and rows[0]["first_seen"] == "2026-10-20" and rows[1]["first_seen"] == "2026-11-20"
    assert warn and "2026-09" in warn[0]


class FakeResp:
    def __init__(self, text="", content=b"", status=200, js=None):
        self.text, self.content, self.status_code, self._js = text, content or text.encode(), status, js

    def json(self):
        return self._js


def test_fetcher_stops_on_403():
    class S:
        headers = {}

        def get(self, url, timeout=None):
            return FakeResp("User-agent: *\nAllow: /\n")

        def request(self, method, url, timeout=None, **kw):
            return FakeResp("blocked", status=403)
    f = L.Fetcher(S(), sleep=lambda s: None, gap=0)
    with pytest.raises(RuntimeError):
        f.get("https://example.com/a")
    with pytest.raises(RuntimeError, match="已被擋"):
        f.get("https://example.com/b")


def test_fetcher_respects_robots():
    class S:
        headers = {}

        def get(self, url, timeout=None):
            return FakeResp("User-agent: *\nDisallow: /private\n")

        def request(self, method, url, timeout=None, **kw):
            return FakeResp("ok")
    f = L.Fetcher(S(), sleep=lambda s: None, gap=0)
    assert f.get("https://example.com/public").text == "ok"
    with pytest.raises(RuntimeError, match="robots"):
        f.get("https://example.com/private/x")


def test_consensus_add_leverage_adds_one_vote(tmp_path, monkeypatch):
    import consensus as C
    m = {"indicators": [{"key": "a", "label": "A", "zone": "hot", "dir": 1}, {"key": "b", "label": "B", "zone": "hot", "dir": 1}],
         "vote_items": [{"key": "a", "label": "A", "zone": "hot"}, {"key": "b", "label": "B", "zone": "hot"}]}
    monkeypatch.setattr(C.mood_leverage, "compute", lambda base, today=None: {"votes": {"intlLeverage": "hot", "note": None}})
    C.add_leverage(m, str(tmp_path))
    assert [x["key"] for x in m["vote_items"]] == ["a", "b", "intl_leverage"]
    assert m["label"] == "偏熱" and m["hot"] == 3
    C.add_leverage(m, str(tmp_path))                       # 重跑不重複加
    assert [x["key"] for x in m["vote_items"]].count("intl_leverage") == 1
    monkeypatch.setattr(C.mood_leverage, "compute", lambda base, today=None: (_ for _ in ()).throw(RuntimeError("x")))
    m2 = {"indicators": [], "vote_items": []}
    C.add_leverage(m2, str(tmp_path))                      # 失敗不影響
    assert "leverage" not in m2
