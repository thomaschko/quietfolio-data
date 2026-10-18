# -*- coding: utf-8 -*-
"""
============================================================
industry_news.py — 產業新聞日報 / 週報(2026-10-18 新增)
============================================================
用所有題材源整理成一份給人讀的產業新聞日報,每週六再彙整一份週報,輸出 JSON + 靜態 HTML
頁面到 industry_news/。題材源 = news_sources.fetch_titles_by_source 的 46 源(含 2026-10-18
新增的產業タイムズ社「電子デバイス産業新聞」)+ 同一次 workflow 稍早產出的偵測源資料:
MOPS 重訊主旨(event_theme_raw)、美股 8-K(us_8k_events)、國際法說/投資人日新聞
(investor_day_signals)、D2 產業術語熱詞的 Google News 標題(gnews_term_heat)。

流程(日報):
  1. 抓全部來源標題 → 同標題跨源合併(記錄來源)
  2. 入選條件:含 watchlist 詞,或來自產業專業來源 / 偵測源(INDUSTRY_SOURCES,日韓英文
     產業媒體標題多半不含中文 watchlist 詞,不能只靠詞比對);依「出現來源數、命中詞數」排序,
     每個來源先保證前 PER_SOURCE_MIN 則,再依排序補滿 MAX_TITLES 則
  3. 免費 LLM(llm_client;Gemini 讀全部,失敗才用 60 則縮短版給 Groq)
     分主題整理、外文翻成繁中,每則必須標註引用的標題編號
  4. 程式驗證:沒有有效引用編號的條目丟棄;每則附上原始標題與來源;AI 列的台灣公司名
     必須能在 TWSE/TPEx 股名表查到(term_stock_ai.lookup_code)且出現在被引用的標題裡才標股號
     (2026-10-18 首次實跑:改用程式子字串抽股號會把「SK海力士」標成 4923 力士、「至上游」
     標成 8112 至上、「5倍光罩尺寸」標成 2338 光罩)
  5. AI 全部失敗 → 退回「依 watchlist 詞分組的原始標題」,明確標示,不補不猜
週報(每週六台北時間的那次執行,或 --weekly):讀近 7 天日報條目請 LLM 彙整成本週主題,
  同樣驗證引用;另附電子デバイス産業新聞當期頭條(AI 翻譯,保留日文原文)。

只是閱讀用的參考頁面:不進雷達、不影響任何偵測門檻、A 區或日報(daily_digest)內容。
版權:只用標題與 AI 依標題寫的一句重點,不抓、不存全文;每則都列出原始標題與來源。
============================================================
"""
import os
import re
import sys
import json
import html
import glob
import datetime as dt

OUT_DIR = "industry_news"
WATCHLIST_FILE = "themes_watchlist.txt"
TPE = dt.timezone(dt.timedelta(hours=8))
MAX_TITLES = 400            # 給 Gemini(約 1.5 萬 tokens,flash-lite 可處理)
MAX_TITLES_SMALL = 60       # Gemini 失敗時給 Groq(每分鐘 8,000 tokens)
PER_SOURCE_MIN = 5          # 每個來源至少保留幾則(避免小來源被多源大新聞擠掉)
MAX_ITEMS_PER_SECTION = 10
TARGET_ITEMS = "30~45"      # 10/18 首次實跑只寫 20 則、引用 70/400 則標題,日韓媒體全沒被引用
# 產業專業來源與偵測源:不含 watchlist 詞也可入選(交給 AI 判斷要不要收)
INDUSTRY_SOURCES = {
    "wapeople", "technews", "eetimes", "trendforce", "semianalysis", "jov", "iek_news", "technice",
    "eetimes_jp", "edn_jp", "sangyo_times", "etnews_kr", "digitaltoday_kr", "tomshardware",
    "techcrunch_semi", "the_circuit_podcast", "simpletechtrend", "wapeople_pod", "edn",
    "mops", "us_8k", "investor_events", "gnews_d2",
    "cnbc_yahoo",   # Yahoo Finance 半導體/AI 個股新聞(NVDA/TSM/AMD…),英文標題幾乎不含中文 watchlist 詞
}
WEEKLY_WEEKDAY = 5          # 週六(Monday=0)
WEEK_DAYS = 7
FALLBACK_GROUPS = 12

SYSTEM = ("你是台灣科技產業新聞編輯。只根據使用者提供的新聞標題整理,不得加入標題以外的事實、數字、"
          "公司或推測;外文標題翻成繁體中文;不確定的標題寧可不收。")


# ============================================================
# 標題池
# ============================================================
def load_terms(path=WATCHLIST_FILE):
    try:
        with open(path, encoding="utf-8") as f:
            return [ln.strip() for ln in f if ln.strip() and not ln.lstrip().startswith("#")]
    except FileNotFoundError:
        return []


def term_regex(term):
    """中文標題用的比對:英文詞只要求「前後不是英文字母」(允許緊貼中文、數字,例:台積電CoWoS、HBM4);
    全大寫縮寫(2~6字母)大小寫須一致,其餘不分大小寫。"""
    flags = 0 if re.fullmatch(r"[A-Z]{2,6}", term) else re.IGNORECASE
    if re.search(r"[A-Za-z]", term):
        return re.compile(r"(?<![A-Za-z])" + re.escape(term) + r"(?![A-Za-z])", flags)
    return re.compile(re.escape(term), flags)


def _norm(t):
    return re.sub(r"\s+", "", t or "").lower()


def build_pool(by_source):
    """{來源: [標題]} → [{"title", "sources"}](同標題跨源合併,保留第一次出現的寫法)。"""
    pool, index = [], {}
    for src, titles in by_source.items():
        for t in titles or []:
            t = (t or "").strip()
            if len(t) < 6:
                continue
            k = _norm(t)
            if k in index:
                if src not in pool[index[k]]["sources"]:
                    pool[index[k]]["sources"].append(src)
                continue
            index[k] = len(pool)
            pool.append({"title": t, "sources": [src]})
    return pool


def select_relevant(pool, terms, limit=MAX_TITLES, per_source=PER_SOURCE_MIN):
    """入選:含 watchlist 詞,或任一來源屬 INDUSTRY_SOURCES。每個來源先保留排序最前的
    per_source 則,再依整體排序補滿 limit;輸出維持整體排序。"""
    matchers = [(t, term_regex(t)) for t in terms]
    eligible = []
    for it in pool:
        hit = [t for t, rx in matchers if rx.search(it["title"])]
        if hit or INDUSTRY_SOURCES.intersection(it["sources"]):
            eligible.append({**it, "terms": hit})
    eligible.sort(key=lambda x: (-len(x["sources"]), -len(x["terms"])))
    chosen, per = set(), {}
    for i, it in enumerate(eligible):
        if any(per.get(s, 0) < per_source for s in it["sources"]):
            chosen.add(i)
            for s in it["sources"]:
                per[s] = per.get(s, 0) + 1
    for i in range(len(eligible)):
        if len(chosen) >= limit:
            break
        chosen.add(i)
    return [eligible[i] for i in sorted(chosen)[:limit]]


def source_coverage(by_source, items, sections=None):
    """每個來源:抓到幾則 / 送 AI 幾則 / 被日報引用幾則。"""
    cited = {}
    for sec in sections or []:
        for it in sec.get("items", []):
            for t in it.get("titles", []):
                for s in t.get("sources", []):
                    cited[s] = cited.get(s, 0) + 1
    sent = {}
    for it in items:
        for s in it["sources"]:
            sent[s] = sent.get(s, 0) + 1
    return [{"source": s, "fetched": len(v or []), "sent": sent.get(s, 0), "cited": cited.get(s, 0)}
            for s, v in by_source.items()]


def _fresh(doc, today):
    return isinstance(doc, dict) and str(doc.get("generated_at", ""))[:10] == today


def detector_sources(today, base="."):
    """同一次 workflow 稍早產出的偵測源資料 → {來源: [標題]}。只用今天產出的檔案(上游步驟
    失敗時檔案會是舊的,不拿舊資料充數)。"""
    def rd(name):
        try:
            with open(os.path.join(base, name), encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None
    out = {"mops": [], "us_8k": [], "investor_events": [], "gnews_d2": []}
    raw = rd("event_theme_raw.json")
    if _fresh(raw, today):
        for t in raw.get("themes", []):
            if t.get("source") == "MOPS重訊" and t.get("subject"):
                who = "、".join(f"{t['names'].get(c, '')}({c})" for c in t.get("codes", [])[:2])
                out["mops"].append(f"重訊 {who}:{t['subject']}")
    k8 = rd("us_8k_events.json")
    if _fresh(k8, today):
        for e in k8.get("events", []):
            out["us_8k"].append(f"美股8-K {e.get('code', '')}({e.get('role', '')}) {e.get('filing_date', '')}:"
                                + "、".join(e.get("item_labels") or []))
    inv = rd("investor_day_signals.json")
    if _fresh(inv, today):
        for e in inv.get("events", []):
            out["investor_events"] += [t for t in e.get("titles", []) if t]
    gh = rd("gnews_term_heat.json")
    if _fresh(gh, today):
        for t in gh.get("terms", []):
            if t.get("status") != "quiet":
                out["gnews_d2"] += t.get("titles", [])
    return out


# ============================================================
# 日報
# ============================================================
def daily_prompt(day, items, n_sources):
    lines = "\n".join(f"[{i}]({len(it['sources'])}源) {it['title']}" for i, it in enumerate(items, 1))
    return (f"以下是 {day} 從 {n_sources} 個來源收集的產業新聞標題(編號、出現來源數;含中、英、日、韓文,"
            f"以及台股重訊、美股 8-K 公告摘要):\n{lines}\n\n"
            "請整理成產業新聞日報,只輸出 JSON:"
            '{"overview":"3句內的今日產業重點","sections":[{"topic":"產業主題","items":'
            '[{"headline":"繁中一句話重點(40字內)","title_ids":[標題編號],'
            '"companies":["標題中明確寫出的台灣上市櫃公司名,沒有就空陣列"]}]}]}。\n'
            "規則:主題用 AI伺服器/半導體製程/先進封裝/記憶體/設備材料/光通訊/電源散熱/PCB載板/"
            "車用功率/消費電子/日韓產業/美股與國際/總經政策/其他 這類產業分類;每則 item 必須引用至少一個標題編號;"
            f"同一事件的多則標題合併成一則並列出全部編號;每個主題最多 {MAX_ITEMS_PER_SECTION} 則;"
            f"總共整理 {TARGET_ITEMS} 則左右,要涵蓋不同來源:日文、韓文、英文產業媒體的重要新聞也要收"
            "(翻成繁中),不要只收中文;外國公司(SK海力士、三星、美光、輝達等)不要放進 companies;"
            "不重要或與產業無關的標題可以不收;headline 只能寫標題裡有的內容。")


def _valid_ids(ids, n):
    out = []
    for i in ids or []:
        try:
            i = int(i)
        except (TypeError, ValueError):
            continue
        if 1 <= i <= n and i not in out:
            out.append(i)
    return out


def _verified_companies(names, titles, name2code, code2name):
    """AI 列的公司名 → 股號;名稱或股號必須出現在被引用的標題裡。"""
    if not name2code or not isinstance(names, list):
        return []
    from term_stock_ai import lookup_code
    out, seen = [], set()
    for n in names[:8]:
        code, name = lookup_code(n, name2code)
        if not code or code in seen or not any(name in t or code in t for t in titles):
            continue
        seen.add(code)
        out.append({"code": code, "name": (code2name or {}).get(code, name)})
    return out[:5]


def verify_daily(ai_out, items, name2code=None, code2name=None):
    """AI 輸出 → 驗證後的 sections。回傳 (sections, stats)。"""
    n = len(items)
    sections, dropped, used = [], 0, set()
    for sec in (ai_out or {}).get("sections") or [] if isinstance(ai_out, dict) else []:
        if not isinstance(sec, dict):
            continue
        kept = []
        for it in sec.get("items") or []:
            if not isinstance(it, dict):
                dropped += 1
                continue
            ids = _valid_ids(it.get("title_ids"), n)
            headline = str(it.get("headline") or "").strip()[:80]
            if not ids or not headline:
                dropped += 1
                continue
            used.update(ids)
            src = [items[i - 1] for i in ids]
            titles = [s["title"] for s in src]
            kept.append({"headline": headline,
                         "titles": [{"title": s["title"], "sources": s["sources"]} for s in src],
                         "n_sources": len({x for s in src for x in s["sources"]}),
                         "codes": _verified_companies(it.get("companies"), titles, name2code, code2name)})
            if len(kept) >= MAX_ITEMS_PER_SECTION:
                break
        if kept:
            sections.append({"topic": str(sec.get("topic") or "其他").strip()[:20], "items": kept})
    overview = str((ai_out or {}).get("overview") or "").strip()[:300] if isinstance(ai_out, dict) else ""
    return sections, {"items_kept": sum(len(s["items"]) for s in sections), "items_dropped": dropped,
                      "titles_used": len(used), "overview": overview}


def fallback_daily(items, groups=FALLBACK_GROUPS):
    """AI 全失敗:依命中 watchlist 詞分組列原始標題(每則標題只放在命中的第一個詞)。"""
    by_term = {}
    for it in items:
        key = it["terms"][0] if it.get("terms") else f"來源:{it['sources'][0]}"
        by_term.setdefault(key, []).append(it)
    top = sorted(by_term.items(), key=lambda kv: -sum(len(x["sources"]) for x in kv[1]))[:groups]
    return [{"topic": term, "items": [{"headline": x["title"], "titles": [{"title": x["title"], "sources": x["sources"]}],
                                       "n_sources": len(x["sources"]), "codes": []} for x in lst[:MAX_ITEMS_PER_SECTION]]}
            for term, lst in top]


def build_daily(day, by_source, terms, chain, name2code=None, code2name=None):
    pool = build_pool(by_source)
    items = select_relevant(pool, terms)
    doc = {"date": day, "generated_at": dt.datetime.now(TPE).strftime("%Y-%m-%d %H:%M:%S"),
           "n_sources": sum(1 for v in by_source.values() if v), "n_sources_total": len(by_source),
           "n_titles": len(pool), "n_relevant": len(items), "provider": None, "fallback": False,
           "overview": "", "sections": [], "stats": {}}
    if not items:
        return doc
    out, provider, used_items = None, None, items
    if chain is not None and chain.available:
        out, provider = chain.ask_json(SYSTEM, daily_prompt(day, items, doc["n_sources"]),
                                       only=("gemini",), timeout=120)
        if out is None:
            small = items[:MAX_TITLES_SMALL]
            out, provider = chain.ask_json(SYSTEM, daily_prompt(day, small, doc["n_sources"]), timeout=90)
            used_items = small
    if out is not None:
        sections, stats = verify_daily(out, used_items, name2code, code2name)
        if sections:
            doc.update(provider=provider, sections=sections, overview=stats.pop("overview"), stats=stats)
            doc["stats"]["titles_sent"] = len(used_items)
            doc["coverage"] = source_coverage(by_source, used_items, sections)
            return doc
    doc.update(fallback=True, sections=fallback_daily(items),
               stats={"reason": "AI 失敗或輸出無有效引用", "errors": (chain.log[:5] if chain else [])})
    doc["coverage"] = source_coverage(by_source, items, doc["sections"])
    return doc


# ============================================================
# 週報
# ============================================================
def load_week(today, out_dir=OUT_DIR, days=WEEK_DAYS):
    docs = []
    for i in range(days - 1, -1, -1):
        d = (today - dt.timedelta(days=i)).isoformat()
        p = os.path.join(out_dir, "daily", f"{d}.json")
        try:
            with open(p, encoding="utf-8") as f:
                docs.append(json.load(f))
        except Exception:
            continue
    return docs


def week_items(daily_docs):
    """近 7 天日報的條目攤平(只取 AI 版;fallback 日的原始標題也收,但標註)。"""
    out = []
    for doc in daily_docs:
        for sec in doc.get("sections", []):
            for it in sec.get("items", []):
                out.append({"date": doc["date"], "topic": sec["topic"], "headline": it["headline"],
                            "n_sources": it.get("n_sources", 1), "titles": it.get("titles", []),
                            "codes": it.get("codes", [])})
    out.sort(key=lambda x: -x["n_sources"])
    return out[:300]


def weekly_prompt(label, items):
    lines = "\n".join(f"[{i}] {it['date'][5:]} {it['topic']}|{it['headline']}" for i, it in enumerate(items, 1))
    return (f"以下是 {label} 一週產業新聞日報的條目(編號、日期、主題|重點):\n{lines}\n\n"
            "請彙整成產業新聞週報,只輸出 JSON:"
            '{"overview":"3~5句本週產業重點","themes":[{"topic":"本週主題","summary":"2句內,只能根據條目",'
            '"item_ids":[條目編號]}]}。規則:5~10 個主題,依重要性排序;每個主題至少引用 2 個條目編號'
            "(只有 1 則但很重要也可);不得寫出條目以外的事實或數字。")


def verify_weekly(ai_out, items):
    n = len(items)
    themes = []
    for th in (ai_out or {}).get("themes") or [] if isinstance(ai_out, dict) else []:
        if not isinstance(th, dict):
            continue
        ids = _valid_ids(th.get("item_ids"), n)
        if not ids:
            continue
        refs = [items[i - 1] for i in ids]
        codes, seen = [], set()
        for r in refs:
            for c in r.get("codes", []):
                if c["code"] not in seen:
                    seen.add(c["code"])
                    codes.append(c)
        themes.append({"topic": str(th.get("topic") or "").strip()[:24] or "未命名",
                       "summary": str(th.get("summary") or "").strip()[:200],
                       "items": [{"date": r["date"], "headline": r["headline"], "titles": r["titles"]} for r in refs],
                       "codes": codes[:8]})
    overview = str((ai_out or {}).get("overview") or "").strip()[:500] if isinstance(ai_out, dict) else ""
    return themes, overview


def translate_sangyo(hl, chain):
    """電子デバイス産業新聞頭條 → 逐條繁中翻譯(依編號對回,對不上的條目只留日文)。"""
    flat = [hl["top"]] + [t for s in hl["sections"] for t in s["items"]]
    zh = {}
    if chain is not None and chain.available:
        lines = "\n".join(f"[{i}] {t}" for i, t in enumerate(flat, 1))
        out, _ = chain.ask_json("你是日翻中譯者。逐條翻成繁體中文,公司名用台灣常用譯名,不增刪內容。",
                                f"{lines}\n\n只輸出 JSON:{{\"items\":[{{\"id\":編號,\"zh\":\"譯文\"}}]}}")
        for it in (out or {}).get("items") or [] if isinstance(out, dict) else []:
            try:
                i = int(it.get("id"))
            except (TypeError, ValueError, AttributeError):
                continue
            if 1 <= i <= len(flat) and it.get("zh"):
                zh[i] = str(it["zh"]).strip()[:120]
    k, sections = 2, []
    for s in hl["sections"]:
        rows = []
        for t in s["items"]:
            rows.append({"ja": t, "zh": zh.get(k, "")})
            k += 1
        sections.append({"name": s["name"], "items": rows})
    return {"issue_date": hl["issue_date"], "issue_no": hl["issue_no"], "url": hl.get("url", ""),
            "top": {"ja": hl["top"], "zh": zh.get(1, "")}, "sections": sections,
            "translated": len(zh), "total": len(flat)}


def week_label(today):
    y, w, _ = today.isocalendar()
    return f"{y}-W{w:02d}"


def build_weekly(today, daily_docs, chain, sangyo=None):
    items = week_items(daily_docs)
    start = (today - dt.timedelta(days=WEEK_DAYS - 1)).isoformat()
    doc = {"week": week_label(today), "range": [start, today.isoformat()],
           "generated_at": dt.datetime.now(TPE).strftime("%Y-%m-%d %H:%M:%S"),
           "n_daily": len(daily_docs), "n_items": len(items), "provider": None,
           "overview": "", "themes": [], "fallback": False, "sangyo": None}
    if items:
        out, provider = (None, None)
        if chain is not None and chain.available:
            out, provider = chain.ask_json(SYSTEM, weekly_prompt(f"{start}~{today.isoformat()}", items),
                                           only=("gemini",), timeout=120)
            if out is None:
                out, provider = chain.ask_json(SYSTEM, weekly_prompt(f"{start}~{today.isoformat()}", items[:60]),
                                               timeout=90)
                items = items[:60] if out is not None else items
        themes, overview = verify_weekly(out, items) if out is not None else ([], "")
        if themes:
            doc.update(provider=provider, themes=themes, overview=overview)
        else:   # 退回:列出本週出現來源數最多的條目
            doc.update(fallback=True, themes=[{"topic": "本週多源條目(AI 失敗,未彙整)", "summary": "",
                                               "items": [{"date": x["date"], "headline": x["headline"],
                                                          "titles": x["titles"]} for x in items[:20]],
                                               "codes": []}])
    if sangyo:
        doc["sangyo"] = translate_sangyo(sangyo, chain)
    return doc


# ============================================================
# HTML
# ============================================================
CSS = """
:root{--bg:#fbfaf8;--fg:#1f2328;--muted:#6a6f76;--card:#ffffff;--line:#e4e1dc;--accent:#2f6f9f;--chip:#eef3f7}
@media (prefers-color-scheme:dark){:root{--bg:#16181b;--fg:#e6e6e3;--muted:#9aa0a6;--card:#1f2226;--line:#30343a;--accent:#7fb3da;--chip:#26303a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.65 -apple-system,"PingFang TC","Noto Sans TC","Microsoft JhengHei",sans-serif}
main{max-width:860px;margin:0 auto;padding:20px 16px 60px}h1{font-size:22px;margin:0 0 4px}
h2{font-size:18px;margin:28px 0 10px;padding-bottom:6px;border-bottom:1px solid var(--line)}
h3{font-size:15px;margin:18px 0 6px;color:var(--accent)}.meta{color:var(--muted);font-size:13px}
nav a{margin-right:14px;color:var(--accent);text-decoration:none}.ov{background:var(--card);border:1px solid var(--line);
border-radius:8px;padding:12px 14px;margin:14px 0}ul{padding-left:18px;margin:6px 0}li{margin:6px 0}
details{color:var(--muted);font-size:13px}summary{cursor:pointer}.chip{display:inline-block;background:var(--chip);
border-radius:10px;padding:0 7px;margin:0 4px 0 0;font-size:12px;color:var(--fg)}.warn{color:#b26a00}
.ja{color:var(--muted);font-size:13px}a{color:var(--accent)}
table.cov{border-collapse:collapse;margin:8px 0;font-size:13px}table.cov td,table.cov th{border-bottom:1px solid var(--line);
padding:2px 10px 2px 0;text-align:left}
"""


def _e(s):
    return html.escape(str(s or ""))


def _titles_html(titles):
    rows = "".join(f"<div>{_e(t['title'])} <span class='meta'>— {_e('、'.join(t.get('sources', [])))}</span></div>"
                   for t in titles)
    return f"<details><summary>原始標題 {len(titles)} 則</summary>{rows}</details>"


def _codes_html(codes):
    return "".join(f"<span class='chip'>{_e(c['code'])}{_e(c.get('name', ''))}</span>" for c in codes)


def page(title, body, nav=""):
    return (f"<!doctype html><html lang='zh-Hant'><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'><title>{_e(title)}</title>"
            f"<style>{CSS}</style></head><body><main>{nav}{body}"
            "<p class='meta' style='margin-top:40px'>只依各來源公開標題整理;AI 重點僅供閱讀參考,"
            "請以原始標題與原文為準。不構成投資建議。</p></main></body></html>")


def daily_body(doc):
    head = (f"<h1>產業新聞日報 {_e(doc['date'])}</h1><p class='meta'>{doc['n_sources']}/{doc['n_sources_total']} 個來源有資料"
            f" · 標題 {doc['n_titles']} 則 · 產業相關 {doc['n_relevant']} 則"
            + (f" · AI:{_e(doc['provider'])}" if doc.get("provider") else "") + f" · 產生於 {_e(doc['generated_at'])}</p>")
    if doc.get("fallback"):
        head += "<p class='warn'>⚠ AI 整理失敗,以下是依 watchlist 詞分組的原始標題,未經整理。</p>"
    if doc.get("overview"):
        head += f"<div class='ov'><b>今日重點(AI 依標題摘要)</b><br>{_e(doc['overview'])}</div>"
    body = ""
    for sec in doc.get("sections", []):
        lis = "".join(f"<li>{_e(it['headline'])} <span class='meta'>({it['n_sources']}源)</span> {_codes_html(it['codes'])}"
                      f"{_titles_html(it['titles'])}</li>" for it in sec["items"])
        body += f"<h3>{_e(sec['topic'])}</h3><ul>{lis}</ul>"
    if not doc.get("sections"):
        body = "<p class='meta'>今天沒有產業相關標題(或全部來源抓取失敗)。</p>"
    cov = doc.get("coverage") or []
    if cov:
        rows = "".join(f"<tr><td>{_e(c['source'])}</td><td>{c['fetched']}</td><td>{c['sent']}</td><td>{c['cited']}</td></tr>"
                       for c in sorted(cov, key=lambda c: (-c["cited"], -c["sent"], -c["fetched"])))
        zero = [c["source"] for c in cov if not c["fetched"]]
        body += ("<details style='margin-top:24px'><summary>來源覆蓋:"
                 f"{sum(1 for c in cov if c['fetched'])}/{len(cov)} 個來源有標題、"
                 f"{sum(1 for c in cov if c['cited'])} 個被引用</summary>"
                 "<table class='cov'><tr><th>來源</th><th>抓到</th><th>送 AI</th><th>被引用</th></tr>"
                 f"{rows}</table>" + (f"<p>今天沒抓到:{_e('、'.join(zero))}</p>" if zero else "") + "</details>")
    return head + body


def render_daily(doc, nav=""):
    return page(f"產業新聞日報 {doc['date']}", daily_body(doc), nav)


def render_weekly(doc, nav=""):
    head = (f"<h1>產業新聞週報 {_e(doc['week'])}</h1><p class='meta'>{_e(doc['range'][0])} ~ {_e(doc['range'][1])}"
            f" · {doc['n_daily']} 份日報 · {doc['n_items']} 則條目" + (f" · AI:{_e(doc['provider'])}" if doc.get("provider") else "")
            + "</p>")
    if doc.get("fallback"):
        head += "<p class='warn'>⚠ AI 彙整失敗,以下只列出本週出現來源數最多的條目。</p>"
    if doc.get("overview"):
        head += f"<div class='ov'><b>本週重點(AI 依日報條目摘要)</b><br>{_e(doc['overview'])}</div>"
    body = ""
    for th in doc.get("themes", []):
        lis = "".join(f"<li><span class='meta'>{_e(it['date'][5:])}</span> {_e(it['headline'])}{_titles_html(it['titles'])}</li>"
                      for it in th["items"])
        body += (f"<h3>{_e(th['topic'])}</h3>" + (f"<p>{_e(th['summary'])}</p>" if th.get("summary") else "")
                 + (f"<p>{_codes_html(th['codes'])}</p>" if th.get("codes") else "") + f"<ul>{lis}</ul>")
    sg = doc.get("sangyo")
    if sg:
        body += (f"<h2>日本:電子デバイス産業新聞 {_e(sg['issue_date'])}(第{sg['issue_no']}號)頭條</h2>"
                 f"<p class='meta'>AI 翻譯 {sg['translated']}/{sg['total']} 則 · <a href='{_e(sg['url'])}'>原頁</a></p>"
                 f"<p><b>{_e(sg['top']['zh'] or sg['top']['ja'])}</b><br><span class='ja'>{_e(sg['top']['ja'])}</span></p>")
        for s in sg["sections"]:
            lis = "".join(f"<li>{_e(r['zh'] or r['ja'])}" + (f"<br><span class='ja'>{_e(r['ja'])}</span>" if r["zh"] else "")
                          + "</li>" for r in s["items"])
            body += f"<h3>{_e(s['name'])}</h3><ul>{lis}</ul>"
    return page(f"產業新聞週報 {doc['week']}", head + body, nav)


def _archive(out_dir, sub, n=30):
    files = sorted(glob.glob(os.path.join(out_dir, sub, "*.json")), reverse=True)[:n]
    return [os.path.splitext(os.path.basename(p))[0] for p in files]


def render_index(out_dir, daily_doc, weekly_doc):
    nav = "<nav><a href='#daily'>最新日報</a><a href='#weekly'>最新週報</a><a href='#archive'>歷史</a></nav>"
    body = "<h1>產業新聞</h1><p class='meta'>每天盤後更新日報;每週六更新週報。</p>"
    if daily_doc:
        body += "<h2 id='daily'>最新日報</h2>" + daily_body(daily_doc).replace("<h1>", "<h1 style='font-size:18px'>", 1)
    if weekly_doc:
        body += (f"<h2 id='weekly'>最新週報 {_e(weekly_doc['week'])}</h2>"
                 f"<p><a href='weekly/{_e(weekly_doc['week'])}.html'>開啟 {_e(weekly_doc['week'])} 週報 →</a></p>"
                 + (f"<div class='ov'>{_e(weekly_doc['overview'])}</div>" if weekly_doc.get("overview") else ""))
    days = _archive(out_dir, "daily")
    weeks = _archive(out_dir, "weekly", 12)
    body += ("<h2 id='archive'>歷史</h2><p><b>週報</b>:" + " · ".join(f"<a href='weekly/{w}.html'>{w}</a>" for w in weeks)
             + "</p><p><b>日報</b>:" + " · ".join(f"<a href='daily/{d}.html'>{d[5:]}</a>" for d in days) + "</p>")
    return page("產業新聞", body, nav)


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


# ============================================================
# main
# ============================================================
def main():
    today = dt.datetime.now(TPE).date()
    force_weekly = "--weekly" in sys.argv
    from news_sources import fetch_titles_by_source, fetch_sangyo_headlines
    from llm_client import LLMChain
    chain = LLMChain()
    name2code, code2name = {}, {}
    try:
        from event_theme_radar import build_name2code
        name2code, code2name = build_name2code()
    except Exception as e:
        print(f"  ⚠ 股名對照載入失敗(不標個股): {e}")
    print(f"產業新聞日報 {today}:可用 AI {chain.available or '無'}")
    by_source = fetch_titles_by_source()
    by_source.update(detector_sources(today.isoformat()))
    terms = load_terms()
    daily = build_daily(today.isoformat(), by_source, terms, chain, name2code, code2name)
    _write(os.path.join(OUT_DIR, "daily", f"{daily['date']}.json"), json.dumps(daily, ensure_ascii=False, indent=1))
    back = "<nav><a href='../index.html'>← 產業新聞首頁</a></nav>"
    _write(os.path.join(OUT_DIR, "daily", f"{daily['date']}.html"), render_daily(daily, back))
    print(f"  來源 {daily['n_sources']}/{daily['n_sources_total']}、標題 {daily['n_titles']}、相關 {daily['n_relevant']}、"
          f"AI {daily['provider']}、主題 {len(daily['sections'])}、fallback={daily['fallback']} {daily.get('stats')}")

    weekly = None
    if force_weekly or today.weekday() == WEEKLY_WEEKDAY:
        sangyo = fetch_sangyo_headlines()
        weekly = build_weekly(today, load_week(today), chain, sangyo)
        _write(os.path.join(OUT_DIR, "weekly", f"{weekly['week']}.json"), json.dumps(weekly, ensure_ascii=False, indent=1))
        _write(os.path.join(OUT_DIR, "weekly", f"{weekly['week']}.html"), render_weekly(weekly, back))
        print(f"  週報 {weekly['week']}:日報 {weekly['n_daily']} 份、條目 {weekly['n_items']}、主題 {len(weekly['themes'])}、"
              f"AI {weekly['provider']}、日本頭條 {'有' if weekly['sangyo'] else '無(抓取失敗)'}")
    else:
        weeks = _archive(OUT_DIR, "weekly", 1)
        weekly = _read_json(os.path.join(OUT_DIR, "weekly", f"{weeks[0]}.json")) if weeks else None
    _write(os.path.join(OUT_DIR, "index.html"), render_index(OUT_DIR, daily, weekly))
    if chain.log:
        print(f"  AI 錯誤紀錄:{chain.log[:6]}")
    print(f"→ {OUT_DIR}/ 已寫出")


if __name__ == "__main__":
    main()
