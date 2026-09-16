# -*- coding: utf-8 -*-
"""
google_news_rss_probe.py — Google News RSS 探測(強制加when:時間窗口)
============================================================
關鍵風險(已查證):不加when:參數,預設回傳內容中位數年齡達6.6天,
只有7.6%是6小時內新聞——等同重蹈之前Podcast歷史存檔污染近日訊號
的覆轍。本探測全程強制加 when:3d,只測「加了防護之後」的實際效果。

測試幾個廣泛的台股科技查詢,不是逐一測140個watchlist關鍵字
(那樣140次HTTP請求太慢太重,且跟src1用cnyes搜尋的目的重疊)。
============================================================
"""
import requests
import re
import xml.etree.ElementTree as ET
from urllib.parse import quote

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"}

QUERIES = [
    "台股 半導體 when:3d",
    "矽光子 when:3d",
    "AI伺服器 供應鏈 when:3d",
    "台積電 when:1d",
]

CHECK_KW = ["CoWoS","CoPoS","CPO","矽光子","光通訊","HBM","記憶體","NAND","ASIC","SiC",
            "碳化矽","先進封裝","玻璃基板","FOPLP","HVDC","法說","毛利率","營收","半導體",
            "晶圓","封裝","AI伺服器","台積電"]


def fetch_query(query):
    url = f"https://news.google.com/rss/search?q={quote(query)}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
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
    print("Google News RSS 探測(強制when:時間窗口)")
    print("=" * 62)
    all_titles = []
    for q in QUERIES:
        print(f"\n■ 查詢: {q}")
        items, code, url = fetch_query(q)
        print(f"  [{code}] {url}")
        if isinstance(items, list):
            print(f"  共{len(items)}則")
            for it in items[:8]:
                print(f"    · {it['title']}")
                print(f"      來源:{it['source']}  時間:{it['pubdate']}")
            all_titles += [it["title"] for it in items]
        else:
            print(f"  {items}")

    print("\n" + "=" * 62)
    print("題材關鍵字命中(判斷整體相關密度)")
    print("=" * 62)
    joined = " ".join(all_titles)
    hits = {k: joined.count(k) for k in CHECK_KW if joined.count(k) > 0}
    print(dict(sorted(hits.items(), key=lambda x: -x[1])) if hits else "無命中")
    print(f"\n總標題數: {len(all_titles)}")
    print("→ 把結果貼回給 Claude,決定要不要正式併入src4")


if __name__ == "__main__":
    main()
