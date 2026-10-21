# -*- coding: utf-8 -*-
"""
fsemi_news.py — 未來半導體(fsemi.tech)「新聞快訊」標題,只給產業新聞日報用(2026-10-20 新增)
============================================================
2026-10-20 探測(fsemi_probe.py)結果 ok:robots 只禁 /bin/、條款只有「版权所有」、列表 10 則/頁、
日期 YYYY-MM-DD(列表上的日期可靠,文章頁的日期不可靠)、近 14 天約 1.8 則/天。
做法:每天抓一次列表第 1 頁(1 個請求,約涵蓋 5 天),只取近 RECENT_DAYS 天,
簡體標題用 OpenCC s2twp 轉台灣用語(再把「臺」→「台」、「英偉達」→「輝達」)給 watchlist 比對與 AI 整理。
抓不到 → 用 industry_news/fsemi_latest.json 快取裡仍在 RECENT_DAYS 內的標題,標 stale。
只用標題、日期、連結,不抓文章頁、不存全文;誠實 User-Agent。
不進雷達:不在 news_sources.fetch_titles_by_source 裡,只由 industry_news.py 加進標題池。
============================================================
"""
import os
import re
import json
import time
import datetime as dt

import requests

LIST_URL = "https://www.fsemi.tech/cms/xinwenkuaixun.html"
UA = "quietfolio/1.0 (+https://github.com/thomaschko/quietfolio-data; daily headline list, 1 request/day)"
RECENT_DAYS = 3
CACHE = os.path.join("industry_news", "fsemi_latest.json")
TPE = dt.timezone(dt.timedelta(hours=8))
ART_RE = re.compile(r"/cms/xinwenkuaixun/(\d+)\.html")
# 2026-10-20 首次實跑:列表日期不是「-」分隔(探測時程式已正規化成 YYYY-MM-DD,看不出原格式)→ 全部解析成 None、
# 0 則進日報。改成和 fsemi_probe.py 一樣接受 - / . 年月日
DATE_RE = re.compile(r"(20\d{2})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})")
# OpenCC s2twp 之後的台灣慣用寫法(只放確定的)
# s2twp 會把「设备」(連繁體「設備」)都轉成「裝置」,半導體語境要「設備」;「制造」有時沒轉成「製造」
TW_FIX = (("臺", "台"), ("英偉達", "輝達"), ("制造", "製造"), ("半導體裝置", "半導體設備"),
          ("裝置量產", "設備量產"), ("裝置廠", "設備廠"), ("裝置商", "設備商"), ("裝置企業", "設備企業"),
          ("裝置材料", "設備材料"), ("裝置國產", "設備國產"))

_cc = None


def to_tw(text):
    """簡 → 繁(台灣用語)。沒有 opencc 套件 → 原文照用(標題仍可給 AI 讀,只是 watchlist 比對會變差)。"""
    global _cc
    if _cc is None:
        try:
            from opencc import OpenCC
            _cc = OpenCC("s2twp")
        except Exception:
            _cc = False
    out = _cc.convert(text) if _cc else text
    for a, b in TW_FIX:
        out = out.replace(a, b)
    return out


def parse_list(html):
    """列表頁 → [{id, title, date, url}](依頁面順序,id 去重)。"""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        m = ART_RE.search(a["href"])
        if not m:
            continue
        aid = int(m.group(1))
        title = (a.get("title") or a.get_text(" ", strip=True)).strip()
        if aid in seen or len(title) < 4:
            continue
        date, box = None, a
        for _ in range(5):
            box = box.parent
            if box is None:
                break
            d = DATE_RE.search(box.get_text(" ", strip=True))
            if d:
                date = dt.date(int(d.group(1)), int(d.group(2)), int(d.group(3))).isoformat()
                break
        seen.add(aid)
        out.append({"id": aid, "title": title[:150], "date": date,
                    "url": f"https://www.fsemi.tech/cms/xinwenkuaixun/{aid}.html"})
    return out


def _recent(items, today, days=RECENT_DAYS):
    lo = (today - dt.timedelta(days=days - 1)).isoformat()
    return [x for x in items if x.get("date") and x["date"] >= lo]


def _read_cache(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def fetch(today=None, get=None, cache_path=CACHE, sleep=time.sleep):
    """→ (items, status)。items:[{id, title(原文), title_tw, date, url}];status:ok / stale / failed。"""
    today = today or dt.datetime.now(TPE).date()
    get = get or (lambda u: requests.get(u, headers={"User-Agent": UA}, timeout=25))
    items, err = None, None
    for attempt in range(2):                          # 2026-10-20 探測時 eccn 曾回 502,這裡也多試一次
        if attempt:
            sleep(5)
        try:
            r = get(LIST_URL)
            if r.status_code == 200:
                r.encoding = "utf-8"
                items = parse_list(r.text)
                if items:
                    break
                err = "列表沒有解析出文章"
            else:
                err = f"HTTP {r.status_code}"
                if r.status_code in (401, 403, 451):      # 被擋就不再重試、不繞過
                    break
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
    if items:
        for x in items:
            x["title_tw"] = to_tw(x["title"])
        try:
            os.makedirs(os.path.dirname(cache_path) or ".", exist_ok=True)
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump({"fetched_at": dt.datetime.now(TPE).strftime("%Y-%m-%d %H:%M"), "items": items},
                          f, ensure_ascii=False, indent=1)
        except Exception as e:
            print(f"  ⚠ fsemi 快取寫入失敗:{e}")
        if not any(x.get("date") for x in items):
            print(f"  ⚠ fsemi 列表有 {len(items)} 則但日期全部解析不到(版面可能改了),這次不收")
            return [], "no_date"
        return _recent(items, today), "ok"
    print(f"  ⚠ fsemi 抓取失敗:{err}")
    cached = _read_cache(cache_path)
    if cached and cached.get("items"):
        rec = _recent(cached["items"], today)
        print(f"  … 改用快取({cached.get('fetched_at')}),近 {RECENT_DAYS} 天 {len(rec)} 則,標 stale")
        return rec, "stale"
    return [], "failed"


def titles_for_pool(items):
    """給 industry_news 標題池:用轉成台灣用語的標題。"""
    return [x["title_tw"] for x in items if x.get("title_tw")]
