# -*- coding: utf-8 -*-
"""
new_theme_discovery.py — 偵測源4:新題材發現(自我探測版)
自動探測可用的鉅亨分類端點/slug/欄位名,避免猜錯。
"""
import requests, datetime as dt, json, time
from collections import defaultdict

UA = {"User-Agent": "Mozilla/5.0 (quietfolio-radar)"}
ENDPOINT_TEMPLATES = [
    "https://api.cnyes.com/media/api/v1/news/cat/{slug}?page={page}&limit=30",
    "https://api.cnyes.com/media/api/v1/news/category/{slug}?page={page}&limit=30",
    "https://api.cnyes.com/media/api/v1/news?cat={slug}&page={page}&limit=30",
]
CANDIDATE_SLUGS = ["headline_all", "headline", "tw_stock", "twstock", "tech", "wd_stock"]
RECENT_DAYS, BASELINE_DAYS = 3, 20
MIN_RECENT_HITS, SURGE_RATIO, MAX_BASELINE_HITS = 3, 2.0, 2
WATCHLIST_FILE = "themes_watchlist.txt"

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

def _extract_items(payload):
    if not isinstance(payload, dict): return None
    items = payload.get("items")
    if isinstance(items, dict) and isinstance(items.get("data"), list): return items["data"]
    if isinstance(items, list): return items
    if isinstance(payload.get("data"), list): return payload["data"]
    return None

def _get_keywords(item):
    kw = item.get("keyword")
    if kw is None: kw = item.get("keywords")
    if isinstance(kw, str): return [kw]
    if isinstance(kw, list): return kw
    return []

def _get_tickers(item):
    tk = item.get("related_tickers") or item.get("relatedTickers") or []
    return [str(t.get("ticker")) for t in tk if isinstance(t, dict) and t.get("market") == "TW"]

def _get_ts(item):
    return item.get("publishAt") or item.get("publish_at") or item.get("newsDatetime") or 0

def probe_working_config():
    print("--- 探測可用端點 ---")
    for tmpl in ENDPOINT_TEMPLATES:
        for slug in CANDIDATE_SLUGS:
            url = tmpl.format(slug=slug, page=1)
            try:
                r = requests.get(url, headers=UA, timeout=15)
                if r.status_code != 200: continue
                items = _extract_items(r.json())
                if items:
                    print(f"  ✓ 可用: cat={slug}  路徑={tmpl.split('/v1/')[1].split('?')[0]}  ({len(items)}則)")
                    return tmpl, slug
                else:
                    print(f"  · {slug} @ {tmpl.split('/v1/')[1].split('?')[0]}: 200但無data")
            except Exception as e:
                print(f"  · {slug}: {str(e)[:40]}")
            time.sleep(0.2)
    print("  ✗ 所有候選端點都抓不到資料")
    return None

def fetch_stream(template, slug, pages=15):
    out = []
    for page in range(1, pages + 1):
        url = template.format(slug=slug, page=page)
        try:
            r = requests.get(url, headers=UA, timeout=20)
            if r.status_code != 200: break
            items = _extract_items(r.json())
            if not items: break
            for it in items:
                out.append({"title": it.get("title",""), "keywords": _get_keywords(it),
                            "tickers": _get_tickers(it), "ts": _get_ts(it)})
        except Exception:
            break
        time.sleep(0.3)
    return out

def main():
    now = dt.datetime.now()
    recent_cut = (now - dt.timedelta(days=RECENT_DAYS)).timestamp()
    base_cut = (now - dt.timedelta(days=BASELINE_DAYS + RECENT_DAYS)).timestamp()
    watchlist = load_watchlist_terms()
    print("="*64); print(f"新題材發現(自我探測版)  排除{len(watchlist)}個已知詞"); print("="*64)
    config = probe_working_config()
    if not config:
        print("→ 無可用端點,寫出空結果。請把探測 log 貼回給 Claude 調整。")
        json.dump({"generated_at": str(now), "candidates": [], "error": "no working endpoint"},
                  open("new_theme_candidates.json","w",encoding="utf-8"), ensure_ascii=False, indent=2)
        return
    template, slug = config
    print(f"\n--- 用 cat={slug} 抓多頁新聞流 ---")
    all_news = fetch_stream(template, slug, pages=15)
    print(f"  共抓 {len(all_news)} 則新聞")
    with_kw = sum(1 for n in all_news if n["keywords"])
    print(f"  其中 {with_kw} 則帶 keyword 標籤")
    if with_kw == 0 and all_news:
        print("  ⚠ 有新聞但無 keyword 標籤,需改用標題斷詞。範例:", all_news[0]["title"][:40])
    kw_recent, kw_base = defaultdict(int), defaultdict(int)
    kw_tickers = defaultdict(lambda: defaultdict(int)); kw_sample = {}
    for n in all_news:
        ts = n["ts"]; in_recent = ts >= recent_cut; in_base = base_cut <= ts < recent_cut
        for kw in n["keywords"]:
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
    print(f"\n新題材候選:{len(candidates)} 個")
    for c in candidates[:30]:
        tag="🆕全新" if c["is_brand_new"] else f"暴增{c['ratio']}"
        stocks=" ".join(c["related_stocks"]) or "(無明確個股)"
        print(f"  [{tag}] {c['term']}  近{c['recent_hits']}次  股:{stocks}")
    json.dump({"generated_at":str(now),"used_slug":slug,"candidates":candidates},
              open("new_theme_candidates.json","w",encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"\n→ new_theme_candidates.json 已寫出({len(candidates)}個候選)")

if __name__ == "__main__":
    main()
