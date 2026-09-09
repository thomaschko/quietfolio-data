# -*- coding: utf-8 -*-
"""
============================================================
ai_theme_discovery.py — 偵測源4升級:AI語意理解版新題材發現
============================================================
取代/補強 new_theme_discovery.py 的 jieba 斷詞統計。
用 Gemini API 對當日新聞標題做「語意理解」而非「詞頻統計」,
解決 BTC/金色財經/人名 這類雜訊無法用停用詞表窮舉排除的問題。

資料源:複用 news_sources.py 的多源標題(cna/moneydj/wealth/wapeople/
       technews/eetimes/trendforce)+ 鉅亨搜尋結果(可選,量大時跳過)

模型:gemini-2.5-flash-lite(免費層1000+次/天,1M context一次塞完當日標題)

輸出格式:刻意對齊 new_theme_discovery.py 的 multi_source 結構,
        讓 theme_tracker.py 不用改就能吃(term/sources/stocks/recent_hits)

版權:只送標題(已公開的新聞標題本身),不送全文;只取回結構化題材判斷。

⚠️ 已知風險:部分回報 Gemini API 從雲端主機(如GitHub Actions)呼叫
   可能遇到403,原因未明確(IP範圍或heaader問題)。本腳本內建探測模式
   (--probe),請先手動跑一次確認可用,再排進每日流程。
============================================================
"""
import os
import re
import json
import datetime as dt
import requests

GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = "gemini-2.5-flash-lite"
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

WATCHLIST_FILE = "themes_watchlist.txt"

SYSTEM_PROMPT = """你是台股半導體/科技供應鏈研究助理。你會收到今天的財經新聞標題清單(每行一則,格式:編號|標題)。

任務:找出真正值得追蹤的「產業題材」——尤其是還沒被廣泛報導、剛冒頭的技術詞/供應鏈動態。

明確排除以下類型(不算題材):
- 加密貨幣/虛擬貨幣相關(比特幣、BTC、以太幣等)
- 單純的公司月營收/財報公告(如「XX公司8月營收年增N%」這類例行公告)
- 人物專訪、人事異動、獲獎新聞
- 大盤/指數漲跌描述(如「台股收漲XX點」)
- 純總體經濟數據(匯率、CPI、失業率)
- 知名科技巨頭的日常新聞(除非該則新聞代表某個新技術/新供應鏈動向的重大訊號)

只保留:半導體製程/封裝技術、記憶體規格、光通訊元件、電源/散熱技術、
機器人/自動化、新能源材料、以及這些領域的具體供應鏈公司動態。

用純JSON格式回傳(不要有其他文字說明),格式:
{"themes":[{"term":"題材名稱(2-8字或英文技術詞)","reason":"為何是題材(15字內)","related_ids":[標題編號,...],"confidence":"high或medium"}]}

只回傳你有信心的題材,寧缺勿濫。沒有值得追蹤的題材時回傳 {"themes":[]}。"""


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


def collect_titles():
    """收集今日多源標題(複用 news_sources.py),回傳 [(idx, title, source)]"""
    titles = []
    try:
        from news_sources import fetch_titles_by_source
        by_source = fetch_titles_by_source()
        for src, ts in by_source.items():
            for t in ts:
                titles.append((t, src))
    except Exception as e:
        print(f"  ⚠ news_sources 讀取失敗: {e}")
    # 去重(同標題只留一次,但記錄可能多源)
    seen = {}
    for t, src in titles:
        if t not in seen:
            seen[t] = set()
        seen[t].add(src)
    return list(seen.items())  # [(title, {sources})]


def call_gemini(prompt_text):
    """呼叫 Gemini API,回傳解析後的 JSON(dict)或 None。"""
    if not GEMINI_KEY:
        print("  ✗ 找不到 GEMINI_API_KEY 環境變數")
        return None
    body = {
        "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": prompt_text}]}],
        "generationConfig": {"response_mime_type": "application/json", "temperature": 0.2},
    }
    try:
        r = requests.post(
            GEMINI_URL,
            headers={"Content-Type": "application/json", "x-goog-api-key": GEMINI_KEY},
            json=body,
            timeout=60,
        )
        print(f"  Gemini API 狀態: {r.status_code}")
        if r.status_code != 200:
            print(f"  回應內容: {r.text[:300]}")
            return None
        data = r.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        return json.loads(text)
    except Exception as e:
        print(f"  ✗ Gemini 呼叫/解析失敗: {e}")
        return None


def probe_mode():
    """探測模式:只測試 API 通不通,用最小請求,不消耗標題資料。"""
    print("=" * 60)
    print("Gemini API 探測模式")
    print("=" * 60)
    if not GEMINI_KEY:
        print("✗ 找不到 GEMINI_API_KEY,請先設定 GitHub Secret")
        return
    body = {"contents": [{"parts": [{"text": "回覆「OK」兩個字即可,不要有其他內容。"}]}]}
    try:
        r = requests.post(
            GEMINI_URL,
            headers={"Content-Type": "application/json", "x-goog-api-key": GEMINI_KEY},
            json=body, timeout=30,
        )
        print(f"狀態碼: {r.status_code}")
        if r.status_code == 200:
            data = r.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            print(f"回應: {text}")
            print("\n✅ API 可正常呼叫,可以排進每日流程")
        else:
            print(f"回應內容: {r.text[:500]}")
            print("\n✗ API 呼叫失敗,請檢查上面錯誤訊息")
            print("  常見原因: 1)key錯誤 2)雲端IP被擋(403) 3)額度用盡(429)")
    except Exception as e:
        print(f"✗ 連線錯誤: {e}")


def main():
    import sys
    if "--probe" in sys.argv:
        probe_mode()
        return

    now = dt.datetime.now()
    print("=" * 60)
    print(f"AI題材發現(Gemini語意版)  {now.strftime('%Y-%m-%d')}")
    print("=" * 60)

    watchlist = load_watchlist_terms()
    title_list = collect_titles()  # [(title, {sources})]
    print(f"收集到 {len(title_list)} 則不重複標題")
    if not title_list:
        print("  無標題可分析,寫出空結果")
        json.dump({"generated_at": str(now), "candidates": [], "multi_source": []},
                  open("ai_theme_candidates.json", "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        return

    # 組 prompt(編號|標題,每行一則)
    lines = [f"{i}|{t}" for i, (t, _) in enumerate(title_list)]
    prompt = "今天的新聞標題:\n" + "\n".join(lines)

    result = call_gemini(prompt)
    if result is None:
        print("  Gemini 呼叫失敗,寫出空結果(不影響其他偵測源)")
        json.dump({"generated_at": str(now), "candidates": [], "multi_source": [],
                   "error": "gemini call failed"},
                  open("ai_theme_candidates.json", "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        return

    themes = result.get("themes", [])
    print(f"\nGemini 判斷出 {len(themes)} 個題材:")

    candidates = []
    for th in themes:
        term = th.get("term", "").strip()
        if not term or term.lower() in watchlist:
            continue
        related_ids = th.get("related_ids", [])
        srcs = set()
        for rid in related_ids:
            if 0 <= rid < len(title_list):
                srcs |= title_list[rid][1]
        entry = {
            "term": term,
            "reason": th.get("reason", ""),
            "confidence": th.get("confidence", "medium"),
            "sources": sorted(srcs),
            "stocks": [],  # AI版暫不解析個股,交給人工或後續比對watchlist
            "recent_hits": len(related_ids),
        }
        candidates.append(entry)
        conf_flag = "🔥高信心" if entry["confidence"] == "high" else ""
        print(f"  {term}  {'/'.join(entry['sources'])}  {th.get('reason','')} {conf_flag}")

    multi_source = [c for c in candidates if len(c["sources"]) >= 2]

    out = {
        "generated_at": str(now),
        "candidates": candidates,
        "multi_source": multi_source,
        "method": "gemini-semantic",
        "titles_analyzed": len(title_list),
    }
    json.dump(out, open("ai_theme_candidates.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print(f"\n→ ai_theme_candidates.json 已寫出({len(candidates)}個題材,"
          f"跨源{len(multi_source)}個)")


if __name__ == "__main__":
    main()
