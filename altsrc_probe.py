# -*- coding: utf-8 -*-
"""altsrc_probe.py — 一次性實測(測完即還原):
1) 鉅亨基數不足的 22 個 watchlist 詞,在 Google News(台灣/美國)近20天的量與日期分布、標題是否含完整詞、帶出的台股
2) 鉅亨多字詞是否被拆字搜尋(HBM Base Die / Vera CPU / Rubin CPX / Vera Rubin / 800V HVDC)
3) 方格子 vocus.cc 的 robots.txt 與 RSS"""
import re
import time
import datetime as dt
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import requests

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"}
TERMS = ["LPO", "XPO", "光交換", "空芯光纖", "Spectrum-XGS", "超乙太網路", "UEC", "Spectrum-6", "BlueField-4",
         "混合鍵合", "玻璃通孔", "CoWoS-L", "EMIB-T", "晶背供電", "UCIe", "小晶片", "冷板", "雙面冷板",
         "兩相冷卻", "垂直供電", "HBF", "高頻寬快閃"]
NOW = dt.datetime.now(dt.timezone.utc)

try:
    from event_theme_radar import build_name2code
    name2code, code2name = build_name2code()
except Exception as e:
    print("股名對照失敗", e)
    name2code, code2name = {}, {}
NAMES = sorted((n for n in name2code if len(n) >= 2), key=len, reverse=True)


def gnews(q, tw=True):
    loc = "hl=zh-TW&gl=TW&ceid=TW:zh-Hant" if tw else "hl=en-US&gl=US&ceid=US:en"
    url = f"https://news.google.com/rss/search?q={quote(q)}&{loc}"
    r = requests.get(url, headers=UA, timeout=25)
    items = []
    for it in ET.fromstring(r.content).iter("item"):
        t = (it.findtext("title") or "").strip()
        try:
            d = parsedate_to_datetime(it.findtext("pubDate") or "")
        except Exception:
            d = None
        items.append((t, d))
    return r.status_code, items


def stocks_in(titles):
    hit = {}
    for t in titles:
        for n in NAMES:
            if n in t:
                hit[name2code[n]] = code2name.get(name2code[n], n)
        for c in re.findall(r"[（(](\d{4})[）)]", t):
            if c in code2name:
                hit[c] = code2name[c]
    return hit


print("=== 1) Google News:近20天 ===")
for term in TERMS:
    for tw in ([True, False] if re.search(r"[A-Za-z]", term) else [True]):
        try:
            code, items = gnews(f'"{term}" when:20d', tw)
        except Exception as e:
            print(f"{term} [{'TW' if tw else 'US'}] 失敗 {str(e)[:60]}")
            continue
        recent = [i for i in items if i[1] and (NOW - i[1]).days < 3]
        exact = [t for t, _ in items if term.lower() in t.lower()]
        st = stocks_in([t for t, _ in items]) if tw else {}
        print(f"\n{term} [{'TW' if tw else 'US'}] HTTP {code}:20天 {len(items)} 則、近3天 {len(recent)}、"
              f"標題含完整詞 {len(exact)}、日均(前17天) {round((len(items)-len(recent))/17, 2)}"
              + (f"、帶出台股 {dict(list(st.items())[:8])}" if st else ""))
        for t, _ in items[:4]:
            print("   ·", t[:90])
        time.sleep(1)

print("\n=== 2) 鉅亨多字詞拆字檢查 ===")
try:
    from event_theme_radar import cnyes_search
    for kw in ["HBM Base Die", "Vera CPU", "Rubin CPX", "Vera Rubin", "800V HVDC"]:
        news = cnyes_search(kw, int((NOW - dt.timedelta(days=20)).timestamp()), max_pages=3)
        full = [n for n in news if kw.lower() in (n["title"] + " " + n["summary"]).lower()]
        print(f"{kw}: 回傳 {len(news)} 則,標題/摘要含完整詞 {len(full)} 則")
        for n in news[:3]:
            print("   ·", n["title"][:80])
        time.sleep(0.5)
except Exception as e:
    print("鉅亨檢查失敗", e)

print("\n=== 3) 方格子 vocus.cc ===")
for u in ["https://vocus.cc/robots.txt", "https://vocus.cc/rss", "https://vocus.cc/feed",
          "https://vocus.cc/user/@saksq/rss", "https://vocus.cc/search?q=CPO"]:
    try:
        r = requests.get(u, headers=UA, timeout=20)
        body = r.text
        kind = "RSS" if "<rss" in body[:500] or "<feed" in body[:500] else ("HTML" if "<html" in body[:2000].lower() else "其他")
        print(f"{u} → HTTP {r.status_code} {kind} 長度{len(body)}")
        if u.endswith("robots.txt"):
            print("\n".join("   " + ln for ln in body.splitlines()[:25]))
    except Exception as e:
        print(u, "失敗", str(e)[:80])
