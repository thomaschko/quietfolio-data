# -*- coding: utf-8 -*-
"""sources_probe.py — 一次性:評估使用者列出的 19 個來源能否納入題材源(測完即還原)
每站:首頁狀態、<link rel=alternate> RSS、常見 RSS 路徑、robots.txt 對 * 的 Disallow、RSS 則數/最新日期/前5則標題。"""
import re
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlparse

import requests

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120 Safari/537.36"}
SITES = {
    "聯合報/經濟日報": ("https://udn.com/news/index", ["https://udn.com/rssfeed/news/2/6644?ch=news",
                                                   "https://money.udn.com/rssfeed/news/1001/5591?ch=money",
                                                   "https://money.udn.com/rssfeed/news/1001/5612?ch=money"]),
    "TVBS": ("https://news.tvbs.com.tw/money", ["https://news.tvbs.com.tw/crawler/rss/money"]),
    "緯來新聞": ("https://news.videoland.com.tw/", []),
    "今周刊": ("https://www.businesstoday.com.tw/", ["https://www.businesstoday.com.tw/rss"]),
    "新頭殼": ("https://newtalk.tw/", ["https://newtalk.tw/rss/all", "https://newtalk.tw/rss/category/3"]),
    "科技島": ("https://www.technice.com.tw/", ["https://www.technice.com.tw/feed/"]),
    "三立": ("https://www.setn.com/ViewAll.aspx?PageGroupID=2", ["https://www.setn.com/rss.aspx?NewsType=2",
                                                              "https://www.setn.com/rss.aspx?PageGroupID=2"]),
    "鏡報": ("https://www.mirrordaily.news/", ["https://www.mirrordaily.news/rss"]),
    "Yahoo Finance": ("https://finance.yahoo.com/", ["https://finance.yahoo.com/news/rssindex"]),
    "Benzinga": ("https://www.benzinga.com/", ["https://www.benzinga.com/feed"]),
    "Investing.com": ("https://www.investing.com/news/", ["https://www.investing.com/rss/news_25.rss",
                                                         "https://www.investing.com/rss/news.rss"]),
    "Hudson Labs": ("https://www.hudson-labs.com/research", ["https://www.hudson-labs.com/research/rss.xml"]),
    "MarketBeat": ("https://www.marketbeat.com/", ["https://www.marketbeat.com/feed/"]),
    "TradingView": ("https://www.tradingview.com/news/", ["https://www.tradingview.com/feed/"]),
    "BigGo Finance": ("https://finance.biggo.com/", ["https://finance.biggo.com/rss"]),
    "TradingKey": ("https://www.tradingkey.com/", ["https://www.tradingkey.com/rss"]),
}
COMMON = ["/feed", "/feed/", "/rss", "/rss.xml", "/feeds"]


def get(url):
    try:
        return requests.get(url, headers=UA, timeout=20)
    except Exception as e:
        return str(e)[:80]


def parse_rss(text):
    try:
        root = ET.fromstring(text.encode("utf-8") if isinstance(text, str) else text)
    except Exception:
        return None
    items = list(root.iter("item")) or list(root.iter("{http://www.w3.org/2005/Atom}entry"))
    titles, dates = [], []
    for it in items:
        t = it.findtext("title") or it.findtext("{http://www.w3.org/2005/Atom}title") or ""
        titles.append(re.sub(r"\s+", " ", t).strip())
        d = it.findtext("pubDate") or it.findtext("{http://www.w3.org/2005/Atom}updated") or ""
        try:
            dates.append(parsedate_to_datetime(d).date())
        except Exception:
            pass
    return titles, (max(dates) if dates else None)


for name, (home, rss_list) in SITES.items():
    print(f"\n######## {name} ########")
    r = get(home)
    if isinstance(r, str):
        print("首頁失敗:", r)
        alt = []
    else:
        r.encoding = r.apparent_encoding or r.encoding
        cf = "cloudflare" in r.headers.get("Server", "").lower() or "Just a moment" in r.text[:3000]
        print(f"首頁 HTTP {r.status_code} 長度 {len(r.text)}{' (Cloudflare挑戰頁)' if cf and r.status_code != 200 else ''}")
        alt = [urljoin(r.url, h) for h in re.findall(
            r'<link[^>]+type="application/(?:rss|atom)\+xml"[^>]+href="([^"]+)"', r.text, re.I)]
        alt += [urljoin(r.url, h) for h in re.findall(
            r'<link[^>]+href="([^"]+)"[^>]+type="application/(?:rss|atom)\+xml"', r.text, re.I)]
        print("頁面宣告的 RSS:", alt[:5])
    rb = get(f"{urlparse(home).scheme}://{urlparse(home).netloc}/robots.txt")
    if not isinstance(rb, str) and rb.status_code == 200:
        block, star = [], False
        for ln in rb.text.splitlines():
            ln = ln.strip()
            if ln.lower().startswith("user-agent:"):
                star = ln.split(":", 1)[1].strip() == "*"
            elif star and ln.lower().startswith("disallow:"):
                block.append(ln.split(":", 1)[1].strip())
        print("robots * Disallow:", block[:12], "(含 / 全擋)" if "/" in block else "")
    else:
        print("robots.txt:", rb if isinstance(rb, str) else rb.status_code)
    tried = []
    base = f"{urlparse(home).scheme}://{urlparse(home).netloc}"
    for u in list(dict.fromkeys(alt[:3] + rss_list + [base + p for p in COMMON])):
        x = get(u)
        if isinstance(x, str) or x.status_code != 200:
            tried.append(f"{u} → {x if isinstance(x, str) else x.status_code}")
            continue
        x.encoding = x.apparent_encoding or "utf-8"
        res = parse_rss(x.text)
        if not res or not res[0]:
            tried.append(f"{u} → 200 但非RSS")
            continue
        titles, newest = res
        print(f"✅ RSS {u}:{len(titles)} 則,最新 {newest}")
        for t in titles[:6]:
            print("     ·", t[:80])
    for t in tried:
        print("   ✗", t)
