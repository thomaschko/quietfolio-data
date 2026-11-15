# -*- coding: utf-8 -*-
"""
adj_factors.py — 還原價係數(Yahoo Finance,yfinance)(2026-11-15)
============================================================
FinMind 還原價資料集(TaiwanStockPriceAdj)在目前方案取不到 → 改由 Yahoo 算每檔的還原係數,
讓題材頁(Python)與 GAS(錯殺回檔、糾結突破、創新高 bootstrap)把 FinMind 未還原價乘上係數。
係數 = 還原收盤 ÷ 交易所原始收盤:
  Yahoo 的 Close 已扣除配股 / 分割(急跌反轉回測報告實測),Adj Close 再扣現金股利;
  交易所原始價(FinMind TaiwanStockPrice)兩者都沒扣 →
  factor(d) = (Adj Close(d) / Close(d)) ÷ Π(d 之後的 Stock Splits)
輸出 adj_factors.json:{"asof","source","stocks":{代號:[[起日, 係數], ...]}},係數只在除權息日改變;
  使用:找起日 ≤ 該日的最後一段;早於第一段用第一段;晚於最後一段用最後一段(通常是 1)。
股票池:bt_disloc/universe.csv.gz(上市 → .TW、上櫃 → .TWO)。
係數不合理(< 0.15 或 > 1.05)的個股不輸出(使用端當作未還原)。
"""
import csv
import datetime as dt
import gzip
import json
import os
import sys
import time

OUT = "adj_factors.json"
UNIVERSE = "bt_disloc/universe.csv.gz"
YEARS = 3
CHUNK = 100


def load_universe(path=UNIVERSE):
    with gzip.open(path, "rt", encoding="utf-8") as f:
        rd = csv.reader(f)
        next(rd, None)
        return {r[0]: (".TW" if r[2] == "twse" else ".TWO") for r in rd}


def segments(dates, adj, close, splits):
    """dates 依時間排序;splits:當天的分割 / 配股比例(0 或 NaN = 沒有)→ [[YYYY-MM-DD, factor], ...]"""
    n = len(dates)
    after = [1.0] * n                       # d 之後(不含 d)的分割連乘
    prod = 1.0
    for i in range(n - 1, -1, -1):
        after[i] = prod
        s = splits[i]
        if s and s == s and s > 0:
            prod *= s
    out = []
    for i in range(n):
        if not close[i] or close[i] != close[i] or adj[i] != adj[i]:
            continue
        f = (adj[i] / close[i]) / after[i]
        f = round(f, 6)
        if not out or abs(f / out[-1][1] - 1) > 1e-4:
            out.append([dates[i], f])
    return out


def sane(seg):
    return bool(seg) and all(0.15 <= f <= 1.05 for _, f in seg)


def run(code_suffix, today=None):
    import yfinance as yf
    import pandas as pd
    today = today or dt.date.today()
    start = (today - dt.timedelta(days=365 * YEARS + 30)).isoformat()
    stocks, failed, dropped = {}, 0, []
    items = sorted(code_suffix.items())
    for i in range(0, len(items), CHUNK):
        part = items[i:i + CHUNK]
        syms = [c + s for c, s in part]
        try:
            raw = yf.download(syms, start=start, auto_adjust=False, actions=True, group_by="ticker",
                              threads=True, progress=False)
        except Exception as e:
            print("  ⚠ 批次 %d: %s" % (i, e))
            failed += len(part)
            continue
        for c, s in part:
            try:
                h = raw[c + s] if isinstance(raw.columns, pd.MultiIndex) else raw
                h = h.dropna(subset=["Close", "Adj Close"])
                if len(h) < 20:
                    failed += 1
                    continue
                sp = h["Stock Splits"].fillna(0).tolist() if "Stock Splits" in h else [0] * len(h)
                dates = [d.strftime("%Y-%m-%d") for d in pd.to_datetime(h.index)]
                seg = segments(dates, h["Adj Close"].tolist(), h["Close"].tolist(), sp)
                if sane(seg):
                    stocks[c] = seg
                else:
                    dropped.append(c)
            except Exception:
                failed += 1
        print("  %d/%d,可用 %d" % (min(i + CHUNK, len(items)), len(items), len(stocks)))
        time.sleep(1.5)
    return stocks, failed, dropped


def main():
    cs = load_universe()
    stocks, failed, dropped = run(cs)
    if len(stocks) < 0.6 * len(cs):
        print(json.dumps({"status": "too_few", "n": len(stocks), "universe": len(cs)}, ensure_ascii=False))
        return 0                                         # 不覆蓋舊檔
    doc = {"asof": dt.date.today().isoformat(), "source": "Yahoo Finance(yfinance,Adj Close ÷ Close ÷ 之後的 Stock Splits)",
           "note": "係數 × 交易所原始價 = 還原價;只在除權息日改變", "stocks": stocks}
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, OUT)
    sample = {c: stocks.get(c) for c in ("2330", "2454", "3081", "3665", "3026")}
    print(json.dumps({"status": "ok", "n": len(stocks), "universe": len(cs), "failed": failed, "dropped": dropped[:20],
                      "with_events": sum(1 for v in stocks.values() if len(v) > 1), "sample": sample}, ensure_ascii=False)[:3000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
