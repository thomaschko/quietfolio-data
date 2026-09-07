# -*- coding: utf-8 -*-
"""
============================================================
intl_news.py — 國際科技新聞來源(CNBC + Yahoo Finance)
============================================================
為 src6 提供「每日國際新聞熱度」,補法說(季度)的空檔。
統計英文技術詞/公司在國際媒體標題的出現頻率 = 國際端討論熱度。

來源(探測驗證可用):
  CNBC-Technology  科技題材密集
  CNBC-Finance     偏宏觀但有料
  Yahoo-Finance    量大
  (CNBC-Markets 剔除:只2則)

版權:只取標題統計詞頻,不儲存全文。
============================================================
"""
import requests
import xml.etree.ElementTree as ET

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"}

INTL_FEEDS = [
    "https://www.cnbc.com/id/19854910/device/rss/rss.html",  # CNBC Technology
    "https://www.cnbc.com/id/10000664/device/rss/rss.html",  # CNBC Finance
    "https://finance.yahoo.com/news/rssindex",               # Yahoo Finance
]


def fetch_intl_titles():
    """抓 CNBC+Yahoo 今日標題,回傳去重後的 list。"""
    titles = []
    for url in INTL_FEEDS:
        try:
            r = requests.get(url, headers=UA, timeout=25)
            if r.status_code != 200:
                continue
            root = ET.fromstring(r.text)
            for it in root.iter("item"):
                t = it.find("title")
                if t is not None and t.text:
                    titles.append(t.text.strip())
        except Exception:
            continue
    # 去重保序
    seen, uniq = set(), []
    for t in titles:
        if t and t not in seen:
            seen.add(t); uniq.append(t)
    return uniq


if __name__ == "__main__":
    ts = fetch_intl_titles()
    print(f"國際新聞今日標題:{len(ts)} 則")
    for t in ts[:15]:
        print(f"  · {t[:60]}")
