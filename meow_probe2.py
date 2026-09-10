# -*- coding: utf-8 -*-
"""
meow_probe2.py — 美股喵/日股喵 解析診斷(印原始HTML+解析結果對照)
============================================================
上次探測確認 robots.txt 允許、頁面可抓,但解析器是基於 web_fetch工具
「轉換過的markdown格式」設計的,不是requests.get()實際拿到的原始HTML。
這支同時印出【原始HTML片段】和【解析結果】,一次確認格式對不對。
============================================================
"""
import requests
import re

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"}

TEST_URL = "https://usstockmeow.com.tw/concept/網通與光通訊/"
INDEX_URL = "https://usstockmeow.com.tw/concept/"


def parse_companies(text):
    companies = []
    seen = set()
    for m in re.finditer(r'href="/stock/([A-Z0-9\.]+)/"[^>]*>([^<]{0,80})', text):
        ticker = m.group(1)
        raw_name = re.sub(r'<[^>]+>', '', m.group(2)).strip()
        if ticker in seen:
            continue
        seen.add(ticker)
        companies.append({"ticker": ticker, "name": raw_name[:60]})
    return companies


def parse_theme_index(text):
    index = {}
    for m in re.finditer(r'href="(/concept/([^"/]+)/)"', text):
        path, theme_raw = m.group(1), m.group(2)
        try:
            theme_name = requests.utils.unquote(theme_raw)
        except Exception:
            theme_name = theme_raw
        if theme_name and theme_name not in index:
            index[theme_name] = path
    return index


def main():
    print("=" * 60)
    print("美股喵解析診斷")
    print("=" * 60)

    # 1. 主題索引頁:印原始HTML + 解析結果
    print(f"\n■ 主題索引頁: {INDEX_URL}")
    r = requests.get(INDEX_URL, headers=UA, timeout=20)
    print(f"  狀態 {r.status_code}, 長度 {len(r.text)}")
    if r.status_code == 200:
        idx = r.text.find("concept/")
        print(f"\n  原始HTML片段(前600字,從第一個concept/出現處):")
        print(f"  {r.text[max(0,idx-50):idx+600]}")

        themes = parse_theme_index(r.text)
        print(f"\n  解析結果:{len(themes)} 個主題")
        for name, path in list(themes.items())[:10]:
            print(f"    {name} → {path}")

    # 2. 個股清單頁:印原始HTML + 解析結果
    print(f"\n■ 主題內容頁: {TEST_URL}")
    r2 = requests.get(TEST_URL, headers=UA, timeout=20)
    print(f"  狀態 {r2.status_code}, 長度 {len(r2.text)}")
    if r2.status_code == 200:
        idx2 = r2.text.find("/stock/")
        print(f"\n  原始HTML片段(前600字,從第一個/stock/出現處):")
        print(f"  {r2.text[max(0,idx2-50):idx2+600]}")

        companies = parse_companies(r2.text)
        print(f"\n  解析結果:{len(companies)} 家公司")
        for c in companies:
            print(f"    {c['ticker']}: {c['name']}")

    print("\n→ 把完整結果(尤其原始HTML片段)貼回給 Claude,確認/修正解析規則")


if __name__ == "__main__":
    main()
