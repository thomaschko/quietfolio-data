# -*- coding: utf-8 -*-
"""
============================================================
pullback_phase3.py — Pullback 回測 Phase 3:穩健性(2026-10-13)
============================================================
沿用 Phase 2 的股票池(前60日平均成交值前 TOP 名,只用當時資料)與資料下載。

1. 時間切分:訊號日 <= IS_END 為樣本內,>= OOS_START 為樣本外。
2. 參數敏感度(只用樣本內):六個參數各自變動、其餘維持預設(one-at-a-time),
   每個設定記錄 n、不重複訊號日、net 平均與 CI,並畫成圖(phase3_sensitivity.png)。
3. 事前登錄的選參規則(寫死在 SELECTION_RULE,先輸出再跑樣本外):
   每個參數取「該值與左右相鄰值的樣本內 net 平均」最高者,
   且該值本身不重複訊號日 >= MIN_DATES;組合成 selected 設定。
4. 樣本外只跑一次:預設參數與 selected 各一次,之後不回頭改。
5. 對照:
   - ex_day10:固定10日報酬 − 訊號日池內平均(Phase 2 同口徑)
   - matched:同訊號日、池內隨機 MATCH_K 檔、持有相同天數(隔日開盤進、出場日收盤出)
     扣同樣成本 → 分離「出場規則」與「訊號」的效果
   - 日期等權:先對同一天的交易取平均,再對日期平均(事件叢集)
   - skip_gap:進場日開盤已越過目標價就不買(進場當下可知,非未來資訊)
6. 所有結果附 n、不重複日期數、日期分群 bootstrap 95% CI;n<30 標示。

用法:python pullback_phase3.py --start 2019-01-01 --top 200
輸出:backtest_out/phase3_summary.json、phase3_sensitivity.png、phase3_trades_oos_*.csv
============================================================
"""
import os
import json
import argparse
import datetime as dt

import numpy as np
import pandas as pd

from pullback_backtest import DEFAULTS
from pullback_phase2 import (market_lists, download, universe_membership, forward_panel,
                             boot_ci, run_set, PHASE1_TICKERS, TPE)
from pullback_backtest import load_bench

IS_END = pd.Timestamp("2022-12-31")
OOS_START = pd.Timestamp("2023-01-01")
MIN_N = 30
MIN_DATES = 30
MATCH_K = 20
GRID = {
    "DD_MIN": [0.08, 0.10, 0.12, 0.15, 0.18],
    "DD_MAX": [0.20, 0.25, 0.30, 0.35, 0.40],
    "VOL_MULT": [1.5, 1.75, 2.0, 2.5, 3.0],
    "TARGET_RECOVER": [0.40, 0.50, 0.60, 0.75, 0.90],
    "STOP_DD": [0.20, 0.25, 0.30, 0.35, 0.40],
    "MAX_HOLD": [5, 10, 20, 30, 40],
}
SELECTION_RULE = ("每個參數各自取『該值與左右相鄰值的樣本內 net 平均』最高者(端點只和一個相鄰值平均),"
                  f"且該值本身樣本內不重複訊號日 >= {MIN_DATES};六個參數的選擇組合成 selected。"
                  "此規則在樣本外執行前寫入輸出,樣本外只跑一次。")


# ============================================================
# 統計輔助
# ============================================================
def matched_random(tr, member, opens, closes, cost, k=MATCH_K, seed=0):
    """每筆交易:同訊號日池內隨機 k 檔(排除自己),隔日開盤進、持有相同天數收盤出,扣成本。
    回傳每筆的對照平均報酬(沒有可抽樣本時為 NaN)。只用訊號日的池名單與實際持有期間價格。"""
    if tr is None or len(tr) == 0:
        return pd.Series(dtype=float)
    rng = np.random.default_rng(seed)
    cal = opens.index
    pos = {d: i for i, d in enumerate(cal)}
    out = []
    for _, r in tr.iterrows():
        t = pd.Timestamp(r["signal_date"])
        i = pos.get(t)
        if i is None or i + int(r["hold_days"]) >= len(cal):
            out.append(np.nan)
            continue
        pool = [c for c in member.columns[member.loc[t].values] if c != r["code"]]
        if not pool:
            out.append(np.nan)
            continue
        pick = rng.choice(pool, size=min(k, len(pool)), replace=False)
        e, x = i + 1, i + int(r["hold_days"])          # 進場日 t+1,持有 hold_days 天後收盤
        ret = closes.iloc[x][pick].values / opens.iloc[e][pick].values - 1 - cost
        out.append(float(np.nanmean(ret)) if np.isfinite(ret).any() else np.nan)
    return pd.Series(out, index=tr.index)


def date_equal_weight(values, dates):
    s = pd.DataFrame({"v": values, "d": dates}).dropna()
    return float(s.groupby("d")["v"].mean().mean()) if len(s) else None


def stats(tr, label):
    if tr is None or len(tr) == 0:
        return {"label": label, "n": 0}
    d = tr["signal_date"].astype(str)
    out = {"label": label, "n": int(len(tr)), "n_dates": int(d.nunique()),
           "small_sample": bool(len(tr) < MIN_N),
           "net_mean": float(tr["net"].mean()), "net_median": float(tr["net"].median()),
           "net_win": float((tr["net"] > 0).mean()), "net_ci95": boot_ci(tr["net"], d),
           "net_date_equal_weight": date_equal_weight(tr["net"], d),
           "hold_mean": float(tr["hold_days"].mean()), "worst_net": float(tr["net"].min()),
           "reasons": tr["reason"].value_counts().to_dict(),
           "entry_day_target_gap": int(((tr["reason"] == "target_gap") & (tr["hold_days"] == 1)).sum())}
    if "ex_day10" in tr:
        out["ex_day10_mean"] = float(tr["ex_day10"].mean())
        out["ex_day10_ci95"] = boot_ci(tr["ex_day10"], d)
    if "matched" in tr:
        ex = tr["net"] - tr["matched"]
        out["matched_mean"] = float(tr["matched"].mean())
        out["net_minus_matched_mean"] = float(ex.mean())
        out["net_minus_matched_ci95"] = boot_ci(ex, d)
    return out


def skip_gap(tr):
    """進場日開盤已越過目標價(引擎記為 target_gap 且持有1日)就不買。"""
    if tr is None or len(tr) == 0:
        return tr
    return tr[~((tr["reason"] == "target_gap") & (tr["hold_days"] == 1))]


def select_params(sens):
    """sens: {param: [(value, stats), ...]}(依 GRID 順序)。依 SELECTION_RULE 選值。"""
    chosen, detail = {}, {}
    for p, rows in sens.items():
        vals = [r[1].get("net_mean") if r[1].get("n") else None for r in rows]
        best, best_score = DEFAULTS[p], None
        scores = []
        for i, (v, st) in enumerate(rows):
            neigh = [vals[j] for j in (i - 1, i, i + 1) if 0 <= j < len(rows) and vals[j] is not None]
            score = float(np.mean(neigh)) if neigh else None
            ok = st.get("n_dates", 0) >= MIN_DATES and score is not None
            scores.append({"value": v, "smoothed_net": score, "eligible": ok})
            if ok and (best_score is None or score > best_score):
                best, best_score = v, score
        chosen[p], detail[p] = best, scores
    return chosen, detail


def plot_sensitivity(sens, path):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as e:
        print(f"  ⚠ 無法畫圖: {e}")
        return False
    fig, axes = plt.subplots(2, 3, figsize=(13, 7))
    for ax, (p, rows) in zip(axes.flat, sens.items()):
        xs = [r[0] for r in rows]
        m = [r[1].get("net_mean", np.nan) * 100 if r[1].get("n") else np.nan for r in rows]
        lo = [r[1]["net_ci95"][0] * 100 if r[1].get("n") else np.nan for r in rows]
        hi = [r[1]["net_ci95"][1] * 100 if r[1].get("n") else np.nan for r in rows]
        ax.fill_between(xs, lo, hi, alpha=0.2)
        ax.plot(xs, m, marker="o")
        ax.axhline(0, color="grey", lw=0.8)
        ax.axvline(DEFAULTS[p], color="red", ls="--", lw=0.8)
        for x, r in zip(xs, rows):
            ax.annotate(f"n={r[1].get('n', 0)}", (x, ax.get_ylim()[0]), fontsize=7, ha="center", va="bottom")
        ax.set_title(f"{p} (in-sample, net %, 95% CI)")
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    return True


# ============================================================
# 主流程
# ============================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2019-01-01")
    ap.add_argument("--top", type=int, default=200)
    ap.add_argument("--out", default="backtest_out")
    ap.add_argument("--limit", type=int, default=0, help="除錯用:只取前 N 檔")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    end = dt.datetime.now(TPE).date().isoformat()

    lists = market_lists()
    for c in PHASE1_TICKERS:
        lists.setdefault(c, ".TWO" if c in ("3081", "6488") else ".TW")
    if a.limit:
        keep = sorted(lists)[:a.limit]
        lists = {c: lists[c] for c in set(keep) | set(PHASE1_TICKERS)}
    data, failed = download(lists, a.start, end)
    bench = load_bench(a.start)
    if bench is not None:
        bench = bench[bench.index < pd.Timestamp(end)]
    cal = bench.index
    turnover = pd.DataFrame({c: d["Turnover"] for c, d in data.items()}).reindex(cal)
    opens = pd.DataFrame({c: d["Open"] for c, d in data.items()}).reindex(cal)
    closes = pd.DataFrame({c: d["Close"] for c, d in data.items()}).reindex(cal)
    fwd = {h: forward_panel(data, cal, h) for h in (5, 10, 20)}
    member = universe_membership(turnover, a.top)
    is_mask = member & pd.DataFrame(np.repeat((cal <= IS_END)[:, None], member.shape[1], 1),
                                    index=cal, columns=member.columns)
    oos_mask = member & pd.DataFrame(np.repeat((cal >= OOS_START)[:, None], member.shape[1], 1),
                                     index=cal, columns=member.columns)
    codes_is = [c for c in member.columns if is_mask[c].any()]
    codes_oos = [c for c in member.columns if oos_mask[c].any()]
    cost = DEFAULTS["COST_RT"]

    def run(mask, codes, **params):
        tr = run_set(data, bench, fwd, mask, codes, True, **params)
        if len(tr):
            tr["matched"] = matched_random(tr, member, opens, closes, cost).values
        return tr

    summary = {
        "generated_at": dt.datetime.now(TPE).strftime("%Y-%m-%d %H:%M:%S"),
        "params_default": DEFAULTS, "top": a.top, "in_sample_end": str(IS_END.date()),
        "out_of_sample_start": str(OOS_START.date()), "grid": GRID, "selection_rule": SELECTION_RULE,
        "data": {"usable": len(data), "failed_or_short": len(failed),
                 "span": [str(cal[0].date()), str(cal[-1].date())]},
        "warnings": ["倖存者偏誤:股票清單取自今天仍上市櫃的普通股 → 結果偏樂觀",
                     "敏感度為一次變一個參數(one-at-a-time),未涵蓋參數間交互作用",
                     "matched 對照以出場日收盤計,實際交易的目標價/停損成交價不同,屬近似"],
    }

    # ── 1. 樣本內:預設 + 敏感度 ──
    print("[IS] 預設參數")
    is_default = run(is_mask, codes_is)
    summary["in_sample_default"] = stats(is_default, "IS default")
    sens = {}
    for p, values in GRID.items():
        sens[p] = []
        for v in values:
            if v == DEFAULTS[p]:
                sens[p].append((v, summary["in_sample_default"]))
                continue
            print(f"[IS] {p}={v}")
            sens[p].append((v, stats(run(is_mask, codes_is, **{p: v}), f"IS {p}={v}")))
    summary["sensitivity_in_sample"] = {p: [{"value": v, **st} for v, st in rows] for p, rows in sens.items()}
    selected, detail = select_params(sens)
    summary["selected_params"] = selected
    summary["selection_detail"] = detail
    summary["plot"] = plot_sensitivity(sens, os.path.join(a.out, "phase3_sensitivity.png"))
    # 選參結果先落地,再跑樣本外
    with open(os.path.join(a.out, "phase3_summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1, default=str)
    print(f"[選參] {selected}")

    # ── 2. 樣本外:只跑一次 ──
    oos = {}
    for name, params in (("default", {}), ("selected", {k: v for k, v in selected.items() if v != DEFAULTS[k]})):
        print(f"[OOS] {name} {params}")
        tr = run(oos_mask, codes_oos, **params)
        if len(tr):
            tr.to_csv(os.path.join(a.out, f"phase3_trades_oos_{name}.csv"), index=False, encoding="utf-8-sig")
        oos[name] = {"params": {**DEFAULTS, **params}, "all": stats(tr, f"OOS {name}"),
                     "skip_gap": stats(skip_gap(tr), f"OOS {name} skip_gap")}
        if len(tr):
            oos[name]["market_stress"] = {str(k): stats(g, f"OOS {name} stress={k}")
                                          for k, g in tr.groupby("market_stress")}
            top_dates = tr.groupby("signal_date").size().sort_values(ascending=False).head(10)
            oos[name]["largest_clusters"] = {str(k): int(v) for k, v in top_dates.items()}
    summary["out_of_sample"] = oos
    summary["in_sample_default_skip_gap"] = stats(skip_gap(is_default), "IS default skip_gap")
    if len(is_default):
        top_dates = is_default.groupby("signal_date").size().sort_values(ascending=False).head(10)
        summary["in_sample_largest_clusters"] = {str(k): int(v) for k, v in top_dates.items()}

    def count_cells(o):
        if isinstance(o, dict):
            return (1 if "net_ci95" in o else 0) + sum(count_cells(v) for v in o.values())
        if isinstance(o, list):
            return sum(count_cells(v) for v in o)
        return 0
    summary["tests_count"] = count_cells({k: summary[k] for k in
                                          ("in_sample_default", "sensitivity_in_sample", "out_of_sample",
                                           "in_sample_default_skip_gap")})
    with open(os.path.join(a.out, "phase3_summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1, default=str)

    def line(s):
        if not s.get("n"):
            return f"{s['label']}: 無交易"
        f = lambda x: f"{x*100:+.2f}%"
        return (f"{s['label']}: n={s['n']} 日期={s['n_dates']} net={f(s['net_mean'])} "
                f"CI[{f(s['net_ci95'][0])},{f(s['net_ci95'][1])}] 日期等權={f(s['net_date_equal_weight'])} "
                f"net−matched={f(s.get('net_minus_matched_mean', float('nan')))} "
                f"CI[{f(s['net_minus_matched_ci95'][0])},{f(s['net_minus_matched_ci95'][1])}] "
                f"ex_day10={f(s.get('ex_day10_mean', float('nan')))}" + (" ⚠樣本不足" if s["small_sample"] else ""))
    print("\n=== Phase 3 摘要 ===")
    print(line(summary["in_sample_default"]))
    for name in ("default", "selected"):
        print(line(oos[name]["all"]))
        print(line(oos[name]["skip_gap"]))
    print(f"檢定格數 {summary['tests_count']}")


if __name__ == "__main__":
    main()
