# -*- coding: utf-8 -*-
"""
============================================================
new_theme_discovery.py — 偵測源4:新題材發現(無預設關鍵字)
============================================================
和 src1~src3 的根本差異:
  src1~3 都是「先有名字才能追」(關鍵字/個股/對照表)
  這支是「讓沒看過的新詞自己冒出來」——新題材發現

核心手法(利用鉅亨已內建的 keywords 標籤,免自己斷詞):
  1. 抓鉅亨最新新聞流(不指定關鍵字,category 列表端點)
  2. 每篇新聞已附 keywords 標籤(財經術語,編輯標好的)+ related_tickers
  3. 統計每個標籤的出現時序:近N日 vs 前M日基線
  4. 篩出「新冒出+暴增」且【不在現有 watchlist】的標籤 = 新題材候選
  5. 對照 related_tickers 給相關個股
  6. 輸出清單 → 你人工判斷是否升格為正式 watchlist 題材

為什麼用 keywords 標籤而非 jieba 斷詞:
  鉅亨 keywords 是財經術語(晶片/算力/CoWoS...),不含人名雜訊,乾淨太多。

處置策略:每日輸出候選清單,人工升格(不自動改 watchlist)。
  理由:新題材判斷需要產業知識(你的強項),自動升格會灌入雜訊題材,
       且可能觸發對雜訊股的錯誤關注。人在迴路是這裡的正解。

輸出:new_theme_candidates.json
依賴:requests
============================================================
"""

import requests
import datetime as dt
import json
import time
from collections import defaultdict

UA = {"User-Agent": "Mozilla/5.0 (quietfolio-radar)"}
CNYES_BASE = "https://api.cnyes.com/media/api/v1"
WATCHLIST_FILE = "themes_watchlist.txt"

# 抓哪些分類的最新新聞流(涵蓋台股/科技/產業)
CATEGORIES = ["tw_stock", "tech", "cn_stock", "wd_stock", "us_stock"]
PAGES_PER_CAT = 10          # 每分類抓幾頁(每頁約30則)

RECENT_DAYS = 3             # 近期窗
BASELINE_DAYS = 20          # 基線窗
MIN_RECENT_HITS = 3         # 近期至少出現幾次才算「有動靜」(濾一次性雜訊)
SURGE_RATIO = 2.0           # 近期日均/基線日均 >= 此值(新題材門檻設高一點,寧缺勿濫)
MAX_BASELINE_HITS = 2       # 基線期出現 <= 此值 才算「新冒出」(基線幾乎沒出現過)


def load_watchlist_terms():
    """載入現有 watchlist,新題材要排除這些已知詞。"""
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


def fetch_news_stream(category, pages):
    """抓某分類最新新聞流。回傳 [{title, keywords, tickers, ts}]."""
    out = []
    for page in range(1, pages + 1):
        url = f"{CNYES_BASE}/news/category/{category}?page={page}&limit=30"
        try:
            r = requests.get(url, headers=UA, timeout=20)
            if r.status_code != 200:
                break
            data = (r.json().get("items") or {}).get("data") or []
            if not data:
                break
            for it in data:
                out.append({
                    "title": it.get("title", ""),
                    "keywords": it.get("keywords") or [],
                    "tickers": [str(t.get("ticker")) for t in (it.get("related_tickers") or [])
                                if t.get("market") == "TW"],
                    "ts": it.get("publishAt", 0),
                })
        except Exception:
            break
        time.sleep(0.3)
    return out


def main():
    now = dt.datetime.now()
    recent_cut = (now - dt.timedelta(days=RECENT_DAYS)).timestamp()
    base_cut = (now - dt.timedelta(days=BASELINE_DAYS + RECENT_DAYS)).timestamp()
    watchlist = load_watchlist_terms()

    print("=" * 64)
    print(f"新題材發現  抓{len(CATEGORIES)}分類最新新聞流  排除{len(watchlist)}個已知詞")
    print("=" * 64)

    # 收集所有新聞
    all_news = []
    for cat in CATEGORIES:
        news = fetch_news_stream(cat, PAGES_PER_CAT)
        print(f"  {cat}: {len(news)} 則")
        all_news.extend(news)
    print(f"  合計 {len(all_news)} 則新聞")

    # 統計每個 keyword 標籤的近期/基線出現次數 + 關聯個股
    kw_recent = defaultdict(int)
    kw_base = defaultdict(int)
    kw_tickers = defaultdict(lambda: defaultdict(int))
    kw_sample_title = {}

    for n in all_news:
        ts = n["ts"]
        in_recent = ts >= recent_cut
        in_base = base_cut <= ts < recent_cut
        for kw in n["keywords"]:
            k = kw.strip()
            if not k or k.lower() in watchlist:
                continue  # 排除已知 watchlist 詞
            if in_recent:
                kw_recent[k] += 1
                if k not in kw_sample_title:
                    kw_sample_title[k] = n["title"]
                for tk in n["tickers"]:
                    kw_tickers[k][tk] += 1
            elif in_base:
                kw_base[k] += 1

    # 篩選新題材候選
    candidates = []
    for kw, rc in kw_recent.items():
        if rc < MIN_RECENT_HITS:
            continue
        bc = kw_base.get(kw, 0)
        if bc > MAX_BASELINE_HITS:
            continue  # 基線期出現太多 = 不是新的
        # 暴增比(基線日均 vs 近期日均)
        r_daily = rc / RECENT_DAYS
        b_daily = bc / BASELINE_DAYS if bc else 0
        ratio = (r_daily / b_daily) if b_daily > 0 else float("inf")
        if ratio < SURGE_RATIO and bc > 0:
            continue
        # 關聯個股(取出現最多的前3)
        top_tickers = sorted(kw_tickers[kw].items(), key=lambda x: -x[1])[:3]
        candidates.append({
            "term": kw,
            "recent_hits": rc,
            "baseline_hits": bc,
            "ratio": round(ratio, 2) if ratio != float("inf") else None,
            "is_brand_new": bc == 0,   # 基線期完全沒出現過 = 全新
            "related_stocks": [t for t, _ in top_tickers],
            "sample_title": kw_sample_title.get(kw, ""),
        })

    # 排序:全新的優先,再按近期熱度
    candidates.sort(key=lambda x: (not x["is_brand_new"], -x["recent_hits"]))

    print("\n" + "=" * 64)
    print(f"新題材候選:{len(candidates)} 個")
    print("=" * 64)
    for c in candidates[:30]:
        tag = "🆕全新" if c["is_brand_new"] else f"暴增{c['ratio']}"
        stocks = " ".join(c["related_stocks"]) if c["related_stocks"] else "(無明確個股)"
        print(f"  [{tag}] {c['term']}  近{c['recent_hits']}次  股:{stocks}")
        print(f"         例:{c['sample_title'][:40]}")

    with open("new_theme_candidates.json", "w", encoding="utf-8") as f:
        json.dump({"generated_at": str(now), "candidates": candidates},
                  f, ensure_ascii=False, indent=2)
    print(f"\n→ new_theme_candidates.json 已寫出({len(candidates)}個候選)")
    print("→ 人工掃描上面清單,有價值的詞加進 themes_watchlist.txt 升格為正式題材")


if __name__ == "__main__":
    main()
