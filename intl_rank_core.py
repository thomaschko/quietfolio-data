# -*- coding: utf-8 -*-
"""
intl_rank_core.py — 美/日/韓成交值排行榜的【純計算核心】(無網路、無檔案 I/O)

輸入:寬表 close / volume(index=日期, columns=ticker),已是「各自市場、未還原股利」的日線。
輸出:給 intl_rank.json 用的單一市場區塊。

設計重點(都有對應單元測試):
  1. 只用「已收盤」的交易日:丟掉尚未收盤的當日 bar,避免用盤中不完整成交量排名。
  2. 覆蓋率保護:最後一個日期若只有極少數股票有資料(休市/資料延遲),視為不完整,往前退一天。
  3. 成交值 = 收盤 × 成交量;vs均 = 對比前 20 個交易日均值;連續 = 連續在榜天數。
  4. 不使用任何未來資訊:每個日期的名次只用當日橫斷面。
"""
import json
import datetime as dt
import numpy as np
import pandas as pd

MIN_PRIOR_FOR_VSAVG = 10     # 前期至少要有幾天有效成交值才算 vs均
VSAVG_WINDOW = 20
COVERAGE_MIN_RATIO = 0.5     # 最後一天的有效股票數 < 前5天中位數 * 此比例 → 視為不完整


def drop_incomplete(close, volume, now_local, close_time):
    """丟掉『尚未收盤』與『未來』的日期,再套覆蓋率保護。回傳 (close, volume, notes)。"""
    notes = []
    idx = pd.to_datetime(close.index)
    close = close.copy(); volume = volume.copy()
    close.index = idx; volume.index = idx
    today = now_local.date()
    keep = []
    for d in idx:
        dd = d.date()
        if dd > today:
            notes.append(f"丟棄未來日期 {dd}")
            keep.append(False)
        elif dd == today and now_local.time() < close_time:
            notes.append(f"丟棄尚未收盤的當日 {dd}")
            keep.append(False)
        else:
            keep.append(True)
    keep = np.array(keep)
    close, volume = close[keep], volume[keep]
    # 覆蓋率保護
    while len(close) >= 6:
        # 有效 = 成交量 > 0 且收盤價有值(2026-10-19:Yahoo 曾回傳 Close 全 NaN、Volume 有值的最後一天)
        cnt = ((volume.fillna(0) > 0) & close.notna()).sum(axis=1)
        med = float(cnt.iloc[-6:-1].median())
        if med > 0 and cnt.iloc[-1] < med * COVERAGE_MIN_RATIO:
            notes.append(f"覆蓋率不足,丟棄 {close.index[-1].date()}({int(cnt.iloc[-1])}/{int(med)})")
            close, volume = close.iloc[:-1], volume.iloc[:-1]
        else:
            break
    return close, volume, notes


def build_market_block(close, volume, meta, *, label, currency, unit_short, price_dec,
                       top_n=30, fx_per_usd=None, tags=None, note=None):
    """
    meta:  {ticker: {"code": "6857", "name": "..."}}  —— code 是顯示用代碼(去掉 .T/.KS 後綴)
    tags:  {code: "記憶體"}(選配,只做標示)
    回傳 dict(asof / universe / by_value ...);資料不足時回傳 None。
    """
    if close is None or len(close) < 2:
        return None
    V = (close * volume).where(volume.fillna(0) > 0)          # 成交量缺或 0 → NaN
    Vf = V.fillna(0.0)
    asof = V.index[-1]
    ranks = Vf.rank(axis=1, ascending=False, method="first")
    in_top = ranks <= top_n
    last = Vf.iloc[-1].sort_values(ascending=False)
    top = [t for t in last.index[:top_n] if last[t] > 0]
    if not top:
        return None
    rows = []
    for i, t in enumerate(top, 1):
        s = in_top[t].values[::-1]
        streak = 0
        for v in s:
            if v: streak += 1
            else: break
        is_new = bool(len(in_top) >= 2 and not in_top[t].iloc[-2])
        prior = V[t].iloc[-(VSAVG_WINDOW + 1):-1].dropna()
        vs = None
        if len(prior) >= MIN_PRIOR_FOR_VSAVG and prior.mean() > 0:
            vs = int(round((V[t].iloc[-1] / prior.mean() - 1) * 100))
        prev_close = close[t].iloc[:-1].dropna()
        chg = None
        if len(prev_close) and prev_close.iloc[-1] > 0 and not np.isnan(close[t].iloc[-1]):
            chg = round((close[t].iloc[-1] / prev_close.iloc[-1] - 1) * 100, 2)
        val_local = float(V[t].iloc[-1]) / 1e8
        m = meta.get(t, {})
        code = m.get("code", t)
        r = {"rank": i, "code": code, "name": m.get("name", ""),
             "close": round(float(close[t].iloc[-1]), price_dec if price_dec else 0) if price_dec else int(round(float(close[t].iloc[-1]))),
             "chgPct": chg,
             "value": round(val_local, 1) if currency == "USD" else int(round(val_local)),
             "vsAvg": vs, "streak": int(streak), "isNew": is_new}
        if fx_per_usd and currency != "USD":
            r["usd"] = round(val_local / fx_per_usd, 1)
        if tags and code in tags:
            r["tag"] = tags[code]
        rows.append(r)
    return {"label": label, "asof": str(asof.date()), "currency": currency, "unit_short": unit_short,
            "price_dec": price_dec, "universe": int((last > 0).sum()), "by_value": rows,
            **({"note": note} if note else {})}


def fit_size(payload, limit=45000, step=5, min_n=10):
    """把每個市場的 by_value 一起縮短,直到 JSON(精簡格式)不超過 limit 字元。回傳 (payload, 最終 top_n)。"""
    def size(p): return len(json.dumps(p, ensure_ascii=False, separators=(",", ":")))
    n = max((len(m.get("by_value", [])) for m in payload["markets"].values()), default=0)
    while size(payload) > limit and n > min_n:
        n -= step
        for m in payload["markets"].values():
            m["by_value"] = m["by_value"][:n]
    payload["top_n"] = n
    return payload, n


def merge_preserve(old, new_markets, generated):
    """新結果覆蓋對應市場;沒更新到的市場沿用舊的(標 stale=True)。新抓失敗(None)也沿用舊的。"""
    out = {"generated": generated, "markets": {}}
    old_m = (old or {}).get("markets", {})
    for k in ("us", "jp", "kr"):
        nm = new_markets.get(k)
        if nm:
            nm.pop("stale", None)
            out["markets"][k] = nm
        elif k in old_m:
            om = dict(old_m[k]); om["stale"] = True
            out["markets"][k] = om
    return out
