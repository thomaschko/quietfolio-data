# -*- coding: utf-8 -*-
"""
tw_coverage_probe.py — 探測 My-TW-Coverage 供應鏈資料庫
============================================================
只讀 raw.githubusercontent.com 的靜態markdown文字,絕不執行對方任何.py腳本。
(對方repo有安全審核標記為中風險,因其設計包含可執行腳本;
 我們只用「讀檔案內容當純文字」這個最安全的用法,等同讀新聞網頁。)

驗證:
  1. themes/README.md 能不能抓到、列出哪些主題檔案存在
  2. 抓幾個對應你研究題材的主題檔(CoWoS/HBM/矽光子等),看公司清單品質
  3. 個股報告檔案(Pilot_Reports/)的供應鏈段落能不能抓到
============================================================
"""
import requests

UA = {"User-Agent": "Mozilla/5.0 (quietfolio-research; read-only)"}
BASE = "https://raw.githubusercontent.com/Timeverse/My-TW-Coverage/master"

# 對照你的核心研究題材,猜可能的主題檔名(repo慣例是英文/技術詞當檔名)
CANDIDATE_THEME_FILES = [
    "CoWoS", "HBM", "AI_伺服器", "NVIDIA", "矽光子", "CPO",
    "碳化矽", "電動車", "5G", "PCB", "EUV",
]


def fetch_text(url):
    try:
        r = requests.get(url, headers=UA, timeout=20)
        return r.status_code, r.text
    except Exception as e:
        return 0, str(e)


def main():
    print("=" * 62)
    print("My-TW-Coverage 供應鏈資料庫探測(只讀文字,不執行腳本)")
    print("=" * 62)

    # 1. 抓主題索引
    print("\n■ themes/README.md(主題索引)")
    code, text = fetch_text(f"{BASE}/themes/README.md")
    print(f"  狀態 {code}, 長度 {len(text)}")
    if code == 200:
        print(f"  內容前500字:\n{text[:500]}")

    # 2. 逐一試候選主題檔
    print("\n■ 逐一測試主題檔案")
    found = []
    for name in CANDIDATE_THEME_FILES:
        url = f"{BASE}/themes/{name}.md"
        code, text = fetch_text(url)
        if code == 200 and len(text) > 100:
            print(f"  ✓ {name}.md ({len(text)}字元)")
            found.append((name, text))
        else:
            print(f"  · {name}.md → {code}")

    # 3. 顯示找到的檔案內容範例(看公司清單品質)
    print("\n" + "=" * 62)
    print("找到的主題檔內容範例")
    print("=" * 62)
    for name, text in found[:3]:
        print(f"\n■ {name}.md 前800字:")
        print(text[:800])

    # 4. 測一份個股報告(驗證Pilot_Reports結構)
    print("\n■ 測試個股報告(2330台積電)")
    code, text = fetch_text(f"{BASE}/Pilot_Reports/Semiconductors/2330_台積電.md")
    print(f"  狀態 {code}, 長度 {len(text)}")
    if code == 200:
        # 找供應鏈段落
        idx = text.find("供應鏈位置")
        if idx != -1:
            print(f"  供應鏈段落:\n{text[idx:idx+400]}")

    print("\n" + "=" * 62)
    print(f"總結:{len(found)}/{len(CANDIDATE_THEME_FILES)} 個候選主題檔存在")
    print("→ 把結果貼回給 Claude,決定怎麼整合進題材→個股驗證邏輯")


if __name__ == "__main__":
    main()
