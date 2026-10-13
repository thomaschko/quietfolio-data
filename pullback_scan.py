# -*- coding: utf-8 -*-
"""
============================================================
pullback_scan.py — 急跌反轉(策略A)每日掃描(2026-10-13)
============================================================
用回測同一套條件(pullback_backtest.signal_frame、DEFAULTS)掃描「今天」的股票池,
輸出 pullback_signals.json 給 Apps Script → 網頁「找股票 → 急跌反轉」。

股票池:前60個交易日(不含當日)平均成交值前 TOP 名(與回測 Phase 2/3 同定義)。
三組清單:
  confirm   今日觸發(有反轉確認):近3日回落12–30% + 近3日爆量收黑 + 今日收盤>昨日最高
            → 依回測規則為「明日開盤進場」,附目標價/停損價
  noconfirm 今日觸發(不等反轉):今日爆量收黑且近3日回落12–30%
  watch     觀察:近1–2日已爆量收黑、回落在區間內,尚未出現反轉日

資料:yfinance 近 HISTORY_DAYS 日還原價量;最新一日改用交易所官方收盤資料
(TWSE STOCK_DAY_ALL / TPEx 收盤行情)覆蓋或補上——Phase 1 發現 Yahoo 最新
一兩日成交量偏低,會讓「爆量」判斷漏掉訊號。

⚠ 回測結論(backtest_out/REPORT.md):相對同日池內隨機選股無顯著優勢,
  輸出中的 backtest_note 會顯示在網頁上。
============================================================
"""
import os
import re
import json
import time
import datetime as dt

import numpy as np
import pandas as pd
import requests
import urllib3

from pullback_backtest import signal_frame, load_bench, DEFAULTS
from pullback_phase2 import download, universe_membership, MIN_TWSE_STOCKS, MIN_TPEX_STOCKS

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

TOP = 200
HISTORY_DAYS = 220          # 日曆天(約150個交易日):涵蓋 60 日成交值均值 + 20 日高點/均量
MIN_ROWS = 90               # 至少要有這麼多交易日才掃描(新上市股略過)
OUT_FILE = "pullback_signals.json"
TPE = dt.timezone(dt.timedelta(hours=8))
BACKTEST_NOTE = ("回測(2019–2026,前200大成交值股票池):扣成本後平均報酬為正,但與同一天在池內"
                 "隨機買股、持有相同天數相比沒有顯著差異(樣本外 −0.42%,95% 信賴區間 −1.48%~+0.74%),"
                 "報酬主要來自大盤急殺後的反彈。")
TWSE_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TPEX_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"
CODE_RE = re.compile(r"[1-9]\d{3}")


def _num(x):
    try:
        v = float(str(x).replace(",", "").strip())
        return v if np.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def _roc_date(v):
    v = re.sub(r"\D", "", str(v or ""))
    if len(v) == 7:
        return pd.Timestamp(f"{int(v[:3]) + 1911}-{v[3:5]}-{v[5:]}")
    if len(v) == 8:
        return pd.Timestamp(f"{v[:4]}-{v[4:6]}-{v[6:]}")
    return None


def _get_json(url, verify=True, tries=4):
    last = None
    for i in range(tries):
        try:
            h = {"User-Agent": "Mozilla/5.0"}
            if i:
                h["Accept-Encoding"] = "identity"
            r = requests.get(url, timeout=40, verify=verify, headers=h)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
            time.sleep(5 * (i + 1))
    raise RuntimeError(f"{url} 抓取失敗({tries}次): {last}")


def fetch_official():
    """交易所官方當日收盤資料。回傳 (lists{code:.TW/.TWO}, names{code:name}, bars{code:{date,O,H,L,C,V}})。"""
    lists, names, bars = {}, {}, {}
    for url, suf, verify, k in (
            (TWSE_URL, ".TW", True, dict(code="Code", name="Name", o="OpeningPrice", h="HighestPrice",
                                          l="LowestPrice", c="ClosingPrice", v="TradeVolume")),
            (TPEX_URL, ".TWO", False, dict(code="SecuritiesCompanyCode", name="CompanyName", o="Open",
                                            h="High", l="Low", c="Close", v="TradingShares"))):
        for row in _get_json(url, verify=verify):
            code = str(row.get(k["code"], "")).strip()
            if not CODE_RE.fullmatch(code) or code in lists:
                continue
            lists[code] = suf
            names[code] = str(row.get(k["name"], "")).strip()
            o, h, l, c, v = (_num(row.get(k[x])) for x in ("o", "h", "l", "c", "v"))
            d = _roc_date(row.get("Date"))
            if d is not None and c and o and h and l and v is not None:
                bars[code] = {"date": d, "Open": o, "High": h, "Low": l, "Close": c, "Volume": v}
    n_tw = sum(s == ".TW" for s in lists.values())
    n_two = len(lists) - n_tw
    if n_tw < MIN_TWSE_STOCKS or n_two < MIN_TPEX_STOCKS:
        raise RuntimeError(f"官方清單數量異常:上市 {n_tw}、上櫃 {n_two},中止")
    return lists, names, bars


def merge_official(df, bar):
    """把官方當日收盤覆蓋/補上到還原價量最後一列(最新一日還原因子為1)。回傳 (df, 動作)。"""
    if bar is None:
        return df, "none"
    d = bar["date"]
    row = pd.DataFrame({k: [bar[k]] for k in ("Open", "High", "Low", "Close", "Volume")}, index=[d])
    row["Turnover"] = bar["Close"] * bar["Volume"]
    if len(df) and df.index[-1] == d:
        df = df.copy()
        df.loc[d, row.columns] = row.iloc[0].values
        return df, "replaced"
    if not len(df) or df.index[-1] < d:
        return pd.concat([df, row]), "appended"
    return df, "older"


def classify_today(df, p=None):
    """回傳 (類別 or None, 指標 dict)。類別:confirm / noconfirm / watch。只看最後一列(今天)。"""
    p = {**DEFAULTS, **(p or {})}
    d, s = signal_frame(df[["Open", "High", "Low", "Close", "Volume"]])
    if len(s) < 25 or pd.isna(s["hi20"].iloc[-1]):
        return None, {}
    last = s.iloc[-1]
    if bool(last["trig_confirm"]):
        kind = "confirm"
    elif bool(last["trig_noconfirm"]):
        kind = "noconfirm"
    elif bool(last["in_band"]) and bool(last["cap_recent"]) and not bool(last["reversal"]):
        kind = "watch"
    else:
        return None, {}
    closes = d["Close"]
    ref_high = float(last["hi20"])
    trough = float(closes.iloc[-3:].min())
    cap_idx = s.index[-3:][s["cap_day"].iloc[-3:].values]
    info = {
        "close": round(float(closes.iloc[-1]), 2),
        "chg_today": round(float(closes.iloc[-1] / closes.iloc[-2] - 1), 4),
        "drawdown_3d_max": round(float(last["dd_trough3"]), 4),
        "drawdown_today": round(float(last["dd"]), 4),
        "vol_mult_max_3d": round(float(s["vol_mult"].iloc[-3:].max()), 2),
        "cap_dates": [str(x.date()) for x in cap_idx],
        "ref_high": round(ref_high, 2),
        "trough_3d": round(trough, 2),
        "target": round(trough + p["TARGET_RECOVER"] * (ref_high - trough), 2),
        "stop": round(ref_high * (1 - p["STOP_DD"]), 2),
    }
    return kind, info


def main():
    now = dt.datetime.now(TPE)
    start = (now - dt.timedelta(days=HISTORY_DAYS)).date().isoformat()
    end = (now + dt.timedelta(days=1)).date().isoformat()

    print("[1] 官方當日收盤(股票清單+名稱+最新一日價量)")
    lists, names, bars = fetch_official()
    official_dates = pd.Series([b["date"] for b in bars.values()]).value_counts()
    official_date = official_dates.index[0] if len(official_dates) else None
    print(f"  上市 {sum(v == '.TW' for v in lists.values())} / 上櫃 {sum(v == '.TWO' for v in lists.values())},"
          f"官方資料日 {official_date.date() if official_date is not None else '無'}")

    print("[2] yfinance 近期價量")
    data, failed = download(lists, start, end, min_rows=MIN_ROWS)
    actions = {}
    for c in list(data):
        b = bars.get(c)
        if b is not None and official_date is not None and b["date"] != official_date:
            b = None                                        # 只用當天的官方資料
        data[c], actions[c] = merge_official(data[c], b)
    data_date = max(df.index[-1] for df in data.values())
    print(f"  可用 {len(data)} 檔;官方覆蓋 {sum(a == 'replaced' for a in actions.values())}、"
          f"補上 {sum(a == 'appended' for a in actions.values())};資料日 {data_date.date()}")

    cal = pd.DatetimeIndex(sorted(set().union(*[set(df.index) for df in data.values()])))
    turnover = pd.DataFrame({c: df["Turnover"] for c, df in data.items()}).reindex(cal)
    member = universe_membership(turnover, TOP)
    today_members = [c for c in member.columns if bool(member.loc[data_date, c])]
    avg60 = turnover.rolling(60, min_periods=60).mean().shift(1).loc[data_date]
    rank = avg60.rank(ascending=False, method="first")

    bench = load_bench(start)
    stress = None
    if bench is not None and len(bench):
        b = bench[bench.index <= data_date]
        b_dd = 1 - b.iloc[-1] / b.iloc[-20:].max()
        stress = {"index_date": str(b.index[-1].date()), "drawdown_from_20d_high": round(float(b_dd), 4),
                  "market_stress": bool(b_dd >= DEFAULTS["MKT_STRESS"])}

    print(f"[3] 掃描今日股票池 {len(today_members)} 檔")
    out = {"confirm": [], "noconfirm": [], "watch": []}
    for c in today_members:
        df = data[c]
        if df.index[-1] != data_date:
            continue                                        # 今天沒有資料(停牌等)
        kind, info = classify_today(df)
        if kind:
            out[kind].append({"code": c, "name": names.get(c, ""), "market": "上市" if lists[c] == ".TW" else "上櫃",
                              "turnover_rank": int(rank.get(c, 0)), **info})
    for k in out:
        out[k].sort(key=lambda x: -x["drawdown_3d_max"])

    result = {
        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "data_date": str(data_date.date()),
        "official_date": str(official_date.date()) if official_date is not None else None,
        "universe": f"前{TOP}大(前60日平均成交值)", "universe_size": len(today_members),
        "params": DEFAULTS, "market": stress, "backtest_note": BACKTEST_NOTE,
        "counts": {k: len(v) for k, v in out.items()},
        **out,
        "data_quality": {"usable": len(data), "failed": len(failed),
                         "official_replaced": sum(a == "replaced" for a in actions.values()),
                         "official_appended": sum(a == "appended" for a in actions.values())},
    }
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    print(f"→ {OUT_FILE}:有確認 {len(out['confirm'])}、不等反轉 {len(out['noconfirm'])}、觀察 {len(out['watch'])}")
    for k in out:
        for x in out[k][:8]:
            print(f"  [{k}] {x['code']} {x['name']} 回落{x['drawdown_3d_max']*100:.1f}% 量{x['vol_mult_max_3d']}倍 "
                  f"收{x['close']} 目標{x['target']} 停損{x['stop']}")


if __name__ == "__main__":
    main()
