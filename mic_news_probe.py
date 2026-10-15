# -*- coding: utf-8 -*-
"""
mic_news_probe.py — 資策會 MIC 產業新聞 API 一次性驗證(2026-10-15)

只做驗證,不改任何檔案:
  1. POST ApiNewsALL.ashx 第1頁,印 HTTP 狀態與前3筆原始 JSON
  2. 403/非 JSON 時,加 Referer + 一般 User-Agent 重試一次;仍失敗就印狀態碼與回應內文後結束
     (不走 apiLogIn.ashx 或任何登入流程)
  3. 抓到第2頁湊滿20筆,印 news_date / new_cate / title / link,並統計:
     日期是否遞減、最新日期距今幾天、link 是 MIC 自家或外部、標題是否像具體產業事件
用法:python mic_news_probe.py
"""
import re
import json
import datetime as dt
from urllib.parse import urlparse

import requests

API = "https://mic.iii.org.tw/aisp/ApiNewsALL.ashx"
PAGE = "https://mic.iii.org.tw/aisp/industry-news"
BASE_HEADERS = {"Content-Type": "application/json; charset=utf-8"}
RETRY_HEADERS = {**BASE_HEADERS, "Referer": PAGE,
                 "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                               "(KHTML, like Gecko) Chrome/120 Safari/537.36"}


def post(st, ed, pg, headers):
    r = requests.post(API, data=json.dumps({"St": str(st), "Ed": str(ed), "Pg": str(pg)}),
                      headers=headers, timeout=20)
    try:
        data = r.json()
    except ValueError:
        data = None
    return r, data


def parse_date(s):
    s = str(s or "").strip()
    m = re.match(r"(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})", s)
    if m:
        return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.match(r"(\d{2,3})[/\-.](\d{1,2})[/\-.](\d{1,2})", s)       # 民國年
    if m:
        return dt.date(int(m.group(1)) + 1911, int(m.group(2)), int(m.group(3)))
    return None


def main():
    print("=== 步驟1-1:POST 第1頁(只帶 Content-Type)===")
    headers = BASE_HEADERS
    r, data = post(1, 10, 1, headers)
    print("HTTP", r.status_code, "Content-Type:", r.headers.get("Content-Type"))
    if r.status_code != 200 or not isinstance(data, list):
        print("回應前500字:", r.text[:500])
        print("\n=== 步驟1-2:加 Referer + User-Agent 重試一次 ===")
        headers = RETRY_HEADERS
        r, data = post(1, 10, 1, headers)
        print("HTTP", r.status_code, "Content-Type:", r.headers.get("Content-Type"))
        if r.status_code != 200 or not isinstance(data, list):
            print("回應前1000字:", r.text[:1000])
            print("\n結論:仍失敗,停止(不嘗試登入或其他繞過方式)。")
            return
    print("前3筆原始 JSON:")
    print(json.dumps(data[:3], ensure_ascii=False, indent=2))

    rows = list(data)
    if len(rows) < 20:
        r2, d2 = post(11, 20, 2, headers)
        print(f"\n第2頁:HTTP {r2.status_code},{len(d2) if isinstance(d2, list) else '非JSON'} 筆")
        if isinstance(d2, list):
            rows += d2
    rows = rows[:20]

    print("\n=== 步驟1-3:前20筆 ===")
    dates, hosts = [], {}
    for i, x in enumerate(rows, 1):
        d = parse_date(x.get("news_date"))
        dates.append(d)
        host = urlparse(x.get("link") or "").netloc or "(相對路徑/空)"
        hosts[host] = hosts.get(host, 0) + 1
        print(f"{i:2d}. {x.get('news_date')} | {x.get('new_cate')} | {x.get('title')}")
        print(f"     link: {x.get('link')}")
    valid = [d for d in dates if d]
    today = dt.date.today()
    print("\n=== 統計 ===")
    print("總頁數 pg:", rows[0].get("pg") if rows else None)
    if valid:
        print(f"最新日期 {max(valid)}(距今 {(today - max(valid)).days} 天),最舊 {min(valid)}")
        print("日期是否遞減(非嚴格):", all(a >= b for a, b in zip(valid, valid[1:])))
    else:
        print("news_date 無法解析,原始值例:", [x.get("news_date") for x in rows[:3]])
    print("link 網域分布:", hosts)
    print("分類分布:", {c: sum(1 for x in rows if x.get("new_cate") == c) for c in {x.get("new_cate") for x in rows}})
    print("\n(標題是否為具體產業事件,請人工看上面20筆;腳本不自動判斷)")


if __name__ == "__main__":
    main()
