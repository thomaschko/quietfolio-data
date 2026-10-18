# -*- coding: utf-8 -*-
"""一次性探測(跑完即刪):用 repo 裡現有的日報實跑一次週報(含 sangyo-times 翻譯),印出結果,不寫入 repo。"""
import json
import datetime as dt
import industry_news as inn
from news_sources import fetch_sangyo_headlines
from llm_client import LLMChain

today = dt.datetime.now(inn.TPE).date()
chain = LLMChain()
docs = inn.load_week(today)
print("可用 AI:", chain.available, "| 日報:", [d["date"] for d in docs])
hl = fetch_sangyo_headlines()
print("sangyo:", None if not hl else (hl["issue_date"], hl["issue_no"], sum(len(s["items"]) for s in hl["sections"])))
w = inn.build_weekly(today, docs, chain, hl)
print(json.dumps({k: w[k] for k in ("week", "range", "n_daily", "n_items", "provider", "fallback")}, ensure_ascii=False))
print("OVERVIEW:", w["overview"])
for th in w["themes"]:
    print(f"\n## {th['topic']} — {th['summary']}")
    print("   股:", " ".join(c["code"] + c["name"] for c in th["codes"]))
    for it in th["items"]:
        print(f"   - {it['date'][5:]} {it['headline']}")
sg = w["sangyo"]
if sg:
    print(f"\n### 電子デバイス産業新聞 {sg['issue_date']} 第{sg['issue_no']}號 翻譯 {sg['translated']}/{sg['total']}")
    print("  TOP:", sg["top"]["zh"], "|", sg["top"]["ja"])
    for s in sg["sections"]:
        print(f"  [{s['name']}]")
        for r in s["items"]:
            print(f"    {r['zh'] or '(未翻)'} | {r['ja']}")
print("AI log:", chain.log)
