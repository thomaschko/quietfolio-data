# -*- coding: utf-8 -*-
"""
test_pullback_backtest.py — pullback_backtest.run_backtest 引擎單元測試(合成價格路徑,不需網路)

用法:python test_pullback_backtest.py   (或 pytest test_pullback_backtest.py)

情境(預設參數 DD 12–30%、爆量2倍、反轉確認、目標回補60%、停損-30%、最長20日):
  前30日收盤 100、量 1000 → 第30日爆量收黑 85 → 第31日 84 → 第32日收 87(>前日高)= 訊號日
  → 第33日開盤進場;參考高點 100、近3日最低收盤 84,目標價 = 84 + 0.6×16 = 93.6、停損線 70。
"""
import numpy as np
import pandas as pd

from pullback_backtest import run_backtest


def make_df(closes, vols, start="2024-01-01"):
    closes = np.asarray(closes, dtype=float)
    opens = np.r_[closes[0], closes[:-1]]          # 開盤 = 前日收盤
    highs = np.maximum(opens, closes) * 1.002
    lows = np.minimum(opens, closes) * 0.998
    idx = pd.bdate_range(start, periods=len(closes))
    return pd.DataFrame({"Open": opens, "High": highs, "Low": lows,
                         "Close": closes, "Volume": np.asarray(vols, dtype=float)}, index=idx)


BASE = [100.0] * 30
CRASH = [85.0, 84.0, 87.0]                       # 第30(爆量收黑)、31、32(反轉)日
BASE_VOL = [1000] * 30
CRASH_VOL = [3000, 1500, 1200]


def scenario(after, crash=CRASH, crash_vol=CRASH_VOL):
    closes = BASE + list(crash) + list(after)
    vols = BASE_VOL + list(crash_vol) + [1000] * len(after)
    return make_df(closes, vols)


def test_target_exit():
    df = scenario([88, 90, 95, 96] + [96] * 30)
    tr, _ = run_backtest(df)
    assert len(tr) == 1, tr
    t = tr.iloc[0]
    assert t["signal_date"] == df.index[32].date() and t["entry_date"] == df.index[33].date()
    assert t["reason"] in ("target", "target_gap") and abs(t["exit"] - 93.6) < 0.05, t
    assert t["exit_date"] == df.index[35].date()           # 第35日盤中觸及 93.6


def test_stop_exit():
    df = scenario([86, 80, 75, 69, 68] + [68] * 30)
    tr, _ = run_backtest(df)
    assert len(tr) == 1
    t = tr.iloc[0]
    assert t["reason"] == "stop", t                         # 第36日收 69 ≤ 70 → 第37日開盤出
    assert t["exit_date"] == df.index[37].date() and abs(t["exit"] - 69) < 1e-6
    assert t["net"] < 0


def test_time_exit():
    df = scenario([88] * 30)
    tr, _ = run_backtest(df)
    assert len(tr) == 1
    t = tr.iloc[0]
    assert t["reason"] == "time" and t["hold_days"] == 20, t
    assert t["exit_date"] == df.index[33 + 19].date()


def test_no_trigger_low_volume():
    df = scenario([88] * 30, crash_vol=[1500, 1200, 1200])  # 1.5倍 < 2倍
    tr, _ = run_backtest(df)
    assert len(tr) == 0


def test_no_trigger_drawdown_too_deep():
    df = scenario([70] * 30, crash=[65.0, 64.0, 67.0])     # 回落 36% > 30%
    tr, _ = run_backtest(df)
    assert len(tr) == 0


def test_entry_after_signal_and_noconfirm():
    df = scenario([88, 90, 95, 96] + [96] * 30)
    tr, _ = run_backtest(df, CONFIRM=False)
    assert len(tr) >= 1
    t = tr.iloc[0]
    assert t["signal_date"] == df.index[30].date()          # 不等反轉:爆量收黑當天觸發
    assert t["entry_date"] > t["signal_date"]


def test_eligible_mask():
    """Phase 2 股票池限制:eligible=None 與原本相同;訊號日不在池內就不觸發。"""
    df = scenario([88, 90, 95, 96] + [96] * 30)
    tr0, _ = run_backtest(df)
    tr1, _ = run_backtest(df, eligible=pd.Series(True, index=df.index))
    assert tr0.equals(tr1)
    mask = pd.Series(True, index=df.index)
    mask.iloc[32] = False                                   # 訊號日(第32日)不在池內
    tr2, _ = run_backtest(df, eligible=mask)
    assert len(tr2) == 0 or tr2.iloc[0]["signal_date"] != df.index[32].date()


def test_no_lookahead():
    """訊號日之後的資料全部亂改,第一筆交易的訊號日與參考值不得改變。"""
    rng = np.random.default_rng(7)
    closes = list(100 * np.exp(np.cumsum(rng.normal(0, 0.025, 400))))
    vols = list(rng.integers(800, 1200, 400).astype(float))
    for k in (120, 260):                                    # 塞兩段急跌爆量反轉
        base = closes[k - 1]
        closes[k], closes[k + 1], closes[k + 2] = base * 0.82, base * 0.81, base * 0.84
        vols[k] = 5000
    df = make_df(closes, vols)
    tr, _ = run_backtest(df)
    assert len(tr) >= 1
    for _, first in tr.iterrows():
        t_pos = df.index.get_loc(pd.Timestamp(first["signal_date"]))
        df2 = df.copy()
        future = df2.index > df2.index[t_pos]
        noise = rng.uniform(0.5, 1.5, future.sum())
        for col in ("Open", "High", "Low", "Close"):
            df2.loc[future, col] = df2.loc[future, col] * noise
        df2.loc[future, "High"] = df2.loc[future, ["Open", "High", "Low", "Close"]].max(axis=1)
        df2.loc[future, "Low"] = df2.loc[future, ["Open", "High", "Low", "Close"]].min(axis=1)
        df2.loc[future, "Volume"] = rng.integers(0, 50000, future.sum())
        tr2, _ = run_backtest(df2)
        sig2 = set(tr2["signal_date"]) if len(tr2) else set()
        assert first["signal_date"] in sig2, (first["signal_date"], sig2)
        row2 = tr2[tr2["signal_date"] == first["signal_date"]].iloc[0]
        assert row2["dd_trough"] == first["dd_trough"]
        break   # 第一筆即可(之後的交易受冷卻期影響,本來就可能隨未來資料改變)


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
