# -*- coding: utf-8 -*-
"""
============================================================
pullback_data_check.py — Pullback 回測 Phase 1 資料驗證(2026-10-13)
============================================================
回測前先確認 yfinance 資料可信,不改動 pullback_backtest.py 的引擎:
  1. 後綴:每檔 .TW / .TWO 都試,確認只有一邊有資料、且與預期市場一致
  2. 範圍:還原價(回測實際用的)的起訖日與筆數
  3. 對帳:yfinance「未還原」收盤價/成交量 vs FinMind TaiwanStockPrice(交易所資料)
     全期間逐日比對 → 缺漏日期、收盤價不一致比例、成交量比值(確認單位是「股」)
     另列 3 個抽查日期的實際數字
  4. 加權指數 ^TWII 範圍與筆數
任何來源失敗都如實記錄,不補值。輸出 backtest_out/data_check.json。

用法:python pullback_data_check.py --tickers 3081,3665,2454,3026,6488 --start 2019-01-01
============================================================
"""
import os
import json
import time
import argparse
import datetime as dt

import numpy as np
import pandas as pd
import requests

from pullback_backtest import load_prices, load_bench

# 任務說明指定的預期市場(上櫃 .TWO / 上市 .TW);其他代號不檢查預期值,只檢查唯一性
EXPECTED_SUFFIX = {"3081": ".TWO", "6488": ".TWO", "3665": ".TW", "2454": ".TW", "3026": ".TW"}
FINMIND = "https://api.finmindtrade.com/api/v4/data"
CLOSE_TOL = 0.005      # 收盤價相差 0.5% 以內視為一致


def yf_raw(code, suffix, start):
    import yfinance as yf
    h = yf.Ticker(code + suffix).history(start=start, auto_adjust=False)
    if h is None or not len(h):
        return None
    h.index = pd.to_datetime(h.index).tz_localize(None).normalize()
    return h[["Open", "High", "Low", "Close", "Volume"]]


def finmind_prices(code, start):
    r = requests.get(FINMIND, params={"dataset": "TaiwanStockPrice", "data_id": code,
                                      "start_date": start,
                                      "end_date": dt.date.today().isoformat()}, timeout=60)
    r.raise_for_status()
    j = r.json()
    rows = j.get("data") or []
    if not rows:
        raise RuntimeError(f"FinMind 無資料: {j.get('msg')}")
    df = pd.DataFrame(rows)
    df.index = pd.to_datetime(df["date"])
    return df


def check_one(code, start):
    out = {"code": code}
    # 1. 後綴唯一性(兩邊都試)
    lens = {}
    for suf in (".TW", ".TWO"):
        try:
            raw = yf_raw(code, suf, start)
            lens[suf] = 0 if raw is None else len(raw)
        except Exception as e:
            lens[suf] = f"error: {str(e)[:80]}"
    out["suffix_rows"] = lens
    # 2. 回測實際使用的還原價(沿用引擎的 load_prices,同一套後綴判斷)
    adj, suf = load_prices(code, start)
    if adj is None:
        out["error"] = "yfinance 無資料或少於250日"
        return out
    out["suffix_used"] = suf
    exp = EXPECTED_SUFFIX.get(code)
    out["suffix_expected"] = exp
    out["suffix_ok"] = (exp is None or exp == suf) and \
        sum(1 for v in lens.values() if isinstance(v, int) and v > 250) == 1
    out["adjusted_span"] = [str(adj.index[0].date()), str(adj.index[-1].date())]
    out["adjusted_rows"] = int(len(adj))
    out["zero_volume_days"] = int((adj["Volume"] == 0).sum())

    # 3. 未還原價 vs FinMind
    raw = yf_raw(code, suf, start)
    try:
        fm = finmind_prices(code, start)
    except Exception as e:
        out["finmind_error"] = str(e)[:160]
        return out
    yf_dates, fm_dates = set(raw.index), set(fm.index)
    out["missing_in_yfinance"] = sorted(str(d.date()) for d in fm_dates - yf_dates)[:20]
    out["missing_in_yfinance_n"] = len(fm_dates - yf_dates)
    out["extra_in_yfinance_n"] = len(yf_dates - fm_dates)
    out["extra_in_yfinance"] = sorted(str(d.date()) for d in yf_dates - fm_dates)[:20]
    common = sorted(yf_dates & fm_dates)
    yc = raw.loc[common, "Close"].astype(float)
    fc = fm.loc[common, "close"].astype(float)
    rel = (yc - fc).abs() / fc
    out["common_days"] = len(common)
    out["close_mismatch_n"] = int((rel > CLOSE_TOL).sum())
    out["close_mismatch_pct"] = round(float((rel > CLOSE_TOL).mean()) * 100, 2)
    out["close_mismatch_examples"] = [
        {"date": str(d.date()), "yfinance": round(float(yc[d]), 2), "finmind": round(float(fc[d]), 2)}
        for d in rel[rel > CLOSE_TOL].index[:5]]
    yv = raw.loc[common, "Volume"].astype(float)
    fv = fm.loc[common, "Trading_Volume"].astype(float)
    ratio = (yv / fv).replace([np.inf, -np.inf], np.nan).dropna()
    out["volume_ratio_median"] = round(float(ratio.median()), 4) if len(ratio) else None
    out["volume_unit"] = ("股" if out["volume_ratio_median"] and 0.8 <= out["volume_ratio_median"] <= 1.2
                          else "張(÷1000)" if out["volume_ratio_median"] and out["volume_ratio_median"] < 0.01
                          else "不明,需人工檢查")
    # 抽查:期間 1/4、1/2、最後一日
    picks = [common[len(common) // 4], common[len(common) // 2], common[-1]] if common else []
    out["spot_checks"] = [{
        "date": str(d.date()),
        "yf_close": round(float(yc[d]), 2), "finmind_close": round(float(fc[d]), 2),
        "yf_volume": int(yv[d]), "finmind_volume": int(fv[d]),
        "yf_adjusted_close": round(float(adj["Close"].get(d, np.nan)), 2),
    } for d in picks]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", default="3081,3665,2454,3026,6488")
    ap.add_argument("--start", default="2019-01-01")
    ap.add_argument("--out", default="backtest_out")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    res = {"generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
           "start": a.start, "close_tolerance": CLOSE_TOL, "stocks": []}
    bench = load_bench(a.start)
    res["bench_TWII"] = ({"rows": int(len(bench)),
                          "span": [str(bench.index[0].date()), str(bench.index[-1].date())]}
                         if bench is not None and len(bench) else {"error": "下載失敗"})
    print(f"加權指數: {res['bench_TWII']}")

    for code in [c.strip() for c in a.tickers.split(",") if c.strip()]:
        try:
            r = check_one(code, a.start)
        except Exception as e:
            r = {"code": code, "error": str(e)[:200]}
        res["stocks"].append(r)
        print(f"\n■ {code}")
        for k in ("suffix_rows", "suffix_used", "suffix_expected", "suffix_ok", "adjusted_span",
                  "adjusted_rows", "zero_volume_days", "common_days", "missing_in_yfinance_n",
                  "extra_in_yfinance_n", "close_mismatch_n", "close_mismatch_pct",
                  "volume_ratio_median", "volume_unit", "error", "finmind_error"):
            if k in r:
                print(f"  {k}: {r[k]}")
        for s in r.get("spot_checks", []):
            print(f"  抽查 {s}")
        time.sleep(2)   # FinMind 免 token 有頻率限制

    with open(os.path.join(a.out, "data_check.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)
    bad = [s["code"] for s in res["stocks"]
           if s.get("error") or s.get("finmind_error") or not s.get("suffix_ok")
           or s.get("close_mismatch_pct", 100) > 1 or s.get("volume_unit") != "股"]
    print("\n資料驗證:" + ("全部通過" if not bad else f"需人工檢查 → {bad}"))


if __name__ == "__main__":
    main()
