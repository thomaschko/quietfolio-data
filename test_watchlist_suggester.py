# -*- coding: utf-8 -*-
"""
test_watchlist_suggester.py — watchlist_suggester.py 的單元測試(不需網路)

用法:python test_watchlist_suggester.py
鉅亨搜尋以假函式取代;分級/排除規則依任務說明。
"""
import json

import watchlist_suggester as ws
from daily_digest import KW_TO_THEME, _method_score

ws.CNYES_SLEEP = 0
NOW = 1_800_000_000
RECENT_START = NOW - 3 * 86400
BASE_START = RECENT_START - 17 * 86400
WATCHLIST = {w.lower(): w for w in ["HVDC", "CPO", "矽光子", "AI"]}
NAME2CODE = {"大立光": "3008", "日月光投控": "3711"}  # 日月光 → 前綴規則
STOPWORDS = {"台股", "fed"}


def T(term, method="ai", streak=1, stocks=(), hits=5):
    return {"term": term, "method": method, "streak_days": streak, "stocks": list(stocks),
            "names": {}, "hits": hits, "reason": ""}


def fake_search(counts):
    """counts: term -> (recent則數, earlier則數)。"""
    def search(term, start_ts, max_pages=5):
        r, e = counts.get(term, (0, 0))
        return ([{"publishAt": NOW - 3600}] * r) + ([{"publishAt": BASE_START + 3600}] * e)
    return search


def run(terms, counts=None, first_seen=()):
    tracker = {"ongoing": terms, "first_seen": list(first_seen)}
    return ws.build(tracker, WATCHLIST, NAME2CODE, STOPWORDS, _method_score, KW_TO_THEME,
                    now_ts=NOW, window=(RECENT_START, BASE_START),
                    search=fake_search(counts or {}) if counts is not None else None)


def by_term(out):
    return {s["term"]: s for s in out["suggestions"]}, {r["term"]: r["reason"] for r in out["rejected"]}


def test_rejections_have_reasons():
    out = run([T("hvdc", streak=5), T("800V HVDC", streak=5), T("AI伺服器", streak=5),
               T("大立光", streak=5), T("台股", streak=5), T("FED", streak=5), T("12.5%", streak=5),
               T("載板", streak=5), T("晶圓代工", method="jieba", streak=4)])
    sug, rej = by_term(out)
    assert rej["hvdc"] == "already_in_watchlist"
    assert rej["800V HVDC"] == "overlaps:HVDC"
    assert rej["大立光"] == "is_company_name"
    assert rej["台股"] == "noise" and rej["FED"] == "noise" and rej["12.5%"] == "noise"
    assert rej["載板"] == "noise"                                  # 中文少於3字
    assert rej["晶圓代工"] == "jieba_insufficient_streak"
    assert "AI伺服器" in sug                                       # AI 只有2字元,不算重疊
    assert all(r["reason"] for r in out["rejected"])


def test_dedup_keeps_longer_streak():
    out = run([T("Glass Core", streak=2, stocks=["1802"])], first_seen=[T("glass core", streak=4)])
    sug, _ = by_term(out)
    assert len(out["suggestions"]) == 1 and out["suggestions"][0]["streak_days"] == 4
    assert out["suggestions"][0]["tier"] == "strong"


def test_tiers():
    out = run([T("玻璃基板材料", "ai", 3), T("散熱模組新規", "search+ai", 1),
               T("光纖陣列單元", "ai", 2, ["3363"]), T("鈣鈦礦電池", "ai", 1),
               T("探針卡測試", "jieba", 5), T("液冷快接頭", "ai", 2)])
    sug, _ = by_term(out)
    assert sug["玻璃基板材料"]["tier"] == "strong"
    assert sug["散熱模組新規"]["tier"] == "strong"        # search + ai 同時背書
    assert sug["光纖陣列單元"]["tier"] == "suggest"
    assert sug["探針卡測試"]["tier"] == "suggest"          # 純 jieba 連5天
    assert sug["鈣鈦礦電池"]["tier"] == "watch"
    assert sug["液冷快接頭"]["tier"] == "watch" and sug["液冷快接頭"]["notes"]   # AI、連2天、無個股
    c = out["counts"]
    assert sum(c.values()) == len(out["suggestions"])


def test_cnyes_viability_and_paste_block():
    # earlier 17日 3 則 → 日均 0.176 >= 0.15 可用;2 則 → 0.118 不可用
    out = run([T("玻璃基板材料", "ai", 3), T("光纖陣列單元", "ai", 3)],
              counts={"玻璃基板材料": (4, 3), "光纖陣列單元": (4, 2)})
    sug, _ = by_term(out)
    a, b = sug["玻璃基板材料"], sug["光纖陣列單元"]
    assert a["cnyes_viable"] is True and a["cnyes_recent"] == 4
    assert a["cnyes_ratio"] == round((4 / 3) / (3 / 17), 2)       # 與 detect_fixed_keywords 同公式
    assert b["cnyes_viable"] is False and ws.NOTE_CNYES_LOW in b["notes"]
    assert out["paste_block"] == "玻璃基板材料"
    assert out["suggestions"][0]["term"] == "玻璃基板材料"         # 同 tier:可用的排前面


def test_cnyes_limit_and_watch_not_checked():
    terms = [T(f"題材詞{i:02d}號", "ai", 3, hits=i) for i in range(12)] + [T("首見題材詞", "ai", 1)]
    out = run(terms, counts={t["term"]: (3, 5) for t in terms})
    assert out["cnyes_checked"] == 10
    unchecked = [s for s in out["suggestions"] if s["tier"] == "strong" and s["cnyes_ratio"] is None]
    assert len(unchecked) == 2 and all("上限" in s["notes"][-1] for s in unchecked)
    sug, _ = by_term(out)
    assert sug["首見題材詞"]["cnyes_ratio"] is None                 # watch 不查


def test_all_empty_means_unknown_not_low_base():
    out = run([T("玻璃基板材料", "ai", 3), T("光纖陣列單元", "ai", 3)], counts={})
    assert out["cnyes_all_empty"]
    assert all(s["cnyes_viable"] is None and ws.NOTE_CNYES_LOW not in s["notes"]
               for s in out["suggestions"])
    assert out["paste_block"] == ""


def test_english_kw_to_theme_note():
    out = run([T("glass substrate", "ai", 3), T("Glass Core", "ai", 3)])
    sug, _ = by_term(out)
    assert ws.NOTE_NO_KW_THEME not in sug["glass substrate"]["notes"]   # 已在 KW_TO_THEME
    assert ws.NOTE_NO_KW_THEME in sug["Glass Core"]["notes"]


def run_state(terms, state, counts=None, date="20261015", promotions=(), event_raw=None):
    tracker = {"date": date, "ongoing": terms, "first_seen": []}
    return ws.build(tracker, WATCHLIST, NAME2CODE, STOPWORDS, _method_score, KW_TO_THEME,
                    now_ts=NOW, window=(RECENT_START, BASE_START),
                    search=fake_search(counts or {}), state=state,
                    promotions=list(promotions), event_raw=event_raw)


def test_phrase_tokens_no_fake_consensus():
    assert ws.phrase_tokens("Google TPU架構") == ["Google", "TPU", "架構"]
    assert ws.phrase_tokens("AI晶片/自研ASIC") == ["AI", "晶片", "自研", "ASIC"]
    entries = [T("AI晶片/自研ASIC", "ai"), T("IC", "jieba", 9), T("先進封裝CoWoS", "ai"), T("OS", "jieba", 9),
               T("機器人/自動化", "ai"), T("自動化", "jieba", 9), T("Google TPU架構", "ai"), T("TPU", "jieba", 3)]
    cons = ws.consensus_today(entries, _method_score, STOPWORDS)
    assert list(cons) == ["TPU"] and cons["TPU"]["ai_phrases"] == ["Google TPU架構"]


def test_consensus_needs_two_days_and_cnyes():
    day1 = [T("Google TPU架構", "ai"), T("TPU", "jieba", 3)]
    state = {}
    out = run_state(day1, state, counts={"TPU": (5, 6)}, date="20261014")
    assert out["auto_added"] == [] and out["consensus"][0]["status"].startswith("pending:共識1天")
    day2 = [T("Google TPU算力生態", "ai"), T("TPU", "jieba", 4)]      # 隔天 AI 措辭不同,拆詞仍相同
    out = run_state(day2, state, counts={"TPU": (5, 6)}, date="20261015")
    assert [a["term"] for a in out["auto_added"]] == ["TPU"]
    assert out["auto_added"][0]["consensus_days"] == 2 and out["auto_added"][0]["source"] == "consensus"
    assert "TPU" not in [s["term"] for s in out["suggestions"]]


def test_consensus_low_base_or_unknown_not_added():
    hist = {"consensus_history": {"20261014": ["TPU", "CXL"]}}
    terms = [T("Google TPU架構", "ai"), T("TPU", "jieba", 3), T("CXL記憶體擴充", "ai"), T("CXL", "jieba", 3)]
    out = run_state(terms, json.loads(json.dumps(hist)), counts={"TPU": (5, 1), "CXL": (5, 6)})
    st = {c["term"]: c["status"] for c in out["consensus"]}
    assert st["TPU"] == "rejected:cnyes基數不足" and st["CXL"] == "auto_added"
    out = run_state(terms, json.loads(json.dumps(hist)), counts={})       # 鉅亨全部0則 → 不加
    assert out["auto_added"] == [] and all(c["status"].startswith("pending:cnyes") for c in out["consensus"])


def test_consensus_daily_cap_and_overlap():
    names = ["AAA", "BBB", "CCC", "DDD"]
    terms, hist = [], {"consensus_history": {"20261014": names + ["HVDC800"]}}
    for n in names + ["HVDC800"]:
        terms += [T(f"{n}供應鏈題材", "ai"), T(n, "jieba", 3)]
    out = run_state(terms, hist, counts={n: (5, 6) for n in names + ["HVDC800"]})
    assert len(out["auto_added"]) == ws.AUTO_ADD_MAX_PER_DAY
    st = {c["term"]: c["status"] for c in out["consensus"]}
    assert st["HVDC800"] == "rejected:overlaps:HVDC"
    assert sum(1 for v in st.values() if "上限" in v) == 1


def test_stale_auto_added():
    promos = [{"term": "TPU", "promoted_date": "20260901", "source": "consensus"},
              {"term": "CXL", "promoted_date": "20260901", "source": "consensus"},
              {"term": "光模組", "promoted_date": "20260901", "source": ""}]       # theme_tracker 升格的不管
    state = {"last_triggered": {"cxl": "20261010"}}
    out = run_state([], state, promotions=promos, event_raw={"themes": [], "near_miss_themes": []})
    assert [x["term"] for x in out["stale_auto_added"]] == ["TPU"]
    run_state([], state, promotions=promos, event_raw={"themes": [{"theme": "TPU", "source": "關鍵字暴增"}]})
    assert state["last_triggered"]["tpu"] == "20261015"


def test_append_to_watchlist(tmp="test_wl_tmp.txt"):
    import os
    open(tmp, "w", encoding="utf-8").write("# 註解\nHVDC\n")
    ws.append_to_watchlist(["TPU", "CXL"], "20261015", path=tmp)
    lines = open(tmp, encoding="utf-8").read().splitlines()
    os.remove(tmp)
    assert lines[:2] == ["# 註解", "HVDC"] and lines[-2:] == ["TPU", "CXL"] and lines[-3].startswith("# ── 共識自動加入")


def test_recent_consensus_themes_for_reverse_lookup():
    promos = [{"term": "TPU", "promoted_date": "20261010", "source": "consensus", "ai_phrases": ["Google TPU架構"]},
              {"term": "CXL", "promoted_date": "20261001", "source": "consensus"},           # 超過7天
              {"term": "光模組", "promoted_date": "20261014", "source": ""}]                 # 非共識
    assert ws.recent_consensus_themes(promos, "20261015") == [{"term": "TPU", "reason": "Google TPU架構"}]


def test_paste_block_excludes_pure_jieba():
    out = run([T("玻璃基板材料", "ai", 3), T("概念股題材", "jieba", 15)],
              counts={"玻璃基板材料": (4, 6), "概念股題材": (20, 50)})
    sug, _ = by_term(out)
    assert sug["概念股題材"]["tier"] == "suggest" and sug["概念股題材"]["cnyes_viable"] is True
    assert out["paste_block"] == "玻璃基板材料"
    assert ws.NOTE_JIEBA_NO_PASTE in sug["概念股題材"]["notes"]


def test_cnyes_truncated_is_unknown():
    """抓滿5頁(100則)都還在近期 → 前段被截斷,不可算成「基數不足」或「可用」。"""
    out = run([T("玻璃基板材料", "ai", 3), T("光纖陣列單元", "ai", 3)],
              counts={"玻璃基板材料": (100, 0), "光纖陣列單元": (40, 30)})
    sug, _ = by_term(out)
    a = sug["玻璃基板材料"]
    assert a["cnyes_truncated"] is True and a["cnyes_viable"] is None
    assert ws.NOTE_CNYES_TRUNC in a["notes"] and ws.NOTE_CNYES_LOW not in a["notes"]
    assert sug["光纖陣列單元"]["cnyes_truncated"] is False and sug["光纖陣列單元"]["cnyes_viable"] is True
    assert out["paste_block"] == "光纖陣列單元"


def test_foreign_and_prefix_company_names():
    out = run([T("海力士", "jieba", 9), T("Marvell", "jieba", 9), T("格羅方德", "jieba", 8),
               T("日月光", "jieba", 10), T("探針卡", "jieba", 6)])
    _, rej = by_term(out)
    assert all(rej[t] == "is_company_name" for t in ("海力士", "Marvell", "格羅方德", "日月光")), rej
    assert "探針卡" not in rej                                     # 題材詞不受影響


def test_ai_failed_keeps_today_history():
    """AI 萃取失敗的那次執行,不可把同一天稍早記下的共識蓋掉(10/15 CCL 案例)。"""
    state = {"consensus_history": {"20261015": ["CCL"]}}
    out = ws.build({"date": "20261015", "ongoing": [T("CCL", "jieba", 3)], "first_seen": []},
                   WATCHLIST, NAME2CODE, STOPWORDS, _method_score, KW_TO_THEME, now_ts=NOW,
                   state=state, ai_failed=True)
    assert state["consensus_history"]["20261015"] == ["CCL"] and out["ai_failed"] is True
    ws.build({"date": "20261016", "ongoing": [], "first_seen": []}, WATCHLIST, NAME2CODE, STOPWORDS,
             _method_score, KW_TO_THEME, now_ts=NOW, state=state, ai_failed=True)
    assert "20261016" not in state["consensus_history"]              # 失敗日不記空白,不算「沒有共識」


def test_watchlist_file_untouched():
    import hashlib
    h = hashlib.md5(open(ws.WATCHLIST_FILE, "rb").read()).hexdigest()
    run([T("玻璃基板材料", "ai", 3)])
    assert hashlib.md5(open(ws.WATCHLIST_FILE, "rb").read()).hexdigest() == h


if __name__ == "__main__":
    import sys
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ✓ {name}")
            except AssertionError as e:
                fails += 1
                print(f"  ✗ {name}: {e}")
    print("全部通過" if not fails else f"{fails} 項失敗")
    sys.exit(1 if fails else 0)
