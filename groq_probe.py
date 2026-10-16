# -*- coding: utf-8 -*-
"""一次性探測(跑完即刪):Groq 免費模型在「D2 熱詞 → 標題引用個股」任務的輸出品質與額度標頭。
提示詞與驗證規則與 term_stock_ai.py 相同。不寫任何檔案。"""
import os
import re
import json
import time
from urllib.parse import quote
import xml.etree.ElementTree as ET

import requests
from event_theme_radar import build_name2code

TERMS = ["高壓直流", "矽電容", "NOR Flash", "3奈米", "氮化鎵", "HBF"]
MODELS = ["llama-3.3-70b-versatile", "openai/gpt-oss-120b", "qwen/qwen3-32b", "moonshotai/kimi-k2-instruct"]
SYSTEM = "你是台股研究助理。只根據使用者提供的新聞標題回答,不得使用標題以外的知識,不確定就不要列。"
URL = "https://api.groq.com/openai/v1/chat/completions"


def titles_for(term, n=25):
    url = ("https://news.google.com/rss/search?q=" + quote(f'"{term}" when:20d')
           + "&hl=zh-TW&gl=TW&ceid=TW:zh-Hant")
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=25)
    out, seen = [], set()
    for it in ET.fromstring(r.content).iter("item"):
        t = (it.findtext("title") or "").rsplit(" - ", 1)[0].strip()
        if term.lower() in t.lower() and t not in seen:
            seen.add(t)
            out.append(t)
    return out[:n]


def make_prompt(term, titles):
    lines = "\n".join(f"[{i}] {t}" for i, t in enumerate(titles, 1))
    return (f"題材詞:「{term}」\n新聞標題:\n{lines}\n\n"
            "列出標題中「明確寫出名稱」且與該題材相關的台灣上市櫃公司。只輸出 JSON:"
            '{"companies":[{"name":"標題中的公司名稱","title_ids":[標題編號],"role":"10字內角色"}]}'
            "。標題沒寫到的公司不要列;外國公司不要列;只是同一則標題順帶提到、與題材無關的公司不要列。")


def parse_json(text):
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip())
    t = re.sub(r"<think>.*?</think>", "", t, flags=re.S).strip()
    try:
        return json.loads(t)
    except Exception:
        m = re.search(r"\{.*\}", t, re.S)
        try:
            return json.loads(m.group(0)) if m else None
        except Exception:
            return None


def lookup(name, n2c):
    n = re.sub(r"[\s*＊]|\(\d{4,6}\)|（\d{4,6}）", "", str(name or "")).strip()
    for v in (n, n.replace("-KY", ""), n + "-KY", n + "*"):
        if v in n2c:
            return n2c[v], n
    return None, n


def verify(out, titles, n2c):
    kept, dropped = [], []
    for c in (out or {}).get("companies") or []:
        code, name = lookup(c.get("name"), n2c)
        if not code:
            dropped.append(f"{name}(查無股名)")
            continue
        ids = [int(i) for i in c.get("title_ids") or [] if str(i).isdigit() and 1 <= int(i) <= len(titles)]
        ok = [i for i in ids if name in titles[i - 1] or code in titles[i - 1]]
        (kept if ok else dropped).append(f"{code}{name}({c.get('role', '')})" if ok else f"{name}(引用不符)")
    return kept, dropped


def call(key, model, p, json_mode=True):
    body = {"model": model, "temperature": 0.1, "max_tokens": 1500,
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": p}]}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    t0 = time.time()
    r = requests.post(URL, headers={"Authorization": f"Bearer {key}"}, json=body, timeout=90)
    rl = {k: v for k, v in r.headers.items() if "ratelimit" in k.lower()}
    txt = r.json()["choices"][0]["message"]["content"] if r.status_code == 200 else r.text[:300]
    return r.status_code, round(time.time() - t0, 1), rl, txt


def main():
    key = os.environ.get("GROQ_API_KEY", "")
    print(f"GROQ key: {'yes' if key else 'NO'}")
    if not key:
        return
    r = requests.get("https://api.groq.com/openai/v1/models", headers={"Authorization": f"Bearer {key}"}, timeout=30)
    print(f"models HTTP {r.status_code}")
    if r.status_code == 200:
        print("  " + ", ".join(sorted(m["id"] for m in r.json().get("data", []))))
    n2c, _ = build_name2code()
    print(f"name2code {len(n2c)}")
    for term in TERMS:
        ts = titles_for(term)
        print(f"\n######## {term}:{len(ts)} 則標題")
        for i, t in enumerate(ts, 1):
            print(f"  [{i}] {t}")
        if len(ts) < 2:
            continue
        p = make_prompt(term, ts)
        for m in MODELS:
            code, sec, rl, txt = call(key, m, p)
            print(f"\n  --- {m} HTTP {code} {sec}s")
            if rl and term == TERMS[0]:
                print(f"      ratelimit: {json.dumps(rl)}")
            if code == 200:
                out = parse_json(txt)
                kept, dropped = verify(out, ts, n2c)
                print(f"      保留: {' '.join(kept) or '(無)'}")
                print(f"      剔除: {' '.join(dropped) or '(無)'}")
                if out is None:
                    print("      原文: " + re.sub(r"\s+", " ", txt)[:400])
            else:
                print("      " + re.sub(r"\s+", " ", txt)[:300])
            time.sleep(3)


if __name__ == "__main__":
    main()
