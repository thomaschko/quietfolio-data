# -*- coding: utf-8 -*-
"""
============================================================
llm_client.py — 免費 LLM 依序備援(2026-10-16 新增)
============================================================
只用免費額度,依序嘗試;某個 provider 這次執行失敗(重試後仍失敗)就整次跳過它,
換下一個。沒有設定 key 的 provider 直接略過。key 只從環境變數(GitHub Secrets)讀,
不寫進任何檔案。

順序:
  1. Gemini(GEMINI_API_KEY,沿用 ai_theme_discovery 的模型)
  2. Groq qwen/qwen3.8-27b(GROQ_API_KEY)
  3. Groq openai/gpt-oss-120b(同一把 key,額度按模型分開算)
  4. OpenRouter(OPENROUTER_API_KEY,:free 模型;免費約 20 RPM / 50 RPD;尚未實測)
Groq 2026-10-16 GHA 實測(回應標頭):每模型 1,000 次/日、8,000 tokens/分;
  - llama-3.3-70b / qwen3-32b / kimi-k2 已下架(404),帳號可用文字模型只剩
    gpt-oss-120b/20b、qwen3.8-27b
  - qwen3.8-27b 另有每分鐘輸出 1,000 tokens 上限,max_tokens 超過就整筆 429 →
    max_tokens 壓在 900、關閉推理;認公司最準(氮化鎵抓到漢磊,gpt-oss 漏掉)
  - gpt-oss-120b 是推理模型,推理 token 也算在 max_tokens 內;21 則標題時 1,500
    不夠(json_validate_failed)→ reasoning_effort=low、max_tokens 3000
GitHub Models 未列入:2026-10-16 GHA 實測 models.github.ai 任何路徑都只回
text/plain「OK」、舊端點 models.inference.ai.azure.com 已查無 DNS,目前無法使用。
============================================================
"""
import os
import re
import json
import time

import requests

try:
    from ai_theme_discovery import GEMINI_MODEL
except Exception:
    GEMINI_MODEL = "gemini-3.5-flash-lite"

GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODEL = "google/gemma-4-31b-it:free"
RETRY_STATUS = {429, 500, 502, 503, 504}
RETRY_WAIT = 20
RETRY_WAIT_MAX = 30
# 這些狀態只代表「這一題」失敗(提示太長、JSON 產生失敗),不代表 provider 掛了
QUESTION_ONLY_STATUS = {400, 413, 422}


def parse_json(text):
    """容忍 ```json 圍欄與前後雜字,取第一個 {...}。失敗回 None。"""
    if not text:
        return None
    t = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    try:
        return json.loads(t)
    except Exception:
        m = re.search(r"\{.*\}", t, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return None
    return None


def _gemini_request(key, system, user):
    body = {"system_instruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"response_mime_type": "application/json", "temperature": 0.1}}
    return GEMINI_URL, {"Content-Type": "application/json", "x-goog-api-key": key}, body


def _gemini_text(data):
    return data["candidates"][0]["content"]["parts"][0]["text"]


def _openai_request(url, model, json_mode, max_tokens=1200, **extra):
    def build(key, system, user):
        body = {"model": model, "temperature": 0.1, "max_tokens": max_tokens,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                **extra}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        return url, {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, body
    return build


def _openai_text(data):
    return data["choices"][0]["message"]["content"]


PROVIDERS = [
    ("gemini", "GEMINI_API_KEY", _gemini_request, _gemini_text),
    ("groq:qwen3.8-27b", "GROQ_API_KEY",
     _openai_request(GROQ_URL, "qwen/qwen3.8-27b", True, max_tokens=900, reasoning_effort="none"), _openai_text),
    ("groq:gpt-oss-120b", "GROQ_API_KEY",
     _openai_request(GROQ_URL, "openai/gpt-oss-120b", True, max_tokens=3000, reasoning_effort="low"), _openai_text),
    ("openrouter", "OPENROUTER_API_KEY", _openai_request(OPENROUTER_URL, OPENROUTER_MODEL, False), _openai_text),
]


def _retry_wait(r):
    """有 retry-after 標頭就照它(上限 30 秒),否則等 20 秒。"""
    try:
        return min(RETRY_WAIT_MAX, max(1.0, float((getattr(r, "headers", None) or {}).get("retry-after"))))
    except (TypeError, ValueError):
        return RETRY_WAIT


class LLMChain:
    """ask_json(system, user) → (dict 或 None, provider 名稱或 None)。
    post / sleep / env 可替換(測試用)。"""

    def __init__(self, env=None, post=requests.post, sleep=time.sleep, providers=PROVIDERS):
        env = os.environ if env is None else env
        self.providers = [(n, env.get(k, ""), b, x) for n, k, b, x in providers if env.get(k, "")]
        self.down = set()
        self.post, self.sleep = post, sleep
        self.log = []

    @property
    def available(self):
        return [p[0] for p in self.providers]

    def _try(self, name, key, build, extract, system, user):
        url, headers, body = build(key, system, user)
        for attempt in (1, 2):
            try:
                r = self.post(url, headers=headers, json=body, timeout=60)
            except Exception as e:
                self.log.append(f"{name}: {type(e).__name__}")
                return None, True
            if r.status_code == 200:
                try:
                    return parse_json(extract(r.json())), False
                except Exception:
                    self.log.append(f"{name}: 回應格式無法解析")
                    return None, False
            self.log.append(f"{name}: HTTP {r.status_code}")
            if r.status_code in QUESTION_ONLY_STATUS:
                return None, False
            if r.status_code in RETRY_STATUS and attempt == 1:
                self.sleep(_retry_wait(r))
                continue
            return None, True
        return None, True

    def ask_json(self, system, user):
        for name, key, build, extract in self.providers:
            if name in self.down:
                continue
            out, provider_failed = self._try(name, key, build, extract, system, user)
            if out is not None:
                return out, name
            if provider_failed:
                self.down.add(name)   # 這次執行不再用它
        return None, None
