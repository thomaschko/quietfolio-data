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


def load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def build_digest():
    now = dt.datetime.now()

    # ── 讀各源 ──
    event = load_json("event_theme_raw.json") or {}
    newtheme = load_json("new_theme_candidates.json") or {}
    broker = load_json("broker_coverage.json") or {}
    earnings = load_json("earnings_keywords.json") or {}  # src6
    tracker = load_json("theme_tracker.json") or {}  # 題材追蹤(AI語意+jieba整合,含首見/追蹤中)

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
    # A. 多源共振(2+源)
    resonance = []
    for code, sig in stock_signals.items():
        n = len(sig["sources"])
        if n >= 2:
            resonance.append({
                "code": code,
                "source_count": n,
                "sources": sorted(sig["sources"]),
                "themes": sig["detail"].get("themes", []),
                "fermentation": sig["detail"].get("fermentation"),
                "broker_count": sig["detail"].get("broker_count"),
            })
    resonance.sort(key=lambda x: -x["source_count"])

    # B. 未發酵題材(src3)
    preferment_themes = []
    for t in event.get("themes", []):
        if t.get("wiki_stage") == "pre-ferment":
            preferment_themes.append({
                "theme": t["theme"],
                "ratio": t.get("ratio"),
                "baseline": t.get("baseline_mean"),
                "codes": t.get("codes", []),
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
        method_rank = {"both": 0, "ai": 1, "jieba": 2}
        pool.sort(key=lambda x: (x["_bucket"], method_rank.get(x.get("method", "jieba"), 3), -x.get("hits", 0)))
        for e in pool[:15]:
            new_candidates.append({
                "term": e.get("term"),
                "recent_hits": e.get("hits"),
                "is_brand_new": e.get("_bucket") == 1,
                "streak_days": e.get("streak_days", 1),
                "method": e.get("method", "jieba"),
                "reason": e.get("reason", ""),
                "stocks": e.get("stocks", []),
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
            "cross_confirmed": len([c for c in cross_confirmed if c["dual_confirmed"]]),
        },
        "intl_news_heat": intl_news_heat,
        "A_multi_source_resonance": resonance,
        "B_preferment_themes": preferment_themes,
        "C_broker_coverage": coverage_list,
        "D_surge_themes": surge_themes,
        "E_new_candidates": new_candidates,
        "F_earnings_rising": earnings_rising,
        "G_cross_confirmed": cross_confirmed,
    }
    return digest


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
            print(f"  {r['code']}  [{r['source_count']}源: {' '.join(r['sources'])}]{ferm}  {themes}")
    else:
        print("  (今日無多源共振)")

    print("\n▍B. 未發酵題材(發酵前緣 — 大眾認知剛翹頭)")
    for t in d["B_preferment_themes"]:
        risk = " ⚠" + t["semantic_risk"][:20] if t.get("semantic_risk") else ""
        print(f"  {t['theme']}  暴增{t['ratio']} 基線{t['baseline']}  股:{' '.join(t['codes'][:5])}{risk}")

    print("\n▍C. 券商覆蓋暴增(機構領先 — 多家券商同時cover)")
    for c in d["C_broker_coverage"]:
        flag = " 🔥" if c["surge"] else ""
        print(f"  {c['code']}  {c['broker_count']}家券商{flag}  {' '.join(c['brokers'])}")

    print("\n▍D. 熱度暴增題材(新聞討論升溫)")
    for t in d["D_surge_themes"][:8]:
        print(f"  {t['theme']}  暴增{t['ratio']}  股:{' '.join(t['codes'][:5])}")

    print("\n▍E. 新題材候選(參考 — 需人工判斷)")
    for c in d["E_new_candidates"][:8]:
        tag = "🆕" if c["is_brand_new"] else ""
        print(f"  {tag}{c['term']}  近{c['recent_hits']}次")

    print("\n▍F. 國際法說關鍵詞升溫(src6 — 季度更新,供應鏈端訊號)")
    er = d.get("F_earnings_rising", [])
    if er:
        for r in er[:12]:
            flag = "🆕新提及" if r.get("prev_count", 0) == 0 else f"↑{r.get('prev_count')}→{r.get('this_count')}"
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
