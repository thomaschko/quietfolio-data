# -*- coding: utf-8 -*-
"""一次性探測(跑完即刪):sangyo-times.jp/scn/ 能否當題材源。不寫任何檔案。"""
import re
import requests
from urllib.parse import urljoin

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"}
BASE = "https://www.sangyo-times.jp/"


def get(url):
    try:
        r = requests.get(url, headers=UA, timeout=25)
        print(f"\n== {url} → HTTP {r.status_code} {r.headers.get('content-type')} {len(r.content)}B final={r.url}")
        return r
    except Exception as e:
        print(f"\n== {url} → {e}")
        return None


def main():
    r = get(BASE + "robots.txt")
    if r is not None and r.status_code == 200:
        print(r.text[:1500])
    r = get(BASE + "scn/")
    html = ""
    if r is not None and r.status_code == 200:
        r.encoding = r.apparent_encoding if not r.encoding or r.encoding.lower() == "iso-8859-1" else r.encoding
        html = r.text
        print("encoding:", r.encoding)
        print("title:", re.findall(r"<title>(.*?)</title>", html, re.S)[:1])
        print("lang:", re.findall(r"<html[^>]*lang=\"([^\"]+)\"", html)[:1])
        print("feeds:", re.findall(r"<link[^>]+(?:rss|atom)[^>]*>", html, re.I)[:5])
        links = re.findall(r"<a[^>]+href=\"([^\"]+)\"[^>]*>(.*?)</a>", html, re.S)
        seen = set()
        n = 0
        for href, txt in links:
            t = re.sub(r"<[^>]+>|\s+", " ", txt).strip()
            u = urljoin(BASE + "scn/", href)
            if len(t) < 8 or u in seen or "sangyo-times" not in u:
                continue
            seen.add(u)
            n += 1
            if n <= 60:
                print(f"  {t[:70]} | {u}")
        print(f"(article-ish links: {n})")
        print("dates on page:", sorted(set(re.findall(r"20\d\d[./年-]\d{1,2}[./月-]\d{1,2}", html)))[-15:])
        print("paywall hints:", [w for w in ("会員", "会员", "有料", "ログイン", "登录", "訂閱", "订阅", "购买", "購読") if w in html])
    for path in ("scn/feed/", "scn/rss", "scn/rss.xml", "feed/", "rss/", "rss.xml", "index.xml", "sitemap.xml", "scn/sitemap.xml"):
        r = get(BASE + path)
        if r is not None and r.status_code == 200:
            print(r.text[:800])
    # 第一篇文章內頁
    m = re.findall(r"href=\"([^\"]*(?:article|news|scn/)[^\"]*\d{3,}[^\"]*)\"", html)
    if m:
        r = get(urljoin(BASE + "scn/", m[0]))
        if r is not None and r.status_code == 200:
            r.encoding = r.apparent_encoding
            body = re.sub(r"<script.*?</script>|<style.*?</style>", "", r.text, flags=re.S)
            txt = re.sub(r"<[^>]+>|\s+", " ", body)
            print("article text sample:", txt[:1500])


if __name__ == "__main__":
    main()
