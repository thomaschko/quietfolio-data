# -*- coding: utf-8 -*-
"""
consensus_core.py — 共識頁純函式(2026-11-04,Phase 2)
============================================================
三個分頁:
  A 市場氛圍:各指標在可得歷史(不含當日)的百分位;方向 +1 的指標高分位 = 偏熱、低分位 = 過度看空,
    方向 0 的只描述(外資、Put/Call、大額交易人)。偏熱 / 過度看空項數用來給整體標籤。
  B 槓桿 + 法人:近 N 日融資張數變化 × 同期三大法人買賣超金額,逐檔分四類;另做產業層級。
  C 回檔 + 融資增加 + 集保大戶增加:距 20 日高點跌幅、融資張數增加、集保大戶比重週變化同時成立。
門檻全部集中在 CONSENSUS_CFG,先登記、看資料前固定,不事後調整。全部是描述,不是買賣建議,不改任何雷達的收錄與排序。
"""
import bisect

CONSENSUS_CFG = {
    "zone_hi": 90.0, "zone_lo": 10.0,          # 百分位 ≥ 90 / ≤ 10 才算極端
    "min_hist": 120,                            # 歷史 < 120 天不給百分位與區間
    "mood_min_count": 3,                        # 偏熱(或過度看空)≥ 3 項且多於另一邊 → 整體標籤
    "window": 5,                                # B、C 的變化期間(交易日)
    "b_min_lots": 200, "b_min_pct": 3.0,        # B:融資 5 日增減 ≥ 200 張且 ≥ 3%
    "b_min_inst": 3e7,                          # B:三大法人 5 日買賣超金額 ≥ 3,000 萬元
    "b_top": 20,
    "c_drawdown": -10.0,                        # C:收盤價距 20 日最高收盤 ≤ −10%
    "c_hi_days": 20,
    "c_min_lots": 100, "c_min_pct": 3.0,        # C:融資 5 日增加 ≥ 100 張且 ≥ 3%
    "c_min_big_pp": 0.1,                        # C:集保大戶比重最新一週 ≥ +0.1 百分點
    "c_top": 40,
}


def pctile(value, past, cfg=CONSENSUS_CFG):
    """value 在 past(不含當日)中的中位排名百分位 0~100;歷史不足 → None"""
    vals = sorted(x for x in past if x is not None)
    if value is None or len(vals) < cfg["min_hist"]:
        return None
    lo, hi = bisect.bisect_left(vals, value), bisect.bisect_right(vals, value)
    return round((lo + hi) / 2 / len(vals) * 100, 1)


def zone(p, direction, cfg=CONSENSUS_CFG):
    """direction +1:高 = 偏熱;−1:高 = 過度看空;0:只描述(回 'high' / 'low' / None)"""
    if p is None:
        return None
    if p >= cfg["zone_hi"]:
        side = "high"
    elif p <= cfg["zone_lo"]:
        side = "low"
    else:
        return None
    if direction == 0:
        return side
    return "hot" if (side == "high") == (direction > 0) else "cold"


def mood_label(zones, cfg=CONSENSUS_CFG):
    hot = sum(1 for z in zones if z == "hot")
    cold = sum(1 for z in zones if z == "cold")
    if hot >= cfg["mood_min_count"] and hot > cold:
        return "偏熱", hot, cold
    if cold >= cfg["mood_min_count"] and cold > hot:
        return "過度看空", hot, cold
    if hot and cold:
        return "分歧", hot, cold
    return "中性", hot, cold


def retail_net_pct(inst_net_sum, total_oi):
    """散戶淨部位 = −三大法人淨未平倉;÷ 全市場未平倉 × 100"""
    if inst_net_sum is None or not total_oi:
        return None
    return round(-inst_net_sum / total_oi * 100, 2)


def drawdown(closes, n):
    """closes 由舊到新;最後一天相對前 n 天(含當日)最高收盤的跌幅 %"""
    xs = [c for c in closes[-n:] if c]
    if len(xs) < n or not closes[-1]:
        return None
    return round((closes[-1] / max(xs) - 1) * 100, 2)


def pct(a, b):
    return round((a / b - 1) * 100, 2) if a is not None and b else None


def classify_b(d_lots, d_pct, inst_value, cfg=CONSENSUS_CFG):
    """→ 'both_up' / 'both_down' / 'margin_up_inst_sell' / 'margin_down_inst_buy' / None"""
    if d_lots is None or d_pct is None or inst_value is None:
        return None
    up = d_lots >= cfg["b_min_lots"] and d_pct >= cfg["b_min_pct"]
    down = d_lots <= -cfg["b_min_lots"] and d_pct <= -cfg["b_min_pct"]
    buy = inst_value >= cfg["b_min_inst"]
    sell = inst_value <= -cfg["b_min_inst"]
    if up and buy:
        return "both_up"
    if down and sell:
        return "both_down"
    if up and sell:
        return "margin_up_inst_sell"
    if down and buy:
        return "margin_down_inst_buy"
    return None


def passes_c(dd, d_lots, d_pct, big_pp, flags, cfg=CONSENSUS_CFG):
    if None in (dd, d_lots, d_pct, big_pp):
        return False
    if "capital_change_suspect" in (flags or []):
        return False
    return (dd <= cfg["c_drawdown"] and d_lots >= cfg["c_min_lots"] and d_pct >= cfg["c_min_pct"]
            and big_pp >= cfg["c_min_big_pp"])


NOTES = [
    "全部是描述,不是買賣建議;門檻在看資料前固定,不事後調整",
    "百分位以可得歷史(不含當日)計算,歷史 < 120 天不給;期貨 / 選擇權自 2024-08(微型臺指上市)起",
    "散戶淨部位 = −(三大法人淨未平倉),是常用推估,包含所有非三大法人的交易人",
    "集保大戶 = 持股 ≥ 400 張的集保帳戶(含外資、投信、自然人),不是董監事;週資料",
    "融資增加不等於後市向上,法人買超也可能是避險或被動調整",
]
