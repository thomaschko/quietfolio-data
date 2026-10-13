# -*- coding: utf-8 -*-
"""
test_pullback_scan.py — 每日掃描(pullback_scan.py)的單元測試(不需網路)

用法:python test_pullback_scan.py
情境同 test_pullback_backtest.py:前30日收盤100 → 第30日爆量收黑85 → 第31日84 → 第32日收87(反轉)。
"""
import pandas as pd

from pullback_scan import classify_today, merge_official
from test_pullback_backtest import scenario


def test_classify_each_day():
    df = scenario([88, 90, 95, 96] + [96] * 30)
    assert classify_today(df.iloc[:30])[0] is None              # 急跌前:無訊號
    assert classify_today(df.iloc[:31])[0] == "noconfirm"       # 第30日:爆量收黑當天
    assert classify_today(df.iloc[:32])[0] == "watch"           # 第31日:已爆量、未反轉
    kind, info = classify_today(df.iloc[:33])                   # 第32日:反轉確認
    assert kind == "confirm", kind
    assert abs(info["target"] - 93.6) < 0.01 and abs(info["stop"] - 70) < 0.01
    assert info["ref_high"] == 100 and info["trough_3d"] == 84
    assert abs(info["drawdown_3d_max"] - 0.16) < 1e-9 and info["vol_mult_max_3d"] == 3.0
    assert info["cap_dates"] == [str(df.index[30].date())]


def test_classify_uses_only_data_up_to_today():
    """後面的資料不影響今天的分類(掃描只看最後一列)。"""
    df = scenario([88, 90, 95, 96] + [96] * 30)
    k1, i1 = classify_today(df.iloc[:33])
    df2 = df.copy()
    df2.iloc[33:, :] = df2.iloc[33:, :] * 3
    k2, i2 = classify_today(df2.iloc[:33])
    assert k1 == k2 and i1 == i2


def _frame():
    idx = pd.bdate_range("2026-09-01", periods=5)
    return pd.DataFrame({"Open": 10.0, "High": 11.0, "Low": 9.0, "Close": 10.0, "Volume": 1000.0,
                         "Turnover": 10000.0}, index=idx)


def test_merge_official_replace_append_older():
    df = _frame()
    last = df.index[-1]
    bar = {"date": last, "Open": 10, "High": 12, "Low": 9.5, "Close": 11.5, "Volume": 5000}
    out, act = merge_official(df, bar)
    assert act == "replaced" and out.loc[last, "Volume"] == 5000 and out.loc[last, "Turnover"] == 11.5 * 5000
    assert len(out) == len(df)
    nxt = last + pd.offsets.BDay(1)
    out, act = merge_official(df, {**bar, "date": nxt})
    assert act == "appended" and out.index[-1] == nxt and len(out) == len(df) + 1
    out, act = merge_official(df, {**bar, "date": df.index[0]})
    assert act == "older" and out.equals(df)
    out, act = merge_official(df, None)
    assert act == "none" and out.equals(df)


if __name__ == "__main__":
    import sys
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ✓ {name}")
            except AssertionError as e:
                fails += 1
                print(f"  ✗ {name}: {e}")
    print("全部通過" if not fails else f"{fails} 項失敗")
    sys.exit(1 if fails else 0)
