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
    candidates.sort(key=lambda x:(not x["is_brand_new"],-x["recent_hits"]))

    print(f"\n新題材候選:{len(candidates)} 個  (方法:{'jieba斷詞' if use_jieba else 'keyword標籤'})")
    for c in candidates[:30]:
        tag="🆕全新" if c["is_brand_new"] else f"暴增{c['ratio']}"
        stocks=" ".join(c["related_stocks"]) or "(無明確個股)"
        print(f"  [{tag}] {c['term']}  近{c['recent_hits']}次  股:{stocks}")

    json.dump({"generated_at":str(now),"method":"jieba" if use_jieba else "keyword",
               "candidates":candidates},
              open("new_theme_candidates.json","w",encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"\n→ new_theme_candidates.json 已寫出({len(candidates)}個候選)")

if __name__ == "__main__":
    main()
