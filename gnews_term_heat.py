# -*- coding: utf-8 -*-
"""
============================================================
gnews_term_heat.py — 產業術語熱度(Google News,2026-10-16 新增)
============================================================
偵測源1(鉅亨關鍵字暴增)對「前段日均 < 0.15」的 watchlist 詞永遠不會觸發——
這些多半是台灣財經新聞還少用、但產業研究常見的術語(混合鍵合、冷板、垂直供電、
CoWoS-L、EMIB-T、HBF…)。本程式只針對這些詞,改查 Google News,用同一套
「近3個交易日 vs 前17天」算暴增比,結果寫 gnews_term_heat.json,
daily_digest 讀成 D2 區。

不改偵測源1、A 區或任何既有門檻;只是另一個參考區塊。

規則(2026-10-16 GHA 實測後訂定):
- 對象:event_theme_raw.json 的 keyword_stats 中 enough_base=False 的詞
- 查詢:台灣版一律查;英文詞只有「含數字或連字號」(BlueField-4、EMIB-T、CoWoS-L…)
  才加查美國版——LPO/XPO/UEC/HBF 這類縮寫在美國版是樂團、物流、鈾礦、基因研究
- 計數:Google 的引號搜尋不精確(「空芯光纖」9則中0則標題含完整詞),
  只計標題(去掉「 - 媒體名」)含完整詞的新聞;台灣版/美國版同標題去重
- 窗口:沿用偵測源1 的交易日窗口(event_theme_raw.json 的 window.recent_start)
- 個股:用 event_theme_radar.extract_stock_codes_from_articles(綜述文排除、
  純股名需出現2次)從命中標題抽股號
- 門檻(初版,待校準):暴增 = 暴增比>=1.5 且近期>=2則 且前段>=2則;
  新出現 = 前段0則 且近期>=2則;關注 = 暴增比 0.8~1.5 且近期>=2則 且前段>=2則
============================================================
"""
import re
import json
import time
import datetime as dt
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import requests

RAW_FILE = "event_theme_raw.json"
OUT_FILE = "gnews_term_heat.json"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120 Safari/537.36"}
TPE = dt.timezone(dt.timedelta(hours=8))
GNEWS = "https://news.google.com/rss/search?q={q}&{loc}"
LOC_TW = "hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
LOC_US = "hl=en-US&gl=US&ceid=US:en"
RECENT_DAYS, BASELINE_DAYS = 3, 20
LOOKBACK_DAYS = 24          # 查詢 when: 天數(含連假時交易日窗口會拉長,多抓幾天)
MAX_TERMS = 150
SLEEP = 1.0
GNEWS_CAP = 100             # Google News RSS 單次最多回傳筆數;抓滿代表可能被截斷
SURGE_RATIO, NEAR_LOW = 1.5, 0.8
MIN_RECENT, MIN_EARLIER = 2, 2
STATUS_ORDER = {"surge": 0, "new": 1, "watch": 2, "quiet": 3}


def norm(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", s or "")).strip().lower()


def strip_source(title):
    """「標題 - 媒體名」→「標題」(避免「工商時報」被當成股名「時報」)。"""
    return title.rsplit(" - ", 1)[0].strip() if " - " in title else title.strip()


def use_us(term):
    """只有含數字或連字號的英文詞才查美國版(縮寫在美國版歧義太多)。"""
    return (re.search(r"[A-Za-z]", term) is not None and not re.search(r"[一-鿿]", term)
            and re.search(r"[\d\-]", term) is not None)


def parse_rss(content):
    items = []
    for it in ET.fromstring(content).iter("item"):
        t = (it.findtext("title") or "").strip()
        try:
            d = parsedate_to_datetime(it.findtext("pubDate") or "")
        except Exception:
            d = None
        items.append({"title": t, "pub": d})
    return items


def fetch(term, loc):
    url = GNEWS.format(q=quote(f'"{term}" when:{LOOKBACK_DAYS}d'), loc=loc)
    r = requests.get(url, headers=UA, timeout=25)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    return parse_rss(r.content)


def count_term(term, items, recent_start, base_start):
    """只計標題含完整詞的新聞(同標題去重),回傳統計 dict。"""
    k = norm(term)
    seen, hits = set(), []
    for it in items:
        t = strip_source(it["title"])
        if it["pub"] is None or k not in norm(t) or norm(t) in seen:
            continue
        seen.add(norm(t))
        ts = it["pub"].timestamp()
        if ts >= base_start:
            hits.append({"title": t, "ts": ts})
    recent = [h for h in hits if h["ts"] >= recent_start]
    earlier = [h for h in hits if h["ts"] < recent_start]
    recent_daily = len(recent) / RECENT_DAYS
    earlier_daily = len(earlier) / (BASELINE_DAYS - RECENT_DAYS) if earlier else 0.01
    ratio = round(recent_daily / earlier_daily, 2)
    if len(recent) >= MIN_RECENT and len(earlier) >= MIN_EARLIER and ratio >= SURGE_RATIO:
        status = "surge"
    elif len(recent) >= MIN_RECENT and not earlier:
        status = "new"
    elif len(recent) >= MIN_RECENT and len(earlier) >= MIN_EARLIER and ratio >= NEAR_LOW:
        status = "watch"
    else:
        status = "quiet"
    return {"recent": len(recent), "earlier": len(earlier), "earlier_daily": round(earlier_daily, 3),
            "ratio": ratio, "status": status,
            "titles": [h["title"] for h in sorted(hits, key=lambda h: -h["ts"])]}


def target_terms(raw):
    stats = raw.get("keyword_stats") or {}
    return [kw for kw, st in stats.items() if not st.get("enough_base")][:MAX_TERMS]


def window_from_raw(raw):
    rs = (raw.get("window") or {}).get("recent_start")
    if rs:
        d = dt.date.fromisoformat(rs)
    else:   # 舊格式沒有 window:退回「今天往前3天」
        d = dt.datetime.now(TPE).date() - dt.timedelta(days=RECENT_DAYS - 1)
    recent_start = int(dt.datetime(d.year, d.month, d.day, tzinfo=TPE).timestamp())
    return recent_start, recent_start - (BASELINE_DAYS - RECENT_DAYS) * 86400


def build(raw, fetcher=fetch, name2code=None, extract=None, sleep=SLEEP):
    """fetcher(term, loc) 可替換成假資料(測試用)。"""
    recent_start, base_start = window_from_raw(raw)
    terms, errors = [], []
    for term in target_terms(raw):
        items, n_raw, truncated = [], 0, False
        for loc in [LOC_TW] + ([LOC_US] if use_us(term) else []):
            try:
                got = fetcher(term, loc)
                n_raw += len(got)
                truncated = truncated or len(got) >= GNEWS_CAP
                items += got
            except Exception as e:
                errors.append(f"{term}: {str(e)[:60]}")
            time.sleep(sleep)
        st = count_term(term, items, recent_start, base_start)
        codes = []
        if name2code and extract and st["titles"]:
            try:
                codes, _ = extract(term, st["titles"], name2code)
            except Exception:
                codes = []
        terms.append({"term": term, "us": use_us(term), "returned": n_raw, "truncated": truncated,
                      **{k: v for k, v in st.items() if k != "titles"},
                      "codes": codes[:6], "titles": st["titles"][:3]})
    terms.sort(key=lambda t: (STATUS_ORDER[t["status"]], -t["ratio"], -t["recent"]))
    return {
        "generated_at": dt.datetime.now(TPE).strftime("%Y-%m-%d %H:%M:%S"),
        "window": {"recent_start": dt.datetime.fromtimestamp(recent_start, TPE).date().isoformat(),
                   "baseline_days": BASELINE_DAYS, "recent_trading_days": RECENT_DAYS},
        "n_terms": len(terms),
        "counts": {s: sum(1 for t in terms if t["status"] == s) for s in STATUS_ORDER},
        "errors": errors[:20],
        "terms": terms,
    }


def main():
    try:
        with open(RAW_FILE, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:
        print(f"  ⚠ 讀不到 {RAW_FILE},略過: {e}")
        return
    if not raw.get("keyword_stats"):
        print("  ⚠ event_theme_raw.json 沒有 keyword_stats(舊版雷達輸出),略過")
        return
    name2code, extract = {}, None
    try:
        from event_theme_radar import build_name2code, extract_stock_codes_from_articles
        name2code, _ = build_name2code()
        extract = extract_stock_codes_from_articles
    except Exception as e:
        print(f"  ⚠ 股名對照載入失敗(不抽個股): {e}")
    print(f"產業術語熱度(Google News):{len(target_terms(raw))} 個鉅亨基數不足的詞")
    out = build(raw, name2code=name2code, extract=extract)
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    c = out["counts"]
    print(f"  暴增 {c['surge']}、新出現 {c['new']}、關注 {c['watch']}、平淡 {c['quiet']};查詢錯誤 {len(out['errors'])}")
    for t in out["terms"]:
        if t["status"] == "quiet":
            break
        print(f"    [{t['status']}] {t['term']} 近{t['recent']} 前段{t['earlier']} 暴增比{t['ratio']} 股:{t['codes']}")
    print(f"→ {OUT_FILE} 已寫出")


if __name__ == "__main__":
    main()
