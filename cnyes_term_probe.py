# -*- coding: utf-8 -*-
"""
cnyes_term_probe.py — 太空算力 / SpaceX Kessel 候選題材詞的鉅亨命中探測(2026-11-16,一次性,只讀)
============================================================
規格:使用者提供的「把 SpaceX Kessel / 太空算力題材併入發酵雷達」任務 1。
對每個候選詞打鉅亨 search API(與 event_theme_radar.py 的 cnyes_search 同一端點與 User-Agent,不 import 雷達、不改雷達),
計算近 20 日:
  raw    搜尋回傳篇數
  exact  標題或摘要真的含完整詞的篇數(鉅亨會把詞拆開找,與雷達 phrase_filter 同理,這裡對所有詞都檢查)
  ctx    exact 之中同時提到相關脈絡詞(SpaceX / 太空 / 衛星 / 伺服器 / PCB / 液冷 / 太陽能…)的篇數
結果只印到 job log(含每詞前 3 則標題),不存檔、不 commit。v2026-11-16-space
"""
import json
import re
import sys
import time
import unicodedata

import requests

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
BASE = "https://api.cnyes.com/media/api/v1/search/news"
DAYS = 20
TERMS_A = ["Kessel", "Starmind", "太空算力", "太空資料中心", "軌道資料中心", "中板", "M10", "石英布", "Q布", "PTFE", "鑽針",
           "快接頭", "輻射散熱", "星間雷射", "雷射通訊", "異質接面", "HJT", "鈣鈦礦", "Singulus", "Space-1", "砷化鎵太陽能"]
TERMS_B = ["Gigasat", "Low Dk-2", "低溫銀膠", "低溫熱管", "抗輻射", "太空SSD", "Paladin", "衛星電池", "衛星機構件", "航太MLCC", "GaN PA"]
CONTEXT = ["SpaceX", "Starlink", "星鏈", "太空", "衛星", "軌道", "伺服器", "機櫃", "PCB", "載板", "銅箔", "CCL", "液冷", "散熱",
           "太陽能", "光電", "電池", "NVIDIA", "輝達", "AI"]


def norm(s):
    return unicodedata.normalize("NFKC", s or "").lower()


def search(term, start_ts, max_pages=10):
    out = []
    for page in range(1, max_pages + 1):
        try:
            r = requests.get(BASE, params={"q": term, "page": page}, headers=UA, timeout=20)
        except requests.RequestException as e:
            return out, "error:" + type(e).__name__
        if r.status_code != 200:
            return out, "http %d" % r.status_code
        j = r.json().get("items") or {}
        items = j.get("data") or []
        if not items:
            break
        stop = False
        for it in items:
            if (it.get("publishAt") or 0) < start_ts:
                stop = True
                continue
            out.append({"t": it.get("title", ""), "s": it.get("summary", "") or ""})
        if stop or page >= (j.get("last_page") or 1):
            break
        time.sleep(0.5)
    return out, "ok"


def main():
    start = int(time.time()) - DAYS * 86400
    rows = []
    for tier, terms in (("A", TERMS_A), ("B", TERMS_B)):
        for term in terms:
            news, st = search(term, start)
            k = norm(term)
            exact = [n for n in news if k in norm(n["t"] + " " + n["s"])]
            ctx = [n for n in exact if any(norm(c) in norm(n["t"] + " " + n["s"]) for c in CONTEXT if norm(c) != k)]
            rows.append({"tier": tier, "term": term, "status": st, "raw": len(news), "exact": len(exact), "ctx": len(ctx),
                         "titles": [n["t"][:60] for n in exact[:3]]})
            print(json.dumps(rows[-1], ensure_ascii=False))
            time.sleep(1.0)
    print("===TABLE===")
    for r in rows:
        print("%s | %-10s | raw %3d | exact %3d | ctx %3d | %s" % (r["tier"], r["term"], r["raw"], r["exact"], r["ctx"], r["status"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
