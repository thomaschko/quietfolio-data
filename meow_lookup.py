# -*- coding: utf-8 -*-
"""
============================================================
meow_lookup.py — 美股喵/日股喵 題材→國際公司查詢
============================================================
定位:src6國際大廠反查的「交叉驗證來源」,跟Gemini反查並列,
     不取代 —— 兩邊都需要連續天數驗證才會自動升格進src6 COMPANIES。

robots.txt 明確允許(Allow: /),讀取公開網頁文字,無需登入/付費功能。

運作:
  1. 抓 /concept/ 主題索引,建立「主題名→URL」對照
  2. 給一個題材詞,模糊比對主題名(找包含關係,如"光通訊"能配到"網通與光通訊")
  3. 抓對應 /concept/{主題}/ 頁面,解析上中下游公司清單(含股票代號)

版權:網站聲明「本站編輯性整理」可公開引用,僅讀取不下載執行任何程式。
============================================================
"""
import requests
import re

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"}

SITES = {
    "us": "https://usstockmeow.com.tw",
    "jp": "https://jpstockmeow.com.tw",
}

_theme_index_cache = {}  # {site: {theme_name: url}}
_lookup_cache = {}       # {(site, theme): result}


def _fetch(url):
    try:
        r = requests.get(url, headers=UA, timeout=20)
        r.encoding = "utf-8"  # 明確指定編碼,避免中文主題名/公司名亂碼
        if r.status_code == 200:
            return r.text
        return None
    except Exception:
        return None


def _get_theme_index(site):
    """抓 /concept/ 索引頁,回傳 {主題名: 完整URL}。同次執行內快取。
    用 href="/concept/{主題名}/" 這個穩定錨點抓,不依賴猜測的HTML標籤結構。"""
    if site in _theme_index_cache:
        return _theme_index_cache[site]
    base = SITES[site]
    text = _fetch(f"{base}/concept/")
    index = {}
    if text:
        # 抓所有 /concept/{中文主題名}/ 連結(URL可能是原始中文或URL編碼,兩種都試)
        for m in re.finditer(r'href="(/concept/([^"/]+)/)"', text):
            path, theme_raw = m.group(1), m.group(2)
            try:
                theme_name = requests.utils.unquote(theme_raw)
            except Exception:
                theme_name = theme_raw
            if theme_name and theme_name not in index:
                index[theme_name] = f"{base}{path}"
    _theme_index_cache[site] = index
    return index


def _parse_companies(text):
    """解析 concept 頁面的公司清單。
    改用頁面內建的 JSON-LD 結構化資料(schema.org ItemList)——這是SEO用的
    固定格式,比猜測HTML標籤穩健得多。格式:
      "name": "公司名(TICKER)", "url": "https://.../stock/TICKER/"
    """
    companies = []
    seen = set()
    pattern = r'"name":\s*"([^"(]+)\((\w+)\)",\s*"url":\s*"https://[^"]+?/stock/(\w+)/"'
    for m in re.finditer(pattern, text):
        name, ticker_in_name, ticker = m.group(1).strip(), m.group(2), m.group(3)
        if ticker in seen:
            continue
        seen.add(ticker)
        companies.append({"ticker": ticker, "name": name})
    return companies


def lookup_theme(theme_term, site="us"):
    """
    查詢一個題材在美股喵/日股喵是否有對應主題。
    模糊比對:題材詞若是主題名的子字串、或主題名含題材詞,都算命中。
    回傳 {"found": True, "companies": [{ticker,name},...], "theme_name": 實際主題名,
          "source": "meow-us"/"meow-jp"} 或 None。
    """
    cache_key = (site, theme_term)
    if cache_key in _lookup_cache:
        return _lookup_cache[cache_key]

    index = _get_theme_index(site)
    matched_theme, matched_url = None, None
    for theme_name, url in index.items():
        if theme_term in theme_name or theme_name in theme_term:
            matched_theme, matched_url = theme_name, url
            break

    if not matched_theme:
        _lookup_cache[cache_key] = None
        return None

    text = _fetch(matched_url)
    if not text:
        _lookup_cache[cache_key] = None
        return None

    companies = _parse_companies(text)
    if not companies:
        _lookup_cache[cache_key] = None
        return None

    result = {
        "found": True,
        "companies": companies,
        "theme_name": matched_theme,
        "source": f"meow-{site}",
    }
    _lookup_cache[cache_key] = result
    return result


def lookup_theme_all_sites(theme_term):
    """對美股喵+日股喵都查一次,回傳合併結果 list(可能0-2筆)。"""
    results = []
    for site in SITES:
        r = lookup_theme(theme_term, site)
        if r:
            results.append(r)
    return results


if __name__ == "__main__":
    for term in ["光通訊", "半導體", "機器人", "核電", "太空", "不存在的題材XYZ"]:
        r = lookup_theme(term, "us")
        if r:
            tickers = [c["ticker"] for c in r["companies"]]
            print(f"{term} → 美股喵主題「{r['theme_name']}」: {len(tickers)}家, {tickers[:8]}")
        else:
            print(f"{term} → 查無對應主題")
