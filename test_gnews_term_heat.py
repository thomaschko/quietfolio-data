# -*- coding: utf-8 -*-
"""
test_gnews_term_heat.py — gnews_term_heat.py 單元測試(不需網路)

用法:python test_gnews_term_heat.py
"""
import datetime as dt

import gnews_term_heat as g

TPE = g.TPE
RAW = {"window": {"recent_start": "2026-10-14"},
       "keyword_stats": {"混合鍵合": {"enough_base": False}, "冷板": {"enough_base": False},
                         "CoWoS": {"enough_base": True}, "EMIB-T": {"enough_base": False},
                         "LPO": {"enough_base": False}}}
RS, BS = g.window_from_raw(RAW)


def at(day, hour=12):
    return dt.datetime(2026, 10, day, hour, tzinfo=TPE)


def it(title, day):
    return {"title": title, "pub": at(day)}


def test_window_and_targets():
    assert dt.datetime.fromtimestamp(RS, TPE).date() == dt.date(2026, 10, 14)
    assert RS - BS == 17 * 86400
    assert g.target_terms(RAW) == ["混合鍵合", "冷板", "EMIB-T", "LPO"]        # enough_base 的不查


def test_use_us_only_for_digit_or_hyphen():
    assert g.use_us("EMIB-T") and g.use_us("BlueField-4") and g.use_us("PCIe 6.0")
    assert not g.use_us("LPO") and not g.use_us("UEC") and not g.use_us("Tomahawk") and not g.use_us("混合鍵合")


def test_count_only_exact_title_and_dedupe():
    items = [it("應材與貝思合攻混合鍵合技術 - 樂聯網", 15),
             it("應材與貝思合攻混合鍵合技術 - LINE TODAY", 15),          # 同標題不同媒體 → 去重
             it("混合銅鍵合——人工智慧時代的製造業槓桿 - 電子工程專輯", 15),  # 不含完整詞
             it("三星HBM 混合鍵合技術或成反超關鍵 - BigGo", 5),
             it("台積電混合鍵合進度 - 經濟日報", 6),
             {"title": "很久以前的混合鍵合 - 某報", "pub": dt.datetime(2026, 9, 20, tzinfo=TPE)}]  # 早於基線期
    st = g.count_term("混合鍵合", items, RS, BS)
    assert st["recent"] == 1 and st["earlier"] == 2, st
    assert st["titles"][0] == "應材與貝思合攻混合鍵合技術"                  # 去掉「 - 媒體名」


def test_status_rules():
    surge = [it(f"冷板新聞{i}", 15) for i in range(4)] + [it(f"冷板舊聞{i}", 5 + i) for i in range(3)]
    assert g.count_term("冷板", surge, RS, BS)["status"] == "surge"
    new = [it(f"HBF新聞{i}", 15) for i in range(3)]
    assert g.count_term("HBF", new, RS, BS)["status"] == "new"
    quiet = [it("冷板新聞", 15)]
    assert g.count_term("冷板", quiet, RS, BS)["status"] == "quiet"


def test_build_queries_us_only_when_allowed_and_extracts_codes():
    calls = []

    def fake(term, loc):
        calls.append((term, "US" if loc == g.LOC_US else "TW"))
        return [it(f"{term} 量產 奇鋐(3017) - 工商時報", 15), it(f"{term} 出貨 - 經濟日報", 15)]
    out = g.build(RAW, fetcher=fake, name2code={"奇鋐": "3017"},
                  extract=lambda kw, texts, n2c: (["3017"], {}), sleep=0)
    assert ("EMIB-T", "US") in calls and ("LPO", "US") not in calls and ("冷板", "US") not in calls
    assert all(t not in [c[0] for c in calls] for t in ["CoWoS"])
    terms = {t["term"]: t for t in out["terms"]}
    assert terms["冷板"]["status"] == "new" and terms["冷板"]["codes"] == ["3017"]
    assert sum(out["counts"].values()) == out["n_terms"] == 4


def test_fetch_error_does_not_crash():
    def bad(term, loc):
        raise RuntimeError("HTTP 503")
    out = g.build(RAW, fetcher=bad, sleep=0)
    assert out["n_terms"] == 4 and all(t["status"] == "quiet" for t in out["terms"]) and out["errors"]


def test_enrich_hook_gets_all_titles_and_errors_are_contained():
    def fake(term, loc):
        return [it(f"{term} 新聞{i}", 15) for i in range(5)]
    seen = {}

    def enrich(terms, tbt):
        seen.update(tbt)
        return {"ok": 1}
    out = g.build(RAW, fetcher=fake, sleep=0, enrich=enrich)
    assert out["ai_lookup"] == {"ok": 1}
    assert all(len(v) == 5 for v in seen.values()) and all(len(t["titles"]) == 3 for t in out["terms"])

    def boom(terms, tbt):
        raise ValueError("x")
    assert g.build(RAW, fetcher=fake, sleep=0, enrich=boom)["ai_lookup"]["skipped"].startswith("例外")
    assert g.build(RAW, fetcher=fake, sleep=0)["ai_lookup"] is None


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
