# -*- coding: utf-8 -*-
"""
============================================================
gemini_active_search.py — 偵測源4補強:Gemini主動搜尋上升關鍵字
============================================================
跟 ai_theme_discovery.py 互補,但機制完全不同:
  ai_theme_discovery.py = 被動萃取(先收集新聞標題,再請AI從中挑題材)
                          → 受限於「我們收集到的新聞範圍」,可能漏掉
                            還沒進入收集範圍的訊號(如Reuters/NPO案例)
  gemini_active_search.py = 主動查詢(直接問Gemini,用Google Search grounding
                            即時上網搜尋,不受限於我們自己收集的新聞池)

用 Gemini 3.x 的 Search grounding 功能(tools: google_search),免費層每月
5,000次落地搜尋額度,本模組用量(3市場×1次/天≈90次/月)遠低於額度。

分台股/美股/日股三個市場各自查詢,找「近期剛開始被討論、還不是主流話題」
的關鍵字/題材,避免問法太廣泛只問到已經人盡皆知的東西。

輸出格式刻意對齊 ai_theme_discovery.py 的 {candidates, multi_source} 結構,
供 theme_tracker.py 直接合併讀取(第三個候選來源)。
============================================================
"""
import os
import re
import json
import time
import datetime as dt
import requests

GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = "gemini-3.5-flash-lite"
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

# 三個市場各自的查詢設計(避免用同一個泛問法,個別鎖定該市場的訊號特性)
MARKET_QUERIES = {
    "TW": {
        "label": "台股",
        "prompt": """請搜尋近3天內的財經新聞與研究報告，找出台股半導體/科技供應鏈相關、
剛開始被討論、還不是主流話題的新興關鍵字或題材（技術名詞、新規格、新供應鏈動態）。
不要列已經是老生常談的詞（如AI、半導體、記憶體這種太泛的詞），
要列具體、可能還沒被多數投資人注意到的新訊號。""",
    },
    "US": {
        "label": "美股",
        "prompt": """請搜尋近3天內的美股科技/半導體相關新聞、外資研究報告（花旗、高盛、
摩根士丹利等）、產業會議紀要，找出正在興起、還未被廣泛報導的技術題材或供應鏈變化
（例如新技術規格、供應鏈瓶頸轉移、新創公司動態）。列出具體技術名詞，不要泛稱。""",
    },
    "JP": {
        "label": "日股",
        "prompt": """請搜尋近3天內日本股市相關的半導體設備、材料、機器人、精密製造領域新聞，
找出剛開始被討論、可能與台灣供應鏈有連動關係的新興關鍵字或題材。""",
    },
}

OUTPUT_INSTRUCTION = """

用純JSON格式回傳（不要有其他文字說明、不要用markdown code fence包裹），格式：
{"themes":[{"term":"題材名稱(2-8字或英文技術詞)","reason":"為何是新興題材(20字內)","confidence":"high或medium"}]}

只回傳你有把握是「近期剛興起」的題材，寧缺勿濫。若搜尋不到明確的新興題材，回傳 {"themes":[]}。"""


def _extract_json(text):
    """從回應文字中提取JSON(容錯:可能包著markdown code fence或前後有雜訊文字)。"""
    text = text.strip()
    # 去除markdown code fence
    text = re.sub(r'^```json\s*', '', text)
    text = re.sub(r'^```\s*', '', text)
    text = re.sub(r'```\s*$', '', text)
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # 容錯:找第一個 { 到最後一個 } 之間的內容
        start, end = text.find('{'), text.rfind('}')
        if start != -1 and end != -1:
            try:
                return json.loads(text[start:end+1])
            except json.JSONDecodeError:
                return None
        return None


COMBINED_PROMPT = """請搜尋近3天內的財經新聞、研究報告與產業會議紀要，分別針對【台股】【美股】【日股】
三個市場，各找出半導體/科技供應鏈相關、剛開始被討論、還不是主流話題的新興關鍵字或題材
（技術名詞、新規格、新供應鏈動態）。不要列已經是老生常談的詞（如AI、半導體、記憶體這種太泛的詞），
要列具體、可能還沒被多數投資人注意到的新訊號。

台股：留意半導體供應鏈、先進封裝、記憶體規格變化
美股：留意外資研究報告（花旗、高盛、摩根士丹利等）提及的技術題材、供應鏈瓶頸轉移
日股：留意半導體設備、材料、機器人、精密製造領域，特別是與台灣供應鏈有連動的動態

用純JSON格式回傳（不要有其他文字說明、不要用markdown code fence包裹），格式：
{"themes":[{"term":"題材名稱(2-8字或英文技術詞)","market":"TW或US或JP","reason":"為何是新興題材(20字內)","confidence":"high或medium"}]}

三個市場合計盡量找5-15個題材，寧缺勿濫。若某市場搜尋不到明確新興題材可以少列或不列。"""


def search_all_markets_combined():
    """單次合併查詢三市場(取代原本3次個別呼叫),大幅降低grounding額度壓力。
    2026-09-11實測發現:Google Search grounding有獨立於一般generateContent的
    更嚴格額度限制,即使一般額度顯示充足,grounding請求仍可能429。合併成1次呼叫
    把grounding請求量從3降到1,是目前最直接的緩解方式。"""
    if not GEMINI_KEY:
        return []
    body = {
        "contents": [{"role": "user", "parts": [{"text": COMBINED_PROMPT}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {"temperature": 0.2},
    }
    try:
        r = requests.post(
            GEMINI_URL,
            headers={"Content-Type": "application/json", "x-goog-api-key": GEMINI_KEY},
            json=body, timeout=60,
        )
        if r.status_code != 200:
            if r.status_code == 429:
                print(f"  ⚠ 合併查詢額度/頻率限制(429): {r.text[:300]}")
            else:
                print(f"  ⚠ 合併查詢失敗: HTTP {r.status_code} {r.text[:200]}")
            return []
        data = r.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        result = _extract_json(text)
        if not result:
            print(f"  ⚠ 回應無法解析為JSON")
            return []
        return result.get("themes", [])
    except Exception as e:
        print(f"  ⚠ 合併查詢錯誤: {e}")
        return []


def search_market(market_code, market_info):
    """對單一市場做 Gemini + Google Search grounding 查詢。
    (保留此函式供未來需要單獨查詢單一市場時使用;預設流程已改用合併查詢)"""
    if not GEMINI_KEY:
        return []
    prompt = market_info["prompt"] + OUTPUT_INSTRUCTION
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {"temperature": 0.2},
    }
    try:
        r = requests.post(
            GEMINI_URL,
            headers={"Content-Type": "application/json", "x-goog-api-key": GEMINI_KEY},
            json=body, timeout=60,
        )
        if r.status_code != 200:
            if r.status_code == 429:
                print(f"  ⚠ {market_info['label']} 額度/頻率限制(429): "
                      f"若是「每分鐘請求數」超限,下次執行會自動恢復;"
                      f"若是「每日/每月額度」用盡,需等額度重置或檢查用量。"
                      f"  {r.text[:150]}")
            else:
                print(f"  ⚠ {market_info['label']} 查詢失敗: HTTP {r.status_code} {r.text[:200]}")
            return []
        data = r.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        result = _extract_json(text)
        if not result:
            print(f"  ⚠ {market_info['label']} 回應無法解析為JSON")
            return []
        return result.get("themes", [])
    except Exception as e:
        print(f"  ⚠ {market_info['label']} 查詢錯誤: {e}")
        return []


def main():
    now = dt.datetime.now()
    print("=" * 60)
    print(f"Gemini主動搜尋:台美日股上升關鍵字  {now.strftime('%Y-%m-%d')}")
    print("=" * 60)

    if not GEMINI_KEY:
        print("✗ 找不到 GEMINI_API_KEY,寫出空結果")
        json.dump({"generated_at": str(now), "candidates": [], "multi_source": []},
                  open("gemini_search_candidates.json", "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        return

    print("\n■ 合併查詢台/美/日三市場中(單次grounding呼叫)...")
    themes = search_all_markets_combined()
    all_candidates = []
    market_label = {"TW": "台股", "US": "美股", "JP": "日股"}
    for th in themes:
        term = th.get("term", "").strip()
        if not term:
            continue
        market = th.get("market", "").strip().upper()
        market_tag = market if market in market_label else "UNKNOWN"
        all_candidates.append({
            "term": term,
            "reason": th.get("reason", ""),
            "confidence": th.get("confidence", "medium"),
            "sources": [f"gemini_search_{market_tag.lower()}"],
            "market": market_label.get(market, market),
            "stocks": [],
            "recent_hits": 1,  # 主動搜尋沒有「次數」概念,固定給1(供theme_tracker門檻判斷用)
        })
        flag = "🔥高信心" if th.get("confidence") == "high" else ""
        print(f"  [{market_label.get(market, market)}] {term}  {th.get('reason','')} {flag}")
    if not themes:
        print(f"  (無明確新興題材,或查詢失敗)")

    # 跨市場出現同一詞(理論上少見,但若有代表訊號更強)→ 合併sources
    merged = {}
    for c in all_candidates:
        term = c["term"]
        if term in merged:
            merged[term]["sources"] = list(set(merged[term]["sources"] + c["sources"]))
        else:
            merged[term] = c
    candidates = list(merged.values())
    multi_source = [c for c in candidates if len(c["sources"]) >= 2]

    out = {
        "generated_at": str(now),
        "candidates": candidates,
        "multi_source": multi_source,
        "method": "gemini-active-search",
    }
    json.dump(out, open("gemini_search_candidates.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print(f"\n→ gemini_search_candidates.json 已寫出({len(candidates)}個題材)")


if __name__ == "__main__":
    main()
