# -*- coding: utf-8 -*-
"""test_eccn_news.py — 電子產品世界「芯闻」(eccn_news.py)翻頁去重、時間窗、快取與失敗處理(不需網路)。"""
import os
import json
import tempfile
import datetime as dt

import eccn_news as en

NOW = dt.datetime(2026, 10, 20, 18, 0)


def li(ts, t="新闻"):
    return f'<li><a href="/consultation/news/{ts}11.html">{t}{ts}芯片测试</a></li>'


def hours_back(h):
    return (NOW - dt.timedelta(hours=h)).strftime("%Y%m%d%H%M%S")


LIST = ("<ul>" + "".join(li(hours_back(h)) for h in range(1, 11)) + "</ul>"
        '<a href="javascript:;" myattr="list" onclick="get_ajax_more_list(\'a_news_more\',\'re\',more_news_url,\'/consultation/news.html\')">展开更多</a>'
        '<script>var more_news_url = "/consultation/ajax/index/action/news.html";</script>')


class R:
    def __init__(self, code, text=""):
        self.status_code, self.text, self.encoding = code, text, "utf-8"


class FakeHttp:
    """模擬站方:列表頁 = 第 1~10 則;AJAX p=k 從第 5+10*(k-2)+1 則開始,每頁 10 則(和列表頁重疊 5 則)。"""
    def __init__(self, list_code=200, ajax_code=200):
        self.calls, self.n, self.list_code, self.ajax_code = [], 0, list_code, ajax_code

    def __call__(self, method, url, **kw):
        self.calls.append((method, url, kw.get("data")))
        self.n += 1
        if method == "GET":
            return R(self.list_code, LIST if self.list_code == 200 else "")
        p = kw["data"]["p"]
        assert kw["data"]["title"] == "list" and url.endswith("/consultation/ajax/index/action/news.html")
        start = 5 + 10 * (p - 2) + 1
        return R(self.ajax_code, "".join(li(hours_back(h)) for h in range(start, start + 10)))


def test_more_button():
    assert en.more_button(LIST) == ("https://m.eccn.com/consultation/ajax/index/action/news.html", "list")


def test_fetch_pages_dedup_and_window():
    with tempfile.TemporaryDirectory() as d:
        http = FakeHttp()
        items, status = en.fetch(NOW, http=http, cache_path=os.path.join(d, "c.json"), sleep=lambda s: None)
        assert status == "ok"
        hrs = sorted(round((NOW - dt.datetime.fromisoformat(x["ts"])).total_seconds() / 3600) for x in items)
        assert hrs == list(range(1, 25))                       # 24 小時內、連續、不重複
        assert [c[2]["p"] for c in http.calls if c[0] == "POST"] == [2, 3]   # p=3 已到 25 小時前 → 停
        assert items[0]["title_tw"].endswith("晶片測試")
        assert json.load(open(os.path.join(d, "c.json"), encoding="utf-8"))["requests"] == 3


def test_fetch_ajax_error_is_partial():
    with tempfile.TemporaryDirectory() as d:
        items, status = en.fetch(NOW, http=FakeHttp(ajax_code=500), cache_path=os.path.join(d, "c.json"),
                                 sleep=lambda s: None)
        assert status == "partial" and len(items) == 10


def test_fetch_failure_uses_cache_then_gives_up():
    with tempfile.TemporaryDirectory() as d:
        cache = os.path.join(d, "c.json")
        en.fetch(NOW, http=FakeHttp(), cache_path=cache, sleep=lambda s: None)
        http = FakeHttp(list_code=502)
        items, status = en.fetch(NOW + dt.timedelta(hours=6), http=http, cache_path=cache, sleep=lambda s: None)
        assert status == "stale" and len(items) == 18 and http.n == 2       # 重試一次;只留時間窗內
        blocked = FakeHttp(list_code=403)
        items, status = en.fetch(NOW + dt.timedelta(days=3), http=blocked, cache_path=cache, sleep=lambda s: None)
        assert status == "stale" and items == [] and blocked.n == 1        # 403 不重試;快取太舊不用


def test_eccn_not_an_industry_source():
    import industry_news as inn
    assert "eccn" not in inn.INDUSTRY_SOURCES and "fsemi" in inn.INDUSTRY_SOURCES
