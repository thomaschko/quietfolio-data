# -*- coding: utf-8 -*-
"""一次性探測(跑完即刪):sangyo-times 連線狀況(多次、不同 timeout/協定)+ 抓到就實跑翻譯。"""
import time
import requests
import industry_news as inn
import news_sources as ns
from llm_client import LLMChain

for url in ["https://www.sangyo-times.jp/scn/headindex.aspx", "http://www.sangyo-times.jp/scn/headindex.aspx",
            "https://www.sangyo-times.jp/scn/headindex.aspx"]:
    t0 = time.time()
    try:
        r = requests.get(url, headers=ns.UA, timeout=60)
        print(f"{url}: HTTP {r.status_code} {len(r.content)}B {time.time()-t0:.1f}s final={r.url}")
    except Exception as e:
        print(f"{url}: {type(e).__name__} {time.time()-t0:.1f}s {str(e)[:120]}")
hl = ns.fetch_sangyo_headlines(timeout=60)
print("parsed:", None if not hl else (hl["issue_date"], hl["issue_no"], [(s["name"], len(s["items"])) for s in hl["sections"]]))
if hl:
    out = inn.translate_sangyo(hl, LLMChain())
    print(f"翻譯 {out['translated']}/{out['total']}  TOP: {out['top']['zh']} | {out['top']['ja']}")
    for s in out["sections"]:
        print(f"  [{s['name']}]")
        for r in s["items"]:
            print(f"    {r['zh'] or '(未翻)'} | {r['ja']}")
