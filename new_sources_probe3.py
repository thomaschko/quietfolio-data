# -*- coding: utf-8 -*-
"""
new_sources_probe3.py — 3個候選來源合併探測
============================================================
1. 優閒聊財經(用Apple Podcast ID透過iTunes官方Lookup API反查真正RSS,
   不用猜SoundOn UUID格式;內容取材自「優分析」,可能是繞過優分析官網
   JS動態渲染問題的後門管道)
2. 聚財網(wearn.com,投資論壇,品質疑慮較高,仍測試看實際內容)
3. MarketWatch(需找科技類專屬feed,避免抓到全站雜訊)
============================================================
"""
import requests
import re
import json
import xml.etree.ElementTree as ET

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"}

CHECK_KW = ["CoWoS","CoPoS","CPO","矽光子","光通訊","HBM","HBM4","DRAM","DDR5","記憶體","NAND",
            "ASIC","SiC","碳化矽","氮化鎵","先進封裝","玻璃基板","FOPLP","HVDC","法說","毛利率",
            "營收","半導體","晶圓","封裝","AI伺服器","台積電","測試","致茂","國巨","功率半導體"]


def strip_html(text):
    return re.sub(r'<[^>]+>', '', text or '').strip()


def report_hits(name, titles):
    joined = " ".join(titles)
    hits = {k: joined.count(k) for k in CHECK_KW if joined.count(k) > 0}
    print(f"\n  → {name} 共{len(titles)}則, 命中關鍵詞: "
          f"{dict(sorted(hits.items(), key=lambda x: -x[1])) if hits else '無'}")


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
            title = it.find("title")
            pubdate = it.find("pubDate")
            if title is not None and title.text:
                titles.append((title.text.strip(), pubdate.text if pubdate is not None else "?"))
        return titles, r.status_code
    except Exception as e:
        return f"err:{str(e)[:60]}", 0


def try_html(url):
    try:
        r = requests.get(url, headers=UA, timeout=20)
        r.encoding = "utf-8"
        if r.status_code != 200:
            return None, r.status_code
        titles = []
        for m in re.finditer(r'<a[^>]*>([^<]{10,60})</a>', r.text):
            txt = m.group(1).strip()
            if re.search(r'[\u4e00-\u9fff]', txt) and not re.search(
                r'登入|會員|訂閱|首頁|更多|下一頁|版權|Cookie|關於|廣告|客服|開戶|下單|APP|隱私|RSS', txt):
                titles.append(txt)
        seen, uniq = set(), []
        for t in titles:
            if t not in seen:
                seen.add(t); uniq.append(t)
        return uniq, r.status_code
    except Exception as e:
        return f"err:{str(e)[:60]}", 0


# ============================================================
# 1. 優閒聊財經(透過iTunes Lookup API反查RSS)
# ============================================================
def probe_youxian():
    print("\n" + "=" * 62)
    print("1. 優閒聊財經(Apple ID→iTunes Lookup API反查RSS)")
    print("=" * 62)
    apple_id = "1765675660"
    lookup_url = f"https://itunes.apple.com/lookup?id={apple_id}"
    try:
        r = requests.get(lookup_url, headers=UA, timeout=20)
        print(f"  iTunes Lookup [{r.status_code}] {lookup_url}")
        if r.status_code != 200:
            return
        data = r.json()
        results = data.get("results", [])
        if not results:
            print("  ⚠ 查無結果")
            return
        feed_url = results[0].get("feedUrl")
        print(f"  反查到RSS網址: {feed_url}")
        if not feed_url:
            print("  ⚠ 沒有feedUrl欄位")
            return
        titles, code = try_rss(feed_url)
        print(f"\n  RSS [{code}] {feed_url}")
        if isinstance(titles, list) and titles:
            for t, d in titles[:10]:
                print(f"    · {t}  [{d}]")
            report_hits("優閒聊財經", [t for t, d in titles])
        else:
            print(f"  {titles if isinstance(titles,str) else '無標題'}")
    except Exception as e:
        print(f"  錯誤: {e}")


# ============================================================
# 2. 聚財網 wearn.com
# ============================================================
def probe_wearn():
    print("\n" + "=" * 62)
    print("2. 聚財網 wearn.com(投資論壇,品質疑慮較高)")
    print("=" * 62)
    candidates = [
        "https://www.wearn.com/rss.asp",
        "https://www.wearn.com/bbs/active.asp",
    ]
    for url in candidates:
        titles, code = try_html(url)
        print(f"  HTML [{code}] {url} → {len(titles) if isinstance(titles,list) else 0}則")
        if isinstance(titles, list) and titles:
            for t in titles[:10]:
                print(f"    · {t}")
            report_hits("聚財網", titles)
            return
        else:
            print(f"    {titles if isinstance(titles,str) else '無標題'}")


# ============================================================
# 3. MarketWatch(科技類)
# ============================================================
def probe_marketwatch():
    print("\n" + "=" * 62)
    print("3. MarketWatch(嘗試科技類專屬feed)")
    print("=" * 62)
    candidates = [
        "https://feeds.content.dowjones.io/public/rss/mw_topstories",
        "https://feeds.content.dowjones.io/public/rss/mw_technology",
        "https://feeds.content.dowjones.io/public/rss/mw_marketpulse",
    ]
    for url in candidates:
        titles, code = try_rss(url)
        print(f"  RSS [{code}] {url} → {len(titles) if isinstance(titles,list) else 0}則")
        if isinstance(titles, list) and titles:
            for t, d in titles[:10]:
                print(f"    · {t}")
            report_hits(f"MarketWatch({url.split('/')[-1]})", [t for t, d in titles])
        else:
            print(f"    {titles if isinstance(titles,str) else '無標題'}")


def main():
    print("############################################################")
    print("3個候選來源 — 合併探測")
    print("############################################################")
    probe_youxian()
    probe_wearn()
    probe_marketwatch()
    print("\n" + "#" * 62)
    print("→ 全部結果貼回給 Claude,逐一決定併入/放棄")
    print("#" * 62)


if __name__ == "__main__":
    main()
