# -*- coding: utf-8 -*-
"""test_group_flow.py — 族群金流共識(group_flow.py)的計算與流程測試(不需網路)。"""
import os
import json
import tempfile
import datetime as dt

import numpy as np
import pandas as pd

import group_flow as gf

IDX = pd.bdate_range("2026-09-01", periods=25)


def panel(spec):
    """spec:{code: (close_fn(i), vol_fn(i))} → close, volume。"""
    close = pd.DataFrame({k: [f(i) for i in range(len(IDX))] for k, (f, _) in spec.items()}, index=IDX)
    vol = pd.DataFrame({k: [g(i) for i in range(len(IDX))] for k, (_, g) in spec.items()}, index=IDX)
    return close, vol


def test_basket_stats_values():
    # A、B 前 24 天成交值各 1 億;最後一天 A=110×2e6=2.2 億、B=49×2e6=0.98 億 → 3.18 億 vs 均 2 億 = +59%
    close, vol = panel({"A": (lambda i: 100.0 if i < 24 else 110.0, lambda i: 1e6 if i < 24 else 2e6),
                        "B": (lambda i: 50.0 if i < 24 else 49.0, lambda i: 2e6)})
    s = gf.basket_stats(close, vol, ["A", "B", "Z"])
    assert s["n"] == 2 and s["value"] == 3.2 and s["value_vs_avg"] == 59.0
    assert type(s["value_vs_avg"]) is float
    assert s["ret_1d"] == round((10.0 + (-2.0)) / 2, 2) and s["up"] == 1
    assert s["members"][0]["code"] == "A" and s["members"][0]["chg"] == 10.0
    assert s["share"] is None                                   # 沒給全市場總額


def test_basket_value_vs_avg_exact():
    close, vol = panel({"A": (lambda i: 100.0, lambda i: 1e6 if i < 24 else 1.5e6)})
    s = gf.basket_stats(close, vol, ["A"])
    assert s["value"] == 1.5 and s["value_vs_avg"] == 50.0 and s["ret_1d"] == 0.0


def test_share_vs_avg():
    close, vol = panel({"A": (lambda i: 100.0, lambda i: 1e6 if i < 24 else 2e6)})
    totals = {str(d.date()): 100.0 for d in IDX}               # 全市場每天 100 億
    s = gf.basket_stats(close, vol, ["A"], totals)
    assert s["share"] == 2.0 and s["share_vs_avg"] == 100.0     # 占比 1% → 2%
    # 分母缺最後一天 → 不算占比
    totals.pop(str(IDX[-1].date()))
    assert gf.basket_stats(close, vol, ["A"], totals)["share"] is None


def test_short_history_excluded():
    close, vol = panel({"A": (lambda i: 100.0 if i >= 15 else np.nan, lambda i: 1e6)})
    assert gf.basket_stats(close, vol, ["A"]) is None


def st(vs, r):
    return {"value_vs_avg": vs, "ret_1d": r}


def test_status_consensus_partial_lead():
    s = gf.theme_status({"tw": st(40, 1), "us": st(35, 2), "jp": st(50, 0.5), "kr": st(5, 1)})
    assert s["status"] == "consensus" and s["direction"] == "up" and s["hot_up"] == ["tw", "us", "jp"]
    s = gf.theme_status({"tw": st(5, 1), "us": st(35, 2), "jp": st(50, 0.5), "kr": st(-10, -1)})
    assert s["status"] == "partial" and s["overseas_lead"] is True       # 美日熱↑、台股 +5% < +10%
    s = gf.theme_status({"tw": st(60, -2), "us": st(45, -1)})            # 只有兩個市場 → 2/2
    assert s["status"] == "consensus" and s["direction"] == "down" and s["need"] == 2
    s = gf.theme_status({"tw": st(10, 1), "us": st(20, 1), "jp": None})
    assert s["status"] == "quiet" and s["markets_with_data"] == 2
    assert gf.theme_status({"us": st(80, 3)})["status"] == "partial"     # 單一市場不算共識


def test_tickers_for():
    assert gf.tickers_for("tw", "2330") == ["2330.TW", "2330.TWO"]
    assert gf.tickers_for("kr", "005930") == ["005930.KS", "005930.KQ"]
    assert gf.tickers_for("jp", "285A") == ["285A.T"] and gf.tickers_for("us", "BRK.B") == ["BRK-B"]


def test_fetch_market_picks_suffix_with_data():
    def dl(tickers):
        cols = pd.MultiIndex.from_product([tickers, ["Close", "Volume"]])
        df = pd.DataFrame(np.nan, index=IDX, columns=cols)
        for t in tickers:
            if t in ("2330.TW", "8299.TWO"):
                df[(t, "Close")] = 10.0
                df[(t, "Volume")] = 1000.0
        return df
    now = dt.datetime(IDX[-1].year, IDX[-1].month, IDX[-1].day, 12, 0, tzinfo=dt.timezone.utc)   # 台北 20:00
    close, vol, miss = gf.fetch_market("tw", ["2330", "8299", "9999"], download=dl, now_utc=now)
    assert sorted(close.columns) == ["2330", "8299"] and miss == ["9999"] and close.index[-1] == IDX[-1]


def test_build_end_to_end():
    baskets = {"記憶體": {"us": ["MU"], "kr": ["005930"], "tw": ["2408"]}, "空題材": {"jp": ["0000"]}}

    def fetch(m, codes):
        if m == "jp":
            return None, None, codes
        close, vol = panel({c: (lambda i: 100.0 + (5 if i == 24 else 0), lambda i: 1e6 if i < 24 else 2e6) for c in codes})
        return close, vol, []
    out = gf.build(baskets, fetch=fetch)
    t = out["themes"][0]
    assert t["theme"] == "記憶體" and t["status"] == "consensus" and t["direction"] == "up"
    assert set(t["markets"]) == {"us", "kr", "tw"} and out["themes"][1]["markets"] == {}
    assert out["missing"] == {"jp": ["0000"]} and out["asof"]["us"] == str(IDX[-1].date())


def test_tw_totals_from_price_history():
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "20261016.json"), "w") as f:
            json.dump({"2330": [1000.0, 3e7], "0050": [100.0, 1e8], "bad": [None, 5]}, f)
        assert gf.tw_totals(d) == {"2026-10-16": round((1000 * 3e7 + 100 * 1e8) / 1e8, 1)}


def test_value_5d_vs_avg():
    # 前 20 天成交值 1 億、最後 5 天 1.5 億 → +50%
    close, vol = panel({"A": (lambda i: 100.0, lambda i: 1e6 if i < 20 else 1.5e6)})
    assert gf.basket_stats(close, vol, ["A"])["value_5d_vs_avg"] == 50.0


def test_rotation_weight_vs_universe():
    # A、B 各 1 億;最後 5 天 A 變 3 億、B 不變 → A 比重 50% → 75%(+50%),B 50% → 25%(−50%)
    close, vol = panel({"A": (lambda i: 100.0, lambda i: 1e6 if i < 20 else 3e6),
                        "B": (lambda i: 100.0, lambda i: 1e6)})
    u = gf.universe_value(close, vol)
    a = gf.basket_stats(close, vol, ["A"], universe=u)
    b = gf.basket_stats(close, vol, ["B"], universe=u)
    assert a["weight"] == 75.0 and a["rot_5d"] == 50.0
    assert a["rot_vs_avg"] == round((75 / ((16 * 50 + 4 * 75) / 20) - 1) * 100, 1)   # 前 20 日含 4 天 75%
    assert b["weight"] == 25.0 and b["rot_5d"] == -50.0
    assert gf.basket_stats(close, vol, ["A"])["weight"] is None          # 沒給分母


def rs(r5, vs=0, r=0):
    return {"value_vs_avg": vs, "ret_1d": r, "rot_5d": r5, "value_5d_vs_avg": vs}


def test_rotation_status_and_averages():
    s = gf.theme_status({"tw": rs(12, 10), "us": rs(15, 20), "jp": rs(30, 30), "kr": rs(-5, -20)})
    assert s["rot_status"] == "inflow" and s["rot_in"] == ["tw", "us", "jp"] and s["rot_out"] == []
    assert s["flow_avg"] == 10.0 and s["rot5_avg"] == 13.0
    assert gf.theme_status({"tw": rs(-20), "us": rs(-11)})["rot_status"] == "outflow"     # 2/2
    assert gf.theme_status({"tw": rs(20), "us": rs(-20), "jp": rs(0)})["rot_status"] == "mixed"
    assert gf.theme_status({"tw": rs(20), "us": rs(1), "jp": rs(0)})["rot_status"] == "partial_in"
    s = gf.theme_status({"tw": rs(None), "us": rs(3)})
    assert s["rot_status"] == "flat" and s["rot5_avg"] == 3.0


def test_build_rotation_uses_union_universe():
    # 同一檔 X 在兩個題材 → 分母只算一次
    baskets = {"甲": {"us": ["X", "Y"]}, "乙": {"us": ["X"]}}

    def fetch(m, codes):
        close, vol = panel({c: (lambda i: 100.0, lambda i: 1e6) for c in codes})
        return close, vol, []
    out = gf.build(baskets, fetch=fetch)
    w = {t["theme"]: t["markets"]["us"]["weight"] for t in out["themes"]}
    assert w == {"甲": 100.0, "乙": 50.0}
