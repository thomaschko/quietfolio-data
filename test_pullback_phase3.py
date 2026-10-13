# -*- coding: utf-8 -*-
"""
test_pullback_phase3.py — Phase 3 選參規則與對照組的單元測試(不需網路)

用法:python test_pullback_phase3.py
"""
import numpy as np
import pandas as pd

from pullback_backtest import DEFAULTS
from pullback_phase3 import matched_random, select_params, skip_gap, GRID, MIN_DATES


def test_matched_random_uses_only_universe_at_signal_date():
    cal = pd.bdate_range("2024-01-01", periods=30)
    cols = ["A", "B", "C", "D"]
    opens = pd.DataFrame(100.0, index=cal, columns=cols)
    closes = pd.DataFrame(100.0, index=cal, columns=cols)
    closes["B"] = 110.0                      # B 持有期間 +10%
    closes["C"] = 50.0                       # C 不在池內,不該被抽到
    member = pd.DataFrame(False, index=cal, columns=cols)
    member.loc[cal[5], ["A", "B"]] = True    # 訊號日池內只有 A(自己)與 B
    tr = pd.DataFrame([{"code": "A", "signal_date": cal[5].date(), "hold_days": 3}])
    m = matched_random(tr, member, opens, closes, cost=0.01)
    assert abs(m.iloc[0] - (110 / 100 - 1 - 0.01)) < 1e-12, m.iloc[0]   # 只抽到 B,排除自己與池外 C


def test_matched_random_holding_window():
    cal = pd.bdate_range("2024-01-01", periods=30)
    opens = pd.DataFrame({"A": 100.0, "B": np.arange(30) + 100.0}, index=cal)
    closes = pd.DataFrame({"A": 100.0, "B": np.arange(30) + 100.5}, index=cal)
    member = pd.DataFrame(True, index=cal, columns=["A", "B"])
    tr = pd.DataFrame([{"code": "A", "signal_date": cal[10].date(), "hold_days": 4}])
    m = matched_random(tr, member, opens, closes, cost=0.0)
    exp = closes["B"].iloc[10 + 4] / opens["B"].iloc[11] - 1   # t+1 開盤進、持有4天收盤出
    assert abs(m.iloc[0] - exp) < 1e-12


def _sens(netmap, n_dates=100):
    return {p: [(v, {"n": 100, "n_dates": n_dates, "net_mean": netmap.get((p, v), 0.01)})
                for v in vals] for p, vals in GRID.items()}


def test_select_params_ignores_isolated_spike():
    # DD_MIN:0.18 單點尖峰(左右很差),0.10~0.12 是平穩高原 → 應選高原,不選尖峰
    netmap = {("DD_MIN", 0.08): 0.02, ("DD_MIN", 0.10): 0.03, ("DD_MIN", 0.12): 0.03,
              ("DD_MIN", 0.15): -0.03, ("DD_MIN", 0.18): 0.08}
    chosen, _ = select_params(_sens(netmap))
    assert chosen["DD_MIN"] in (0.10, 0.12), chosen["DD_MIN"]


def test_select_params_requires_min_dates():
    s = _sens({("VOL_MULT", 3.0): 0.5})
    s["VOL_MULT"] = [(v, {**st, "n_dates": (MIN_DATES - 1 if v == 3.0 else st["n_dates"])})
                     for v, st in s["VOL_MULT"]]
    chosen, _ = select_params(s)
    assert chosen["VOL_MULT"] != 3.0


def test_select_params_flat_keeps_first_best():
    chosen, _ = select_params(_sens({}))
    assert set(chosen) == set(GRID) and all(chosen[p] in GRID[p] for p in GRID)


def test_skip_gap_only_drops_entry_day_gap():
    tr = pd.DataFrame([{"reason": "target_gap", "hold_days": 1}, {"reason": "target_gap", "hold_days": 3},
                       {"reason": "target", "hold_days": 1}, {"reason": "stop", "hold_days": 5}])
    assert len(skip_gap(tr)) == 3


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
