# -*- coding: utf-8 -*-
"""
============================================================
news_sources.py — 補充新聞來源池(中央社RSS + MoneyDJ HTML)
============================================================
為 src1/src4 提供鉅亨以外的當日新聞標題,擴大題材關鍵字統計樣本。

來源(探測驗證可用):
  中央社-產經 RSS  feeds.feedburner.com/rsscna/finance     ★乾淨
  中央社-科技 RSS  feeds.feedburner.com/rsscna/technology  ★乾淨
  MoneyDJ-台股 HTML ListNewArticles.aspx?svc=NW&a=X0100001 ★可用(加過濾)

限制:RSS/HTML 只有「當下最新」標題(無20天歷史),故當「當日新聞加成」用,
     計入 src1 的近期(recent)樣本,不獨立算時序暴增。
版權:只取標題做關鍵字統計,不儲存全文。

用法(src1 併入):
  from news_sources import fetch_supplement_titles
  extra_titles = fetch_supplement_titles()   # 回傳今天的標題 list
  # 統計某關鍵字時,把 extra_titles 裡含該詞的計入 recent
============================================================
"""
import requests
import xml.etree.ElementTree as ET
import re

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"}

CNA_FEEDS = [
    "https://feeds.feedburner.com/rsscna/finance",
    "https://feeds.feedburner.com/rsscna/technology",
]
MONEYDJ_HTML = "https://www.moneydj.com/KMDJ/Common/ListNewArticles.aspx?svc=NW&a=X0100001"
WEALTH_RSS = "https://www.wealth.com.tw/rss"           # 財訊 RSS(乾淨)
WAPEOPLE_HTML = "https://www.wa-people.com/"            # Wa-people(半導體/光電硬題材)
TECHNEWS_HTML = "https://technews.tw/"                  # TechNews 科技新報(題材密度高)
EETIMES_RSS = "https://www.eettaiwan.com/feed/"         # EE Times(電子工程/先進封裝深度)
TRENDFORCE_RSS = "https://www.trendforce.com/news/feed" # TrendForce英文(研究機構產業情報)

# HTML 導覽雜訊過濾
NOISE = re.compile(r'MoneyDJ社論|MoneyDJ理財網|加入會員|查詢密碼|登入|首頁|更多|下一頁|版權|Cookie|理財網|iQuote|專題報導|個人理財|商城|水晶|鹽燈|詐騙|澄清聲明|報名|購買|電子報|關於我們|廣告')


def _fetch_cna(url):
    try:
        r = requests.get(url, headers=UA, timeout=20)
        if r.status_code != 200:
            return []
        root = ET.fromstring(r.text)
        return [it.find("title").text.strip() for it in root.iter("item")
                if it.find("title") is not None and it.find("title").text]
    except Exception:
        return []


def _fetch_moneydj():
    try:
        r = requests.get(MONEYDJ_HTML, headers=UA, timeout=20)
        r.encoding = "utf-8"
        if r.status_code != 200:
            return []
        titles = []
        for m in re.finditer(r'<a[^>]*>([^<]{8,60})</a>', r.text):
            txt = m.group(1).strip()
            if re.search(r'[\u4e00-\u9fff]', txt) and not NOISE.search(txt):
                titles.append(txt)
        # 去重保序
        seen, uniq = set(), []
        for t in titles:
            if t not in seen:
                seen.add(t); uniq.append(t)
        return uniq
    except Exception:
        return []


def _fetch_wealth():
    """財訊 RSS(乾淨)。"""
    try:
        r = requests.get(WEALTH_RSS, headers=UA, timeout=20)
        if r.status_code != 200:
            return []
        root = ET.fromstring(r.text)
        return [it.find("title").text.strip() for it in root.iter("item")
                if it.find("title") is not None and it.find("title").text]
    except Exception:
        return []


def _fetch_wapeople():
    """Wa-people HTML(半導體/光電硬題材)。"""
    return _fetch_html_titles(WAPEOPLE_HTML)


def _fetch_technews():
    """TechNews 科技新報 HTML(題材密度高)。"""
    return _fetch_html_titles(TECHNEWS_HTML)


def _fetch_eetimes():
    """EE Times Taiwan RSS(電子工程/先進封裝深度,命中率高)。"""
    try:
        r = requests.get(EETIMES_RSS, headers=UA, timeout=20)
        if r.status_code != 200:
            return []
        root = ET.fromstring(r.text)
        return [it.find("title").text.strip() for it in root.iter("item")
                if it.find("title") is not None and it.find("title").text]
    except Exception:
        return []


def _fetch_trendforce():
    """TrendForce 英文 RSS(研究機構第一手產業情報)。去掉[News]/[Insights]前綴。"""
    try:
        r = requests.get(TRENDFORCE_RSS, headers=UA, timeout=20)
        if r.status_code != 200:
            return []
        root = ET.fromstring(r.text)
        titles = []
        for it in root.iter("item"):
            t = it.find("title")
            if t is not None and t.text:
                titles.append(re.sub(r'^\[[^\]]+\]\s*', '', t.text.strip()))
        return titles
    except Exception:
        return []


def _fetch_html_titles(url):
    """通用 HTML 標題抽取(a標籤含中文、過濾導覽雜訊)。"""
    try:
        r = requests.get(url, headers=UA, timeout=20)
        r.encoding = "utf-8"
        if r.status_code != 200:
            return []
        titles = []
        for m in re.finditer(r'<a[^>]*>([^<]{10,55})</a>', r.text):
            txt = m.group(1).strip()
            if re.search(r'[\u4e00-\u9fff]', txt) and not NOISE.search(txt):
                titles.append(txt)
        seen, uniq = set(), []
        for t in titles:
            if t not in seen:
                seen.add(t); uniq.append(t)
        return uniq
    except Exception:
        return []


def fetch_supplement_titles():
    """抓中央社+MoneyDJ 今日標題,回傳去重後的 list。(src1 用)"""
    titles = []
    for url in CNA_FEEDS:
        titles += _fetch_cna(url)
    titles += _fetch_moneydj()
    seen, uniq = set(), []
    for t in titles:
        if t and t not in seen:
            seen.add(t); uniq.append(t)
    return uniq


def fetch_titles_by_source():
    """回傳 {來源: [標題]},供 src4 做跨源交叉。(src4 用)
    來源多 = 原料廣;跨源出現的詞 = 更可能是真題材,不是單一媒體用語。"""
    return {
        "cna": [t for url in CNA_FEEDS for t in _fetch_cna(url)],
        "moneydj": _fetch_moneydj(),
        "wealth": _fetch_wealth(),
        "wapeople": _fetch_wapeople(),
        "technews": _fetch_technews(),
        "eetimes": _fetch_eetimes(),
        "trendforce": _fetch_trendforce(),
    }


if __name__ == "__main__":
    ts = fetch_supplement_titles()
    print(f"補充來源今日標題:{len(ts)} 則")
    for t in ts[:20]:
        print(f"  · {t[:52]}")
