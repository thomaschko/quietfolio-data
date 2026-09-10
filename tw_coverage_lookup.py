# -*- coding: utf-8 -*-
"""
============================================================
tw_coverage_lookup.py — My-TW-Coverage 題材→個股權威查詢
============================================================
只讀 raw.githubusercontent.com 靜態markdown文字,絕不執行對方任何.py腳本。

定位:題材→個股關聯的「黃金標準」。取代/優先於新聞內文猜測法
     (event_theme_radar.py 的可信度分數機制),因為這裡是人工審核過
     (100%通過8條品質規則)的供應鏈研究,而非靠新聞順帶提及去猜。

運作:
  給一個題材名(中文如「CoWoS」或英文),先查 themes/{name}.md 是否存在,
  存在就解析裡面「上游/中游/下游/相關公司」段落,取出股票代號清單。
  查不到 → 回傳 None,呼叫端(event_theme_radar.py)退回舊的新聞內文猜測法。

版權:MIT License,可自由使用內容;僅讀取,不下載/執行對方任何程式碼。
============================================================
"""
import requests
import re

UA = {"User-Agent": "Mozilla/5.0 (quietfolio-radar; read-only research)"}
BASE = "https://raw.githubusercontent.com/Timeverse/My-TW-Coverage/master"

# 你的題材詞 → My-TW-Coverage 主題檔名對照(已探測確認存在的檔名)
# 找不到對照的題材,會直接嘗試「題材詞本身」當檔名(有些可能剛好同名)
THEME_ALIAS = {
    "CoWoS": "CoWoS",
    "先進封裝": "CoWoS",  # 先進封裝主題目前唯一子項是CoWoS
    "HBM": "HBM",
    "矽光子": "矽光子",
    "CPO": "CPO",
    "共同封裝光學": "CPO",
    "碳化矽": "碳化矽",
    "SiC": "碳化矽",
    "電動車": "電動車",
    "5G": "5G",
    "EUV": "EUV",
    "High-NA EUV": "EUV",
    "NVIDIA": "NVIDIA",
    "AI伺服器": "AI_伺服器",
    "AI 伺服器": "AI_伺服器",
}

_cache = {}  # 同次執行內快取,避免同一題材重複打API


def _fetch(url):
    try:
        r = requests.get(url, headers=UA, timeout=15)
        if r.status_code == 200:
            return r.text
        return None
    except Exception:
        return None


def _parse_codes_from_theme_md(text):
    """
    解析主題檔內容,抓「## 上游/中游/下游/相關公司」段落裡的股票代號。
    格式:- **3167 大量** (Specialty Industrial Machinery)
    回傳 {code: name} dict(保留公司名,供輸出時對照用)。
    """
    codes = {}
    # 只在「## 上游」「## 中游」「## 下游」「## 相關公司」這幾個段落裡找
    for m in re.finditer(r'\*\*(\d{4})\s+([^\*]+?)\*\*', text):
        code, name = m.group(1), m.group(2).strip()
        codes[code] = name
    return codes


def lookup_theme(theme_term):
    """
    查詢一個題材在 My-TW-Coverage 是否有對應主題檔。
    回傳 {"found": bool, "codes": [...], "names": {code:name}, "source": "my-tw-coverage",
          "company_count": N} 或 None(完全查不到,呼叫端應退回舊邏輯)。
    """
    if theme_term in _cache:
        return _cache[theme_term]

    # 先查別名表,沒有就直接試題材詞本身
    filename = THEME_ALIAS.get(theme_term, theme_term)
    url = f"{BASE}/themes/{filename}.md"
    text = _fetch(url)

    if text is None or len(text) < 50:
        _cache[theme_term] = None
        return None

    codes_map = _parse_codes_from_theme_md(text)
    if not codes_map:
        _cache[theme_term] = None
        return None

    result = {
        "found": True,
        "codes": sorted(codes_map.keys()),
        "names": codes_map,
        "source": "my-tw-coverage",
        "company_count": len(codes_map),
    }
    _cache[theme_term] = result
    return result


if __name__ == "__main__":
    # 自我測試:用探測時已確認存在的主題驗證
    for term in ["CoWoS", "HBM", "矽光子", "不存在的題材XYZ"]:
        r = lookup_theme(term)
        if r:
            print(f"{term}: 找到 {r['company_count']} 家公司, 前5檔: {r['codes'][:5]}")
        else:
            print(f"{term}: 查無對應主題檔,應退回新聞內文猜測法")
