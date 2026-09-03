# -*- coding: utf-8 -*-
"""
new_theme_discovery.py — 偵測源4:新題材發現(搜尋端點版)
============================================================
改用 src1 已驗證可用的 search/news 端點(不再用有問題的分類列表端點)。
以廣詞(台股/AI/半導體/輝達/記憶體)搜最新新聞當新聞流,聚合去重後,
統計 keyword 標籤的近期vs基線,篩「新冒出+暴增」且不在watchlist的新題材。

若搜尋端點回應不含 keyword 欄位,自動 fallback 用 jieba 對標題斷詞。
============================================================
"""
import requests, datetime as dt, json, time
from collections import defaultdict

UA = {"User-Agent": "Mozilla/5.0 (quietfolio-radar)"}
CNYES_BASE = "https://api.cnyes.com/media/api/v1"
# 廣詞:必定大量回結果,當作「最新新聞流」的來源
SEED_QUERIES = ["台股", "AI", "半導體", "輝達", "記憶體", "台積電", "AI伺服器"]

# ── 雜訊過濾:停用詞(盤面術語+泛詞+宏觀,這些不是題材)──
STOPWORDS = {
    # 盤面術語(鉅亨固定標籤,非題材)
    "盤中漲跌速","盤中漲跌幅","盤中漲跌停","領漲跌產業","漲停","跌停","漲跌",
    "market預估","市場預估","盤前","盤後","收盤","開盤","台股盤","法人",
    # 泛詞
    "台灣","台股","eps","出口","匯率","手機","面板","債券","債市","分紅",
    "拋售","股價","營收","獲利","財報","股利","除息","除權","殖利率","本益比",
    # 宏觀/國際(非個股題材)
    "國際油價","油價","日元","日圓","日債","全球債市","美債","美元","通膨",
    "升息","降息","fed","聯準會","cpi","gdp","景氣",
    # 大盤指數/機構
    "msci","台積電","加權指數","道瓊","那斯達克","標普","費半","token",
    # 總經/國際/事件雜訊(法說裡的固定詞,非台股題材)
    "基金","非農","新興市場","esg","永續","fomc","歐洲","美國","中國","日本",
    "自駕車","電動車","比特幣","加密貨幣","黃金","原油","經濟數據","財報季",
    "利率","就業","消費","零售","製造業","pmi","財政","關稅","貿易","選舉",
}

# 明顯是「個股名/公司名」的線索:出現在 name2code 或含這些詞根 → 不是新題材
ENTITY_HINTS = {"科","電","控","-ky","半導體","光電","國際","投控","金","銀行","證券"}

RECENT_DAYS, BASELINE_DAYS = 3, 20
MIN_RECENT_HITS, SURGE_RATIO, MAX_BASELINE_HITS = 3, 2.0, 2
WATCHLIST_FILE = "themes_watchlist.txt"

def load_watchlist_terms():
    terms = set()
    try:
        with open(WATCHLIST_FILE, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if ln and not ln.startswith("#"): terms.add(ln.lower())
    except FileNotFoundError: pass
    return terms

def _get_keywords(it):
    kw = it.get("keyword")
    if kw is None: kw = it.get("keywords")
    if isinstance(kw, str): return [kw]
    if isinstance(kw, list): return kw
    return []

def _get_tickers(it):
    tk = it.get("related_tickers") or it.get("relatedTickers") or []
    return [str(t.get("ticker")) for t in tk if isinstance(t, dict) and t.get("market")=="TW"]

def fetch_by_search(query, max_pages=8):
    """複用 src1 驗證過的 search/news 端點,多取 keyword+tickers。"""
    out = []
    for page in range(1, max_pages+1):
        url = f"{CNYES_BASE}/search/news?q={requests.utils.quote(query)}&page={page}"
        try:
            r = requests.get(url, headers=UA, timeout=20)
            if r.status_code != 200: break
            items = (r.json().get("items") or {}).get("data") or []
            if not items: break
            for it in items:
                out.append({
                    "newsId": it.get("newsId"),
                    "title": it.get("title",""),
                    "keywords": _get_keywords(it),
                    "tickers": _get_tickers(it),
                    "ts": it.get("publishAt") or 0,
                })
            last_page = (r.json().get("items") or {}).get("last_page", 1)
            if page >= last_page: break
            time.sleep(0.3)
        except Exception as e:
            print(f"  ⚠ 搜 {query} 第{page}頁失敗: {str(e)[:40]}")
            break
    return out

def main():
    now = dt.datetime.now()
    recent_cut = (now - dt.timedelta(days=RECENT_DAYS)).timestamp()
    base_cut = (now - dt.timedelta(days=BASELINE_DAYS+RECENT_DAYS)).timestamp()
    watchlist = load_watchlist_terms()
    print("="*64); print(f"新題材發現(搜尋端點版)  排除{len(watchlist)}個已知詞"); print("="*64)

    # 聚合多個廣詞的新聞,用 newsId 去重
    seen = set(); all_news = []
    for q in SEED_QUERIES:
        news = fetch_by_search(q)
        added = 0
        for n in news:
            nid = n["newsId"]
            if nid and nid not in seen:
                seen.add(nid); all_news.append(n); added += 1
        print(f"  搜「{q}」: {len(news)}則 (新增{added})")
    print(f"  去重後共 {len(all_news)} 則新聞")

    with_kw = sum(1 for n in all_news if n["keywords"])
    print(f"  其中 {with_kw} 則帶 keyword 標籤")

    use_jieba = False
    if with_kw == 0 and all_news:
        print("  ⚠ 搜尋端點無keyword欄位,fallback用jieba斷標題")
        use_jieba = True
        import jieba
        # 用 watchlist 詞當自訂詞典,提高專有名詞切詞率
        for t in watchlist: jieba.add_word(t)

    kw_recent, kw_base = defaultdict(int), defaultdict(int)
    kw_tickers = defaultdict(lambda: defaultdict(int)); kw_sample = {}
    for n in all_news:
        ts = n["ts"]; in_recent = ts>=recent_cut; in_base = base_cut<=ts<recent_cut
        if use_jieba:
            import jieba
            terms = [w for w in jieba.lcut(n["title"]) if len(w)>=2]
        else:
            terms = n["keywords"]
        for kw in terms:
            k = kw.strip()
            if not k or k.lower() in watchlist: continue
            if k.lower() in STOPWORDS: continue  # 停用詞過濾
            if len(k) < 2: continue  # 單字太短
            if k.isdigit(): continue  # 純數字
            if in_recent:
                kw_recent[k]+=1; kw_sample.setdefault(k,n["title"])
                for tk in n["tickers"]: kw_tickers[k][tk]+=1
            elif in_base:
                kw_base[k]+=1

    candidates=[]
    for kw,rc in kw_recent.items():
        if rc<MIN_RECENT_HITS: continue
        bc=kw_base.get(kw,0)
        if bc>MAX_BASELINE_HITS: continue
        r_daily=rc/RECENT_DAYS; b_daily=bc/BASELINE_DAYS if bc else 0
        ratio=(r_daily/b_daily) if b_daily>0 else float("inf")
        if ratio<SURGE_RATIO and bc>0: continue
        top=sorted(kw_tickers[kw].items(),key=lambda x:-x[1])[:3]
        candidates.append({"term":kw,"recent_hits":rc,"baseline_hits":bc,
            "ratio":round(ratio,2) if ratio!=float("inf") else None,
            "is_brand_new":bc==0,"related_stocks":[t for t,_ in top],
            "sample_title":kw_sample.get(kw,"")})
    # 分兩區:有關聯個股的(高價值)vs 無個股的(參考)
    with_stocks = [c for c in candidates if c["related_stocks"]]
    no_stocks = [c for c in candidates if not c["related_stocks"]]
    with_stocks.sort(key=lambda x:(not x["is_brand_new"],-x["recent_hits"]))
    no_stocks.sort(key=lambda x:(not x["is_brand_new"],-x["recent_hits"]))

    print(f"\n新題材候選:{len(candidates)} 個  (方法:{'jieba斷詞' if use_jieba else 'keyword標籤'})")
    print(f"\n★ 有關聯個股(高價值,{len(with_stocks)}個):")
    for c in with_stocks[:20]:
        tag="🆕全新" if c["is_brand_new"] else f"暴增{c['ratio']}"
        print(f"  [{tag}] {c['term']}  近{c['recent_hits']}次  股:{' '.join(c['related_stocks'])}")
    print(f"\n○ 無關聯個股(參考,前10/{len(no_stocks)}個):")
    for c in no_stocks[:10]:
        tag="🆕全新" if c["is_brand_new"] else f"暴增{c['ratio']}"
        print(f"  [{tag}] {c['term']}  近{c['recent_hits']}次")

    json.dump({"generated_at":str(now),"method":"jieba" if use_jieba else "keyword",
               "candidates":candidates},
              open("new_theme_candidates.json","w",encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"\n→ new_theme_candidates.json 已寫出({len(candidates)}個候選)")

if __name__ == "__main__":
    main()
