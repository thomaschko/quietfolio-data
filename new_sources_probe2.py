# -*- coding: utf-8 -*-
"""
new_sources_probe2.py — 6個新候選來源合併探測
============================================================
1. 產業人物Wa-People Podcast(需驗證是否跟既有wapeople網站來源重疊)
2. BigGo財經(法說會AI摘要,內容深度不錯)
3. 富果部落格(個股分析+法說會備忘錄)
4. 永豐金豐雲學堂(內容夾雜大量免責聲明,需清洗)
5. 凱基證券即時新聞(疑似JS動態渲染,可能失敗)
6. TechNice科技島(2026-09-16修正:先前誤判成純職涯媒體,
   實際/issues/semicon/等分類下有扎實半導體/AI產業新聞)
============================================================
"""
import requests
import re
import xml.etree.ElementTree as ET

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"}

CHECK_KW = ["CoWoS","CoPoS","CPO","矽光子","光通訊","HBM","HBM4","DRAM","DDR5","記憶體","NAND",
            "ASIC","SiC","碳化矽","氮化鎵","先進封裝","玻璃基板","FOPLP","HVDC","法說","毛利率",
            "營收","半導體","晶圓","封裝","AI伺服器","台積電","測試","泰瑞達","探針卡"]


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
        titles = [it.find("title").text.strip() for it in root.iter("item")
                  if it.find("title") is not None and it.find("title").text]
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
                r'登入|會員|訂閱|首頁|更多|下一頁|版權|Cookie|關於|廣告|客服|開戶|下單|APP|隱私', txt):
                titles.append(txt)
        seen, uniq = set(), []
        for t in titles:
            if t not in seen:
                seen.add(t); uniq.append(t)
        return uniq, r.status_code
    except Exception as e:
        return f"err:{str(e)[:60]}", 0


# ============================================================
# 1. 產業人物Wa-People Podcast
# ============================================================
def probe_wapeople_podcast():
    print("\n" + "=" * 62)
    print("1. 產業人物Wa-People Podcast(對照既有wapeople網站來源)")
    print("=" * 62)
    url = "https://feeds.soundon.fm/podcasts/39dfac7d-76f0-43de-97ad-93303a03d7ed.xml"
    titles, code = try_rss(url)
    print(f"  [{code}] {url}")
    if isinstance(titles, list) and titles:
        for t in titles[:10]:
            print(f"    · {t}")
        report_hits("Wa-People Podcast", titles)
    else:
        print(f"  {titles if isinstance(titles,str) else '無標題'}")


# ============================================================
# 2. BigGo財經
# ============================================================
def probe_biggo():
    print("\n" + "=" * 62)
    print("2. BigGo財經")
    print("=" * 62)
    candidates_rss = [
        "https://finance.biggo.com.tw/feed",
        "https://finance.biggo.com.tw/rss",
    ]
    for url in candidates_rss:
        titles, code = try_rss(url)
        print(f"  RSS [{code}] {url} → {titles if isinstance(titles,str) else (str(len(titles))+'則' if titles else '無')}")
        if isinstance(titles, list) and titles:
            for t in titles[:8]:
                print(f"    · {t}")
            report_hits("BigGo財經(RSS)", titles)
            return
    print("\n  HTML備援:")
    titles, code = try_html("https://finance.biggo.com.tw/")
    print(f"  HTML [{code}] 解析出{len(titles) if isinstance(titles,list) else 0}則")
    if isinstance(titles, list) and titles:
        for t in titles[:10]:
            print(f"    · {t}")
        report_hits("BigGo財經(HTML)", titles)


# ============================================================
# 3. 富果部落格
# ============================================================
def probe_fugle():
    print("\n" + "=" * 62)
    print("3. 富果部落格")
    print("=" * 62)
    candidates_rss = [
        "https://blog.fugle.tw/feed",
        "https://blog.fugle.tw/feed/",
    ]
    for url in candidates_rss:
        titles, code = try_rss(url)
        print(f"  RSS [{code}] {url} → {titles if isinstance(titles,str) else (str(len(titles))+'則' if titles else '無')}")
        if isinstance(titles, list) and titles:
            for t in titles[:8]:
                print(f"    · {t}")
            report_hits("富果部落格(RSS)", titles)
            return
    print("\n  HTML備援:")
    titles, code = try_html("https://blog.fugle.tw/")
    print(f"  HTML [{code}] 解析出{len(titles) if isinstance(titles,list) else 0}則")
    if isinstance(titles, list) and titles:
        for t in titles[:10]:
            print(f"    · {t}")
        report_hits("富果部落格(HTML)", titles)


# ============================================================
# 4. 永豐金豐雲學堂
# ============================================================
def probe_sinotrade():
    print("\n" + "=" * 62)
    print("4. 永豐金豐雲學堂")
    print("=" * 62)
    titles, code = try_html("https://www.sinotrade.com.tw/richclub/news")
    print(f"  HTML [{code}] 解析出{len(titles) if isinstance(titles,list) else 0}則")
    if isinstance(titles, list) and titles:
        for t in titles[:10]:
            print(f"    · {t}")
        report_hits("永豐金豐雲學堂", titles)
    else:
        print(f"  {titles if isinstance(titles,str) else '無標題'}")


# ============================================================
# 5. 凱基證券即時新聞
# ============================================================
def probe_kgi():
    print("\n" + "=" * 62)
    print("5. 凱基證券即時新聞(疑似JS動態渲染,預期可能失敗)")
    print("=" * 62)
    titles, code = try_html("https://www.kgi.com.tw/zh-tw/product-market/news-and-announcement/realtime-news")
    print(f"  HTML [{code}] 解析出{len(titles) if isinstance(titles,list) else 0}則")
    if isinstance(titles, list) and titles:
        for t in titles[:10]:
            print(f"    · {t}")
        report_hits("凱基證券", titles)
    else:
        print(f"  {titles if isinstance(titles,str) else '無標題(可能為JS動態渲染)'}")


# ============================================================
# 6. TechNice科技島(2026-09-16修正:先前誤判,實際有扎實半導體新聞)
# ============================================================
def probe_technice():
    print("\n" + "=" * 62)
    print("6. TechNice科技島")
    print("=" * 62)
    candidates_rss = [
        "https://www.technice.com.tw/feed",
        "https://www.technice.com.tw/feed/",
    ]
    for url in candidates_rss:
        titles, code = try_rss(url)
        print(f"  RSS [{code}] {url} → {titles if isinstance(titles,str) else (str(len(titles))+'則' if titles else '無')}")
        if isinstance(titles, list) and titles:
            for t in titles[:8]:
                print(f"    · {t}")
            report_hits("TechNice(RSS)", titles)
            return
    print("\n  HTML備援(鎖定/issues/semicon/類產業新聞分類):")
    titles, code = try_html("https://www.technice.com.tw/category/issues/")
    print(f"  HTML [{code}] 解析出{len(titles) if isinstance(titles,list) else 0}則")
    if isinstance(titles, list) and titles:
        for t in titles[:10]:
            print(f"    · {t}")
        report_hits("TechNice(HTML)", titles)
    else:
        print(f"  {titles if isinstance(titles,str) else '無標題'}")


def main():
    print("############################################################")
    print("6個新候選來源 — 合併探測")
    print("############################################################")
    probe_wapeople_podcast()
    probe_biggo()
    probe_fugle()
    probe_sinotrade()
    probe_kgi()
    probe_technice()
    print("\n" + "#" * 62)
    print("→ 全部結果貼回給 Claude,逐一決定併入/放棄")
    print("#" * 62)


if __name__ == "__main__":
    main()
