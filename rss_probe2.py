# -*- coding: utf-8 -*-
"""
rss_probe2.py — 深挖 MoneyDJ RSS XML 結構
上次發現 RssCenter.aspx 回傳 text/xml 但解析不到 item,這支印出原始 XML 前段,
並試多個 arg 分類,看哪個有台股/科技題材內容。
"""
import requests
import xml.etree.ElementTree as ET
import datetime as dt

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

# MoneyDJ RssCenter 多分類試(arg 是分類代碼)
MONEYDJ_ARGS = {
    "X0000000-全部": "https://www.moneydj.com/kmdj/RssCenter.aspx?svc=NR&fno=1&arg=X0000000",
    "台股即時": "https://www.moneydj.com/kmdj/RssCenter.aspx?svc=NR&fno=1&arg=X0100000",
    "個股新聞": "https://www.moneydj.com/kmdj/RssCenter.aspx?svc=NR&fno=1&arg=X0100001",
    "產業": "https://www.moneydj.com/kmdj/RssCenter.aspx?svc=NR&fno=1&arg=X0300000",
    "svc=NW台股": "https://www.moneydj.com/KMDJ/Common/ListNewArticles.aspx?svc=NW&a=X0100001",
}

CHECK_KW = ["CoWoS","CPO","矽光子","光通訊","HBM","DRAM","記憶體","NAND","ASIC","SiC",
            "碳化矽","氮化鎵","先進封裝","玻璃基板","HVDC","人形機器人","矽智財","台積電",
            "輝達","AI","散熱","液冷","半導體","晶圓","面板","伺服器"]


def probe(name, url):
    print(f"\n■ {name}")
    print(f"  {url}")
    try:
        r = requests.get(url, headers=UA, timeout=25)
        print(f"  狀態 {r.status_code}, 類型 {r.headers.get('Content-Type','')[:40]}, 長度 {len(r.text)}")
        if r.status_code != 200:
            return []
        body = r.text
        # 印原始 XML 前 500 字(看結構)
        print(f"  原始前500字:\n    {body[:500]}")
        # 試解析
        titles = []
        try:
            root = ET.fromstring(body)
            print(f"  根標籤: {root.tag}")
            # 列出所有子標籤結構
            tags = set()
            for el in root.iter():
                tags.add(el.tag)
            print(f"  所有標籤: {sorted(tags)[:20]}")
            for item in root.iter("item"):
                t = item.find("title")
                if t is not None and t.text:
                    titles.append(t.text.strip())
        except ET.ParseError as e:
            print(f"  XML 解析錯誤: {str(e)[:80]}")
        if titles:
            print(f"  ✓ 抓到 {len(titles)} 則標題:")
            for t in titles[:8]:
                print(f"    · {t[:55]}")
        return titles
    except Exception as e:
        print(f"  錯誤: {str(e)[:60]}")
        return []


def main():
    print("=" * 62)
    print(f"MoneyDJ RSS 深度探測  {dt.date.today()}")
    print("=" * 62)
    all_titles = []
    working = []
    for name, url in MONEYDJ_ARGS.items():
        titles = probe(name, url)
        if titles:
            working.append((name, url, len(titles)))
            all_titles.extend(titles)

    print("\n" + "=" * 62)
    print("總結")
    print("=" * 62)
    if working:
        for name, url, n in working:
            print(f"  ✓ {name}: {n}則")
        joined = " ".join(all_titles)
        hits = {k: joined.count(k) for k in CHECK_KW if joined.count(k) > 0}
        print(f"\n題材關鍵字命中: {hits}")
    else:
        print("  全部無法解析,把上面各端點的『原始前500字』貼回給 Claude。")


if __name__ == "__main__":
    main()
