# -*- coding: utf-8 -*-
"""
google_news_us_probe.py — Google News RSS 美國版探測
============================================================
跟台灣版同一套機制,換成 hl=en-US&gl=US&ceid=US:en。
目的:補足你系統目前英文來源太少的缺口(只有gs_exchanges+cnbc_yahoo)。
同樣強制when:天數窗口,同樣避免裸公司名稱查詢。
============================================================
"""
import requests
import xml.etree.ElementTree as ET
from urllib.parse import quote

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"}

QUERIES = [
    "silicon photonics when:3d",
    "CoWoS advanced packaging when:3d",
    "HBM memory chip when:3d",
    "AI data center power when:3d",
    "semiconductor supply chain when:3d",
]

CHECK_KW = ["CoWoS","CoPoS","CPO","silicon photonics","HBM","DRAM","NAND","ASIC","SiC",
            "advanced packaging","glass substrate","FOPLP","HVDC","earnings","margin",
            "revenue","semiconductor","wafer","packaging","AI server","TSMC","memory",
            "data center","transceiver","laser"]


def fetch_query(query):
    url = f"https://news.google.com/rss/search?q={quote(query)}&hl=en-US&gl=US&ceid=US:en"
    try:
        r = requests.get(url, headers=UA, timeout=20)
        if r.status_code != 200:
            return None, r.status_code, url
        root = ET.fromstring(r.text)
        items = []
        for it in root.iter("item"):
            title = it.find("title")
            pubdate = it.find("pubDate")
            source = it.find("source")
            if title is not None and title.text:
                items.append({
                    "title": title.text.strip(),
                    "pubdate": pubdate.text if pubdate is not None else "?",
                    "source": source.text if source is not None else "?",
                })
        return items, r.status_code, url
    except Exception as e:
        return f"err:{str(e)[:80]}", 0, url


def main():
    print("=" * 62)
    print("Google News RSS 美國版探測(en-US/US/US:en)")
    print("=" * 62)
    all_titles = []
    source_counts = {}
    for q in QUERIES:
        print(f"\n■ 查詢: {q}")
        items, code, url = fetch_query(q)
        print(f"  [{code}] {url}")
        if isinstance(items, list):
            print(f"  共{len(items)}則")
            for it in items[:8]:
                print(f"    · {it['title']}")
                print(f"      來源:{it['source']}  時間:{it['pubdate']}")
                source_counts[it['source']] = source_counts.get(it['source'], 0) + 1
            all_titles += [it["title"] for it in items]
        else:
            print(f"  {items}")

    print("\n" + "=" * 62)
    print("題材關鍵字命中")
    print("=" * 62)
    joined = " ".join(all_titles)
    hits = {k: joined.count(k) for k in CHECK_KW if joined.count(k) > 0}
    print(dict(sorted(hits.items(), key=lambda x: -x[1])) if hits else "無命中")

    print("\n" + "=" * 62)
    print("來源媒體分布(前10)")
    print("=" * 62)
    for src, cnt in sorted(source_counts.items(), key=lambda x: -x[1])[:10]:
        print(f"  {src}: {cnt}則")

    print(f"\n總標題數: {len(all_titles)}")
    print("→ 把結果貼回給 Claude,決定要不要正式併入src4")


if __name__ == "__main__":
    main()
