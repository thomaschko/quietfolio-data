# -*- coding: utf-8 -*-
"""investor_probe.py — 財訊快報 investor.com.tw 一次性驗證 第3輪(列表頁標題,測完即還原)"""
import re
from collections import Counter
from urllib.parse import urljoin

import requests

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120 Safari/537.36"}
BASE = "https://www.investor.com.tw/onlineNews/"


def get(url):
    r = requests.get(url, headers=UA, timeout=20)
    enc = re.search(rb'charset=["\']?([\w-]+)', r.content[:3000], re.I)
    r.encoding = (enc.group(1).decode() if enc else None) or "big5"
    if r.encoding.lower() in ("big5", "big-5"):
        r.encoding = "cp950"
    return r


def titles(html):
    out = []
    for m in re.finditer(r'<a[^>]+href="([^"]*(?:NewsContent|Detail)\.asp\?articleNo=(\d+))"[^>]*>(.*?)</a>', html, re.S | re.I):
        t = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m.group(3))).strip()
        if t:
            out.append((m.group(2), t))
    return out


home = get(BASE + "index.asp")
print("首頁編碼", home.encoding)
lists = []
for h in re.findall(r'href="([^"]*(?:NewsList2|NewList)\.asp\?[^"]*)"', home.text, re.I):
    u = urljoin(BASE, h.replace("&amp;", "&"))
    if u not in lists:
        lists.append(u)
anchor = {urljoin(BASE, h.replace("&amp;", "&")): re.sub(r"<[^>]+>", "", t).strip()
          for h, t in re.findall(r'<a[^>]+href="([^"]*(?:NewsList2|NewList)\.asp\?[^"]*)"[^>]*>(.*?)</a>', home.text, re.S | re.I)}
print("分類列表頁", len(lists))
for u in [BASE + "TodayNews.asp", BASE + "DayTodayNews.asp"] + lists[:12]:
    try:
        r = get(u)
        ts = titles(r.text)
        days = Counter(no[2:10] for no, _ in ts)
        print(f"\n=== {anchor.get(u, '')} {u} → HTTP {r.status_code},文章 {len(ts)},截斷 {sum(t.endswith('..') or t.endswith('…') for _, t in ts)},日期 {dict(days)}")
        for no, t in ts[:12]:
            print(f"   {no} {t[:70]}")
    except Exception as e:
        print(u, "失敗", e)
