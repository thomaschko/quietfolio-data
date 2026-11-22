# -*- coding: utf-8 -*-
"""
============================================================
disloc_backtest.py — 錯殺回檔 路徑 A / B 回測(預先登記:docs/prereg_dislocation_path_b.md)
============================================================
v2026-11-22-disloc-bt

問題:路徑 B(淺回檔強動能)收錄的股票,收錄後是否比同日股票池平均、同日隨機抽樣、
或同日路徑 A 收錄的股票表現更好或更差。

條件:逐行移植 GAS Dislocation.js(2026-11-15 版)runDislocation / evaluateDislocation /
analyzeRevenue / analyzeGrossMargin,以及 QualityGrowth.js fetchCapexYoYHistory_。
參數(DISLOC)照抄,不調整。

時點(避免前視偏差):
  - 訊號日 t = 每月第一個交易日,用 t 收盤(含)以前的資料;隔日開盤進場、持有 20 個交易日收盤出場。
  - 月營收:法定期限每月 10 日 → t 在 11 日(含)以後才可用上個月,否則只能用前兩個月。
  - 季財報 / 現金流量:法定申報期限後才可用(Q1 5/15、Q2 8/14、Q3 11/14、Q4 隔年 3/31)。
  - 「階段1」的 MOPS 當月 / 當季快照,以當時最新可得的月份 / 季別重建(資料:bt_disloc,FinMind 歷史)。
  - FinMind 抓取期間照 GAS:股價 400 日曆天、月營收 500 天、財報 900 天、現金流量 950 天。
  - 價格:yfinance 還原價(與急跌反轉回測同一套下載);成交量張數 = Volume / 1000。
股票池(超額報酬基準):前 60 個交易日(不含當日)平均成交值前 TOP 名(與急跌反轉 Phase 3 同定義,TOP=200)。
報酬:C[t+20] / O[t+1] − 1,扣來回成本 0.585%。
超額:(a) − 同日股票池平均(同口徑、同成本);(b) − 同日池內隨機抽與該路徑同檔數(排除已收錄者)、持有相同天數。
統計:以訊號日期分群 bootstrap 2,000 次、95% 信賴區間。2019–2023 樣本內、2024 起樣本外。
判定(登記):路徑 B 樣本外超額 (b) CI 下限 > 0 → 保留;上限 < 0 → 建議移除;含 0 → 標「未經驗證」。
已知限制:只用今天仍上市的股票(倖存者偏差);階段1 季報快照以法定期限近似(實際在申報期間會缺部分公司)。

用法:python disloc_backtest.py [--start 2019-01-01] [--top 200] [--max-codes N(測試用)]
輸出:backtest_out/disloc/{summary.json, REPORT.md, trades.csv}
============================================================
"""
import argparse
import csv
import datetime as dt
import gzip
import json
import os

import numpy as np
import pandas as pd

VERSION = "v2026-11-22-disloc-bt"
OUT_DIR = os.path.join("backtest_out", "disloc")
BT_DIR = "bt_disloc"

# ── Dislocation.js 2026-11-15 版 DISLOC(照抄,不調整)──
DISLOC = dict(
    DRAWDOWN_MIN=20, DRAWDOWN_MAX=50, TREND_MA=120, MUST_ABOVE_TREND=True, TREND_TOLERANCE=-1,
    MOMENTUM_MIN_TREND_GAP=10, MOMENTUM_MIN_RETURN_10D=5, REQUIRE_MOMENTUM=True,
    MIN_REV_YOY=0, MIN_CUM_REV_YOY=0, MIN_GROSS_MARGIN=0, MIN_GROSS_MARGIN_LEVEL=15,
    MIN_GROW_STREAK=3, REQUIRE_SUSTAINED_OR_HIGH=True,
    REV_HIGH_MONTHS=12, GM_HIGH_QUARTERS=4, MIN_GM_GROWTH_RATE=0,
    CAPEX_QUARTERS_REQUIRED=2, CAPEX_MIN_YOY=0,
    MIN_AVG_VOL_5D=500, MIN_AVG_TURNOVER_5D=25000000, MAX_CANDIDATES=120,
)
LOOKBACK_PRICE, LOOKBACK_REV, LOOKBACK_FIN, LOOKBACK_CF = 400, 500, 900, 950   # 日曆天(GAS isoDaysAgo)

# ── 預先登記的回測設定 ──
HOLD = 20
COST_RT = 0.00585
TOP = 200
IS_END = pd.Timestamp("2023-12-31")
OOS_START = pd.Timestamp("2024-01-01")
BOOT = 2000
SEED = 20261115


# ============================================================
# 時點:資料何時可用
# ============================================================
def latest_rev_month(t):
    """t 時點可用的最新月營收(year, month):每月 10 日法定期限 → 11 日起才有上個月。"""
    y, m = t.year, t.month
    back = 1 if t.day >= 11 else 2
    m -= back
    while m <= 0:
        m += 12
        y -= 1
    return y, m


def quarter_available(qdate, t):
    """季底日 qdate('YYYY-MM-DD')的財報在 t 是否已過法定申報期限。"""
    y, mm = int(qdate[:4]), qdate[5:7]
    dl = {"03": dt.date(y, 5, 15), "06": dt.date(y, 8, 14), "09": dt.date(y, 11, 14),
          "12": dt.date(y + 1, 3, 31)}.get(mm)
    return dl is not None and t.date() > dl


def r2(x):
    return None if x is None else round(x, 2)


# ============================================================
# 基本面(移植 analyzeRevenue / analyzeGrossMargin / fetchCapexYoYHistory_)
# ============================================================
def rev_rows_at(rows, t, window_days=None):
    """rows: [(year, month, revenue)] 由舊到新。回傳 t 可用的列;window_days 模擬 FinMind start_date(資料日 = 次月 1 日)。"""
    ly, lm = latest_rev_month(t)
    out = []
    for y, m, v in rows:
        if (y, m) > (ly, lm):
            continue
        if window_days is not None:
            ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
            if dt.date(ny, nm, 1) < (t - pd.Timedelta(days=window_days)).date():
                continue
        out.append((y, m, v))
    return out


def mops_rev_snapshot(rows, t):
    """階段1 MOPS 月營收快照:最新可得月份的 當月YoY、累計YoY(缺資料 → None)。"""
    ly, lm = latest_rev_month(t)
    d = {(y, m): v for y, m, v in rows}
    cur, prev = d.get((ly, lm)), d.get((ly - 1, lm))
    rev_yoy = r2((cur - prev) / prev * 100) if cur is not None and prev and prev > 0 else None
    c1 = [d.get((ly, k)) for k in range(1, lm + 1)]
    c0 = [d.get((ly - 1, k)) for k in range(1, lm + 1)]
    cum_yoy = None
    if None not in c1 and None not in c0 and sum(c0) > 0:
        cum_yoy = r2((sum(c1) - sum(c0)) / sum(c0) * 100)
    return rev_yoy, cum_yoy


def analyze_revenue(rows):
    """移植 analyzeRevenue(sid);rows 已按 t 可用與 500 天窗口過濾。"""
    n = len(rows)
    if n < 4:
        return None
    rev = [v for _, _, v in rows]

    def sum3(e):
        if e - 2 < 0:
            return None
        s = rev[e - 2:e + 1]
        return None if any(v is None for v in s) else sum(s)

    rev3m = None
    if n >= 15:
        c3, p3 = sum3(n - 1), sum3(n - 13)
        if c3 is not None and p3 is not None and p3 > 0:
            rev3m = r2((c3 - p3) / p3 * 100)
    rev1m = None
    if n >= 13:
        c1, p1 = rev[n - 1], rev[n - 13]
        if c1 is not None and p1 is not None and p1 > 0:
            rev1m = r2((c1 - p1) / p1 * 100)
    is_high, months = None, 0
    if n >= DISLOC["REV_HIGH_MONTHS"]:
        recent = rev[-DISLOC["REV_HIGH_MONTHS"]:]
        months = len(recent)
        cur = recent[-1]
        if cur is not None:
            prev = [v for v in recent[:-1] if v is not None]
            is_high = cur > (max(prev) if prev else -np.inf)
    streak = 0
    gi = n - 1
    while gi - 12 >= 0:
        c, p = rev[gi], rev[gi - 12]
        if c is None or p is None or p <= 0:
            break
        if c > p:
            streak += 1
        else:
            break
        gi -= 1
    return dict(rev3mYoY=rev3m, rev1mYoY=rev1m, isHigh=is_high, months=months, growStreak=streak)


def analyze_gross_margin(rows):
    """移植 analyzeGrossMargin(sid);rows: [(date, revenue, gross)] 已按 t 可用與 900 天窗口過濾。"""
    g = [(d, gp / rv * 100) for d, rv, gp in rows if rv is not None and gp is not None and rv > 0]
    if len(g) < 2:
        return None
    cur = g[-1][1]
    gm_yoy = r2(cur - g[-5][1]) if len(g) >= 5 else None
    return dict(gm=r2(cur), gmYoY=gm_yoy)


def quarter_of(d):
    return {"03": 1, "06": 2, "09": 3, "12": 4}.get(d[5:7])


def capex_yoy_history(rows, need):
    """移植 fetchCapexYoYHistory_;rows: [(date, capex_ytd)] 已按 t 可用與 950 天窗口過濾。"""
    if len(rows) < need + 5:
        return None
    disc = []
    for i, (d, c) in enumerate(rows):
        q = quarter_of(d)
        if c is None or q is None:
            disc.append((d, None))
            continue
        if q == 1:
            disc.append((d, c))
            continue
        prev = None
        for j in range(i - 1, -1, -1):
            if rows[j][0][:4] != d[:4]:
                break
            if quarter_of(rows[j][0]) == q - 1:
                prev = rows[j][1]
                break
        disc.append((d, None if prev is None else c - prev))
    n, out = len(disc), []
    for back in range(need):
        ci, pi = n - 1 - back, n - 1 - back - 4
        if pi < 0:
            return None
        c, p = disc[ci][1], disc[pi][1]
        if c is None or p is None or p == 0:
            return None
        out.append((abs(c) - abs(p)) / abs(p) * 100)
    return list(reversed(out))


# ============================================================
# 技術面(移植 evaluateDislocation)
# ============================================================
def evaluate_dislocation(close, vol_lots):
    """close: 還原收盤(由舊到新,含訊號日);vol_lots: 張數。回傳 dict(pass, why, path, ...)。"""
    n = len(close)
    c = np.asarray(close, float)
    v = np.asarray(vol_lots, float)
    avg_vol5 = v[-5:].mean()
    avg_to5 = (c[-5:] * v[-5:] * 1000).mean()
    if avg_vol5 < DISLOC["MIN_AVG_VOL_5D"] and avg_to5 < DISLOC["MIN_AVG_TURNOVER_5D"]:
        return dict(ok=False, why="volLow")
    win = c[-250:]
    year_high = win.max()
    if not year_high > 0:
        return dict(ok=False, why="dataShort")
    cur = c[-1]
    dd = (year_high - cur) / year_high * 100
    tp = min(DISLOC["TREND_MA"], n)
    ma_t = c[-tp:].mean()
    trend_gap = (cur - ma_t) / ma_t * 100
    if dd > DISLOC["DRAWDOWN_MAX"]:
        return dict(ok=False, why="ddOut", drawdown=dd)
    ret10 = (c[-1] - c[-11]) / c[-11] * 100 if n >= 11 and c[-11] > 0 else None
    held = (not DISLOC["MUST_ABOVE_TREND"]) or trend_gap >= DISLOC["TREND_TOLERANCE"]
    if dd >= DISLOC["DRAWDOWN_MIN"]:
        if not held:
            return dict(ok=False, why="belowTrend", drawdown=dd)
        path = "A"
    else:
        if not DISLOC["REQUIRE_MOMENTUM"]:
            return dict(ok=False, why="ddOut", drawdown=dd)
        if trend_gap < DISLOC["MOMENTUM_MIN_TREND_GAP"] or ret10 is None or ret10 < DISLOC["MOMENTUM_MIN_RETURN_10D"]:
            return dict(ok=False, why="momentumWeak", drawdown=dd)
        path = "B"
    return dict(ok=True, path=path, drawdown=round(dd, 2), trendGap=round(trend_gap, 2),
                return10d=None if ret10 is None else round(ret10, 2))


# ============================================================
# 一個訊號日的完整流程(移植 runDislocation)
# ============================================================
def screen_day(t, codes, fund, px, reasons=None):
    """回傳 [(code, path, info)]。fund: {code: {rev, fin, cf}};px: {code: DataFrame(Close, Volume, RawClose)}"""
    reasons = reasons if reasons is not None else {}

    def rej(k):
        reasons[k] = reasons.get(k, 0) + 1

    # 階段1:MOPS 月營收 / 季報快照粗篩
    stage1 = []
    for c in codes:
        f = fund.get(c)
        if not f:
            continue
        rev_yoy, cum_yoy = mops_rev_snapshot(f["rev"], t)
        fins = [r for r in f["fin"] if quarter_available(r[0], t)]
        if not fins or fins[-1][1] is None or fins[-1][2] is None or not fins[-1][1]:
            continue                                                   # 無季報資料
        gm = fins[-1][2] / fins[-1][1] * 100
        if not (rev_yoy is not None and rev_yoy > DISLOC["MIN_REV_YOY"]):
            continue
        if not (cum_yoy is not None and cum_yoy > DISLOC["MIN_CUM_REV_YOY"]):
            continue
        if not (gm > DISLOC["MIN_GROSS_MARGIN"]):
            continue
        stage1.append((c, rev_yoy, cum_yoy, gm))
    # 階段3.5:當日量值粗篩(張數或成交值任一達標;無當日報價 → 保留)→ 基本面強度排序 → 前 120
    liquid = []
    for c, ry, cy, gm in stage1:
        df = px.get(c)
        if df is not None and t in df.index:
            row = df.loc[t]
            lots = row["Volume"] / 1000
            if not (lots >= DISLOC["MIN_AVG_VOL_5D"] or row["RawClose"] * row["Volume"] >= DISLOC["MIN_AVG_TURNOVER_5D"]):
                continue
        liquid.append((c, (ry or 0) + (cy or 0) + (gm or 0)))
    liquid.sort(key=lambda x: -x[1])
    cands = [c for c, _ in liquid[:DISLOC["MAX_CANDIDATES"]]]
    reasons["stage1"] = reasons.get("stage1", 0) + len(stage1)
    reasons["candidates"] = reasons.get("candidates", 0) + len(cands)

    picks = []
    for c in cands:
        df = px.get(c)
        if df is None:
            rej("dataShort")
            continue
        w = df.loc[(df.index > t - pd.Timedelta(days=LOOKBACK_PRICE)) & (df.index <= t)]
        if len(w) < DISLOC["TREND_MA"] + 10:
            rej("dataShort")
            continue
        tech = evaluate_dislocation(w["Close"].values, (w["Volume"] / 1000).values)
        if not tech["ok"]:
            rej(tech["why"])
            continue
        f = fund[c]
        ra = analyze_revenue(rev_rows_at(f["rev"], t, LOOKBACK_REV))
        if not ra:
            rej("revNoData")
            continue
        if ra["rev3mYoY"] is not None and ra["rev3mYoY"] <= DISLOC["MIN_REV_YOY"]:
            rej("revDecline")
            continue
        if DISLOC["REQUIRE_SUSTAINED_OR_HIGH"] and ra["months"] > 0:
            if not (ra["growStreak"] >= DISLOC["MIN_GROW_STREAK"] or ra["isHigh"] is True):
                rej("revNotSustained")
                continue
        t0 = (t - pd.Timedelta(days=LOOKBACK_FIN)).strftime("%Y-%m-%d")
        ga = analyze_gross_margin([r for r in f["fin"] if r[0] >= t0 and quarter_available(r[0], t)])
        if not ga:
            rej("gmNoData")
            continue
        if ga["gm"] is not None and ga["gm"] < DISLOC["MIN_GROSS_MARGIN_LEVEL"]:
            rej("gmTooLow")
            continue
        if ga["gm"] is not None and ga["gmYoY"] is not None:
            prior = ga["gm"] - ga["gmYoY"]
            if prior > 0 and not (r2(ga["gmYoY"] / prior * 100) > DISLOC["MIN_GM_GROWTH_RATE"]):
                rej("gmNotGrowing")
                continue
        t1 = (t - pd.Timedelta(days=LOOKBACK_CF)).strftime("%Y-%m-%d")
        cx = capex_yoy_history([r for r in f["cf"] if r[0] >= t1 and quarter_available(r[0], t)],
                               DISLOC["CAPEX_QUARTERS_REQUIRED"])
        if cx is None:
            rej("capexDataShort")
            continue
        if not all(y > DISLOC["CAPEX_MIN_YOY"] for y in cx):
            rej("capexNotExpanding")
            continue
        rej("pass_" + tech["path"])
        picks.append((c, tech["path"], tech))
    return picks


# ============================================================
# 資料載入
# ============================================================
def _f(x):
    try:
        return float(x) if x not in ("", None) else None
    except ValueError:
        return None


def load_fund(base=BT_DIR):
    def rd(name):
        with gzip.open(os.path.join(base, name + ".csv.gz"), "rt", encoding="utf-8") as fh:
            r = csv.reader(fh)
            next(r)
            return list(r)
    fund = {}
    for c, y, m, v in rd("rev"):
        fund.setdefault(c, {"rev": [], "fin": [], "cf": []})["rev"].append((int(y), int(m), _f(v)))
    for c, d, rv, gp in rd("fin"):
        fund.setdefault(c, {"rev": [], "fin": [], "cf": []})["fin"].append((d, _f(rv), _f(gp)))
    for c, d, cx in rd("cf"):
        fund.setdefault(c, {"rev": [], "fin": [], "cf": []})["cf"].append((d, _f(cx)))
    for f in fund.values():
        f["rev"].sort()
        f["fin"].sort()
        f["cf"].sort()
    uni = {r[0]: r[2] for r in rd("universe")}
    return fund, uni


def first_trading_days(cal, start, end):
    s = pd.Series(cal, index=cal)
    s = s[(s >= start) & (s <= end)]
    return list(s.groupby([s.dt.year, s.dt.month]).min())


# ============================================================
# 統計
# ============================================================
def boot_ci(values, dates, reps=BOOT, seed=42):
    s = pd.DataFrame({"v": values, "d": dates}).dropna()
    if s.empty:
        return [None, None]
    g = s.groupby("d")["v"].agg(["sum", "count"])
    sums, cnts = g["sum"].values, g["count"].values
    rng = np.random.default_rng(seed)
    k = len(g)
    means = [sums[i].sum() / cnts[i].sum() for i in (rng.integers(0, k, k) for _ in range(reps))]
    return [round(float(np.percentile(means, 2.5)), 5), round(float(np.percentile(means, 97.5)), 5)]


def stats(df, col):
    d = df.dropna(subset=[col])
    if d.empty:
        return dict(n=0, dates=0, mean=None, ci=[None, None], date_eq=None)
    return dict(n=int(len(d)), dates=int(d["signal_date"].nunique()), mean=round(float(d[col].mean()), 5),
                ci=boot_ci(d[col].values, d["signal_date"].values),
                date_eq=round(float(d.groupby("signal_date")[col].mean().mean()), 5))


def verdict(ci):
    lo, hi = ci
    if lo is None:
        return "樣本不足"
    if lo > 0:
        return "保留"
    if hi < 0:
        return "建議移除"
    return "未經驗證(信賴區間含 0)"


# ============================================================
# 主程式
# ============================================================
def run(signal_days, codes, fund, px, member, fwd_net, rng):
    """回傳 trades DataFrame 與淘汰原因統計。"""
    rows, reasons = [], {}
    cal = fwd_net.index
    for t in signal_days:
        if t not in cal:
            continue
        picks = screen_day(t, codes, fund, px, reasons)
        pool = [c for c in member.columns[member.loc[t].values]] if t in member.index else []
        f = fwd_net.loc[t]
        pool_vals = f.reindex(pool).dropna()
        pool_mean = float(pool_vals.mean()) if len(pool_vals) else np.nan
        for path in ("A", "B"):
            ps = [c for c, p, _ in picks if p == path]
            if not ps:
                continue
            cand = [c for c in pool_vals.index if c not in ps]
            k = min(len(ps), len(cand))
            rand_mean = float(f.reindex(rng.choice(cand, size=k, replace=False)).mean()) if k else np.nan
            for c, p, info in picks:
                if p != path:
                    continue
                r = f.get(c, np.nan)
                rows.append(dict(signal_date=t.strftime("%Y-%m-%d"), code=c, path=p, net=r,
                                 ex_pool=r - pool_mean if np.isfinite(r) else np.nan,
                                 ex_rand=r - rand_mean if np.isfinite(r) else np.nan,
                                 pool_n=len(pool_vals), drawdown=info["drawdown"], trend_gap=info["trendGap"],
                                 return10d=info["return10d"], in_pool=c in pool))
    return pd.DataFrame(rows), reasons


def summarize(tr):
    out = {}
    if tr.empty:
        return out
    tr = tr.copy()
    sd = pd.to_datetime(tr["signal_date"])
    for seg, mask in (("in_sample", sd <= IS_END), ("out_of_sample", sd >= OOS_START)):
        s = tr[mask]
        seg_out = {}
        for path in ("A", "B"):
            p = s[s["path"] == path]
            seg_out[path] = dict(net=stats(p, "net"), ex_pool=stats(p, "ex_pool"), ex_rand=stats(p, "ex_rand"))
        # 同日 B − A(兩條路徑同一天都有收錄的日子)
        a = s[s["path"] == "A"].groupby("signal_date")["net"].mean()
        b = s[s["path"] == "B"].groupby("signal_date")["net"].mean()
        both = pd.concat([a, b], axis=1, keys=["A", "B"]).dropna()
        diff = (both["B"] - both["A"])
        seg_out["B_minus_A_same_day"] = dict(dates=int(len(diff)),
                                             mean=None if diff.empty else round(float(diff.mean()), 5),
                                             ci=boot_ci(diff.values, diff.index.values) if len(diff) else [None, None])
        out[seg] = seg_out
    oos_b = out.get("out_of_sample", {}).get("B", {}).get("ex_rand", {})
    out["verdict_path_B"] = verdict(oos_b.get("ci", [None, None])) if oos_b.get("dates", 0) else "樣本不足"
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2019-01-01")
    ap.add_argument("--top", type=int, default=TOP)
    ap.add_argument("--max-codes", type=int, default=0, help="測試用:只跑前 N 檔")
    ap.add_argument("--smoke", action="store_true", help="冒煙測試:只跑最後 3 個訊號日,只印淘汰原因與收錄檔數,不算報酬(避免先偷看樣本外)")
    a = ap.parse_args()
    from pullback_phase2 import market_lists, download, universe_membership   # 與急跌反轉回測同一套下載 / 股票池

    fund, uni = load_fund()
    suffix = market_lists()
    codes = sorted(c for c in uni if c in suffix)
    if a.max_codes:
        codes = codes[:a.max_codes]
    print(f"{VERSION}:基本面 {len(fund)} 檔、宇宙 {len(uni)} 檔、今天仍上市且有後綴 {len(codes)} 檔")
    pstart = (pd.Timestamp(a.start) - pd.Timedelta(days=LOOKBACK_PRICE + 120)).strftime("%Y-%m-%d")
    data, failed = download({c: suffix[c] for c in codes}, pstart, None)
    print(f"價格:可用 {len(data)} 檔、失敗 {len(failed)} 檔")
    cal = sorted(set().union(*[set(df.index) for df in data.values()]))
    cal = pd.DatetimeIndex(cal)
    px = {}
    for c, df in data.items():
        d = df.copy()
        d["RawClose"] = d["Turnover"] / d["Volume"].replace(0, np.nan)
        d["RawClose"] = d["RawClose"].fillna(d["Close"])
        d["Turnover"] = d["RawClose"] * d["Volume"]
        px[c] = d
    turnover = pd.DataFrame({c: d["Turnover"] for c, d in px.items()}).reindex(cal)
    member = universe_membership(turnover, a.top)
    opens = pd.DataFrame({c: d["Open"] for c, d in px.items()}).reindex(cal)
    closes = pd.DataFrame({c: d["Close"] for c, d in px.items()}).reindex(cal)
    fwd_net = closes.shift(-HOLD) / opens.shift(-1) - 1 - COST_RT
    last_full = cal[-HOLD - 1] if len(cal) > HOLD + 1 else cal[-1]
    days = [d for d in first_trading_days(cal, pd.Timestamp(a.start), last_full)]
    print(f"訊號日 {len(days)} 個:{days[0].date()} ~ {days[-1].date()}(持有 {HOLD} 日需完整)")
    if a.smoke:
        for t in days[-3:]:
            rs = {}
            picks = screen_day(t, codes, fund, px, rs)
            print(f"[smoke] {t.date()} 收錄 A {sum(p == 'A' for _, p, _ in picks)} / B {sum(p == 'B' for _, p, _ in picks)};",
                  json.dumps(rs, ensure_ascii=False), "池", int(member.loc[t].sum()))
        return
    rng = np.random.default_rng(SEED)
    tr, reasons = run(days, [c for c in codes if c in px or c in fund], fund, px, member, fwd_net, rng)
    summ = summarize(tr)
    os.makedirs(OUT_DIR, exist_ok=True)
    meta = dict(version=VERSION, generated_at=dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M"),
                prereg="docs/prereg_dislocation_path_b.md", params=DISLOC, hold=HOLD, cost_rt=COST_RT, top=a.top,
                in_sample_end=str(IS_END.date()), out_of_sample_start=str(OOS_START.date()), boot=BOOT, seed=SEED,
                signal_days=len(days), first_signal=str(days[0].date()), last_signal=str(days[-1].date()),
                codes=len(codes), price_ok=len(data), price_failed=len(failed), reject_reasons=reasons,
                trades=int(len(tr)), survivorship_bias="只用今天仍上市的股票")
    with open(os.path.join(OUT_DIR, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump(dict(meta=meta, results=summ), fh, ensure_ascii=False, indent=1)
    tr.to_csv(os.path.join(OUT_DIR, "trades.csv"), index=False)
    with open(os.path.join(OUT_DIR, "REPORT.md"), "w", encoding="utf-8") as fh:
        fh.write(report_md(meta, summ))
    print(json.dumps(summ, ensure_ascii=False, indent=1))
    print("淘汰原因:", json.dumps(reasons, ensure_ascii=False))


def pct(x):
    return "—" if x is None else f"{x * 100:+.2f}%"


def ci_s(ci):
    return "—" if ci[0] is None else f"[{ci[0] * 100:+.2f}%, {ci[1] * 100:+.2f}%]"


def report_md(meta, s):
    L = [f"# 錯殺回檔 路徑 A / B 回測報告({meta['version']})", "",
         f"- 預先登記:`{meta['prereg']}`;參數照 Dislocation.js 2026-11-15 版,未調整",
         f"- 訊號日:每月第一個交易日,{meta['first_signal']} ~ {meta['last_signal']},共 {meta['signal_days']} 個",
         f"- 持有 {meta['hold']} 個交易日(隔日開盤進、第 {meta['hold']} 日收盤出),扣來回成本 {meta['cost_rt'] * 100:.3f}%",
         f"- 股票池:前 60 日平均成交值前 {meta['top']} 名;價格 yfinance 還原價;{meta['survivorship_bias']}(倖存者偏差)",
         f"- 樣本內 ≤ {meta['in_sample_end']};樣本外 ≥ {meta['out_of_sample_start']};以訊號日分群 bootstrap {meta['boot']} 次",
         f"- 交易筆數 {meta['trades']};價格可用 {meta['price_ok']} 檔、失敗 {meta['price_failed']} 檔", ""]
    for seg, name in (("in_sample", "樣本內(2019–2023)"), ("out_of_sample", "樣本外(2024 起)")):
        x = s.get(seg)
        if not x:
            continue
        L += [f"## {name}", "", "| 路徑 | 筆數 | 訊號日 | 平均淨報酬 | (a) − 股票池平均 | (a) 95% CI | (b) − 同日隨機 | (b) 95% CI |",
              "|---|---|---|---|---|---|---|---|"]
        for p in ("A", "B"):
            r = x[p]
            L.append(f"| {p} | {r['net']['n']} | {r['net']['dates']} | {pct(r['net']['mean'])} | {pct(r['ex_pool']['mean'])} | "
                     f"{ci_s(r['ex_pool']['ci'])} | {pct(r['ex_rand']['mean'])} | {ci_s(r['ex_rand']['ci'])} |")
        d = x["B_minus_A_same_day"]
        L += ["", f"同日 B − A(兩路徑同一天都有收錄,{d['dates']} 天):{pct(d['mean'])},95% CI {ci_s(d['ci'])}", ""]
    L += [f"## 判定(登記規則:路徑 B 樣本外超額 (b) 的信賴區間)", "", f"**{s.get('verdict_path_B', '樣本不足')}**", "",
          "路徑 A 只回報,不因本回測改動。歷史回測,非投資建議。", ""]
    return "\n".join(L)


if __name__ == "__main__":
    main()
