# -*- coding: utf-8 -*-
"""
test_term_stock_ai.py — term_stock_ai.py / llm_client.py 單元測試(不需網路)

用法:python test_term_stock_ai.py
"""
import json

import llm_client
import term_stock_ai as tsa

N2C = {"泰碩": "3338", "愛普*": "6531", "愛普": "6531", "力積電": "6770", "華邦電": "2344", "矽格": "6257"}
TITLES = ["泰碩(3338)攻高壓直流CDU商機,攜手輝達",
          "資料中心掀電力革命 800V 高壓直流躍升新主流",
          "愛普*矽電容放量"]


class Resp:
    def __init__(self, status, payload=None, headers=None):
        self.status_code, self._p, self.headers = status, payload, headers or {}

    def json(self):
        return self._p


def gemini_ok(obj):
    return Resp(200, {"candidates": [{"content": {"parts": [{"text": json.dumps(obj, ensure_ascii=False)}]}}]})


def openai_ok(text):
    return Resp(200, {"choices": [{"message": {"content": text}}]})


def test_verify_keeps_only_cited_listed_companies():
    ai = {"companies": [
        {"name": "泰碩", "title_ids": [1], "role": "CDU"},
        {"name": "輝達", "title_ids": [1], "role": "客戶"},            # 外國公司 → 查無股名
        {"name": "力積電", "title_ids": [2], "role": "亂引用"},        # 引用標題不含名稱
        {"name": "愛普*", "title_ids": [3, 99, "x"], "role": "矽電容"},  # 無效編號忽略
        {"name": "泰碩", "title_ids": [1], "role": "重複"},
    ]}
    kept, dropped = tsa.verify(ai, TITLES, N2C)
    assert [(k["code"], k["title_ids"]) for k in kept] == [("3338", [1]), ("6531", [3])], kept
    assert {d["name"]: d["reason"] for d in dropped} == {"輝達": "查無上市櫃股名", "力積電": "引用標題不含公司名"}


def test_verify_accepts_code_in_title_and_bad_shapes():
    kept, _ = tsa.verify({"companies": [{"name": "泰碩", "title_ids": ["1"]}]}, ["3338 盤中強攻"], N2C)
    assert kept and kept[0]["code"] == "3338"
    assert tsa.verify(None, TITLES, N2C) == ([], [])
    assert tsa.verify({"companies": "壞掉"}, TITLES, N2C) == ([], [])


def test_lookup_unique_prefix_only():
    n2c = {"日月光投控": "3711", "南亞": "1303", "南亞科": "2408", "南亞電": "8046"}
    assert tsa.lookup_code("日月光", n2c) == ("3711", "日月光")
    assert tsa.lookup_code("南亞", n2c) == ("1303", "南亞")          # 精確命中優先
    assert tsa.lookup_code("南", n2c) == (None, "南")                # 太短不猜
    assert tsa.lookup_code("南亞電", n2c)[0] == "8046"
    assert tsa.lookup_code("亞", {"亞力": "1514", "亞光": "3019"})[0] is None
    assert tsa.lookup_code("華新", {"華新": "1605", "華新科": "2492"})[0] == "1605"
    assert tsa.lookup_code("華", {"華新": "1605", "華新科": "2492"})[0] is None
    kept, _ = tsa.verify({"companies": [{"name": "日月光", "title_ids": [1]}]}, ["日月光火拚3D封裝"], n2c)
    assert kept[0]["code"] == "3711"


def test_parse_json_tolerates_fences():
    assert llm_client.parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert llm_client.parse_json('以下是結果:{"a": 2} 謝謝') == {"a": 2}
    assert llm_client.parse_json("不是 JSON") is None
    assert llm_client.parse_json('<think>想一下 {x}</think>{"a": 3}') == {"a": 3}


def test_chain_falls_back_and_marks_provider_down():
    calls = []

    def post(url, headers, json, timeout):
        calls.append(url)
        if "googleapis" in url:
            return Resp(503)
        return openai_ok('{"companies": []}')

    chain = llm_client.LLMChain(env={"GEMINI_API_KEY": "k", "GROQ_API_KEY": "g"}, post=post, sleep=lambda s: None)
    assert chain.available == ["gemini", "groq:qwen3.8-27b", "groq:gpt-oss-120b"]
    assert chain.ask_json("s", "u") == ({"companies": []}, "groq:qwen3.8-27b")
    assert sum("googleapis" in u for u in calls) == 2          # 503 重試一次
    calls.clear()
    assert chain.ask_json("s", "u")[1] == "groq:qwen3.8-27b"
    assert not any("googleapis" in u for u in calls)           # 這次執行不再打 Gemini


def test_groq_bodies_match_tested_settings():
    bodies = []

    def post(url, headers, json, timeout):
        bodies.append(json)
        return Resp(429, headers={"retry-after": "7"})
    waits = []
    chain = llm_client.LLMChain(env={"GROQ_API_KEY": "g"}, post=post, sleep=waits.append)
    assert chain.ask_json("s", "u") == (None, None)
    assert waits == [7.0, 7.0]                                  # 照 retry-after,每個模型重試一次
    q, g = bodies[0], bodies[2]
    assert (q["model"], q["max_tokens"], q["reasoning_effort"]) == ("qwen/qwen3.8-27b", 900, "none")
    assert (g["model"], g["max_tokens"], g["reasoning_effort"]) == ("openai/gpt-oss-120b", 3000, "low")


def test_400_fails_question_but_keeps_provider():
    status = iter([400, 200])
    chain = llm_client.LLMChain(env={"GEMINI_API_KEY": "k"}, sleep=lambda s: None,
                                post=lambda *a, **k: (lambda st: Resp(st) if st != 200 else gemini_ok({"ok": 1}))(next(status)))
    assert chain.ask_json("s", "u") == (None, None)
    assert chain.ask_json("s", "u") == ({"ok": 1}, "gemini")    # 沒被標成掛掉


def test_chain_no_keys_and_all_fail():
    assert llm_client.LLMChain(env={}).available == []
    chain = llm_client.LLMChain(env={"GEMINI_API_KEY": "k"}, post=lambda *a, **k: Resp(400), sleep=lambda s: None)
    assert chain.ask_json("s", "u") == (None, None)


def test_enrich_limits_terms_and_records_meta():
    terms = [{"term": f"詞{i}", "status": "surge"} for i in range(12)] + [{"term": "平淡", "status": "quiet"}]
    tbt = {t["term"]: TITLES for t in terms}
    tbt["詞0"] = TITLES[:1]   # 只有 1 則標題 → 不送 AI
    chain = llm_client.LLMChain(env={"GEMINI_API_KEY": "k"}, sleep=lambda s: None,
                                post=lambda *a, **k: gemini_ok({"companies": [{"name": "泰碩", "title_ids": [1]}]}))
    meta = tsa.enrich(terms, tbt, chain, N2C, sleep=0)
    assert meta["attempted"] == 10 and meta["ok"] == 10 and meta["by_provider"] == {"gemini": 10}
    assert "ai_companies" not in terms[0] and "ai_companies" not in terms[-1]
    assert terms[1]["ai_companies"][0]["code"] == "3338" and terms[1]["ai_provider"] == "gemini"
    assert tsa.enrich(terms, tbt, llm_client.LLMChain(env={}), N2C) == {"skipped": "沒有任何 LLM API key"}


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
