# -*- coding: utf-8 -*-
"""test_fsemi_news.py — 未來半導體標題(fsemi_news.py)解析、簡轉繁、快取與失敗處理(不需網路)。"""
import os
import json
import tempfile
import datetime as dt

import fsemi_news as fn

TODAY = dt.date(2026, 10, 20)
HTML = """<html><body><ul>
<li><a href="/cms/xinwenkuaixun/5672.html" title="源归材料完成数亿元融资">源归材料完成数亿元融资</a><span>2026-10-20</span></li>
<li><a href="/cms/xinwenkuaixun/5670.html">三星将与台积电合作生产HBM，英伟达推动供应链标准化</a><span>2026-10-19</span></li>
<li><a href="/cms/xinwenkuaixun/5670.html">三星将与台积电合作生产HBM(重複連結)</a><span>2026-10-19</span></li>
<li><a href="/cms/xinwenkuaixun/5660.html">芯爱科技五年砺行</a><span>2026-10-15</span></li>
<li><a href="/cms/other/1.html">不是快訊</a><span>2026-10-20</span></li>
</ul></body></html>"""


class R:
    def __init__(self, code, text=""):
        self.status_code, self.text, self.encoding = code, text, None


def test_parse_list_ids_dates_dedup():
    items = fn.parse_list(HTML)
    assert [x["id"] for x in items] == [5672, 5670, 5660]
    assert [x["date"] for x in items] == ["2026-10-20", "2026-10-19", "2026-10-15"]
    assert items[0]["url"] == "https://www.fsemi.tech/cms/xinwenkuaixun/5672.html"


def test_to_tw_taiwan_usage():
    t = fn.to_tw("三星将与台积电合作生产HBM，英伟达推动供应链标准化；存储芯片")
    assert "台積電" in t and "輝達" in t and "晶片" in t and "臺" not in t


def test_fetch_ok_filters_recent_and_writes_cache():
    with tempfile.TemporaryDirectory() as d:
        cache = os.path.join(d, "fsemi_latest.json")
        items, status = fn.fetch(TODAY, get=lambda u: R(200, HTML), cache_path=cache)
        assert status == "ok" and [x["id"] for x in items] == [5672, 5670]      # 10-15 超過 3 天
        assert json.load(open(cache, encoding="utf-8"))["items"][0]["title_tw"] == "源歸材料完成數億元融資"
        assert fn.titles_for_pool(items)[1].startswith("三星將與台積電")


def test_fetch_failure_uses_cache_as_stale():
    with tempfile.TemporaryDirectory() as d:
        cache = os.path.join(d, "fsemi_latest.json")
        fn.fetch(TODAY, get=lambda u: R(200, HTML), cache_path=cache)
        calls = []
        items, status = fn.fetch(TODAY + dt.timedelta(days=1), get=lambda u: calls.append(u) or R(502),
                                 cache_path=cache, sleep=lambda s: None)
        assert status == "stale" and [x["id"] for x in items] == [5672, 5670] and len(calls) == 2   # 重試一次
        items, status = fn.fetch(TODAY + dt.timedelta(days=5), get=lambda u: R(502), cache_path=cache,
                                 sleep=lambda s: None)
        assert status == "stale" and items == []                                 # 快取太舊 → 不用


def test_fetch_blocked_does_not_retry():
    with tempfile.TemporaryDirectory() as d:
        calls = []
        items, status = fn.fetch(TODAY, get=lambda u: calls.append(u) or R(403),
                                 cache_path=os.path.join(d, "c.json"), sleep=lambda s: None)
        assert status == "failed" and items == [] and len(calls) == 1


def test_parse_list_date_formats():
    for d in ("2026年10月20日", "2026/10/20", "2026.10.20", "2026 - 10 - 20"):
        html = f'<li><a href="/cms/xinwenkuaixun/1.html">半导体设备国产化新闻</a><span>{d}</span></li>'
        assert fn.parse_list(html)[0]["date"] == "2026-10-20", d


def test_fetch_no_date_is_reported():
    html = '<li><a href="/cms/xinwenkuaixun/1.html">半导体设备国产化新闻</a></li>'
    with tempfile.TemporaryDirectory() as d:
        items, status = fn.fetch(TODAY, get=lambda u: R(200, html), cache_path=os.path.join(d, "c.json"))
        assert status == "no_date" and items == []


def test_to_tw_semiconductor_equipment():
    t = fn.to_tw("屹唐股份北京工厂第1000台机台发车 设备量产能力迈上新台阶；美国半导体制造；半导体设备企业")
    assert "設備量產" in t and "半導體製造" in t and "半導體設備企業" in t
