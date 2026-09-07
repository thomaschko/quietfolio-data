# -*- coding: utf-8 -*-
"""
src4_source_probe.py — 財訊 / 理財周刊 / Wa-people 探測
============================================================
為 src4(新題材發現)擴充來源。測 RSS 和 HTML 兩種抓法,看哪個能抓到標題。
依賴:requests(標準庫 xml/re)
============================================================
"""
import requests
import xml.etree.ElementTree as ET
import re
import datetime as dt

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"}

# 每家給多個候選(RSS 優先,HTML 備援)
SOURCES = {
    "財訊-RSS": [
        "https://www.wealth.com.tw/rss",
        "https://www.wealth.com.tw/feed",
        "https://www.wealth.com.tw/articles/rss",
    ],
    "財訊-HTML": [
        "https://www.wealth.com.tw/articles",
        "https://www.wealth.com.tw/",
    ],
    "理財周刊-RSS": [
        "https://www.moneyweekly.com.tw/rss",
        "https://www.moneyweekly.com.tw/feed",
    ],
    "理財周刊-HTML": [
        "https://www.moneyweekly.com.tw/",
    ],
    "WaPeople-RSS": [
        "https://www.wa-people.com/rss.xml",
        "https://www.wa-people.com/feed",
    ],
    "WaPeople-HTML": [
        "https://www.wa-people.com/",
    ],
}

CHECK_KW = ["CoWoS","CPO","矽光子","光通訊","HBM","記憶體","ASIC","SiC","碳化矽","氮化鎵",
            "先進封裝","玻璃基板","HVDC","人形機器人","矽智財","台積電","輝達","AI",
            "半導體","晶圓","面板","伺服器","光電","散熱","液冷","矽光","封裝"]


def try_rss(url):
    try:
        r = requests.get(url, headers=UA, timeout=20)
        if r.status_code != 200:
            return None, r.status_code
        body = r.text
        if not (body.lstrip().startswith("<?xml") or "<rss" in body[:300] or "<feed" in body[:300]):
            return "notxml", r.status_code
        root = ET.fromstring(body)
        titles = []
        for it in root.iter("item"):
            t = it.find("title")
            if t is not None and t.text:
                titles.append(t.text.strip())
        if not titles:  # Atom
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
        for m in re.finditer(r'<a[^>]*>([^<]{10,50})</a>', r.text):
            txt = m.group(1).strip()
            if re.search(r'[\u4e00-\u9fff]', txt) and not re.search(r'登入|會員|訂閱|首頁|更多|下一頁|版權|Cookie|關於|廣告|訂閱電子報', txt):
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
    print(f"財訊/理財周刊/Wa-people 探測  {dt.date.today()}")
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
                print(f"    抓到 {len(titles)} 則,範例:")
                for t in titles[:6]:
                    print(f"      · {t[:50]}")
                working.append((name, url, len(titles)))
                all_titles += titles
                got = True
                break
            else:
                info = titles if isinstance(titles, str) else "無標題"
                print(f"  · [{code}] {url.split('//')[1][:40]} → {info}")
        if not got:
            print(f"  ✗ {name} 無可用端點")

    print("\n" + "=" * 62)
    print("總結")
    print("=" * 62)
    if working:
        for name, url, n in working:
            print(f"  ✓ {name}: {n}則  {url}")
        joined = " ".join(all_titles)
        hits = {k: joined.count(k) for k in CHECK_KW if joined.count(k) > 0}
        print(f"\n題材關鍵字命中: {dict(sorted(hits.items(), key=lambda x:-x[1]))}")
    else:
        print("  三家都無法抓,把上面各端點狀態貼回給 Claude。")
    print("\n→ 貼回給 Claude,把可用來源併進 src4 並做過濾升級。")


if __name__ == "__main__":
    main()
