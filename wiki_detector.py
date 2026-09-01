# -*- coding: utf-8 -*-
"""
============================================================
wiki_detector.py — 偵測源3:維基題材關注度(發酵前緣)
============================================================
接進 event_theme_radar.py 當第三個偵測源。

邏輯(與 radar 既有暴增邏輯同構,方向對準「發酵前」):
  對 wiki_theme_map.py 裡每個確認過的題材,抓維基條目瀏覽量:
    近3日日均 / 20日基線 >= SURGE_RATIO  → 關注度翹頭
    且 基線日均 <= BASELINE_MAX          → 尚未大眾化(排除已飽和)
    兩者同時 → PRE-FERMENT 未發酵候選 ✅

輸出格式對齊 radar 的 theme 項:
  {theme, ratio, recent_count, codes, source, wiki_stage, baseline_mean}
  → source 標 "wiki",codes 直接帶對照表裡的個股(不靠新聞撈)

用法(在 event_theme_radar.py 的 main() 裡):
  from wiki_detector import detect_wiki_attention
  src3 = detect_wiki_attention()
  all_themes = src1 + src2 + src3
============================================================
"""

import requests
import urllib.parse
import datetime as dt

try:
    from wiki_theme_map import get_theme_map, SURGE_RATIO, BASELINE_MAX
except ImportError:
    print("  ⚠ 找不到 wiki_theme_map.py,維基偵測源略過")
    get_theme_map = None

UA = {"User-Agent": "quietfolio-radar/1.0 (https://github.com/thomaschko; theme attention)"}
WIKI = "zh.wikipedia"
REST = "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article"

RECENT_DAYS = 3
BASELINE_DAYS = 20


def _get_pageviews(title, start, end):
    enc = urllib.parse.quote(title.replace(" ", "_"), safe="")
    url = f"{REST}/{WIKI}/all-access/all-agents/{enc}/daily/{start}/{end}"
    try:
        r = requests.get(url, headers=UA, timeout=20)
        if r.status_code != 200:
            return None
        return [it["views"] for it in r.json().get("items", [])]
    except Exception:
        return None


def _analyze(vals):
    if not vals or len(vals) < RECENT_DAYS + 5:
        return None
    recent = vals[-RECENT_DAYS:]
    baseline = vals[-(RECENT_DAYS + BASELINE_DAYS):-RECENT_DAYS] or vals[:-RECENT_DAYS]
    r_mean = sum(recent) / len(recent)
    b_mean = sum(baseline) / len(baseline) if baseline else 0
    ratio = (r_mean / b_mean) if b_mean > 0 else 0
    surging = ratio >= SURGE_RATIO
    still_low = 0 < b_mean <= BASELINE_MAX
    if surging and still_low:
        stage = "pre-ferment"
    elif surging:
        stage = "active"
    else:
        stage = "flat"
    return {"ratio": round(ratio, 2), "baseline_mean": round(b_mean, 1),
            "recent_mean": round(r_mean, 1), "stage": stage}


def detect_wiki_attention():
    """回傳 radar 格式的 theme list。只回報 pre-ferment 與 active(flat 不輸出以免雜訊)。"""
    print("[偵測源3] 維基題材關注度(發酵前緣)")
    if get_theme_map is None:
        return []
    theme_map = get_theme_map()
    today = dt.date.today()
    start = (today - dt.timedelta(days=BASELINE_DAYS + RECENT_DAYS + 2)).strftime("%Y%m%d")
    end = today.strftime("%Y%m%d")

    results = []
    for theme, cfg in theme_map.items():
        vals = _get_pageviews(cfg["wiki"], start, end)
        a = _analyze(vals) if vals else None
        if a is None:
            continue
        if a["stage"] == "flat":
            continue  # 平淡不輸出
        results.append({
            "theme": theme,
            "ratio": a["ratio"],
            "recent_count": a["recent_mean"],
            "codes": sorted(set(cfg["stocks"])),
            "source": "wiki",
            "wiki_stage": a["stage"],           # pre-ferment / active
            "baseline_mean": a["baseline_mean"],
            "wiki_note": cfg.get("note", ""),
            "semantic_risk": cfg.get("semantic_risk", ""),  # 語意偏差警示(空=無)
        })
        flag = "✅未發酵" if a["stage"] == "pre-ferment" else "已發酵"
        print(f"  {theme}({cfg['wiki']}) 暴增{a['ratio']} 基線{a['baseline_mean']} → {flag}")

    pre = [r for r in results if r["wiki_stage"] == "pre-ferment"]
    print(f"  偵測源3完成:{len(results)}個有動靜,其中{len(pre)}個未發酵翹頭")
    return results


if __name__ == "__main__":
    import json
    res = detect_wiki_attention()
    print("\n輸出預覽:")
    for r in res:
        r2 = dict(r)
        r2["codes"] = sorted(r2["codes"])
        print(json.dumps(r2, ensure_ascii=False))
