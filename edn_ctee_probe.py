# -*- coding: utf-8 -*-
"""
edn_ctee_probe.py — 經濟日報/工商時報 探測(補強src4新題材發現)
============================================================
經濟日報:試第三方RSS閱讀器實際在用的端點(fund.udn.com/rss/lists/1002)
工商時報:官網無公開RSS,試HTML解析即時新聞頁(livenews/ctee)
============================================================
"""
import requests
import xml.etree.ElementTree as ET
import re

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"}

EDN_RSS_CANDIDATES = [
    "https://fund.udn.com/rss/lists/1002",       # 產經分類(第三方RSS reader實際在用)
    "https://money.udn.com/rssfeed/news/1001/5590/5591?ch=news",
    "https://edn.udn.com/rss.jsp",
]
CTEE_CANDIDATES = {
    "rss": [
        "https://www.ctee.com.tw/rss",
        "https://www.ctee.com.tw/feed",
    ],
    "html": [
        "https://www.ctee.com.tw/livenews/ctee",   # 即時新聞頁
        "https://www.ctee.com.tw/industry",         # 產業分類
    ],
}

CHECK_KW = ["CoWoS","CoPoS","CPO","矽光子","光通訊","HBM","HBM4","DRAM","DDR5","記憶體","NAND",
            "ASIC","SiC","碳化矽","氮化鎵","先進封裝","玻璃基板","FOPLP","面板級封裝","HVDC",
            "人形機器人","矽智財","台積電","輝達","AI伺服器","散熱","液冷","半導體","晶圓","面板",
            "伺服器","光電","封裝","基板","測試","泰瑞達","探針卡"]


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
        for m in re.finditer(r'<a[^>]*>([^<]{10,60})</a>', r.text):
            txt = m.group(1).strip()
            if re.search(r'[\u4e00-\u9fff]', txt) and not re.search(r'登入|會員|訂閱|首頁|更多|下一頁|版權|Cookie|關於|廣告|讀報區|APP', txt):
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
    print("經濟日報 / 工商時報 探測")
    print("=" * 62)
    all_titles = []
    working = []

    print("\n■ 經濟日報(RSS)")
    for url in EDN_RSS_CANDIDATES:
        titles, code = try_rss(url)
        if isinstance(titles, list) and titles:
            print(f"  ✓ [{code}] {url}")
            for t in titles[:6]:
                print(f"    · {t[:52]}")
            working.append(("經濟日報", url, len(titles)))
            all_titles += titles
            break
        else:
            info = titles if isinstance(titles, str) else "無標題"
            print(f"  · [{code}] {url.split('//')[1][:44]} → {info}")

    print("\n■ 工商時報")
    got = False
    for url in CTEE_CANDIDATES["rss"]:
        titles, code = try_rss(url)
        if isinstance(titles, list) and titles:
            print(f"  ✓RSS [{code}] {url}")
            for t in titles[:6]:
                print(f"    · {t[:52]}")
            working.append(("工商時報-RSS", url, len(titles)))
            all_titles += titles
            got = True
            break
        else:
            info = titles if isinstance(titles, str) else "無標題"
            print(f"  ·RSS [{code}] {url.split('//')[1][:44]} → {info}")
    if not got:
        for url in CTEE_CANDIDATES["html"]:
            titles, code = try_html(url)
            if isinstance(titles, list) and titles:
                print(f"  ✓HTML [{code}] {url}")
                for t in titles[:6]:
                    print(f"    · {t[:52]}")
                working.append(("工商時報-HTML", url, len(titles)))
                all_titles += titles
                got = True
                break
            else:
                info = titles if isinstance(titles, str) else "無標題"
                print(f"  ·HTML [{code}] {url.split('//')[1][:44]} → {info}")

    print("\n" + "=" * 62 + "\n總結\n" + "=" * 62)
    for name, url, n in working:
        print(f"  ✓ {name}: {n}則  {url}")
    joined = " ".join(all_titles)
    hits = {k: joined.count(k) for k in CHECK_KW if joined.count(k) > 0}
    print(f"\n題材關鍵字命中: {dict(sorted(hits.items(), key=lambda x: -x[1]))}")
    print("\n→ 把結果貼回給 Claude,決定怎麼併入 src4 來源池")


if __name__ == "__main__":
    main()
