# -*- coding: utf-8 -*-
"""
new_theme_discovery.py — 偵測源4:新題材發現(多源跨源交叉升級版)
============================================================
升級重點(相對舊版):
  1. 多源標題池:鉅亨keyword標籤 + 中央社/財訊/Wa-people 標題(jieba斷詞)
  2. 跨源交叉:一個新詞出現在越多【不同來源】,越可能是真題材(非單一媒體用語)
     → 這是過濾雜訊的核心:奶茶/鹽燈只在單源出現一次,真題材跨源出現
  3. 強化停用詞:雜誌套語、泛詞、總經詞
  4. 排序:跨源數 > 出現次數 > 是否對應個股

處置:每日輸出候選,人工升格(不自動改 watchlist)。
============================================================
"""
import requests, datetime as dt, json, time, re
from collections import defaultdict

UA = {"User-Agent": "Mozilla/5.0 (quietfolio-radar)"}
CNYES_BASE = "https://api.cnyes.com/media/api/v1"
SEED_QUERIES = ["台股", "AI", "半導體", "輝達", "記憶體", "台積電", "AI伺服器"]
RECENT_DAYS, BASELINE_DAYS = 3, 20
MIN_RECENT_HITS, SURGE_RATIO, MAX_BASELINE_HITS = 2, 1.5, 5
# 放寬紀錄(2026-09-10):
#   MIN_RECENT_HITS 3→2:降低候選門檻,讓更多詞進來(雜訊也會增加)
#   MAX_BASELINE_HITS 2→5:不只抓全新詞,也抓基線期已有但持續熱度上升的詞
#   SURGE_RATIO 2.0→1.5:配合基線放寬,門檻同步降低(否則基線變大會更難達到暴增比)
WATCHLIST_FILE = "themes_watchlist.txt"

STOPWORDS = {
    # 盤面/交易術語
    "盤中漲跌速","盤中漲跌幅","盤中漲跌停","領漲跌產業","漲停","跌停","漲跌",
    "市場預估","盤前","盤後","收盤","開盤","台股盤","法人","融資","融券","主力",
    # 泛詞
    "台灣","台股","eps","出口","匯率","手機","面板","債券","債市","分紅",
    "拋售","股價","營收","獲利","財報","股利","除息","除權","殖利率","本益比",
    "指出","表示","看好","看壞","上漲","下跌","大漲","大跌","飆漲","重挫",
    # 宏觀/國際
    "國際油價","油價","日元","日圓","日債","全球債市","美債","美元","通膨",
    "升息","降息","fed","聯準會","cpi","gdp","景氣","基金","非農","新興市場",
    "esg","永續","fomc","歐洲","美國","中國","日本","比特幣","加密貨幣","黃金",
    "利率","就業","消費","零售","製造業","pmi","財政","關稅","貿易","選舉",
    # 大盤/指數/機構
    "msci","台積電","加權指數","道瓊","那斯達克","標普","費半","token",
    # 雜誌/媒體套語
    "獨家","專訪","專題","封面","焦點","解析","深度","報導","一次看","懶人包",
    "重磅","快訊","即時","最新","熱門","精選","推薦","分析","觀點","評論","社論",
    # 補充停用詞(theme_tracker實測發現的漏網通用詞)
    "新台幣","發表會","下半年","上半年","本季","上季","下季",
    "今日","昨日","明日","本週","上週","下週","...","…",
    # 英文碎詞/停用詞(英文標題被jieba切碎產生,或AI模型名非題材)
    "as","the","of","to","in","on","for","and","or","by","with","reportedly",
    "prices","price","says","said","new","update","report","reports","spot",
    "astra","agi","gpt","llm","chatgpt","gemini","claude","copilot","samsung",
    "sk","intel","china","us","eu","q1","q2","q3","q4","inc","corp","ltd",
    # 通用碎詞(jieba斷詞產生的高頻通用詞,非題材)
    "一次","風險","全球","代理","模型","億元","台積","智慧","技術","關鍵",
    "企業","經濟","最大","新高","啟動","推出","股盤","應鏈","表格","盤中",
    "台股","除權息","盤後","速報","韓股","泡沫","安全","財經","金色","股市",
    "美股","陸股","日股","歐股","國際","市場","投資","法人","外資","投信",
    "布局","題材","概念","類股","族群","營運","展望","看好","目標","評等",
    "報告","研究","預估","調查","數據","統計","指數","漲幅","跌幅","成長",
    "產業","公司","集團","廠商","供應","需求","訂單","出貨","產能","營收",
    "獲利","毛利","財報","法說","股價","股東","董事","高層","執行長","董事長",
    "一年","一天","今年","明年","去年","本月","上月","下月","本週","上週",
    "美元","台幣","人民幣","日圓","歐元","匯率","利率","升息","降息",
    "中國","美國","日本","韓國","台灣","歐洲","印度","越南","德國","英國",
    "系統","平台","服務","方案","應用","功能","版本","升級","發布","發表",
    "合作","結盟","收購","入股","投資案","簽約","協議","布局","進軍","跨足",
}

# 雜誌/廣告/公告雜訊(標題含這些整條丟棄)
TITLE_NOISE = re.compile(r'商城|水晶|鹽燈|詐騙|澄清|報名|購買|電子報|廣告|抽獎|活動|優惠|折扣|免費|奶茶|美食|旅遊|餐廳|飯店')


# 英文技術詞白名單:英文來源標題直接比對這些完整詞(不靠jieba斷詞)
EN_TECH_TERMS = [
    "advanced packaging", "glass substrate", "silicon photonics", "co-packaged",
    "spot price", "HBM", "HBM4", "DRAM", "NAND", "DDR5", "LPDDR", "base die",
    "CoWoS", "CoPoS", "FOPLP", "panel-level", "TSV", "interposer", "chiplet",
    "800G", "1.6T", "transceiver", "EML", "InP", "indium phosphide", "laser",
    "SiC", "GaN", "power semiconductor", "solid-state battery", "humanoid",
    "liquid cooling", "immersion cooling", "HVDC", "800V", "data center",
    "passive component", "MLCC", "substrate", "wafer", "foundry", "yield",
]


def load_watchlist_terms():
    terms = set()
    try:
        with open(WATCHLIST_FILE, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if ln and not ln.startswith("#"):
                    terms.add(ln.lower())
    except FileNotFoundError:
        pass
    return terms


def _get_keywords(it):
    kw = it.get("keyword") or it.get("keywords")
    if isinstance(kw, str): return [kw]
    if isinstance(kw, list): return kw
    return []


def _get_tickers(it):
    tk = it.get("related_tickers") or it.get("relatedTickers") or []
    return [str(t.get("ticker")) for t in tk if isinstance(t, dict) and t.get("market") == "TW"]


def fetch_cnyes(query, max_pages=8):
    """鉅亨搜尋,回傳 [{title, keywords, tickers, ts}]"""
    out = []
    for page in range(1, max_pages + 1):
        url = f"{CNYES_BASE}/search/news?q={requests.utils.quote(query)}&page={page}"
        try:
            r = requests.get(url, headers=UA, timeout=20)
            if r.status_code != 200: break
            items = (r.json().get("items") or {}).get("data") or []
            if not items: break
            for it in items:
                out.append({"newsId": it.get("newsId"), "title": it.get("title",""),
                            "keywords": _get_keywords(it), "tickers": _get_tickers(it),
                            "ts": it.get("publishAt") or 0})
            last = (r.json().get("items") or {}).get("last_page", 1)
            if page >= last: break
            time.sleep(0.3)
        except Exception:
            break
    return out


def main():
    now = dt.datetime.now()
    recent_cut = (now - dt.timedelta(days=RECENT_DAYS)).timestamp()
    base_cut = (now - dt.timedelta(days=BASELINE_DAYS + RECENT_DAYS)).timestamp()
    watchlist = load_watchlist_terms()
    print("=" * 64)
    print(f"新題材發現(多源跨源交叉版 v2025-09-10-filterfix)  排除{len(watchlist)}個已知詞")
    print("=" * 64)

    import jieba
    for t in watchlist:
        jieba.add_word(t)
    # 補一批完整專有名詞進詞典,避免被切碎(供應鏈→應鏈、台積電→台積 等)
    for w in ["供應鏈","台積電","聯發科","日月光","南亞科","華邦電","力積電",
              "環球晶","中美晶","探針卡","矽晶圓","載板","覆晶","打線","封測",
              "矽光子","光收發","光模組","雷射二極體","磷化銦","氮化鎵","碳化矽",
              "人形機器人","諧波減速機","伺服馬達","固態電池","鈉離子電池",
              "低軌衛星","衛星通訊","超級電容","液冷散熱","浸沒式散熱",
              "先進封裝","玻璃基板","面板級封裝","矽中介層","記憶體","超級循環"]:
        jieba.add_word(w)

    # 每個詞記錄:近期次數、基線次數、來源集合、關聯個股
    term_recent = defaultdict(int)
    term_base = defaultdict(int)
    term_sources = defaultdict(set)   # 跨源交叉核心
    term_tickers = defaultdict(lambda: defaultdict(int))
    term_sample = {}

    def add_term(term, source, in_recent, tickers=None):
        k = term.strip()
        # 純中文詞要≥3字(2字多為通用詞);含英數的技術詞(HBM/CoWoS)≥2字即可
        has_ascii = any(c.isascii() and c.isalnum() for c in k)
        min_len = 2 if has_ascii else 3
        if len(k) < min_len or k.isdigit(): return
        if k.lower() in watchlist or k.lower() in STOPWORDS: return
        # 過濾:全是通用單字組成的詞(如「一次」「全球」已在STOPWORDS,這裡擋漏網)
        if not has_ascii and len(k) <= 2: return
        # 過濾:純數字/百分比/純符號(如「30%」「50億」這類斷詞殘留,不是題材)
        if re.match(r'^[\d.,]+[%億萬元次年月日]*$', k): return
        if in_recent:
            term_recent[k] += 1
            term_sources[k].add(source)
            term_sample.setdefault(k, source)
            for tk in (tickers or []):
                term_tickers[k][tk] += 1
        else:
            term_base[k] += 1

    # ── 來源1:鉅亨(keyword標籤 + 時序)──
    seen = set()
    cnyes_news = []
    for q in SEED_QUERIES:
        for n in fetch_cnyes(q):
            nid = n["newsId"]
            if nid and nid not in seen:
                seen.add(nid); cnyes_news.append(n)
    print(f"  鉅亨: {len(cnyes_news)} 則")
    for n in cnyes_news:
        in_recent = n["ts"] >= recent_cut
        in_base = base_cut <= n["ts"] < recent_cut
        if not (in_recent or in_base): continue
        for kw in n["keywords"]:
            add_term(kw, "cnyes", in_recent, n["tickers"])

    # ── 來源2-5:中央社/MoneyDJ/財訊/Wa-people(標題斷詞,當今日=recent)──
    try:
        from news_sources import fetch_titles_by_source
        by_source = fetch_titles_by_source()
        EN_SOURCES = {"trendforce", "eetimes"}  # 英文為主的來源
        for src, titles in by_source.items():
            cnt = 0
            for title in titles:
                if TITLE_NOISE.search(title): continue
                # 判斷標題是否以英文為主
                ascii_ratio = sum(1 for c in title if c.isascii()) / max(len(title), 1)
                if src in EN_SOURCES or ascii_ratio > 0.6:
                    # 英文標題:只比對英文技術詞白名單(不靠jieba)
                    low = title.lower()
                    for term in EN_TECH_TERMS:
                        if term.lower() in low:
                            add_term(term, src, True)
                    # 中文技術詞也撈(混合標題)
                    for w in jieba.lcut(title):
                        if len(w) >= 3 and not w.isascii():
                            add_term(w, src, True)
                else:
                    # 中文標題:jieba斷詞
                    for w in jieba.lcut(title):
                        if len(w) >= 2:
                            add_term(w, src, True)
                cnt += 1
            print(f"  {src}: {cnt} 則標題")
    except Exception as e:
        print(f"  ⚠ 補充來源失敗(不影響鉅亨): {e}")

    # ── 篩選新題材候選 ──
    candidates = []
    for term, rc in term_recent.items():
        if rc < MIN_RECENT_HITS: continue
        bc = term_base.get(term, 0)
        if bc > MAX_BASELINE_HITS: continue
        r_daily = rc / RECENT_DAYS
        b_daily = bc / BASELINE_DAYS if bc else 0
        ratio = (r_daily / b_daily) if b_daily > 0 else float("inf")
        if ratio < SURGE_RATIO and bc > 0: continue
        n_sources = len(term_sources[term])
        top = sorted(term_tickers[term].items(), key=lambda x: -x[1])[:3]
        candidates.append({
            "term": term, "recent_hits": rc, "baseline_hits": bc,
            "source_count": n_sources, "sources": sorted(term_sources[term]),
            "is_brand_new": bc == 0,
            "related_stocks": [t for t, _ in top],
        })

    # 排序:跨源數優先 > 有個股 > 出現次數(跨源=真題材的最強訊號)
    candidates.sort(key=lambda x: (-x["source_count"], not x["related_stocks"], -x["recent_hits"]))

    # ── 分區輸出 ──
    multi = [c for c in candidates if c["source_count"] >= 2]
    single = [c for c in candidates if c["source_count"] < 2]
    print(f"\n新題材候選:{len(candidates)} 個")
    print(f"\n★ 跨源出現(高信心,{len(multi)}個 — 多個來源同時提):")
    for c in multi[:20]:
        stk = " 股:" + " ".join(c["related_stocks"]) if c["related_stocks"] else ""
        tag = "🆕" if c["is_brand_new"] else ""
        print(f"  {tag}{c['term']}  {c['source_count']}源({' '.join(c['sources'])}) 近{c['recent_hits']}次{stk}")
    print(f"\n○ 單源出現(參考,前10/{len(single)}個):")
    for c in single[:10]:
        tag = "🆕" if c["is_brand_new"] else ""
        print(f"  {tag}{c['term']}  {c['sources'][0]} 近{c['recent_hits']}次")

    json.dump({"generated_at": str(now), "candidates": candidates,
               "multi_source": multi},
              open("new_theme_candidates.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print(f"\n→ new_theme_candidates.json 已寫出(跨源{len(multi)}個)")


if __name__ == "__main__":
    main()
