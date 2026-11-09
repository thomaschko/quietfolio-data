# -*- coding: utf-8 -*-
"""
theme_page_build.py — 題材頁面(趨勢檢視版)Phase 2:抓資料、套規則、輸出 JSON(2026-11-09)
============================================================
只「讀取」既有輸出與資料源,組合成:
  theme_page/index.json        題材清單(第一層)
  theme_page/{theme_id}.json   各題材詳細(第二、三層;個股多時分頁 {id}_p2.json …)
每個檔 ≤ 45,000 字元;失敗保留舊檔;來源不存在標 not_built。

資料來源(Phase 0 探測結論):
  月營收、季損益、現金流量、本益比、收盤價:FinMind(Secret FINMIND_TOKEN;第三方,整理自官方)
  最新一季損益補充:MOPS opendata t187ap06_L_ci / _O_ci(官方,只有最新一季;單位仟元 → 用月營收合計核對後 ×1000)
  合約負債:FinMind 與官方開放資料都沒有 → 第 3 題只接受手動證據
  歷史季報彙總頁(mopsov t163sb05 / t164sb05):robots.txt 不允許,不使用
快取 theme_page/cache/{code}.json:月營收 / 季報只在「應該有新一期」時重抓;本益比、收盤價每天只補新日期。
FinMind 額度:每次執行最多 CFG["finmind_budget"] 次;402(超量)→ 停止抓取,用快取繼續。
判定規則全部在 theme_checks_core.py(THEME_PAGE_CFG);本程式只組裝。全部是研究排序用的描述,不是買賣訊號。
"""
import csv
import datetime as dt
import io
import json
import os
import sys
import time

import requests

import theme_checks_core as T

BUILD_CFG = {
    "finmind_budget": 400, "finmind_gap": 1.0, "years_back": 3,
    "refetch_quarter_days": 7,          # 應有新季報但 FinMind 還沒有 → 最多每 7 天重抓一次
    "size_limit": 45000, "page_size": 18,
    "mops_unit_check": (0.9, 1.1),      # MOPS 營收 ×1000 ÷ 同季月營收合計 必須落在此區間才採用
    "pos_red": 0.10, "pos_yellow": 0.25,
}
OUT_DIR = "theme_page"
CACHE_DIR = os.path.join(OUT_DIR, "cache")
FINMIND = "https://api.finmindtrade.com/api/v4/data"
MOPS_FIN = ["https://mopsfin.twse.com.tw/opendata/t187ap06_L_ci.csv", "https://mopsfin.twse.com.tw/opendata/t187ap06_O_ci.csv"]
TPE = dt.timezone(dt.timedelta(hours=8))


class Quota(Exception):
    pass


class FinMind:
    def __init__(self, token, budget, session=None, sleep=time.sleep):
        self.token, self.budget, self.used = token, budget, 0
        self.s = session or requests.Session()
        self.sleep = sleep
        self.stopped = None

    def get(self, dataset, data_id, start):
        if self.stopped:
            raise Quota(self.stopped)
        if self.used >= self.budget:
            self.stopped = "本次額度 %d 次用完" % self.budget
            raise Quota(self.stopped)
        if self.used:
            self.sleep(BUILD_CFG["finmind_gap"])
        self.used += 1
        r = self.s.get(FINMIND, params={"dataset": dataset, "data_id": data_id, "start_date": start},
                       headers={"Authorization": "Bearer " + self.token}, timeout=60)
        if r.status_code == 402:
            self.stopped = "FinMind 超過每小時額度(402)"
            raise Quota(self.stopped)
        if r.status_code != 200:
            return None
        j = r.json()
        return j.get("data") if j.get("status") == 200 else None


# ------------------------------------------------------------------ 讀檔
def _read(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write(path, obj):
    s = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(s)
    os.replace(tmp, path)
    return len(s)


def theme_stocks(theme, base="."):
    """→ [(code, 來源說明)](依序去重)"""
    cov = (_read(os.path.join(base, "margin_share_themes.json"), {}) or {}).get("themes") or {}
    bas = (_read(os.path.join(base, "intl_theme_baskets.json"), {}) or {}).get("themes") or {}
    out, seen = [], set()
    for name in theme.get("coverage") or []:
        for c in cov.get(name) or []:
            if c not in seen:
                seen.add(c)
                out.append((c, "My-TW-Coverage " + name))
    for name in theme.get("baskets") or []:
        for c in (bas.get(name) or {}).get("tw") or []:
            if c not in seen:
                seen.add(c)
                out.append((c, "題材籃子 " + name))
    for c in theme.get("extra") or []:
        if c not in seen:
            seen.add(c)
            out.append((c, "手動加入"))
    return out


def lookup_meta(base="."):
    names, ind = {}, {}
    for c, s in ((_read(os.path.join(base, "chip_structure_full.json"), {}) or {}).get("stocks") or {}).items():
        names[c] = s.get("n") or ""
    cls = _read(os.path.join(base, "margin_share_classes.json"), {}) or {}
    for mk in ("twse", "tpex"):
        ind.update(((cls.get(mk) or {}).get("industry")) or {})
    return names, ind


# ------------------------------------------------------------------ 快取與抓取
def latest_available_month(asof):
    y, m = asof.year, asof.month
    for _ in range(3):
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
        if T.month_avail(y, m) <= asof:
            return (y, m)
    return None


def latest_available_quarter(asof):
    y, q = asof.year, (asof.month - 1) // 3 + 1
    for _ in range(6):
        y, q = (y - 1, 4) if q == 1 else (y, q - 1)
        if T.quarter_avail(y, q) <= asof:
            return (y, q)
    return None


def refresh_cache(code, fm, asof, cache):
    """依需要重抓;遇 Quota 往外丟。cache 結構見檔頭。"""
    start = (asof - dt.timedelta(days=365 * BUILD_CFG["years_back"] + 400)).isoformat()
    meta = cache.setdefault("meta", {})
    lm = latest_available_month(asof)
    have_m = {(r[0], r[1]) for r in cache.get("rev") or []}
    if lm and lm not in have_m and meta.get("rev_try") != asof.isoformat():
        rows = fm.get("TaiwanStockMonthRevenue", code, start)
        meta["rev_try"] = asof.isoformat()
        if rows is not None:
            cache["rev"] = sorted({(int(r["revenue_year"]), int(r["revenue_month"])): float(r["revenue"]) for r in rows
                                   if r.get("revenue") is not None}.items())
            cache["rev"] = [[y, m, v] for (y, m), v in cache["rev"]]
    lq = latest_available_quarter(asof)
    have_q = {(r[0], r[1]) for r in cache.get("fin") or [] if r[6] == "finmind"}
    last_try = meta.get("fin_try")
    stale = not last_try or (asof - dt.date.fromisoformat(last_try)).days >= BUILD_CFG["refetch_quarter_days"]
    if lq and lq not in have_q and stale:
        meta["fin_try"] = asof.isoformat()
        fs = fm.get("TaiwanStockFinancialStatements", code, start)
        cf = fm.get("TaiwanStockCashFlowsStatement", code, start)
        if fs is not None:
            q = {}
            for r in fs:
                d = dt.date.fromisoformat(r["date"])
                k = (d.year, (d.month - 1) // 3 + 1)
                q.setdefault(k, {})[r["type"]] = r.get("value")
            keep = [x for x in cache.get("fin") or [] if x[6] != "finmind"]
            have = {(x[0], x[1]) for x in keep}
            fin = [[y, qq, v.get("Revenue"), v.get("GrossProfit"), v.get("OperatingIncome"), v.get("EPS"), "finmind"]
                   for (y, qq), v in sorted(q.items())]
            cache["fin"] = fin + [x for x in keep if (x[0], x[1]) not in {(f[0], f[1]) for f in fin}]
        if cf is not None:
            q = {}
            for r in cf:
                d = dt.date.fromisoformat(r["date"])
                k = (d.year, (d.month - 1) // 3 + 1)
                if r["type"] in ("NetCashInflowFromOperatingActivities", "CashFlowsFromOperatingActivities"):
                    q.setdefault(k, {}).setdefault("ocf", r.get("value"))
                elif r["type"] == "PropertyAndPlantAndEquipment":
                    q.setdefault(k, {})["capex"] = r.get("value")
            cache["cf"] = [[y, qq, v.get("ocf"), v.get("capex")] for (y, qq), v in sorted(q.items())]
    for key, ds, field in (("pe", "TaiwanStockPER", "PER"), ("px", "TaiwanStockPrice", "close")):
        cur = cache.get(key) or []
        last = cur[-1][0] if cur else None
        if last and last >= (asof - dt.timedelta(days=1)).isoformat() and meta.get(key + "_try") == asof.isoformat():
            continue
        if meta.get(key + "_try") == asof.isoformat():
            continue
        s = (dt.date.fromisoformat(last) + dt.timedelta(days=1)).isoformat() if last else \
            (asof - dt.timedelta(days=365 * BUILD_CFG["years_back"] + 10)).isoformat()
        rows = fm.get(ds, code, s)
        meta[key + "_try"] = asof.isoformat()
        if rows:
            add = [[r["date"], r.get(field)] for r in rows if r.get(field) is not None]
            cutoff = (asof - dt.timedelta(days=365 * BUILD_CFG["years_back"] + 10)).isoformat()
            merged = {d: v for d, v in cur + add if d >= cutoff}
            cache[key] = sorted([d, v] for d, v in merged.items())
    return cache


def income_is_cumulative(cache):
    """FinMind 損益是單季還是年度累計:用 Q2/Q3 營收 ÷ 同季 3 個月營收合計判斷(≈1 → 單季;≈2、3 → 累計)"""
    mon = {(y, m): v for y, m, v in cache.get("rev") or []}
    votes = []
    for y, q, rev, *_ in cache.get("fin") or []:
        if q in (2, 3) and rev:
            s = [mon.get((y, 3 * (q - 1) + i)) for i in (1, 2, 3)]
            if None not in s and sum(s) > 0:
                votes.append(rev / sum(s))
    if not votes:
        return None
    med = sorted(votes)[len(votes) // 2]
    return med > 1.5


def mops_supplement(cache, code, mops_rows):
    """最新一季 FinMind 還沒有 → 用 MOPS opendata(仟元 ×1000),先用月營收合計核對單位"""
    r = mops_rows.get(code)
    if not r:
        return None
    y, q = r["year"], r["q"]
    if any(x[0] == y and x[1] == q for x in cache.get("fin") or []):
        return None
    mon = {(yy, m): v for yy, m, v in cache.get("rev") or []}
    s = [mon.get((y, 3 * (q - 1) + i)) for i in (1, 2, 3)]
    if None in s or not r["revenue"]:
        return "unit_unchecked"
    ratio = r["revenue"] * 1000 / sum(s)
    lo, hi = BUILD_CFG["mops_unit_check"]
    if not lo <= ratio <= hi:
        return "unit_mismatch %.2f" % ratio
    cache.setdefault("fin", []).append([y, q, r["revenue"] * 1000, r["gross"] * 1000 if r["gross"] is not None else None,
                                        r["op"] * 1000 if r["op"] is not None else None, r["eps"], "mops"])
    return "added"


def load_mops(get=requests.get):
    out = {}
    for url in MOPS_FIN:
        try:
            resp = get(url, timeout=60, headers={"User-Agent": "quietfolio-research (github.com/thomaschko/quietfolio-data)"})
        except requests.RequestException:
            continue
        if resp.status_code != 200:
            continue
        rows = list(csv.DictReader(io.StringIO(resp.content.decode("utf-8-sig", errors="replace"))))
        for r in rows:
            def num(k):
                v = (r.get(k) or "").replace(",", "").strip()
                try:
                    return float(v)
                except ValueError:
                    return None
            try:
                out[r["公司代號"].strip()] = {"year": int(r["年度"]) + 1911, "q": int(r["季別"]),
                                          "revenue": num("營業收入"), "gross": num("營業毛利（毛損）淨額") if num("營業毛利（毛損）淨額") is not None else num("營業毛利（毛損）"),
                                          "op": num("營業利益（損失）"), "eps": num("基本每股盈餘（元）")}
            except (KeyError, ValueError):
                continue
        time.sleep(2)
    return out


# ------------------------------------------------------------------ 組裝
def fin_quarters(cache):
    cum = income_is_cumulative(cache)
    rows = sorted(cache.get("fin") or [])
    if cum:
        conv = {}
        for field in (2, 3, 4, 5):
            single = T.cum_to_quarter([(r[0], r[1], r[field]) for r in rows])
            for k, v in single.items():
                conv.setdefault(k, {})[field] = v
        rows = [[r[0], r[1], conv[(r[0], r[1])].get(2), conv[(r[0], r[1])].get(3), conv[(r[0], r[1])].get(4),
                 conv[(r[0], r[1])].get(5), r[6]] for r in rows]
    q = [(r[0], r[1], {"revenue": r[2], "gross": r[3], "op": r[4]}) for r in rows]
    eps = [(r[0], r[1], r[5]) for r in rows if r[5] is not None]
    return q, eps, cum


def position(px):
    rows = [(d, c) for d, c in px][-250:]
    if not rows:
        return None
    hi = max(c for _, c in rows)
    gap = 1 - rows[-1][1] / hi
    return {"gap": round(gap, 4), "mark": "red" if gap < BUILD_CFG["pos_red"] else ("yellow" if gap <= BUILD_CFG["pos_yellow"] else "green"),
            "close": rows[-1][1], "date": rows[-1][0]}


def theme_evidence(theme, base="."):
    """題材層級證據:雷達(daily_digest)命中、美股新聞稿 / 法說(us_tw_resonance)"""
    dg = _read(os.path.join(base, "daily_digest.json"))
    terms = [t.lower() for t in theme.get("radar_terms") or []]
    hit = lambda s: any(t in str(s).lower() for t in terms)
    radar = []
    if dg:
        for key, label, f in (("B_preferment_themes", "發酵前", "theme"), ("D_surge_themes", "關鍵字暴增", "theme"),
                              ("G_cross_confirmed", "雙邊確認", "theme"), ("E_new_candidates", "新題材", "term")):
            for x in dg.get(key) or []:
                if hit(x.get(f)):
                    radar.append([label, x.get(f)])
        for x in dg.get("D2_term_heat") or []:
            if x.get("status") in ("surge", "new") and hit(x.get("term")):
                radar.append(["詞熱度", x.get("term")])
    res = _read(os.path.join(base, "us_tw_resonance.json"))
    us = None
    if res:
        gs = [g for g in res.get("groups") or [] if g.get("g") in (theme.get("us_groups") or [])]
        us = {"groups": [[g["g"], g.get("pr_n30"), g.get("tr_cur"), g.get("label")] for g in gs],
              "pr_n30": max([g.get("pr_n30") or 0 for g in gs] or [0]), "tr_cur": max([g.get("tr_cur") or 0 for g in gs] or [0]),
              "asof": (res.get("asof") or {}).get("pr_days")}
    has = bool(radar) or bool(us and (us["pr_n30"] or us["tr_cur"]))
    return {"radar": radar[:8] if dg else "not_built", "radar_date": dg and dg.get("date"),
            "us": us if res else "not_built", "edgar_capex": "not_built", "has": has}


def build_stock(code, src, cache, asof, industry, has_evidence, dd, idx_px, manual=None):
    manual = manual or {}
    q, eps, cum = fin_quarters(cache)
    q1 = T.q1_revenue([tuple(r) for r in cache.get("rev") or []], asof)
    q4 = T.q4_gross_margin(q, asof, industry=industry)
    q5 = T.q5_operating(q, asof)
    q6 = T.q6_fcf([tuple(r) for r in cache.get("cf") or []], asof, eps_rows=eps, industry=industry)
    pe_rows = [(dt.date.fromisoformat(d), v) for d, v in cache.get("pe") or []]
    q9 = T.q9_pe_percentile(pe_rows, asof)
    q2 = T.q2_eps_revision(manual.get("eps_estimates"), asof)
    q3 = T.q3_orders(manual.get("orders"))
    tr = T.tier(q1, q4, q5, has_evidence, q2=q2 if q2["state"] != "n" else None, override=manual.get("tier_override"))
    px = [(dt.date.fromisoformat(d), v) for d, v in cache.get("px") or []]
    out = {"code": code, "src": src, "industry": industry, "tier": tr,
           "checks": {"q1": q1, "q2": q2, "q3": q3, "q4": q4, "q5": q5, "q6": q6, "q9": q9},
           "pos": position([(d.isoformat(), v) for d, v in px]), "income_cumulative": cum}
    if dd.get("peak_date") and px:
        out["rs"] = T.relative_strength(px, idx_px, dd["peak_date"])
        out["box"] = T.four_box(q1, q4, q5, q9, T.stock_drawdown(px), manual_box4=manual.get("box4")) if dd.get("active") else None
    if q6.get("state") == "n" and not cache.get("cf"):
        q6["reason"] = "現金流量表不可得"
    return out


def compact_cells(stock):
    """縮小輸出:日期 / 證據欄位保留;去掉 None"""
    for c in stock["checks"].values():
        for k in [k for k, v in c.items() if v is None]:
            del c[k]
    return stock


def write_theme(theme, payload, base="."):
    """≤ size_limit;超過就把個股分頁({id}_p2.json …)"""
    stocks = payload["stocks"]
    size = BUILD_CFG["size_limit"]
    pages, cur = [], []
    for s in stocks:
        cur.append(s)
        if len(json.dumps(dict(payload, stocks=cur), ensure_ascii=False, separators=(",", ":"))) > size - 500:
            cur.pop()
            pages.append(cur)
            cur = [s]
    pages.append(cur)
    names = []
    for i, ps in enumerate(pages):
        fn = "%s.json" % theme["id"] if i == 0 else "%s_p%d.json" % (theme["id"], i + 1)
        doc = dict(payload, stocks=ps, page=i + 1, pages=len(pages))
        n = len(json.dumps(doc, ensure_ascii=False, separators=(",", ":")))
        if n > size:
            raise ValueError("%s 超過 %d 字元" % (fn, size))
        _write(os.path.join(base, OUT_DIR, fn), doc)
        names.append(fn)
    return names


def run(base=".", fm=None, now=None, mops=None, idx_px=None):
    now = now or dt.datetime.now(TPE)
    asof = now.date()
    cfg = _read(os.path.join(base, "theme_page_themes.json"), {}) or {}
    themes = cfg.get("themes") or []
    names, ind = lookup_meta(base)
    os.makedirs(os.path.join(base, CACHE_DIR), exist_ok=True)
    log = {"started": now.isoformat(timespec="seconds"), "finmind_used": 0, "stopped": None, "mops": {}, "errors": []}
    if fm is None:
        tok = os.environ.get("FINMIND_TOKEN", "").strip()
        if not tok:
            raise SystemExit("缺 FINMIND_TOKEN(GitHub Secret),停止;快取與舊輸出保留")
        fm = FinMind(tok, BUILD_CFG["finmind_budget"])
    if mops is None:
        mops = load_mops()
    # 指數(大跌檢視、相對強度):加權指數
    if idx_px is None:
        icache = _read(os.path.join(base, CACHE_DIR, "_TAIEX.json"), {}) or {}
        try:
            cur = icache.get("px") or []
            last = cur[-1][0] if cur else (asof - dt.timedelta(days=400)).isoformat()
            if icache.get("try") != asof.isoformat():
                rows = fm.get("TaiwanStockPrice", "TAIEX", last) or []
                icache["try"] = asof.isoformat()
                merged = {d: v for d, v in cur + [[r["date"], r["close"]] for r in rows if r.get("close")]}
                icache["px"] = sorted([d, v] for d, v in merged.items() if d >= (asof - dt.timedelta(days=400)).isoformat())
                _write(os.path.join(base, CACHE_DIR, "_TAIEX.json"), icache)
        except Quota as e:
            log["stopped"] = str(e)
        idx_px = [(dt.date.fromisoformat(d), v) for d, v in icache.get("px") or []]
    dd = T.drawdown_state(idx_px) if idx_px else {"active": False, "peak_date": None}
    if idx_px and not dd.get("peak_date"):
        win = idx_px[-T.CFG["dd_lookback"]:]
        dd["peak_date"] = max(win, key=lambda x: x[1])[0]           # 未在大跌期間:相對強度以近 60 日高點為起點(切換檢視用)
        dd["peak"] = max(win, key=lambda x: x[1])[1]
    crash = T.crash_days(idx_px) if idx_px else []
    manual_all = _read(os.path.join(base, "theme_notes_export.json"), {}) or {}
    index = []
    for th in themes:
        ev = theme_evidence(th, base)
        stocks = []
        for code, src in theme_stocks(th, base):
            cp = os.path.join(base, CACHE_DIR, "%s.json" % code)
            cache = _read(cp, {}) or {}
            try:
                refresh_cache(code, fm, asof, cache)
            except Quota as e:
                log["stopped"] = str(e)
            except (requests.RequestException, ValueError, KeyError) as e:
                log["errors"].append("%s: %s" % (code, type(e).__name__))
            st = mops_supplement(cache, code, mops)
            if st:
                log["mops"][code] = st
            _write(cp, cache)
            s = build_stock(code, src, cache, asof, ind.get(code), ev["has"], dd, idx_px,
                            manual=(manual_all.get("stocks") or {}).get(code))
            s["name"] = names.get(code, "")
            stocks.append(compact_cells(s))
        order = {t: i for i, t in enumerate(T.TIERS)}
        stocks.sort(key=lambda s: (order.get(s["tier"]["tier"], 9), s["code"]))
        payload = {"generated": now.isoformat(timespec="seconds"), "asof": asof.isoformat(), "theme": th, "evidence": ev,
                   "drawdown": {"active": dd.get("active"), "peak_date": dd.get("peak_date") and dd["peak_date"].isoformat(),
                                "dd": dd.get("dd"), "crash_days": [[d.isoformat(), r] for d, r in crash[-5:]]},
                   "cfg": {k: T.CFG[k] for k in ("version", "note", "flat_note", "box_note", "gm_drop_pp", "pe_hi_pct",
                                                  "rs_pp", "dd_trigger", "stock_dd", "rev_min_months", "q_min_quarters")},
                   "labels": {"ev": ["事實", "公司財測", "分析師預估", "推論"],
                              "state": {"g": "有證據支持", "y": "需要留意", "n": "資料不足", "na": "不適用", "flat": "持平"}},
                   "notes": ["本頁所有分類、狀態與標籤都是研究優先順序用途,不是買賣訊號;不加總成分數",
                             "四箱以已公布的營收與利潤率代替 EPS 預估(台股沒有免費預估資料);④ 只能手動標記",
                             "分層與四箱是未經驗證的機械規則;累積至少 4 季快照前,不宣稱任何預測力",
                             "月營收、季報、現金流量、本益比、收盤價:FinMind(整理自官方);最新一季損益可能由 MOPS 開放資料補上"],
                   "stocks": stocks}
        try:
            files = write_theme(th, payload, base)
        except ValueError as e:
            log["errors"].append(str(e))
            files = []
        tiers = {}
        for s in stocks:
            tiers[s["tier"]["tier"]] = tiers.get(s["tier"]["tier"], 0) + 1
        index.append({"id": th["id"], "name": th["name"], "files": files, "n_stocks": len(stocks), "tiers": tiers,
                      "radar": ev["radar"] if isinstance(ev["radar"], str) else len(ev["radar"]),
                      "us": ev["us"] if isinstance(ev["us"], str) else {"pr_n30": ev["us"]["pr_n30"], "tr_cur": ev["us"]["tr_cur"]},
                      "kill": {"triggered": 0, "pending": 0, "note": "認錯條件在 Phase 3(Google Sheet)後啟用"},
                      "manual_notes": "not_built" if not manual_all else "ok"})
    log["finmind_used"] = fm.used
    doc = {"generated": now.isoformat(timespec="seconds"), "asof": asof.isoformat(), "themes": index,
           "drawdown_active": bool(dd.get("active")), "log": log,
           "notes": ["研究優先順序用途,不是買賣訊號;沒有分數、不依分數排序"]}
    _write(os.path.join(base, OUT_DIR, "index.json"), doc)
    print(json.dumps({k: v for k, v in log.items()}, ensure_ascii=False))
    return doc


def main():
    run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
