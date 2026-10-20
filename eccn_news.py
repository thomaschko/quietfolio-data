# -*- coding: utf-8 -*-
"""
eccn_news.py — 電子產品世界 m.eccn.com「芯闻」標題,只給產業新聞日報用(2026-10-20 新增)
============================================================
2026-10-20 三次探測(eccn_probe / eccn_list_probe / eccn_page_probe):
  robots 允許(只禁 Sogou/Bingbot/SemrushBot 等特定爬蟲與 /accounts、/webinar/*);
  列表頁 /consultation/news.html 10 則只涵蓋約 3 小時、每天約 30~50 則;
  翻頁 = 站方 get_ajax_list.js 的「展开更多」:POST more_news_url,data {p: 頁碼, title: 按鈕 myattr};
  AJAX 頁是照首頁 5 則設計的(p=2 從第 6 則開始),和列表頁重疊 5 則 → 用網址去重,三頁連續無斷層;
  發佈時間 = 網址前 14 碼(YYYYmmddHHMMSS,中國時間,與文章頁一致);首頁曾回 502。
做法:每天抓列表頁 + POST p=2..MAX_PAGE(最多 5 個請求、間隔 ≥ 2 秒),抓到超過 WINDOW_HOURS 前的就停;
只收「芯闻」(不收「芯品」產品促銷)。簡體標題用 fsemi_news.to_tw 轉台灣用語。
抓不到 → 用 industry_news/eccn_latest.json 快取裡仍在時間窗內的標題,標 stale。
只用標題、時間、連結,不抓文章頁、不存全文;誠實 User-Agent,不加其他 header。
不進雷達、也不列入 industry_news.INDUSTRY_SOURCES(內容較雜:汽車/手機/通訊/機器人),
只有命中 watchlist 詞的標題才會進日報。
============================================================
"""
import os
import re
import json
import time
import datetime as dt
from urllib.parse import urljoin

import requests

from fsemi_news import to_tw

BASE = "https://m.eccn.com"
LIST_URL = BASE + "/consultation/news.html"
DEFAULT_AJAX = BASE + "/consultation/ajax/index/action/news.html"
UA = "quietfolio/1.0 (+https://github.com/thomaschko/quietfolio-data; daily headline list, <= 5 requests/day)"
MAX_PAGE = 5
WINDOW_HOURS = 24
MIN_GAP = 2.0
CACHE = os.path.join("industry_news", "eccn_latest.json")
CN = dt.timezone(dt.timedelta(hours=8))           # 網址時間是中國時間(UTC+8,與台北相同)
BLOCK = (401, 403, 451)
ITEM_RE = re.compile(r"""<a[^>]+href=["']([^"']*?/consultation/news/(\d{14})\d*\.html)["'][^>]*>(.*?)</a>""", re.S | re.I)


def parse_items(html, base=LIST_URL):
    """HTML 片段 → [{title, url, ts}](依頁面順序、網址去重;ts = 網址前 14 碼)。"""
    out, seen = [], set()
    for m in ITEM_RE.finditer(html or ""):
        url = urljoin(base, m.group(1))
        title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(3))).strip()
        if url in seen or len(title) < 6:
            continue
        try:
            ts = dt.datetime.strptime(m.group(2), "%Y%m%d%H%M%S").strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        seen.add(url)
        out.append({"title": title[:150], "url": url, "ts": ts})
    return out


def more_button(html, base=LIST_URL):
    """「展开更多」按鈕 → (ajax_url, myattr)。找不到 URL 變數就用探測時看到的預設端點。"""
    btn = re.search(r"""<a[^>]*get_ajax_more_list\(([^)]*)\)[^>]*>""", html or "", re.I)
    if not btn:
        return None, None
    attr = re.search(r"""myattr=["']([^"']*)["']""", btn.group(0))
    args = [a.strip().strip("'\"") for a in btn.group(1).split(",")]
    var = args[2] if len(args) >= 3 else "more_news_url"
    vm = re.search(r"""var\s+%s\s*=\s*["']([^"']+)["']""" % re.escape(var), html)
    return (urljoin(base, vm.group(1)) if vm else DEFAULT_AJAX), (attr.group(1) if attr else "")


def _read_cache(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _in_window(items, now, hours=WINDOW_HOURS):
    lo = (now - dt.timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    return [x for x in items if x.get("ts") and x["ts"] >= lo]


class _Http:
    """節流的 GET/POST(間隔 ≥ MIN_GAP 秒)。"""

    def __init__(self, sleep=time.sleep, clock=time.time, session=None):
        self.s = session or requests.Session()
        self.s.headers.update({"User-Agent": UA})
        self.sleep, self.clock, self.last, self.n = sleep, clock, None, 0

    def __call__(self, method, url, **kw):
        if self.last is not None:
            w = MIN_GAP - (self.clock() - self.last)
            if w > 0:
                self.sleep(w)
        self.n += 1
        try:
            return self.s.request(method, url, timeout=25, **kw)
        finally:
            self.last = self.clock()


def fetch(now=None, http=None, cache_path=CACHE, sleep=time.sleep):
    """→ (items, status)。items:[{title(原文), title_tw, url, ts}](新到舊);status:ok / partial / stale / failed。"""
    now = now or dt.datetime.now(CN).replace(tzinfo=None)
    http = http or _Http(sleep=sleep)
    lo = (now - dt.timedelta(hours=WINDOW_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
    err, got, html = None, [], None
    for attempt in range(2):                          # 2026-10-20 探測時首頁曾回 502 → 多試一次
        if attempt:
            sleep(5)
        try:
            r = http("GET", LIST_URL)
            if r.status_code == 200:
                r.encoding = r.encoding or "utf-8"
                html = r.text
                break
            err = f"列表頁 HTTP {r.status_code}"
            if r.status_code in BLOCK:
                break
        except Exception as e:
            err = f"列表頁 {type(e).__name__}: {e}"
    status = "ok"
    if html is not None:
        got = parse_items(html)
        ajax, attr = more_button(html)
        seen = {x["url"] for x in got}
        if not got:
            err = "列表頁沒有解析出任何條目"
        elif ajax:
            for p in range(2, MAX_PAGE + 1):
                if min(x["ts"] for x in got) < lo:
                    break                             # 已涵蓋整個時間窗
                try:
                    rr = http("POST", ajax, data={"p": p, "title": attr})
                except Exception as e:
                    print(f"  ⚠ eccn p={p}:{type(e).__name__}: {e}")
                    status = "partial"
                    break
                if rr.status_code != 200:
                    print(f"  ⚠ eccn p={p}:HTTP {rr.status_code}")
                    status = "partial"
                    break
                rr.encoding = rr.encoding or "utf-8"
                page = [x for x in parse_items(rr.text) if x["url"] not in seen]
                if not page:
                    break
                seen |= {x["url"] for x in page}
                got += page
    if got:
        got.sort(key=lambda x: x["ts"], reverse=True)
        for x in got:
            x["title_tw"] = to_tw(x["title"])
        try:
            os.makedirs(os.path.dirname(cache_path) or ".", exist_ok=True)
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump({"fetched_at": now.strftime("%Y-%m-%d %H:%M"), "status": status,
                           "requests": http.n, "items": got}, f, ensure_ascii=False, indent=1)
        except Exception as e:
            print(f"  ⚠ eccn 快取寫入失敗:{e}")
        return _in_window(got, now), status
    print(f"  ⚠ eccn 抓取失敗:{err}")
    cached = _read_cache(cache_path)
    if cached and cached.get("items"):
        rec = _in_window(cached["items"], now)
        print(f"  … 改用快取({cached.get('fetched_at')}),時間窗內 {len(rec)} 則,標 stale")
        return rec, "stale"
    return [], "failed"


def titles_for_pool(items):
    return [x["title_tw"] for x in items if x.get("title_tw")]
