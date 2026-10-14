# -*- coding: utf-8 -*-
"""
test_pullback_phase2.py — Phase 2 股票池與統計的單元測試(不需網路)

用法:python test_pullback_phase2.py
"""
import numpy as np
import pandas as pd

from pullback_phase2 import universe_membership, boot_ci, forward_panel


def _panel(days=200, stocks=30, seed=1):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2023-01-02", periods=days)
    return pd.DataFrame(rng.lognormal(10, 1, (days, stocks)), index=idx,
                        columns=[str(1000 + i) for i in range(stocks)])


def test_universe_no_lookahead():
    """截斷 t 之後的資料,t(含)以前每一天的入池名單都不能變。"""
    p = _panel()
    full = universe_membership(p, 10)
    for t in (70, 120, 199):
        cut = universe_membership(p.iloc[:t + 1], 10)
        assert cut.equals(full.iloc[:t + 1]), t


def test_universe_excludes_signal_day():
    """當日成交值暴增不影響當日入池(用的是前60日,不含當日)。"""
    p = _panel()
    base = universe_membership(p, 10)
    p2 = p.copy()
    t = p.index[150]
    low = p.columns[base.loc[t].values.argmin()]          # 當日不在池內的一檔
    p2.loc[t, low] = 1e15
    assert universe_membership(p2, 10).loc[t, low] == base.loc[t, low]
    assert universe_membership(p2, 10).iloc[151:].loc[:, low].any()   # 隔天起才可能入池


def test_universe_min_history_and_size():
    from pullback_phase2 import MIN_TURNOVER_DAYS
    p = _panel()
    m = universe_membership(p, 10)
    assert not m.iloc[:MIN_TURNOVER_DAYS].any().any()     # 資料不足 MIN_TURNOVER_DAYS 日 → 不入池
    assert (m.iloc[61:].sum(axis=1) == 10).all()


def test_universe_survives_missing_day():
    """單日缺漏(Yahoo 全市場缺某天)不可讓股票之後60日都出局(2025-08~09 股票池為空的根因)。"""
    p = _panel()
    p.iloc[100, :] = np.nan                                 # 全市場缺一天
    m = universe_membership(p, 10)
    assert (m.iloc[102:161].sum(axis=1) == 10).all()


def test_boot_ci_clusters_by_date():
    """同一天重複的觀測不應讓 CI 變窄(以日期為群組)。"""
    rng = np.random.default_rng(0)
    v = rng.normal(0.01, 0.05, 40)
    d = [f"2024-01-{i % 20 + 1:02d}" for i in range(40)]
    lo, hi = boot_ci(v, d)
    assert lo < np.mean(v) < hi
    v2 = np.repeat(v[:20], 10)                             # 20天、每天10筆完全相同
    d2 = np.repeat([f"2024-02-{i + 1:02d}" for i in range(20)], 10)
    lo2, hi2 = boot_ci(v2, d2)
    lo3, hi3 = boot_ci(v[:20], [f"2024-02-{i + 1:02d}" for i in range(20)])
    assert abs((hi2 - lo2) - (hi3 - lo3)) < 0.01           # 重複10倍,CI 寬度不變


def test_forward_panel_matches_engine_definition():
    idx = pd.bdate_range("2024-01-01", periods=30)
    df = pd.DataFrame({"Open": np.arange(30) + 100.0, "Close": np.arange(30) + 100.5}, index=idx)
    f = forward_panel({"X": df}, idx, 5)["X"]
    t = 3                                                  # C[t+5]/O[t+1]-1
    assert abs(f.iloc[t] - (df["Close"].iloc[t + 5] / df["Open"].iloc[t + 1] - 1)) < 1e-12


def test_market_lists_aborts_on_partial_list():
    """任一邊清單抓取失敗或數量異常 → 直接中止,不可只用上市股繼續跑。"""
    import types
    import pullback_phase2 as P
    calls = {"n": 0}

    def fake_get(url, **kw):
        calls["n"] += 1
        if "tpex" in url:
            raise ConnectionError("TPEx down")
        return types.SimpleNamespace(raise_for_status=lambda: None,
                                     json=lambda: [{"Code": str(1000 + i)} for i in range(1000)])
    old_get, old_sleep = P.requests.get, P.time.sleep
    P.requests.get, P.time.sleep = fake_get, (lambda s: None)
    try:
        try:
            P.market_lists()
            raised = False
        except RuntimeError:
            raised = True
        assert raised
        assert calls["n"] == 1 + 4                         # TPEx 重試 4 次
    finally:
        P.requests.get, P.time.sleep = old_get, old_sleep


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
