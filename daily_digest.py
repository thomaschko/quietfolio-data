# -*- coding: utf-8 -*-
"""
============================================================
daily_digest.py — 每日摘要整合器
============================================================
把所有偵測源的輸出整合成一份「每天早上快速掃過」的摘要。

輸入(各源產出,存在才讀,缺了不報錯):
  event_theme_raw.json     src1鉅亨暴增 + src2 MOPS + src3維基未發酵
  new_theme_candidates.json src4 新題材發現
  broker_coverage.json     src5 券商覆蓋率暴增(GAS產出,或Drive同步)

核心設計:以「個股」為中心做交叉聚合。
  同一檔股票被越多源命中 → 訊號越強(multi-source resonance)。
  這是整套系統的價值所在:單源易雜訊,多源共振才是高品質訊號。

輸出:daily_digest.json —— 分區呈現:
  A. 多源共振個股(被2+源命中,最高優先)
  B. 未發酵題材(src3維基,發酵前緣)
  C. 券商覆蓋暴增(src5,機構領先)
  D. 熱度暴增題材(src1)
  E. 新題材候選(src4,參考)
============================================================
"""

import json
import datetime as dt
from collections import defaultdict

from theme_alias_groups import canonical_theme


def load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def build_code2name(event, newtheme, tracker):
    """2026-10-06新增:彙整一份全域code->name對照表,供A區(多源共振)標名稱用。
    event_theme_radar.py自己建的code2name只在該腳本內部用,沒有存進
    event_theme_raw.json頂層;但每個題材/候選項目自己已經帶了{code:name}
    (names/related_names),這裡把各來源已經抓到的名稱全部彙整起來重用,
    不必為了補名稱另外打一次TWSE/TPEx API。後出現的來源會覆蓋先出現的
    空字串(例如某來源查無名稱但另一來源有),盡量填滿。"""
    code2name = {}
    for t in event.get("themes", []):
        for cd, nm in (t.get("names") or {}).items():
            if nm and not code2name.get(cd):
                code2name[cd] = nm
    for c in newtheme.get("candidates", []):
        for cd, nm in (c.get("related_names") or c.get("names") or {}).items():
            if nm and not code2name.get(cd):
                code2name[cd] = nm
    for bucket in ("first_seen", "ongoing"):
        for e in tracker.get(bucket, []):
            for cd, nm in (e.get("names") or {}).items():
                if nm and not code2name.get(cd):
                    code2name[cd] = nm
    return code2name


EVIDENCE_LABELS = {
    "keyword_surge": "台股新聞",
    "mops": "公開資訊觀測站",
    "wiki": "維基",
    "earnings": "國際法說",
    "intl_news": "國際新聞",
    "ai_extraction": "AI萃取",
}


def build_theme_evidence(event, earnings_rising, intl_news_heat, tracker, kw_to_theme):
    """2026-10-06新增:每個題材(正規化後)背後有哪幾類獨立來源佐證,分級:
    單一來源/雙來源/三源以上。只看「來源類別」不看「來源數量」——同一類別
    (例如src1關鍵字暴增)命中好幾次還是算一種來源,不能靠同一種偵測機制
    重複出現就灌水成多來源(這正是item1要修的同一種問題,這裡從一開始
    就用類別去重,不會重蹈覆轍)。
    回傳 {canonical_theme: {"sources": [標籤,...], "level": "單一來源"/"雙來源"/"三源以上"}}
    """
    evidence = defaultdict(set)

    for t in event.get("themes", []):
        canon = canonical_theme(t["theme"])
        src = t.get("source", "")
        is_keyword_src = ("暴增" in src) or ("關鍵字" in src) or (src == "cnyes")
        if t.get("wiki_stage"):
            evidence[canon].add("wiki")
        elif src == "MOPS重訊" or src.startswith("重訊"):
            evidence[canon].add("mops")
        elif is_keyword_src:
            evidence[canon].add("keyword_surge")

    for r in earnings_rising:
        theme = kw_to_theme.get(r.get("keyword"))
        if theme:
            evidence[canonical_theme(theme)].add("earnings")

    for h in intl_news_heat:
        theme = kw_to_theme.get(h.get("keyword"))
        if theme:
            evidence[canonical_theme(theme)].add("intl_news")

    for bucket in ("first_seen", "ongoing"):
        for e in tracker.get(bucket, []):
            method = e.get("method", "")
            if ("ai" in method or "search" in method) and e.get("term"):
                evidence[canonical_theme(e["term"])].add("ai_extraction")

    result = {}
    for canon, cats in evidence.items():
        n = len(cats)
        level = "單一來源" if n <= 1 else ("雙來源" if n == 2 else "三源以上")
        result[canon] = {
            "sources": sorted(EVIDENCE_LABELS.get(c, c) for c in cats),
            "level": level,
        }
    return result


def build_digest():
    now = dt.datetime.now()

    # ── 讀各源 ──
    event = load_json("event_theme_raw.json") or {}
    newtheme = load_json("new_theme_candidates.json") or {}
    broker = load_json("broker_coverage.json") or {}
    earnings = load_json("earnings_keywords.json") or {}  # src6
    tracker = load_json("theme_tracker.json") or {}  # 題材追蹤(AI語意+jieba整合,含首見/追蹤中)
    code2name = build_code2name(event, newtheme, tracker)  # 2026-10-06新增

    # 2026-10-06新增:來源「類別」對照——wiki關注度與wiki發酵階段標記本質上
    # 是同一個偵測機制(src3維基),不該被當成兩個獨立來源分別計數。
    # 根因:event_theme_radar.py的stocks[].sources已經含"wiki"(來自
    # t["source"]),daily_digest.py原本又依fermentation另外疊加
    # "wiki_preferment"/"wiki"進同一個集合,wiki單一來源命中時
    # len(sources)直接變成2,直接觸發via_source_diversity的多源共振,
    # 但背後其實只有一種偵測機制給出訊號。改成先把每個來源字串正規化成
    # 「類別」再計數,wiki/wiki_preferment一律算同一類。
    SOURCE_CATEGORY = {
        "wiki": "wiki", "wiki_preferment": "wiki",
    }

    def source_category(src):
        return SOURCE_CATEGORY.get(src, src)

    # 以個股為中心聚合:code -> {source_categories:set, detail:{}}
    stock_signals = defaultdict(lambda: {"source_categories": set(), "detail": {}})

    # src1-3:event_theme 的 stocks
    for s in event.get("stocks", []):
        code = s.get("code")
        if not code:
            continue
        sig = stock_signals[code]
        for src in s.get("sources", []):
            sig["source_categories"].add(source_category(src))  # cnyes/mops/wiki
        sig["detail"]["themes"] = s.get("themes", [])
        sig["detail"]["hit_count"] = s.get("hit_count", 0)
        if s.get("fermentation"):
            # 發酵階段只存進detail供顯示用,不再額外塞進來源集合裡(避免
            # 跟上面已經算進sources的"wiki"重複計數成兩個來源)
            sig["detail"]["fermentation"] = s["fermentation"]

    # src5:券商覆蓋率
    broker_surge = []
    for c in broker.get("coverage", []):
        code = c.get("code")
        if not code:
            continue
        if c.get("recentBrokerCount", c.get("recent_broker_count", 0)) >= 3:
            sig = stock_signals[code]
            sig["source_categories"].add("broker")
            sig["detail"]["broker_count"] = c.get("recentBrokerCount", c.get("recent_broker_count"))
            sig["detail"]["brokers"] = c.get("recentBrokers", c.get("recent_brokers", []))
            if c.get("surge"):
                broker_surge.append(code)

    # ── 分區 ──
    # A. 多源共振:兩種獨立的高信心型態,任一成立即納入
    #   (1) 跨來源類型共振(原邏輯):同一股被2種以上不同「來源類型」命中
    #       (cnyes關鍵字暴增/MOPS重訊/wiki關注度/券商覆蓋),來源類型多樣性代表
    #       訊號來自不同偵測機制,互相獨立佐證。
    #   (2) 2026-09-21新增:題材群聚共識(theme cluster):同一股被3個以上「不同
    #       關鍵字」命中,即使全部同屬「關鍵字暴增」這一種來源類型也算。
    #       根因:貿聯-KY(3665)在9/16曾同時被power shelf/sidecar power/
    #       power rack/HVDC/800V HVDC五個獨立關鍵字命中,股價於9/18真的噴出
    #       (+9.95%,逼近漲停)——但因為這五個關鍵字的t["source"]全部寫死是
    #       同一個字串「關鍵字暴增」,原本的source_count邏輯把它們全部收斂成
    #       同一個「來源類型」,n=1,永遠達不到>=2門檻,導致這組真實訊號完全
    #       沒進A區。多個獨立關鍵字同時收斂到同一檔股票,代表市場敘事正在
    #       往這檔股票集中,是跟MOPS+關鍵字同樣有效、甚至更強的共振型態。
    HIT_COUNT_THRESHOLD = 3
    resonance = []
    for code, sig in stock_signals.items():
        n = len(sig["source_categories"])
        hit_count = sig["detail"].get("hit_count", 0)
        via_source_diversity = n >= 2
        via_theme_cluster = hit_count >= HIT_COUNT_THRESHOLD
        if via_source_diversity or via_theme_cluster:
            resonance.append({
                "code": code,
                "name": code2name.get(code, ""),  # 2026-10-06新增
                "source_count": n,
                "sources": sorted(sig["source_categories"]),
                "hit_count": hit_count,
                "themes": sig["detail"].get("themes", []),
                "fermentation": sig["detail"].get("fermentation"),
                "broker_count": sig["detail"].get("broker_count"),
                # 標記這筆是靠哪種型態達標,兩者皆達標時都標記(信心最高)
                "via_source_diversity": via_source_diversity,
                "via_theme_cluster": via_theme_cluster,
            })
    # 排序:兩種型態都達標的最優先,其次看來源數,再看關鍵字命中數
    resonance.sort(key=lambda x: (-(x["via_source_diversity"] and x["via_theme_cluster"]),
                                   -x["source_count"], -x["hit_count"]))

    # B. 未發酵題材(src3)
    preferment_themes = []
    for t in event.get("themes", []):
        if t.get("wiki_stage") == "pre-ferment":
            preferment_themes.append({
                "theme": t["theme"],
                "ratio": t.get("ratio"),
                "baseline": t.get("baseline_mean"),
                "codes": t.get("codes", []),
                "names": t.get("names", {}),  # 2026-09-17新增
                "stock_relation": t.get("stock_relation", {}),  # 2026-10-06新增:direct/indirect
                "semantic_risk": t.get("semantic_risk", ""),
            })

    # C. 券商覆蓋暴增(src5)
    coverage_list = []
    for c in broker.get("coverage", []):
        cnt = c.get("recentBrokerCount", c.get("recent_broker_count", 0))
        if cnt >= 3:
            coverage_list.append({
                "code": c.get("code"),
                "name": code2name.get(c.get("code"), ""),  # 2026-10-06新增
                "broker_count": cnt,
                "brokers": c.get("recentBrokers", c.get("recent_brokers", [])),
                "surge": c.get("surge", False),
            })
    coverage_list.sort(key=lambda x: -x["broker_count"])

    # D. 熱度暴增題材(src1)
    surge_themes = []
    for t in event.get("themes", []):
        # src1 的 source 是「關鍵字暴增」(非 wiki/MOPS),暴增比達標即納入
        src = t.get("source", "")
        is_keyword_src = ("暴增" in src) or ("關鍵字" in src) or (src == "cnyes")
        if is_keyword_src and t.get("ratio", 0) >= 1.5:
            surge_themes.append({
                "theme": t["theme"], "ratio": t.get("ratio"),
                "codes": t.get("codes", []),
                "names": t.get("names", {}),  # 2026-09-17新增
            })
    surge_themes.sort(key=lambda x: -(x.get("ratio") or 0))

    # E. 新題材候選(優先讀theme_tracker整合結果:AI語意版🤖優先,jieba補充🔤降權)
    new_candidates = []
    if tracker.get("first_seen") or tracker.get("ongoing"):
        # 排序:both/ai優先(最可信) > jieba+高次數 > jieba其他;連續追蹤中排前面
        pool = []
        for e in tracker.get("ongoing", []):
            pool.append({**e, "_bucket": 0})  # 連續追蹤中最優先
        for e in tracker.get("first_seen", []):
            pool.append({**e, "_bucket": 1})
        # method現在可能是 search/ai/jieba 或組合(如"search+ai"),依「有幾個獨立來源背書」評分
        def _method_score(m):
            m = m or "jieba"
            n_sources = m.count("+") + 1
            has_search = "search" in m
            has_ai = "ai" in m
            if has_search and has_ai: return (0, -n_sources)  # 主動搜尋+AI萃取都找到,最可信
            if has_search: return (1, -n_sources)
            if has_ai: return (2, -n_sources)
            return (3, -n_sources)  # 純jieba
        # 2026-10-06修正:排序鍵原本把_bucket(連續追蹤中=0優先)放在
        # _method_score前面,導致純jieba、連續追蹤中的通用詞(供應鏈/伺服器/
        # 毛利率這類高頻但無鑑別度的詞,只要連續出現幾天就會「連續追蹤中」)
        # 排到ai_theme_candidates.json裡high confidence的AI背書新題材前面,
        # E區前15名被通用詞佔滿。改成來源可信度(_method_score)優先,同一
        # 可信度等級內再看是否連續追蹤中、最後看出現次數。
        pool.sort(key=lambda x: (_method_score(x.get("method")), x["_bucket"], -x.get("hits", 0)))
        for e in pool[:15]:
            new_candidates.append({
                "term": e.get("term"),
                "recent_hits": e.get("hits"),
                "is_brand_new": e.get("_bucket") == 1,
                "streak_days": e.get("streak_days", 1),
                "method": e.get("method", "jieba"),
                "reason": e.get("reason", ""),
                "stocks": e.get("stocks", []),
                "names": e.get("names", {}),  # 2026-09-18新增
            })
    else:
        # 降級:theme_tracker還沒產出時,退回讀jieba原始版(相容舊資料)
        for c in (newtheme.get("candidates", []))[:15]:
            new_candidates.append({
                "term": c.get("term"),
                "recent_hits": c.get("recent_hits"),
                "is_brand_new": c.get("is_brand_new"),
                "method": "jieba",
                "stocks": c.get("related_stocks", []),
            })

    # F. 國際法說訊號(src6)—— 頻率上升的關鍵詞,依公司整理
    # 2026-10-06:rising_keywords裡混了三種status(見earnings_keywords.py),
    # 單季額度模式下沒有基期的no_baseline_count不算「上升」,全部原樣保留
    # 顯示(F區仍要看得到這些絕對次數訊號),但summary_counts跟「上升」語意
    # 相關的統計只算真正有基期可比的rising/new_mention。
    earnings_rising = earnings.get("rising_keywords", [])
    earnings_rising_with_baseline = [r for r in earnings_rising if r.get("status") != "no_baseline_count"]
    intl_news_heat = earnings.get("intl_news_heat", [])  # 國際新聞每日熱度(CNBC+Yahoo)
    # 法說關鍵詞 → 你的中文題材對應橋(讓國際訊號對到台股題材)
    KW_TO_THEME = {
        "HBM": "HBM", "HBM4": "HBM", "base die": "HBM", "custom HBM": "HBM",
        "DRAM": "記憶體", "LPDDR": "記憶體", "NAND": "NAND", "memory": "記憶體",
        "CoWoS": "CoWoS", "SoIC": "先進封裝", "advanced packaging": "先進封裝",
        "glass substrate": "玻璃基板", "interposer": "先進封裝",
        "CPO": "CPO", "co-packaged": "CPO", "silicon photonics": "矽光子",
        "800V": "HVDC", "HVDC": "HVDC", "liquid cooling": "液冷",
        "immersion": "浸沒式散熱", "power shelf": "HVDC",
        "Vera Rubin": "CoWoS", "Rubin": "CoWoS",  # Rubin平台帶動先進封裝
        # 光通訊/CPO(對應 LITE/COHR/AAOI 及你的CPO研究)
        "optical": "光通訊", "photonics": "矽光子", "laser": "光通訊",
        "transceiver": "光通訊", "EML": "光通訊", "InP": "磷化銦",
        "indium phosphide": "磷化銦", "800G": "光通訊", "1.6T": "光通訊",
        "LPO": "CPO", "optical engine": "CPO",
        # AI語意版(Gemini)新發現題材對應 2026-09-09
        "High-NA": "High-NA EUV", "EUV": "High-NA EUV", "lithography": "微影設備",
        "passive component": "被動元件", "MLCC": "MLCC", "glass fiber": "玻纖布",
        "XPU": "XPU", "custom silicon": "客製化晶片", "custom ASIC": "客製化晶片",
        "satellite": "衛星通訊", "LEO": "低軌衛星頻譜",
        "drone": "無人機", "counter-drone": "反無人機",
        "smart glasses": "智慧眼鏡", "AR": "AR光學", "waveguide": "波導技術",
        "agentic": "代理式AI", "edge computing": "邊緣運算", "on-device": "端側模型",
    }

    # 2026-10-06新增:證據等級(見build_theme_evidence)。放在B/D區已經建好
    # 之後才算,所以這裡回填進去;G區在下面建立時直接查這份表。
    theme_evidence = build_theme_evidence(event, earnings_rising, intl_news_heat, tracker, KW_TO_THEME)
    _no_evidence = {"sources": [], "level": "單一來源"}
    for t in preferment_themes:
        t["evidence"] = theme_evidence.get(canonical_theme(t["theme"]), _no_evidence)
    for t in surge_themes:
        t["evidence"] = theme_evidence.get(canonical_theme(t["theme"]), _no_evidence)

    # 統計每個「台股題材」被幾家國際大廠法說提及升溫
    # 2026-10-06:KW_TO_THEME的目標值先經canonical_theme正規化,讓HBM/DDR5/
    # 記憶體漲價等同義詞收斂到同一個group key,避免同一敘事被拆成好幾個
    # theme_earnings_backing條目,各自都湊不滿國際背書家數。
    theme_earnings_backing = defaultdict(lambda: {"companies": set(), "keywords": set()})
    for r in earnings_rising:
        theme = KW_TO_THEME.get(r["keyword"])
        if theme:
            theme = canonical_theme(theme)
            theme_earnings_backing[theme]["companies"].add(r["symbol"])
            theme_earnings_backing[theme]["keywords"].add(r["keyword"])
    # 國際新聞熱度(CNBC+Yahoo)也算國際背書 —— 用 "新聞" 當來源標記
    for h in intl_news_heat:
        theme = KW_TO_THEME.get(h["keyword"])
        if theme:
            theme = canonical_theme(theme)
            theme_earnings_backing[theme]["companies"].add("國際新聞")
            theme_earnings_backing[theme]["keywords"].add(h["keyword"])

    # G. 交叉:哪些題材「同時有台股訊號 + 國際法說背書」(最高價值)
    # 收集當日有台股訊號的題材(src1暴增 或 src3未發酵)
    # 2026-10-06:同樣先正規化再收進集合——HBM已經發酵時,「記憶體」這個
    # 分開追蹤的watchlist關鍵字即使自己沒過暴增門檻,也該被視為同一個
    # 「記憶體」敘事台股已有訊號,不能因為watchlist把它們拆成不同關鍵字
    # 追蹤,就在G區比對時各算各的、互相看不到對方。
    tw_active_themes = set()
    # 同時收集每個正規化題材群組底下有哪些受惠股代號,供G區算
    # stocks_low/stocks_high用(user要求的股價位置底部/高檔檔數,改讀
    # price_stage.py算出的階段,不再自己另外打Yahoo Finance)。
    theme_codes_by_canonical = defaultdict(set)
    for t in event.get("themes", []):
        src = t.get("source", "")
        is_keyword_src = ("暴增" in src) or ("關鍵字" in src) or (src == "cnyes")
        if t.get("wiki_stage") == "pre-ferment" or (is_keyword_src and t.get("ratio", 0) >= 1.5):
            canon = canonical_theme(t["theme"])
            tw_active_themes.add(canon)
            theme_codes_by_canonical[canon].update(t.get("codes", []))
    cross_confirmed = []
    for theme, backing in theme_earnings_backing.items():
        n_intl = len(backing["companies"])
        in_tw = theme in tw_active_themes
        cross_confirmed.append({
            "theme": theme,
            "intl_companies": sorted(backing["companies"]),
            "intl_keywords": sorted(backing["keywords"]),
            "tw_active": in_tw,
            # 雙邊確認 = 台股有訊號 且 國際法說背書
            "dual_confirmed": in_tw and n_intl >= 1,
            # 2026-10-06新增:受惠股代號(只在tw_active時有意義,未發酵題材
            # 沒有台股代號可言),限前10檔避免股價位置查詢量暴衝
            "codes": sorted(theme_codes_by_canonical.get(theme, set()))[:10],
            "evidence": theme_evidence.get(theme, _no_evidence),
        })
    # 雙邊確認的排前面,再按國際家數
    cross_confirmed.sort(key=lambda x: (not x["dual_confirmed"], -len(x["intl_companies"])))

    digest = {
        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "date": now.strftime("%Y%m%d"),
        "summary_counts": {
            "resonance": len(resonance),
            "preferment_themes": len(preferment_themes),
            "broker_coverage": len(coverage_list),
            "surge_themes": len(surge_themes),
            "new_candidates": len(new_candidates),
            "earnings_rising": len(earnings_rising_with_baseline),
            "cross_confirmed": len([c for c in cross_confirmed if c["dual_confirmed"]]),
        },
        "intl_news_heat": intl_news_heat,
        "A_multi_source_resonance": resonance,
        "B_preferment_themes": preferment_themes,
        "C_broker_coverage": coverage_list,
        "D_surge_themes": surge_themes,
        "E_new_candidates": new_candidates,
        # 2026-09-15新增:固定關鍵字清單裡「有動能但未過暴增門檻」的詞(暴增比0.8~1.5),
        # 跟E_new_candidates(src4發現的全新詞)不同來源——這個是src1既有追蹤詞的早期訊號,
        # 用同一套防小基數雜訊機制(enough_base),不會混入eMMC那種統計假訊號。
        "E2_near_miss": event.get("near_miss_themes", []),
        "F_earnings_rising": earnings_rising,
        "G_cross_confirmed": cross_confirmed,
    }

    # ── 價量階段標籤(2026-10-06新增,price_stage.py)──
    # 讓每檔股票帶「潛伏/啟動/過熱」。同時填入A區的price_pos(low/mid/high),
    # daily_report.py本來就會讀這個欄位(🟢底部/🟡中段/🔴高檔),但先前沒有
    # 任何程式產生它,等於一直是空的。
    try:
        from price_stage import stage_for_codes
        codes = set()
        for r in resonance:
            codes.add(r["code"])
        for key in ("D_surge_themes", "B_preferment_themes"):
            for t in digest.get(key, []):
                codes.update(t.get("codes", []))
        for c in new_candidates:
            codes.update(c.get("stocks", []))
        # 2026-10-06新增:C區(券商覆蓋)跟G區(雙邊確認的受惠股)也要查,
        # 原本只查A/B/D/E區的代號。
        for c in coverage_list:
            if c.get("code"):
                codes.add(c["code"])
        for x in cross_confirmed:
            codes.update(x.get("codes", []))
        stage_map = stage_for_codes(codes, event.get("overheat", {}))
        digest["price_stage"] = stage_map
        for r in resonance:
            info = stage_map.get(r["code"])
            if info:
                r["stage"] = info["stage"]
                r["price_pos"] = info["price_pos"]
                r["chg20"] = info.get("chg20")
                r["chg5"] = info.get("chg5")
        # 2026-10-06新增:C區個股也補上price_pos(daily_report.py的C區會讀)
        for c in coverage_list:
            info = stage_map.get(c.get("code"))
            if info:
                c["price_pos"] = info["price_pos"]
                c["stage"] = info["stage"]
        # 2026-10-06新增:G區統計受惠股裡幾檔底部/高檔(user要求的
        # stocks_low/stocks_high),改讀price_stage算出的結果,不用再
        # 另外打Yahoo Finance。price_data_available區分「真的查到0檔」
        # 跟「這批代號完全沒有股價位置資料可查」,避免daily_report.py
        # 把後者誤印成「底部0檔」。
        for x in cross_confirmed:
            positions = [stage_map.get(cd, {}).get("price_pos") for cd in x.get("codes", [])]
            x["stocks_low"] = sum(1 for p in positions if p == "low")
            x["stocks_high"] = sum(1 for p in positions if p == "high")
            x["price_data_available"] = any(p is not None for p in positions)
    except Exception as e:
        print(f"  ⚠ 價量階段標籤略過: {e}")
        digest["price_stage"] = {}
    return digest


def _stage_tag(d, code):
    """顯示用:' [🔴過熱+35%⚠注意交易]';沒資料回空字串。"""
    info = (d.get("price_stage") or {}).get(code)
    if not info:
        return ""
    try:
        from price_stage import tag
        return f" [{tag(info)}]"
    except Exception:
        return ""


def print_digest(d):
    print("=" * 60)
    print(f"每日題材雷達摘要  {d['date']}")
    print("=" * 60)
    c = d["summary_counts"]
    print(f"多源共振{c['resonance']} | 未發酵題材{c['preferment_themes']} | "
          f"券商覆蓋{c['broker_coverage']} | 暴增題材{c['surge_themes']} | "
          f"新候選{c['new_candidates']} | 雙邊確認{c.get('cross_confirmed',0)}")

    # G. 最高價值:台股訊號 × 國際法說雙邊確認
    print("\n▍G. 雙邊確認題材(台股訊號 × 國際法說背書 — 最高價值)")
    dual = [x for x in d.get("G_cross_confirmed", []) if x["dual_confirmed"]]
    if dual:
        for x in dual:
            ev = x.get("evidence", {})
            ev_tag = f" [{ev.get('level','')}]" if ev.get("sources") else ""
            print(f"  ✅ {x['theme']}{ev_tag}  台股訊號✓ + 國際法說: {' '.join(x['intl_companies'])} "
                  f"({' '.join(x['intl_keywords'][:4])})")
    else:
        print("  (今日無雙邊確認)")
    # 只有國際法說、台股還沒燒的(發酵前純訊號)
    intl_only = [x for x in d.get("G_cross_confirmed", []) if not x["tw_active"] and len(x["intl_companies"]) >= 2]
    if intl_only:
        print("\n  ○ 國際先行、台股未燃(最前緣 — 國際法說已提,台股新聞未跟上):")
        for x in intl_only[:6]:
            print(f"    {x['theme']}  國際: {' '.join(x['intl_companies'])} ({' '.join(x['intl_keywords'][:3])})")

    print("\n▍A. 多源共振個股(被多個獨立訊號同時命中)")
    if d["A_multi_source_resonance"]:
        for r in d["A_multi_source_resonance"]:
            ferm = " 🌱未發酵" if r.get("fermentation") == "pre-ferment" else ""
            themes = "/".join(r["themes"][:3]) if r["themes"] else ""
            tags = []
            if r.get("via_source_diversity"):
                tags.append(f"{r['source_count']}源: {' '.join(r['sources'])}")
            if r.get("via_theme_cluster"):
                tags.append(f"🌾題材群聚: {r['hit_count']}個關鍵字同時命中")
            _stg = _stage_tag(d, r["code"])
            print(f"  {r['code']}{r.get('name','')}  [{' | '.join(tags)}]{ferm}{_stg}  {themes}")
    else:
        print("  (今日無多源共振)")

    print("\n▍B. 未發酵題材(發酵前緣 — 大眾認知剛翹頭)")
    for t in d["B_preferment_themes"]:
        risk = " ⚠" + t["semantic_risk"][:20] if t.get("semantic_risk") else ""
        names = t.get("names", {})  # 2026-09-17新增,舊來源查無此欄位時優雅退回純代碼
        relation = t.get("stock_relation", {})  # 2026-10-06新增
        # 間接受惠股標(間接)字樣,不跟直接供應鏈股混淆
        stock_str = " ".join(
            f"{cd}{names.get(cd,'')}" + ("(間接)" if relation.get(cd) == "indirect" else "")
            for cd in t["codes"][:5])
        ev = t.get("evidence", {})
        ev_tag = f" [{ev.get('level','')}]" if ev.get("sources") else ""
        print(f"  {t['theme']}{ev_tag}  暴增{t['ratio']} 基線{t['baseline']}  股:{stock_str}{risk}")

    print("\n▍C. 券商覆蓋暴增(機構領先 — 多家券商同時cover)")
    for c in d["C_broker_coverage"]:
        flag = " 🔥" if c["surge"] else ""
        print(f"  {c['code']}{c.get('name','')}  {c['broker_count']}家券商{flag}  {' '.join(c['brokers'])}")

    print("\n▍D. 熱度暴增題材(新聞討論升溫)")
    for t in d["D_surge_themes"][:8]:
        names = t.get("names", {})  # 2026-09-17新增
        stock_str = " ".join(f"{cd}{names.get(cd,'')}{_stage_tag(d, cd)}" for cd in t["codes"][:5])
        ev = t.get("evidence", {})
        ev_tag = f" [{ev.get('level','')}]" if ev.get("sources") else ""
        print(f"  {t['theme']}{ev_tag}  暴增{t['ratio']}  股:{stock_str}")

    print("\n▍E. 新題材候選(參考 — 需人工判斷)")
    for c in d["E_new_candidates"][:8]:
        tag = "🆕" if c["is_brand_new"] else ""
        names = c.get("names", {})
        stocks = c.get("stocks", [])
        stk = "  股:" + " ".join(f"{cd}{names.get(cd,'')}" for cd in stocks) if stocks else ""
        print(f"  {tag}{c['term']}  近{c['recent_hits']}次{stk}")

    print("\n▍E2. 近期關注(固定追蹤詞,有動能但未達暴增門檻)")
    if d.get("E2_near_miss"):
        for t in d["E2_near_miss"][:10]:
            print(f"  👀{t['theme']}  暴增比{t['ratio']}  近{t['recent_count']}次")
    else:
        print("  (今日無)")

    print("\n▍F. 國際法說關鍵詞升溫(src6 — 季度更新,供應鏈端訊號)")
    er = d.get("F_earnings_rising", [])
    if er:
        for r in er[:12]:
            status = r.get("status")
            if status == "no_baseline_count":
                flag = f"提及×{r.get('this_count')}(無基期可比)"
            elif status == "new_mention":
                flag = "🆕新提及"
            else:
                flag = f"↑{r.get('prev_count')}→{r.get('this_count')}"
            print(f"  {r['symbol']:5s} {r['keyword']:18s} [{r['category']}] {flag}")
    else:
        print("  (無 src6 資料,或非財報季)")


def main():
    d = build_digest()
    print_digest(d)
    with open("daily_digest.json", "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    print(f"\n→ daily_digest.json 已寫出")


if __name__ == "__main__":
    main()
