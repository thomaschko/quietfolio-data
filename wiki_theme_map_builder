# -*- coding: utf-8 -*-
"""
============================================================
wiki_theme_map_builder.py — 題材↔維基條目 對照表建構器
============================================================
目的:把你整份 themes_watchlist.txt 對中文維基跑一遍,自動產出
      「哪些題材有穩定維基條目、近期關注度如何」的清單,供你人工確認。

為什麼需要這步:
  上次驗證發現「世芯」會 fallback 錯配到「智原科技」。題材層不能重蹈覆轍。
  這支腳本【只回報探測到的條目名】,由你人工確認語意正確後,才寫進正式對照表。
  → 絕不自動 fallback、絕不猜。寧可漏,不可錯配。

輸出:wiki_theme_map_draft.json —— 內含每個題材的:
  - 探測到的條目名(或 None)
  - 近期關注度指標(基線日均/近期日均/暴增比/階段)
  - needs_review 旗標:提醒你人工確認條目語意是否真的對應該題材

跑法:放 quietfolio-data 根目錄,用 wiki-probe.yml 同款 workflow 手動觸發
      (或直接改 wiki-probe.yml 的 run 那行指向這支)
依賴:requests
============================================================
"""

import requests
import urllib.parse
import datetime as dt
import json
import time

UA = {"User-Agent": "quietfolio-radar/1.0 (https://github.com/thomaschko; theme attention research)"}
WIKI = "zh.wikipedia"
REST = "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article"
APIURL = "https://zh.wikipedia.org/w/api.php"
WATCHLIST_FILE = "themes_watchlist.txt"

RECENT_DAYS = 3
BASELINE_DAYS = 20
SURGE_RATIO = 1.5
BASELINE_MAX = 150   # 基線日均低於此才算「尚未大眾化」(用上次實測校準:題材落23-125,台積電600)


def check_article(title):
    """確認條目是否存在。回傳 (正規化標題 or None, 是否為重定向)。
    重定向會被標記,因為重定向可能語意跑掉(如世芯→智原),需人工複查。"""
    params = {"action": "query", "format": "json", "titles": title, "redirects": 1}
    try:
        r = requests.get(APIURL, params=params, headers=UA, timeout=15)
        r.raise_for_status()
        data = r.json().get("query", {})
        redirected = bool(data.get("redirects"))
        pages = data.get("pages", {})
        for pid, page in pages.items():
            if pid != "-1" and "missing" not in page:
                return page.get("title", title), redirected
    except Exception as e:
        print(f"    (check error {str(e)[:50]})")
    return None, False


def get_pageviews(title, start, end):
    enc = urllib.parse.quote(title.replace(" ", "_"), safe="")
    url = f"{REST}/{WIKI}/all-access/all-agents/{enc}/daily/{start}/{end}"
    try:
        r = requests.get(url, headers=UA, timeout=20)
        if r.status_code != 200:
            return None
        return [(it["timestamp"][:8], it["views"]) for it in r.json().get("items", [])]
    except Exception:
        return None


def analyze(series):
    if not series or len(series) < RECENT_DAYS + 5:
        return None
    vals = [v for _, v in series]
    recent = vals[-RECENT_DAYS:]
    baseline = vals[-(RECENT_DAYS + BASELINE_DAYS):-RECENT_DAYS] or vals[:-RECENT_DAYS]
    r_mean = sum(recent) / len(recent)
    b_mean = sum(baseline) / len(baseline) if baseline else 0
    ratio = (r_mean / b_mean) if b_mean > 0 else None
    surging = ratio is not None and ratio >= SURGE_RATIO
    still_low = b_mean <= BASELINE_MAX
    if surging and still_low:
        stage = "PRE-FERMENT"
    elif surging:
        stage = "ACTIVE-HOT"
    else:
        stage = "flat"
    return {"baseline_mean": round(b_mean, 1), "recent_mean": round(r_mean, 1),
            "ratio": round(ratio, 2) if ratio else None,
            "recent_max": max(recent), "stage": stage}


def load_watchlist():
    kws = []
    with open(WATCHLIST_FILE, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if ln and not ln.startswith("#"):
                kws.append(ln)
    return kws


def main():
    today = dt.date.today()
    start = (today - dt.timedelta(days=BASELINE_DAYS + RECENT_DAYS + 2)).strftime("%Y%m%d")
    end = today.strftime("%Y%m%d")
    kws = load_watchlist()
    print("=" * 64)
    print(f"題材↔維基 對照表建構  {len(kws)}個題材  期間{start}~{end}")
    print("=" * 64)

    out = []
    have, redirect_warn, preferment = [], [], []

    for kw in kws:
        title, redirected = check_article(kw)
        time.sleep(0.25)
        if not title:
            print(f"  {kw:12s} → 無條目 ✗")
            out.append({"theme": kw, "article": None})
            continue

        series = get_pageviews(title, start, end)
        a = analyze(series) if series else None
        entry = {"theme": kw, "article": title, "redirected": redirected}
        if a:
            entry.update(a)

        # 條目名 != 題材名 或 有重定向 → 標記需人工複查(防「智原」錯配)
        needs_review = redirected or (title != kw)
        entry["needs_review"] = needs_review
        out.append(entry)
        have.append(kw)

        flag = " ⚠需複查" if needs_review else ""
        stg = a["stage"] if a else "no-data"
        ratio = a["ratio"] if a else "-"
        bmean = a["baseline_mean"] if a else "-"
        print(f"  {kw:12s} → 「{title}」 基線{bmean} 暴增{ratio} [{stg}]{flag}")
        if needs_review:
            redirect_warn.append((kw, title))
        if a and a["stage"] == "PRE-FERMENT":
            preferment.append((kw, title, a["ratio"]))

    print("\n" + "=" * 64)
    print("建構總結")
    print("=" * 64)
    print(f"有維基條目:{len(have)}/{len(kws)}")
    print(f"\n⚠ 需人工複查語意(條目名≠題材名 或 有重定向,可能錯配):")
    for kw, title in redirect_warn:
        print(f"    「{kw}」→「{title}」  ← 這是同一個東西嗎?")
    print(f"\n目前呈『未發酵翹頭 PRE-FERMENT』:")
    for kw, title, r in sorted(preferment, key=lambda x: -x[2]):
        print(f"    {kw}(暴增{r})")

    with open("wiki_theme_map_draft.json", "w", encoding="utf-8") as f:
        json.dump({"generated_at": str(dt.datetime.now()),
                   "baseline_max_threshold": BASELINE_MAX,
                   "surge_ratio_threshold": SURGE_RATIO,
                   "themes": out}, f, ensure_ascii=False, indent=2)
    print("\n→ wiki_theme_map_draft.json 已寫出")
    print("→ 請人工檢查上面『需複查』清單,確認語意對的才保留,錯配的剔除")
    print("→ 把這份 log(尤其需複查清單)貼回給 Claude,建正式對照表")


if __name__ == "__main__":
    main()
