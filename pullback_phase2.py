# -*- coding: utf-8 -*-
"""
============================================================
pullback_phase2.py — Pullback 回測 Phase 2:去除選擇偏誤(2026-10-13)
============================================================
Phase 1 的 5 檔是近期熱門股(事後挑選),結果必然偏樂觀。Phase 2:
  1. 股票池不事後挑選:每個交易日以「前60個交易日(不含當日)平均成交值」
     排名,取前 N(100 / 200)。只用當時資料計算。
  2. 參數固定為 pullback_backtest.DEFAULTS,不調參;有確認 / 不等反轉兩版。
  3. 引擎只多傳 eligible(當日是否在池內),其餘邏輯不變。
  4. 統計:
     - 每筆淨報酬(扣成本);固定 5/10/20 日報酬
     - 超額報酬兩種:(a) 減「同一檔在池內所有日子」的無條件報酬
                      (b) 減「訊號日當天池內全部股票」的平均報酬(去除大盤漲跌)
     - 95% 信賴區間:以「訊號日期」為群組重抽的 bootstrap(同一天多筆綁在一起)
     - 每格附 n 與有效樣本數(不重複訊號日數);n<30 標示樣本不足
     - 拆分:大盤同步 vs 個股單獨、回落幅度三桶
  5. 對照:Phase 1 的 5 檔用同一份資料、同一套統計重跑(不限股票池)

⚠ 倖存者偏誤:股票清單取自「今天」仍上市/上櫃的普通股,已下市股票 yfinance
  通常也沒有資料 → 股票池只含存活至今的標的,結果偏樂觀(寫進輸出 warnings)。
⚠ 資料:yfinance(auto_adjust=False 取 Adj Close 自行還原 OHLC);截止日為
  執行日前一天(避開當日未定稿資料)。下載失敗的代號如實列出,不補值。

用法:python pullback_phase2.py --start 2019-01-01 --top 100,200
輸出:backtest_out/phase2_summary.json、phase2_trades_{confirm|noconfirm}_top{N}.csv
============================================================
"""
import os
import re
import json
import time
import argparse
import datetime as dt

import numpy as np
import pandas as pd
import requests

from pullback_backtest import run_backtest, load_bench, DEFAULTS

PHASE1_TICKERS = ["3081", "3665", "2454", "3026", "6488"]
TURNOVER_WINDOW = 60
BOOT = 2000
MIN_N = 30
HORIZONS = (5, 10, 20)
TPE = dt.timezone(dt.timedelta(hours=8))


# ============================================================
# 股票清單與資料
# ============================================================
def market_lists():
    """今天仍上市(.TW)/上櫃(.TWO)的 4 碼普通股(排除 0 開頭 ETF 與 6 碼權證/ETF)。"""
    out = {}
    try:
        r = requests.get("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL", timeout=30)
        r.raise_for_status()
        for row in r.json():
            c = str(row.get("Code", "")).strip()
            if re.fullmatch(r"[1-9]\d{3}", c):
                out[c] = ".TW"
    except Exception as e:
        print(f"  ⚠ TWSE 清單失敗: {e}")
    try:
        r = requests.get("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes",
                         timeout=30, verify=False)
        r.raise_for_status()
        for row in r.json():
            c = str(row.get("SecuritiesCompanyCode", "")).strip()
            if re.fullmatch(r"[1-9]\d{3}", c):
                out.setdefault(c, ".TWO")
    except Exception as e:
        print(f"  ⚠ TPEx 清單失敗: {e}")
    return out


def download(code_suffix, start, end, chunk=100):
    """yfinance 批次下載。回傳 ({code: df[Open High Low Close Volume Turnover]}, 失敗清單)。
    OHLC 以 Adj Close/Close 比例還原(與 Phase 1 auto_adjust 同口徑);
    Turnover = 未還原收盤 × 成交量(成交值,排名用)。"""
    import yfinance as yf
    data, failed = {}, []
    items = sorted(code_suffix.items())
    for i in range(0, len(items), chunk):
        part = items[i:i + chunk]
        syms = [c + s for c, s in part]
        try:
            raw = yf.download(syms, start=start, end=end, auto_adjust=False, group_by="ticker",
                              threads=True, progress=False)
        except Exception as e:
            print(f"  ⚠ 批次 {i}: {e}")
            failed += [c for c, _ in part]
            continue
        for c, s in part:
            try:
                h = raw[c + s] if isinstance(raw.columns, pd.MultiIndex) else raw
                h = h.dropna(subset=["Open", "High", "Low", "Close", "Adj Close"])
                if len(h) < 250:
                    failed.append(c)
                    continue
                f = h["Adj Close"] / h["Close"]
                df = pd.DataFrame({"Open": h["Open"] * f, "High": h["High"] * f, "Low": h["Low"] * f,
                                   "Close": h["Adj Close"], "Volume": h["Volume"].fillna(0),
                                   "Turnover": h["Close"] * h["Volume"].fillna(0)})
                df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
                data[c] = df
            except Exception:
                failed.append(c)
        print(f"  已下載 {min(i + chunk, len(items))}/{len(items)},可用 {len(data)}")
        time.sleep(1.5)
    return data, failed


# ============================================================
# 股票池(只用當時資料)
# ============================================================
def universe_membership(turnover_panel, top_n, window=TURNOVER_WINDOW):
    """turnover_panel: index=日期, columns=代號。每日以「前 window 日(不含當日)平均成交值」
    排名,前 top_n 為 True。資料不足 window 日者不入池。"""
    avg = turnover_panel.rolling(window, min_periods=window).mean().shift(1)
    rank = avg.rank(axis=1, ascending=False, method="first")
    return (rank <= top_n) & avg.notna()


def forward_panel(data, cal, h):
    """訊號日 t 的固定 h 日報酬 = C[t+h] / O[t+1] - 1(與引擎 fwd{h} 同定義)。"""
    cols = {}
    for c, df in data.items():
        d = df.reindex(cal)
        cols[c] = d["Close"].shift(-h) / d["Open"].shift(-1) - 1
    return pd.DataFrame(cols, index=cal)


# ============================================================
# 統計
# ============================================================
def boot_ci(values, dates, reps=BOOT, seed=42):
    """以日期分群重抽的平均數 95% CI。"""
    s = pd.DataFrame({"v": values, "d": dates}).dropna()
    if s.empty:
        return [None, None]
    g = s.groupby("d")["v"].agg(["sum", "count"])
    sums, cnts = g["sum"].values, g["count"].values
    rng = np.random.default_rng(seed)
    k = len(g)
    means = []
    for _ in range(reps):
        idx = rng.integers(0, k, k)
        means.append(sums[idx].sum() / cnts[idx].sum())
    return [float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))]


def describe(tr, label):
    if tr is None or len(tr) == 0:
        return {"label": label, "n": 0, "note": "無交易"}
    d = tr["signal_date"].astype(str)
    out = {
        "label": label, "n": int(len(tr)), "n_dates": int(d.nunique()),
        "small_sample": bool(len(tr) < MIN_N),
        "net_mean": float(tr["net"].mean()), "net_median": float(tr["net"].median()),
        "net_win": float((tr["net"] > 0).mean()), "net_ci95": boot_ci(tr["net"], d),
        "hold_mean": float(tr["hold_days"].mean()), "worst_net": float(tr["net"].min()),
        "reasons": tr["reason"].value_counts().to_dict(),
        "entry_day_target_gap": int(((tr["reason"] == "target_gap") & (tr["hold_days"] == 1)).sum()),
    }
    for h in HORIZONS:
        for kind in ("own", "day"):
            col = f"ex_{kind}{h}"
            if col in tr:
                out[f"{col}_mean"] = float(tr[col].mean())
                out[f"{col}_ci95"] = boot_ci(tr[col], d)
        out[f"fwd{h}_mean"] = float(tr[f"fwd{h}"].mean())
    return out


def breakdown(tr, label):
    res = {"all": describe(tr, label)}
    if tr is None or len(tr) == 0:
        return res
    res["market_stress"] = {str(k): describe(g, f"{label}|stress={k}") for k, g in tr.groupby("market_stress")}
    bins = pd.cut(tr["dd_trough"], [0.12, 0.18, 0.24, 0.30], include_lowest=True)
    res["drawdown"] = {str(k): describe(g, f"{label}|dd={k}") for k, g in tr.groupby(bins, observed=True)}
    return res


def attach_excess(tr, code, fwd, member):
    """每筆加上兩種超額報酬:ex_own{h} 減同檔池內日子的平均;ex_day{h} 減訊號日池內平均。"""
    if tr is None or len(tr) == 0:
        return tr
    sd = pd.to_datetime(tr["signal_date"])
    for h in HORIZONS:
        f = fwd[h]
        m = member if member is not None else pd.DataFrame(True, index=f.index, columns=f.columns)
        own = f[code].where(m[code]).mean()
        day = f.where(m).mean(axis=1)
        tr[f"base_own{h}"] = own
        tr[f"ex_own{h}"] = tr[f"fwd{h}"] - own
        tr[f"base_day{h}"] = day.reindex(sd).values
        tr[f"ex_day{h}"] = tr[f"fwd{h}"] - tr[f"base_day{h}"]
    return tr


def run_set(data, bench, fwd, member, codes, confirm):
    out = []
    for c in codes:
        df = data[c]
        elig = member[c].reindex(df.index).fillna(False) if member is not None else None
        if elig is not None and not elig.any():
            continue
        tr, _ = run_backtest(df[["Open", "High", "Low", "Close", "Volume"]], bench,
                             eligible=elig, CONFIRM=confirm)
        if len(tr):
            tr.insert(0, "code", c)
            out.append(attach_excess(tr, c, fwd, member))
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()


# ============================================================
# 主流程
# ============================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2019-01-01")
    ap.add_argument("--top", default="100,200")
    ap.add_argument("--out", default="backtest_out")
    ap.add_argument("--limit", type=int, default=0, help="除錯用:只取前 N 檔")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    tops = [int(x) for x in a.top.split(",")]
    end = dt.datetime.now(TPE).date().isoformat()   # yfinance end 不含當日 → 截止於前一交易日

    print("[1] 股票清單(今天仍上市櫃的 4 碼普通股)")
    lists = market_lists()
    for c in PHASE1_TICKERS:
        lists.setdefault(c, ".TWO" if c in ("3081", "6488") else ".TW")
    if a.limit:
        keep = sorted(lists)[:a.limit]
        lists = {c: lists[c] for c in set(keep) | set(PHASE1_TICKERS)}
    print(f"  上市 {sum(v == '.TW' for v in lists.values())} / 上櫃 {sum(v == '.TWO' for v in lists.values())}")

    print("[2] 下載價量")
    data, failed = download(lists, a.start, end)
    bench = load_bench(a.start)
    if bench is not None:
        bench = bench[bench.index < pd.Timestamp(end)]
    cal = bench.index if bench is not None and len(bench) else \
        pd.DatetimeIndex(sorted(set().union(*[set(d.index) for d in data.values()])))
    print(f"  可用 {len(data)} 檔,失敗/不足250日 {len(failed)} 檔;交易日曆 {len(cal)} 日")

    turnover = pd.DataFrame({c: d["Turnover"] for c, d in data.items()}).reindex(cal)
    fwd = {h: forward_panel(data, cal, h) for h in HORIZONS}

    summary = {
        "generated_at": dt.datetime.now(TPE).strftime("%Y-%m-%d %H:%M:%S"),
        "params": {**DEFAULTS, "start": a.start, "end_exclusive": end, "turnover_window": TURNOVER_WINDOW,
                   "tops": tops, "bootstrap_reps": BOOT, "bootstrap_cluster": "signal_date"},
        "data": {"listed_now": len(lists), "usable": len(data), "failed_or_short": len(failed),
                 "failed_sample": sorted(failed)[:50], "calendar_days": len(cal),
                 "span": [str(cal[0].date()), str(cal[-1].date())] if len(cal) else None},
        "warnings": [
            "倖存者偏誤:股票清單取自今天仍上市櫃的普通股,已下市股票未納入 → 結果偏樂觀",
            "目標價以盤中觸價成交計算,偏樂觀;未模擬漲跌停鎖死無法出場",
            "單筆全額進場,未模擬分批;資料尾端未出場交易捨棄",
            "「收黑」定義為收盤低於前一日收盤(非收盤低於開盤)",
            "yfinance 資料有少數缺漏日與個別錯誤價格(Phase 1 data_check 已列出)",
        ],
        "results": {}, "tests_count": 0,
    }

    for N in tops:
        member = universe_membership(turnover, N)
        ever = [c for c in member.columns if member[c].any()]
        base = {h: float(fwd[h].where(member).stack().mean()) for h in HORIZONS}
        summary["results"][f"top{N}"] = {"universe_ever": len(ever),
                                         "universe_baseline_fwd": base}
        for confirm in (True, False):
            tag = "confirm" if confirm else "noconfirm"
            print(f"[3] top{N} {tag}:{len(ever)} 檔曾入池")
            tr = run_set(data, bench, fwd, member, ever, confirm)
            if len(tr):
                tr.to_csv(os.path.join(a.out, f"phase2_trades_{tag}_top{N}.csv"),
                          index=False, encoding="utf-8-sig")
            summary["results"][f"top{N}"][tag] = breakdown(tr, f"top{N}|{tag}")

    print("[4] Phase 1 五檔對照(同一份資料、不限股票池)")
    p1_codes = [c for c in PHASE1_TICKERS if c in data]
    summary["results"]["phase1_5stocks"] = {}
    for confirm in (True, False):
        tag = "confirm" if confirm else "noconfirm"
        tr = run_set(data, bench, fwd, None, p1_codes, confirm)
        summary["results"]["phase1_5stocks"][tag] = breakdown(tr, f"phase1|{tag}")

    # 檢定格數(多重比較):每個 describe 的 CI 都算一次檢定
    def count_cells(o):
        if isinstance(o, dict):
            return (1 if "net_ci95" in o else 0) + sum(count_cells(v) for v in o.values())
        return 0
    summary["tests_count"] = count_cells(summary["results"])

    with open(os.path.join(a.out, "phase2_summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1, default=str)

    print("\n=== 摘要(net 扣成本;CI 為日期分群 bootstrap 95%)===")
    for key, grp in summary["results"].items():
        for tag in ("confirm", "noconfirm"):
            s = grp.get(tag, {}).get("all")
            if not s or not s.get("n"):
                continue
            flag = " ⚠樣本不足" if s["small_sample"] else ""
            print(f"  {key:15s} {tag:9s} n={s['n']:4d} 日期={s['n_dates']:4d} "
                  f"net={s['net_mean']*100:+.2f}% CI[{s['net_ci95'][0]*100:+.2f},{s['net_ci95'][1]*100:+.2f}] "
                  f"超額10日(同日池)={s.get('ex_day10_mean', float('nan'))*100:+.2f}% "
                  f"CI[{s['ex_day10_ci95'][0]*100:+.2f},{s['ex_day10_ci95'][1]*100:+.2f}]{flag}")
    print(f"  檢定格數 {summary['tests_count']}")
    print("⚠ " + " | ".join(summary["warnings"][:2]))


if __name__ == "__main__":
    main()
