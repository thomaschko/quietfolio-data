# -*- coding: utf-8 -*-
"""一次性探測(跑完即刪):sangyo-times 標題索引頁內容 + 文章 ID 對應日期(估更新頻率)、內文是否全文。"""
import re
import requests

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"}


def text(html):
    html = re.sub(r"<script.*?</script>|<style.*?</style>", "", html, flags=re.S)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


r = requests.get("https://www.sangyo-times.jp/scn/headindex.aspx", headers=UA, timeout=25)
r.encoding = "utf-8"
print("headindex HTTP", r.status_code, len(r.content))
t = text(r.text)
i = t.find("EUV")
print(t[max(0, i - 1500):i + 3000])
print("links:", re.findall(r"href=\"([^\"]*article[^\"]*)\"", r.text)[:40])

for aid in [15232, 15229, 15225, 15220, 15215, 15210, 15200, 15190, 15180, 15160, 15140, 15100]:
    try:
        r = requests.get(f"https://www.sangyo-times.jp/article.aspx?ID={aid}", headers=UA, timeout=25)
        r.encoding = "utf-8"
        title = re.findall(r"<title>(.*?)</title>", r.text, re.S)
        d = re.findall(r"20\d\d/\d{1,2}/\d{1,2}", r.text)
        body = text(r.text)
        print(f"ID {aid}: HTTP {r.status_code} {d[:1]} | {re.sub(r'\\s+', ' ', title[0]).strip()[:70] if title else ''} | len={len(body)}"
              f" | 続き/会員: {[w for w in ('続きは', '会員', 'ログイン', '購読者') if w in body]}")
    except Exception as e:
        print(aid, e)
