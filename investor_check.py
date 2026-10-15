# -*- coding: utf-8 -*-
"""investor_check.py — 一次性:在 GHA 確認 news_sources._fetch_investor() 回傳 >=5 則(測完即還原)"""
import inspect
import re
import sys

import news_sources as ns

titles = ns._fetch_investor()
print(f"investor 標題數:{len(titles)}")
for t in titles[:20]:
    print("  ·", t)
keys = re.findall(r'"(\w+)":', inspect.getsource(ns.fetch_titles_by_source))
print("來源數", len(keys), "| iek_news 之後:", keys[keys.index("iek_news") + 1])
sys.exit(0 if len(titles) >= 5 else 1)
