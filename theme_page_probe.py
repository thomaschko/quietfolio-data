# -*- coding: utf-8 -*-
"""
theme_page_probe.py — 題材頁面(趨勢檢視版)Phase 0 探測(2026-11-09,只讀)
============================================================
確認第 3 節「待驗證」來源的可得性、欄位與回溯期間,結果只印「結構」到 job log(不存全文):
  A. 個股日本益比:證交所 openapi BWIBBU_ALL(當日)、BWIBBU_d(依日期,歷史)、櫃買 openapi / 歷史頁
  B. 季財報(官方):MOPS opendata t187ap06(損益)/ t187ap07(資產負債,找合約負債)/ 現金流量候選
  C. MOPS 歷史彙總(t163sb04 / t163sb05 / t164sb05 等,先看 robots)
  D. FinMind(只有 Secret FINMIND_TOKEN 存在時):月營收、財報、現金流量、資產負債(合約負債)、PER 的回溯期數與科目名
每個網域先讀 robots.txt;403 / 429 / 驗證碼 → 記錄後跳過,不繞過。請求間隔 ≥ 2 秒。
"""
import csv
import io
import json
import os
import re
import sys
import time
from urllib import robotparser
from urllib.parse import urlsplit

import requests

UA = "Mozilla/5.0 (quietfolio-research probe; github.com/thomaschko/quietfolio-data)"
GAP = 2.0
S = requests.Session()
S.headers.update({"User-Agent": UA})
_last = [0.0]
_robots = {}
OUT = {"generated": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "checks": []}


def robots_ok(url):
    host = urlsplit(url).scheme + "://" + urlsplit(url).netloc
    if host not in _robots:
        try:
            r = S.get(host + "/robots.txt", timeout=20)
            rp = robotparser.RobotFileParser()
            rp.parse(r.text.splitlines() if r.status_code == 200 else [])
            _robots[host] = (rp, r.status_code)
        except requests.RequestException as e:
            _robots[host] = (None, type(e).__name__)
    rp, _ = _robots[host]
    return True if rp is None else rp.can_fetch(UA, url)


def get(url, method="GET", data=None, params=None, headers=None):
    w = GAP - (time.time() - _last[0])
    if w > 0:
        time.sleep(w)
    try:
        if method == "POST":
            return S.post(url, data=data, headers=headers, timeout=40)
        return S.get(url, params=params, headers=headers, timeout=40)
    except requests.RequestException as e:
        return e
    finally:
        _last[0] = time.time()


def record(name, url, **kv):
    row = {"name": name, "url": re.sub(r"token=[^&]+", "token=***", url)}
    row.update(kv)
    OUT["checks"].append(row)
    print(json.dumps(row, ensure_ascii=False)[:1500])


def probe_json(name, url, params=None, list_key=None):
    if not robots_ok(url):
        return record(name, url, status="robots_disallow")
    r = get(url, params=params)
    if isinstance(r, Exception):
        return record(name, url, status="error", err=type(r).__name__)
    info = {"status": r.status_code, "ctype": r.headers.get("content-type", "")[:40], "bytes": len(r.content)}
    if r.status_code != 200:
        return record(name, url, **info)
    try:
        j = r.json()
    except ValueError:
        info["captcha_or_html"] = bool(re.search(r"(?i)captcha|驗證碼", r.text[:5000]))
        info["head"] = re.sub(r"\s+", " ", r.text[:160])
        return record(name, url, **info)
    rows = j if isinstance(j, list) else (j.get(list_key) if list_key else None)
    if isinstance(j, dict):
        info["keys"] = list(j.keys())[:15]
        for k in ("fields", "title", "date", "stat"):
            if k in j:
                info[k] = j[k] if not isinstance(j[k], list) else j[k][:20]
        if rows is None and isinstance(j.get("data"), list):
            rows = j["data"]
        if rows is None and isinstance(j.get("tables"), list) and j["tables"]:
            t0 = j["tables"][0]
            info["t0_fields"] = (t0.get("fields") or [])[:20]
            info["t0_title"] = t0.get("title")
            rows = t0.get("data")
    if isinstance(rows, list):
        info["n_rows"] = len(rows)
        if rows:
            first = rows[0]
            info["first_keys"] = list(first.keys())[:30] if isinstance(first, dict) else None
            info["first_len"] = len(first) if isinstance(first, list) else None
    return record(name, url, **info)


def probe_csv(name, url, want=()):
    if not robots_ok(url):
        return record(name, url, status="robots_disallow")
    r = get(url)
    if isinstance(r, Exception):
        return record(name, url, status="error", err=type(r).__name__)
    info = {"status": r.status_code, "bytes": len(r.content)}
    if r.status_code != 200:
        return record(name, url, **info)
    txt = r.content.decode("utf-8-sig", errors="replace")
    rows = list(csv.reader(io.StringIO(txt)))
    hdr = rows[0] if rows else []
    info.update({"n_rows": len(rows) - 1, "header": hdr[:40]})
    info["has"] = {w: any(w in h for h in hdr) for w in want}
    if len(rows) > 1:
        yi = [i for i, h in enumerate(hdr) if h in ("年度", "出表日期", "資料年月", "季別")]
        info["period_cols"] = {hdr[i]: rows[1][i] for i in yi}
    return record(name, url, **info)


def section_pe():
    probe_json("TWSE 本益比 當日 openapi", "https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL")
    for d in ("20231102", "20251103", "20261106"):
        probe_json("TWSE 本益比 依日期 " + d, "https://www.twse.com.tw/rwd/zh/afterTrading/BWIBBU_d",
                   params={"date": d, "selectType": "ALL", "response": "json"})
    probe_json("TWSE 本益比 個股月份 2330", "https://www.twse.com.tw/rwd/zh/afterTrading/BWIBBU",
               params={"date": "20231101", "stockNo": "2330", "response": "json"})
    probe_json("TPEx 本益比 當日 openapi", "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_peratio_analysis")
    for d in ("2023/11/02", "2026/11/06"):
        probe_json("TPEx 本益比 依日期 " + d, "https://www.tpex.org.tw/www/zh-tw/afterTrading/peQryDate",
                   params={"date": d, "response": "json"})


def section_mops_opendata():
    want = ("營業毛利", "營業利益", "合約負債", "營業活動", "不動產、廠房及設備", "年度", "季別")
    for f in ("t187ap06_L_ci", "t187ap06_O_ci", "t187ap07_L_ci", "t187ap07_O_ci", "t187ap08_L_ci", "t187ap10_L", "t187ap11_L"):
        probe_csv("MOPS opendata " + f, "https://mopsfin.twse.com.tw/opendata/%s.csv" % f, want)
    for f in ("t187ap07_L_ci", "t187ap08_L_ci"):
        probe_json("TWSE openapi " + f, "https://openapi.twse.com.tw/v1/opendata/" + f)
    probe_json("TPEx openapi 資產負債 一般業", "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap07_O_ci")


def section_mops_hist():
    for host in ("https://mopsov.twse.com.tw", "https://mops.twse.com.tw"):
        robots_ok(host + "/mops/web/t163sb05")
        rp, code = _robots[host]
        record("robots " + host, host + "/robots.txt", status=code,
               t163sb05_ok=(rp.can_fetch(UA, host + "/mops/web/ajax_t163sb05") if rp else None),
               t164sb05_ok=(rp.can_fetch(UA, host + "/mops/web/ajax_t164sb05") if rp else None))
    url = "https://mopsov.twse.com.tw/mops/web/ajax_t163sb05"
    if robots_ok(url):
        r = get(url, method="POST", data={"encodeURIComponent": "1", "step": "1", "firstin": "1", "off": "1",
                                           "isQuery": "Y", "TYPEK": "sii", "year": "112", "season": "02"})
        if isinstance(r, Exception):
            record("MOPS 歷史 資產負債彙總 t163sb05 112Q2", url, status="error", err=type(r).__name__)
        else:
            t = r.text
            record("MOPS 歷史 資產負債彙總 t163sb05 112Q2", url, status=r.status_code, bytes=len(r.content),
                   captcha=bool(re.search(r"(?i)captcha|驗證碼", t[:20000])), has_contract_liab="合約負債" in t,
                   n_tables=t.count("<table"), too_frequent="查詢過於頻繁" in t or "頻繁" in t[:3000])
    else:
        record("MOPS 歷史 t163sb05", url, status="robots_disallow")
    url = "https://mopsov.twse.com.tw/mops/web/ajax_t164sb05"
    if robots_ok(url):
        r = get(url, method="POST", data={"encodeURIComponent": "1", "step": "1", "firstin": "1", "off": "1",
                                           "queryName": "co_id", "inpuType": "co_id", "TYPEK": "all", "isnew": "false",
                                           "co_id": "2330", "year": "112", "season": "02"})
        if isinstance(r, Exception):
            record("MOPS 歷史 現金流量 t164sb05 2330 112Q2", url, status="error", err=type(r).__name__)
        else:
            t = r.text
            record("MOPS 歷史 現金流量 t164sb05 2330 112Q2", url, status=r.status_code, bytes=len(r.content),
                   captcha=bool(re.search(r"(?i)captcha|驗證碼", t[:20000])),
                   has_ocf="營業活動之淨現金流入" in t, has_capex="取得不動產、廠房及設備" in t,
                   too_frequent="頻繁" in t[:3000])
    else:
        record("MOPS 歷史 t164sb05", url, status="robots_disallow")


def section_finmind():
    tok = os.environ.get("FINMIND_TOKEN", "").strip()
    if not tok:
        return record("FinMind", "-", status="skipped_no_secret")
    url = "https://api.finmindtrade.com/api/v4/data"
    for ds, start in (("TaiwanStockMonthRevenue", "2019-01-01"), ("TaiwanStockFinancialStatements", "2018-01-01"),
                      ("TaiwanStockCashFlowsStatement", "2018-01-01"), ("TaiwanStockBalanceSheet", "2018-01-01"),
                      ("TaiwanStockPER", "2015-01-01")):
        r = get(url, params={"dataset": ds, "data_id": "2330", "start_date": start},
                headers={"Authorization": "Bearer " + tok})
        if isinstance(r, Exception) or r.status_code != 200:
            record("FinMind " + ds, url, status=getattr(r, "status_code", type(r).__name__))
            continue
        rows = r.json().get("data") or []
        dates = sorted({x.get("date") for x in rows if x.get("date")})
        info = {"status": 200, "n_rows": len(rows), "first_date": dates[:1], "last_date": dates[-1:], "n_dates": len(dates),
                "keys": list(rows[0].keys()) if rows else []}
        if ds == "TaiwanStockMonthRevenue" and rows:
            info["sample_last"] = {k: rows[-1].get(k) for k in ("date", "revenue_year", "revenue_month")}
        if "type" in (rows[0] if rows else {}):
            types = sorted({x["type"] for x in rows})
            info["n_types"] = len(types)
            info["types_match"] = [t for t in types if re.search(r"(?i)gross|operatingincome|revenue|contract|operating|propertyandplant|capital", t)][:30]
            ol = sorted({x.get("origin_name", "") for x in rows if re.search("合約負債|營業活動|不動產、廠房", x.get("origin_name", ""))})
            info["origin_match"] = ol[:15]
        record("FinMind " + ds, url, **info)
    r = get("https://api.finmindtrade.com/api/v4/user_info", params={"token": tok})
    if not isinstance(r, Exception) and r.status_code == 200:
        j = r.json()
        record("FinMind 額度", "user_info", level=j.get("level"), api_request_limit=j.get("api_request_limit"))


def main():
    for f in (section_pe, section_mops_opendata, section_mops_hist, section_finmind):
        try:
            f()
        except Exception as e:      # 探測:單一段落失敗不影響其他段落
            record(f.__name__, "-", status="section_error", err="%s: %s" % (type(e).__name__, str(e)[:200]))
    print("===RESULT_JSON===")
    print(json.dumps(OUT, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
