# -*- coding: utf-8 -*-
"""
statementdog_uanalyze_probe.py — 財報狗/優分析 探測(補強src4新題材發現)
============================================================
財報狗:用Substack RSS(比直接爬官網穩定,Substack平台標準格式)
優分析:官網文章列表為動態頁面,需探測HTML結構才知道能否穩定抓
============================================================
"""
import requests
import xml.etree.ElementTree as ET
import re

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"}

STATEMENTDOG_RSS = "https://statementdog.substack.com/feed"
UANALYZE_URL = "https://uanalyze.com.tw/articles"

CHECK_KW = ["CoWoS","CoPoS","CPO","矽光子","光通訊","HBM","HBM4","DRAM","DDR5","記憶體","NAND",
            "ASIC","SiC","碳化矽","氮化鎵","先進封裝","玻璃基板","FOPLP","面板級封裝","HVDC",
            "人形機器人","矽智財","台積電","輝達","AI伺服器","散熱","液冷","半導體","晶圓","面板",
            "伺服器","光電","封裝","基板","CXL","GPUDirect","記憶體池化","鑽針","PCB"]


def probe_statementdog():
    print("■ 財報狗 Substack RSS")
    try:
        r = requests.get(STATEMENTDOG_RSS, headers=UA, timeout=20)
        print(f"  狀態 {r.status_code}, 長度 {len(r.text)}")
        if r.status_code != 200:
            return []
        root = ET.fromstring(r.text)
        titles = [it.find("title").text.strip() for it in root.iter("item")
                  if it.find("title") is not None and it.find("title").text]
        print(f"  ✓ 抓到 {len(titles)} 則,範例:")
        for t in titles[:8]:
            print(f"    · {t[:55]}")
        return titles
    except Exception as e:
        print(f"  錯誤: {str(e)[:80]}")
        return []


def probe_uanalyze():
    print("\n■ 優分析 官網文章列表")
    try:
        r = requests.get(UANALYZE_URL, headers=UA, timeout=20)
        r.encoding = "utf-8"
        print(f"  狀態 {r.status_code}, 長度 {len(r.text)}")
        if r.status_code != 200:
            return []
        # 印一段原始HTML,確認是靜態內容還是需要JS渲染
        idx = r.text.find("articles/")
        if idx != -1:
            print(f"  原始HTML片段(前400字):")
            print(f"  {r.text[max(0,idx-50):idx+400]}")
        # 嘗試抓標題(常見模式:連結文字或h標籤)
        titles = []
        for m in re.finditer(r'<a[^>]*href="[^"]*articles/\d+[^"]*"[^>]*>([^<]{8,60})</a>', r.text):
            t = m.group(1).strip()
            if re.search(r'[\u4e00-\u9fff]', t):
                titles.append(t)
        seen, uniq = set(), []
        for t in titles:
            if t not in seen:
                seen.add(t); uniq.append(t)
        print(f"  解析出 {len(uniq)} 則標題,範例:")
        for t in uniq[:8]:
            print(f"    · {t[:55]}")
        return uniq
    except Exception as e:
        print(f"  錯誤: {str(e)[:80]}")
        return []


def main():
    print("=" * 60)
    print("財報狗(Substack) + 優分析(官網) 探測")
    print("=" * 60)
    t1 = probe_statementdog()
    t2 = probe_uanalyze()

    all_titles = t1 + t2
    joined = " ".join(all_titles)
    hits = {k: joined.count(k) for k in CHECK_KW if joined.count(k) > 0}
    print("\n" + "=" * 60)
    print("題材關鍵字命中")
    print("=" * 60)
    print(dict(sorted(hits.items(), key=lambda x: -x[1])))
    print(f"\n財報狗: {len(t1)}則 | 優分析: {len(t2)}則")
    print("→ 把結果貼回給 Claude,決定怎麼併入 src4 來源池")


if __name__ == "__main__":
    main()
