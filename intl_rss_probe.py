# -*- coding: utf-8 -*-
"""
intl_rss_probe.py — CNBC + Yahoo Finance RSS 探測(國際科技供應鏈訊號)
============================================================
定位:併入 src6 國際訊號圈,追英文技術詞熱度(每日),補法說(季度)的空檔。
驗證兩個 RSS 可達 + 英文技術詞/公司命中。
依賴:requests(標準庫 xml 解析)
============================================================
"""
import requests
import xml.etree.ElementTree as ET
import datetime as dt

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"}

FEEDS = {
    "CNBC-Technology": "https://www.cnbc.com/id/19854910/device/rss/rss.html",
    "CNBC-Finance": "https://www.cnbc.com/id/10000664/device/rss/rss.html",
    "CNBC-Markets": "https://www.cnbc.com/id/20409666/device/rss/rss.html",
    "Yahoo-Finance": "https://finance.yahoo.com/news/rssindex",
}

# 英文技術詞 + 關鍵公司(對應你的供應鏈題材)
CHECK_KW = ["CoWoS","HBM","HBM4","CPO","co-packaged","silicon photonics","advanced packaging",
            "glass substrate","Rubin","Blackwell","NVLink","Vera Rubin","liquid cooling","800V",
            "TSMC","Micron","Nvidia","NVIDIA","AMD","ASML","Broadcom","SK Hynix","Samsung",
            "AI chip","data center","GPU","ASIC","DRAM","NAND","memory","foundry","capacity"]


def probe(name, url):
    print(f"\n■ {name}")
    print(f"  {url}")
    try:
        r = requests.get(url, headers=UA, timeout=25)
        print(f"  狀態 {r.status_code}, 長度 {len(r.text)}")
        if r.status_code != 200:
            return []
        root = ET.fromstring(r.text)
        titles = [it.find("title").text.strip() for it in root.iter("item")
                  if it.find("title") is not None and it.find("title").text]
        print(f"  ✓ 抓到 {len(titles)} 則")
        for t in titles[:6]:
            print(f"    · {t[:60]}")
        return titles
    except Exception as e:
        print(f"  錯誤: {str(e)[:70]}")
        return []


def main():
    print("=" * 62)
    print(f"CNBC + Yahoo Finance RSS 探測  {dt.date.today()}")
    print("=" * 62)
    all_titles = []
    working = []
    for name, url in FEEDS.items():
        t = probe(name, url)
        if t:
            working.append((name, len(t))); all_titles += t

    print("\n" + "=" * 62)
    print("總結")
    print("=" * 62)
    for name, n in working:
        print(f"  ✓ {name}: {n}則")
    joined = " ".join(all_titles)
    hits = {k: joined.lower().count(k.lower()) for k in CHECK_KW if joined.lower().count(k.lower()) > 0}
    print(f"\n英文技術詞/公司命中: {dict(sorted(hits.items(), key=lambda x:-x[1]))}")
    print(f"\n總標題數: {len(all_titles)}")
    print("→ 把可用來源與命中貼回給 Claude,併入 src6 國際訊號圈。")


if __name__ == "__main__":
    main()
