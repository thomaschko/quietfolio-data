# -*- coding: utf-8 -*-
"""
test_industry_news.py — industry_news.py 與 news_sources 的 sangyo-times 解析單元測試(不需網路)

用法:python test_industry_news.py
"""
import json
import os
import shutil
import tempfile
import datetime as dt

import industry_news as inn
import llm_client
import news_sources

D = dt.date


class Resp:
    def __init__(self, status, payload=None, text=""):
        self.status_code, self._p, self.text, self.headers = status, payload, text, {}
        self.encoding = "utf-8"

    def json(self):
        return self._p


def gemini(obj):
    return Resp(200, {"candidates": [{"content": {"parts": [{"text": json.dumps(obj, ensure_ascii=False)}]}}]})


def groq(obj):
    return Resp(200, {"choices": [{"message": {"content": json.dumps(obj, ensure_ascii=False)}}]})


BY_SOURCE = {
    "cna": ["台積電CoWoS擴產 月產能再增", "SK海力士HBM4送樣", "颱風假明天放不放"],
    "ctee": ["台積電CoWoS擴產 月產能再增", "Needham ups price target"],
    "technews": ["矽光子CPO進度", ""],
    "empty": [],
}
TERMS = ["CoWoS", "HBM", "UPS", "CPO", "矽光子"]


def test_term_regex_for_chinese_titles():
    assert inn.term_regex("CoWoS").search("台積電cowos擴產")           # 緊貼中文、不分大小寫
    assert inn.term_regex("HBM").search("SK海力士HBM4送樣")             # 後面接數字也算
    assert not inn.term_regex("HBM").search("hbm4")                     # 全大寫縮寫大小寫須一致
    assert not inn.term_regex("UPS").search("Needham ups price target")
    assert not inn.term_regex("CPO").search("CPOE")                     # 後面接英文字母不算
    assert inn.term_regex("矽光子").search("矽光子CPO進度")


def test_pool_merges_sources_and_selects_relevant():
    pool = inn.build_pool(BY_SOURCE)
    first = [p for p in pool if p["title"].startswith("台積電CoWoS")][0]
    assert first["sources"] == ["cna", "ctee"]
    rel = inn.select_relevant(pool, TERMS)
    assert [r["title"] for r in rel] == ["台積電CoWoS擴產 月產能再增", "矽光子CPO進度", "SK海力士HBM4送樣"]
    assert rel[1]["terms"] == ["CPO", "矽光子"]
    assert len(inn.select_relevant(pool, TERMS, limit=1)) == 1


def test_industry_sources_and_per_source_quota():
    by = {"cna": [f"CoWoS新聞{i}" for i in range(20)] + ["颱風假"],      # 一般來源:要含 watchlist 詞
          "ctee": [f"CoWoS新聞{i}" for i in range(20)],                   # 跟 cna 重疊 → 2 源,排前面
          "etnews_kr": ["삼성전자 파운드리 수주", "HBM4 공급"],             # 產業來源:不含詞也收
          "trendforce": ["TSMC raises capex"]}
    pool = inn.build_pool(by)
    rel = inn.select_relevant(pool, ["CoWoS", "HBM"], limit=8, per_source=2)
    titles = [r["title"] for r in rel]
    assert "颱風假" not in titles
    assert "삼성전자 파운드리 수주" in titles and "TSMC raises capex" in titles   # 小來源沒被 20 則雙源新聞擠掉
    assert len(rel) == 8
    assert rel[0]["sources"] == ["cna", "ctee"]                                  # 仍依整體排序輸出
    cov = {c["source"]: c for c in inn.source_coverage(by, rel)}
    assert cov["etnews_kr"]["fetched"] == 2 and cov["etnews_kr"]["sent"] == 2 and cov["cna"]["cited"] == 0
    fb = inn.fallback_daily([r for r in rel if not r["terms"]])
    assert fb[0]["topic"].startswith("來源:")


def test_detector_sources_only_fresh_files():
    tmp = tempfile.mkdtemp()
    try:
        def w(name, obj):
            with open(os.path.join(tmp, name), "w", encoding="utf-8") as f:
                json.dump(obj, f, ensure_ascii=False)
        w("event_theme_raw.json", {"generated_at": "2026-10-18 20:00:00", "themes": [
            {"source": "MOPS重訊", "codes": ["2330"], "names": {"2330": "台積電"}, "subject": "董事會決議資本預算"},
            {"source": "關鍵字暴增", "theme": "CPO"}]})
        w("us_8k_events.json", {"generated_at": "2026-10-18 20:01:00", "events": [
            {"code": "SMCI", "role": "Supermicro", "filing_date": "2026-10-15", "item_labels": ["高層異動"]}]})
        w("investor_day_signals.json", {"generated_at": "2026-10-17 20:00:00", "events": [{"titles": ["舊的"]}]})
        w("gnews_term_heat.json", {"generated_at": "2026-10-18 21:00:00", "terms": [
            {"status": "surge", "titles": ["泰碩攻高壓直流"]}, {"status": "quiet", "titles": ["不收"]}]})
        out = inn.detector_sources("2026-10-18", base=tmp)
        assert out["mops"] == ["重訊 台積電(2330):董事會決議資本預算"]
        assert out["us_8k"] == ["美股8-K SMCI(Supermicro) 2026-10-15:高層異動"]
        assert out["investor_events"] == []                                   # 昨天的檔案不用
        assert out["gnews_d2"] == ["泰碩攻高壓直流"]
        assert inn.detector_sources("2026-10-18", base=os.path.join(tmp, "none")) == \
            {"mops": [], "us_8k": [], "investor_events": [], "gnews_d2": []}
    finally:
        shutil.rmtree(tmp)


def _items():
    return inn.select_relevant(inn.build_pool(BY_SOURCE), TERMS)


def test_companies_inside_foreign_names_are_rejected():
    n2c = {"力士": "4923", "台積電": "2330"}
    titles = ["SK海力士展示下一代AI記憶體", "三星將採用台積電為輝達客製化HBM"]
    assert inn._verified_companies(["力士", "台積電"], titles, n2c, {"2330": "台積電"}) == [{"code": "2330", "name": "台積電"}]
    assert inn._verified_companies(["力士"], ["力士營收創高"], n2c, {})[0]["code"] == "4923"


def test_verify_daily_requires_valid_citations():
    ai = {"overview": "重點", "sections": [
        {"topic": "先進封裝", "items": [{"headline": "台積電擴CoWoS", "title_ids": [1, 1, 99],
                                     "companies": ["台積電", "力士", "SK海力士", "華邦電"]},
                                    {"headline": "沒引用", "title_ids": []},
                                    {"headline": "", "title_ids": [2]}]},
        {"topic": "空的", "items": [{"headline": "x", "title_ids": ["a"]}]}]}
    n2c = {"台積電": "2330", "力士": "4923", "華邦電": "2344"}
    secs, stats = inn.verify_daily(ai, _items(), n2c, {"2330": "台積電"})
    assert [s["topic"] for s in secs] == ["先進封裝"]
    it = secs[0]["items"][0]
    assert it["titles"][0]["sources"] == ["cna", "ctee"] and it["n_sources"] == 2
    assert it["codes"] == [{"code": "2330", "name": "台積電"}]     # 力士/華邦電不在引用標題裡、SK海力士查無股名
    assert stats["items_kept"] == 1 and stats["items_dropped"] == 3 and stats["overview"] == "重點"


def test_build_daily_gemini_then_groq_small_then_fallback():
    ok = {"sections": [{"topic": "記憶體", "items": [{"headline": "HBM4送樣", "title_ids": [3]}]}]}
    # Gemini 成功
    chain = llm_client.LLMChain(env={"GEMINI_API_KEY": "k"}, post=lambda *a, **k: gemini(ok), sleep=lambda s: None)
    doc = inn.build_daily("2026-10-18", BY_SOURCE, TERMS, chain)
    assert doc["provider"] == "gemini" and not doc["fallback"] and doc["n_sources"] == 3 and doc["n_sources_total"] == 4
    # Gemini 503 → 縮短版給 Groq
    calls = []

    def post(url, headers, json, timeout):
        calls.append(url)
        return Resp(503) if "googleapis" in url else groq(ok)
    chain = llm_client.LLMChain(env={"GEMINI_API_KEY": "k", "GROQ_API_KEY": "g"}, post=post, sleep=lambda s: None)
    doc = inn.build_daily("2026-10-18", BY_SOURCE, TERMS, chain)
    assert doc["provider"] == "groq:qwen3.8-27b" and doc["stats"]["titles_sent"] == 3
    assert sum("groq" in u for u in calls) == 1                       # 長提示沒有送給 Groq
    # 全失敗 → 原始標題分組
    chain = llm_client.LLMChain(env={"GEMINI_API_KEY": "k"}, post=lambda *a, **k: Resp(400), sleep=lambda s: None)
    doc = inn.build_daily("2026-10-18", BY_SOURCE, TERMS, chain)
    assert doc["fallback"] and doc["sections"][0]["topic"] == "CoWoS"
    assert inn.build_daily("2026-10-18", {"a": []}, TERMS, chain)["sections"] == []


def test_weekly_verify_and_load_week():
    tmp = tempfile.mkdtemp()
    try:
        for d, head in [("2026-10-11", "舊的不算"), ("2026-10-13", "HBM4送樣"), ("2026-10-17", "CoWoS擴產")]:
            doc = {"date": d, "sections": [{"topic": "t", "items": [{"headline": head, "n_sources": 2,
                                                                       "titles": [{"title": head, "sources": ["cna"]}],
                                                                       "codes": [{"code": "2330", "name": "台積電"}]}]}]}
            os.makedirs(os.path.join(tmp, "daily"), exist_ok=True)
            with open(os.path.join(tmp, "daily", f"{d}.json"), "w", encoding="utf-8") as f:
                json.dump(doc, f, ensure_ascii=False)
        docs = inn.load_week(D(2026, 10, 18), out_dir=tmp)
        assert [d["date"] for d in docs] == ["2026-10-13", "2026-10-17"]
        items = inn.week_items(docs)
        themes, ov = inn.verify_weekly({"overview": "本週", "themes": [{"topic": "封裝", "summary": "s", "item_ids": [1, 2, 9]},
                                                                      {"topic": "無引用", "item_ids": []}]}, items)
        assert len(themes) == 1 and len(themes[0]["items"]) == 2 and themes[0]["codes"] == [{"code": "2330", "name": "台積電"}]
        chain = llm_client.LLMChain(env={}, sleep=lambda s: None)
        w = inn.build_weekly(D(2026, 10, 18), docs, chain)
        assert w["week"] == "2026-W42" and w["fallback"] and w["themes"][0]["items"]
    finally:
        shutil.rmtree(tmp)


HEADINDEX = """<html><head><title>
\t2026/10/15(2723号)主なヘッドライン｜EUV投資、DRAMが拡大後押し｜電子デバイス産業新聞（旧半導体産業新聞）
</title></head><body><strong>メニュー</strong><hr>
本文…<div align='right'>（以下、本紙2026年10月15日号1面）</div><br /><hr><br />
<strong>IT・半導体産業</strong><br />
<img src='../images/scn/headlineDtlIcon.gif' />ヴァンガードとNXP、初の300mm工場を開所<br />
<img src='../images/scn/headlineDtlIcon.gif' />京都大学ら、6インチのSiC<br />
<strong>パッケージ・回路基板</strong><br />
<img src='../images/scn/headlineDtlIcon.gif' />TOPPAN、FCBGA基板強化<br />
<br /><hr><br /><strong>インタビュー</strong><br />
<img src='../images/scn/headlineDtlIcon.gif' />日東紡 三上雄広氏<br /></body></html>"""
SCN_TOP = """<span id="a">第675回（2026.10.16）</span><br />
                <a id="b" class="oh4" href="../article.aspx?ID=15230">ソーラーカー実現の道険し</a><br />
<span id="c">（2025.4.25）</span><br />
                <a id="d" class="oh4" href="../article.aspx?ID=13742">米国相互関税がもたらすもの</a>"""


def test_sangyo_headlines_parse_and_daily_window():
    orig_get, orig_dt = news_sources.requests.get, None
    news_sources.requests.get = lambda url, **k: Resp(200, text=HEADINDEX if "headindex" in url else SCN_TOP)
    try:
        hl = news_sources.fetch_sangyo_headlines()
        assert hl["issue_date"] == "2026-10-15" and hl["issue_no"] == 2723 and hl["top"] == "EUV投資、DRAMが拡大後押し"
        assert [s["name"] for s in hl["sections"]] == ["IT・半導体産業", "パッケージ・回路基板"]   # 不含メニュー、インタビュー
        assert hl["sections"][0]["items"][0].startswith("ヴァンガード")
        titles = news_sources._fetch_sangyo_times()
        today = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date()
        if (today - D(2026, 10, 15)).days <= news_sources.SANGYO_RECENT_DAYS:
            assert "TOPPAN、FCBGA基板強化" in titles
        assert "米国相互関税がもたらすもの" not in titles                        # 舊文章不收
        news_sources.requests.get = lambda url, **k: (_ for _ in ()).throw(TimeoutError("x"))
        assert news_sources.fetch_sangyo_headlines() is None and news_sources._fetch_sangyo_times() == []
    finally:
        news_sources.requests.get = orig_get


def test_translate_sangyo_maps_ids():
    hl = {"issue_date": "2026-10-15", "issue_no": 2723, "top": "EUV投資",
          "sections": [{"name": "IT", "items": ["ヴァンガード工場", "SiC"]}]}
    chain = llm_client.LLMChain(env={"GEMINI_API_KEY": "k"}, sleep=lambda s: None,
                                post=lambda *a, **k: gemini({"items": [{"id": 1, "zh": "EUV投資"}, {"id": 2, "zh": "世界先進工廠"},
                                                                       {"id": 9, "zh": "不存在"}]}))
    out = inn.translate_sangyo(hl, chain)
    assert out["top"]["zh"] == "EUV投資" and out["sections"][0]["items"][0] == {"ja": "ヴァンガード工場", "zh": "世界先進工廠"}
    assert out["sections"][0]["items"][1]["zh"] == "" and out["translated"] == 2 and out["total"] == 3


def test_sangyo_cache_for_weekly():
    tmp = tempfile.mkdtemp()
    try:
        cache = os.path.join(tmp, "sangyo_latest.json")
        hl = {"issue_date": "2026-10-15", "issue_no": 2723, "top": "x", "sections": []}
        assert inn.sangyo_for_weekly(D(2026, 10, 17), hl, cache) == hl          # 現抓成功 → 用並存快取
        assert inn.sangyo_for_weekly(D(2026, 10, 24), None, cache) == hl        # 週六抓失敗 → 用快取(9 天內)
        assert inn.sangyo_for_weekly(D(2026, 10, 25), None, cache) is None      # 太舊不用
        assert inn.sangyo_for_weekly(D(2026, 10, 24), None, os.path.join(tmp, "none.json")) is None
    finally:
        shutil.rmtree(tmp)


def test_build_latest_for_gas():
    tmp = tempfile.mkdtemp()
    try:
        os.makedirs(os.path.join(tmp, "daily"))
        os.makedirs(os.path.join(tmp, "weekly"))
        for i in range(9):
            d = f"2026-10-{10 + i:02d}"
            with open(os.path.join(tmp, "daily", f"{d}.json"), "w", encoding="utf-8") as f:
                json.dump({"date": d, "sections": [], "coverage": [{"source": "cna"}]}, f)
        for w in ("2026-W41", "2026-W42"):
            with open(os.path.join(tmp, "weekly", f"{w}.json"), "w", encoding="utf-8") as f:
                json.dump({"week": w}, f)
        out = inn.build_latest(tmp)
        assert [d["date"] for d in out["dailies"]] == [f"2026-10-{x}" for x in range(18, 11, -1)]   # 近 7 份、新到舊
        assert "coverage" in out["dailies"][0] and "coverage" not in out["dailies"][1]
        assert out["weekly"] == {"week": "2026-W42"} and out["weeks"] == ["2026-W42", "2026-W41"]
        empty = inn.build_latest(os.path.join(tmp, "none"))
        assert empty["dailies"] == [] and empty["weekly"] is None
    finally:
        shutil.rmtree(tmp)


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
