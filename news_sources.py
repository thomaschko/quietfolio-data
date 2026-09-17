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
from urllib.parse import quote

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
WAPEOPLE_PODCAST_RSS = "https://feeds.soundon.fm/podcasts/39dfac7d-76f0-43de-97ad-93303a03d7ed.xml"  # 產業人物Wa-People Podcast(2026-09-16確認,個股深度訪談,跟既有wapeople網站版互補不重複)
FUGLE_BLOG_HTML = "https://blog.fugle.tw/"  # 富果部落格(無RSS,HTML解析,2026-09-16確認個股分析內容扎實)
YOUXIAN_RSS = "https://feeds.soundon.fm/podcasts/40f2d2d6-994c-4499-93ef-d5e7f2b24306.xml"  # 優閒聊財經(前身為優分析Podcast,2026-09-16確認密度極高)
GS_EXCHANGES_RSS = "https://feeds.megaphone.fm/GLD9218176758"  # Goldman Sachs Exchanges(2026-09-16確認,英文,機構級AI/總經分析,雜訊比例高於中文來源)
MONEYDJ_PODCAST_RSS = "https://feeds.soundon.fm/podcasts/489a6945-a341-40ca-88ab-73c174057634.xml"  # MoneyDJ財經新聞Podcast(2026-09-16確認,506集,命中密度極高:法說26/半導體12/台積電7)

# Google News RSS(2026-09-16確認)——搜尋引擎索引,橫跨大量長尾媒體,補足21個固定
# 媒體來源觸及不到的範圍。關鍵限制(已查證):不加when:會回傳中位數6.6天前的舊聞,
# 必須強制加when:天數窗口。另一個實測發現的風險:裸公司名稱查詢(如單獨查「台積電」)
# 會連八卦新聞都撈進來(員工緋聞等),故只用「公司/產業+限定詞」組合查詢,不用裸名稱。
GOOGLE_NEWS_QUERIES = [
    "矽光子 when:3d",
    "AI伺服器 供應鏈 when:3d",
    "台股 半導體 when:3d",
    "CoWoS 先進封裝 when:3d",
    "HBM 記憶體 when:3d",
]

# Google News美國版(2026-09-16確認)——補足英文來源不足的缺口。
# 關鍵發現:太廣泛的查詢(如"AI data center power")會被地方政治新聞
# (居民反對資料中心/川普評論)、行銷垃圾內容(不相關市場預測文)淹沒;
# 只留技術限定詞夠精準的查詢(矽光子/CoWoS/HBM),已實測確認高相關密度,
# 其中HBM查詢還抓到Reuters獨家(SK Hynix與Intel洽談赴美設廠生產記憶體)。
GOOGLE_NEWS_US_QUERIES = [
    "silicon photonics when:3d",
    "CoWoS advanced packaging when:3d",
    "HBM memory chip when:3d",
    "SiC GaN power semiconductor when:3d",
]

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


RSS_RECENT_DAYS = 7  # 部落格/Podcast類RSS的「近期」窗口(比src1的3天寬,涵蓋較低頻的來源如AmiNext/SemiAnalysis)


def _parse_rss_date(date_text):
    """解析RSS pubDate(標準RFC822格式),解析失敗回傳None(不代表要排除,由呼叫端決定)。
    2026-09-16修正:部分平台(如Megaphone/GS Exchanges)用「-0000」時區標記
    (RFC2822規範代表「時區不明」),Python的parsedate_to_datetime會回傳
    「沒有時區資訊」的datetime,拿去跟有時區的cutoff比較會直接拋TypeError,
    導致整個_fetch_simple_rss的try/except把結果靜默吞成空清單(偽裝成
    「沒有近期內容」,實際上是程式當掉)。修正:沒有時區資訊時,補上UTC。"""
    if not date_text:
        return None
    try:
        from email.utils import parsedate_to_datetime
        import datetime as _dt
        parsed = parsedate_to_datetime(date_text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_dt.timezone.utc)
        return parsed
    except Exception:
        return None


def _fetch_simple_rss(url, timeout=25, filter_recent_days=None):
    """通用RSS標題抓取,供股海飯桶/SemiAnalysis/AmiNext/財報狗/科技報橘/IC之音共用。
    2026-09-15修正:部分Podcast RSS(財報狗638集/科技報橘1020集/IC之音613集)
    會回傳「創台以來全部集數」,不像一般新聞RSS天生只顯示最近幾十則——若不過濾,
    每天都會把好幾年份的舊集數當成「今天的內容」重複計入跨源比對,嚴重稀釋
    「近日暴增」訊號的意義。filter_recent_days有給值時,只保留pubDate落在
    該天數內的項目;沒給日期欄位的項目(RSS規範不強制pubDate)則保留,避免
    因為缺欄位就誤刪合法內容。"""
    try:
        r = requests.get(url, headers=UA, timeout=timeout)
        if r.status_code != 200:
            return []
        root = ET.fromstring(r.text)
        results = []
        cutoff = None
        if filter_recent_days is not None:
            import datetime as _dt
            cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=filter_recent_days)
        for it in root.iter("item"):
            title_el = it.find("title")
            if title_el is None or not title_el.text:
                continue
            if cutoff is not None:
                pubdate_el = it.find("pubDate")
                pub_dt = _parse_rss_date(pubdate_el.text if pubdate_el is not None else None)
                try:
                    if pub_dt is not None and pub_dt < cutoff:
                        continue  # 超過窗口的舊項目,跳過
                except TypeError:
                    pass  # 日期比較失敗(如時區資訊異常),保守保留該項目,不讓單筆錯誤拖垮整個來源
            results.append(title_el.text.strip())
        return results
    except Exception:
        return []


def _fetch_stockrice():
    """股海飯桶Podcast RSS(半導體供應鏈深度分析,每週二次,2026-09-15確認可用)。"""
    return _fetch_simple_rss(STOCKRICE_RSS, filter_recent_days=RSS_RECENT_DAYS)


def _fetch_semianalysis():
    """SemiAnalysis(高品質AI/半導體深度分析,2026-09-15確認頻率近乎每週數篇,免費層仍有標題+摘要)。"""
    return _fetch_simple_rss(SEMIANALYSIS_RSS, filter_recent_days=RSS_RECENT_DAYS)


def _fetch_aminext():
    """AmiNext科技筆記(2026-09-15確認RSS可用,選題含半導體但範圍較廣,偶有國防/總經題材)。"""
    return _fetch_simple_rss(AMINEXT_RSS, filter_recent_days=RSS_RECENT_DAYS)


def _fetch_statementdog_podcast():
    """財報狗Podcast(2026-09-15確認舊Firstory網址仍有效,內容精準度高;
    RSS含638集全部歷史存檔,已加日期過濾避免陳年集數污染每日訊號)。"""
    return _fetch_simple_rss(STATEMENTDOG_PODCAST_RSS, filter_recent_days=RSS_RECENT_DAYS)


def _fetch_techorange():
    """科技報橘「科技早餐」(2026-09-15確認,正牌媒體流線傳媒,每日更新,
    內容全免費完整無付費牆,密度極高,直接命中HBM/CoWoS/矽光子等追蹤題材;
    RSS含1020集全部歷史存檔,已加日期過濾)。"""
    return _fetch_simple_rss(TECHORANGE_RSS, filter_recent_days=RSS_RECENT_DAYS)


def _fetch_wapeople_podcast():
    """產業人物Wa-People Podcast(2026-09-16確認,127集個股深度訪談,
    命中密度高(台積電/半導體/法說),跟既有wapeople網站版內容型態不同
    不重複,已加日期過濾避免歷史存檔污染)。"""
    return _fetch_simple_rss(WAPEOPLE_PODCAST_RSS, filter_recent_days=RSS_RECENT_DAYS)


def _fetch_fugle_blog():
    """富果部落格(2026-09-16確認,無公開RSS,HTML解析;內容為個股分析+
    法說會備忘錄,扎實但首頁夾雜API文件/法律聲明等選單雜訊,額外排除)。"""
    titles = _fetch_html_titles(FUGLE_BLOG_HTML)
    noise = ("API", "使用管理辦法", "交易資訊", "隱私", "服務條款", "客服")
    return [t for t in titles if not any(n in t for n in noise)]


def _fetch_youxian():
    """優閒聊財經(2026-09-16確認,前身為優分析Podcast,150集,命中密度
    極高(營收/AI伺服器/記憶體/法說/半導體/光通訊/ASIC等19種關鍵詞),
    等同繞道取得優分析分析內容(官網本身JS動態渲染抓不到);
    已加日期過濾避免歷史存檔污染)。"""
    return _fetch_simple_rss(YOUXIAN_RSS, filter_recent_days=RSS_RECENT_DAYS)


def _fetch_gs_exchanges():
    """Goldman Sachs Exchanges(2026-09-16確認,英文,機構級AI/總經分析。
    內容偏總經/大盤(Fed政策/貨幣干預/IPO/信用市場),非半導體供應鏈
    術語密度高的類型,但偶有直接相關集數(如AI資料中心電力需求)。
    已加日期過濾(641集全部歷史存檔)。已加入EN_SOURCES白名單,
    避免jieba對英文誤切。"""
    return _fetch_simple_rss(GS_EXCHANGES_RSS, filter_recent_days=RSS_RECENT_DAYS)


def _fetch_moneydj_podcast():
    """MoneyDJ財經新聞Podcast(2026-09-16確認,506集,命中密度極高:
    法說26/半導體12/台積電7/記憶體4/CoWoS2/先進封裝2/玻璃基板2/
    AI伺服器2/測試2,是這批擴充裡數一數二精準的來源;已加日期過濾)。"""
    return _fetch_simple_rss(MONEYDJ_PODCAST_RSS, filter_recent_days=RSS_RECENT_DAYS)


def _fetch_google_news():
    """Google News RSS(2026-09-16確認,矽光子/AI伺服器供應鏈查詢命中密度極高)。
    每個查詢已內含when:天數窗口,回傳本身已是近期內容,不需再套用
    RSS_RECENT_DAYS過濾(那是給不支援時間窗口的一般RSS用的)。
    標題結尾都帶「- 媒體名稱」(如「- Yahoo新聞」),先清掉避免媒體名稱
    被誤判成熱門詞。"""
    all_titles = []
    for q in GOOGLE_NEWS_QUERIES:
        url = f"https://news.google.com/rss/search?q={quote(q)}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
        try:
            r = requests.get(url, headers=UA, timeout=20)
            if r.status_code != 200:
                continue
            root = ET.fromstring(r.text)
            for it in root.iter("item"):
                title_el = it.find("title")
                if title_el is None or not title_el.text:
                    continue
                title = title_el.text.strip()
                # 去除結尾的「 - 媒體名稱」(Google News固定格式)
                title = re.sub(r'\s*-\s*[^-]{2,20}$', '', title).strip()
                if title:
                    all_titles.append(title)
        except Exception:
            continue
    return all_titles


def _fetch_google_news_us():
    """Google News RSS美國版(2026-09-16確認)。只用技術限定詞夠精準的查詢
    (矽光子/CoWoS/HBM/SiC GaN),避免太廣泛的查詢被地方政治新聞或行銷垃圾
    內容淹沒(已實測:「AI data center power」會抓到大量居民反對資料中心
    的地方新聞,「semiconductor supply chain」會抓到不相關市場預測垃圾文)。
    英文媒體名稱通常較長,後綴清理長度上限放寬到40字。"""
    all_titles = []
    for q in GOOGLE_NEWS_US_QUERIES:
        url = f"https://news.google.com/rss/search?q={quote(q)}&hl=en-US&gl=US&ceid=US:en"
        try:
            r = requests.get(url, headers=UA, timeout=20)
            if r.status_code != 200:
                continue
            root = ET.fromstring(r.text)
            for it in root.iter("item"):
                title_el = it.find("title")
                if title_el is None or not title_el.text:
                    continue
                title = title_el.text.strip()
                title = re.sub(r'\s*-\s*[^-]{2,40}$', '', title).strip()
                if title:
                    all_titles.append(title)
        except Exception:
            continue
    return all_titles


def _fetch_ic975():
    """IC之音科技咖(全站節目大雜燴,只保留IC975_KEEP_PREFIXES指定的科技相關子節目標題,
    濾掉生活/歷史/親子類等無關內容;RSS含613集全部歷史存檔,已加日期過濾)。"""
    titles = _fetch_simple_rss(IC975_RSS, timeout=30, filter_recent_days=RSS_RECENT_DAYS)
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
        "wapeople_pod": _fetch_wapeople_podcast(),
        "fugle_blog": _fetch_fugle_blog(),
        "youxian": _fetch_youxian(),
        "gs_exchanges": _fetch_gs_exchanges(),
        "moneydj_pod": _fetch_moneydj_podcast(),
        "google_news": _fetch_google_news(),
        "google_news_us": _fetch_google_news_us(),
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
