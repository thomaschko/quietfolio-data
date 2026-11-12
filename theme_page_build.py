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

import theme_auto as A
import theme_checks_core as T

BUILD_CFG = {
    "finmind_budget": 550, "finmind_gap": 1.0, "years_back": 3,
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
        params = {"dataset": dataset}
        if data_id:
            params["data_id"] = data_id
        if start:
            params["start_date"] = start
        r = self.s.get(FINMIND, params=params,
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
    """→ [(code, 來源說明)](依序去重);Sheet 有這個題材的個股清單時以 Sheet 為準"""
    if theme.get("_auto_stocks") and not theme.get("_sheet_stocks"):
        return list(theme["_auto_stocks"])
    if theme.get("_sheet_stocks"):
        out, seen = [], set()
        for r in theme["_sheet_stocks"]:
            c = str(r["code"]).strip()
            if c and c not in seen and r.get("status") != "移除":
                seen.add(c)
                out.append((c, "Sheet theme_stocks"))
        return out
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


THEME_TEXT_FIELDS = ("who_pays", "bottleneck", "revenue_path", "chain", "segment", "supply_note", "source", "edited")


def merge_export(themes, export):
    """Google Sheet theme_notes 匯出(theme_notes_export.json,GAS 每小時推送)併入題材設定:
    三句話與鏈條等文字;theme_stocks 有該題材的列 → 以 Sheet 為準(狀態「移除」不列入)。Sheet 有、預設沒有的題材也會建立。"""
    ex_th = (export or {}).get("themes") or {}
    ex_st = (export or {}).get("theme_stocks") or {}
    out = []
    ids = [t["id"] for t in themes] + [i for i in ex_th if i not in {t["id"] for t in themes}]
    by_id = {t["id"]: t for t in themes}
    for i in ids:
        th = dict(by_id.get(i) or {"id": i, "name": (ex_th.get(i) or {}).get("name") or i, "radar_terms": [], "us_groups": []})
        notes = ex_th.get(i)
        if notes:
            th["name"] = notes.get("name") or th["name"]
            th["notes"] = {k: notes.get(k) for k in THEME_TEXT_FIELDS if notes.get(k)}
        rows = [r for r in ex_st.get(i) or [] if r.get("code")]
        if rows:
            th["_sheet_stocks"] = rows
        out.append(th)
    return out


def manual_for(code, theme_row, export):
    """→ build_stock 的 manual:EPS 預估(題 2)、訂單(題 3,取最新)、分層覆寫、四箱 ④、其他手動證據"""
    ev = [e for e in ((export or {}).get("evidence") or {}).get(code) or []]
    m = {"eps_estimates": [{"target_year": e.get("target_year"), "date": e.get("date"), "eps": e.get("eps"),
                            "source": e.get("source")} for e in ev if str(e.get("q")) == "2" and e.get("target_year") and e.get("eps") is not None],
         "other": [e for e in ev if str(e.get("q")) not in ("2", "3")]}
    orders = sorted([e for e in ev if str(e.get("q")) == "3"], key=lambda e: e.get("date") or "")
    if orders:
        o = orders[-1]
        m["orders"] = {"content": o.get("content"), "source": o.get("source"), "date": o.get("date"), "ev": o.get("ev"),
                       "cancel_terms": o.get("cancel_terms"), "delivery": o.get("delivery")}
    r = theme_row or {}
    if r.get("tier_override") and r.get("override_reason"):
        m["tier_override"] = {"tier": r["tier_override"], "reason": r["override_reason"]}
    if r.get("box4_reason"):
        m["box4"] = {"reason": r["box4_reason"]}
    return m


def kill_for(code, export, cache, asof):
    """認錯條件:最新版本逐條檢查(自動條件用快取資料,其他標手動確認);舊版本一併附上"""
    rows = (export or {}).get("kill") or []
    cc = T.current_conditions(rows, code)
    if not cc["current"]:
        return None
    q, _, _ = fin_quarters(cache)
    pe_rows = [(dt.date.fromisoformat(d), v) for d, v in cache.get("pe") or []]
    cur = []
    for c in cc["current"]:
        res = T.evaluate_condition(c, [tuple(r) for r in cache.get("rev") or []], q, pe_rows, asof) \
            if str(c.get("mode") or "").startswith("自動") else {"status": "manual"}
        cur.append(dict(c, result=res))
    return {"current": cur, "history": cc["history"]}


def theme_margin_crowd(codes_by_theme, base=".", min_days=120):
    """題材自己的個股算融資擁擠度(margin_hist/*.csv.gz:每日每檔融資張數與收盤價)。
    占比 = Σ 題材個股(融資張數 × 1000 × 收盤價)÷ 上市 + 上櫃全部股票同日總和;張數 = Σ 題材個股融資張數。
    另算等權指標:每檔融資張數在自己歷史的分位,題材取中位數(擁擠判斷用這個,避免台積電這類大型股主導、也不受股價影響)。
    分位 = 今天在自己過去可得歷史(不含今天)中的百分位;歷史 < min_days 天不給。
    → {theme_id: {share_pct, share_pctile, lots, lots_pctile, n_days, asof}}"""
    import bisect
    import glob
    import gzip
    files = sorted(glob.glob(os.path.join(base, "margin_hist", "*.csv.gz")))
    if not files:
        return {}
    want = {}
    for tid, codes in codes_by_theme.items():
        for c in codes:
            want.setdefault(c, []).append(tid)
    total, ev, lots, per = {}, {}, {}, {}
    for fn in files:
        with gzip.open(fn, "rt", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                try:
                    l, p = float(r["margin_lots"] or 0), float(r["close"] or 0)
                except ValueError:
                    continue
                if not p:
                    continue
                d = r["date"]
                v = l * 1000 * p
                total[d] = total.get(d, 0) + v
                if r["code"] in want:
                    per.setdefault(r["code"], {})[d] = l
                for tid in want.get(r["code"], ()):
                    ev.setdefault(tid, {})
                    ev[tid][d] = ev[tid].get(d, 0) + v
                    lots.setdefault(tid, {})
                    lots[tid][d] = lots[tid].get(d, 0) + l

    def pct(vals, x):
        vals = sorted(vals)
        lo, hi = bisect.bisect_left(vals, x), bisect.bisect_right(vals, x)
        return round((lo + hi) / 2 / len(vals) * 100, 1)
    out = {}
    for tid in codes_by_theme:
        days = sorted(d for d in (ev.get(tid) or {}) if total.get(d))
        if not days:
            continue
        share = [ev[tid][d] / total[d] for d in days]
        lt = [lots[tid][d] for d in days]
        o = {"asof": days[-1], "n_days": len(days), "share_pct": round(share[-1] * 100, 3), "lots": int(lt[-1])}
        if len(days) - 1 >= min_days:
            o["share_pctile"] = pct(share[:-1], share[-1])
            o["lots_pctile"] = pct(lt[:-1], lt[-1])
        # 等權:每檔先跟自己過去的融資張數比(不受股價影響、不被大型股主導),題材取中位數
        ps = []
        for c in codes_by_theme[tid]:
            h = per.get(c) or {}
            ds = sorted(h)
            if len(ds) - 1 >= min_days and ds[-1] == days[-1] and max(h[x] for x in ds[:-1]) > 0:
                ps.append(pct([h[x] for x in ds[:-1]], h[ds[-1]]))
        if ps:
            ps.sort()
            o["stock_lots_pctile_median"] = ps[len(ps) // 2] if len(ps) % 2 else round((ps[len(ps) // 2 - 1] + ps[len(ps) // 2]) / 2, 1)
            o["stock_lots_hi_n"] = sum(1 for x in ps if x >= 90)
            o["stock_n"] = len(ps)
        out[tid] = o
    return out


def theme_herd(codes, base="."):
    """散戶羊群(描述):集保週資料(chip_structure_full.json)與共識頁(consensus.json)。
    → 散戶比重週變化平均、股東人數週變化、「散戶比重增 + 大戶比重減」檔數、「融資增 + 法人賣」檔數"""
    chip = _read(os.path.join(base, "chip_structure_full.json"), {}) or {}
    st = chip.get("stocks") or {}
    cons = _read(os.path.join(base, "consensus.json"), {}) or {}
    mu = {str(r[0]) for r in (((cons.get("lev_inst") or {}).get("lists") or {}).get("margin_up_inst_sell") or [])}
    n = herd = 0
    retail_pp, holders_now, holders_chg = [], 0, 0
    for c in codes:
        s = st.get(c) or {}
        d1, w = s.get("d1"), s.get("w") or []
        if not d1 or len(d1) < 3 or d1[0] is None or d1[1] is None:
            continue
        n += 1
        retail_pp.append(d1[1])
        if d1[1] > 0 and d1[0] < 0:
            herd += 1
        if w and w[-1][3] and d1[2] is not None:
            holders_now += w[-1][3]
            holders_chg += d1[2]
    if not n:
        return {"n": 0}
    base_h = holders_now - holders_chg
    return {"n": n, "asof": chip.get("tdcc_asof"), "retail_pp_avg": round(sum(retail_pp) / n, 3),
            "herd_n": herd, "holders_chg_pct": round(holders_chg / base_h * 100, 2) if base_h else None,
            "margin_up_inst_sell_n": sum(1 for c in codes if c in mu),
            "consensus_window": (cons.get("window") or {}).get("t")}


def radar_verification(base="."):
    """雷達驗證台帳(verification_ledger.py → daily_digest.json H_verification_summary)原樣帶出,頁面顯示用"""
    dg = _read(os.path.join(base, "daily_digest.json"), {}) or {}
    h = dg.get("H_verification_summary")
    if not h:
        return "not_built"
    return {"by_type": h.get("by_detection_type"), "pending": h.get("pending_count"), "total": h.get("total_claims"),
            "rule": "標記後 30 天,收盤價較標記日漲 ≥ +10% 算印證,否則算反駁(反駁不代表下跌);沒有和隨機挑股比較",
            "asof": dg.get("date")}


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
    rows = [(d, c) for d, c in px if c][-250:]
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
    qs = {r[1] for r in cache.get("fin") or []}
    if qs and qs <= {2, 4}:
        out["note"] = "只有半年報(Q2、Q4,常見於興櫃公司),季度判定多為資料不足"
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


def run(base=".", fm=None, now=None, mops=None, idx_px=None, llm=None):
    now = now or dt.datetime.now(TPE)
    asof = now.date()
    auto_meta = None
    if os.path.exists(os.path.join(base, "theme_auto_map.json")):
        themes, auto_meta = A.select_themes(base, asof)        # 建議關注趨勢題材(自動選)
    else:
        themes = (_read(os.path.join(base, "theme_page_themes.json"), {}) or {}).get("themes") or []
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
    themes = merge_export(themes, manual_all)
    all_codes = {c for th in themes for c, _ in theme_stocks(th, base)}
    name_cache = _read(os.path.join(base, CACHE_DIR, "_names.json"), {}) or {}
    names = dict(name_cache, **{k: v for k, v in names.items() if v})
    if any(not names.get(c) for c in all_codes) and name_cache.get("_try") != asof.isoformat():
        try:                                           # 籌碼檔沒有的代號(例如興櫃)→ FinMind 股票清單補名稱(1 次請求)
            rows = fm.get("TaiwanStockInfo", "", "") or []
            name_cache = {r["stock_id"]: r.get("stock_name") or "" for r in rows if r.get("stock_id") in all_codes}
            name_cache["_try"] = asof.isoformat()
            _write(os.path.join(base, CACHE_DIR, "_names.json"), name_cache)
            names.update({k: v for k, v in name_cache.items() if k != "_try" and v and not names.get(k)})
        except Quota as e:
            log["stopped"] = str(e)
    ai_notes = A.draft_notes(themes, names, base, asof, chain=llm)
    # 2026-11-12:擁擠度改用題材自己的個股(趨勢題材頁列出的那幾檔)計算,不再借 My-TW-Coverage 分類
    crowd = theme_margin_crowd({th["id"]: [c for c, _ in theme_stocks(th, base)] for th in themes}, base)
    st_all = _read(os.path.join(base, OUT_DIR, "auto_state.json"), {}) or {}
    firsts = [v.get("first") for v in st_all.values() if v.get("first")]
    tracking_start = min(firsts) if firsts else None
    kill_auto = _read(os.path.join(base, OUT_DIR, "kill_auto.json"), []) or []
    index = []
    for th in themes:
        ev = theme_evidence(th, base)
        stocks = []
        px_by_code = {}
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
            px_by_code[code] = [(dt.date.fromisoformat(d), v) for d, v in cache.get("px") or []]
            row = next((r for r in th.get("_sheet_stocks") or [] if str(r.get("code")).strip() == code), None)
            man = manual_for(code, row, manual_all)
            try:
                s = build_stock(code, src, cache, asof, ind.get(code), ev["has"], dd, idx_px, manual=man)
            except (ArithmeticError, ValueError, TypeError, KeyError, IndexError) as e:   # 單檔資料異常不中斷整次
                log["errors"].append("%s build: %s" % (code, type(e).__name__))
                continue
            s["name"] = names.get(code, "")
            if row:
                s["sheet"] = {k: row.get(k) for k in ("segment", "added", "status") if row.get(k)}
            if man.get("other"):
                s["manual_evidence"] = man["other"][:6]
            kill_auto = A.auto_kill({code: s["tier"]["mech_tier"]}, base, asof)
            k = kill_for(code, dict(manual_all, kill=kill_auto + list(manual_all.get("kill") or [])), cache, asof)
            if k:
                s["kill"] = k
            stocks.append(compact_cells(s))
        order = {t: i for i, t in enumerate(T.TIERS)}
        stocks.sort(key=lambda s: (order.get(s["tier"]["tier"], 9), s["code"]))
        th_pub = {k: v for k, v in th.items() if not k.startswith("_")}
        if th.get("auto"):
            mc = crowd.get(th["id"]) or {}
            # 擁擠判斷用「個股融資張數分位的中位數」(等權、不含股價);加權占比分位只顯示
            th_pub["lifecycle"] = A.lifecycle(th["auto"], px_by_code, idx_px or [], mc.get("stock_lots_pctile_median"), tracking_start)
            th_pub["lifecycle"]["margin"] = mc
            th_pub["lifecycle"]["herd"] = theme_herd(list(px_by_code), base)
            th_pub["auto"] = {k: v for k, v in th["auto"].items() if k != "heat_hist"}
        if not th_pub.get("notes") and ai_notes.get(th["id"]):
            th_pub["ai_notes"] = dict(ai_notes[th["id"]], label="AI 草稿,需人工核對(只用本頁資料產生)")
        payload = {"generated": now.isoformat(timespec="seconds"), "asof": asof.isoformat(), "theme": th_pub, "evidence": ev,
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
            files = write_theme(th_pub, payload, base)
        except ValueError as e:
            log["errors"].append(str(e))
            files = []
        tiers = {}
        trig = pend = 0
        for s in stocks:
            tiers[s["tier"]["tier"]] = tiers.get(s["tier"]["tier"], 0) + 1
            trig += sum(1 for c in (s.get("kill") or {}).get("current") or [] if c["result"]["status"] == "triggered")
            own = [c for c in (s.get("kill") or {}).get("current") or [] if c.get("source") != "系統預設"]
            if (s.get("sheet") or {}).get("status") == "追蹤中" and not own:     # 只有系統預設 → 仍算待填(使用者還沒寫)
                pend += 1
        index.append({"id": th["id"], "name": th["name"], "files": files, "n_stocks": len(stocks), "tiers": tiers,
                      "radar": ev["radar"] if isinstance(ev["radar"], str) else len(ev["radar"]),
                      "us": ev["us"] if isinstance(ev["us"], str) else {"pr_n30": ev["us"]["pr_n30"], "tr_cur": ev["us"]["tr_cur"]},
                      "kill": {"triggered": trig, "pending": pend},
                      "notes": (th.get("notes") or {}),
                      "ai_notes": bool(th_pub.get("ai_notes")),
                      "auto": th_pub.get("auto"),
                      "lifecycle": th_pub.get("lifecycle"),
                      "manual_notes": "not_built" if not manual_all else "ok"})
    log["finmind_used"] = fm.used
    doc = {"generated": now.isoformat(timespec="seconds"), "asof": asof.isoformat(), "themes": index,
           "sheet": {"generated": manual_all.get("generated"), "pending": (manual_all.get("pending") or [])[:30],
                     "errors": (manual_all.get("errors") or [])[:20]} if manual_all else "not_built",
           "drawdown_active": bool(dd.get("active")), "log": log, "auto_meta": auto_meta,
           "lifecycle_cfg": A.LIFECYCLE_CFG, "stages": A.STAGES, "tracking_start": tracking_start,
           "radar_verification": radar_verification(base),
           "notes": ["研究優先順序用途,不是買賣訊號;沒有分數、不依分數排序"]}
    _write(os.path.join(base, OUT_DIR, "index.json"), doc)
    print(json.dumps({k: v for k, v in log.items()}, ensure_ascii=False))
    return doc


def main():
    run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
