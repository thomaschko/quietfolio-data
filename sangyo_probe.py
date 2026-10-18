# -*- coding: utf-8 -*-
"""一次性探測(跑完即刪):sangyo-times 標題頁與首頁的 HTML 結構。"""
import re
import requests

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"}
for url, anchors in [("https://www.sangyo-times.jp/scn/headindex.aspx", ["2723号", "リープモーター", "ヴァンガード", "エア・リキード"]),
                     ("https://www.sangyo-times.jp/scn/", ["ID=15232", "ID=15230", "EUV投資", "headindex"])]:
    r = requests.get(url, headers=UA, timeout=25)
    r.encoding = "utf-8"
    h = re.sub(r"<script.*?</script>|<style.*?</style>", "", r.text, flags=re.S)
    print(f"\n######## {url} {r.status_code} {len(h)}")
    for a in anchors:
        i = h.find(a)
        print(f"\n--- [{a}] at {i}")
        if i >= 0:
            print(h[max(0, i - 700):i + 900])
