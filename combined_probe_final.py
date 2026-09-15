# -*- coding: utf-8 -*-
"""
combined_probe.py — 剩餘4個待驗證來源,一次測試
============================================================
1. SemiAnalysis(付費深度分析,兩週一次)
2. AmiNext部落格(免費深度分析,Wix架站)
3. IC之音科技咖(每日更新Podcast,已知確切網址)
4. 財報狗Podcast(待驗證SoundOn UUID)
5. Telegram: 股癌+定錨產業筆記(創作者本人自營,只截80字短片段+出處連結)
============================================================
"""
import requests
import re
import xml.etree.ElementTree as ET

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"}

CHECK_KW = ["CoWoS", "CoPoS", "CPO", "Hybrid Bonding", "矽光子", "Silicon Photonics",
            "FOPLP", "玻璃基板", "先進封裝", "Advanced Packaging", "HBM", "TSMC", "台積電",
            "SoIC", "3D IC", "InP", "半導體", "封裝", "sidecar", "HVDC", "800V", "記憶體",
            "晶圓代工", "DRAM", "NAND", "ASIC", "矽晶圓", "Rubin", "功率半導體", "群聯", "法說"]


# ============================================================
# Telegram頻道用常數與輔助函式(股癌+定錨產業筆記)
# ============================================================
SNIPPET_MAX_LEN = 80  # 硬性截斷長度,只取標題等級片段,不存完整貼文

CHANNELS = {
    "gooaye_view": {"handle": "Gooaye", "label": "股癌(謝孟恭)"},
    "investanchors": {"handle": "investanchors", "label": "定錨產業筆記"},
}


def _clean_text(raw_html_text):
    """去除HTML標籤與多餘空白。"""
    text = re.sub(r'<[^>]+>', '', raw_html_text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def fetch_channel(channel_key, pages=1):
    """抓單一頻道的最新貼文,回傳 [{snippet, link, source_label}] 列表。
    只取硬性截斷後的短片段+原始連結,不保留完整貼文內文。"""
    info = CHANNELS.get(channel_key)
    if not info:
        return []
    handle, label = info["handle"], info["label"]
    url = f"https://t.me/s/{handle}"

    try:
        r = requests.get(url, headers=UA, timeout=20)
        if r.status_code != 200:
            return []
        html = r.text
    except Exception:
        return []

    results = []
    # 每則貼文包在 tgme_widget_message_text 區塊,搭配對應的 data-post 連結
    # 用正則配對「貼文區塊」與其前後的 post id,寬鬆比對避免結構微調就失效
    post_blocks = re.findall(
        r'data-post="(Gooaye/\d+|investanchors/\d+)"[^>]*>.*?'
        r'class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>',
        html, re.DOTALL
    )
    for post_id, raw_text in post_blocks:
        text = _clean_text(raw_text)
        if not text:
            continue
        snippet = text[:SNIPPET_MAX_LEN]
        if len(text) > SNIPPET_MAX_LEN:
            snippet += "..."
        results.append({
            "snippet": snippet,
            "link": f"https://t.me/{post_id}",
            "source_label": label,
        })
    return results




def strip_html(text):
    return re.sub(r'<[^>]+>', '', text or '').strip()


def report_hits(name, titles):
    joined = " ".join(titles)
    hits = {k: joined.count(k) for k in CHECK_KW if joined.count(k) > 0}
    print(f"\n  → {name} 共{len(titles)}則, 命中關鍵詞: "
          f"{dict(sorted(hits.items(), key=lambda x: -x[1])) if hits else '無'}")


# ============================================================
# 1. SemiAnalysis
# ============================================================
def probe_semianalysis():
    print("\n" + "=" * 62)
    print("1. SemiAnalysis")
    print("=" * 62)
    candidates = [
        "https://semianalysis.substack.com/feed",
        "https://newsletter.semianalysis.com/feed",
    ]
    for url in candidates:
        print(f"\n■ {url}")
        try:
            r = requests.get(url, headers=UA, timeout=20)
            print(f"  狀態 {r.status_code}, 長度 {len(r.text)}")
            if r.status_code != 200:
                continue
            root = ET.fromstring(r.text)
            items = list(root.iter("item"))
            print(f"  找到 {len(items)} 則")
            titles = []
            for it in items[:8]:
                title = it.find("title")
                desc = it.find("description")
                pubdate = it.find("pubDate")
                t = title.text.strip() if title is not None and title.text else "(無標題)"
                d = strip_html(desc.text)[:150] if desc is not None and desc.text else "(無摘要)"
                print(f"    · {t}")
                print(f"      發布:{pubdate.text if pubdate is not None else '?'}  摘要:{d}")
                titles.append(t + " " + d)
            if titles:
                report_hits("SemiAnalysis", titles)
                return
        except Exception as e:
            print(f"  錯誤: {str(e)[:100]}")


# ============================================================
# 2. AmiNext
# ============================================================
def probe_aminext():
    print("\n" + "=" * 62)
    print("2. AmiNext 科技筆記")
    print("=" * 62)
    rss_candidates = [
        "https://www.aminext.blog/en/blog-feed.xml",
        "https://www.aminext.blog/blog-feed.xml",
    ]
    for url in rss_candidates:
        print(f"\n■ RSS: {url}")
        try:
            r = requests.get(url, headers=UA, timeout=20)
            print(f"  狀態 {r.status_code}, 長度 {len(r.text)}")
            if r.status_code != 200:
                continue
            body = r.text
            if not (body.lstrip().startswith("<?xml") or "<rss" in body[:300]):
                print(f"  非XML,前150字: {body[:150]}")
                continue
            root = ET.fromstring(body)
            items = list(root.iter("item"))
            print(f"  找到 {len(items)} 則")
            titles = []
            for it in items[:10]:
                title = it.find("title")
                t = title.text.strip() if title is not None and title.text else "(無標題)"
                print(f"    · {t}")
                titles.append(t)
            if titles:
                report_hits("AmiNext(RSS)", titles)
                return
        except Exception as e:
            print(f"  錯誤: {str(e)[:100]}")

    print("\n■ HTML備援: 分類頁")
    html_pages = [
        "https://www.aminext.blog/en/all-notes/categories/semicon-1",
        "https://www.aminext.blog/en/all-notes",
    ]
    for url in html_pages:
        try:
            r = requests.get(url, headers=UA, timeout=20)
            r.encoding = "utf-8"
            print(f"\n  {url}  [狀態{r.status_code}]")
            if r.status_code != 200:
                continue
            titles = []
            for m in re.finditer(r'<a[^>]*href="/en/post/[^"]+"[^>]*>([^<]{10,100})</a>', r.text):
                t = m.group(1).strip()
                if t:
                    titles.append(t)
            seen, uniq = set(), []
            for t in titles:
                if t not in seen:
                    seen.add(t); uniq.append(t)
            print(f"  解析出標題: {len(uniq)}")
            for t in uniq[:10]:
                print(f"    · {t}")
            if uniq:
                report_hits("AmiNext(HTML)", uniq)
                return
        except Exception as e:
            print(f"  錯誤: {str(e)[:100]}")


# ============================================================
# 3. IC之音科技咖
# ============================================================
def probe_ic975():
    print("\n" + "=" * 62)
    print("3. IC之音「科技咖」")
    print("=" * 62)
    url = "https://www.ic975.com/feed/hitech/"
    print(f"\n■ {url}")
    try:
        r = requests.get(url, headers=UA, timeout=25)
        print(f"  狀態 {r.status_code}, 長度 {len(r.text)}")
        if r.status_code != 200:
            return
        root = ET.fromstring(r.text)
        items = list(root.iter("item"))
        print(f"  找到 {len(items)} 集")
        titles = []
        for it in items[:15]:
            title = it.find("title")
            desc = it.find("description")
            pubdate = it.find("pubDate")
            t = title.text.strip() if title is not None and title.text else "(無標題)"
            d = strip_html(desc.text)[:150] if desc is not None and desc.text else ""
            print(f"\n  · {t}")
            print(f"    發布: {pubdate.text if pubdate is not None else '?'}")
            if d:
                print(f"    摘要: {d}")
            titles.append(t + " " + d)
        if titles:
            report_hits("IC之音科技咖", titles)
    except Exception as e:
        print(f"  錯誤: {e}")


# ============================================================
# 4. 財報狗Podcast
# ============================================================
def probe_statementdog():
    print("\n" + "=" * 62)
    print("4. 財報狗 Podcast")
    print("=" * 62)
    candidates = [
        ("SoundOn(猜測,跟股海飯桶同格式)", "https://feeds.soundon.fm/podcasts/e75f72d6-a458-4982-861e-6c5fbad85956.xml"),
        ("Firstory(舊,可能仍有效)", "https://feed.firstory.me/rss/user/clcftm46z000201z45w1c47fi"),
    ]
    for name, url in candidates:
        print(f"\n■ {name}\n  {url}")
        try:
            r = requests.get(url, headers=UA, timeout=25)
            print(f"  狀態 {r.status_code}, 長度 {len(r.text)}")
            if r.status_code != 200:
                continue
            body = r.text
            if not (body.lstrip().startswith("<?xml") or "<rss" in body[:300]):
                print(f"  非XML,前150字: {body[:150]}")
                continue
            root = ET.fromstring(body)
            items = list(root.iter("item"))
            print(f"  找到 {len(items)} 集")
            titles = []
            for it in items[:10]:
                title = it.find("title")
                pubdate = it.find("pubDate")
                t = title.text.strip() if title is not None and title.text else "(無標題)"
                print(f"    · {t}  [{pubdate.text if pubdate is not None else '?'}]")
                titles.append(t)
            if titles:
                report_hits("財報狗", titles)
                print(f"\n  ✓ 這個網址可用: {url}")
                return
        except Exception as e:
            print(f"  錯誤: {str(e)[:100]}")




# ============================================================
# 5. Telegram頻道(股癌 + 定錨產業筆記)
# ============================================================
def probe_telegram():
    print("\n" + "=" * 62)
    print("5. Telegram頻道: 股癌 + 定錨產業筆記")
    print("=" * 62)
    for key, info in CHANNELS.items():
        print(f"\n■ {info['label']} (@{info['handle']})")
        url = f"https://t.me/s/{info['handle']}"
        try:
            r = requests.get(url, headers=UA, timeout=20)
            print(f"  狀態 {r.status_code}, 長度 {len(r.text)}")
            if r.status_code != 200:
                continue
            pattern = (r'data-post="(Gooaye/\d+|investanchors/\d+)"[^>]*>.*?'
                       r'class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>')
            post_blocks = re.findall(pattern, r.text, re.DOTALL)
            print(f"  比對到貼文區塊數: {len(post_blocks)}")
            titles = []
            for post_id, raw_text in post_blocks[:10]:
                text = _clean_text(raw_text)
                if not text:
                    continue
                snippet = text[:SNIPPET_MAX_LEN]
                print(f"    · {snippet}")
                print(f"      出處: https://t.me/{post_id}")
                titles.append(snippet)
            if titles:
                report_hits(info["label"], titles)
            else:
                print("  ⚠ 正則沒比對到任何貼文,可能結構跟預期不同,需要看原始HTML調整")
        except Exception as e:
            print(f"  錯誤: {str(e)[:100]}")


def main():
    print("############################################################")
    print("4個待驗證來源 — 合併探測")
    print("############################################################")
    probe_semianalysis()
    probe_aminext()
    probe_ic975()
    probe_statementdog()
    probe_telegram()
    print("\n" + "#" * 62)
    print("→ 全部結果貼回給 Claude,逐一決定併入/放棄")
    print("#" * 62)


if __name__ == "__main__":
    main()
