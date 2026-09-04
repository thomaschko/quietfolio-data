# -*- coding: utf-8 -*-
"""
rss_probe3.py — 三源整合探測:中央社RSS + DigiTimes/MoneyDJ HTML標題
============================================================
中央社:活的RSS(有內容、合法授權),抓 finance + technology
DigiTimes/MoneyDJ:RSS已空,改抓新聞列表HTML,用正則抽標題(只取標題,版權合規)

驗證三家能不能抓、標題解析得出來、有沒有題材關鍵字。
依賴:requests(標準庫 re/xml 解析)
============================================================
"""
import requests
import xml.etree.ElementTree as ET
import re
import datetime as dt

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"}

# ── 中央社 RSS(活的、有內容)──
CNA_FEEDS = {
    "中央社-產經": "https://feeds.feedburner.com/rsscna/finance",
    "中央社-科技": "https://feeds.feedburner.com/rsscna/technology",
}

# ── DigiTimes / MoneyDJ HTML 頁(RSS已空,抓HTML)──
HTML_PAGES = {
    "DigiTimes-科技": "https://www.digitimes.com.tw/tech/",
    "MoneyDJ-台股": "https://www.moneydj.com/KMDJ/Common/ListNewArticles.aspx?svc=NW&a=X0100001",
}

CHECK_KW = ["CoWoS","CPO","矽光子","光通訊","HBM","HBM4","DRAM","記憶體","NAND","ASIC","SiC",
            "碳化矽","氮化鎵","先進封裝","玻璃基板","HVDC","人形機器人","矽智財","台積電",
            "輝達","AI","散熱","液冷","半導體","晶圓","面板","伺服器","美光","記憶體"]


def probe_rss(name, url):
    print(f"\n■ {name} (RSS)")
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
            print(f"    · {t[:52]}")
        return titles
    except Exception as e:
        print(f"  錯誤: {str(e)[:60]}")
        return []


def probe_html(name, url):
    print(f"\n■ {name} (HTML)")
    try:
        r = requests.get(url, headers=UA, timeout=25)
        r.encoding = "utf-8"
        print(f"  狀態 {r.status_code}, 長度 {len(r.text)}")
        if r.status_code != 200:
            return []
        html = r.text
        # 抽標題:試多種模式(a標籤文字、title屬性、h標籤)
        titles = []
        # 模式1:<a ...>標題</a> 中含中文且長度合理
        for m in re.finditer(r'<a[^>]*>([^<]{8,60})</a>', html):
            txt = m.group(1).strip()
            # 只留含中文、不含明顯導覽字樣的
            if re.search(r'[\u4e00-\u9fff]', txt) and not re.search(r'登入|會員|訂閱|首頁|更多|下一頁|版權|Cookie', txt):
                titles.append(txt)
        # 去重、保序
        seen = set()
        uniq = []
        for t in titles:
            if t not in seen:
                seen.add(t); uniq.append(t)
        print(f"  抽到 {len(uniq)} 個候選標題(前10):")
        for t in uniq[:10]:
            print(f"    · {t[:52]}")
        return uniq
    except Exception as e:
        print(f"  錯誤: {str(e)[:60]}")
        return []


def main():
    print("=" * 62)
    print(f"三源整合探測  {dt.date.today()}")
    print("=" * 62)
    all_titles = []
    working = []

    for name, url in CNA_FEEDS.items():
        t = probe_rss(name, url)
        if t:
            working.append((name, len(t))); all_titles += t
    for name, url in HTML_PAGES.items():
        t = probe_html(name, url)
        if t:
            working.append((name, len(t))); all_titles += t

    print("\n" + "=" * 62)
    print("總結")
    print("=" * 62)
    for name, n in working:
        print(f"  ✓ {name}: {n}則")
    joined = " ".join(all_titles)
    hits = {k: joined.count(k) for k in CHECK_KW if joined.count(k) > 0}
    print(f"\n題材關鍵字命中: {dict(sorted(hits.items(), key=lambda x:-x[1]))}")
    print("\n→ 把可用來源與範例標題貼回給 Claude,併進 src1/src4。")


if __name__ == "__main__":
    main()
