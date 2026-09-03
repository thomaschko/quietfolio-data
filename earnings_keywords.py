# -*- coding: utf-8 -*-
"""
============================================================
earnings_keywords.py — 偵測源6:國際大廠法說關鍵詞頻率
============================================================
資料源:Alpha Vantage EARNINGS_CALL_TRANSCRIPT(正規API,免費key)
  優於抓 fool.com:結構化JSON、無反爬風險、無版權疑慮、附情緒分數。

核心邏輯:
  抓 NVDA上下游生態系 各家最新季法說逐字稿
  → 統計你關心的關鍵詞頻率(CoWoS/HBM/CPO/供應商...)
  → 季度對比:這季 vs 上季,哪些詞頻率上升 = 客戶端/供應鏈訊號升溫
  → 只輸出「詞×次數」數字(版權安全,不存原文)

節奏:季度性。法說一季一次,平時休眠,財報季跑。
額度:AV免費25次/天。五家×1次=5次,綽綽有餘。

設定:
  API key 存 GitHub Secret,用環境變數 ALPHAVANTAGE_KEY 讀(勿寫死在碼裡)
  公司清單、詞表 見下方,可自由增減

用法:
  export ALPHAVANTAGE_KEY=你的key
  python earnings_keywords.py
============================================================
"""

import os
import re
import json
import time
import datetime as dt
import requests

AV_KEY = os.environ.get("ALPHAVANTAGE_KEY", "")
AV_URL = "https://www.alphavantage.co/query"

# ── 監控公司:NVDA 生態系(上下游+競爭+客戶)──
# 角色標註幫你解讀:不同角色講的話領先性不同
COMPANIES = {
    "NVDA": "本尊-需求指引",
    "TSM":  "上游-晶圓封裝(CoWoS)",
    "MU":   "上游-記憶體(HBM)",
    "AMD":  "競爭-GPU/ASIC",
    "ASML": "上游-設備(產能領先)",
    # 之後可加:AVGO博通 MRVL邁威爾(競爭/ASIC), MSFT GOOGL META(客戶capex)
}

# ── 五層關鍵詞表(對應你的持股與研究)──
KEYWORDS = {
    "先進封裝/光互連": [
        "CoWoS", "SoIC", "CoPoS", "CPO", "co-packaged", "silicon photonics",
        "advanced packaging", "2.5D", "3D packaging", "interposer",
        "glass substrate", "panel-level", "chiplet",
    ],
    "記憶體": [
        "HBM", "HBM4", "HBM4E", "DRAM", "LPDDR", "NAND", "custom HBM",
        "base die", "memory", "high bandwidth memory",
    ],
    "AI電源/散熱": [
        "800V", "HVDC", "power shelf", "sidecar", "liquid cooling",
        "immersion", "BBU", "power delivery",
    ],
    "平台/網通/供應鏈": [
        "Vera Rubin", "Rubin", "Blackwell", "NVLink", "co-design",
        "supply chain", "foundry", "capacity", "lead time", "bottleneck",
        "yield",
    ],
    "台廠點名": [
        "TSMC", "Taiwan", "Alchip", "eMemory", "MediaTek",
    ],
}
# 攤平成單一 list 供統計,同時保留分類供輸出
FLAT_KEYWORDS = [(cat, kw) for cat, kws in KEYWORDS.items() for kw in kws]


def get_recent_quarters(n=2):
    """回傳最近 n 季的 YYYYQM 標籤(如 2026Q2)。用當前日期推算。"""
    today = dt.date.today()
    # 粗略季度推算(財報通常落後一季,取當前季往前推)
    q = (today.month - 1) // 3 + 1
    y = today.year
    quarters = []
    for _ in range(n + 1):  # 多取一季緩衝(財報有延遲)
        q -= 1
        if q < 1:
            q = 4
            y -= 1
        quarters.append(f"{y}Q{q}")
    return quarters


def fetch_transcript(symbol, quarter):
    """抓某公司某季逐字稿。回傳 (transcript_text, avg_sentiment) 或 (None, None)。"""
    params = {"function": "EARNINGS_CALL_TRANSCRIPT", "symbol": symbol,
              "quarter": quarter, "apikey": AV_KEY}
    try:
        r = requests.get(AV_URL, params=params, timeout=30)
        if r.status_code != 200:
            return None, None
        data = r.json()
        # AV 回傳結構:{"symbol":..,"quarter":..,"transcript":[{content, sentiment,..}]}
        segs = data.get("transcript")
        if not segs:
            # 額度用盡或無資料時,AV 會回 Note/Information
            note = data.get("Note") or data.get("Information") or data.get("error")
            if note:
                print(f"    ⚠ {symbol} {quarter}: {str(note)[:80]}")
            return None, None
        # 合併所有段落文字(只用於本地統計,不儲存)
        texts, sents = [], []
        for seg in segs:
            if isinstance(seg, dict):
                texts.append(seg.get("content", ""))
                s = seg.get("sentiment")
                if s is not None:
                    try:
                        sents.append(float(s))
                    except (ValueError, TypeError):
                        pass
            elif isinstance(seg, str):
                texts.append(seg)
        full = " ".join(texts)
        avg_sent = round(sum(sents) / len(sents), 3) if sents else None
        return full, avg_sent
    except Exception as e:
        print(f"    ⚠ {symbol} {quarter} 抓取錯誤: {str(e)[:60]}")
        return None, None


def count_keywords(text):
    """統計每個關鍵詞出現次數(大小寫不敏感)。只回數字,不留原文。"""
    counts = {}
    for cat, kw in FLAT_KEYWORDS:
        n = len(re.findall(re.escape(kw), text, re.IGNORECASE))
        if n > 0:
            counts[kw] = {"count": n, "category": cat}
    return counts


def analyze_company(symbol, role, quarters):
    """抓最近兩季,做關鍵詞頻率 + 季度對比。"""
    print(f"\n■ {symbol} ({role})")
    results = {}
    sentiments = {}
    for q in quarters[:2]:  # 最近兩季(季度對比用)
        text, sent = fetch_transcript(symbol, q)
        time.sleep(1)  # 尊重 AV 頻率限制
        if text:
            results[q] = count_keywords(text)
            sentiments[q] = sent
            print(f"    {q}: {len(text.split())}字, 情緒{sent}, "
                  f"命中{len(results[q])}個關鍵詞")
        else:
            results[q] = {}
    return results, sentiments


def main():
    if not AV_KEY:
        print("✗ 找不到 ALPHAVANTAGE_KEY 環境變數。請設定後再跑。")
        print("  export ALPHAVANTAGE_KEY=你的key")
        return

    quarters = get_recent_quarters(2)
    print("=" * 60)
    print(f"國際大廠法說關鍵詞追蹤  查詢季度: {quarters[:2]}")
    print("=" * 60)

    all_data = {}
    for symbol, role in COMPANIES.items():
        res, sents = analyze_company(symbol, role, quarters)
        all_data[symbol] = {"role": role, "quarters": res, "sentiments": sents}

    # ── 季度對比:找頻率上升的詞 ──
    print("\n" + "=" * 60)
    print("季度對比:關鍵詞頻率上升(客戶端/供應鏈訊號升溫)")
    print("=" * 60)
    rising = []
    for symbol, d in all_data.items():
        qs = list(d["quarters"].keys())
        if len(qs) < 2:
            continue
        # 明確排序:年季字串排序後,較新的在後(2026Q2 > 2026Q1)
        qs_sorted = sorted(qs)  # ['2026Q1','2026Q2']
        prev_q, this_q = qs_sorted[0], qs_sorted[-1]  # prev=舊, this=新
        this_c = d["quarters"][this_q]
        prev_c = d["quarters"][prev_q]
        for kw, info in this_c.items():
            now_n = info["count"]
            prev_n = prev_c.get(kw, {}).get("count", 0)
            if now_n > prev_n and now_n >= 2:  # 新季頻率高於舊季,且至少2次
                rising.append({
                    "symbol": symbol, "keyword": kw, "category": info["category"],
                    "this_count": now_n, "prev_count": prev_n,
                    "delta": now_n - prev_n,
                    "this_q": this_q, "prev_q": prev_q,
                })
    rising.sort(key=lambda x: -x["delta"])
    for r in rising[:25]:
        newflag = "🆕新提及" if r["prev_count"] == 0 else f"↑{r['prev_count']}→{r['this_count']}"
        print(f"  {r['symbol']:5s} {r['keyword']:20s} [{r['category']}] {newflag} ({r['prev_q']}→{r['this_q']})")

    # ── 輸出(只有數字,版權安全)──
    out = {
        "generated_at": str(dt.datetime.now()),
        "quarters_queried": quarters[:2],
        "companies": {s: {"role": d["role"],
                          "sentiments": d["sentiments"],
                          "keyword_counts": {q: {k: v["count"] for k, v in c.items()}
                                             for q, c in d["quarters"].items()}}
                      for s, d in all_data.items()},
        "rising_keywords": rising,
    }
    with open("earnings_keywords.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n→ earnings_keywords.json 已寫出({len(rising)}個上升關鍵詞)")


if __name__ == "__main__":
    main()
