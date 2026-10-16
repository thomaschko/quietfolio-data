# -*- coding: utf-8 -*-
"""一次性探測(跑完即刪):GitHub Models / Groq / OpenRouter 免費模型能否從 GHA 呼叫、
額度標頭、以及「D2 熱詞 → 標題引用個股」任務的實際輸出品質。不寫任何檔案。"""
import os
import re
import json
import time
from urllib.parse import quote
import xml.etree.ElementTree as ET

import requests

TERMS = ["高壓直流", "矽電容", "NOR Flash", "3奈米", "氮化鎵"]
UA = {"User-Agent": "Mozilla/5.0"}
SYS = "你是台股研究助理。只根據使用者提供的新聞標題回答,不得使用標題以外的知識,不確定就不要列。"


def titles_for(term, n=25):
    url = ("https://news.google.com/rss/search?q=" + quote(f'"{term}" when:20d')
           + "&hl=zh-TW&gl=TW&ceid=TW:zh-Hant")
    r = requests.get(url, headers=UA, timeout=25)
    out, seen = [], set()
    for it in ET.fromstring(r.content).iter("item"):
        t = (it.findtext("title") or "").rsplit(" - ", 1)[0].strip()
        if term.lower() in t.lower() and t not in seen:
            seen.add(t)
            out.append(t)
    return out[:n]


def prompt(term, titles):
    lines = "\n".join(f"[{i}] {t}" for i, t in enumerate(titles, 1))
    return (f"題材詞:「{term}」\n新聞標題:\n{lines}\n\n"
            "列出標題中「明確寫出名稱」且與該題材相關的台灣上市櫃公司。只輸出 JSON:"
            '{"companies":[{"name":"標題中的公司名稱","title_ids":[標題編號],"role":"10字內角色"}]}'
            "。標題沒寫到的公司不要列;外國公司不要列。")


def openai_style(url, key, model, p, extra_headers=None):
    h = {"Authorization": f"Bearer {key}", "Content-Type": "application/json", **(extra_headers or {})}
    body = {"model": model, "temperature": 0.1, "max_tokens": 800,
            "messages": [{"role": "system", "content": SYS}, {"role": "user", "content": p}]}
    t0 = time.time()
    r = requests.post(url, headers=h, json=body, timeout=90)
    dt_ = round(time.time() - t0, 1)
    rl = {k: v for k, v in r.headers.items() if "ratelimit" in k.lower() or k.lower() == "retry-after"}
    if r.status_code != 200:
        return r.status_code, dt_, rl, r.text[:300]
    try:
        return r.status_code, dt_, rl, r.json()["choices"][0]["message"]["content"][:900]
    except Exception as e:
        return r.status_code, dt_, rl, f"parse fail {e}: {r.text[:200]}"


def gemini(p):
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return "no key", 0, {}, ""
    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent"
    body = {"system_instruction": {"parts": [{"text": SYS}]},
            "contents": [{"role": "user", "parts": [{"text": p}]}],
            "generationConfig": {"response_mime_type": "application/json", "temperature": 0.1}}
    t0 = time.time()
    r = requests.post(url, headers={"x-goog-api-key": key}, json=body, timeout=90)
    dt_ = round(time.time() - t0, 1)
    if r.status_code != 200:
        return r.status_code, dt_, {}, r.text[:200]
    return 200, dt_, {}, r.json()["candidates"][0]["content"]["parts"][0]["text"][:900]


def main():
    gh = os.environ.get("GITHUB_TOKEN", "")
    groq = os.environ.get("GROQ_API_KEY", "")
    ork = os.environ.get("OPENROUTER_API_KEY", "")
    print(f"keys: github_token={'yes' if gh else 'no'} groq={'yes' if groq else 'no'} openrouter={'yes' if ork else 'no'}")

    # ---- GitHub Models catalog ----
    try:
        r = requests.get("https://models.github.ai/catalog/models",
                         headers={"Authorization": f"Bearer {gh}", "Accept": "application/vnd.github+json",
                                  "X-GitHub-Api-Version": "2022-11-28"}, timeout=30)
        print(f"\n== GitHub Models catalog HTTP {r.status_code}")
        if r.status_code == 200:
            for m in r.json():
                print(f"  {m.get('id')} | tier={m.get('rate_limit_tier')} | {','.join(m.get('supported_output_modalities') or [])}")
        else:
            print(r.text[:300])
    except Exception as e:
        print(f"catalog error {e}")

    # ---- OpenRouter free model list(公開端點,不需 key)----
    try:
        r = requests.get("https://openrouter.ai/api/v1/models", timeout=30)
        print(f"\n== OpenRouter models HTTP {r.status_code}")
        if r.status_code == 200:
            free = [m for m in r.json().get("data", []) if m.get("id", "").endswith(":free")]
            for m in free:
                print(f"  {m['id']} | ctx={m.get('context_length')}")
    except Exception as e:
        print(f"openrouter list error {e}")

    # ---- task test ----
    gh_models = ["openai/gpt-4.1-mini", "openai/gpt-4o-mini", "deepseek/DeepSeek-V3-0324",
                 "meta/Llama-3.3-70B-Instruct", "microsoft/Phi-4"]
    groq_models = ["llama-3.3-70b-versatile", "openai/gpt-oss-120b", "qwen/qwen3-32b"]
    for term in TERMS[:3]:
        ts = titles_for(term)
        print(f"\n######## {term}:{len(ts)} 則標題")
        for i, t in enumerate(ts, 1):
            print(f"  [{i}] {t}")
        p = prompt(term, ts)
        res = [("gemini", *gemini(p))]
        if gh:
            for m in gh_models:
                res.append((f"gh:{m}", *openai_style("https://models.github.ai/inference/chat/completions", gh, m, p)))
                time.sleep(4)
        if groq:
            for m in groq_models:
                res.append((f"groq:{m}", *openai_style("https://api.groq.com/openai/v1/chat/completions", groq, m, p)))
                time.sleep(2)
        if ork:
            res.append(("or:openrouter/free", *openai_style("https://openrouter.ai/api/v1/chat/completions", ork,
                                                            "openrouter/free", p)))
        for name, code, sec, rl, txt in res:
            print(f"\n  --- {name} HTTP {code} {sec}s")
            if rl:
                print(f"      ratelimit: {json.dumps(rl, ensure_ascii=False)}")
            print("      " + re.sub(r"\s+", " ", str(txt))[:700])
        time.sleep(3)


if __name__ == "__main__":
    main()
