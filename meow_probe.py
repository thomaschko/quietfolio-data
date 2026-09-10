# -*- coding: utf-8 -*-
"""
meow_probe.py — 美股喵/日股喵 探測(src6國際大廠反查的驗證層)
============================================================
定位:不是新聞源,是「結構化個股檔案資料庫」,用來驗證/補強
     Gemini反查出的國際公司清單,提供上中下游供應鏈位置標籤。

只讀 HTML 頁面文字(公開個股檔案頁),不執行任何腳本、不涉及會員/付費功能。

驗證:
  1. 個股頁面格式能不能抓、有沒有主題/供應鏈位置標籤
  2. 日股喵的結構是否與美股喵一致
  3. 有沒有 robots.txt 限制自動化讀取
============================================================
"""
import requests
import re

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"}

SITES = {
    "美股喵": "https://usstockmeow.com.tw",
    "日股喵": "https://jpstockmeow.com.tw",
    "台股喵": "https://twstockmeow.com.tw",  # 對照組,你已知它引用My-TW-Coverage
}

# 美股喵已知範例(從搜尋結果確認存在):FN=Fabrinet(光通訊,對應你的CPO研究)
TEST_PAGES = {
    "美股喵-FN(Fabrinet)": "https://usstockmeow.com.tw/stock/FN/",
    "美股喵-NVDA": "https://usstockmeow.com.tw/stock/NVDA/",
    "美股喵-AAOI": "https://usstockmeow.com.tw/stock/AAOI/",
}


def check_robots(base_url):
    try:
        r = requests.get(f"{base_url}/robots.txt", headers=UA, timeout=15)
        if r.status_code == 200:
            return r.text
        return f"(無robots.txt或狀態{r.status_code})"
    except Exception as e:
        return f"錯誤: {e}"


def probe_page(url):
    try:
        r = requests.get(url, headers=UA, timeout=20)
        return r.status_code, r.text
    except Exception as e:
        return 0, str(e)


def main():
    print("=" * 62)
    print("美股喵/日股喵/台股喵 探測(結構化驗證層可行性)")
    print("=" * 62)

    # 1. robots.txt 檢查(確認自動化讀取的合規性)
    print("\n■ robots.txt 檢查")
    for name, base in SITES.items():
        robots = check_robots(base)
        print(f"\n  {name} ({base}/robots.txt):")
        print(f"    {robots[:300]}")

    # 2. 首頁/about頁可達性
    print("\n" + "=" * 62)
    print("首頁/about頁可達性")
    print("=" * 62)
    for name, base in SITES.items():
        code, text = probe_page(f"{base}/about/")
        print(f"\n  {name} /about/: 狀態{code}, 長度{len(text)}")

    # 3. 測試個股頁(看有沒有主題/供應鏈標籤)
    print("\n" + "=" * 62)
    print("個股頁面內容測試(看主題/供應鏈位置標籤)")
    print("=" * 62)
    for name, url in TEST_PAGES.items():
        code, text = probe_page(url)
        print(f"\n■ {name}")
        print(f"  {url}")
        print(f"  狀態 {code}, 長度 {len(text)}")
        if code == 200:
            # 找主題標籤相關文字(上游/中游/下游/投資主題)
            for keyword in ["上游", "中游", "下游", "投資主題", "供應鏈"]:
                idx = text.find(keyword)
                if idx != -1:
                    snippet = text[max(0,idx-20):idx+150]
                    snippet = re.sub(r'\s+', ' ', snippet)
                    print(f"    找到「{keyword}」: ...{snippet}...")
                    break

    print("\n→ 把完整結果貼回給 Claude,決定怎麼整合進src6反查驗證。")


if __name__ == "__main__":
    main()
