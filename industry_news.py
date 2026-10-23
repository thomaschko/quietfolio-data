# -*- coding: utf-8 -*-
"""
============================================================
industry_news.py — 產業新聞日報 / 週報(2026-10-18 新增)
============================================================
用所有題材源整理成一份給人讀的產業新聞日報,每週六再彙整一份週報,輸出 JSON 到 industry_news/,
由 Quietfolio(GAS:IndustryNewsFetch.js → 網頁「市場 → 產業新聞」)讀 industry_news/latest.json 顯示。
題材源 = news_sources.fetch_titles_by_source 的 46 源(含 2026-10-18 新增的產業タイムズ社「電子デバイス産業新聞」)+ 同一次 workflow 稍早產出的偵測源資料:
MOPS 重訊主旨(event_theme_raw)、美股 8-K(us_8k_events)、國際法說/投資人日新聞
(investor_day_signals)、D2 產業術語熱詞的 Google News 標題(gnews_term_heat)。
2026-10-20 另加「只進產業新聞、不進雷達」的來源:未來半導體 fsemi.tech(fsemi_news.py,簡體標題轉台灣用語)、
電子產品世界 m.eccn.com「芯闻」(eccn_news.py;內容較雜,不列入 INDUSTRY_SOURCES,只收命中 watchlist 詞的標題)。

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
import glob
import datetime as dt

OUT_DIR = "industry_news"
WATCHLIST_FILE = "themes_watchlist.txt"
TPE = dt.timezone(dt.timedelta(hours=8))
MAX_TITLES = 750            # 給 Gemini(約 3 萬 tokens,flash-lite 可處理);2026-10-23 400 → 750(每天抓約 2,600 則,只用 400 則)
MAX_TITLES_SMALL = 60       # Gemini 失敗時給 Groq(每分鐘 8,000 tokens)
PER_SOURCE_MIN = 5          # 每個來源至少保留幾則(避免小來源被多源大新聞擠掉)
MAX_ITEMS_PER_SECTION = 14  # 2026-10-23 10 → 14
TARGET_ITEMS = "50~70"      # 10/18 首次實跑只寫 20 則 → 30~45;2026-10-23 再加大到 50~70
GEMINI_TIMEOUT = 180        # 標題變多、輸出變長,120 → 180 秒
# 2026-10-23:改成一天兩份(早報約台北 07:00、晚報約 18:00),由 industry-news.yml(GAS 準時觸發)產生;
# event-theme.yml 裡不帶參數的那次呼叫不再寫任何檔(避免兩個 workflow 同時改 latest.json 造成 push 衝突)
EDITIONS = {"am": "早報", "pm": "晚報"}
# 產業專業來源與偵測源:不含 watchlist 詞也可入選(交給 AI 判斷要不要收)
INDUSTRY_SOURCES = {
    "wapeople", "technews", "eetimes", "trendforce", "semianalysis", "jov", "iek_news", "technice",
    "eetimes_jp", "edn_jp", "sangyo_times", "etnews_kr", "digitaltoday_kr", "tomshardware",
    "techcrunch_semi", "the_circuit_podcast", "simpletechtrend", "wapeople_pod", "edn",
    "mops", "us_8k", "investor_events", "gnews_d2",
    "cnbc_yahoo",   # Yahoo Finance 半導體/AI 個股新聞(NVDA/TSM/AMD…),英文標題幾乎不含中文 watchlist 詞
    "fsemi",        # 2026-10-20 未來半導體(中國半導體產業快訊,只進產業新聞;fsemi_news.py)
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


def _fresh(doc, today, days=0):
    """generated_at 的日期在 today 往前 days 天內(含當天)。"""
    if not isinstance(doc, dict):
        return False
    d = str(doc.get("generated_at", ""))[:10]
    try:
        t = dt.date.fromisoformat(today)
        return any(d == (t - dt.timedelta(days=i)).isoformat() for i in range(days + 1))
    except ValueError:
        return d == today


def detector_sources(today, base=".", days=0):
    """event-theme workflow 產出的偵測源資料 → {來源: [標題]}。只用 days 天內產出的檔案(上游步驟
    失敗時檔案會是舊的,不拿舊資料充數)。早晚報不在同一次 workflow,用 days=1(昨晚的也收;
    已被上一份早晚報引用過的標題會再被濾掉)。"""
    def rd(name):
        try:
            with open(os.path.join(base, name), encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None
    out = {"mops": [], "us_8k": [], "investor_events": [], "gnews_d2": []}
    raw = rd("event_theme_raw.json")
    if _fresh(raw, today, days):
        for t in raw.get("themes", []):
            if t.get("source") == "MOPS重訊" and t.get("subject"):
                who = "、".join(f"{t['names'].get(c, '')}({c})" for c in t.get("codes", [])[:2])
                out["mops"].append(f"重訊 {who}:{t['subject']}")
    k8 = rd("us_8k_events.json")
    if _fresh(k8, today, days):
        for e in k8.get("events", []):
            out["us_8k"].append(f"美股8-K {e.get('code', '')}({e.get('role', '')}) {e.get('filing_date', '')}:"
                                + "、".join(e.get("item_labels") or []))
    inv = rd("investor_day_signals.json")
    if _fresh(inv, today, days):
        for e in inv.get("events", []):
            out["investor_events"] += [t for t in e.get("titles", []) if t]
    gh = rd("gnews_term_heat.json")
    if _fresh(gh, today, days):
        for t in gh.get("terms", []):
            if t.get("status") != "quiet":
                out["gnews_d2"] += t.get("titles", [])
    return out


# ============================================================
# 日報
# ============================================================
def daily_prompt(day, items, n_sources, label="日報"):
    lines = "\n".join(f"[{i}]({len(it['sources'])}源) {it['title']}" for i, it in enumerate(items, 1))
    return (f"以下是 {day} {label}從 {n_sources} 個來源收集的產業新聞標題(編號、出現來源數;含中、英、日、韓文,"
            f"以及台股重訊、美股 8-K 公告摘要):\n{lines}\n\n"
            f"請整理成產業新聞{label},只輸出 JSON:"
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
    try:
        from watchlist_suggester import FOREIGN_COMPANY_NAMES
    except Exception:
        FOREIGN_COMPANY_NAMES = {"海力士", "sk海力士"}
    # 10/18 第二次實跑:AI 仍把「SK海力士」標成 4923 力士(「力士」確實出現在標題裡)→
    # 比對前先把外國公司名(海力士、三星…)從標題挖掉,再看台灣公司名是否還在
    foreign = sorted((f for f in FOREIGN_COMPANY_NAMES if re.search(r"[一-鿿]", f)), key=len, reverse=True)

    def strip_foreign(t):
        for f in foreign:
            t = t.replace(f, "　")
        return t
    clean = [strip_foreign(t) for t in titles]
    out, seen = [], set()
    for n in names[:8]:
        code, name = lookup_code(n, name2code)
        if not code or code in seen or not any(name in t or code in t for t in clean):
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


def build_daily(day, by_source, terms, chain, name2code=None, code2name=None, edition=None, exclude=None):
    """edition:am/pm(早報/晚報)或 None(舊版一天一份);exclude:上一份早晚報已引用的標題(_norm 後),
    這份不再送給 AI,避免早晚報重複。"""
    pool = build_pool(by_source)
    n_excluded = 0
    if exclude:
        keep = [it for it in pool if _norm(it["title"]) not in exclude]
        n_excluded, pool = len(pool) - len(keep), keep
    items = select_relevant(pool, terms)
    label = EDITIONS.get(edition, "日報")
    doc = {"date": day, "edition": edition, "label": label, "n_excluded": n_excluded,
           "generated_at": dt.datetime.now(TPE).strftime("%Y-%m-%d %H:%M:%S"),
           "n_sources": sum(1 for v in by_source.values() if v), "n_sources_total": len(by_source),
           "n_titles": len(pool), "n_relevant": len(items), "provider": None, "fallback": False,
           "overview": "", "sections": [], "stats": {}}
    if not items:
        return doc
    out, provider, used_items = None, None, items
    if chain is not None and chain.available:
        out, provider = chain.ask_json(SYSTEM, daily_prompt(day, items, doc["n_sources"], label),
                                       only=("gemini",), timeout=GEMINI_TIMEOUT)
        if out is None:
            small = items[:MAX_TITLES_SMALL]
            out, provider = chain.ask_json(SYSTEM, daily_prompt(day, small, doc["n_sources"], label), timeout=90)
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
def daily_name(day, edition=None):
    return f"{day}-{edition}" if edition else day


def load_week(today, out_dir=OUT_DIR, days=WEEK_DAYS):
    """近 days 天的日報(含早晚報 YYYY-MM-DD-am/pm 與舊版 YYYY-MM-DD),舊到新。"""
    lo = (today - dt.timedelta(days=days - 1)).isoformat()
    docs = []
    for p in sorted(glob.glob(os.path.join(out_dir, "daily", "*.json"))):
        name = os.path.splitext(os.path.basename(p))[0]
        if not (lo <= name[:10] <= today.isoformat()):
            continue
        try:
            with open(p, encoding="utf-8") as f:
                docs.append(json.load(f))
        except Exception:
            continue
    return docs


def previous_cited(out_dir, current_name):
    """排在 current_name 之前最近一份日報(早晚報)引用過的標題(_norm 後)。"""
    names = [n for n in _archive(out_dir, "daily", 10) if n < current_name]
    if not names:
        return set()
    doc = _read_json(os.path.join(out_dir, "daily", f"{names[0]}.json")) or {}
    return {_norm(t.get("title")) for s in doc.get("sections", []) for it in s.get("items", [])
            for t in it.get("titles", []) if t.get("title")}


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
        out, _ = chain.ask_json("你是日翻中譯者。逐條翻成繁體中文,不增刪內容。公司名用台灣常用譯名;"
                                "台灣公司用台股慣用簡稱(例:ヴァンガード→世界先進、ホンハイ/鴻海精密→鴻海、"
                                "ASE→日月光、南亜プラスチック→南亞)。",
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
# 給 Quietfolio(GAS)讀的彙整檔
# ============================================================
LATEST_DAYS = 7


def _archive(out_dir, sub, n=30):
    # 用去掉副檔名的檔名排序:「2026-10-22.json」整個路徑比會排在「2026-10-22-pm.json」後面('.' > '-')
    names = sorted((os.path.splitext(os.path.basename(p))[0] for p in glob.glob(os.path.join(out_dir, sub, "*.json"))),
                   reverse=True)
    return names[:n]


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def build_latest(out_dir=OUT_DIR, days=LATEST_DAYS):
    """industry_news/latest.json:近 days 天的日報(含早晚報,新到舊)+ 最新週報 + 週報清單。
    GAS(IndustryNewsFetch.js)每小時讀這一個檔;只有最新一份日報保留來源覆蓋表(控制大小)。"""
    dailies, dates = [], []
    for i, d in enumerate(_archive(out_dir, "daily", days * 2 + 2)):
        if d[:10] not in dates:
            if len(dates) >= days:
                break
            dates.append(d[:10])
        doc = _read_json(os.path.join(out_dir, "daily", f"{d}.json"))
        if not doc:
            continue
        if i > 0:
            doc = {k: v for k, v in doc.items() if k != "coverage"}
        dailies.append(doc)
    weeks = _archive(out_dir, "weekly", 12)
    weekly = _read_json(os.path.join(out_dir, "weekly", f"{weeks[0]}.json")) if weeks else None
    return {"generated_at": dt.datetime.now(TPE).strftime("%Y-%m-%d %H:%M:%S"),
            "dailies": dailies, "weekly": weekly, "weeks": weeks}


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


# ============================================================
# main
# ============================================================
SANGYO_CACHE = os.path.join(OUT_DIR, "sangyo_latest.json")
SANGYO_MAX_AGE_DAYS = 9     # 週報最多用 9 天前出刊的那期(週四出刊 → 下週六仍可用)


def sangyo_for_weekly(today, fresh, cache_path=SANGYO_CACHE):
    """週報用的電子デバイス産業新聞頭條:現抓成功就用並更新快取;失敗就用每日執行時存下的快取
    (10/18 GHA 實測同一天 4 次有 2 次連線逾時,成功時 1~2 秒)。太舊(> 9 天)不用。"""
    if fresh:
        _write(cache_path, json.dumps(fresh, ensure_ascii=False))
        return fresh
    hl = _read_json(cache_path)
    try:
        if hl and (today - dt.date.fromisoformat(hl["issue_date"])).days <= SANGYO_MAX_AGE_DAYS:
            return hl
    except Exception:
        pass
    return None


def _arg(name):
    if name in sys.argv:
        i = sys.argv.index(name)
        return sys.argv[i + 1] if i + 1 < len(sys.argv) else None
    return None


def main():
    """--edition am|pm:早報/晚報(industry-news.yml,GAS 約台北 07:00 / 18:00 觸發);晚報那次在週六另產週報。
    --skip-if-exists:今天這一份已經有了就不重做(GitHub 延遲的 schedule 備援用)。
    不帶 --edition(event-theme.yml 那次):2026-10-23 起什麼都不做(industry_news/ 只由 industry-news.yml 寫)。
    --weekly:強制週報。--weekly-only:不抓新聞,只用 repo 裡現有日報重做週報(手動 workflow 用)。"""
    today = dt.datetime.now(TPE).date()
    weekly_only = "--weekly-only" in sys.argv
    force_weekly = weekly_only or "--weekly" in sys.argv
    edition = _arg("--edition")
    if edition is not None and edition not in EDITIONS:
        print(f"未知的 --edition:{edition}(只接受 am / pm)")
        sys.exit(2)
    name = daily_name(today.isoformat(), edition)
    if edition and "--skip-if-exists" in sys.argv and os.path.exists(os.path.join(OUT_DIR, "daily", f"{name}.json")):
        print(f"產業新聞 {name} 已存在,略過(--skip-if-exists)")
        return
    from news_sources import fetch_titles_by_source, fetch_sangyo_headlines
    from llm_client import LLMChain
    chain = LLMChain()
    print(f"產業新聞 {today} {EDITIONS.get(edition, '')}:可用 AI {chain.available or '無'}"
          + ("(只重做週報)" if weekly_only else ""))
    if edition and not weekly_only:
        name2code, code2name = {}, {}
        try:
            from event_theme_radar import build_name2code
            name2code, code2name = build_name2code()
        except Exception as e:
            print(f"  ⚠ 股名對照載入失敗(不標個股): {e}")
        by_source = fetch_titles_by_source()
        by_source.update(detector_sources(today.isoformat(), days=1))
        extra = {}
        try:                                          # 只進產業新聞的來源(不在雷達的 fetch_titles_by_source 裡)
            import fsemi_news
            fs_items, fs_status = fsemi_news.fetch(today)
            by_source["fsemi"] = fsemi_news.titles_for_pool(fs_items)
            extra["fsemi"] = {"status": fs_status, "n": len(fs_items)}
        except Exception as e:
            print(f"  ⚠ fsemi 來源失敗(略過): {e}")
            extra["fsemi"] = {"status": "failed", "n": 0}
        try:
            import eccn_news
            ec_items, ec_status = eccn_news.fetch()
            by_source["eccn"] = eccn_news.titles_for_pool(ec_items)
            extra["eccn"] = {"status": ec_status, "n": len(ec_items)}
        except Exception as e:
            print(f"  ⚠ eccn 來源失敗(略過): {e}")
            extra["eccn"] = {"status": "failed", "n": 0}
        terms = load_terms()
        exclude = previous_cited(OUT_DIR, name)
        daily = build_daily(today.isoformat(), by_source, terms, chain, name2code, code2name,
                            edition=edition, exclude=exclude)
        daily["extra_sources"] = extra
        _write(os.path.join(OUT_DIR, "daily", f"{name}.json"), json.dumps(daily, ensure_ascii=False, indent=1))
        print(f"  {name}:來源 {daily['n_sources']}/{daily['n_sources_total']}、標題 {daily['n_titles']}"
              f"(排除上一份已引用 {daily['n_excluded']})、相關 {daily['n_relevant']}、AI {daily['provider']}、"
              f"主題 {len(daily['sections'])}、fallback={daily['fallback']} {daily.get('stats')}")
    elif not weekly_only:
        # 不寫任何檔:industry_news/ 只由 industry-news.yml 寫,避免和 event-theme 的 commit 在 latest.json 衝突
        print("  不帶 --edition:日報改由 industry-news.yml(早報/晚報)產生,這次不寫任何檔")
        return

    sangyo = sangyo_for_weekly(today, fetch_sangyo_headlines())   # 每天都試著更新快取
    if force_weekly or (edition == "pm" and today.weekday() == WEEKLY_WEEKDAY):
        weekly = build_weekly(today, load_week(today), chain, sangyo)
        _write(os.path.join(OUT_DIR, "weekly", f"{weekly['week']}.json"), json.dumps(weekly, ensure_ascii=False, indent=1))
        print(f"  週報 {weekly['week']}:日報 {weekly['n_daily']} 份、條目 {weekly['n_items']}、主題 {len(weekly['themes'])}、"
              f"AI {weekly['provider']}、日本頭條 {'有' if weekly['sangyo'] else '無(抓取失敗且無快取)'}")
    _write(os.path.join(OUT_DIR, "latest.json"), json.dumps(build_latest(), ensure_ascii=False, separators=(",", ":")))
    if chain.log:
        print(f"  AI 錯誤紀錄:{chain.log[:6]}")
    print(f"→ {OUT_DIR}/ 已寫出")


if __name__ == "__main__":
    main()
