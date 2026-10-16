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
  I. 建議加入 watchlist(watchlist_suggester.py,人工升格)
============================================================
"""

import json
import datetime as dt
from collections import defaultdict


# method現在可能是 search/ai/jieba 或組合(如"search+ai"),依「有幾個獨立來源背書」評分
# 2026-10-15:從 build_digest 抽成模組層函式,watchlist_suggester.py 共用同一套判定
def _method_score(m):
    m = m or "jieba"
    n_sources = m.count("+") + 1
    has_search = "search" in m
    has_ai = "ai" in m
    if has_search and has_ai: return (0, -n_sources)  # 主動搜尋+AI萃取都找到,最可信
    if has_search: return (1, -n_sources)
    if has_ai: return (2, -n_sources)
    return (3, -n_sources)  # 純jieba


# 法說關鍵詞 → 你的中文題材對應橋(讓國際訊號對到台股題材)
# 2026-10-15:從 build_digest 抽成模組層常數,watchlist_suggester.py 會 import 檢查英文詞有無對應
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
    # 2026-10-12:investor day 常見詞(investor_events.py EXTRA_KEYWORDS),只對到 watchlist 既有題材
    "custom XPU": "XPU", "custom accelerator": "客製化晶片", "custom compute": "客製化晶片",
    "optical interconnect": "光通訊", "electro-optics": "光通訊",
    "DSP": "DSP", "SerDes": "SerDes", "Ethernet switch": "交換器晶片", "switch ASIC": "交換器晶片",
    "Tomahawk": "Tomahawk",
}


def load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def build_digest(today=None):
    """today:大會日曆用的日期(datetime.date);None = 台北時間今天。只影響日曆,不影響其他區塊。"""
    now = dt.datetime.now()

    # ── 讀各源 ──
    event = load_json("event_theme_raw.json") or {}
    newtheme = load_json("new_theme_candidates.json") or {}
    broker = load_json("broker_coverage.json") or {}
    earnings = load_json("earnings_keywords.json") or {}  # src6
    investor = load_json("investor_day_signals.json") or {}  # 2026-10-12:investor day/技術大會
    tracker = load_json("theme_tracker.json") or {}  # 題材追蹤(AI語意+jieba整合,含首見/追蹤中)
    wl_sugg = load_json("watchlist_suggestions.json") or {}  # 2026-10-15:watchlist_suggester.py,缺檔不報錯

    # 以個股為中心聚合:code -> {sources:set, detail:{}}
    stock_signals = defaultdict(lambda: {"sources": set(), "detail": {}})

    # src1-3:event_theme 的 stocks
    for s in event.get("stocks", []):
        code = s.get("code")
        if not code:
            continue
        sig = stock_signals[code]
        for src in s.get("sources", []):
            sig["sources"].add(src)  # cnyes/mops/wiki
        sig["detail"]["themes"] = s.get("themes", [])
        sig["detail"]["hit_count"] = s.get("hit_count", 0)
        if s.get("fermentation"):
            sig["sources"].add("wiki_preferment" if s["fermentation"] == "pre-ferment" else "wiki")
            sig["detail"]["fermentation"] = s["fermentation"]

    # src5:券商覆蓋率
    broker_surge = []
    for c in broker.get("coverage", []):
        code = c.get("code")
        if not code:
            continue
        if c.get("recentBrokerCount", c.get("recent_broker_count", 0)) >= 3:
            sig = stock_signals[code]
            sig["sources"].add("broker")
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
        n = len(sig["sources"])
        hit_count = sig["detail"].get("hit_count", 0)
        via_source_diversity = n >= 2
        via_theme_cluster = hit_count >= HIT_COUNT_THRESHOLD
        if via_source_diversity or via_theme_cluster:
            resonance.append({
                "code": code,
                "source_count": n,
                "sources": sorted(sig["sources"]),
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
                "semantic_risk": t.get("semantic_risk", ""),
            })

    # C. 券商覆蓋暴增(src5)
    coverage_list = []
    for c in broker.get("coverage", []):
        cnt = c.get("recentBrokerCount", c.get("recent_broker_count", 0))
        if cnt >= 3:
            coverage_list.append({
                "code": c.get("code"),
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
        pool.sort(key=lambda x: (x["_bucket"], _method_score(x.get("method")), -x.get("hits", 0)))
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
    earnings_rising = earnings.get("rising_keywords", [])
    intl_news_heat = earnings.get("intl_news_heat", [])  # 國際新聞每日熱度(CNBC+Yahoo)
    # 法說關鍵詞 → 台股題材對應見模組層 KW_TO_THEME
    # 統計每個「台股題材」被幾家國際大廠法說提及升溫
    theme_earnings_backing = defaultdict(lambda: {"companies": set(), "keywords": set()})
    for r in earnings_rising:
        theme = KW_TO_THEME.get(r["keyword"])
        if theme:
            theme_earnings_backing[theme]["companies"].add(r["symbol"])
            theme_earnings_backing[theme]["keywords"].add(r["keyword"])
    # 國際新聞熱度(CNBC+Yahoo)也算國際背書 —— 用 "新聞" 當來源標記
    for h in intl_news_heat:
        theme = KW_TO_THEME.get(h["keyword"])
        if theme:
            theme_earnings_backing[theme]["companies"].add("國際新聞")
            theme_earnings_backing[theme]["keywords"].add(h["keyword"])

    # F2. Investor day / 技術大會(investor_events.py):進行中活動的關鍵詞也算國際背書,
    # 公司標成「代號(活動名)」,跟法說來源區分;技術大會沒有單一公司時用大會名稱。
    investor_events = investor.get("events", [])
    for h in investor.get("keyword_hits", []):
        theme = KW_TO_THEME.get(h["keyword"])
        if theme:
            theme_earnings_backing[theme]["companies"].add(h.get("label") or f"{h['symbol']}({h['event']})")
            theme_earnings_backing[theme]["keywords"].add(h["keyword"])

    # G. 交叉:哪些題材「同時有台股訊號 + 國際法說背書」(最高價值)
    # 收集當日有台股訊號的題材(src1暴增 或 src3未發酵)
    tw_active_themes = set()
    for t in event.get("themes", []):
        src = t.get("source", "")
        is_keyword_src = ("暴增" in src) or ("關鍵字" in src) or (src == "cnyes")
        if t.get("wiki_stage") == "pre-ferment" or (is_keyword_src and t.get("ratio", 0) >= 1.5):
            tw_active_themes.add(t["theme"])
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
            "earnings_rising": len(earnings_rising),
            "investor_events": len(investor_events),
            "cross_confirmed": len([c for c in cross_confirmed if c["dual_confirmed"]]),
            # 2026-10-15:I 區只算 strong + suggest(watch 是首見觀察,不計)
            "watchlist_suggestions": len([x for x in wl_sugg.get("suggestions", [])
                                          if x.get("tier") in ("strong", "suggest")]),
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
        # 2026-10-12:F2 = 國際大廠 investor day / 技術大會(進行中)+ 即將舉行的行事曆
        "F2_investor_events": investor_events,
        "F2_upcoming": investor.get("upcoming", [])[:10],
        "G_cross_confirmed": cross_confirmed,
        # 2026-10-15:I = 建議加入 themes_watchlist.txt 的詞(人工升格,不會自動加入)
        "I_watchlist_suggestions": wl_sugg.get("suggestions", []),
        "I_paste_block": wl_sugg.get("paste_block", ""),
        # 共識自動加入(AI片語拆詞=jieba詞,近7日>=2天+鉅亨基數夠):今日已寫入 watchlist 的詞、
        # 尚未達標的共識候選、加入滿30天未在偵測源1出現的(建議移除,不自動刪)
        "I_auto_added": wl_sugg.get("auto_added", []),
        "I_consensus": wl_sugg.get("consensus", []),
        "I_stale_auto_added": wl_sugg.get("stale_auto_added", []),
        "I_ai_failed": wl_sugg.get("ai_failed", False),
    }

    # ── 價量階段標籤(2026-10-06新增,price_stage.py)──
    # 讓每檔股票帶「潛伏/啟動/過熱」(stage)與價格位置(price_pos/range_pos)。
    # 2026-10-11:兩者已拆成獨立欄位——stage 只看近期漲幅與量能;price_pos 是
    # 近約40個交易日收盤價區間的位置(low/mid/high,daily_report 顯示🟢底部/
    # 🟡中段/🔴高檔),資料不足20日時為 None,下游要容忍。
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
        stage_map = stage_for_codes(codes, event.get("overheat", {}))
        digest["price_stage"] = stage_map
        for r in resonance:
            info = stage_map.get(r["code"])
            if info:
                r["stage"] = info.get("stage")
                r["price_pos"] = info.get("price_pos")    # 可能是 None(區間不足20日或價格持平)
                r["range_pos"] = info.get("range_pos")
                r["chg20"] = info.get("chg20")
                r["chg5"] = info.get("chg5")
    except Exception as e:
        print(f"  ⚠ 價量階段標籤略過: {e}")
        digest["price_stage"] = {}

    # ── 大會日曆(2026-10-16新增,events_calendar.py,v1 僅顯示)──
    # 大會前後兩週題材新聞密度會自然升高(可預期的事件效應)。只加背景與 A 區標記,
    # 不改變任何門檻、A 區的納入與排序;沒有事件時不寫 events 欄位(輸出與改動前相同)。
    # 任何錯誤整段略過,不影響其他區塊。
    try:
        from events_calendar import window, tag_for_theme
        cal_today = today or dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date()
        events = window(cal_today)
        if events:
            digest["events"] = events
            for r in resonance:
                t = tag_for_theme(r.get("themes", []), cal_today, events=events)
                if t:
                    r["event_tag"] = t
    except Exception as e:
        print(f"  ⚠ 大會日曆略過: {e}")
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

    # 大會日曆(2026-10-16新增):前後兩週內的大會;沒有事件時不印這一節
    if d.get("events"):
        try:
            from events_calendar import format_line
            print("\n📅 大會日曆(前後兩週;大會前後題材熱度上升可能是事件效應,非投資訊號)")
            for e in d["events"]:
                print(format_line(e))
        except Exception as e:
            print(f"  ⚠ 大會日曆顯示略過: {e}")

    # G. 最高價值:台股訊號 × 國際法說雙邊確認
    print("\n▍G. 雙邊確認題材(台股訊號 × 國際法說背書 — 最高價值)")
    dual = [x for x in d.get("G_cross_confirmed", []) if x["dual_confirmed"]]
    if dual:
        for x in dual:
            print(f"  ✅ {x['theme']}  台股訊號✓ + 國際法說: {' '.join(x['intl_companies'])} "
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
            _evt = f"  {r['event_tag']}" if r.get("event_tag") else ""   # 大會標記,只顯示,不影響排序
            print(f"  {r['code']}  [{' | '.join(tags)}]{ferm}{_stg}  {themes}{_evt}")
    else:
        print("  (今日無多源共振)")

    print("\n▍B. 未發酵題材(發酵前緣 — 大眾認知剛翹頭)")
    for t in d["B_preferment_themes"]:
        risk = " ⚠" + t["semantic_risk"][:20] if t.get("semantic_risk") else ""
        names = t.get("names", {})  # 2026-09-17新增,舊來源查無此欄位時優雅退回純代碼
        stock_str = " ".join(f"{cd}{names.get(cd,'')}" for cd in t["codes"][:5])
        print(f"  {t['theme']}  暴增{t['ratio']} 基線{t['baseline']}  股:{stock_str}{risk}")

    print("\n▍C. 券商覆蓋暴增(機構領先 — 多家券商同時cover)")
    for c in d["C_broker_coverage"]:
        flag = " 🔥" if c["surge"] else ""
        print(f"  {c['code']}  {c['broker_count']}家券商{flag}  {' '.join(c['brokers'])}")

    print("\n▍D. 熱度暴增題材(新聞討論升溫)")
    for t in d["D_surge_themes"][:8]:
        names = t.get("names", {})  # 2026-09-17新增
        stock_str = " ".join(f"{cd}{names.get(cd,'')}{_stage_tag(d, cd)}" for cd in t["codes"][:5])
        print(f"  {t['theme']}  暴增{t['ratio']}  股:{stock_str}")

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
            flag = "🆕新提及" if r.get("prev_count", 0) == 0 else f"↑{r.get('prev_count')}→{r.get('this_count')}"
            print(f"  {r['symbol']:5s} {r['keyword']:18s} [{r['category']}] {flag}")
    else:
        print("  (無 src6 資料,或非財報季)")

    print("\n▍F2. 國際大廠 Investor Day / 技術大會(活動日起5天內)")
    ev = d.get("F2_investor_events", [])
    if ev:
        for e in ev[:10]:
            top = " ".join(f"{k}×{v['count']}" for k, v in list(e.get("keywords", {}).items())[:6])
            when = f"{e['event_date']}(D+{e['day_offset']})" if e.get("event_date") else "近期"
            head = e['name'] if e.get('type') == 'conference' else f"{e['symbol']} {e['name']} {e['event']}"
            print(f"  {head} {when} "
                  f"報導{e.get('news_count', 0)} recap{e.get('recap_count', 0)}  {top}")
    else:
        print("  (目前沒有進行中的活動)")
    up = d.get("F2_upcoming", [])
    if up:
        print("  即將舉行:" + "、".join(f"{u['date']} {u['symbol']} {u['event']}" for u in up[:6]))

    print("\n▍I. Watchlist 自動加入與建議")
    added = d.get("I_auto_added", [])
    if added:
        print("  ✅ 今日自動加入 themes_watchlist.txt(題材源共識):")
        for a in added:
            names = a.get("names", {})
            stk = " ".join(f"{cd}{names.get(cd, '')}" for cd in a.get("stocks", [])[:5])
            print(f"    {a['term']}  共識{a['consensus_days']}天 ← {'、'.join(a['ai_phrases'][:3])}  "
                  f"cnyes暴增{a.get('cnyes_ratio')}" + (f"  股:{stk}" if stk else ""))
    if d.get("I_ai_failed"):
        print("  ⚠ AI 語意萃取今天失敗,共識自動加入暫停一天")
    pend = [c for c in d.get("I_consensus", []) if c.get("status") != "auto_added"]
    if pend:
        print("  ⏳ 共識候選(未加入):" + "、".join(f"{c['term']}({c['status']})" for c in pend[:8]))
    for x in d.get("I_stale_auto_added", []):
        print(f"  🗑 建議移除 {x['term']}(自動加入於 {x['promoted_date']},30天未在暴增/近期關注出現)")
    print("  ── 以下為分級建議,不會自動寫入 ──")
    sugg = d.get("I_watchlist_suggestions", [])
    if sugg:
        mark = {"strong": "⭐strong", "suggest": "➕suggest", "watch": "👀watch"}
        for x in sugg:
            src = "🔤" if _method_score(x.get("method"))[0] == 3 else "🤖"
            if x.get("cnyes_ratio") is None:
                cn = "cnyes未查"
            elif x.get("cnyes_truncated"):
                cn = "cnyes新聞量過大,數字不可信"
            elif x.get("cnyes_viable") is None:
                cn = "cnyes可用性未知(查詢全0則)"
            else:
                cn = f"cnyes暴增{x.get('cnyes_ratio')} {'✓可用' if x['cnyes_viable'] else '✗基數不足'}"
            names = x.get("names", {})
            stk = " ".join(f"{cd}{names.get(cd, '')}" for cd in x.get("stocks", [])[:5])
            print(f"  {mark.get(x.get('tier'), x.get('tier'))} {src}{x['term']}  連{x.get('streak_days')}天  "
                  f"{cn}" + (f"  股:{stk}" if stk else ""))
        print("  ── 可直接貼進 themes_watchlist.txt(strong/suggest 且 cnyes 可用)──")
        print("\n".join("  " + t for t in (d.get("I_paste_block") or "").splitlines()) or "  (無)")
    else:
        print("  (今日無建議,或 watchlist_suggestions.json 不存在)")


def main():
    import sys
    today = None
    if "--today" in sys.argv:      # 測試用:指定大會日曆的日期(YYYY-MM-DD)
        today = dt.date.fromisoformat(sys.argv[sys.argv.index("--today") + 1])
    d = build_digest(today)
    print_digest(d)
    with open("daily_digest.json", "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    print(f"\n→ daily_digest.json 已寫出")


if __name__ == "__main__":
    main()
