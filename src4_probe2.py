# -*- coding: utf-8 -*-
"""
src4_probe2.py — TechNews / TrendForce 探測(src4 專業科技源)
============================================================
兩家都是科技產業專業媒體,對「發現新題材」對口:
  TechNews  半導體/電子/AI 原創報導
  TrendForce 研究機構,趨勢源頭(HBM供需/先進封裝/面板報價,比新聞更領先)
測 RSS 優先、HTML 備援。
依賴:requests(標準庫 xml/re)
============================================================
"""
import requests
import xml.etree.ElementTree as ET
import re
import datetime as dt

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"}

SOURCES = {
    "TechNews-RSS": [
        "https://technews.tw/tn-rss",
        "https://technews.tw/feed/",
        "https://technews.tw/rss/",
    ],
    "TechNews-HTML": [
        "https://technews.tw/",
    ],
    "TrendForce-RSS": [
        "https://www.trendforce.com.tw/presscenter/rss",
        "https://www.trendforce.com.tw/rss",
        "https://press.trendforce.com.tw/rss",
    ],
    "TrendForce-HTML": [
        "https://www.trendforce.com.tw/presscenter/news",
    ],
}

CHECK_KW = ["CoWoS","CoPoS","CPO","矽光子","光通訊","HBM","HBM4","DRAM","DDR5","記憶體","NAND",
            "ASIC","SiC","碳化矽","氮化鎵","先進封裝","玻璃基板","FOPLP","面板級封裝","HVDC",
            "人形機器人","矽智財","台積電","輝達","AI伺服器","AI server","散熱","液冷","半導體",
            "晶圓","面板","伺服器","光電","封裝","基板","超級循環"]


def try_rss(url):
    try:
        r = requests.get(url, headers=UA, timeout=20)
        if r.status_code != 200:
            return None, r.status_code
        body = r.text
        if not (body.lstrip().startswith("<?xml") or "<rss" in body[:300] or "<feed" in body[:300]):
            return "notxml", r.status_code
        root = ET.fromstring(body)
        titles = [it.find("title").text.strip() for it in root.iter("item")
                  if it.find("title") is not None and it.find("title").text]
        if not titles:
            for e in root.iter("{http://www.w3.org/2005/Atom}entry"):
                t = e.find("{http://www.w3.org/2005/Atom}title")
                if t is not None and t.text:
                    titles.append(t.text.strip())
        return titles, r.status_code
    except Exception as e:
        return f"err:{str(e)[:40]}", 0


def try_html(url):
    try:
        r = requests.get(url, headers=UA, timeout=20)
        r.encoding = "utf-8"
        if r.status_code != 200:
            return None, r.status_code
        titles = []
        for m in re.finditer(r'<a[^>]*>([^<]{10,55})</a>', r.text):
            txt = m.group(1).strip()
            if re.search(r'[\u4e00-\u9fff]', txt) and not re.search(r'登入|會員|訂閱|首頁|更多|下一頁|版權|Cookie|關於|廣告|報告|購買|聯絡', txt):
                titles.append(txt)
        seen, uniq = set(), []
        for t in titles:
            if t not in seen:
                seen.add(t); uniq.append(t)
        return uniq, r.status_code
    except Exception as e:
        return f"err:{str(e)[:40]}", 0


def main():
    print("=" * 62)
    print(f"TechNews / TrendForce 探測  {dt.date.today()}")
    print("=" * 62)
    all_titles = []
    working = []
    for name, urls in SOURCES.items():
        print(f"\n■ {name}")
        is_rss = "RSS" in name
        got = False
        for url in urls:
            titles, code = (try_rss(url) if is_rss else try_html(url))
            if isinstance(titles, list) and titles:
                print(f"  ✓ [{code}] {url}")
                for t in titles[:7]:
                    print(f"    · {t[:52]}")
                working.append((name, url, len(titles)))
                all_titles += titles
                got = True
                break
            else:
                info = titles if isinstance(titles, str) else "無標題"
                print(f"  · [{code}] {url.split('//')[1][:42]} → {info}")
        if not got:
            print(f"  ✗ 無可用端點")

    print("\n" + "=" * 62 + "\n總結\n" + "=" * 62)
    for name, url, n in working:
        print(f"  ✓ {name}: {n}則  {url}")
    joined = " ".join(all_titles)
    hits = {k: joined.count(k) for k in CHECK_KW if joined.count(k) > 0}
    print(f"\n題材關鍵字命中: {dict(sorted(hits.items(), key=lambda x:-x[1]))}")
    print("\n→ 貼回給 Claude,把可用來源併進 src4。")


if __name__ == "__main__":
    main()
