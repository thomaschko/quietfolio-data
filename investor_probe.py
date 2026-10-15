# -*- coding: utf-8 -*-
"""investor_probe.py — investor.com.tw 一次性驗證(2026-10-15,測完即還原,不併入每日流程)"""
import re
import socket
from collections import Counter
from urllib.parse import urljoin, urlparse

import requests

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120 Safari/537.36"}


def show(url):
    print(f"\n=== GET {url} ===")
    try:
        r = requests.get(url, headers=UA, timeout=20, allow_redirects=True)
    except Exception as e:
        print("失敗:", str(e)[:300])
        return None
    r.encoding = r.apparent_encoding or r.encoding
    print("HTTP", r.status_code, "最終網址:", r.url, "Content-Type:", r.headers.get("Content-Type"))
    t = re.search(r"<title[^>]*>(.*?)</title>", r.text, re.S | re.I)
    print("title:", (t.group(1).strip() if t else None), " 長度:", len(r.text))
    return r


for host in ("www.investor.com.tw", "investor.com.tw"):
    try:
        print("DNS", host, "→", sorted({a[4][0] for a in socket.getaddrinfo(host, 443)}))
    except Exception as e:
        print("DNS", host, "失敗:", e)

r = None
for u in ("https://www.investor.com.tw/", "https://investor.com.tw/", "http://www.investor.com.tw/"):
    r = show(u)
    if r is not None and r.status_code == 200:
        break

if r is not None and r.status_code == 200:
    html = r.text
    rss = re.findall(r'<link[^>]+type="application/(?:rss|atom)\+xml"[^>]*>', html, re.I)
    print("\nRSS <link>:", rss[:5])
    links = []
    for m in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', html, re.S | re.I):
        txt = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m.group(2))).strip()
        if len(txt) >= 8 and re.search(r"[一-鿿]", txt):
            links.append((txt, urljoin(r.url, m.group(1))))
    pat = Counter(re.sub(r"\d+", "N", urlparse(h).path + ("?" + urlparse(h).query if urlparse(h).query else ""))
                  for _, h in links)
    print(f"\n中文長錨點 {len(links)} 個;網址格式前10:", pat.most_common(10))
    print("\n前30則:")
    for i, (t, h) in enumerate(links[:30], 1):
        print(f"{i:2d}. {t[:60]}\n     {h}")
    dates = re.findall(r"20\d{2}[/\-.]\d{1,2}[/\-.]\d{1,2}", html)
    print("\n頁面日期字串(前10):", dates[:10])
    print("付費/會員字樣:", sorted(set(re.findall(r"訂閱|付費|會員|登入|試閱", html)))[:10])
    for path in ("/robots.txt", "/rss", "/rss.xml", "/feed"):
        x = show(urljoin(r.url, path))
        if x is not None and x.status_code == 200:
            print(x.text[:400])
