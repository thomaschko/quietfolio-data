# -*- coding: utf-8 -*-
"""
margin_share_probe.py — 族群 / ETF 融資占比模組 Phase 0 探測(2026-11-01,只讀)
============================================================
只在 GitHub Actions 手動執行(margin-share-probe.yml)。每一項獨立 try,失敗只記錄。
- 每個主機先讀 robots.txt;不允許就不查該網址。
- 請求間隔 ≥ 3 秒(官方網站),誠實 User-Agent。
- 遇到 403 / 429 / 驗證碼頁 → 該來源停止,不重試、不換來源繞過。
- 歷史查詢只看「查不查得到、欄位、單位」,不存任何資料。
- FinMind 只有環境變數 FINMIND_TOKEN 存在時才探測可用性,不寫入任何資料。
輸出 margin_share_probe_result.json。
"""
import datetime as dt
import glob
import gzip
import io
import json
import os
import re
import statistics
import sys
import time
from urllib import robotparser
from urllib.parse import urlsplit

import requests

UA = "quietfolio-margin-share-probe/1.0 (read-only research; github.com/thomaschko/quietfolio-data)"
GAP_OFFICIAL = 3.0
GAP_OTHER = 2.0
OUT = "margin_share_probe_result.json"
TPE = dt.timezone(dt.timedelta(hours=8))
TODAY = dt.datetime.now(TPE).date()


class Http:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA})
        self.last = {}
        self.robots = {}
        self.blocked = {}     # host → 原因(403/429/驗證碼);之後不再對該主機發請求
        self.log = []

    def _gap(self, host):
        return GAP_OFFICIAL if ("twse" in host or "tpex" in host) else GAP_OTHER

    def robots_ok(self, url):
        p = urlsplit(url)
        host = p.netloc
        if host not in self.robots:
            try:
                self._wait(host)
                r = self.s.get(f"{p.scheme}://{host}/robots.txt", timeout=30)
                self.last[host] = time.time()
                if r.status_code == 404:
                    self.robots[host] = None
                elif r.status_code == 200:
                    rp = robotparser.RobotFileParser()
                    rp.parse(r.text.splitlines())
                    self.robots[host] = rp
                else:
                    self.robots[host] = "unreadable:%s" % r.status_code
            except Exception as e:
                self.robots[host] = "unreadable:%s" % type(e).__name__
        rp = self.robots[host]
        if rp is None:
            return True, "no robots.txt"
        if isinstance(rp, str):
            return False, rp
        ok = rp.can_fetch(UA, url) and rp.can_fetch("*", url)
        return ok, "allowed" if ok else "disallowed"

    def _wait(self, host):
        w = self._gap(host) - (time.time() - self.last.get(host, 0))
        if w > 0:
            time.sleep(w)

    def get(self, url, headers=None, check_robots=True):
        host = urlsplit(url).netloc
        if host in self.blocked:
            return None, "host_blocked:" + self.blocked[host]
        if check_robots:
            ok, why = self.robots_ok(url)
            if not ok:
                return None, "robots:" + why
        self._wait(host)
        t0 = time.time()
        try:
            r = self.s.get(url, timeout=60, headers=headers or {}, allow_redirects=False)
        except Exception as e:
            self.last[host] = time.time()
            self.log.append({"url": url, "error": type(e).__name__})
            return None, "error:" + type(e).__name__
        self.last[host] = time.time()
        sec = round(time.time() - t0, 2)
        self.log.append({"url": url, "status": r.status_code, "sec": sec, "bytes": len(r.content)})
        if r.status_code in (403, 429) or _captcha(r):
            self.blocked[host] = "HTTP %s%s" % (r.status_code, " captcha" if _captcha(r) else "")
        return r, None


def _captcha(r):
    try:
        t = r.text[:5000].lower() if "html" in r.headers.get("content-type", "") else ""
    except Exception:
        return False
    return "captcha" in t or "驗證碼" in t


def roc(d):
    return "%d/%02d/%02d" % (d.year - 1911, d.month, d.day)


def ymd(d):
    return d.strftime("%Y%m%d")


def weekday_back(days):
    d = TODAY - dt.timedelta(days=days)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d


def jsummary(r):
    """回應摘要:狀態、型別、JSON 的表格欄位與列數、第一列樣本。"""
    if r is None:
        return None
    out = {"status": r.status_code, "content_type": r.headers.get("content-type", ""), "bytes": len(r.content)}
    if r.status_code in (301, 302, 307, 308):
        out["location"] = r.headers.get("location")
        return out
    try:
        j = r.json()
    except Exception:
        out["json"] = False
        out["head"] = r.text[:200]
        return out
    out["json"] = True
    if isinstance(j, list):
        out["rows"] = len(j)
        out["keys"] = list(j[0].keys()) if j and isinstance(j[0], dict) else None
        out["sample"] = j[0] if j else None
        return out
    out["top_keys"] = list(j.keys())[:20]
    for k in ("stat", "date", "reportDate", "iTotalRecords"):
        if k in j:
            out[k] = j[k]
    tables = []
    if isinstance(j.get("tables"), list):
        for t in j["tables"]:
            if isinstance(t, dict):
                tables.append({"title": t.get("title"), "fields": t.get("fields"), "rows": len(t.get("data") or []),
                               "sample": (t.get("data") or [None])[0], "summary": t.get("summary")})
    for k in ("fields", "data", "aaData", "tfootData", "tfootData_one", "tfootData_two"):
        if k in j:
            v = j[k]
            tables.append({"key": k, "rows": len(v) if isinstance(v, list) else None,
                           "sample": v[0] if isinstance(v, list) and v and k != "fields" else (v if k == "fields" else v)})
    out["tables"] = tables
    return out


def has_rows(s, min_rows=50):
    if not s or not s.get("json"):
        return False
    if s.get("rows", 0) >= min_rows:
        return True
    return any((t.get("rows") or 0) >= min_rows for t in s.get("tables", []))


# ---------------------------------------------------------------- 1 / 2 / 8.1.1 repo 內既有資料
def probe_repo():
    out = {}
    md = sorted(glob.glob("margin_daily/*.json"))
    out["margin_daily"] = {"files": len(md), "first": md[0][-13:-5] if md else None, "last": md[-1][-13:-5] if md else None,
                           "note": "GAS runMarginDrop 推送(2026-11-01 新增);在那之前 repo 沒有任何全市場個股融資餘額"}
    if md:
        try:
            d = json.load(open(md[-1], encoding="utf-8"))
            out["margin_daily"]["latest_sample"] = {k: d.get(k) for k in ("date", "status", "unit", "fields", "tpex_date", "twse_fields", "tpex_keys")}
            out["margin_daily"]["latest_counts"] = {"twse": len(d.get("twse") or {}), "tpex": len(d.get("tpex") or {})}
            out["margin_daily"]["twse_summary"] = d.get("twse_summary")
        except Exception as e:
            out["margin_daily"]["error"] = repr(e)
    ph = sorted(p for p in glob.glob("price_history/2*.json"))
    days = [os.path.basename(p)[:8] for p in ph]
    info = {"days": len(days), "first": days[0] if days else None, "last": days[-1] if days else None}
    if ph:
        d = json.load(open(ph[-1], encoding="utf-8"))
        info["latest_codes"] = len(d)
        info["latest_etf_00"] = sum(1 for c in d if c.startswith("00"))
        try:
            mk = json.load(open("price_history/market.json", encoding="utf-8"))
            info["market_json_codes"] = len(mk)
        except Exception:
            pass
    # 缺日:以 insti_daily 為交易日參考
    ins = sorted(os.path.basename(p)[:8] for p in glob.glob("insti_daily/2*.json"))
    info["missing_vs_insti_daily"] = [x for x in ins if days and days[0] <= x <= days[-1] and x not in days]
    out["price_history"] = info
    out["existing_margin_fetch"] = {
        "where": "quietfolio-gas MarginDrop.js(runMarginDrop,排程鏈 night 22:00)",
        "twse": "MI_MARGN selectType=ALL(可指定日期)",
        "tpex": "openapi v1 tpex_mainboard_margin_balance(只有最新一日)",
        "kept_before_2026_11_01": "只留排行前 50 名(margindrop 分頁、daily.json.marginDrop),全市場個股沒有存",
    }
    return out


# ---------------------------------------------------------------- 3 / 4 產業別與 ETF
ISIN_URL = "https://isin.twse.com.tw/isin/C_public.jsp?strMode=%d"


def parse_isin(html):
    """→ [{code, name, market, industry, cfi, section}];section = 表格中的分類標題列(股票 / ETF / …)。"""
    rows, section = [], None
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, flags=re.S | re.I):
        tds = [re.sub(r"<[^>]+>", "", x).replace("&nbsp;", " ").strip() for x in re.findall(r"<td[^>]*>(.*?)</td>", tr, flags=re.S | re.I)]
        if len(tds) == 1 or (len(tds) >= 1 and all(not x for x in tds[1:])):
            if tds and tds[0]:
                section = tds[0].strip()
            continue
        if len(tds) < 6 or "有價證券代號" in tds[0]:
            continue
        m = re.match(r"^(\S+)\s+(.+)$", tds[0].replace("　", " "))
        if not m:
            continue
        rows.append({"code": m.group(1), "name": m.group(2).strip(), "market": tds[3], "industry": tds[4],
                     "cfi": tds[5], "section": section})
    return rows


def probe_industry(http):
    out = {}
    for mkt, mode in (("twse", 2), ("tpex", 4)):
        url = ISIN_URL % mode
        r, err = http.get(url)
        if err:
            out[mkt + "_isin"] = {"error": err}
            continue
        try:
            html = r.content.decode("cp950", errors="replace")
            rows = parse_isin(html)
            secs = {}
            for x in rows:
                secs[x["section"]] = secs.get(x["section"], 0) + 1
            stocks = [x for x in rows if x["section"] and "股票" in x["section"] and "特別" not in x["section"]]
            etf = [x for x in rows if x["section"] and "ETF" in x["section"].upper()]
            inds = {}
            for x in stocks:
                inds[x["industry"]] = inds.get(x["industry"], 0) + 1
            out[mkt + "_isin"] = {
                "url": url, "status": r.status_code, "rows": len(rows), "sections": secs,
                "stocks": len(stocks), "industries": len(inds), "industry_counts": dict(sorted(inds.items(), key=lambda kv: -kv[1])),
                "stock_blank_industry": sum(1 for x in stocks if not x["industry"]),
                "etf": len(etf), "etf_industry_values": sorted({x["industry"] for x in etf})[:10],
                "etf_cfi_prefix": sorted({x["cfi"][:2] for x in etf}),
                "etf_code_not_00": [x["code"] for x in etf if not x["code"].startswith("00")][:20],
                "code00_not_etf_section": [(x["code"], x["section"]) for x in rows if x["code"].startswith("00") and x not in etf][:20],
                "sample_stock": stocks[0] if stocks else None, "sample_etf": etf[0] if etf else None,
            }
            out["_isin_rows_" + mkt] = rows   # 後面比對用,輸出前刪掉
        except Exception as e:
            out[mkt + "_isin"] = {"error": repr(e)}
    # 官方開放資料(公司基本資料;產業別為代碼)
    for key, url in (("twse_openapi_t187ap03_L", "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"),
                     ("tpex_openapi_mopsfin_t187ap03_O", "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O")):
        r, err = http.get(url)
        out[key] = {"error": err} if err else jsummary(r)
        if not err and out[key].get("sample"):
            s = out[key]["sample"]
            out[key]["sample"] = {k: s[k] for k in list(s)[:12]}
    return out


# ---------------------------------------------------------------- 5 題材對照
def parse_theme_md(text):
    return {m.group(1): m.group(2).strip() for m in re.finditer(r"\*\*(\d{4,6}[A-Z]?)\s+([^\*]+?)\*\*", text)}


def probe_themes(http, official_codes, etf_codes):
    out = {}
    # (a) repo 內 intl_theme_baskets.json 的台股部分(族群金流在用)
    try:
        b = json.load(open("intl_theme_baskets.json", encoding="utf-8"))["themes"]
        m = {t: (v.get("tw") or []) for t, v in b.items()}
        out["intl_theme_baskets_tw"] = _theme_stats(m, official_codes, etf_codes)
    except Exception as e:
        out["intl_theme_baskets_tw"] = {"error": repr(e)}
    # (b) My-TW-Coverage themes/*.md
    tree_url = "https://api.github.com/repos/Timeverse/My-TW-Coverage/git/trees/master?recursive=1"
    r, err = http.get(tree_url, headers={"Accept": "application/vnd.github+json"}, check_robots=False)
    if err or r.status_code != 200:
        out["my_tw_coverage"] = {"error": err or "HTTP %s" % r.status_code}
        return out
    paths = [x["path"] for x in r.json().get("tree", []) if x.get("type") == "blob" and x["path"].endswith(".md")]
    theme_paths = [p for p in paths if p.startswith("themes/")]
    info = {"md_files": len(paths), "theme_files": len(theme_paths), "dirs": sorted({p.split("/")[0] for p in paths})[:20]}
    mapping = {}
    for p in theme_paths[:400]:
        rr, e2 = http.get("https://raw.githubusercontent.com/Timeverse/My-TW-Coverage/master/" + requests.utils.quote(p), check_robots=False)
        if e2 or rr.status_code != 200:
            continue
        codes = parse_theme_md(rr.text)
        mapping[os.path.splitext(os.path.basename(p))[0]] = list(codes)
    info.update(_theme_stats(mapping, official_codes, etf_codes))
    info["fetched_theme_files"] = len(mapping)
    info["license_note"] = "repo README 標 MIT(既有 tw_coverage_lookup.py 已在使用)"
    out["my_tw_coverage"] = info
    return out


def _theme_stats(mapping, official_codes, etf_codes):
    per = {}
    for t, codes in mapping.items():
        for c in set(codes):
            per.setdefault(c, set()).add(t)
    dist = {}
    for c, ts in per.items():
        k = "1" if len(ts) == 1 else "2+"
        dist[k] = dist.get(k, 0) + 1
    unmatched = sorted(c for c in per if official_codes and c not in official_codes and c not in etf_codes)
    sizes = sorted(len(set(v)) for v in mapping.values())
    return {"themes": len(mapping), "stocks": len(per), "membership": dist,
            "max_themes_per_stock": max((len(v) for v in per.values()), default=0),
            "theme_size_median": statistics.median(sizes) if sizes else None, "theme_size_max": max(sizes) if sizes else None,
            "etf_in_mapping": sorted(c for c in per if c in etf_codes)[:20],
            "matched_official_ratio": round(1 - len(unmatched) / len(per), 4) if per and official_codes else None,
            "unmatched_first10": unmatched[:10]}


# ---------------------------------------------------------------- 6 / 7 官方融資總額與一致性
TWSE_MARGN = "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?response=json&selectType=ALL&date=%s"
TWSE_MI_INDEX = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?response=json&type=ALLBUT0999&date=%s"
TWSE_INDEX_HIST = "https://www.twse.com.tw/rwd/zh/TAIEX/MI_5MINS_HIST?response=json&date=%s"
TPEX_MARGIN_LATEST = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_margin_balance"
TPEX_MARGIN_HIST = [
    "https://www.tpex.org.tw/www/zh-tw/margin/balance?response=json&date={slash}",
    "https://www.tpex.org.tw/web/stock/margin_trading/margin_balance/margin_bal_result.php?l=zh-tw&o=json&d={roc}",
]
TPEX_CLOSE_HIST = [
    "https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes?response=json&date={slash}",
    "https://www.tpex.org.tw/web/stock/aftertrading/daily_close_quotes/stk_quote_result.php?l=zh-tw&o=json&d={roc}",
]
TPEX_INDEX_HIST = [
    "https://www.tpex.org.tw/www/zh-tw/indexInfo/inx?response=json&date={slash}",
    "https://www.tpex.org.tw/web/stock/iNdex_info/inxh/Inx_result.php?l=zh-tw&o=json&d={roc_month}",
]


def _fmt(tpl, d):
    return tpl.format(slash=d.strftime("%Y/%m/%d"), roc=roc(d), roc_month="%d/%02d" % (d.year - 1911, d.month))


def _num(x):
    try:
        return float(str(x).replace(",", "").strip())
    except Exception:
        return None


def twse_margin_table(j):
    """MI_MARGN JSON → ({code: (bal, prev, note)}, summary_rows, fields)"""
    stock, summary, fields = None, None, None
    for t in j.get("tables") or []:
        f = ",".join(t.get("fields") or [])
        if "代號" in f and "今日餘額" in f:
            stock, fields = t.get("data") or [], t["fields"]
        elif t.get("data"):
            summary = {"title": t.get("title"), "fields": t.get("fields"), "data": t.get("data")}
    out = {}
    if stock:
        ic = fields.index(next(x for x in fields if "代號" in x))
        it = next(i for i, x in enumerate(fields) if "今日餘額" in x)
        ip = next(i for i, x in enumerate(fields) if "前日餘額" in x)
        inote = next((i for i, x in enumerate(fields) if "註記" in x), None)
        for r in stock:
            out[str(r[ic]).strip()] = (_num(r[it]), _num(r[ip]), r[inote] if inote is not None else None)
    return out, summary, fields


def probe_latest_consistency(http, etf_codes):
    out = {}
    # 最近交易日:往前找 MI_MARGN 有資料的那天(最多 6 天)
    margin, summ, fields, date = None, None, None, None
    for back in range(0, 8):
        d = TODAY - dt.timedelta(days=back)
        if d.weekday() >= 5:
            continue
        r, err = http.get(TWSE_MARGN % ymd(d))
        if err:
            out["twse_margin_error"] = err
            break
        try:
            j = r.json()
        except Exception:
            continue
        m, s, f = twse_margin_table(j)
        if m:
            margin, summ, fields, date = m, s, f, d
            out["twse_notes_raw"] = j.get("notes")
            break
    out["twse_latest_date"] = date.isoformat() if date else None
    out["twse_fields"] = fields
    out["twse_summary_table"] = summ
    if margin:
        notes = {}
        for c, (_, _, n) in margin.items():
            if n and str(n).strip():
                for ch in re.split(r"[\s,]+", str(n).strip()):
                    if ch:
                        notes[ch] = notes.get(ch, 0) + 1
        out["twse_note_values"] = notes
        out["twse_note_legend"] = "MI_MARGN 註記欄(例如停止融資、除權息…)的代碼說明在原回應 notes 欄,見 twse_notes_raw"
        out["twse_codes"] = len(margin)
        out["twse_etf_with_margin"] = sum(1 for c, v in margin.items() if c in etf_codes and (v[0] or 0) > 0)
        out["twse_zero_balance"] = sum(1 for v in margin.values() if not v[0])
    # TWSE 收盤(同日)
    close = {}
    if date:
        r, err = http.get(TWSE_MI_INDEX % ymd(date))
        if not err:
            try:
                j = r.json()
                for t in j.get("tables") or []:
                    f = t.get("fields") or []
                    if "證券代號" in f and "收盤價" in f:
                        ic, ix = f.index("證券代號"), f.index("收盤價")
                        for row in t.get("data") or []:
                            v = _num(row[ix])
                            if v:
                                close[str(row[ic]).strip()] = v
                out["twse_close_date"] = j.get("date")
                out["twse_close_codes"] = len(close)
            except Exception as e:
                out["twse_close_error"] = repr(e)
    if margin and close:
        est, miss, miss_lots = 0.0, 0, 0.0
        for c, (bal, _, _) in margin.items():
            if not bal:
                continue
            if c in close:
                est += bal * 1000 * close[c]
            else:
                miss += 1
                miss_lots += bal
        out["twse_est_value"] = est
        out["twse_missing_price"] = {"n": miss, "lots": miss_lots}
        off = None
        for row in (summ or {}).get("data") or []:
            if "金額" in str(row[0]):
                off = _num(row[-1])
        out["twse_official_margin_k"] = off
        out["twse_recon_ratio"] = round(est / (off * 1000), 4) if off else None
    # TPEx 最新(OpenAPI)與同日收盤(price_history)
    r, err = http.get(TPEX_MARGIN_LATEST)
    if err:
        out["tpex_margin_error"] = err
    else:
        s = jsummary(r)
        out["tpex_margin_latest"] = {k: s.get(k) for k in ("status", "rows", "keys", "sample")}
        try:
            arr = r.json()
            dates = sorted({str(x.get("Date")) for x in arr})
            out["tpex_margin_dates"] = dates[:3]
            d8 = None
            if len(dates) == 1 and re.fullmatch(r"\d{7}", dates[0]):
                d8 = str(int(dates[0][:3]) + 1911) + dates[0][3:]
            ph = "price_history/%s.json" % d8 if d8 else None
            tm = {str(x.get("SecuritiesCompanyCode")).strip(): _num(x.get("MarginPurchaseBalance")) for x in arr}
            out["tpex_codes"] = len(tm)
            out["tpex_etf_with_margin"] = sum(1 for c, v in tm.items() if c in etf_codes and (v or 0) > 0)
            out["tpex_zero_balance"] = sum(1 for v in tm.values() if not v)
            if ph and os.path.exists(ph):
                pc = json.load(open(ph, encoding="utf-8"))
                est, miss, ml = 0.0, 0, 0.0
                for c, bal in tm.items():
                    if not bal:
                        continue
                    v = pc.get(c)
                    p = v[0] if isinstance(v, list) and v else None
                    if p:
                        est += bal * 1000 * p
                    else:
                        miss += 1
                        ml += bal
                out["tpex_est_value"] = est
                out["tpex_missing_price"] = {"n": miss, "lots": ml, "price_source": ph}
            else:
                out["tpex_est_value_note"] = "price_history 沒有 %s 的收盤,無法估算" % d8
        except Exception as e:
            out["tpex_parse_error"] = repr(e)
    return out


# ---------------------------------------------------------------- 8.1 歷史可得性
def probe_history(http):
    targets = {"1w": 7, "3m": 91, "6m": 182, "1y": 365, "2y": 730}
    out = {"dates": {}, "twse_margin": {}, "twse_close": {}, "tpex_margin": {}, "tpex_close": {}}
    for label, days in targets.items():
        d = weekday_back(days)
        out["dates"][label] = d.isoformat()
        # TWSE 融資:非交易日就往前一天,最多 3 次
        got = None
        for k in range(3):
            dd = weekday_back(days + k)
            r, err = http.get(TWSE_MARGN % ymd(dd))
            if err:
                got = {"error": err}
                break
            try:
                m, s, f = twse_margin_table(r.json())
            except Exception:
                m, s, f = {}, None, None
            if m:
                got = {"date": dd.isoformat(), "codes": len(m), "has_summary": bool(s), "fields_same_as_today": None, "fields": f}
                break
            got = {"date": dd.isoformat(), "codes": 0, "resp": jsummary(r)}
        out["twse_margin"][label] = got
        dd = dt.date.fromisoformat(got["date"]) if got and got.get("date") else d
        r, err = http.get(TWSE_MI_INDEX % ymd(dd))
        s = {"error": err} if err else jsummary(r)
        out["twse_close"][label] = {"date": dd.isoformat(), "ok": has_rows(s, 500), "summary": _slim(s)}
        for key, cands in (("tpex_margin", TPEX_MARGIN_HIST), ("tpex_close", TPEX_CLOSE_HIST)):
            res = []
            for tpl in cands:
                url = _fmt(tpl, dd)
                r, err = http.get(url)
                s = {"error": err} if err else jsummary(r)
                res.append({"url": url, "ok": has_rows(s, 300), "summary": _slim(s)})
                if res[-1]["ok"]:
                    break
            out[key][label] = res
    # 指數:上市 MI_5MINS_HIST(一個月一次)、上櫃候選
    out["twse_index"] = {}
    out["tpex_index"] = {}
    for label in ("1w", "1y", "2y"):
        d = dt.date.fromisoformat(out["dates"][label])
        r, err = http.get(TWSE_INDEX_HIST % (d.strftime("%Y%m") + "01"))
        s = {"error": err} if err else jsummary(r)
        out["twse_index"][label] = {"month": d.strftime("%Y-%m"), "ok": has_rows(s, 10), "summary": _slim(s)}
        res = []
        for tpl in TPEX_INDEX_HIST:
            url = _fmt(tpl, d.replace(day=1))
            r, err = http.get(url)
            s = {"error": err} if err else jsummary(r)
            res.append({"url": url, "ok": has_rows(s, 10), "summary": _slim(s)})
            if res[-1]["ok"]:
                break
        out["tpex_index"][label] = res
    return out


def _slim(s):
    if not s:
        return s
    s = dict(s)
    for t in s.get("tables", []) or []:
        if isinstance(t.get("sample"), list) and len(t["sample"]) > 20:
            t["sample"] = t["sample"][:20]
    return s


def probe_rate_limit(http):
    """MI_MARGN 連續 20 次(間隔 3 秒,不同日期)。遇 403/429/驗證碼停止。"""
    res = []
    d = TODAY
    while len(res) < 20:
        d -= dt.timedelta(days=1)
        if d.weekday() >= 5:
            continue
        r, err = http.get(TWSE_MARGN % ymd(d))
        if err:
            res.append({"date": d.isoformat(), "stopped": err})
            break
        res.append({"date": d.isoformat(), "status": r.status_code, "sec": http.log[-1].get("sec")})
        if r.status_code in (403, 429) or _captcha(r):
            break
    return {"requests": len(res), "results": res, "blocked": dict(http.blocked)}


def probe_finmind():
    tok = os.environ.get("FINMIND_TOKEN")
    if not tok:
        return {"skipped": "FINMIND_TOKEN 不存在"}
    out = {"note": "只探測可用性與起訖年份;資料不寫入。授權:教育與非商業用途,是否採用由使用者決定"}
    url = "https://api.finmindtrade.com/api/v4/data"
    try:
        time.sleep(GAP_OTHER)
        r = requests.get(url, params={"dataset": "TaiwanStockMarginPurchaseShortSale", "data_id": "2330",
                                      "start_date": "2000-01-01", "end_date": TODAY.isoformat()},
                         headers={"Authorization": "Bearer " + tok, "User-Agent": UA}, timeout=60)
        j = r.json()
        data = j.get("data") or []
        out["by_stock_2330"] = {"status": r.status_code, "msg": j.get("msg"), "rows": len(data),
                                "first": data[0].get("date") if data else None, "last": data[-1].get("date") if data else None,
                                "fields": list(data[0].keys()) if data else None}
        time.sleep(GAP_OTHER)
        d = weekday_back(7).isoformat()
        r = requests.get(url, params={"dataset": "TaiwanStockMarginPurchaseShortSale", "start_date": d, "end_date": d},
                         headers={"Authorization": "Bearer " + tok, "User-Agent": UA}, timeout=60)
        j = r.json()
        out["all_stocks_one_day"] = {"status": r.status_code, "msg": j.get("msg"), "rows": len(j.get("data") or [])}
        time.sleep(GAP_OTHER)
        r = requests.get("https://api.web.finmindtrade.com/v2/user_info", headers={"Authorization": "Bearer " + tok, "User-Agent": UA}, timeout=30)
        try:
            ui = r.json()
            out["quota"] = {k: ui.get(k) for k in ("user_count", "api_request_limit", "level", "level_title")}
        except Exception:
            out["quota"] = {"status": r.status_code}
    except Exception as e:
        out["error"] = repr(e)
    return out


def feasibility(hist, rl):
    def earliest(sec):
        ok = [(v.get("date") if isinstance(v, dict) else None) for v in sec.values()
              if isinstance(v, dict) and v.get("codes")]
        return min(ok) if ok else None

    def earliest_c(sec):
        ds = []
        for lab, v in sec.items():
            if isinstance(v, list) and any(x.get("ok") for x in v):
                ds.append(hist["dates"][lab])
            elif isinstance(v, dict) and v.get("ok"):
                ds.append(v.get("date"))
        return min(ds) if ds else None

    blocked = bool(rl.get("blocked"))
    days = 490
    per_day = {"twse": 2, "tpex": 2}
    req = days * sum(per_day.values())
    return {
        "twse_margin_earliest_tested_ok": earliest(hist["twse_margin"]),
        "twse_close_earliest_tested_ok": earliest_c(hist["twse_close"]),
        "tpex_margin_earliest_tested_ok": earliest_c(hist["tpex_margin"]),
        "tpex_close_earliest_tested_ok": earliest_c(hist["tpex_close"]),
        "rate_limited": blocked,
        "estimate_490d": {"requests": req, "minutes_at_3s": round(req * 3 / 60), "batches_of_150_days": -(-days // 150)},
        "survivorship_note": "官方每日全市場行情含當日所有掛牌股(當時已存在、之後下市的也在);yfinance 缺已下市股票",
    }


def main():
    http = Http()
    res = {"generated": dt.datetime.now(TPE).isoformat(timespec="seconds"), "ua": UA}
    steps = {}

    def run(name, fn, *a):
        t0 = time.time()
        try:
            steps[name] = fn(*a)
        except Exception as e:
            steps[name] = {"error": repr(e)}
        print("  %-24s %5.0fs" % (name, time.time() - t0), flush=True)

    run("repo", probe_repo)
    run("industry_etf", probe_industry, http)
    ind = steps.get("industry_etf") or {}
    rows = (ind.pop("_isin_rows_twse", None) or []) + (ind.pop("_isin_rows_tpex", None) or [])
    official = {x["code"] for x in rows if x["section"] and "股票" in x["section"]}
    etf = {x["code"] for x in rows if x["section"] and "ETF" in x["section"].upper()}
    run("themes", probe_themes, http, official, etf)
    run("latest_consistency", probe_latest_consistency, http, etf)
    run("history", probe_history, http)
    run("rate_limit", probe_rate_limit, http)
    run("finmind", probe_finmind)
    res.update(steps)
    try:
        res["history_feasibility"] = feasibility(steps["history"], steps["rate_limit"])
    except Exception as e:
        res["history_feasibility"] = {"error": repr(e)}

    # verdict
    lc = steps.get("latest_consistency") or {}
    have = {
        "existing_margin_daily": (steps["repo"].get("margin_daily", {}).get("files") or 0) > 0,
        "twse_margin": bool(lc.get("twse_codes")),
        "tpex_margin": bool(lc.get("tpex_codes")),
        "industry_twse": bool((ind.get("twse_isin") or {}).get("stocks")),
        "industry_tpex": bool((ind.get("tpex_isin") or {}).get("stocks")),
        "themes": bool(((steps.get("themes") or {}).get("my_tw_coverage") or {}).get("themes")),
        "official_total_twse": lc.get("twse_official_margin_k") is not None,
    }
    if http.blocked:
        verdict = "blocked"
    elif not any(have.values()):
        verdict = "unreachable"
    elif all(have.values()):
        verdict = "ok"
    else:
        verdict = "partial"
    res["have"] = have
    res["verdict"] = verdict
    res["request_log_tail"] = http.log[-30:]
    res["request_count"] = len(http.log)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)
    print(json.dumps({"verdict": verdict, "have": have, "feasibility": res["history_feasibility"]}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
