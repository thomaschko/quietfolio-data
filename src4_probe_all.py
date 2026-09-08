# -*- coding: utf-8 -*-
"""
src4_probe_all.py — 9個科技媒體來源大探測
============================================================
一次測:EE Times / CTimes / TrendForce / MEM / bnext / TechNice /
        iEK產業情報 / 關鍵評論網科技 / INSIDE
每個試 RSS 優先、HTML 備援,評估題材密度決定納入哪幾個。
============================================================
"""
import requests
import xml.etree.ElementTree as ET
import re
import datetime as dt

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"}

SOURCES = {
    "EETimes": {"rss": ["https://www.eettaiwan.com/feed/"], "html": ["https://www.eettaiwan.com/"]},
    "CTimes-cmnews": {"rss": ["https://cmnews.com.tw/rss", "https://cmnews.com.tw/feed"], "html": ["https://cmnews.com.tw/"]},
    "TrendForce-EN": {"rss": ["https://www.trendforce.com/rss", "https://www.trendforce.com/news/feed"], "html": ["https://www.trendforce.com/news"]},
    "MEM": {"rss": ["https://www.mem.com.tw/feed", "https://www.mem.com.tw/rss"], "html": ["https://www.mem.com.tw/"]},
    "bnext數位時代": {"rss": ["https://www.bnext.com.tw/rss", "https://www.bnext.com.tw/feed"], "html": ["https://www.bnext.com.tw/"]},
    "TechNice科技島": {"rss": ["https://www.technice.com.tw/feed/", "https://www.technice.com.tw/rss/"], "html": ["https://www.technice.com.tw/"]},
    "iEK產業情報": {"rss": [], "html": ["https://ieknet.iek.org.tw/ieknews/Default.aspx"]},
    "關鍵評論網-科技": {"rss": ["https://www.thenewslens.com/rss/tech"], "html": ["https://www.thenewslens.com/category/tech"]},
    "INSIDE": {"rss": ["https://www.inside.com.tw/feed/rss", "https://www.inside.com.tw/rss"], "html": ["https://www.inside.com.tw/"]},
}

CHECK_KW = ["CoWoS","CoPoS","CPO","矽光子","光通訊","HBM","HBM4","DRAM","DDR5","記憶體","NAND",
            "ASIC","SiC","碳化矽","氮化鎵","先進封裝","玻璃基板","FOPLP","面板級封裝","HVDC",
            "人形機器人","矽智財","台積電","輝達","AI伺服器","散熱","液冷","半導體","晶圓","面板",
            "伺服器","光電","封裝","基板","超級循環","記憶體","晶片","IC設計","車用","電動車"]

# 通用碎詞(算題材密度時排除,免得被灌水)
GENERIC = {"科技","數位","創新","趨勢","分析","觀點","專題","新聞","報導","文章","專欄"}


def try_rss(url):
    try:
        r = requests.get(url, headers=UA, timeout=18)
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
        return f"err:{str(e)[:35]}", 0


def try_html(url):
    try:
        r = requests.get(url, headers=UA, timeout=18)
        r.encoding = "utf-8"
        if r.status_code != 200:
            return None, r.status_code
        titles = []
        for m in re.finditer(r'<a[^>]*>([^<]{10,60})</a>', r.text):
            txt = m.group(1).strip()
            if re.search(r'[\u4e00-\u9fff]', txt) and not re.search(r'登入|會員|訂閱|首頁|更多|下一頁|版權|Cookie|關於|廣告|購買|報名', txt):
                titles.append(txt)
        seen, uniq = set(), []
        for t in titles:
            if t not in seen:
                seen.add(t); uniq.append(t)
        return uniq, r.status_code
    except Exception as e:
        return f"err:{str(e)[:35]}", 0


def theme_density(titles):
    """算題材命中密度:命中題材詞的標題數 / 總標題數"""
    if not titles:
        return 0, 0
    joined = " ".join(titles)
    hit = sum(1 for k in CHECK_KW if k in joined)
    return hit, len(titles)


def main():
    print("=" * 62)
    print(f"9個科技媒體來源大探測  {dt.date.today()}")
    print("=" * 62)
    results = {}
    for name, cfg in SOURCES.items():
        print(f"\n■ {name}")
        got_titles = None
        got_via = None
        # RSS 優先
        for url in cfg.get("rss", []):
            titles, code = try_rss(url)
            if isinstance(titles, list) and titles:
                print(f"  ✓RSS [{code}] {url}")
                got_titles, got_via = titles, "RSS"
                break
            else:
                print(f"  ·RSS [{code}] {url.split('//')[1][:38]} → {titles if isinstance(titles,str) else '無'}")
        # HTML 備援
        if not got_titles:
            for url in cfg.get("html", []):
                titles, code = try_html(url)
                if isinstance(titles, list) and titles:
                    print(f"  ✓HTML [{code}] {url}")
                    got_titles, got_via = titles, "HTML"
                    break
                else:
                    print(f"  ·HTML [{code}] {url.split('//')[1][:38]} → {titles if isinstance(titles,str) else '無'}")
        if got_titles:
            hit, total = theme_density(got_titles)
            print(f"  → {got_via} {total}則, 題材命中{hit}種, 範例:")
            for t in got_titles[:5]:
                print(f"    · {t[:50]}")
            results[name] = {"via": got_via, "total": total, "hit": hit}
        else:
            print(f"  ✗ 無可用端點")

    print("\n" + "=" * 62 + "\n總結(依題材密度排序,決定納入哪幾個)\n" + "=" * 62)
    ranked = sorted(results.items(), key=lambda x: -x[1]["hit"])
    for name, r in ranked:
        density = round(r["hit"] / r["total"] * 100, 1) if r["total"] else 0
        rec = "★推薦" if r["hit"] >= 8 else ("可考慮" if r["hit"] >= 4 else "雜訊多")
        print(f"  {name}: {r['via']} {r['total']}則, 命中{r['hit']}種題材詞 [{rec}]")
    print("\n→ 貼回給 Claude,挑題材密度高的3-4個併進 src4。")


if __name__ == "__main__":
    main()
