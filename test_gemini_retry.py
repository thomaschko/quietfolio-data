# -*- coding: utf-8 -*-
"""
test_gemini_retry.py — ai_theme_discovery.call_gemini 暫時性錯誤重試(不需網路)

用法:python test_gemini_retry.py
"""
import json

import ai_theme_discovery as atd


class Resp:
    def __init__(self, code, payload=None):
        self.status_code = code
        self.text = json.dumps(payload or {"error": {"code": code}})
        self._payload = payload

    def json(self):
        return self._payload


def ok(obj):
    return Resp(200, {"candidates": [{"content": {"parts": [{"text": json.dumps(obj)}]}}]})


def run_with(responses):
    calls = []

    def fake_post(*a, **k):
        calls.append(1)
        return responses[len(calls) - 1]
    atd.requests.post, atd.time.sleep = fake_post, lambda s: None
    atd.GEMINI_KEY = "x"
    return atd.call_gemini("t"), len(calls)


def test_retry_503_then_ok():
    result, n = run_with([Resp(503), ok({"themes": []})])
    assert result == {"themes": []} and n == 2


def test_gives_up_after_three_tries():
    result, n = run_with([Resp(503), Resp(503), Resp(503)])
    assert result is None and n == 3


def test_no_retry_on_auth_error():
    result, n = run_with([Resp(403)])
    assert result is None and n == 1


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
