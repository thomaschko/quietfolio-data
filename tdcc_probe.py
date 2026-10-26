# -*- coding: utf-8 -*-
"""
tdcc_probe.py — Phase 0 一次性探測:集保戶股權分散表 × 法人籌碼(2026-10-26)
只探測,不建立任何資料檔、不接入雷達。輸出 tdcc_probe_result.json。
規則:誠實 UA、請求間隔 ≥ 2 秒、總請求 ≤ 30;每個檢查項獨立 try,失敗只記錄不中斷;
robots 不允許 / 401 / 403 / 451 / 驗證碼 / 登入牆 → 停止該路徑並記錄,不繞過。
集保官網歷史查詢頁有驗證碼:只讀一次頁面判斷有沒有可程式化 API,不嘗試破解、不送查詢。
第三方(FinMind)只在環境變數 FINMIND_TOKEN 有值時才測,token 不寫進任何檔案或記錄。
"""
import io
import os
import re
import csv
import sys
import json
import time
import datetime as dt
import urllib.robotparser
from urllib.parse import urlparse

import requests

UA = "quietfolio-probe/1.0 (+https://github.com/thomaschko/quietfolio-data; one-off feasibility check)"
MIN_GAP = 2.0
MAX_REQ = 30
OUT = "tdcc_probe_result.json"
OD_URLS = ["https://smart.tdcc.com.tw/opendata/getOD.ashx?id=1-5",
           "https://opendata.tdcc.com.tw/getOD.ashx?id=1-5"]
QUERY_PAGE = "https://www.tdcc.com.tw/portal/zh/smWeb/qryStock"
SAMPLES = ["2337", "2408", "6488"]           # 旺宏、南亞科(上市)、環球晶(上櫃)
FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"
FINMIND_DATASET = "TaiwanStockHoldingSharesPer"
BLOCK = {401, 403, 451}
WALL = ("驗證碼", "验证码", "captcha", "請登入", "登入後", "recaptcha")
TPE = dt.timezone(dt.timedelta(hours=8))

_s = requests.Session()
_s.headers.update({"User-Agent": UA})
_st = {"n": 0, "last": 0.0}
_robots = {}
notes = []


def req(method, url, **kw):
    if _st["n"] >= MAX_REQ:
        notes.append(f"已達請求上限 {MAX_REQ},略過 {url}")
        return None
    w = MIN_GAP - (time.time() - _st["last"])
    if w > 0:
        time.sleep(w)
    _st["n"] += 1
    _st["last"] = time.time()
    try:
        r = _s.request(method, url, timeout=60, **kw)
    except Exception as e:
        notes.append(f"{method} {urlparse(url).netloc}{urlparse(url).path} 失敗:{type(e).__name__}: {str(e)[:160]}")
        return None
    shown = url if "token" not in url.lower() else url.split("?")[0]
    print(f"[{_st['n']:02d}] {r.status_code} {method} {shown} ({len(r.content)} bytes)", flush=True)
    return r


def allowed(url):
    host = urlparse(url).netloc
    if host not in _robots:
        rp = urllib.robotparser.RobotFileParser()
        r = req("GET", f"https://{host}/robots.txt")
        rec = {"status": None if r is None else r.status_code, "rules": ""}
        if r is not None and r.status_code == 200 and "<html" not in r.text[:500].lower():
            rp.parse(r.text.splitlines())
            rec["rules"] = " | ".join(l.strip() for l in r.text.splitlines()
                                      if re.match(r"\s*(user-agent|allow|disallow|sitemap|crawl-delay)", l, re.I))[:500]
        elif r is not None and (r.status_code in (404, 410) or r.status_code == 200):
            rp.parse([])
            rec["rules"] = "沒有 robots.txt(或回傳 HTML),視為無限制"
        else:
            rp.parse(["User-agent: *", "Disallow: /"])
            rec["rules"] = "robots.txt 取不到,保守視為不允許"
        _robots[host] = (rp, rec)
    rp, _ = _robots[host]
    return rp.can_fetch(UA, url) and rp.can_fetch("*", url)


def decode_csv(r):
    raw = r.content
    for enc in ("utf-8-sig", "big5", "cp950"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace"), "utf-8(replace)"


def parse_od(text):
    """集保分散表 CSV → (header, rows)。欄位名稱不先假設,依表頭關鍵字找。"""
    rows = list(csv.reader(io.StringIO(text)))
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        return [], []
    return [h.strip() for h in rows[0]], rows[1:]


def col(header, *keys):
    for i, h in enumerate(header):
        if any(k in h for k in keys):
            return i
    return None


def num(s):
    try:
        return float(str(s).replace(",", "").strip())
    except ValueError:
        return None


def check_open_data(out):
    res = []
    best = None
    for url in OD_URLS:
        for method in ("GET", "POST"):
            rec = {"url": url, "method": method}
            try:
                if not allowed(url):
                    rec["error"] = "robots 不允許"
                    res.append(rec)
                    continue
                r = req(method, url)
                if r is None:
                    rec["error"] = "連線失敗(見 notes)"
                    res.append(rec)
                    continue
                rec["status"] = r.status_code
                rec["content_type"] = r.headers.get("Content-Type", "")
                if r.status_code in BLOCK:
                    rec["error"] = f"HTTP {r.status_code},停止此路徑"
                    res.append(rec)
                    continue
                if r.status_code != 200:
                    res.append(rec)
                    continue
                text, enc = decode_csv(r)
                rec["encoding"] = enc
                header, rows = parse_od(text)
                rec["header"] = header
                rec["rows"] = len(rows)
                rec["bytes"] = len(r.content)
                if header and rows and best is None:
                    best = (url, method, header, rows, enc)
            except Exception as e:
                rec["error"] = f"{type(e).__name__}: {str(e)[:160]}"
            res.append(rec)
    out["open_data"]["attempts"] = res
    if not best:
        out["open_data"]["usable"] = False
        return None
    url, method, header, rows, enc = best
    ci = {"date": col(header, "資料日期", "日期"), "code": col(header, "證券代號", "代號"),
          "level": col(header, "持股分級", "分級"), "holders": col(header, "人數"),
          "shares": col(header, "股數", "單位數"), "pct": col(header, "比例", "%")}
    dates = sorted({r[ci["date"]].strip() for r in rows if ci["date"] is not None and len(r) > ci["date"]})
    codes = {r[ci["code"]].strip() for r in rows if ci["code"] is not None and len(r) > ci["code"]}
    levels = sorted({int(num(r[ci["level"]])) for r in rows
                     if ci["level"] is not None and len(r) > ci["level"] and num(r[ci["level"]]) is not None})
    out["open_data"].update({"usable": True, "url": url, "method": method, "encoding": enc, "columns": header,
                             "column_index": ci, "data_dates": dates[:5], "only_latest_week": len(dates) == 1,
                             "rows": len(rows), "stocks": len(codes),
                             "levels": [levels[0], levels[-1]] if levels else [], "levels_all": levels})
    return rows, ci


def check_samples(out, parsed):
    if not parsed:
        return
    rows, ci = parsed
    for code in SAMPLES:
        try:
            sub = [r for r in rows if r[ci["code"]].strip() == code]
            levels = {}
            for r in sub:
                lv = int(num(r[ci["level"]]))
                levels[lv] = {"holders": num(r[ci["holders"]]), "shares": num(r[ci["shares"]]),
                              "pct": num(r[ci["pct"]]) if ci["pct"] is not None else None}
            if not levels:
                out["samples"][code] = {"error": "開放資料裡沒有這檔"}
                continue
            tiers = [levels[k] for k in range(1, 16) if k in levels]
            total = levels.get(17)
            s_sh = sum(x["shares"] or 0 for x in tiers)
            s_ho = sum(x["holders"] or 0 for x in tiers)
            s_pct = sum(x["pct"] or 0 for x in tiers)
            adj = levels.get(16)
            out["samples"][code] = {
                "levels": {str(k): v for k, v in sorted(levels.items())},
                "sum_1_15": {"holders": s_ho, "shares": s_sh, "pct": round(s_pct, 2)},
                "level16_adjust": adj, "level17_total": total,
                "shares_match_total": bool(total) and abs(s_sh + ((adj or {}).get("shares") or 0) - (total["shares"] or 0)) < 1,
                "shares_match_total_without_16": bool(total) and abs(s_sh - (total["shares"] or 0)) < 1,
                "holders_match_total": bool(total) and abs(s_ho - (total["holders"] or 0)) < 1,
                "pct_sum_close_to_100": abs(s_pct - 100) < 0.5,
            }
        except Exception as e:
            out["samples"][code] = {"error": f"{type(e).__name__}: {str(e)[:160]}"}


def check_query_page(out):
    rec = {"url": QUERY_PAGE}
    try:
        if not allowed(QUERY_PAGE):
            rec["result"] = "robots 不允許,未讀取"
            out["history"]["official_query"] = rec
            return
        r = req("GET", QUERY_PAGE)
        if r is None:
            rec["result"] = "連線失敗"
        else:
            rec["status"] = r.status_code
            low = r.text.lower()
            rec["captcha"] = any(k.lower() in low for k in WALL)
            apis = sorted(set(re.findall(r"""["']((?:/[\w\-./]*)?(?:api|ajax|json|Ajax|Api)[\w\-./]*)["']""", r.text)))[:10]
            rec["api_hints_in_html"] = apis
            rec["result"] = ("僅網頁查詢+驗證碼(依規格不自動化,不嘗試破解)" if rec["captcha"]
                             else "頁面沒有看到驗證碼字樣;未送出任何查詢,需人工確認")
    except Exception as e:
        rec["result"] = f"{type(e).__name__}: {str(e)[:160]}"
    out["history"]["official_query"] = rec


def check_finmind(out):
    rec = {"dataset": FINMIND_DATASET, "doc": "https://finmind.github.io/"}
    token = os.environ.get("FINMIND_TOKEN", "").strip()
    if not token:
        rec["tested"] = False
        rec["result"] = "沒有提供 FINMIND_TOKEN 環境變數,依規格不測(需在 GitHub Secrets 設定後重跑)"
        out["history"]["finmind"] = rec
        return
    rec["tested"] = True
    try:
        start = (dt.date.today() - dt.timedelta(days=730)).isoformat()
        r = req("GET", FINMIND_URL, params={"dataset": FINMIND_DATASET, "data_id": "2408", "start_date": start},
                headers={"Authorization": f"Bearer {token}"})
        if r is None:
            rec["result"] = "連線失敗"
        else:
            rec["status"] = r.status_code
            try:
                j = r.json()
            except ValueError:
                j = {}
            data = j.get("data") or []
            rec["msg"] = str(j.get("msg", ""))[:200]
            rec["rows"] = len(data)
            if data:
                rec["fields"] = list(data[0].keys())
                ds = sorted({x.get("date") for x in data if x.get("date")})
                rec["date_range"] = [ds[0], ds[-1]] if ds else []
                rec["weeks"] = len(ds)
                rec["levels_sample"] = sorted({str(x.get("HoldingSharesLevel")) for x in data})[:20]
            rec["result"] = "可取得" if data else "沒有資料(可能需付費方案或資料集名稱不同,見 msg)"
    except Exception as e:
        rec["result"] = f"{type(e).__name__}: {str(e)[:160]}"
    rec["license_note"] = "授權條款需人工閱讀 FinMind 網站;本探測不抓條款頁"
    out["history"]["finmind"] = rec


def check_repo_reuse(out):
    """法人 / 外資持股 / 交易日曆 / 名單:只掃 repo 內現有檔案(不連網)。"""
    rec = {"institutional_daily": [], "foreign_holding": [], "trading_calendar": [], "watchlists": []}
    try:
        for root, _, files in os.walk("."):
            if any(p in root for p in ("/.git", "node_modules", "backtest_out")):
                continue
            for f in files:
                p = os.path.join(root, f)
                if not f.endswith((".py", ".json", ".csv", ".txt")) or os.path.getsize(p) > 5_000_000:
                    continue
                try:
                    head = open(p, encoding="utf-8", errors="ignore").read(200_000)
                except Exception:
                    continue
                if re.search(r"fund/T86|3itrade_hedge|三大法人買賣超", head):
                    rec["institutional_daily"].append(p)
                if re.search(r"MI_QFIIS|tpex_3insti_qfii|外資持股比", head):
                    rec["foreign_holding"].append(p)
                if re.search(r"holiday|休市|trading_?days|交易日曆", head, re.I) and f.endswith(".py"):
                    rec["trading_calendar"].append(p)
                if re.search(r"watchlist|持股", f, re.I):
                    rec["watchlists"].append(p)
        dj = sorted(x for x in os.listdir(".") if re.match(r"daily_\d{8}\.json$", x))
        if dj:
            try:
                d = json.load(open(dj[-1], encoding="utf-8"))
                rec["gas_daily_json"] = {"latest": dj[-1], "count": len(dj), "top_keys": list(d.keys())[:25]}
            except Exception as e:
                rec["gas_daily_json"] = {"error": str(e)[:120]}
    except Exception as e:
        rec["error"] = f"{type(e).__name__}: {str(e)[:160]}"
    rec["finding"] = ("repo 內有逐檔逐日法人買賣超資料" if rec["institutional_daily"]
                      else "repo 內沒有逐檔逐日的 T86/TPEx 法人資料(由 GAS InstitutionalConsensus.gs 抓取,存在 Google 試算表)")
    rec["foreign_finding"] = ("repo 內有外資持股比例資料" if rec["foreign_holding"]
                              else "repo 內沒有外資持股比例資料(由 GAS ForeignHolding.gs 抓取,存在 Google 試算表)")
    out["repo_reuse"] = rec


def main():
    out = {"checked_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
           "checked_at_tpe": dt.datetime.now(TPE).strftime("%Y-%m-%d %H:%M:%S"),
           "open_data": {}, "history": {}, "update_timing": {}, "repo_reuse": {}, "samples": {},
           "robots": {}, "verdict": "unknown", "notes": notes, "requests_used": 0}
    parsed = None
    for fn in (lambda: check_open_data(out),):
        try:
            parsed = fn()
        except Exception as e:
            notes.append(f"open_data 例外:{type(e).__name__}: {str(e)[:160]}")
    for fn in (lambda: check_samples(out, parsed), lambda: check_query_page(out),
               lambda: check_finmind(out), lambda: check_repo_reuse(out)):
        try:
            fn()
        except Exception as e:
            notes.append(f"檢查項例外:{type(e).__name__}: {str(e)[:160]}")
    od = out["open_data"]
    out["update_timing"] = {"fetched_at_tpe": out["checked_at_tpe"], "data_dates": od.get("data_dates"),
                            "note": "單次探測只能記錄當下的資料日期;要判斷更新時間需在週五晚上、週六、週一各跑一次比對"}
    out["robots"] = {h: rec for h, (_, rec) in _robots.items()}
    fm = out["history"].get("finmind", {})
    if not od.get("usable"):
        blocked = any(a.get("status") in BLOCK or a.get("error", "").startswith("robots") for a in od.get("attempts", []))
        verdict = "blocked" if blocked else "unreachable"
    elif fm.get("rows"):
        verdict = "ok_with_history"
    else:
        verdict = "ok_latest_only"
    out["verdict"] = verdict
    out["requests_used"] = _st["n"]
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
