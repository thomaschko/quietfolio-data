# -*- coding: utf-8 -*-
"""
============================================================
pullback_backtest.py — 策略A「非資訊性急跌反轉」價量回測(第一版)
============================================================
2026-10-11。依據:Ackman 訪談「高槓桿/短期化市場製造急跌折價」的想法,
以及 Chan(2003)、Avramov-Chordia-Goyal(2006) 的反轉文獻。

這版只測【價量部分】(不含月營收、MOPS 負面消息過濾、題材雷達候選池——
這些歷史資料這裡沒有)。目的是先回答:
  「對這幾檔股票,『急跌+爆量+反轉日確認』進場,事後報酬有沒有比隨機進場好?」

進場(全部只用當天以前的資訊,下一個交易日開盤買):
  1. 回落幅度:近3日內最大的「距20日高點回落」落在 [DD_MIN, DD_MAX]
  2. 爆量:近3日內有一天 成交量 >= VOL_MULT × 前20日均量 且 收黑
  3. 反轉日確認(CONFIRM=True):今日收盤 > 昨日最高價
出場(先到先出):
  - 目標:回補「波段回落幅度」的 TARGET_RECOVER 比例(盤中觸價,跳空則以開盤價)
  - 停損:收盤 <= 參考高點 × (1 - STOP_DD),隔日開盤出
  - 時間:持有滿 MAX_HOLD 個交易日,收盤出
成本:來回 COST_RT(預設 0.585% = 證交稅0.3% + 手續費0.1425%×2,未計券商折讓)

比較基準:
  - 同一檔股票【所有交易日】進場後 5/10/20 日的平均報酬(無條件基準)
  - 觸發日報酬 vs 基準 → 訊號本身的超額報酬(與出場規則無關)
  - 拆分:大盤同步回落(加權指數距20日高>=MKT_STRESS)vs 個股單獨跌

⚠ 重要限制(印在結果裡,不要忽略):
  - 股票是「近期熱門股」→ 事後挑選 → 選擇偏誤,回測結果會偏樂觀
  - 5檔×數年,交易筆數通常很少(幾十筆),統計力量弱
  - 大盤急殺時多檔同時觸發,事件並不獨立
  - 未模擬分三批進場、未含融資、未含流動性/漲跌停鎖死無法出場
  - 使用還原價(auto_adjust),已含除權息
============================================================
"""

import os
import sys
import json
import math
import argparse
import datetime as dt

import numpy as np
import pandas as pd

DEFAULTS = dict(
    DD_MIN=0.12, DD_MAX=0.30, VOL_MULT=2.0, CONFIRM=True,
    TARGET_RECOVER=0.60, STOP_DD=0.30, MAX_HOLD=20,
    COST_RT=0.00585, MKT_STRESS=0.05, COOLDOWN=5,
)


# ============================================================
# 核心引擎(純函式,方便用合成資料測試)
# ============================================================
def run_backtest(df, bench=None, **kw):
    """df: index=日期, 欄位 Open High Low Close Volume(已還原)。
    bench: 加權指數 Close 的 Series(index=日期),可為 None。
    回傳 (trades_df, baseline_dict)。"""
    p = {**DEFAULTS, **kw}
    d = df.copy().dropna(subset=["Open", "High", "Low", "Close"])
    d["Volume"] = d["Volume"].fillna(0)

    hi20 = d["Close"].rolling(20).max()
    dd = 1 - d["Close"] / hi20
    dd_trough3 = dd.rolling(3).max()                       # 近3日最深回落
    vol_ma = d["Volume"].rolling(20).mean().shift(1)       # 前20日均量(不含當日)
    cap_day = (d["Volume"] >= p["VOL_MULT"] * vol_ma) & (d["Close"] < d["Close"].shift(1))
    cap_recent = cap_day.rolling(3).max() > 0
    reversal = d["Close"] > d["High"].shift(1)
    in_band = (dd_trough3 >= p["DD_MIN"]) & (dd_trough3 <= p["DD_MAX"])

    if p["CONFIRM"]:
        trig = in_band & cap_recent & reversal
    else:
        trig = in_band & cap_day                            # 不等反轉,爆量收黑當天就觸發

    if bench is not None and len(bench):
        b = bench.reindex(d.index).ffill()
        b_dd = 1 - b / b.rolling(20).max()
        stress = (b_dd >= p["MKT_STRESS"]).fillna(False)
    else:
        stress = pd.Series(False, index=d.index)

    idx = d.index
    O, H, L, C = d["Open"].values, d["High"].values, d["Low"].values, d["Close"].values
    n = len(d)
    trades = []
    i = 0
    cooldown_until = -1
    while i < n - 2:
        if not bool(trig.iloc[i]) or i <= cooldown_until or np.isnan(hi20.iloc[i]):
            i += 1
            continue
        t = i                                               # 訊號日
        e = t + 1                                           # 進場日(下一日開盤)
        entry = O[e]
        ref_high = hi20.iloc[t]
        trough = np.nanmin(C[max(0, t - 2):t + 1])          # 近3日最低收盤
        target = trough + p["TARGET_RECOVER"] * (ref_high - trough)
        stop_level = ref_high * (1 - p["STOP_DD"])
        exit_i, exit_px, reason = None, None, None
        for k in range(e, min(n, e + p["MAX_HOLD"])):
            held = k - e + 1
            if H[k] >= target:                              # 盤中觸及目標(開盤已越過則以開盤價)
                exit_i, exit_px = k, (O[k] if O[k] >= target else target)
                reason = "target_gap" if O[k] >= target else "target"
                break
            if C[k] <= stop_level:
                if k + 1 < n:
                    exit_i, exit_px, reason = k + 1, O[k + 1], "stop"
                else:
                    exit_i, exit_px, reason = k, C[k], "stop_last"
                break
            if held >= p["MAX_HOLD"]:
                exit_i, exit_px, reason = k, C[k], "time"
                break
        if exit_i is None:                                  # 資料尾端尚未出場
            i = n
            break
        gross = exit_px / entry - 1
        rec = dict(
            signal_date=idx[t].date(), entry_date=idx[e].date(), exit_date=idx[exit_i].date(),
            entry=round(entry, 2), exit=round(exit_px, 2), reason=reason,
            hold_days=exit_i - e + 1, dd_trough=round(float(dd_trough3.iloc[t]), 3),
            market_stress=bool(stress.iloc[t]),
            gross=gross, net=gross - p["COST_RT"],
        )
        for h in (5, 10, 20):                               # 固定天數報酬(與出場規則無關)
            rec[f"fwd{h}"] = (C[e + h - 1] / entry - 1) if e + h - 1 < n else np.nan
        trades.append(rec)
        cooldown_until = exit_i + p["COOLDOWN"]
        i = exit_i + 1

    # 無條件基準:任一日開盤進場,持有h日收盤
    base = {}
    for h in (5, 10, 20):
        # 進場日 = 隔日開盤:用 (t+h) 收盤 / (t+1) 開盤
        ent = pd.Series(O, index=idx).shift(-1).values
        ex = pd.Series(C, index=idx).shift(-h).values
        r = ex / ent - 1
        base[f"fwd{h}"] = float(np.nanmean(r)) if np.isfinite(r).any() else np.nan
    return pd.DataFrame(trades), base


# ============================================================
# 統計摘要
# ============================================================
def _stats(x):
    x = pd.Series(x).dropna()
    n = len(x)
    if n == 0:
        return dict(n=0)
    sd = x.std(ddof=1) if n > 1 else float("nan")
    return dict(n=n, mean=float(x.mean()), median=float(x.median()),
                win=float((x > 0).mean()),
                t=float(x.mean() / (sd / math.sqrt(n))) if n > 1 and sd and sd > 0 else float("nan"))


def summarize(trades, base):
    out = {"baseline_unconditional": base}
    if trades is None or len(trades) == 0:
        out["note"] = "無觸發交易"
        return out
    out["all"] = {
        "net": _stats(trades["net"]),
        "hold_days_mean": float(trades["hold_days"].mean()),
        "reasons": trades["reason"].value_counts().to_dict(),
        "worst_net": float(trades["net"].min()),
    }
    for h in (5, 10, 20):
        s = _stats(trades[f"fwd{h}"])
        s["excess_vs_baseline_mean"] = (s["mean"] - base[f"fwd{h}"]) if s.get("n") else None
        out["all"][f"fwd{h}"] = s
    out["by_market_stress"] = {
        str(k): {"net": _stats(g["net"]), "fwd10": _stats(g["fwd10"])}
        for k, g in trades.groupby("market_stress")}
    bins = pd.cut(trades["dd_trough"], [0.12, 0.18, 0.24, 0.30], include_lowest=True)
    out["by_drawdown"] = {str(k): {"net": _stats(g["net"]), "fwd10": _stats(g["fwd10"])}
                          for k, g in trades.groupby(bins, observed=True)}
    return out


# ============================================================
# 資料下載(yfinance;每檔先試上市 .TW 再試上櫃 .TWO)
# ============================================================
def load_prices(code, start):
    import yfinance as yf
    for suf in (".TW", ".TWO"):
        try:
            h = yf.Ticker(code + suf).history(start=start, auto_adjust=True)
        except Exception as e:
            print(f"    ⚠ {code}{suf}: {str(e)[:60]}")
            continue
        if h is not None and len(h) > 250:
            h.index = pd.to_datetime(h.index).tz_localize(None).normalize()
            return h[["Open", "High", "Low", "Close", "Volume"]], suf
    return None, None


def load_bench(start):
    import yfinance as yf
    try:
        h = yf.Ticker("^TWII").history(start=start)
        h.index = pd.to_datetime(h.index).tz_localize(None).normalize()
        return h["Close"]
    except Exception as e:
        print(f"  ⚠ 加權指數下載失敗: {e}")
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", default="3081,3665,2454,3026,6488")
    ap.add_argument("--start", default="2019-01-01")
    ap.add_argument("--out", default="backtest_out")
    ap.add_argument("--no-confirm", action="store_true", help="不等反轉日確認(測接刀)")
    a = ap.parse_args()

    os.makedirs(a.out, exist_ok=True)
    bench = load_bench(a.start)
    print(f"加權指數: {'OK ' + str(len(bench)) + '天' if bench is not None else '無'}")

    all_tr, per_stock = [], {}
    for code in [c.strip() for c in a.tickers.split(",") if c.strip()]:
        df, suf = load_prices(code, a.start)
        if df is None:
            print(f"  ✗ {code} 下載失敗或資料不足")
            per_stock[code] = {"error": "no data"}
            continue
        tr, base = run_backtest(df, bench, CONFIRM=not a.no_confirm)
        tr.insert(0, "code", code) if len(tr) else None
        print(f"  {code}{suf}: {len(df)}日 ({df.index[0].date()}~{df.index[-1].date()}) → 觸發 {len(tr)} 筆")
        per_stock[code] = {"suffix": suf, "days": len(df),
                           "span": [str(df.index[0].date()), str(df.index[-1].date())],
                           "buy_hold": float(df['Close'].iloc[-1] / df['Close'].iloc[0] - 1),
                           **summarize(tr, base)}
        if len(tr):
            all_tr.append(tr)

    trades = pd.concat(all_tr, ignore_index=True) if all_tr else pd.DataFrame()
    # 合併基準:各檔無條件基準的簡單平均
    bases = [v["baseline_unconditional"] for v in per_stock.values() if "baseline_unconditional" in v]
    pooled_base = {k: float(np.nanmean([b[k] for b in bases])) for k in ("fwd5", "fwd10", "fwd20")} if bases else {}
    pooled = summarize(trades, pooled_base) if len(trades) else {"note": "無觸發交易"}

    result = {"generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
              "params": {**DEFAULTS, "CONFIRM": not a.no_confirm, "start": a.start},
              "pooled": pooled, "per_stock": per_stock,
              "warnings": ["近期熱門股事後挑選→選擇偏誤", "交易筆數少統計力量弱",
                           "大盤急殺時多檔同時觸發,事件不獨立",
                           "未模擬分批進場/融資/漲跌停鎖死無法出場"]}
    tag = "noconfirm" if a.no_confirm else "confirm"
    if len(trades):
        trades.to_csv(os.path.join(a.out, f"pullback_trades_{tag}.csv"), index=False, encoding="utf-8-sig")
    with open(os.path.join(a.out, f"pullback_summary_{tag}.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1, default=str)
    print(json.dumps(pooled, ensure_ascii=False, indent=1, default=str)[:3000])
    print("\n⚠ " + " | ".join(result["warnings"]))


if __name__ == "__main__":
    main()
