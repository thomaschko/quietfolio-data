# -*- coding: utf-8 -*-
"""
chip_structure_core.py — 個股籌碼結構:集保戶股權分散表 × 法人買賣超(Phase 2 純函式;2026-10-27 新增)
============================================================
全部是純函式,不做 IO(讀檔、組輸出在之後的 Phase 3)。全部屬「描述」,不是買賣建議,
不影響任何雷達的收錄、排序、門檻。

輸入格式:
  集保週:[(資料日 'YYYY-MM-DD', {代號: {分級: {"holders", "shares", "pct"}}})],由舊到新
          (分級 1–15 級距、16 差異數調整、17 合計;tdcc_fetch.parse 的列轉過來)
  法人日:{'YYYYMMDD': insti_daily/YYYYMMDD.json 的內容}
          twse / tpex:{代號: [f 外資(含外資自營商), t 投信, d 自營商, n 三大法人合計]},單位「股」
          foreign_pct:{"twse": {代號: %}, "tpex": {代號: %}}
  交易日:set('YYYYMMDD')(trading_calendar.known_trading_days)

身分限制(輸出的 notes 會寫,見 NOTES):集保分散表不標示持有人身分;投信基金分戶持股,
  投信買超不一定反映成大戶增加 → agreement 只用「外資 + 自營商」,投信單獨顯示、不納入。
  這是依一般理解的推論,尚未用資料驗證。
============================================================
"""
import datetime as dt

# ── 參數(門檻都是「未驗證的描述用預設值」,沒有回測依據;Phase 5 驗證前不要當成有預測力)──
CHIP_CFG = {
    # 分層用集保分級(股數界線見註解);邊界只放這裡,函式不寫死
    "tiers": {
        "retail": list(range(1, 9)),     # 1–8:≤ 50,000 股(≤ 50 張)
        "mid": list(range(9, 12)),       # 9–11:50,001–400,000 股
        "big": list(range(12, 16)),      # 12–15:≥ 400,001 股(≥ 400 張)
    },
    "big_min_shares": 400001,
    "k1000_levels": [15],                # 千張大戶:≥ 1,000,001 股
    "level_adj": 16,                     # 差異數調整:不併入任何一層,另列
    "level_total": 17,
    "trend_weeks": 8,                    # big_trend 看近 8 週(7 個週變化)
    "trend_min_moves": 6,                # 同方向週數 ≥ 6
    "trend_pp": 1.0,                     # 累積變化門檻(百分點)
    "flat_pp": 0.3,
    "osc_flips": 3,                      # 方向翻轉次數
    "big_delta_min_pp": 0.05,            # 大戶比重週變化 < 0.05 百分點 → 視為沒變(agreement=unclear)
    "inst_min_pct": 0.02,                # 法人週買賣超 < 集保庫存的 0.02% → 視為沒變
    "explain_min_pct": 0.05,             # |Δ大戶股數| < 集保庫存的 0.05% → explain_ratio 不算(分母近 0)
    "share_jump_pct": 1.0,               # 合計股數週變動 > 1% → capital_change_suspect
    "missing_week_days": 9,              # 相鄰資料日相隔 > 9 個日曆日 → missing_week
    "stab_lookback": 12,
    "stab_recent": 4,
    "stab_d1w_weeks": 2,
}

LOTS_TO_SHARES = 1000

NOTES = [
    "集保為週資料、法人為日資料,只在集保週的尺度上對齊;集保週 = 上一個資料日(不含)到本次資料日(含)之間的交易日。",
    "集保分散表不標示持有人身分:大戶級距混有外資、投信、自營商、董監、政府基金與自然人,無法分辨是誰在買賣;與法人同向只是印證,不是因果。",
    "投信基金通常分戶持股,投信買超不一定反映為大戶增加;agreement 只用外資+自營商,投信單獨顯示、不納入。此限制依一般理解推論,尚未用資料驗證。",
    "外資持股比例(證交所/櫃買公布)與集保大戶比重是兩份資料,分開顯示,不相加、不相減、不互相推算。",
    "增資、減資、配股、可轉債轉換會改變分母;capital_change_suspect 成立的那週,週變化標 unreliable。",
    "所有門檻是未驗證的描述用預設值;本模組沒有任何預測效力的證據。",
]


# ── 單位 ──
def to_shares(value, unit):
    """法人買賣超換成「股」。unit:'shares'(股,T86 與 insti_daily 都是)/ 'lots'(張 = 1,000 股)。
    None 照回 None(缺資料不補 0)。"""
    if value is None:
        return None
    if unit == "shares":
        return int(value)
    if unit == "lots":
        return int(round(value * LOTS_TO_SHARES))
    raise ValueError(f"不認得的單位:{unit!r}")


# ── 集保:單週指標 ──
def levels_from_rows(rows):
    """tdcc_fetch.parse 的列 → {代號: {分級: {"holders", "shares", "pct"}}}。"""
    out = {}
    for r in rows:
        out.setdefault(r["code"], {})[r["level"]] = {"holders": r["holders"], "shares": r["shares"], "pct": r["pct"]}
    return out


def week_metrics(levels, cfg=CHIP_CFG):
    """單檔單週 → 指標;沒有合計(第 17 級)或合計為 0 → None。
    比重用「該層股數 / 第17級合計股數」(分母是集保庫存合計,不是發行股數);第 16 級不併入任何一層。"""
    tot = levels.get(cfg["level_total"])
    if not tot or not tot.get("shares") or tot.get("holders") is None:
        return None
    total = tot["shares"]

    def tier(lvs, key):
        return sum((levels[i][key] or 0) for i in lvs if i in levels)

    t = cfg["tiers"]
    adj = levels.get(cfg["level_adj"]) or {}
    m = {
        "holders_total": int(tot["holders"]), "shares_total": total,
        "shares_big": tier(t["big"], "shares"), "shares_mid": tier(t["mid"], "shares"),
        "shares_retail": tier(t["retail"], "shares"),
        "holders_big": int(tier(t["big"], "holders")), "holders_mid": int(tier(t["mid"], "holders")),
        "holders_retail": int(tier(t["retail"], "holders")),
        "adj_shares": adj.get("shares") or 0, "adj_holders": int(adj.get("holders") or 0),
    }
    for k in ("big", "mid", "retail"):
        m[f"pct_{k}"] = round(m[f"shares_{k}"] / total * 100, 4)
    m["pct_1000"] = round(tier(cfg["k1000_levels"], "shares") / total * 100, 4)
    m["avg_shares_per_holder"] = round(total / tot["holders"], 1) if tot["holders"] else None
    return m


# ── 日期 ──
def _d8(d):
    return str(d).replace("-", "")


def _date(d):
    return dt.datetime.strptime(_d8(d), "%Y%m%d").date()


def tdcc_week_days(prev_date, date, calendar):
    """集保週的交易日:(上一個資料日, 本次資料日],依交易日曆(遇連假就是實際交易日,不是固定 5 天)。
    prev_date 為 None(第一週)→ 往前 7 個日曆日。"""
    hi = _d8(date)
    lo = _d8(prev_date) if prev_date else (_date(date) - dt.timedelta(days=7)).strftime("%Y%m%d")
    return sorted(d for d in calendar if lo < d <= hi)


def week_of(day, tdcc_dates):
    """某交易日屬於哪個集保週 → 那週的資料日('YYYY-MM-DD');比最後一個資料日還晚 → None(下一週,還沒公布)。"""
    d = _d8(day)
    for td in sorted(tdcc_dates):
        if d <= _d8(td):
            return td
    return None


# ── 法人:同一集保週加總 ──
def _inst_row(doc, code):
    """→ (市場, [f, t, d, n]) 或 (None, None)。"""
    for mk in ("twse", "tpex"):
        sec = (doc or {}).get(mk)
        if sec and code in sec:
            return mk, sec[code]
    return None, None


def inst_week(code, days, insti_daily, unit="shares"):
    """→ {foreign, trust, dealer, net, net_for_agreement, days, missing_days, zero_filled_days}。
    2026-10-27 使用者決定(選項 2):某天「該股所屬市場的那段有資料」但代號不在表裡 → 當天算 0 股,
      記在 zero_filled_days(推定:T86/TPEx 當天沒有法人交易的股票不列出,官方沒有明文,未驗證)。
      所屬市場從同一集保週其他天判斷;整週都沒出現(多是興櫃/未上市,T86/TPEx 不涵蓋)→ 判斷不了,算缺。
    該市場那天整段沒有(檔案不在、或 twse/tpex 是 null/空),或上櫃那天標 tpex_count_low → 算缺。任一天缺 → 數值全部 None,不補值。"""
    out = {"days": list(days), "missing_days": [], "zero_filled_days": []}
    rows = {d: _inst_row(insti_daily.get(d), code) for d in days}
    markets = {mk for mk, _ in rows.values() if mk}
    market = markets.pop() if len(markets) == 1 else None
    acc = [0, 0, 0, 0]
    for d in days:
        mk, row = rows[d]
        if row is None:
            doc = insti_daily.get(d) or {}
            sec = doc.get(market) if market else None
            # 2026-10-28:那天上櫃檔數明顯偏少(GAS 標 tpex_count_low)→ 名單可能不完整,不補 0
            if sec and not (market == "tpex" and doc.get("tpex_count_low")):
                out["zero_filled_days"].append(d)
            else:
                out["missing_days"].append(d)
            continue
        for i in range(4):
            acc[i] += to_shares(row[i], unit)
    if out["missing_days"] or not days:
        out.update(foreign=None, trust=None, dealer=None, net=None, net_for_agreement=None)
    else:
        out.update(foreign=acc[0], trust=acc[1], dealer=acc[2], net=acc[3], net_for_agreement=acc[0] + acc[2])
    return out


def foreign_pct_at(code, days, insti_daily):
    """該集保週「最後一個交易日」的外資持股比例;那天沒有就是 None(不往前找、不補值)。"""
    if not days:
        return None
    doc = insti_daily.get(days[-1]) or {}
    for mk in ("twse", "tpex"):
        sec = (doc.get("foreign_pct") or {}).get(mk)
        if sec and code in sec:
            return sec[code]
    return None


# ── 旗標 ──
def capital_change_suspect(prev_total, total, cfg=CHIP_CFG):
    if not prev_total:
        return False
    return abs(total - prev_total) / prev_total * 100 > cfg["share_jump_pct"]


def missing_week(prev_date, date, cfg=CHIP_CFG):
    return prev_date is not None and (_date(date) - _date(prev_date)).days > cfg["missing_week_days"]


def date_mismatch(tdcc_asof, inst_asof, tdcc_dates):
    """集保最新資料日與法人最新日期不屬於同一個集保週 → True(只標示)。"""
    if not tdcc_asof or not inst_asof:
        return True
    return week_of(inst_asof, tdcc_dates) != tdcc_asof


# ── 標籤 ──
def explain_ratio(inst_net, d_shares_big, shares_total, cfg=CHIP_CFG):
    """inst_net_week / Δ大戶股數;|Δ大戶股數| < 集保庫存 × explain_min_pct% → None(分母近 0)。"""
    if inst_net is None or d_shares_big is None or not shares_total:
        return None
    if abs(d_shares_big) < shares_total * cfg["explain_min_pct"] / 100:
        return None
    return round(inst_net / d_shares_big, 3)


def agreement(d_big_pp, inst_for_agreement, shares_total, unreliable=False, cfg=CHIP_CFG):
    """同週:大戶比重變化 × 外資+自營商買賣超(投信不納入)。任一方太小、缺資料、或該週 unreliable → unclear。"""
    if unreliable or d_big_pp is None or inst_for_agreement is None or not shares_total:
        return "unclear"
    if abs(d_big_pp) < cfg["big_delta_min_pp"] or abs(inst_for_agreement) < shares_total * cfg["inst_min_pct"] / 100:
        return "unclear"
    if d_big_pp > 0 and inst_for_agreement > 0:
        return "confirm_up"
    if d_big_pp < 0 and inst_for_agreement < 0:
        return "confirm_down"
    if inst_for_agreement > 0:
        return "diverge_inst_buy_big_down"
    return "diverge_inst_sell_big_up"


def big_trend(pct_big, unreliable=(), cfg=CHIP_CFG):
    """近 trend_weeks 週的 pct_big(由舊到新)→ 標籤。不足 → insufficient_history;
    窗內有 unreliable 的週變化(股本變動)→ unreliable(不硬給方向)。"""
    n = cfg["trend_weeks"]
    if len(pct_big) < n or any(v is None for v in pct_big[-n:]):
        return "insufficient_history"
    if any(unreliable[-(n - 1):]):
        return "unreliable"
    w = pct_big[-n:]
    ch = [b - a for a, b in zip(w, w[1:])]
    cum = w[-1] - w[0]
    ups, downs = sum(c > 0 for c in ch), sum(c < 0 for c in ch)
    signs = [1 if c > 0 else -1 for c in ch if c != 0]
    flips = sum(a != b for a, b in zip(signs, signs[1:]))
    if ups >= cfg["trend_min_moves"] and cum >= cfg["trend_pp"]:
        return "rising"
    if downs >= cfg["trend_min_moves"] and cum <= -cfg["trend_pp"]:
        return "falling_trend"
    if flips >= cfg["osc_flips"] and abs(cum) < cfg["trend_pp"]:
        return "oscillating"
    if abs(cum) < cfg["flat_pp"]:
        return "flat"
    return "mixed"


def stabilizing(pct_big, d1w, cfg=CHIP_CFG):
    """近 4 週 pct_big 沒有再低於前 8 週(近 12 週內)的最低,且近 2 週 d1w ≥ 0。歷史不足 → None。只是描述狀態。"""
    lb, rc, k = cfg["stab_lookback"], cfg["stab_recent"], cfg["stab_d1w_weeks"]
    if len(pct_big) < lb or len(d1w) < k:
        return None
    w = pct_big[-lb:]
    last = d1w[-k:]
    if any(v is None for v in w) or any(v is None for v in last):
        return None
    return min(w[-rc:]) >= min(w[:-rc]) and all(v >= 0 for v in last)


# ── 單檔整條時間序列 ──
def _diff(series, i, lag, key):
    if i - lag < 0 or series[i] is None or series[i - lag] is None:
        return None
    return round(series[i][key] - series[i - lag][key], 4)


def stock_series(code, tdcc_weeks, insti_daily, calendar, cfg=CHIP_CFG):
    """單檔 → 每週一筆紀錄(由舊到新)。第 i 週的所有欄位只用第 0..i 週與該週以前的法人資料(無 look-ahead)。"""
    metrics = [week_metrics(levels.get(code, {}), cfg) for _, levels in tdcc_weeks]
    out = []
    for i, (date, _) in enumerate(tdcc_weeks):
        m = metrics[i]
        prev_date = tdcc_weeks[i - 1][0] if i else None
        days = tdcc_week_days(prev_date, date, calendar)
        flags = []
        if missing_week(prev_date, date, cfg):
            flags.append("missing_week")
        prev = metrics[i - 1] if i else None
        unreliable = bool(m and prev and capital_change_suspect(prev["shares_total"], m["shares_total"], cfg))
        if unreliable:
            flags.append("capital_change_suspect")
        if m is None:
            flags.append("no_tdcc_row")
        inst = inst_week(code, days, insti_daily)
        if inst["missing_days"]:
            flags.append("inst_missing_days")
        if inst["zero_filled_days"]:
            flags.append("inst_zero_filled_days")
        if not days:
            flags.append("no_trading_days_in_calendar")
        rec = {"d": date, "m": m, "inst": inst, "flags": flags, "unreliable": unreliable,
               "foreign_pct": foreign_pct_at(code, days, insti_daily), "trading_days": days}
        for lag, name in ((1, "d1w"), (4, "d4w"), (12, "d12w")):
            rec[name] = None if m is None else {
                "big": _diff(metrics, i, lag, "pct_big"), "mid": _diff(metrics, i, lag, "pct_mid"),
                "retail": _diff(metrics, i, lag, "pct_retail"), "k1000": _diff(metrics, i, lag, "pct_1000"),
                "holders": _diff(metrics, i, lag, "holders_total"),
                "holders_retail": _diff(metrics, i, lag, "holders_retail"),
            }
        # 股本變動的那週(或往回 lag 週內有股本變動)→ 週變化標 unreliable,不修正數值
        rec["d1w_unreliable"] = unreliable
        rec["d4w_unreliable"] = any(r["unreliable"] for r in out[-3:]) or unreliable
        d_shares_big = (m["shares_big"] - prev["shares_big"]) if (m and prev) else None
        d_big = rec["d1w"]["big"] if rec["d1w"] else None
        rec["explain_ratio"] = None if unreliable else explain_ratio(
            inst["net"], d_shares_big, m["shares_total"] if m else None, cfg)
        rec["agreement"] = agreement(d_big, inst["net_for_agreement"], m["shares_total"] if m else None,
                                     unreliable, cfg)
        pct_hist = [x["pct_big"] if x else None for x in metrics[:i + 1]]
        unrel_hist = [r["unreliable"] for r in out] + [unreliable]
        rec["big_trend"] = big_trend(pct_hist, unrel_hist[1:], cfg)
        d1w_hist = [r["d1w"]["big"] if r["d1w"] else None for r in out] + [d_big]
        rec["stabilizing"] = stabilizing(pct_hist, d1w_hist, cfg)
        fp_prev = out[i - 4]["foreign_pct"] if i >= 4 else None
        rec["foreign_pct_d4w"] = (round(rec["foreign_pct"] - fp_prev, 4)
                                  if rec["foreign_pct"] is not None and fp_prev is not None else None)
        out.append(rec)
    return out
