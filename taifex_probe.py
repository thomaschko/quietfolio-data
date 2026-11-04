# -*- coding: utf-8 -*-
"""
taifex_probe.py — 期交所期貨 / 選擇權資料可得性探測(2026-11-04,共識頁 Phase 0;只查不存)
============================================================
目的(使用者 11/04):用期貨 / 選擇權觀察「散戶行為(微型臺指)」與「外資動態」,判斷市場是否偏熱或過度看空。
要確認的事:
  1. robots.txt(www.taifex.com.tw、openapi.taifex.com.tw)
  2. OpenAPI 有哪些端點(swagger)、欄位、是否只有最新一日
  3. 歷史下載(CSV)能否指定日期區間、可回溯多久、單次區間上限、回應編碼
     - 三大法人 期貨分契約(futContractsDateDown):TX 臺指期、MTX 小臺、TMF 微臺
     - 三大法人 選擇權買賣權分計(callsAndPutsDateDown):TXO
     - 期貨每日交易行情(futDataDown):TMF / TX 各月份未平倉 → 全市場未平倉
     - 臺指選擇權 Put/Call Ratio(pcRatioDown)
     - 大額交易人未沖銷部位(largeTraderFutDown)
  4. 限流:請求間隔 3 秒、誠實 User-Agent;403 / 429 / 驗證碼 / robots 不允許 → 停止,不重試、不換來源。
結果寫 taifex_probe_result.json(每個端點:HTTP、內容型別、前幾行、列數、日期範圍),不存資料本身。
"""
import datetime as dt
import io
import json
import re
import time
from urllib import robotparser
from urllib.parse import urlsplit

import requests

UA = "quietfolio-taifex-probe/1.0 (read-only research; github.com/thomaschko/quietfolio-data)"
GAP = 3.0
OUT = "taifex_probe_result.json"
TPE = dt.timezone(dt.timedelta(hours=8))


class Stop(Exception):
    pass


class Http:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA})
        self.last = {}
        self.robots = {}
        self.log = []

    def _wait(self, host):
        if host in self.last:
            w = GAP - (time.time() - self.last[host])
            if w > 0:
                time.sleep(w)

    def robots_ok(self, url):
        p = urlsplit(url)
        if p.netloc not in self.robots:
            self._wait(p.netloc)
            try:
                r = self.s.get("%s://%s/robots.txt" % (p.scheme, p.netloc), timeout=30)
            finally:
                self.last[p.netloc] = time.time()
            info = {"status": r.status_code, "text": r.text[:600] if r.status_code == 200 else None}
            if r.status_code == 200:
                rp = robotparser.RobotFileParser()
                rp.parse(r.text.splitlines())
                self.robots[p.netloc] = (rp, info)
            elif r.status_code == 404:
                self.robots[p.netloc] = (None, info)
            else:
                self.robots[p.netloc] = (False, info)
        rp = self.robots[p.netloc][0]
        if rp is None:
            return True
        if rp is False:
            return False
        return rp.can_fetch(UA, url) and rp.can_fetch("*", url)

    def req(self, method, url, data=None):
        if not self.robots_ok(url):
            raise Stop("robots.txt 不允許:" + url)
        host = urlsplit(url).netloc
        self._wait(host)
        try:
            r = self.s.request(method, url, data=data, timeout=60)
        finally:
            self.last[host] = time.time()
        if r.status_code in (403, 429):
            raise Stop("HTTP %s:%s" % (r.status_code, url))
        head = r.content[:400].decode("utf-8", errors="replace").lower()
        if "captcha" in head or "驗證碼" in head:
            raise Stop("驗證碼頁:" + url)
        return r


def decode(r):
    for enc in ("utf-8-sig", "big5", "cp950"):
        try:
            return r.content.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return r.content.decode("utf-8", errors="replace"), "utf-8(replace)"


def csv_summary(r):
    text, enc = decode(r)
    lines = [x for x in text.splitlines() if x.strip()]
    dates = sorted({m.group(0) for m in re.finditer(r"20\d\d/\d\d/\d\d", text)})
    return {"status": r.status_code, "ctype": r.headers.get("Content-Type"), "encoding": enc, "bytes": len(r.content),
            "lines": len(lines), "head": lines[:4], "tail": lines[-2:] if len(lines) > 4 else [],
            "dates": [dates[0], dates[-1], len(dates)] if dates else None,
            "looks_html": text.lstrip()[:15].lower().startswith(("<!doctype", "<html"))}


def ymd(d):
    return d.strftime("%Y/%m/%d")


def main():
    http = Http()
    out = {"generated": dt.datetime.now(TPE).isoformat(timespec="seconds"), "ua": UA, "gap_sec": GAP, "results": {}}
    today = dt.datetime.now(TPE).date()
    res = out["results"]
    try:
        # ---- OpenAPI swagger
        sw = "https://openapi.taifex.com.tw/swagger.json"
        try:
            r = http.req("GET", sw)
            j = r.json() if r.status_code == 200 else None
            paths = sorted((j or {}).get("paths", {}).keys())
            res["openapi_swagger"] = {"status": r.status_code, "n_paths": len(paths), "paths": paths}
        except Stop:
            raise
        except Exception as e:
            res["openapi_swagger"] = {"error": repr(e)[:300]}
        # 挑跟需求相關的端點各抓一次看欄位與日期
        want = re.compile(r"(?i)(Institutional|PutCall|LargeTrader|DailyMarketReport|OpenInterest)")
        for p in [x for x in res.get("openapi_swagger", {}).get("paths", []) if want.search(x)][:14]:
            try:
                r = http.req("GET", "https://openapi.taifex.com.tw/v1" + p if not p.startswith("/v1") else "https://openapi.taifex.com.tw" + p)
                try:
                    j = r.json()
                except ValueError:
                    j = None
                rows = j if isinstance(j, list) else []
                keys = list(rows[0].keys()) if rows and isinstance(rows[0], dict) else None
                ds = sorted({str(v) for row in rows[:5000] if isinstance(row, dict) for k, v in row.items() if "Date" in k or "日期" in k})
                res["openapi" + p] = {"status": r.status_code, "rows": len(rows), "keys": keys, "sample": rows[0] if rows else None,
                                      "dates": [ds[0], ds[-1], len(ds)] if ds else None}
            except Stop:
                raise
            except Exception as e:
                res["openapi" + p] = {"error": repr(e)[:300]}

        # ---- 歷史 CSV 下載(POST 表單)
        base = "https://www.taifex.com.tw/cht/3/"
        end = today
        tries = [
            ("futContractsDateDown_TMF_1m", "futContractsDateDown", {"queryStartDate": ymd(end - dt.timedelta(days=30)), "queryEndDate": ymd(end), "commodityId": "TMF"}),
            ("futContractsDateDown_TX_1m", "futContractsDateDown", {"queryStartDate": ymd(end - dt.timedelta(days=30)), "queryEndDate": ymd(end), "commodityId": "TXF"}),
            ("futContractsDateDown_TMF_2024_08", "futContractsDateDown", {"queryStartDate": "2024/08/01", "queryEndDate": "2024/08/31", "commodityId": "TMF"}),
            ("futContractsDateDown_TX_1y", "futContractsDateDown", {"queryStartDate": ymd(end - dt.timedelta(days=365)), "queryEndDate": ymd(end), "commodityId": "TXF"}),
            ("callsAndPutsDateDown_TXO_1m", "callsAndPutsDateDown", {"queryStartDate": ymd(end - dt.timedelta(days=30)), "queryEndDate": ymd(end), "commodityId": "TXO"}),
            ("futDataDown_TMF_1m", "futDataDown", {"down_type": "1", "commodity_id": "TMF", "queryStartDate": ymd(end - dt.timedelta(days=30)), "queryEndDate": ymd(end)}),
            ("futDataDown_TX_1m", "futDataDown", {"down_type": "1", "commodity_id": "TX", "queryStartDate": ymd(end - dt.timedelta(days=30)), "queryEndDate": ymd(end)}),
            ("pcRatioDown_1m", "pcRatioDown", {"queryStartDate": ymd(end - dt.timedelta(days=30)), "queryEndDate": ymd(end)}),
            ("pcRatioDown_2y", "pcRatioDown", {"queryStartDate": ymd(end - dt.timedelta(days=730)), "queryEndDate": ymd(end)}),
            ("largeTraderFutDown_1m", "largeTraderFutDown", {"queryStartDate": ymd(end - dt.timedelta(days=30)), "queryEndDate": ymd(end)}),
        ]
        for key, path, form in tries:
            try:
                r = http.req("POST", base + path, data=form)
                res[key] = csv_summary(r)
                res[key]["form"] = form
            except Stop:
                raise
            except Exception as e:
                res[key] = {"error": repr(e)[:300], "form": form}
    except Stop as e:
        out["stopped"] = str(e)
    out["robots"] = {h: v[1] for h, v in http.robots.items()}
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(json.dumps({"stopped": out.get("stopped"), "keys": list(res)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
