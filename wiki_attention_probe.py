# -*- coding: utf-8 -*-
"""
============================================================
wiki_attention_probe.py — Wikipedia 中文條目瀏覽量 可行性驗證
============================================================
目的:在【你的 GitHub Actions】上跑一次,驗證三件事——
  1. Wikimedia REST API 在 GHA 的 IP 抓不抓得到(沙盒白名單擋了,無法在該處驗)
  2. 台股個股/題材的中文維基「條目名稱」到底叫什麼(自動探測多個候選名)
  3. 瀏覽量對「發酵前緣」有沒有鑑別度(基線平淡 vs 近期翹頭)

這支【不動】你現有的 event_theme_radar.py。純驗證。
驗證通過後,再把 wiki_attention() 併進 radar 當第3個偵測源。

資料源(全免費、免金鑰、鼓勵程式存取):
  條目存在探測  https://zh.wikipedia.org/w/api.php  (opensearch / query)
  每日瀏覽量    https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/...

依賴:requests(你 radar 已用)
============================================================
"""

import requests
import urllib.parse
import datetime as dt
import json
import time

# Wikimedia 要求描述性 UA(含聯絡/用途),否則可能擋。改成你自己的。
UA = {"User-Agent": "quietfolio-radar/1.0 (https://github.com/thomaschko; wiki pageview research)"}
WIKI = "zh.wikipedia"           # 中文維基
REST = "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article"
APIURL = "https://zh.wikipedia.org/w/api.php"

RECENT_DAYS = 3
BASELINE_DAYS = 20
SURGE_RATIO = 1.5        # 近期日均 >= 基線日均 * 1.5 → 關注度翹頭
PRE_FERMENT_MAXMEAN = 200  # 基線日均低於此 → 屬「冷門/尚未大眾化」,符合發酵前特徵
                           # (這門檻要靠這次驗證的實際數字來校準,先給個起點)

# ── 驗證樣本:個股 + 題材。每個給多個候選條目名,自動探測哪個存在 ──
# 格式:(顯示標籤, [候選條目名, ...])
PROBE_TARGETS = [
    ("世芯-KY (3661)",  ["世芯電子", "世芯-KY", "Alchip", "智原科技"]),
    ("台積電 (2330)",   ["台灣積體電路製造", "台積電"]),
    ("聯亞 (3081)",     ["聯亞光電", "聯亞", "聯亞科技"]),
    ("貿聯-KY (3665)",  ["貿聯-KY", "貿聯", "BizLink"]),
    ("群聯 (8299)",     ["群聯電子", "群聯"]),
    # 題材
    ("矽光子",          ["矽光子", "矽光子學"]),
    ("CoWoS",           ["CoWoS", "晶圓級封裝"]),
    ("碳化矽",          ["碳化矽"]),
    ("共同封裝光學",    ["共同封裝光學", "共封裝光學", "CPO"]),
    ("HBM",             ["高頻寬記憶體", "HBM"]),
    ("固態電池",        ["固態電池"]),
    ("人形機器人",      ["人形機器人", "仿人機器人"]),
]


def article_exists(title):
    """用 MediaWiki query API 確認條目是否存在(且非重定向誤判)。回傳正規化後標題或 None。"""
    params = {
        "action": "query", "format": "json",
        "titles": title, "redirects": 1,
    }
    try:
        r = requests.get(APIURL, params=params, headers=UA, timeout=15)
        r.raise_for_status()
        pages = r.json().get("query", {}).get("pages", {})
        for pid, page in pages.items():
            if pid != "-1" and "missing" not in page:
                return page.get("title", title)
    except Exception as e:
        print(f"      (exists check error: {str(e)[:60]})")
    return None


def get_pageviews(title, start, end):
    """抓某條目日瀏覽量。回傳 [(date,int)] list。"""
    enc = urllib.parse.quote(title.replace(" ", "_"), safe="")
    url = f"{REST}/{WIKI}/all-access/all-agents/{enc}/daily/{start}/{end}"
    r = requests.get(url, headers=UA, timeout=20)
    if r.status_code != 200:
        return None, r.status_code
    items = r.json().get("items", [])
    return [(it["timestamp"][:8], it["views"]) for it in items], 200


def analyze(series):
    """用 radar 同款邏輯:基線日均 vs 近期日均。回傳指標 dict。"""
    if not series or len(series) < RECENT_DAYS + 5:
        return None
    vals = [v for _, v in series]
    recent = vals[-RECENT_DAYS:]
    baseline = vals[-(RECENT_DAYS + BASELINE_DAYS):-RECENT_DAYS]
    if not baseline:
        baseline = vals[:-RECENT_DAYS]
    r_mean = sum(recent) / len(recent)
    b_mean = sum(baseline) / len(baseline) if baseline else 0
    ratio = (r_mean / b_mean) if b_mean > 0 else float("inf")
    # 發酵階段判定
    surging = ratio >= SURGE_RATIO
    still_low = b_mean <= PRE_FERMENT_MAXMEAN
    if surging and still_low:
        stage = "PRE-FERMENT 未發酵翹頭 ✅"
    elif surging and not still_low:
        stage = "ACTIVE 已發酵(量已高)"
    else:
        stage = "flat 平淡"
    return {
        "baseline_mean": round(b_mean, 1),
        "recent_mean": round(r_mean, 1),
        "ratio": round(ratio, 2) if ratio != float("inf") else None,
        "recent_max": max(recent),
        "stage": stage,
    }


def main():
    today = dt.date.today()
    start = (today - dt.timedelta(days=BASELINE_DAYS + RECENT_DAYS + 2)).strftime("%Y%m%d")
    end = today.strftime("%Y%m%d")
    print("=" * 64)
    print(f"Wikipedia 瀏覽量驗證  期間 {start}~{end}  ({WIKI})")
    print("=" * 64)

    reachable = None  # 記錄 API 到底通不通
    results = []

    for label, candidates in PROBE_TARGETS:
        # 步驟1:探測哪個候選條目名存在
        found = None
        for c in candidates:
            t = article_exists(c)
            if t:
                found = t
                break
            time.sleep(0.3)
        if not found:
            print(f"\n■ {label}")
            print(f"    條目探測:候選 {candidates} 全部不存在 ✗")
            results.append({"label": label, "article": None, "note": "no article"})
            continue

        # 步驟2:抓瀏覽量
        series, code = get_pageviews(found, start, end)
        if reachable is None:
            reachable = (code == 200) or (series is not None)
        print(f"\n■ {label}  →  條目「{found}」")
        if series is None:
            print(f"    瀏覽量抓取失敗 HTTP {code}")
            results.append({"label": label, "article": found, "note": f"HTTP {code}"})
            continue

        # 步驟3:分析鑑別度
        a = analyze(series)
        if a is None:
            print(f"    資料點不足({len(series)}天),無法分析")
            results.append({"label": label, "article": found, "points": len(series)})
            continue
        print(f"    基線日均={a['baseline_mean']}  近期日均={a['recent_mean']}  "
              f"暴增比={a['ratio']}  近期峰值={a['recent_max']}")
        print(f"    → {a['stage']}")
        results.append({"label": label, "article": found, **a})
        time.sleep(0.3)

    # ── 總結 ──
    print("\n" + "=" * 64)
    print("驗證總結")
    print("=" * 64)
    if reachable:
        print("✅ Wikimedia REST API 在此環境【可達】—— Wikipedia 這條路可用")
    else:
        print("❌ API 不可達 —— 這條路在你的環境也不通,需另尋替代")
    ok = [r for r in results if r.get("article")]
    print(f"條目探測成功:{len(ok)}/{len(PROBE_TARGETS)}")
    pre = [r for r in results if "PRE-FERMENT" in str(r.get("stage", ""))]
    if pre:
        print(f"其中呈『未發酵翹頭』:{[r['label'] for r in pre]}")
    print("\n(把上面各標的的『基線日均』數字回報給 Claude,用來校準 PRE_FERMENT_MAXMEAN 門檻)")

    with open("wiki_probe_result.json", "w", encoding="utf-8") as f:
        json.dump({"generated_at": str(dt.datetime.now()),
                   "reachable": reachable, "results": results},
                  f, ensure_ascii=False, indent=2)
    print("\n結果已寫入 wiki_probe_result.json")


if __name__ == "__main__":
    main()
