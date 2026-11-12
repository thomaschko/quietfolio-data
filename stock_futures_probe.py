# -*- coding: utf-8 -*-
"""
stock_futures_probe.py — 個股期貨籌碼探測(2026-11-12,只讀)
============================================================
目的:題材擁擠度想加「個股期貨未平倉相對歷史高檔」。先確認期交所公開資料:
  A. 個股期貨契約 ↔ 標的股票代號對照(stockLists 頁、openapi)
  B. 每日期貨行情 futDataDown:commodity_id 留空 / all 能不能一次拿到全部個股期貨的未沖銷契約數;回溯 1、2 年前
  C. 三大法人(futContractsDateDown)有沒有個股期貨分項
  D. openapi.taifex.com.tw 候選端點
只印結構(欄位、列數、範例代碼)到 job log,不存檔、不 commit。
規則同 taifex_hist.py:先讀 robots.txt;同一主機間隔 ≥ 3 秒;誠實 User-Agent;403 / 429 / 驗證碼 → 記錄並跳過該項。
"""
import csv
import io
import json
import re
import sys
import time
from urllib import robotparser
from urllib.parse import urlsplit

import requests

UA = "quietfolio-taifex/1.0 (read-only research; github.com/thomaschko/quietfolio-data)"
GAP = 3.0
S = requests.Session()
S.headers.update({"User-Agent": UA})
_last, _robots = {}, {}
OUT = []


def robots_ok(url):
    host = urlsplit(url).scheme + "://" + urlsplit(url).netloc
    if host not in _robots:
        try:
            r = S.get(host + "/robots.txt", timeout=30)
            rp = robotparser.RobotFileParser()
            rp.parse(r.text.splitlines() if r.status_code == 200 else [])
            _robots[host] = rp
        except requests.RequestException:
            _robots[host] = None
    rp = _robots[host]
    return True if rp is None else rp.can_fetch(UA, url)


def req(method, url, data=None, params=None):
    host = urlsplit(url).netloc
    w = GAP - (time.time() - _last.get(host, 0))
    if w > 0:
        time.sleep(w)
    try:
        return S.request(method, url, data=data, params=params, timeout=90)
    except requests.RequestException as e:
        return e
    finally:
        _last[host] = time.time()


def rec(name, **kv):
    row = dict(name=name, **kv)
    OUT.append(row)
    print(json.dumps(row, ensure_ascii=False)[:1800])


def text_of(r):
    raw = r.content
    for enc in ("utf-8-sig", "big5", "cp950"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def probe_stocklists():
    url = "https://www.taifex.com.tw/cht/2/stockLists"
    if not robots_ok(url):
        return rec("stockLists", status="robots_disallow")
    r = req("GET", url)
    if isinstance(r, Exception):
        return rec("stockLists", status="error", err=type(r).__name__)
    t = r.text
    rows = re.findall(r"(?is)<tr[^>]*>(.*?)</tr>", t)
    cells = [[re.sub(r"<[^>]+>|\s+", " ", c).strip() for c in re.findall(r"(?is)<t[dh][^>]*>(.*?)</t[dh]>", x)] for x in rows]
    cells = [c for c in cells if len(c) >= 3]
    rec("stockLists 個股期貨對照", status=r.status_code, bytes=len(r.content), n_rows=len(cells),
        header=cells[0][:12] if cells else None, sample=cells[1:4] if len(cells) > 1 else None,
        captcha=bool(re.search(r"(?i)captcha|驗證碼", t[:20000])))


def probe_futdata(date, commodity):
    url = "https://www.taifex.com.tw/cht/3/futDataDown"
    if not robots_ok(url):
        return rec("futDataDown", status="robots_disallow")
    form = {"down_type": "1", "commodity_id": commodity, "queryStartDate": date, "queryEndDate": date}
    r = req("POST", url, data=form)
    if isinstance(r, Exception):
        return rec("futDataDown %s %s" % (date, commodity or "(空)"), status="error", err=type(r).__name__)
    t = text_of(r)
    rows = list(csv.reader(io.StringIO(t)))
    hdr = rows[0] if rows else []
    body = [x for x in rows[1:] if len(x) > 5]
    codes = sorted({x[1].strip() for x in body})
    oi_col = next((i for i, h in enumerate(hdr) if "未沖銷" in h), None)
    rec("futDataDown %s commodity=%s" % (date, commodity or "(空)"), status=r.status_code, ctype=r.headers.get("content-type", "")[:40],
        bytes=len(r.content), header=hdr[:20], n_rows=len(body), n_contracts=len(codes), contracts_sample=codes[:40],
        oi_col=oi_col, html=t.lstrip()[:15].lower().startswith("<!doctype") or "<html" in t[:300].lower(),
        sample_row=body[0][:20] if body else None)


def probe_inst(date, commodity):
    url = "https://www.taifex.com.tw/cht/3/futContractsDateDown"
    if not robots_ok(url):
        return rec("futContractsDateDown", status="robots_disallow")
    r = req("POST", url, data={"queryStartDate": date, "queryEndDate": date, "commodityId": commodity})
    if isinstance(r, Exception):
        return rec("futContractsDateDown %s" % commodity, status="error", err=type(r).__name__)
    t = text_of(r)
    rows = [x for x in csv.reader(io.StringIO(t)) if x]
    rec("futContractsDateDown(三大法人)%s commodity=%s" % (date, commodity or "(空)"), status=r.status_code, bytes=len(r.content),
        header=rows[0][:16] if rows else None, n_rows=len(rows) - 1,
        products=sorted({x[1] for x in rows[1:] if len(x) > 1})[:60])


def probe_openapi():
    for ep in ("DailyMarketReportFut", "DailyFuturesTradingStatisticsOfStockFutures", "StockFuturesList",
               "SSFLists", "MarketDataOfMajorInstitutionalTradersDetailsOfFuturesContractsBytheDate"):
        url = "https://openapi.taifex.com.tw/v1/" + ep
        if not robots_ok(url):
            rec("openapi " + ep, status="robots_disallow")
            continue
        r = req("GET", url)
        if isinstance(r, Exception):
            rec("openapi " + ep, status="error", err=type(r).__name__)
            continue
        info = {"status": r.status_code, "bytes": len(r.content), "ctype": r.headers.get("content-type", "")[:40]}
        try:
            j = r.json()
            if isinstance(j, list):
                info["n_rows"] = len(j)
                info["keys"] = list(j[0].keys())[:25] if j and isinstance(j[0], dict) else None
                if j and isinstance(j[0], dict):
                    k = next((k for k in j[0] if re.search(r"(?i)contract|commodity|契約|代號", k)), None)
                    if k:
                        info["contract_sample"] = sorted({str(x.get(k)) for x in j})[:40]
        except ValueError:
            info["head"] = re.sub(r"\s+", " ", r.text[:200])
        rec("openapi " + ep, **info)


def main():
    probe_stocklists()
    for d in ("2026/11/11", "2025/11/11", "2024/11/11"):
        probe_futdata(d, "")
    probe_futdata("2026/11/11", "all")
    probe_futdata("2026/11/11", "CDF")          # 常見的台積電股票期貨代碼(以 stockLists 結果為準)
    probe_inst("2026/11/11", "")
    probe_inst("2026/11/11", "STF")
    probe_openapi()
    print("===RESULT_JSON===")
    print(json.dumps(OUT, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
