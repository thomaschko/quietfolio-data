# -*- coding: utf-8 -*-
"""
============================================================
term_stock_ai.py — D2 熱詞 → 個股(Google News 標題 + 免費 AI 引用,2026-10-16 新增)
============================================================
gnews_term_heat.py 抓到的非平淡詞(暴增 / 新出現 / 關注),每天最多 10 個,把命中標題
編號後交給免費 LLM(llm_client 依序備援),請它列出「標題中明確寫出名稱」的台灣上市櫃
公司並標註引用的標題編號。程式再逐筆驗證,AI 的話不直接採用:
  1. 名稱必須能在 TWSE/TPEx 股名對照(name2code)查到 → 查不到(外國公司、寫錯名)丟棄
  2. 引用的標題編號必須存在,且至少一則被引用的標題真的含該公司名稱或股號 → 否則丟棄
只是參考資訊,不進 A 區、不改任何門檻。AI 失敗就是沒有,不補、不猜。
============================================================
"""
import re
import time

MAX_AI_TERMS = 10
MAX_TITLES = 25
SLEEP = 4
ROLE_MAX = 20

SYSTEM = "你是台股研究助理。只根據使用者提供的新聞標題回答,不得使用標題以外的知識,不確定就不要列。"


def make_prompt(term, titles):
    lines = "\n".join(f"[{i}] {t}" for i, t in enumerate(titles, 1))
    return (f"題材詞:「{term}」\n新聞標題:\n{lines}\n\n"
            "列出標題中「明確寫出名稱」且與該題材相關的台灣上市櫃公司。只輸出 JSON:"
            '{"companies":[{"name":"標題中的公司名稱","title_ids":[標題編號],"role":"10字內角色"}]}'
            "。標題沒寫到的公司不要列;外國公司不要列;只是同一則標題順帶提到、與題材無關的公司不要列。")


def _clean_name(name):
    return re.sub(r"[\s*＊]|\(\d{4,6}\)|（\d{4,6}）", "", str(name or "")).strip()


def lookup_code(name, name2code):
    n = _clean_name(name)
    if not n:
        return None, n
    if n in name2code:
        return name2code[n], n
    for v in (n.replace("-KY", ""), n + "-KY", n + "*"):
        if v in name2code:
            return name2code[v], n
    # 2026-10-17 首次正式執行:「日月光」(官方簡稱「日月光投控」)、「攸泰」被誤判查無股名 →
    # 標題常用的是官方簡稱的前段;只有「以它開頭的官方名稱全部指向同一檔」才採用,有歧義就放棄
    if len(n) >= 2:
        codes = {c for k, c in name2code.items() if k.startswith(n)}
        if len(codes) == 1:
            return codes.pop(), n
    return None, n


def verify(ai_out, titles, name2code):
    """回傳 (kept, dropped)。kept: [{name, code, role, title_ids, evidence}];
    dropped: [{name, reason}]。同一股號只留第一筆。"""
    kept, dropped, seen = [], [], set()
    comps = (ai_out or {}).get("companies") if isinstance(ai_out, dict) else None
    for c in comps if isinstance(comps, list) else []:
        if not isinstance(c, dict):
            continue
        code, name = lookup_code(c.get("name"), name2code)
        if len(name) < 2:
            continue
        if not code:
            dropped.append({"name": name, "reason": "查無上市櫃股名"})
            continue
        ids = []
        for i in c.get("title_ids") or []:
            try:
                i = int(i)
            except (TypeError, ValueError):
                continue
            if 1 <= i <= len(titles) and i not in ids:
                ids.append(i)
        ok_ids = [i for i in ids if name in titles[i - 1] or code in titles[i - 1]]
        if not ok_ids:
            dropped.append({"name": name, "reason": "引用標題不含公司名"})
            continue
        if code in seen:
            continue
        seen.add(code)
        kept.append({"name": name, "code": code, "role": str(c.get("role") or "")[:ROLE_MAX],
                     "title_ids": ok_ids, "evidence": titles[ok_ids[0] - 1][:80]})
    return kept, dropped


def enrich(terms, titles_by_term, chain, name2code, max_terms=MAX_AI_TERMS, sleep=SLEEP):
    """在 terms(gnews_term_heat 的 term dict 清單)上就地加 ai_companies / ai_dropped / ai_provider。
    回傳本次摘要 dict(寫進 gnews_term_heat.json 的 ai_lookup)。"""
    if not chain.available:
        return {"skipped": "沒有任何 LLM API key"}
    if not name2code:
        return {"skipped": "股名對照載入失敗"}
    picked = [t for t in terms if t["status"] != "quiet"
              and len(titles_by_term.get(t["term"], [])) >= 2][:max_terms]
    meta = {"providers_available": chain.available, "attempted": 0, "ok": 0, "failed": 0,
            "by_provider": {}, "kept": 0, "dropped": 0}
    for n, t in enumerate(picked):
        titles = titles_by_term[t["term"]][:MAX_TITLES]
        meta["attempted"] += 1
        out, provider = chain.ask_json(SYSTEM, make_prompt(t["term"], titles))
        if out is None:
            meta["failed"] += 1
            t["ai_provider"] = None
        else:
            kept, dropped = verify(out, titles, name2code)
            t["ai_companies"], t["ai_dropped"], t["ai_provider"] = kept, dropped, provider
            meta["ok"] += 1
            meta["by_provider"][provider] = meta["by_provider"].get(provider, 0) + 1
            meta["kept"] += len(kept)
            meta["dropped"] += len(dropped)
        if n < len(picked) - 1:
            time.sleep(sleep)
    meta["errors"] = chain.log[:10]
    return meta
