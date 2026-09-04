# -*- coding: utf-8 -*-
"""
============================================================
rss_probe.py — DigiTimes / MoneyDJ RSS 可行性探測
============================================================
目的:在 GitHub Actions 上驗證(沙盒白名單擋這兩站,無法在該處測):
  1. 兩個 RSS 端點抓不抓得到(狀態碼)
  2. RSS 的實際 XML 結構(標題欄位、有幾則、範例標題)
  3. 標題裡有沒有你關心的題材關鍵字(驗證訊號價值)

版權注意:DigiTimes/MoneyDJ 全文有版權,本工具【只取標題】做關鍵字統計,
         不儲存全文(與 src6 法說同樣的合規處理)。

跑法:放 quietfolio-data 根目錄,用手動觸發 workflow 跑一次。
依賴:requests(標準庫 xml.etree 解析,不需額外裝)
============================================================
"""

import requests
import xml.etree.ElementTree as ET
import datetime as dt

UA = {"User-Agent": "Mozilla/5.0 (quietfolio-radar; research)"}

# 候選 RSS 端點(多試幾個,自動找可用的)
FEEDS = {
    "DigiTimes-科技": [
        "https://www.digitimes.com.tw/tech/rss/rss.asp",
        "https://www.digitimes.com.tw/rss/rss.asp",
    ],
    "MoneyDJ-即時": [
        "https://www.moneydj.com/kmdj/rss/rsslist.aspx",
        "https://www.moneydj.com/kmdj/RssCenter.aspx?svc=NR&fno=1&arg=X0000000",
        "https://www.moneydj.com/kmdj/news/newshome.aspx",
    ],
}

# 你關心的題材關鍵字(驗證 RSS 標題有沒有這些訊號)
CHECK_KEYWORDS = [
    "CoWoS", "CPO", "矽光子", "光通訊", "HBM", "DRAM", "記憶體", "NAND",
    "ASIC", "SiC", "碳化矽", "氮化鎵", "先進封裝", "玻璃基板", "HVDC",
    "人形機器人", "矽智財", "台積電", "輝達", "AI伺服器", "散熱", "液冷",
]


def try_feed(name, urls):
    print(f"\n■ {name}")
    for url in urls:
        try:
            r = requests.get(url, headers=UA, timeout=20)
            code = r.status_code
            ctype = r.headers.get("Content-Type", "")[:40]
            body = r.text
            is_xml = body.strip().startswith("<?xml") or "<rss" in body[:500] or "<item" in body[:2000]
            print(f"  [{code}] {url.split('//')[1][:50]}... ({ctype})")
            if code != 200:
                continue
            if not is_xml:
                print(f"      ⚠ 回應非 RSS/XML(可能是 HTML 頁面),前100字:{body[:100].strip()[:100]}")
                continue
            # 解析 RSS
            titles = parse_rss(body)
            if titles:
                print(f"      ✓ 可用 RSS,抓到 {len(titles)} 則標題")
                print(f"      範例標題:")
                for t in titles[:5]:
                    print(f"        · {t[:50]}")
                return titles, url
        except Exception as e:
            print(f"  [ERR] {url.split('//')[1][:40]}: {str(e)[:50]}")
    return None, None


def parse_rss(body):
    """從 RSS XML 抽標題。相容 <item><title> 結構。"""
    titles = []
    try:
        root = ET.fromstring(body)
        # 標準 RSS: rss > channel > item > title
        for item in root.iter("item"):
            t = item.find("title")
            if t is not None and t.text:
                titles.append(t.text.strip())
        # Atom: entry > title
        if not titles:
            ns = {"a": "http://www.w3.org/2005/Atom"}
            for entry in root.iter("{http://www.w3.org/2005/Atom}entry"):
                t = entry.find("{http://www.w3.org/2005/Atom}title")
                if t is not None and t.text:
                    titles.append(t.text.strip())
    except ET.ParseError as e:
        print(f"      XML 解析失敗: {str(e)[:60]}")
    return titles


def check_keywords(all_titles):
    """統計標題裡出現哪些題材關鍵字。"""
    print("\n" + "=" * 60)
    print("題材關鍵字命中(驗證訊號價值)")
    print("=" * 60)
    hits = {}
    joined = " ".join(all_titles)
    for kw in CHECK_KEYWORDS:
        c = joined.count(kw)
        if c > 0:
            hits[kw] = c
    if hits:
        for kw, c in sorted(hits.items(), key=lambda x: -x[1]):
            print(f"  {kw}: {c} 次")
    else:
        print("  (標題中無題材關鍵字 — 可能是綜合新聞,或需抓更多則)")


def main():
    print("=" * 60)
    print(f"DigiTimes / MoneyDJ RSS 探測  {dt.date.today()}")
    print("=" * 60)
    all_titles = []
    working = []
    for name, urls in FEEDS.items():
        titles, url = try_feed(name, urls)
        if titles:
            all_titles.extend(titles)
            working.append((name, url, len(titles)))

    print("\n" + "=" * 60)
    print("探測總結")
    print("=" * 60)
    if working:
        print(f"可用 RSS:{len(working)}/{len(FEEDS)}")
        for name, url, n in working:
            print(f"  ✓ {name}: {n}則  {url}")
        check_keywords(all_titles)
        print("\n→ 把上面『可用 RSS 的 URL』和『範例標題』貼回給 Claude,")
        print("  Claude 會把可用的 RSS 併進 src1/src4 的新聞來源池。")
    else:
        print("✗ 兩個 RSS 都抓不到 — 可能被擋或端點不對,把完整 log 貼回給 Claude 調整。")


if __name__ == "__main__":
    main()
