# -*- coding: utf-8 -*-
"""investor_probe.py — 財訊快報 investor.com.tw 一次性驗證 第2輪(測完即還原)
只看列表頁/標題,不抓內文。"""
import re
from collections import Counter
from urllib.parse import urljoin

import requests

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120 Safari/537.36"}
BASE = "https://www.investor.com.tw/onlineNews/"


def get(url):
    r = requests.get(url, headers=UA, timeout=20)
    r.encoding = r.apparent_encoding or r.encoding
    return r


home = get(BASE + "index.asp")
paths = Counter(re.sub(r"=[^&\"]*", "=", h) for h in re.findall(r'href="([^"]+\.asp[^"]*)"', home.text, re.I))
print("首頁 .asp 連結種類:")
for p, n in paths.most_common(40):
    print(f"  {n:3d} {p}")

print("\n=== sitemap.xml ===")
try:
    sm = get("https://www.investor.com.tw/sitemap.xml")
    locs = re.findall(r"<loc>(.*?)</loc>", sm.text)
    print("HTTP", sm.status_code, "URL數", len(locs))
    for u in locs[:15]:
        print("  ", u)
except Exception as e:
    print("失敗", e)

# 候選列表頁:首頁連結裡含 List/Category/News 的頁面
cands = sorted({urljoin(BASE, h) for h in re.findall(r'href="([^"]*(?:List|list|Cate|cate|News|news)[^"]*\.asp[^"]*)"', home.text)
                if "NewsContent" not in h and "Detail" not in h})[:8]
for u in cands:
    print(f"\n=== 列表頁候選 {u} ===")
    try:
        r = get(u)
        items = []
        for m in re.finditer(r'<a[^>]+href="([^"]*(?:NewsContent|Detail)\.asp\?articleNo=\d+)"[^>]*>(.*?)</a>', r.text, re.S | re.I):
            t = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m.group(2))).strip()
            if t:
                items.append(t)
        print("HTTP", r.status_code, "文章連結", len(items), "被截斷(..)", sum(t.endswith("..") for t in items))
        for t in items[:25]:
            print("   ·", t[:70])
    except Exception as e:
        print("失敗", e)

print("\n=== 單篇頁面只看 <title>/標題標籤(不印內文)===")
for no in re.findall(r"articleNo=(\d+)", home.text)[:3]:
    try:
        r = get(BASE + f"NewsContent.asp?articleNo={no}")
        t = re.search(r"<title[^>]*>(.*?)</title>", r.text, re.S | re.I)
        h = re.findall(r"<h[1-3][^>]*>(.*?)</h[1-3]>", r.text, re.S | re.I)
        print(no, "HTTP", r.status_code, "| title:", (t.group(1).strip()[:80] if t else None),
              "| h1-3:", [re.sub(r"<[^>]+>", "", x).strip()[:60] for x in h[:3]],
              "| 會員/登入字樣:", sorted(set(re.findall(r"會員|登入|訂閱|付費", r.text))))
    except Exception as e:
        print(no, "失敗", e)
