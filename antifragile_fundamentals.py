# -*- coding: utf-8 -*-
"""
antifragile_fundamentals.py — 反脆弱選股模組:脆弱旗標 + 反脆弱體質分數(2026-11-23)v2026-11-23-init
============================================================
規格(使用者 2026-11-23「Quietfolio 反脆弱選股模組 — 實作規格」)§3、§4、§7。
讀 af_data/(af_fin_data.py、af_daily.py 累積)+ bt_disloc/universe.csv.gz → 寫 antifragile_fundamentals.json(repo 根目錄,
沿用既有 JSON 都放根目錄的慣例;規格寫 data/,以現有架構為準)。
GAS Antifragile.js 每日讀這份 JSON,加上當日估值 / 52 週高點 / 融資使用率算「進場凸性」與最終標籤。

資料處理原則(規格 §3、§4.2、§9.5):缺資料不推估、不補零,全部進 missing_fields;
唯一例外:資產負債表當季存在、但沒有列示某項借款 → 該借款視為 0(報表沒有這一行代表沒有這筆借款),記在 notes。
"""
import csv
import datetime as dt
import gzip
import json
import os
import re
import statistics
import sys

VERSION = "v2026-11-23-init"
AF_DIR = "af_data"
UNIVERSE = os.path.join("bt_disloc", "universe.csv.gz")
OUT = "antifragile_fundamentals.json"

AF_CONFIG = {
    # §3 脆弱旗標門檻
    "HIGH_DEBT": 60.0,            # 負債比 %
    "HIGH_PLEDGE": 30.0,          # 董監設質比率 %
    "LOW_COVERAGE": 3.0,          # 利息保障倍數
    "CUSTOMER_CONC": 30.0,        # 最大客戶營收占比 %(手動,GAS AF_Manual)
    "INSIDER_EXIT": 5.0,          # 近 60 日董監申報轉讓 / 董監持股 %
    "INSIDER_EXIT_DAYS": 60,
    "EXIT_EXCLUDE_METHODS": ("贈與", "信託"),   # 不是市場出脫
    # §4 體質權重
    "W": {"net_cash": 25, "fcf": 15, "insider": 20, "resilience": 15, "gm_stability": 15, "lindy": 10},
    "INSIDER_SUB": {"holding": 0.6, "change": 0.2, "buyback": 0.2},
    "RESILIENCE_SUB": {"worst_yoy": 0.5, "no_loss": 0.5},
    "INSIDER_CHANGE_PP": 0.5,
    "FCF_QUARTERS": 8, "GM_QUARTERS": 12, "YEARS": 5, "LINDY_CAP": 40,
    "MIN_COVERAGE": 0.7,
    # §5.2 估值歷史(週取樣):≥ 50 週且跨度 ≥ 1 年 ≈ 250 個交易日
    "VAL_MIN_POINTS": 50, "VAL_MIN_SPAN_DAYS": 360, "VAL_YEARS": 5,
}
CF_OCF = ("CashFlowsFromOperatingActivities", "NetCashInflowFromOperatingActivities")
CF_CAPEX = ("PropertyAndPlantAndEquipment", "AcquisitionOfPropertyPlantAndEquipment",
            "PaymentsToAcquirePropertyPlantAndEquipment", "PurchaseOfPropertyPlantAndEquipment")
FIN_IND = re.compile(r"金融|保險")


def rd(path):
    if not os.path.exists(path):
        return []
    op = gzip.open if path.endswith(".gz") else open
    with op(path, "rt", encoding="utf-8") as f:
        r = csv.reader(f)
        next(r, None)
        return list(r)


def fnum(x):
    try:
        return float(x) if x not in ("", None) else None
    except ValueError:
        return None


def long_by_code(rows):
    """[[code,date,type,value]] → {code: {date: {type: value}}}"""
    out = {}
    for c, d, t, v in rows:
        v = fnum(v)
        if v is not None:
            out.setdefault(c, {}).setdefault(d, {})[t] = v
    return out


def quarter_of(d):
    return {"03": 1, "06": 2, "09": 3, "12": 4}.get(d[5:7])


def discrete(series):
    """[(date, ytd)] 年初至今累計 → [(date, 單季)](與 QualityGrowth.js toDiscreteQuarterCapex_ 同法)"""
    out = []
    for i, (d, v) in enumerate(series):
        q = quarter_of(d)
        if v is None or q is None:
            out.append((d, None))
            continue
        if q == 1:
            out.append((d, v))
            continue
        prev = None
        for j in range(i - 1, -1, -1):
            if series[j][0][:4] != d[:4]:
                break
            if quarter_of(series[j][0]) == q - 1:
                prev = series[j][1]
                break
        out.append((d, None if prev is None else v - prev))
    return out


def consecutive_tail(series, n):
    """最近 n 個連續季度的值(缺任一季或值為 None → None)"""
    if len(series) < n:
        return None
    tail = series[-n:]
    if any(v is None for _, v in tail):
        return None
    for (a, _), (b, _) in zip(tail, tail[1:]):
        ya, qa, yb, qb = int(a[:4]), quarter_of(a), int(b[:4]), quarter_of(b)
        if qa is None or qb is None or (yb * 4 + qb) - (ya * 4 + qa) != 1:
            return None
    return [v for _, v in tail]


def pctile_map(values):
    """{code: v} → {code: 0..1}(平均名次 / n,越大越高)"""
    items = [(c, v) for c, v in values.items() if v is not None]
    if not items:
        return {}
    items.sort(key=lambda x: x[1])
    n, out, i = len(items), {}, 0
    while i < n:
        j = i
        while j + 1 < n and items[j + 1][1] == items[i][1]:
            j += 1
        p = ((i + j) / 2 + 1) / n
        for k in range(i, j + 1):
            out[items[k][0]] = p
        i = j + 1
    return out


def quantiles(vals, steps=20):
    s = sorted(vals)
    n = len(s)
    out = []
    for k in range(steps + 1):
        pos = k / steps * (n - 1)
        lo = int(pos)
        hi = min(lo + 1, n - 1)
        out.append(round(s[lo] + (s[hi] - s[lo]) * (pos - lo), 4))
    return out


def add_months(ym, k):
    y, m = int(ym[:4]), int(ym[5:7]) + k
    while m <= 0:
        m += 12
        y -= 1
    while m > 12:
        m -= 12
        y += 1
    return f"{y:04d}-{m:02d}"


# ============================================================
# 單檔基本面
# ============================================================
def stock_metrics(c, bs, inc, cf, notes_seen):
    m, miss = {}, []
    # 資產負債(最新一季)
    b = bs.get(c) or {}
    bdates = sorted(d for d, v in b.items() if v.get("TotalAssets"))
    if bdates:
        v = b[bdates[-1]]
        ta = v["TotalAssets"]
        m["bs_date"] = bdates[-1]
        m["debt_ratio"] = round(v["Liabilities"] / ta * 100, 2) if v.get("Liabilities") is not None else None
        st = sum(x for t, x in v.items() if re.search(r"short", t, re.I) and re.search(r"Borrowing|BillsPayable", t))
        lt = sum(x for t, x in v.items() if re.search(r"long", t, re.I) and re.search(r"Borrowing", t)) + \
            v.get("LongtermLiabilitiesCurrentPortion", 0)
        cash = v.get("CashAndCashEquivalents")
        m["short_borrow"], m["long_borrow"], m["cash"], m["total_assets"] = st, lt, cash, ta
        if cash is not None:
            m["net_cash_ratio"] = round((cash - st - lt) / ta * 100, 2)
            m["liquidity_gap"] = st > cash
        notes_seen.add("borrow_zero")
    for k in ("debt_ratio", "net_cash_ratio", "liquidity_gap"):
        if m.get(k) is None:
            miss.append(k)
    # 損益(單季)
    q = inc.get(c) or {}
    qd = sorted(q)
    op = consecutive_tail([(d, q[d].get("OperatingIncome")) for d in qd], 4)
    eps = consecutive_tail([(d, q[d].get("EPS")) for d in qd], 4)
    m["eps_ttm"] = round(sum(eps), 2) if eps else None
    gm = consecutive_tail([(d, (q[d]["GrossProfit"] / q[d]["Revenue"] * 100) if q[d].get("Revenue") and q[d].get("GrossProfit") is not None else None)
                           for d in qd], AF_CONFIG["GM_QUARTERS"])
    m["gm_std_12q"] = round(statistics.pstdev(gm), 3) if gm else None
    # 年度營收 / 虧損年數(完整 4 季的年度)
    years = {}
    for d in qd:
        years.setdefault(int(d[:4]), []).append(q[d])
    full = {y: v for y, v in years.items() if len(v) == 4}
    yrs = sorted(full)
    Y = AF_CONFIG["YEARS"]
    if len(yrs) >= Y + 1 and yrs[-(Y + 1):] == list(range(yrs[-1] - Y, yrs[-1] + 1)):
        rev = {y: sum(x.get("Revenue") or 0 for x in full[y]) if all(x.get("Revenue") is not None for x in full[y]) else None for y in yrs}
        yoys = [(rev[y] - rev[y - 1]) / rev[y - 1] * 100 for y in yrs[-Y:] if rev.get(y) is not None and rev.get(y - 1)]
        m["worst_rev_yoy_5y"] = round(min(yoys), 2) if len(yoys) == Y else None
        ni = [sum(x.get("IncomeAfterTaxes", x.get("EPS", 0)) for x in full[y]) if all(
            ("IncomeAfterTaxes" in x) or ("EPS" in x) for x in full[y]) else None for y in yrs[-Y:]]
        m["loss_years_5y"] = sum(1 for v in ni if v < 0) if all(v is not None for v in ni) else None
    else:
        m["worst_rev_yoy_5y"] = m["loss_years_5y"] = None
    # 現金流(年初至今累計 → 單季)
    f = cf.get(c) or {}
    fd = sorted(f)
    ocf_ytd = [(d, next((f[d][k] for k in CF_OCF if k in f[d]), None)) for d in fd]
    capex_ytd = [(d, next((f[d][k] for k in CF_CAPEX if k in f[d]), None)) for d in fd]
    int_ytd = [(d, f[d].get("InterestExpense", 0.0 if f[d] else None)) for d in fd]   # 現金流量表有、但沒列利息 → 0
    ocf_q, capex_q, int_q = discrete(ocf_ytd), discrete(capex_ytd), discrete(int_ytd)
    ocf4 = consecutive_tail(ocf_q, 4)
    m["ocf_ttm"] = round(sum(ocf4)) if ocf4 else None
    int4 = consecutive_tail(int_q, 4)
    if op and int4 is not None:
        interest = sum(abs(x) for x in int4)
        m["interest_coverage"] = None if interest == 0 else round(sum(op) / interest, 2)
        m["interest_zero"] = interest == 0
    fcf = consecutive_tail([(d, (o - abs(x)) if o is not None and x is not None else None)
                            for (d, o), (_, x) in zip(ocf_q, capex_q)], AF_CONFIG["FCF_QUARTERS"])
    m["fcf_positive_quarters"] = sum(1 for v in fcf if v > 0) if fcf else None
    for k in ("eps_ttm", "gm_std_12q", "worst_rev_yoy_5y", "loss_years_5y", "ocf_ttm", "fcf_positive_quarters"):
        if m.get(k) is None:
            miss.append(k)
    if m.get("interest_coverage") is None and not m.get("interest_zero"):
        miss.append("interest_coverage")
    return m, miss


def insider_metrics(c, ins, issued, transfers, buybacks, meta, today):
    m, miss = {}, []
    rows = ins.get(c) or {}
    if rows:
        ym = max(rows)
        sh, pl = rows[ym]
        m["insider_ym"] = ym
        m["pledge_ratio"] = round(pl / sh * 100, 2) if sh > 0 else None
        m["insider_holding"] = round(sh / issued * 100, 3) if issued and sh else None
        prev = rows.get(add_months(ym, -12))
        if prev and prev[0] and issued:
            ch = (sh - prev[0]) / issued * 100
            pp = AF_CONFIG["INSIDER_CHANGE_PP"]
            m["insider_change"] = round(ch, 3)
            m["insider_change_dir"] = 1.0 if ch > pp else 0.0 if ch < -pp else 0.5
        # 近 60 日董監轉讓
        cut = (today - dt.timedelta(days=AF_CONFIG["INSIDER_EXIT_DAYS"])).isoformat()
        sold = sum(s for d, role, method, s in transfers.get(c, [])
                   if d >= cut and re.search(r"董事|監察人", role) and not any(x in method for x in AF_CONFIG["EXIT_EXCLUDE_METHODS"]))
        since = meta.get("insider_transfer_since")
        ratio = sold / sh * 100 if sh > 0 else None
        if ratio is not None and (ratio > AF_CONFIG["INSIDER_EXIT"] or (since and since <= cut)):
            m["insider_exit_pct"] = round(ratio, 2)
    # 近 12 個月庫藏股:有公告 → 1;沒有公告且累積已滿 12 個月 → 0;否則未知
    cut12 = (today - dt.timedelta(days=365)).isoformat()
    if any(d >= cut12 for d in buybacks.get(c, [])):
        m["buyback_12m"] = 1
    elif meta.get("buyback_since") and meta["buyback_since"] <= cut12:
        m["buyback_12m"] = 0
    for k in ("pledge_ratio", "insider_holding", "insider_change", "buyback_12m", "insider_exit_pct"):
        if m.get(k) is None:
            miss.append(k)
    return m, miss


def valuation_quantiles(rows, today):
    """週取樣估值 → 近 5 年的 21 個分位點(PE 只取 > 0);不足 → None"""
    cut = (today - dt.timedelta(days=365 * AF_CONFIG["VAL_YEARS"])).isoformat()
    rows = [r for r in rows if r[1] >= cut]
    out = {}
    for key, idx, pos in (("pe", 2, True), ("pb", 3, True), ("dy", 4, False)):
        pts = [(r[1], fnum(r[idx])) for r in rows]
        pts = [(d, v) for d, v in pts if v is not None and (v > 0 if pos else v >= 0)]
        if len(pts) >= AF_CONFIG["VAL_MIN_POINTS"] and \
                (dt.date.fromisoformat(pts[-1][0]) - dt.date.fromisoformat(pts[0][0])).days >= AF_CONFIG["VAL_MIN_SPAN_DAYS"]:
            out[key] = quantiles([v for _, v in pts])
            out[key + "_n"] = len(pts)
        else:
            out[key] = None
    return out


# ============================================================
# 主流程
# ============================================================
def build(base=".", today=None):
    today = today or (dt.datetime.utcnow() + dt.timedelta(hours=8)).date()
    A = os.path.join(base, AF_DIR)
    uni = {r[0]: {"name": r[1], "market": r[2], "industry": r[3]} for r in rd(os.path.join(base, UNIVERSE))}
    comp = {r[0]: r for r in rd(os.path.join(A, "company.csv.gz"))}
    bs, inc, cf = (long_by_code(rd(os.path.join(A, k + ".csv.gz"))) for k in ("bs", "is", "cf"))
    per = {}
    wk = {}
    for name in ("per.csv.gz", "per_daily.csv.gz"):      # FinMind 回補 + 每日官方累積(同一週以每日為準)
        for r in rd(os.path.join(A, name)):
            y, w, _ = dt.date.fromisoformat(r[1]).isocalendar()
            wk[(r[0], y, w)] = r
    for k in sorted(wk):
        per.setdefault(k[0], []).append(wk[k])
    ins = {}
    for ym, c, sh, pl, _n in rd(os.path.join(A, "insider_monthly.csv.gz")):
        ins.setdefault(c, {})[ym] = (fnum(sh) or 0.0, fnum(pl) or 0.0)
    transfers = {}
    for d, c, role, name, method, sh in rd(os.path.join(A, "insider_transfer.csv.gz")):
        transfers.setdefault(c, []).append((d, role, method, fnum(sh) or 0.0))
    buybacks = {}
    for d, c, _t in rd(os.path.join(A, "buyback.csv.gz")):
        buybacks.setdefault(c, []).append(d)
    try:
        meta = json.load(open(os.path.join(A, "daily_meta.json"), encoding="utf-8"))
    except (OSError, ValueError):
        meta = {}

    notes_seen, stocks = set(), {}
    for c, u in sorted(uni.items()):
        cr = comp.get(c)
        fin = bool(FIN_IND.search(u["industry"] or "")) or (cr is not None and cr[4] == "17")
        s = {"name": u["name"], "industry": u["industry"], "market": u["market"], "financial": fin}
        if fin:
            s["label"] = "na"
            stocks[c] = s
            continue
        m, miss = stock_metrics(c, bs, inc, cf, notes_seen)
        issued = fnum(cr[5]) if cr else None
        im, imiss = insider_metrics(c, ins, issued, transfers, buybacks, meta, today)
        m.update(im)
        miss += imiss
        if cr and cr[2]:
            age = (today - dt.date.fromisoformat(cr[2])).days / 365.25
            m["company_age"] = round(age, 1)
        else:
            miss.append("company_age")
        s["metrics"], s["missing_fields"] = m, sorted(set(miss))
        s["valuation_q"] = valuation_quantiles(per.get(c, []), today)
        for k in ("pe", "pb", "dy"):
            if s["valuation_q"].get(k) is None:
                s["missing_fields"].append(k + "_history")
        stocks[c] = s

    # 脆弱旗標(§3)
    C = AF_CONFIG
    for c, s in stocks.items():
        if s.get("financial"):
            continue
        m, r = s["metrics"], []
        if m.get("debt_ratio") is not None and m["debt_ratio"] > C["HIGH_DEBT"]:
            r.append("HIGH_DEBT")
        if m.get("pledge_ratio") is not None and m["pledge_ratio"] > C["HIGH_PLEDGE"]:
            r.append("HIGH_PLEDGE")
        if m.get("interest_coverage") is not None and not m.get("interest_zero") and m["interest_coverage"] < C["LOW_COVERAGE"]:
            r.append("LOW_COVERAGE")
        if m.get("ocf_ttm") is not None and m["ocf_ttm"] < 0:
            r.append("NEG_OCF")
        if m.get("liquidity_gap") is True:
            r.append("LIQUIDITY_GAP")
        if m.get("insider_exit_pct") is not None and m["insider_exit_pct"] > C["INSIDER_EXIT"]:
            r.append("INSIDER_EXIT")
        s["fragile_reasons"], s["fragile"] = r, bool(r)

    # 橫斷面百分位(排除金融業)
    live = {c: s for c, s in stocks.items() if not s.get("financial")}
    p_net = pctile_map({c: s["metrics"].get("net_cash_ratio") for c, s in live.items()})
    p_hold = pctile_map({c: s["metrics"].get("insider_holding") for c, s in live.items()})
    p_worst = pctile_map({c: s["metrics"].get("worst_rev_yoy_5y") for c, s in live.items()})
    p_gm = pctile_map({c: s["metrics"].get("gm_std_12q") for c, s in live.items()})
    fcf_avail = sum(1 for s in live.values() if s["metrics"].get("fcf_positive_quarters") is not None)
    fcf_off = fcf_avail < 0.1 * max(1, len(live))
    W = dict(C["W"])
    if fcf_off:
        W.pop("fcf")

    for c, s in live.items():
        m = s["metrics"]
        sub = {}   # 子項 → (值 0..1, 實際計入的權重比例 0..1)
        if c in p_net:
            sub["net_cash"] = (p_net[c], 1.0)
        if "fcf" in W and m.get("fcf_positive_quarters") is not None:
            sub["fcf"] = (m["fcf_positive_quarters"] / C["FCF_QUARTERS"], 1.0)
        comp_ = [(C["INSIDER_SUB"]["holding"], p_hold.get(c)), (C["INSIDER_SUB"]["change"], m.get("insider_change_dir")),
                 (C["INSIDER_SUB"]["buyback"], m.get("buyback_12m"))]
        wsum = sum(w for w, v in comp_ if v is not None)
        if c in p_hold:          # 持股率是切身利害的主體;沒有持股率就整項不計
            sub["insider"] = (sum(w * v for w, v in comp_ if v is not None) / wsum, wsum)
        comp_ = [(C["RESILIENCE_SUB"]["worst_yoy"], p_worst.get(c)),
                 (C["RESILIENCE_SUB"]["no_loss"], None if m.get("loss_years_5y") is None else 1 - m["loss_years_5y"] / C["YEARS"])]
        wsum = sum(w for w, v in comp_ if v is not None)
        if wsum:
            sub["resilience"] = (sum(w * v for w, v in comp_ if v is not None) / wsum, wsum)
        if c in p_gm:
            sub["gm_stability"] = (1 - p_gm[c], 1.0)
        if m.get("company_age") is not None:
            sub["lindy"] = (min(m["company_age"], C["LINDY_CAP"]) / C["LINDY_CAP"], 1.0)
        eff = {k: W[k] * f for k, (v, f) in sub.items() if k in W}
        tot = sum(eff.values())
        s["data_coverage"] = round(tot / sum(W.values()), 3)
        s["af_score"] = None if s["fragile"] or not tot else round(sum(eff[k] * sub[k][0] for k in eff) / tot * 100, 1)
        s["af_parts"] = {k: round(v * 100, 1) for k, (v, _) in sub.items()}
        m["net_cash_pct"] = round(p_net[c] * 100, 1) if c in p_net else None

    notes = []
    if fcf_off:
        notes.append("本版未含自由現金流")
    if "borrow_zero" in notes_seen:
        notes.append("資產負債表當季沒有列示的借款科目視為 0")
    for k, lab, days in (("insider_transfer_since", "董監轉讓申報", C["INSIDER_EXIT_DAYS"]), ("buyback_since", "庫藏股公告", 365)):
        since = meta.get(k)
        if not since or since > (today - dt.timedelta(days=days)).isoformat():
            notes.append(f"{lab}自 {since or '尚未'} 起累積,未滿 {days} 天,沒有紀錄者列為缺漏")
    if not any(len(v) > 12 for v in ins.values()):
        notes.append("董監持股月表累積未滿 12 個月,持股變化方向列為缺漏")

    summ = {"stocks": len(stocks), "financial": sum(1 for s in stocks.values() if s.get("financial")),
            "scored": sum(1 for s in live.values() if s.get("af_score") is not None),
            "fragile": sum(1 for s in live.values() if s.get("fragile")),
            "coverage_ge_0_7": sum(1 for s in live.values() if s.get("data_coverage", 0) >= C["MIN_COVERAGE"])}
    return {"version": VERSION, "updated_at": dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(timespec="seconds"),
            "config": C, "weights_used": W, "notes": notes, "summary": summ,
            "accumulation": {k: meta.get(k) for k in ("insider_monthly_since", "insider_transfer_since", "buyback_since")},
            "stocks": stocks}


def main():
    out = build()
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    print(VERSION, json.dumps(out["summary"], ensure_ascii=False), out["notes"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
