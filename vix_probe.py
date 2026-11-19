# -*- coding: utf-8 -*-
"""
vix_probe.py — 臺指選擇權波動率指數(VIXTWN)資料來源探測(2026-11-19,一次性,只讀,不存檔)v2026-11-19-vixmood
============================================================
規格:使用者「乖離追蹤加入市場氛圍 — VIXTWN / IVP / IVR」第 1 節。
  1. 期交所 GetStockDayPrices(TAIWANVIX)歷史日收盤:endDate = 今天 / 前一日 各打一次,印出筆數、首末筆;
     完整序列以精簡 JSON 印在 job log(公開行情,用來在本機驗證 IVP/IVR 與事件重播)。
  2. 當月檔 YYYYMMnew.txt(Big5):印前 20 行與末 5 行,確認欄位。
  3. 加權指數(FinMind TaiwanStockTotalReturnIndex TAIEX,與乖離追蹤同源)自 2021-06 起的收盤,給事件 T3 重播用。
限速:請求間隔 3 秒;403/429 → 停止。
"""
import datetime as dt
import json
import os
import sys
import time

import requests

UA = {"User-Agent": "quietfolio-vix-probe/1.0 (read-only research)"}
BASE = "https://www.taifex.com.tw/indes/index.aspx/GetStockDayPrices"


def hist(end):
    r = requests.post(BASE, json={"syid": "TAIWANVIX", "flag": "MS", "startDate": "2021/07/01", "endDate": end},
                      headers=dict(UA, **{"Content-Type": "application/json; charset=utf-8"}), timeout=60)
    print("hist end=%s http %s len %d" % (end, r.status_code, len(r.content)))
    if r.status_code != 200:
        print(r.text[:300]); return None
    try:
        d = json.loads(r.json()["d"])
    except Exception as e:
        print("parse fail", type(e).__name__, r.text[:300]); return None
    td = d.get("TrendData") or []
    print(" keys", list(d.keys())[:10], "n", len(td), "first", td[:2], "last", td[-3:])
    return td


def main():
    now = dt.datetime.utcnow() + dt.timedelta(hours=8)
    today = now.strftime("%Y/%m/%d")
    prev = (now - dt.timedelta(days=1)).strftime("%Y/%m/%d")
    print("TPE now", now.isoformat(timespec="minutes"))
    full = hist(today)
    time.sleep(3)
    hist(prev)
    time.sleep(3)
    for ym in sorted({now.strftime("%Y%m"), (now.replace(day=1) - dt.timedelta(days=1)).strftime("%Y%m")}):
        url = "https://www.taifex.com.tw/file/taifex/Dailydownload/vix/log2data/%snew.txt" % ym
        r = requests.get(url, headers=UA, timeout=60)
        print("month", ym, "http", r.status_code, "bytes", len(r.content), r.headers.get("content-type"))
        if r.status_code == 200:
            t = r.content.decode("big5", errors="replace").splitlines()
            print(" lines", len(t))
            for i, l in enumerate(t[:20]): print(" H%02d|%s" % (i, l[:160]))
            for l in t[-5:]: print(" T  |%s" % l[:160])
        time.sleep(3)
    tok = os.environ.get("FINMIND_TOKEN", "")
    if tok:
        r = requests.get("https://api.finmindtrade.com/api/v4/data", params={"dataset": "TaiwanStockTotalReturnIndex", "data_id": "TAIEX", "start_date": "2021-06-01"},
                         headers={"Authorization": "Bearer " + tok}, timeout=60)
        rows = (r.json().get("data") or []) if r.status_code == 200 else []
        print("TAIEX http", r.status_code, "n", len(rows))
        print("TAIEX_JSON=" + json.dumps([[x["date"].replace("-", ""), x["price"]] for x in rows], separators=(",", ":")))
    if full:
        print("VIX_JSON=" + json.dumps([[x["Time"], x["Price"]] for x in full], separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
