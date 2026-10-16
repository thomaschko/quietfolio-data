# -*- coding: utf-8 -*-
"""phrase_probe.py — 一次性(測完即還原):含空格 watchlist 詞,偵測源1 修正前(原始)vs 修正後(只計完整詞)"""
import re
import time
from event_theme_radar import cnyes_search, window_bounds, load_tw_holidays, RECENT_DAYS, BASELINE_DAYS

terms = [ln.strip() for ln in open("themes_watchlist.txt", encoding="utf-8")
         if ln.strip() and not ln.startswith("#") and " " in ln.strip()]
now = int(time.time())
rs, bs, _ = window_bounds(now, load_tw_holidays())


def norm(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", s or "")).lower()


def stat(news):
    rec = [n for n in news if n["publishAt"] >= rs]
    ear = [n for n in news if n["publishAt"] < rs]
    ed = len(ear) / (BASELINE_DAYS - RECENT_DAYS) if ear else 0.01
    return len(rec), round(ed, 2), round((len(rec) / RECENT_DAYS) / ed, 2)


print(f"{'詞':16s} 原始(近3日,前段日均,暴增比)  →  只計完整詞")
for kw in terms:
    news = cnyes_search(kw, bs)
    full = [n for n in news if norm(kw) in norm(n["title"] + " " + n["summary"])]
    print(f"{kw:16s} {stat(news)}  →  {stat(full)}   (回傳{len(news)} 含完整詞{len(full)})")
    time.sleep(0.3)
