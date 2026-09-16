# -*- coding: utf-8 -*-
"""
moneydj_podcast_probe.py — MoneyDJ財經新聞 Podcast + 官方Telegram頻道探測
============================================================
1. MoneyDJ財經新聞Podcast:用iTunes Lookup API(加country=tw)反查RSS,
   記者各自有產業路線、集數常標記具體股票代號,內容看起來對口。
2. 官方Telegram頻道(t.me/moneydjnews):Podcast說明欄裡提到的官方頻道,
   同樣是創作者本人自營,可用跟股癌/定錨一樣的模式處理。
============================================================
"""
import requests
import re
import xml.etree.ElementTree as ET

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"}

CHECK_KW = ["CoWoS","CoPoS","CPO","矽光子","光通訊","HBM","HBM4","DRAM","DDR5","記憶體","NAND",
            "ASIC","SiC","碳化矽","氮化鎵","先進封裝","玻璃基板","FOPLP","HVDC","法說","毛利率",
            "營收","半導體","晶圓","封裝","AI伺服器","台積電","測試","功率半導體"]


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
        if not (body.lstrip().startswith("<?xml") or "<rss" in body[:300]):
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


# ============================================================
# 1. MoneyDJ財經新聞Podcast
# ============================================================
def probe_moneydj_podcast():
    print("\n" + "=" * 62)
    print("1. MoneyDJ財經新聞 Podcast(iTunes Lookup反查,加country=tw)")
    print("=" * 62)
    apple_id = "1531443831"
    for country_param in ["&country=tw", ""]:
        lookup_url = f"https://itunes.apple.com/lookup?id={apple_id}{country_param}"
        try:
            r = requests.get(lookup_url, headers=UA, timeout=20)
            print(f"  iTunes Lookup [{r.status_code}] {lookup_url}")
            if r.status_code != 200:
                continue
            data = r.json()
            results = data.get("results", [])
            if not results:
                print("  ⚠ 查無結果")
                continue
            feed_url = results[0].get("feedUrl")
            print(f"  反查到RSS網址: {feed_url}")
            if not feed_url:
                continue
            titles, code = try_rss(feed_url)
            print(f"\n  RSS [{code}] {feed_url}")
            if isinstance(titles, list) and titles:
                for t, d in titles[:10]:
                    print(f"    · {t}  [{d}]")
                report_hits("MoneyDJ財經新聞Podcast", [t for t, d in titles])
                return
            else:
                print(f"  {titles if isinstance(titles,str) else '無標題'}")
        except Exception as e:
            print(f"  錯誤: {e}")


# ============================================================
# 2. MoneyDJ官方Telegram頻道
# ============================================================
def probe_moneydj_telegram():
    print("\n" + "=" * 62)
    print("2. MoneyDJ官方Telegram頻道(t.me/moneydjnews)")
    print("=" * 62)
    handle = "moneydjnews"
    url = f"https://t.me/s/{handle}"
    try:
        r = requests.get(url, headers=UA, timeout=20)
        print(f"  狀態 {r.status_code}, 長度 {len(r.text)}")
        if r.status_code != 200:
            return
        pattern = (r'data-post="(?:' + re.escape(handle) + r')/\d+"[^>]*>.*?'
                   r'class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>')
        blocks = re.findall(pattern, r.text, re.DOTALL)
        print(f"  比對到貼文區塊數: {len(blocks)}")
        titles = []
        for raw_text in blocks[:10]:
            text = re.sub(r'<[^>]+>', '', raw_text)
            text = re.sub(r'\s+', ' ', text).strip()
            snippet = text[:80]
            if snippet:
                print(f"    · {snippet}")
                titles.append(snippet)
        if titles:
            report_hits("MoneyDJ Telegram", titles)
    except Exception as e:
        print(f"  錯誤: {e}")


def main():
    print("############################################################")
    print("MoneyDJ財經新聞 Podcast + 官方Telegram 探測")
    print("############################################################")
    probe_moneydj_podcast()
    probe_moneydj_telegram()
    print("\n" + "#" * 62)
    print("→ 結果貼回給 Claude,決定併入/放棄")
    print("#" * 62)


if __name__ == "__main__":
    main()
