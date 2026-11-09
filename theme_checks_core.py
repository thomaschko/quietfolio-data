# -*- coding: utf-8 -*-
"""
theme_checks_core.py — 題材頁面(趨勢檢視版)Phase 1:檢視項目判定規則(2026-11-09)
============================================================
純函式、不連網。輸入是已整理好的數列,輸出每一格的狀態 + 證據(數字、期間、來源、資料日、證據類型)。

狀態(state):
  "g"  有證據支持(●)    "y"  需要留意(◐)    "n"  資料不足(○)
  "na" 不適用(—,附原因與替代指標名稱)       "flat" 持平(無 ●/◐;只用在 Q4、Q5、Q6,見 CFG["flat_note"])
證據類型(ev):事實(已公布財報、公告)/ 公司財測 / 分析師預估 / 推論(程式規則推得)

規則依規格第 4 節,門檻全部在 THEME_PAGE_CFG。
**所有門檻都是未經驗證的描述用預設值,先登記,不得看結果後調整。**
本模組只做研究排序用的描述;不加總成分數,不輸出任何買賣判斷。

不偷看未來(no look-ahead):每筆資料有「可得日」,判定時只用 asof 當天(含)以前可得的資料。
  月營收:次月 avail_month_day 日(法定期限,保守;實際公布可能更早)
  季財報:Q1 5/15、Q2 8/14、Q3 11/14、Q4 隔年 3/31(一般業法定期限;金融業期限不同,先用同一組並註明)
缺資料一律不插補、不以前期值代替。
"""
import datetime as dt

THEME_PAGE_CFG = {
    "version": "2026-11-09",
    "note": "所有門檻皆為未經驗證的描述用預設值;登記後不得看結果調整",
    # 可得日
    "avail_month_day": 10,
    "avail_quarter": {1: (5, 15), 2: (8, 14), 3: (11, 14), 4: (3, 31)},   # Q4 為隔年
    # Q1 營收加速
    "rev_min_months": 18,
    "rev_accel_periods": 2,          # accel 連續幾「期」> 0;一期 = 一個月(滾動 3 個月),見 q1_revenue 說明
    "low_base_yoy3": -0.20,          # 去年同期 yoy3 < −20% → 低基期旗標
    # Q4 / Q5
    "q_min_quarters": 5,
    "gm_drop_pp": 1.5,               # 最近一季毛利率較前一季下降超過 1.5 個百分點 → ◐
    "flat_note": "Q4、Q5、Q6 不符合 ● 也不符合 ◐ 時標「持平」(flat),頁面不上色、不算 ●",
    # Q6
    "fcf_quarters": 8,               # 近 4 季 FCF 與去年同期比較 → 需 8 季
    # Q9
    "pe_years": 3,
    "pe_min_days": 250,              # 少於約 1 年交易日 → ○
    "pe_hi_pct": 80,
    # 大跌檢視
    "dd_lookback": 60,
    "dd_trigger": 0.08,              # 指數自近 60 日最高收盤回落 ≥ 8% → 大跌期間
    "dd_recover": 0.97,              # 回到前高 97% 以上 → 結束
    "rs_pp": 3.0,                    # 相對強度 ≥ +3 點 ●;≤ −3 點 ◐
    "crash_pct": 5,                  # 大跌日:單日報酬落在近 1 年最低 5%
    "crash_max_ret": -0.015,         #         且 ≤ −1.5%
    "stock_dd": 0.20,                # 四箱前提:個股自 52 週高點回落 ≥ 20%
    "high_window": 250,
    # 產業不適用
    "industry_overrides": {
        "金融保險業": {"q4": "淨利差、手續費收入占比", "q6": "資本適足率、逾放比"},
        "金融業": {"q4": "淨利差、手續費收入占比", "q6": "資本適足率、逾放比"},
    },
    "box_note": "台股沒有免費的 EPS 預估,四箱以已公布的營收與利潤率代替 EPS 預估判斷;④ 只能手動標記",
}
CFG = THEME_PAGE_CFG

TIERS = ("已證明", "開始出現", "只有故事", "資料不足", "無題材證據")


# ------------------------------------------------------------------ 共用
def cell(state, ev="推論", **kw):
    d = {"state": state, "ev": ev}
    d.update({k: v for k, v in kw.items() if v is not None})
    return d


def month_avail(y, m, cfg=CFG):
    y2, m2 = (y + 1, 1) if m == 12 else (y, m + 1)
    return dt.date(y2, m2, cfg["avail_month_day"])


def quarter_avail(y, q, cfg=CFG):
    mo, d = cfg["avail_quarter"][q]
    return dt.date(y + 1 if q == 4 else y, mo, d)


def _mkey(y, m):
    return y * 12 + (m - 1)


def _qkey(y, q):
    return y * 4 + (q - 1)


def _qlabel(k):
    return "%dQ%d" % (k // 4, k % 4 + 1)


def _mlabel(k):
    return "%d/%02d" % (k // 12, k % 12 + 1)


def industry_na(industry, item, cfg=CFG):
    alt = ((cfg["industry_overrides"].get(industry or "")) or {}).get(item)
    if alt:
        return cell("na", reason="%s 不適用此指標" % industry, alt=alt)
    return None


# ------------------------------------------------------------------ Q1 營收加速
def available_months(rows, asof, cfg=CFG):
    """rows: [(year, month, revenue)] → {month_key: revenue}(只留 asof 可得的)"""
    return {_mkey(y, m): float(v) for y, m, v in rows if v is not None and month_avail(y, m, cfg) <= asof}


def yoy3(mon, t):
    """最近 3 個月合計 ÷ 去年同期 3 個月合計 − 1;任一個月缺 → None(不插補)"""
    cur = [mon.get(t - i) for i in range(3)]
    base = [mon.get(t - 12 - i) for i in range(3)]
    if None in cur or None in base or sum(base) <= 0:
        return None
    return sum(cur) / sum(base) - 1


def q1_revenue(rows, asof, cfg=CFG, source="月營收(FinMind TaiwanStockMonthRevenue,原始資料 MOPS)"):
    """accel(t) = yoy3(t) − yoy3(t−3)。「連續 2 期」解讀為最近兩個月的滾動值 accel(t)、accel(t−1) 都 > 0
    (一期 = 一個月;規格未明確,登記於 CFG["rev_accel_periods"])。"""
    mon = available_months(rows, asof, cfg)
    if not mon:
        return cell("n", reason="沒有可得的月營收")
    t = max(mon)
    span = t - min(mon) + 1
    if len(mon) < cfg["rev_min_months"]:
        return cell("n", reason="月營收只有 %d 個月(需 ≥ %d)" % (len(mon), cfg["rev_min_months"]),
                    period="%s–%s" % (_mlabel(min(mon)), _mlabel(t)))
    y0 = yoy3(mon, t)
    acc = []
    for k in range(cfg["rev_accel_periods"]):
        a, b = yoy3(mon, t - k), yoy3(mon, t - k - 3)
        acc.append(None if a is None or b is None else a - b)
    if y0 is None or None in acc:
        return cell("n", reason="計算所需月份有缺(不插補)", period="%s–%s" % (_mlabel(min(mon)), _mlabel(t)))
    base = yoy3(mon, t - 12)
    low_base = base is not None and base < cfg["low_base_yoy3"]
    ev = dict(ev="事實", source=source, period="%s–%s" % (_mlabel(t - 2), _mlabel(t)),
              asof=month_avail(t // 12, t % 12 + 1, cfg).isoformat(),
              yoy3=round(y0, 4), yoy3_prev=round(yoy3(mon, t - 3), 4), accel=[round(a, 4) for a in acc],
              low_base=low_base if base is not None else None, base_yoy3=None if base is None else round(base, 4),
              note="併購、一次性大單可能造成加速假象,旗標只能提示")
    if y0 > 0 and all(a > 0 for a in acc):
        return cell("g", **ev)
    if y0 <= 0 or acc[0] < 0:
        return cell("y", **ev)
    return cell("flat", **ev)


# ------------------------------------------------------------------ 季資料
def available_quarters(rows, asof, cfg=CFG):
    """rows: [(year, quarter, {field: value})] → {q_key: dict}(只留 asof 可得的)"""
    return {_qkey(y, q): d for y, q, d in rows if quarter_avail(y, q, cfg) <= asof}


def cum_to_quarter(rows):
    """年度累計 → 單季:Q1 = 累計 Q1;Qn = 累計 Qn − 累計 Q(n−1)。同年前一季缺 → None(不插補)
    rows: [(year, quarter, value)] → {(year, quarter): value}"""
    cum = {(y, q): v for y, q, v in rows if v is not None}
    out = {}
    for (y, q), v in cum.items():
        if q == 1:
            out[(y, q)] = v
        elif (y, q - 1) in cum:
            out[(y, q)] = v - cum[(y, q - 1)]
        else:
            out[(y, q)] = None
    return out


def _ratio(a, b):
    return None if a is None or b is None or b == 0 else a / b


def q4_gross_margin(qrows, asof, industry=None, cfg=CFG, source="季財報(FinMind / MOPS)"):
    na = industry_na(industry, "q4", cfg)
    if na:
        return na
    qs = available_quarters(qrows, asof, cfg)
    if len(qs) < cfg["q_min_quarters"]:
        return cell("n", reason="季資料只有 %d 季(需 ≥ %d)" % (len(qs), cfg["q_min_quarters"]))
    t = max(qs)
    gm = {k: _ratio(qs[k].get("gross"), qs[k].get("revenue")) for k in qs}
    g = [gm.get(t - i) for i in range(3)]
    if None in g:
        return cell("n", reason="最近 3 季毛利率有缺(不插補)")
    ev = dict(ev="事實", source=source, period=_qlabel(t), asof=quarter_avail(t // 4, t % 4 + 1, cfg).isoformat(),
              gm=[round(x * 100, 2) for x in g], unit="%(由近到遠)")
    if g[0] > g[1] > g[2]:
        return cell("g", **ev)
    if (g[1] - g[0]) * 100 > cfg["gm_drop_pp"]:
        return cell("y", **ev)
    return cell("flat", **ev)


def q5_operating(qrows, asof, cfg=CFG, source="季財報(FinMind / MOPS)"):
    """● 最近一季營業利益年增率 > 營收年增率,且營益率高於去年同季;去年同季營業利益 ≤ 0 而今年 > 0 視為「營業利益成長較快」。
    ◐ 營收成長但營益率下降。"""
    qs = available_quarters(qrows, asof, cfg)
    if len(qs) < cfg["q_min_quarters"]:
        return cell("n", reason="季資料只有 %d 季(需 ≥ %d)" % (len(qs), cfg["q_min_quarters"]))
    t = max(qs)
    a, b = qs.get(t), qs.get(t - 4)
    if not a or not b or None in (a.get("revenue"), a.get("op"), b.get("revenue"), b.get("op")) or b["revenue"] <= 0:
        return cell("n", reason="缺去年同季資料(不插補)")
    rev_g = a["revenue"] / b["revenue"] - 1
    opm, opm0 = a["op"] / a["revenue"], b["op"] / b["revenue"]
    if b["op"] > 0:
        op_faster = a["op"] / b["op"] - 1 > rev_g
        op_g = round(a["op"] / b["op"] - 1, 4)
    else:
        op_faster = a["op"] > 0
        op_g = None
    ev = dict(ev="事實", source=source, period="%s vs %s" % (_qlabel(t), _qlabel(t - 4)),
              asof=quarter_avail(t // 4, t % 4 + 1, cfg).isoformat(), rev_yoy=round(rev_g, 4), op_yoy=op_g,
              opm=round(opm * 100, 2), opm_prev=round(opm0 * 100, 2))
    if op_faster and opm > opm0:
        return cell("g", **ev)
    if rev_g > 0 and opm < opm0:
        return cell("y", **ev)
    return cell("flat", **ev)


def q6_fcf(cf_rows, asof, eps_rows=(), industry=None, cfg=CFG, source="現金流量表(FinMind,年度累計換算單季)"):
    """cf_rows: [(year, quarter, ocf_cum, capex_cum)](年度累計;capex 取絕對值)
    eps_rows: [(year, quarter, eps_單季)]。FCF_ttm = 近 4 季營業現金流 − 近 4 季資本支出。"""
    na = industry_na(industry, "q6", cfg)
    if na:
        return na
    ok = [(y, q) for y, q, *_ in cf_rows if quarter_avail(y, q, cfg) <= asof]
    rows = [r for r in cf_rows if (r[0], r[1]) in set(ok)]
    ocf = cum_to_quarter([(y, q, o) for y, q, o, c in rows])
    cap = cum_to_quarter([(y, q, None if c is None else abs(c)) for y, q, o, c in rows])
    if not ocf:
        return cell("n", reason="現金流量表不可得")
    keys = sorted(_qkey(y, q) for y, q in ocf)
    t = keys[-1]

    def ttm(end):
        vs = []
        for k in range(end - 3, end + 1):
            yq = (k // 4, k % 4 + 1)
            o, c = ocf.get(yq), cap.get(yq)
            if o is None or c is None:
                return None
            vs.append(o - c)
        return sum(vs)
    f0, f1 = ttm(t), ttm(t - 4)
    if f0 is None or f1 is None:
        return cell("n", reason="計算近 4 季 / 去年同期 FCF 所需季度有缺(需 %d 季,不插補)" % cfg["fcf_quarters"])
    eps = {(y, q): e for y, q, e in eps_rows if quarter_avail(y, q, cfg) <= asof}
    eps_ttm = None
    if all((k // 4, k % 4 + 1) in eps for k in range(t - 3, t + 1)):
        eps_ttm = sum(eps[(k // 4, k % 4 + 1)] for k in range(t - 3, t + 1))
    ev = dict(ev="事實", source=source, period="%s–%s" % (_qlabel(t - 3), _qlabel(t)),
              asof=quarter_avail(t // 4, t % 4 + 1, cfg).isoformat(), fcf_ttm=f0, fcf_ttm_prev=f1, eps_ttm=eps_ttm)
    if f0 > 0 and f0 > f1:
        return cell("g", **ev)
    if eps_ttm is not None and eps_ttm > 0 and f0 < 0:
        return cell("y", note="EPS 為正但 FCF 為負,需查原因(應收帳款、資本支出、存貨)", **ev)
    return cell("flat", **ev)


# ------------------------------------------------------------------ Q2 EPS 預估上修(只接受手動證據)
def q2_eps_revision(estimates, asof):
    """estimates: [{"target_year", "date", "eps", "source"}](分析師預估,手動輸入)。
    只用同一個未來年度、至少兩個不同日期的預估比較;不得以實際 EPS 成長代替。"""
    est = [e for e in estimates or [] if e.get("date") and e.get("source") and e.get("eps") is not None
           and dt.date.fromisoformat(e["date"]) <= asof and int(e["target_year"]) >= asof.year]
    by_year = {}
    for e in est:
        by_year.setdefault(int(e["target_year"]), []).append(e)
    cand = [(y, sorted(v, key=lambda e: e["date"])) for y, v in by_year.items() if len({e["date"] for e in v}) >= 2]
    if not cand:
        return cell("n", reason="沒有同一未來年度、至少兩個日期的預估(無免費分析師共識來源,需手動填)")
    y, v = min(cand)
    first, last = v[0], v[-1]
    ev = dict(ev="分析師預估", target_year=y, first=[first["date"], first["eps"], first["source"]],
              last=[last["date"], last["eps"], last["source"]])
    if last["eps"] > first["eps"]:
        return cell("g", **ev)
    if last["eps"] < first["eps"]:
        return cell("y", **ev)
    return cell("flat", **ev)


def q3_orders(manual):
    """訂單只接受手動證據;缺取消條款或交付時程 → ◐。合約負債(預收款)不等於訂單,目前沒有來源。"""
    if not manual or not manual.get("content") or not manual.get("source") or not manual.get("date"):
        return cell("n", reason="台股多數不揭露訂單;合約負債目前沒有可用來源")
    ev = dict(ev=manual.get("ev") or "公司財測", source=manual["source"], asof=manual["date"], content=manual["content"])
    if not manual.get("cancel_terms") or not manual.get("delivery"):
        return cell("y", note="大訂單可能取消或延後(缺取消條款或交付時程)", **ev)
    return cell("g", **ev)


# ------------------------------------------------------------------ Q9 估值位階
def q9_pe_percentile(pe_rows, asof, cfg=CFG, source="個股日本益比(證交所 / 櫃買)"):
    """pe_rows: [(date, pe)];pe ≤ 0 或缺 → 虧損 / 不適用。百分位 = 近 3 年(可得期間)中 ≤ 最新值的比例。
    只顯示數字;≥ 80 百分位標 ◐,其他不上色(state "flat");不判定貴或便宜。"""
    start = asof - dt.timedelta(days=365 * cfg["pe_years"])
    rows = sorted((d, p) for d, p in pe_rows if start <= d <= asof)
    if not rows:
        return cell("n", reason="沒有本益比資料")
    d_last, p_last = rows[-1]
    if p_last is None or p_last <= 0:
        return cell("na", reason="虧損或本益比不適用", alt="股價淨值比、營收倍數")
    vals = [p for _, p in rows if p is not None and p > 0]
    if len(vals) < cfg["pe_min_days"]:
        return cell("n", reason="本益比歷史只有 %d 天(需 ≥ %d)" % (len(vals), cfg["pe_min_days"]))
    pct = 100.0 * sum(1 for v in vals if v <= p_last) / len(vals)
    ev = dict(ev="事實", source=source, asof=d_last.isoformat(), pe=p_last, pct=round(pct, 1),
              period="%s–%s" % (rows[0][0].isoformat(), d_last.isoformat()))
    return cell("y" if pct >= cfg["pe_hi_pct"] else "flat", **ev)


# ------------------------------------------------------------------ 大跌檢視
def drawdown_state(index_rows, cfg=CFG):
    """index_rows: [(date, close)](依日期)。由前往後走:
    未在大跌期間時,以近 60 日最高收盤為峰值,回落 ≥ 8% → 啟動(記住峰值日);期間內收盤回到峰值 97% 以上 → 結束。
    → {"active", "peak_date", "peak", "dd"}(以最後一天的狀態)"""
    rows = sorted(index_rows)
    active, peak_d, peak = False, None, None
    for i, (d, c) in enumerate(rows):
        if active:
            if c >= peak * cfg["dd_recover"]:
                active, peak_d, peak = False, None, None
            continue
        win = rows[max(0, i - cfg["dd_lookback"] + 1): i + 1]
        pd_, pk = max(win, key=lambda x: x[1])
        if c <= pk * (1 - cfg["dd_trigger"]):
            active, peak_d, peak = True, pd_, pk
    last = rows[-1][1] if rows else None
    return {"active": active, "peak_date": peak_d, "peak": peak,
            "dd": None if not active else round(last / peak - 1, 4)}


def relative_strength(stock_rows, index_rows, since, cfg=CFG):
    """自 since(指數峰值日)起:個股報酬 − 指數報酬(百分點)。起點或終點缺 → ○"""
    s = {d: v for d, v in stock_rows if v}          # 收盤價 0 / 缺(停牌、無成交)視為沒有資料
    i = {d: v for d, v in index_rows if v}
    if since not in s or since not in i:
        return cell("n", reason="起點當天沒有收盤價(停牌或無成交)")
    end = max(d for d in s if d in i)
    rs = (s[end] / s[since] - 1 - (i[end] / i[since] - 1)) * 100
    ev = dict(ev="事實", source="收盤價(證交所 / 櫃買)", period="%s–%s" % (since.isoformat(), end.isoformat()),
              stock_ret=round((s[end] / s[since] - 1) * 100, 2), index_ret=round((i[end] / i[since] - 1) * 100, 2),
              rs=round(rs, 2))
    if rs >= cfg["rs_pp"]:
        return cell("g", **ev)
    if rs <= -cfg["rs_pp"]:
        return cell("y", **ev)
    return cell("flat", **ev)


def crash_days(index_rows, cfg=CFG):
    """近 1 年單日報酬最低 5% 且 ≤ −1.5% 的日子(只列日期與報酬)"""
    rows = sorted(index_rows)
    rets = [(rows[k][0], rows[k][1] / rows[k - 1][1] - 1) for k in range(1, len(rows))]
    rets = rets[-cfg["high_window"]:]
    if len(rets) < 20:
        return []
    srt = sorted(r for _, r in rets)
    cut = srt[max(0, int(len(srt) * cfg["crash_pct"] / 100.0) - 1)]
    return [(d, round(r, 4)) for d, r in rets if r <= cut and r <= cfg["crash_max_ret"]]


def stock_drawdown(close_rows, cfg=CFG):
    rows = [(d, c) for d, c in sorted(close_rows) if c][-cfg["high_window"]:]
    if not rows:
        return None
    hi = max(c for _, c in rows)
    return rows[-1][1] / hi - 1


def four_box(q1, q4, q5, pe, stock_dd, manual_box4=None, cfg=CFG):
    """機械判定,只在大跌期間使用。以已公布的營收與利潤率代替 EPS 預估(頁面需註明 CFG["box_note"])。
    Q1、Q4、Q5 任一為 ○ → 無法歸類(資料不足時不當成「沒變」)。④ 只接受手動標記(需理由)。"""
    if manual_box4 and manual_box4.get("reason"):
        return {"box": 4, "manual": True, "reason": manual_box4["reason"], "note": cfg["box_note"]}
    if stock_dd is None or stock_dd > -cfg["stock_dd"]:
        return {"box": None, "reason": "個股自 52 週高點回落未達 %d%%" % int(cfg["stock_dd"] * 100), "note": cfg["box_note"]}
    st = [x["state"] for x in (q1, q4, q5)]
    if "n" in st:
        return {"box": None, "reason": "無法歸類(營收或利潤率資料不足)", "note": cfg["box_note"]}
    if q1["state"] == "y" and (q4["state"] == "y" or q5["state"] == "y"):
        return {"box": 2, "note": cfg["box_note"]}
    if "y" not in st:
        if pe and pe.get("pct") is not None and pe["pct"] >= cfg["pe_hi_pct"]:
            return {"box": 3, "note": cfg["box_note"]}
        return {"box": 1, "note": cfg["box_note"]}
    return {"box": None, "reason": "無法歸類", "note": cfg["box_note"]}


# ------------------------------------------------------------------ 三層分類
def tier(q1, q4, q5, theme_evidence, q2=None, override=None):
    """→ {"tier", "mech_tier", "override_reason"}。手動覆寫必須有理由,並保留機械分層。"""
    st = [q1["state"], q4["state"], q5["state"]]
    if sum(1 for s in st if s == "n") >= 2:
        mech = "資料不足"
    elif all(s == "g" for s in st) and (q2 is None or q2["state"] in ("n", "g")):   # Q2 有手動證據時也須 ●
        mech = "已證明"
    elif theme_evidence and any(s == "g" for s in st):
        mech = "開始出現"
    elif theme_evidence:
        mech = "只有故事"
    else:
        mech = "無題材證據"
    out = {"tier": mech, "mech_tier": mech, "override_reason": None}
    if override and override.get("tier") in TIERS and override.get("reason"):
        out.update(tier=override["tier"], override_reason=override["reason"], manual=True)
    return out


# ------------------------------------------------------------------ 認錯條件(append-only)
def check_append_only(old_rows, new_rows):
    """old_rows / new_rows: [dict](含 code, version, created, …)。舊列必須原封不動存在於新列,只能新增。
    → (ok, problems)"""
    key = lambda r: (str(r.get("code")), int(r.get("version")), int(r.get("idx", 0)))
    new = {key(r): r for r in new_rows}
    probs = []
    for r in old_rows:
        k = key(r)
        if k not in new:
            probs.append("刪除了舊條件 %s v%s #%s" % k)
        elif new[k] != r:
            probs.append("修改了舊條件 %s v%s #%s" % k)
    return not probs, probs


def current_conditions(rows, code):
    """該檔最新版本的條件(最多 3 條),並附所有舊版本供查詢"""
    mine = [r for r in rows if str(r.get("code")) == str(code)]
    if not mine:
        return {"current": [], "history": []}
    v = max(int(r["version"]) for r in mine)
    cur = sorted([r for r in mine if int(r["version"]) == v], key=lambda r: int(r.get("idx", 0)))[:3]
    hist = sorted([r for r in mine if int(r["version"]) < v], key=lambda r: (int(r["version"]), int(r.get("idx", 0))))
    return {"current": cur, "history": hist}


def evaluate_condition(cond, rev_rows, qrows, pe_rows, asof, cfg=CFG):
    """可自動判斷的條件類型(cond["metric"]):
      rev_yoy_neg  : 月營收年增率連續 n 個月 < 0(n = threshold)
      gm_down      : 毛利率連續 n 季下降
      opm_down     : 營益率連續 n 季下降
      pe_pct_above : 本益比百分位 ≥ threshold
    其他 → 手動確認。→ {"status": "triggered"/"not_triggered"/"insufficient"/"manual", "detail"}"""
    m, th = cond.get("metric"), cond.get("threshold")
    if m == "rev_yoy_neg":
        mon = available_months(rev_rows, asof, cfg)
        if not mon:
            return {"status": "insufficient"}
        t = max(mon)
        ys = []
        for k in range(int(th)):
            a, b = mon.get(t - k), mon.get(t - k - 12)
            if a is None or b is None or b <= 0:
                return {"status": "insufficient"}
            ys.append(a / b - 1)
        return {"status": "triggered" if all(y < 0 for y in ys) else "not_triggered", "detail": [round(y, 4) for y in ys]}
    if m in ("gm_down", "opm_down"):
        qs = available_quarters(qrows, asof, cfg)
        if not qs:
            return {"status": "insufficient"}
        t = max(qs)
        f = "gross" if m == "gm_down" else "op"
        ms = []
        for k in range(int(th) + 1):
            d = qs.get(t - k)
            r = _ratio(d.get(f), d.get("revenue")) if d else None
            if r is None:
                return {"status": "insufficient"}
            ms.append(r)
        down = all(ms[k] < ms[k + 1] for k in range(int(th)))
        return {"status": "triggered" if down else "not_triggered", "detail": [round(x * 100, 2) for x in ms]}
    if m == "pe_pct_above":
        c = q9_pe_percentile(pe_rows, asof, cfg)
        if c.get("pct") is None:
            return {"status": "insufficient"}
        return {"status": "triggered" if c["pct"] >= float(th) else "not_triggered", "detail": c["pct"]}
    return {"status": "manual"}


def compare_snapshots(prev, cur, items=("q1", "q2", "q3", "q4", "q5", "q6", "q9")):
    """與上次檢視比較:● 為好、◐ 為差;○ / 持平 / — 為不確定。→ {"better", "worse", "uncertain", "changes"}"""
    rank = {"g": 2, "flat": 1, "y": 0}
    out = {"better": 0, "worse": 0, "uncertain": 0, "changes": []}
    for k in items:
        a, b = (prev or {}).get(k, {}).get("state"), (cur or {}).get(k, {}).get("state")
        if a != b:
            out["changes"].append([k, a, b])
        if b not in rank:
            out["uncertain"] += 1
        elif a != b:
            ra = rank.get(a, 1)                     # 先前 ○ / — 視同中性
            if rank[b] > ra:
                out["better"] += 1
            elif rank[b] < ra:
                out["worse"] += 1
    return out
