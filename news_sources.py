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
# 鉅亨分類拉取(2026-09-10新增,補NPO案例暴露的缺口:
# 正確端點是 newslist/category/{slug},之前src1開發時誤用news/category/失敗過。
# wd_stock(美股/國際股)這個分類,原本的廣詞搜尋池(台股本位)幾乎不會觸及,
# 但「國際市場先發生、還沒被主流報導」的訊號常常先出現在這裡。
CNYES_CATEGORY_BASE = "https://api.cnyes.com/media/api/v1/newslist/category"
CNYES_CATEGORIES = ["wd_stock", "headline"]  # 美股/國際股, 頭條(綜合廣度備援)
WAPEOPLE_HTML = "https://www.wa-people.com/"            # Wa-people(半導體/光電硬題材)
TECHNEWS_HTML = "https://technews.tw/"                  # TechNews 科技新報(題材密度高)
EETIMES_RSS = "https://www.eettaiwan.com/feed/"         # EE Times(電子工程/先進封裝深度)
TRENDFORCE_RSS = "https://www.trendforce.com/news/feed" # TrendForce英文(研究機構產業情報)
CTEE_HTML = "https://www.ctee.com.tw/livenews/ctee"      # 工商時報即時新聞(HTML解析,無公開RSS)
EDN_RSS = "https://money.udn.com/rssfeed/news/1001/5590?ch=money"  # 經濟日報-證券分類(2026-09-14探測確認)
STOCKRICE_RSS = "https://feeds.soundon.fm/podcasts/537b7401-756c-4d0d-b1df-36a49e2793d3.xml"  # 股海飯桶Podcast(半導體供應鏈,每週二次)
SEMIANALYSIS_RSS = "https://newsletter.semianalysis.com/feed"  # SemiAnalysis(高品質AI/半導體深度分析,2026-09-15確認頻率近乎每週數篇)
AMINEXT_RSS = "https://www.aminext.blog/en/blog-feed.xml"  # AmiNext科技筆記(選題含半導體但範圍較廣,偶有國防/總經題材)
IC975_RSS = "https://www.ic975.com/feed/hitech/"  # IC之音科技咖(全站節目大雜燴,靠IC975_KEEP_PREFIXES過濾出科技相關子節目)
STATEMENTDOG_PODCAST_RSS = "https://feed.firstory.me/rss/user/clcftm46z000201z45w1c47fi"  # 財報狗Podcast(舊Firstory網址,2026-09-15確認仍有效)
TECHORANGE_RSS = "https://feeds.soundon.fm/podcasts/ead686e9-4513-4217-beb5-5fa4d215860d.xml"  # 科技報橘「科技早餐」(2026-09-15確認,每日更新,密度極高)

# IC之音科技咖是全電台節目大雜燴(含生活/歷史/親子類與科技無關內容),
# 只保留標題開頭是這些科技相關子節目標籤的集數,濾掉其餘雜訊
IC975_KEEP_PREFIXES = ("【科技領航家】", "【iSEE夢想家】", "【科技聽IC】", "【DIGITIMES每日新聞】",
                       "【IC部落格】", "【科技行腳】", "【零碳未來】")

# Telegram頻道(創作者本人自營,非第三方未經授權轉載,只截短片段+保留出處連結)
TELEGRAM_CHANNELS = {
    "gooaye_view": {"handle": "Gooaye", "label": "股癌(謝孟恭)"},
    "investanchors": {"handle": "investanchors", "label": "定錨產業筆記"},
}
TELEGRAM_SNIPPET_MAX_LEN = 80  # 硬性截斷,只取標題等級片段,不存完整貼文全文

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


def _fetch_ctee():
    """工商時報即時新聞 HTML(無公開RSS,2026-09-14探測確認HTML可用)。"""
    return _fetch_html_titles(CTEE_HTML)


def _fetch_edn():
    """經濟日報-證券分類 RSS(2026-09-14探測確認,分類代號5590)。"""
    try:
        r = requests.get(EDN_RSS, headers=UA, timeout=20)
        if r.status_code != 200:
            return []
        root = ET.fromstring(r.text)
        return [it.find("title").text.strip() for it in root.iter("item")
                if it.find("title") is not None and it.find("title").text]
    except Exception:
        return []


def _fetch_simple_rss(url, timeout=25):
    """通用RSS標題抓取,供股海飯桶/SemiAnalysis/AmiNext/財報狗共用。"""
    try:
        r = requests.get(url, headers=UA, timeout=timeout)
        if r.status_code != 200:
            return []
        root = ET.fromstring(r.text)
        return [it.find("title").text.strip() for it in root.iter("item")
                if it.find("title") is not None and it.find("title").text]
    except Exception:
        return []


def _fetch_stockrice():
    """股海飯桶Podcast RSS(半導體供應鏈深度分析,每週二次,2026-09-15確認可用)。"""
    return _fetch_simple_rss(STOCKRICE_RSS)


def _fetch_semianalysis():
    """SemiAnalysis(高品質AI/半導體深度分析,2026-09-15確認頻率近乎每週數篇,免費層仍有標題+摘要)。"""
    return _fetch_simple_rss(SEMIANALYSIS_RSS)


def _fetch_aminext():
    """AmiNext科技筆記(2026-09-15確認RSS可用,選題含半導體但範圍較廣,偶有國防/總經題材)。"""
    return _fetch_simple_rss(AMINEXT_RSS)


def _fetch_statementdog_podcast():
    """財報狗Podcast(2026-09-15確認舊Firstory網址仍有效,內容精準度高)。"""
    return _fetch_simple_rss(STATEMENTDOG_PODCAST_RSS)


def _fetch_techorange():
    """科技報橘「科技早餐」(2026-09-15確認,正牌媒體流線傳媒,每日更新,
    內容全免費完整無付費牆,密度極高,直接命中HBM/CoWoS/矽光子等追蹤題材)。"""
    return _fetch_simple_rss(TECHORANGE_RSS)


def _fetch_ic975():
    """IC之音科技咖(全站節目大雜燴,只保留IC975_KEEP_PREFIXES指定的科技相關子節目標題,
    濾掉生活/歷史/親子類等無關內容)。"""
    titles = _fetch_simple_rss(IC975_RSS, timeout=30)
    return [t for t in titles if t.startswith(IC975_KEEP_PREFIXES)]


def _fetch_telegram():
    """股癌+定錨產業筆記官方Telegram頻道(創作者本人自營,非未經授權轉載)。
    每則只截80字硬性上限,不存完整貼文全文,避免連頻道主轉貼的第三方新聞
    完整段落也一併存進系統。回傳格式跟其他RSS來源一致(純標題list)。"""
    all_snippets = []
    for key, info in TELEGRAM_CHANNELS.items():
        url = f"https://t.me/s/{info['handle']}"
        try:
            r = requests.get(url, headers=UA, timeout=20)
            if r.status_code != 200:
                continue
            pattern = (r'data-post="(?:' + re.escape(info["handle"]) + r')/\d+"[^>]*>.*?'
                       r'class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>')
            blocks = re.findall(pattern, r.text, re.DOTALL)
            for raw_text in blocks:
                text = re.sub(r'<[^>]+>', '', raw_text)
                text = re.sub(r'\s+', ' ', text).strip()
                if text:
                    all_snippets.append(text[:TELEGRAM_SNIPPET_MAX_LEN])
        except Exception:
            continue
    return all_snippets


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


def _fetch_cnyes_category(slug, pages=3):
    """鉅亨分類拉取(不靠關鍵字,直接拉該分類最新新聞)。
    正確端點:newslist/category/{slug},非news/category/(曾在src1開發時測試過後者失敗)。
    """
    titles = []
    for page in range(1, pages + 1):
        url = f"{CNYES_CATEGORY_BASE}/{slug}?page={page}&limit=30"
        try:
            r = requests.get(url, headers=UA, timeout=20)
            if r.status_code != 200:
                break
            data = r.json()
            items = ((data.get("items") or {}).get("data")
                     or data.get("data") or [])
            if not items:
                break
            for it in items:
                t = it.get("title")
                if t:
                    titles.append(t)
        except Exception:
            break
    return titles


def _fetch_cnyes_categories():
    """抓所有設定的鉅亨分類(美股/頭條),回傳去重標題list。"""
    titles = []
    for slug in CNYES_CATEGORIES:
        titles += _fetch_cnyes_category(slug)
    seen, uniq = set(), []
    for t in titles:
        if t not in seen:
            seen.add(t); uniq.append(t)
    return uniq


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
    來源多 = 原料廣;跨源出現的詞 = 更可能是真題材,不是單一媒體用語。
    2026-09-10新增cnyes_intl(鉅亨美股/國際分類直拉)+cnbc_yahoo,
    解決原本廣詞搜尋池台股本位、漏掉國際市場先行訊號的缺口(NPO案例)。"""
    result = {
        "cna": [t for url in CNA_FEEDS for t in _fetch_cna(url)],
        "moneydj": _fetch_moneydj(),
        "wealth": _fetch_wealth(),
        "wapeople": _fetch_wapeople(),
        "technews": _fetch_technews(),
        "eetimes": _fetch_eetimes(),
        "trendforce": _fetch_trendforce(),
        "cnyes_intl": _fetch_cnyes_categories(),
        "ctee": _fetch_ctee(),
        "edn": _fetch_edn(),
        "stockrice": _fetch_stockrice(),
        "semianalysis": _fetch_semianalysis(),
        "aminext": _fetch_aminext(),
        "ic975": _fetch_ic975(),
        "statementdog_pod": _fetch_statementdog_podcast(),
        "techorange": _fetch_techorange(),
        "telegram": _fetch_telegram(),
    }
    try:
        from intl_news import fetch_intl_titles
        result["cnbc_yahoo"] = fetch_intl_titles()
    except Exception:
        result["cnbc_yahoo"] = []
    return result


if __name__ == "__main__":
    ts = fetch_supplement_titles()
    print(f"補充來源今日標題:{len(ts)} 則")
    for t in ts[:20]:
        print(f"  · {t[:52]}")
