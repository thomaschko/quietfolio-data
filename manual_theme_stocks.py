# -*- coding: utf-8 -*-
"""
============================================================
manual_theme_stocks.py — 題材→個股手動補充對照表(2026-10-06新增)
============================================================
tw_coverage_lookup.py(My-TW-Coverage)是題材→個股的「黃金標準」,但它
不是每個題材都有收錄,也不是每個收錄的題材都夠完整——例如BBU(電池備援)
目前在My-TW-Coverage只對到映興(3597)一檔,漏了AES-KY/系統電/順達/
新盛力/加百裕/台達電/光寶科這些實際在做的廠商。

這份表是你自己維護的補充清單,用在tw_coverage_lookup.py查無/查不全的
題材上。跟My-TW-Coverage的關係是「聯集,不是取代」:查詢時兩邊都查,
結果合併,My-TW-Coverage有的照用,這裡補它沒有/漏掉的。

格式:{題材詞(對應themes_watchlist.txt的關鍵字): {股號: 股名}}

用法:
  from manual_theme_stocks import lookup_manual_theme
  lookup_manual_theme("BBU") -> {"6781": "AES-KY", ...} 或 None(查無)
============================================================
"""

MANUAL_THEME_STOCKS = {
    # 2026-10-06新增:My-TW-Coverage目前只對到映興(3597)一檔,漏了這些
    # 實際在做AI伺服器電池備援/BBU的廠商(依使用者研究補上)。
    "BBU": {
        "6781": "AES-KY",
        "5309": "系統電",
        "3211": "順達",
        "4931": "新盛力",
        "3323": "加百裕",
        "2308": "台達電",
        "2301": "光寶科",
    },
}


def lookup_manual_theme(theme_term):
    """查詢手動補充表。回傳{code:name} dict,查無回傳None(呼叫端應視為
    "這份表沒有補充資料",不影響其他來源的判斷)。"""
    return MANUAL_THEME_STOCKS.get(theme_term)


if __name__ == "__main__":
    for term, codes in MANUAL_THEME_STOCKS.items():
        print(f"{term}: {len(codes)} 檔 — {', '.join(f'{c}{n}' for c, n in codes.items())}")
