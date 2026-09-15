# -*- coding: utf-8 -*-
"""
============================================================
daily_report.py — 每日題材研究報告(Gemini API生成,免費層)
============================================================
讀 daily_digest.json(六源整合A-G區,含2026-09-15新增的E2近期關注)+
theme_tracker.json(新舊題材追蹤),格式化成清楚文字後呼叫Gemini,
產出一篇有分析語氣的研究報告——涵蓋三大區塊:
  1. 題材雷達(D暴增題材 + E新候選 + E2近期關注,含國際法說F區)
  2. 發酵雷達(A多源共振 + B未發酵 + G雙邊確認,你的核心買點邏輯)
  3. 候選題材(E區src4發現的新詞 + E2固定詞的早期訊號,均需人工判斷)

用 Gemini 3.5 Flash Lite(免費層,複用你已有的GEMINI_API_KEY):讀結構化
資料寫摘要的任務用免費層完全夠用,不需要另外付費申請Claude API。

用法:排在 daily_digest.py 之後執行,複用你已設定好的 GEMINI_API_KEY 環境變數,
     不需要新增任何Secret或申請新帳號。
============================================================
"""
import os
import json
import datetime as dt
import requests

GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")  # 複用你已有的Gemini key,免費層
GEMINI_MODEL = "gemini-3.5-flash-lite"
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

DIGEST_FILE = "daily_digest.json"
TRACKER_FILE = "theme_tracker.json"
OUT_FILE = "daily_report.json"


def load_json(path):
    try:
        return json.load(open(path, encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def format_digest_for_prompt(digest, tracker):
    """把digest+tracker轉成清楚的中文文字區塊,分三大類組織
    (題材雷達/發酵雷達/候選題材),比直接丟原始JSON更省token、
    也讓Gemini不用自己解析巢狀結構,能專注在寫分析。"""
    lines = []

    # ════════════════════════════════════════
    # 第一部分:發酵雷達(核心買點邏輯 — 最高優先)
    # ════════════════════════════════════════
    lines.append("═══ 發酵雷達(台股訊號×國際法說,核心買點邏輯) ═══")

    cross = digest.get("G_cross_confirmed", [])
    dual = [x for x in cross if x.get("dual_confirmed")]
    intl_only = [x for x in cross if not x.get("tw_active") and len(x.get("intl_companies", [])) >= 2]
    lines.append("\n【G區 雙邊確認題材(最高優先)】")
    if dual:
        for x in dual:
            lines.append(f"- {x['theme']}: 台股有訊號 + 國際法說({'/'.join(x.get('intl_companies', []))}), "
                        f"關鍵詞:{'/'.join(x.get('intl_keywords', [])[:5])}, "
                        f"受惠股底部{x.get('stocks_low', 0)}檔/高檔{x.get('stocks_high', 0)}檔")
    else:
        lines.append("- 今日無雙邊確認題材")
    lines.append("\n【國際先行、台股未燃(最前緣訊號)】")
    if intl_only:
        for x in intl_only[:8]:
            lines.append(f"- {x['theme']}: 國際({'/'.join(x.get('intl_companies', []))}) 已提及,"
                        f"台股新聞尚未跟上,關鍵詞:{'/'.join(x.get('intl_keywords', [])[:3])}")
    else:
        lines.append("- 無")

    reso = digest.get("A_multi_source_resonance", [])
    lines.append("\n【A區 多源共振個股】")
    if reso:
        for r in reso[:10]:
            pos = {"low": "🟢底部", "mid": "🟡中段", "high": "🔴高檔"}.get(r.get("price_pos"), "")
            name = r.get("name", "")
            lines.append(f"- {r['code']}{name} [{r.get('source_count')}源:{'/'.join(r.get('sources', []))}] "
                        f"{pos} 相關題材:{'/'.join(r.get('themes', [])[:3])}")
    else:
        lines.append("- 今日無多源共振個股")

    pre = digest.get("B_preferment_themes", [])
    lines.append("\n【B區 未發酵題材(維基關注度剛翹頭)】")
    if pre:
        for t in pre:
            lines.append(f"- {t['theme']}: 暴增比{t.get('ratio')} 基線{t.get('baseline')} "
                        f"相關股:{'/'.join(t.get('codes', [])[:5])}")
    else:
        lines.append("- 無")

    chips = digest.get("C_chip_signals", [])
    lines.append("\n【C區 籌碼共振】")
    if chips:
        for c in chips[:8]:
            name = c.get("name", "")
            pos = {"low": "🟢底部", "mid": "🟡中段", "high": "🔴高檔"}.get(c.get("price_pos"), "")
            lines.append(f"- {c['code']}{name} {pos}: 法人合計{c.get('inst_total', 0)}, "
                        f"主力{c.get('mainforce_broker', '無')}")
    else:
        lines.append("- 今日無籌碼共振個股")

    # ════════════════════════════════════════
    # 第二部分:題材雷達(熱度與國際法說)
    # ════════════════════════════════════════
    lines.append("\n\n═══ 題材雷達(新聞熱度與國際法說) ═══")

    surge = digest.get("D_surge_themes", [])
    lines.append("\n【D區 熱度暴增題材(已觸發門檻)】")
    if surge:
        for t in surge[:8]:
            positions = t.get("stock_positions", [])
            pos_str = " ".join(f"{p.get('code')}{p.get('name', '')}"
                              f"{'🟢' if p.get('pos') == 'low' else '🔴' if p.get('pos') == 'high' else '🟡'}"
                              for p in positions[:5]) or " ".join(t.get("codes", [])[:5])
            lines.append(f"- {t['theme']} 暴增{t.get('ratio')}倍: {pos_str}")
    else:
        lines.append("- 無")

    earnings = digest.get("F_earnings_rising", [])
    lines.append("\n【F區 國際大廠法說關鍵詞升溫】")
    if earnings:
        for e in earnings[:10]:
            flag = "新提及" if e.get("prev_count", 0) == 0 else f"{e.get('prev_count')}→{e.get('this_count')}"
            lines.append(f"- {e.get('symbol')} {e.get('keyword')} [{e.get('category')}] {flag}")
    else:
        lines.append("- 無(本週非法說更新日)")

    # ════════════════════════════════════════
    # 第三部分:候選題材(需人工判斷,新舊皆有)
    # ════════════════════════════════════════
    lines.append("\n\n═══ 候選題材(尚未確認,需人工判斷) ═══")

    first_seen = tracker.get("first_seen", [])
    ongoing = tracker.get("ongoing", [])
    lines.append(f"\n【E區 今日首見新題材】(共{len(first_seen)}個,只列method含ai或search的高可信度項)")
    high_conf_new = [e for e in first_seen if "ai" in e.get("method", "") or "search" in e.get("method", "")]
    if high_conf_new:
        for e in high_conf_new[:10]:
            lines.append(f"- {e['term']}: {e.get('reason', '')} (來源:{'/'.join(e.get('sources', []))})")
    else:
        lines.append("- 今日無AI背書的高可信度新題材")

    lines.append(f"\n【E區 連續追蹤中題材】(持續發酵,共{len(ongoing)}個,列連續天數最長前10)")
    if ongoing:
        for e in ongoing[:10]:
            lines.append(f"- {e['term']}: 連續{e.get('streak_days')}天 {e.get('reason', '')}")
    else:
        lines.append("- 無")

    # E2近期關注(2026-09-15新增):固定追蹤詞裡有動能但未過暴增門檻的
    near_miss = digest.get("E2_near_miss", [])
    lines.append(f"\n【E2區 近期關注(固定追蹤詞,有動能但未達暴增門檻)】(共{len(near_miss)}個)")
    if near_miss:
        for t in near_miss[:10]:
            lines.append(f"- {t['theme']}: 暴增比{t.get('ratio')} 近{t.get('recent_count')}次"
                        f"(尚未過1.5倍門檻,但已呈現成長動能)")
    else:
        lines.append("- 今日無")

    return "\n".join(lines)


SYSTEM_PROMPT = """你是台股題材研究員,根據結構化資料撰寫每日研究報告。

資料分三大類,報告要完整涵蓋這三塊,不能只挑一部分寫:
1. 發酵雷達(G/A/B/C區):核心買點邏輯,判斷「台股訊號×國際法說」雙邊確認、
   多源共振個股、籌碼動向——這是最高優先,要優先展開分析。
2. 題材雷達(D/F區):新聞熱度暴增題材、國際大廠法說關鍵詞升溫——次要展開。
3. 候選題材(E/E2區):還沒有把握、需要人工判斷的早期訊號,包含src4發現的
   全新詞(E區)跟既有追蹤詞裡開始有動能但還沒正式觸發的(E2區)——這塊要明確
   標註「初步觀察、非確定訊號」,不要講得太篤定,但仍要點名值得留意的幾個。

寫作原則:
1. 用繁體中文,語氣像研究員在跟熟悉市場的朋友做簡報,不是罐頭式條列數字。
2. 三大類都要有一段,依優先順序展開(發酵雷達>題材雷達>候選題材),
   但不用生硬地分「第一部分第二部分」,用自然的段落過渡銜接。
3. 交易哲學要貫穿全文:股價位置🟢底部的標的要優先展開、明確標出來是關注重點;
   🔴高檔的標的即使訊號很強,也要明確提醒「已大幅反映,不建議此時追價」,
   不能因為訊號強就淡化追高風險提醒。
4. E2區的「近期關注」要明確跟E區/D區區分語氣——這是「還在觀察階段」的訊號,
   暴增比還沒過門檻,用「值得留意但尚未確認」這種謹慎語氣,不要講得像已經是題材。
5. 「今日首見」的新題材要註明是初步發現、需要時間驗證;「連續追蹤中」天數
   越長的題材可信度越高,可以講得更肯定。
6. 不要重複列出所有原始資料,要做「解讀」——例如指出某題材同時出現在G區和
   D區,代表台股熱度和國際法說互相印證,這種跨區塊關聯才是報告的價值所在。
7. 篇幅約700-1000字(比純題材雷達的版本略長,因為現在要涵蓋三大類),
   結尾用一句話點出「今天最值得留意的訊號」。
8. 不涉及任何個人持股、帳戶、部位資訊——只分析市場公開資料本身。"""


def call_gemini(digest_text, date_str):
    """呼叫Gemini(免費層,複用你已有的GEMINI_API_KEY)。不用Search grounding
    (那個額度極小、容易429),純文字生成,一般額度充足(之前實測7/500)。"""
    if not GEMINI_KEY:
        return None, "找不到 GEMINI_API_KEY"
    prompt = f"今天是{date_str},以下是今日的題材雷達、發酵雷達、候選題材資料:\n\n{digest_text}\n\n請撰寫今日研究報告。"
    body = {
        "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.4, "maxOutputTokens": 2500},
    }
    try:
        r = requests.post(
            GEMINI_URL,
            headers={"Content-Type": "application/json", "x-goog-api-key": GEMINI_KEY},
            json=body, timeout=60,
        )
        if r.status_code != 200:
            return None, f"HTTP {r.status_code}: {r.text[:300]}"
        data = r.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        usage = data.get("usageMetadata", {})
        return text, usage
    except Exception as e:
        return None, str(e)


def main():
    now = dt.datetime.now()
    date_str = now.strftime("%Y年%m月%d日")
    print("=" * 60)
    print(f"每日題材研究報告生成  {date_str}")
    print("=" * 60)

    digest = load_json(DIGEST_FILE)
    tracker = load_json(TRACKER_FILE)
    if not digest:
        print("  ⚠ 找不到 daily_digest.json,無法產出報告")
        json.dump({"generated_at": str(now), "report": None, "error": "no digest"},
                  open(OUT_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        return

    digest_text = format_digest_for_prompt(digest, tracker)
    print(f"  已格式化資料摘要(涵蓋發酵雷達+題材雷達+候選題材),長度 {len(digest_text)} 字元")

    report_text, meta = call_gemini(digest_text, date_str)
    if report_text is None:
        print(f"  ⚠ 報告生成失敗: {meta}")
        json.dump({"generated_at": str(now), "report": None, "error": str(meta)},
                  open(OUT_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        return

    print(f"\n{'=' * 60}\n報告內容\n{'=' * 60}")
    print(report_text)
    print(f"\n{'=' * 60}")
    print(f"用量: {meta}")

    out = {
        "generated_at": str(now),
        "date": now.strftime("%Y%m%d"),
        "report": report_text,
        "model": GEMINI_MODEL,
        "usage": meta if isinstance(meta, dict) else None,
    }
    json.dump(out, open(OUT_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"\n→ {OUT_FILE} 已寫出")


if __name__ == "__main__":
    main()
