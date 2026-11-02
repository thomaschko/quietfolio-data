# -*- coding: utf-8 -*-
"""
margin_share_core.py — 族群 / ETF 融資占比 純函式核心(2026-11-01,Phase 1)
============================================================
全部是純函式,不做 IO。參數集中在 MARGIN_SHARE_CFG。
口徑(規格第 1 節):
  est_value(code) = margin_lots × 1000 × close(同一交易日)
  這是「擔保股票市值估算」,不是官方「融資金額」(官方為借出的本金)。程式與輸出一律稱 est_value。
  市場總額(分母)= 同一市場、同一日所有有 est_value 的股票加總;上市、上櫃各自為分母,不混。
  有融資餘額但缺同日收盤價 → 不計入 est_value,列入 excluded_lots;不補價、不以前一日代替。
全部是描述,不改任何收錄、排序、門檻,不輸出買賣建議;融資占比不代表看多或看空。
"""
import bisect
import statistics

MARGIN_SHARE_CFG = {
    "change_windows": (5, 20),     # share 與 5、20 個交易日前比較(百分點)
    "top_k": 3,                    # 每組列出 est_value 最大的 3 檔
    "recon_warn_pct": 10.0,        # recon_ratio 偏離歷史中位數超過 10% → 旗標(只提示,不調整)
    "recon_min_history": 20,       # 至少 20 天 recon 歷史才判斷偏離
    # 歷史深度(規格 8.3)
    "hist_none": 120,              # < 120 天:不輸出分位數與事件
    "hist_full": 245,              # 120~244:low_confidence
    "hist_2y": 490,                # ≥ 490:ok_2y
    # 事件日(規格 8.5;先登記,看資料前固定,不得事後調整)
    "event_tail": 0.05,            # 日報酬落在可得歷史最低 / 最高 5%
    "min_abs_ret": 1.5,            # 且 |日報酬| ≥ 1.5%
    "event_min_for_stats": 10,     # 事件數 < 10:不輸出中位數 / 平均 / 次數彙整,只列事件
    "event_fwd_days": 5,           # d_lots_5d_pct:t → t+5(只用於歷史報表)
}

UNCLASSIFIED = "未分類"
ETF = "ETF"


# ------------------------------------------------------------------ 基本計算
def est_values(margin, close):
    """margin {代號: 融資餘額(張)}、close {代號: 收盤價(元)}(同一交易日)
    → (est {代號: 元}, lots {代號: 張}, excluded {代號: 張})
    - 融資餘額為 0:est = 0,仍算一檔(n_stocks)
    - 融資餘額 > 0 但缺收盤價:不進 est,列入 excluded
    - 融資餘額缺(None):整檔不列(不視為 0)"""
    est, lots, excluded = {}, {}, {}
    for code, l in margin.items():
        if l is None:
            continue
        l = float(l)
        if l == 0:
            est[code], lots[code] = 0.0, 0.0
            continue
        p = close.get(code)
        if p is None or p <= 0:
            excluded[code] = l
            continue
        est[code] = l * 1000.0 * float(p)
        lots[code] = l
    return est, lots, excluded


def _top(codes, est, names, k, group_total):
    ranked = sorted((c for c in codes if est.get(c, 0) > 0), key=lambda c: (-est[c], c))[:k]
    return [{"code": c, "name": names.get(c, ""), "w": round(est[c] / group_total, 4) if group_total else None}
            for c in ranked]


def _group_row(codes, est, lots, names, total, k):
    v = sum(est[c] for c in codes)
    top = _top(codes, est, names, k, v)
    return {
        "est_value": v,
        "lots": sum(lots[c] for c in codes),
        "share": v / total if total else None,
        "n_stocks": len(codes),
        "n_with_margin": sum(1 for c in codes if lots[c] > 0),
        "top3": top,
        "concentration": round(sum(est[c] for c in codes if c in {t["code"] for t in top}) / v, 4) if v else None,
    }


def industry_of(code, industry_map, etf_set):
    """互斥分組:ETF → 'ETF';官方產業別對得上 → 產業別;其餘 → '未分類'(不丟棄)。"""
    if code in etf_set:
        return ETF
    return industry_map.get(code) or UNCLASSIFIED


def by_industry(est, lots, industry_map, etf_set, names, k=None):
    """→ (total, {組: row});所有組 est_value 加總 = total(含 ETF、未分類)。"""
    k = k or MARGIN_SHARE_CFG["top_k"]
    total = sum(est.values())
    groups = {}
    for c in est:
        groups.setdefault(industry_of(c, industry_map, etf_set), []).append(c)
    return total, {g: _group_row(cs, est, lots, names, total, k) for g, cs in groups.items()}


def by_theme(est, lots, themes, etf_set, names, k=None):
    """題材可重疊(inclusive):一檔屬於多個題材,每個題材都完整計入。ETF 不計入任何題材。
    themes {題材: [代號]} → (rows {題材: row + n_multi}, coverage_union, unmapped_share)
    只計入當日有 est 的股票(其他市場或無融資資料的代號忽略)。"""
    k = k or MARGIN_SHARE_CFG["top_k"]
    total = sum(est.values())
    member = {}
    for t, codes in themes.items():
        for c in set(codes):
            if c in est and c not in etf_set:
                member.setdefault(c, set()).add(t)
    rows = {}
    for t, codes in themes.items():
        cs = sorted({c for c in codes if c in est and c not in etf_set})
        r = _group_row(cs, est, lots, names, total, k)
        r["n_multi"] = sum(1 for c in cs if len(member[c]) > 1)
        rows[t] = r
    union = sum(est[c] for c in member)
    cov = union / total if total else None
    return rows, cov, (1 - cov) if cov is not None else None


# ------------------------------------------------------------------ 變化與歷史
def share_change(history, date, key, n):
    """history:[(日期, {key: share})] 由舊到新、只含 ≤ date 的資料日(呼叫端保證無 look-ahead)。
    → date 的 share 減去 n 個資料日前的 share(百分點);任一缺 → None。"""
    dates = [d for d, _ in history]
    i = bisect.bisect_right(dates, date) - 1
    if i < 0 or dates[i] != date or i - n < 0:
        return None
    a, b = history[i][1].get(key), history[i - n][1].get(key)
    if a is None or b is None:
        return None
    return round((a - b) * 100, 3)


def history_level(n_days, cfg=MARGIN_SHARE_CFG):
    """→ 'none'(< 120,不輸出分位數與事件)/ 'low'(low_confidence)/ 'ok' / 'ok_2y'"""
    if n_days < cfg["hist_none"]:
        return "none"
    if n_days < cfg["hist_full"]:
        return "low"
    return "ok_2y" if n_days >= cfg["hist_2y"] else "ok"


def pctile(value, past, n_days=None, cfg=MARGIN_SHARE_CFG):
    """value 在 past(不含當日)中的百分位(0~100,中位排名)。歷史不足 120 天 → None。"""
    n = len(past) if n_days is None else n_days
    if value is None or history_level(n, cfg) == "none":
        return None
    vals = sorted(x for x in past if x is not None)
    if not vals:
        return None
    lo = bisect.bisect_left(vals, value)
    hi = bisect.bisect_right(vals, value)
    return round((lo + hi) / 2 / len(vals) * 100, 1)


def recon_ratio(total_est, official_k):
    """估算總額 ÷ 官方融資金額(仟元 → 元)。只顯示,不用來調整任何占比。"""
    if not official_k or total_est is None:
        return None
    return round(total_est / (official_k * 1000.0), 4)


def recon_off(ratio, past_ratios, cfg=MARGIN_SHARE_CFG):
    """recon_ratio 偏離歷史中位數超過 recon_warn_pct → True;歷史不足 → None。"""
    past = [x for x in past_ratios if x]
    if ratio is None or len(past) < cfg["recon_min_history"]:
        return None
    m = statistics.median(past)
    return abs(ratio / m - 1) * 100 > cfg["recon_warn_pct"]


def note_flags(notes):
    """官方註記(原文)→ {註記: 檔數};空白不列。特殊狀態預設仍計入,只標示。"""
    out = {}
    for n in notes.values():
        n = (n or "").strip()
        if n:
            out[n] = out.get(n, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def market_flags(margin_date, close_date, excluded, notes):
    """→ (flags list, unreliable bool)"""
    flags = []
    unreliable = False
    if margin_date != close_date:
        flags.append({"flag": "date_mismatch", "margin_date": margin_date, "close_date": close_date})
        unreliable = True
    if excluded:
        flags.append({"flag": "missing_price", "n": len(excluded), "lots": sum(excluded.values())})
    nf = note_flags(notes)
    if nf:
        flags.append({"flag": "suspended_or_special", "n": sum(nf.values()), "by_note": nf,
                      "note": "官方註記原文;預設仍計入"})
    return flags, unreliable


# ------------------------------------------------------------------ 事件日(規格 8.5)
def _quantile(sorted_vals, q):
    if not sorted_vals:
        return None
    pos = (len(sorted_vals) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def index_returns(index_series):
    """[(日期, 收盤)] 由舊到新 → [(日期, 日報酬%)](第一天沒有)"""
    out = []
    for (d0, c0), (d1, c1) in zip(index_series, index_series[1:]):
        if c0 and c1:
            out.append((d1, (c1 / c0 - 1) * 100))
    return out


def event_days(returns, cfg=MARGIN_SHARE_CFG):
    """大跌日:報酬 ≤ 可得歷史第 5 百分位 且 ≤ −1.5%;大漲日:≥ 第 95 百分位 且 ≥ +1.5%。
    → [(日期, 'down'|'up', 報酬%)];歷史不足 120 天 → []。"""
    if history_level(len(returns), cfg) == "none":
        return []
    vals = sorted(r for _, r in returns)
    lo = _quantile(vals, cfg["event_tail"])
    hi = _quantile(vals, 1 - cfg["event_tail"])
    m = cfg["min_abs_ret"]
    out = []
    for d, r in returns:
        if r <= lo and r <= -m:
            out.append((d, "down", round(r, 3)))
        elif r >= hi and r >= m:
            out.append((d, "up", round(r, 3)))
    return out


def _pct(a, b):
    return round((a / b - 1) * 100, 3) if a is not None and b else None


def event_group_changes(prev, cur, fwd=None):
    """prev / cur / fwd:{組: {share, lots, est_value}}(t−1 / t / t+5)。
    → {組: {d_share_pp, d_lots, d_lots_pct, d_est_pct, d_lots_5d_pct}};d_lots = 張數增減(張);
    d_lots_5d_pct 是回溯描述,只在歷史報表(fwd 有給)時出現,不得用於當日輸出。"""
    out = {}
    for g, c in cur.items():
        p = prev.get(g)
        if not p:
            continue
        row = {
            "d_share_pp": round((c["share"] - p["share"]) * 100, 3) if c.get("share") is not None and p.get("share") is not None else None,
            "d_lots": round(c["lots"] - p["lots"]) if c.get("lots") is not None and p.get("lots") is not None else None,
            "d_lots_pct": _pct(c.get("lots"), p.get("lots")),
            "d_est_pct": _pct(c.get("est_value"), p.get("est_value")),
        }
        if fwd is not None:
            f = fwd.get(g)
            row["d_lots_5d_pct"] = _pct(f.get("lots"), c.get("lots")) if f else None
        out[g] = row
    return out


def top_lot_drops(prev_lots, cur_lots, names, n=10):
    """全市場減少張數最多的前 n 檔:代號、名稱、減少張數、占前一日餘額比例。"""
    rows = []
    for c, p in prev_lots.items():
        cur = cur_lots.get(c)
        if cur is None or not p:
            continue
        d = cur - p
        if d < 0:
            rows.append({"code": c, "name": names.get(c, ""), "drop_lots": -d, "pct_of_prev": round(-d / p * 100, 2)})
    rows.sort(key=lambda r: (-r["drop_lots"], r["code"]))
    return rows[:n]


def summarize_events(events, per_event, cfg=MARGIN_SHARE_CFG):
    """events [(日期, 方向, 報酬)];per_event {日期: {組: 變化}}。
    n_events < 10 → 只回逐事件列表;≥ 10 → 加上各組 d_lots_pct 中位數,
    以及大跌日「融資減少張數最多的前 3 組」出現次數。"""
    out = {"n_events": len(events), "events": [{"date": d, "dir": k, "ret": r} for d, k, r in events]}
    if len(events) < cfg["event_min_for_stats"]:
        out["stats"] = None
        out["stats_note"] = "事件數不足 %d,不輸出中位數或彙整,只列事件" % cfg["event_min_for_stats"]
        return out
    stats = {}
    for kind in ("down", "up"):
        ds = [d for d, k, _ in events if k == kind]
        if len(ds) < cfg["event_min_for_stats"]:
            stats[kind] = {"n": len(ds), "median_d_lots_pct": None}
            continue
        groups = {}
        for d in ds:
            for g, row in (per_event.get(d) or {}).items():
                if row.get("d_lots_pct") is not None:
                    groups.setdefault(g, []).append(row["d_lots_pct"])
        stats[kind] = {"n": len(ds), "median_d_lots_pct": {g: round(statistics.median(v), 3) for g, v in groups.items()}}
        if kind == "down":
            cnt = {}
            for d in ds:
                # 2026-11-02 使用者同意:依減少張數(不是百分比)排序,避免一兩檔的小產業排到前面;只計真的減少
                rows = [(g, r["d_lots"]) for g, r in (per_event.get(d) or {}).items() if r.get("d_lots") is not None and r["d_lots"] < 0]
                for g, _ in sorted(rows, key=lambda x: (x[1], x[0]))[:3]:
                    cnt[g] = cnt.get(g, 0) + 1
            stats[kind]["top3_lot_drop_counts"] = dict(sorted(cnt.items(), key=lambda kv: -kv[1]))
    out["stats"] = stats
    return out


NOTES = [
    "est_value = 融資餘額(張)× 1000 × 同日收盤價,是擔保股票市值估算,不是官方融資金額;占比與官方口徑會有差異",
    "share 為占各自市場(上市 / 上櫃)est_value 總額;題材可重疊,題材 share 相加不等於 100%",
    "融資餘額是時點存量;占比上升可能只是該群組股價上漲,請與張數(lots)對照",
    "歷史上的產業別與題材皆為現行分類回推,不代表當時的市場結構",
    "本模組沒有預測效力的證據,只描述槓桿資金的分布",
]
