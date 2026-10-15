# -*- coding: utf-8 -*-
"""yahoo_probe.py — 一次性:找 Yahoo Finance 替代 RSS + 確認科技島/新頭殼財經(測完即還原)"""
import datetime as dt
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

import requests

import news_sources as ns

UA = ns.UA
YAHOO = [
    "https://finance.yahoo.com/news/rssindex",
    "https://finance.yahoo.com/rss/topstories",
    "https://finance.yahoo.com/rss/",
    "https://finance.yahoo.com/news/rss",
    "https://news.yahoo.com/rss/finance",
    "https://feeds.finance.yahoo.com/rss/2.0/headline?s=NVDA,TSM,AMD,AVGO,MU,INTC,ASML&region=US&lang=en-US",
    "https://feeds.finance.yahoo.com/rss/2.0/headline?s=%5EGSPC&region=US&lang=en-US",
    "https://finance.yahoo.com/rss/headline?s=NVDA",
    "https://finance.yahoo.com/topic/tech/rss",
]
for u in YAHOO:
    try:
        r = requests.get(u, headers=UA, timeout=20)
        items, dates = [], []
        if r.status_code == 200:
            try:
                root = ET.fromstring(r.content)
                for it in root.iter("item"):
                    items.append((it.findtext("title") or "").strip())
                    try:
                        dates.append(parsedate_to_datetime(it.findtext("pubDate") or ""))
                    except Exception:
                        pass
            except Exception as e:
                items = None
                print(f"\n{u}\n  HTTP 200 但非 RSS: {str(e)[:60]} | {r.text[:120]!r}")
                continue
        print(f"\n{u}\n  HTTP {r.status_code} 則數 {len(items) if items is not None else '-'} "
              f"最新 {max(dates).date() if dates else None}")
        for t in (items or [])[:6]:
            print("   ·", t[:90])
    except Exception as e:
        print(f"\n{u}\n  失敗 {str(e)[:80]}")

print("\n=== 新來源(_fetch_simple_rss, 近2天)===")
for name, url in [("technice", "https://www.technice.com.tw/feed/"),
                  ("newtalk_finance", "https://newtalk.tw/rss/category/3")]:
    ts = ns._fetch_simple_rss(url, filter_recent_days=2)
    print(f"{name}: {len(ts)} 則")
    for t in ts[:8]:
        print("   ·", t[:80])
print("\n今天(UTC)", dt.datetime.utcnow().date())
