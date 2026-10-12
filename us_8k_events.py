# -*- coding: utf-8 -*-
"""
============================================================
us_8k_events.py — 偵測源:美股8-K重訊(2026-09-22新增)
============================================================
發想來源:研究compassingai.com、fxbaogao.com、S&P Global Market
Intelligence這些第三方平台能不能接入題材源之後,回頭確認美國有沒有
跟TWSE openapi對等的官方免費資源——找到SEC EDGAR,這是真正對等的
東西:官方、免費、不用申請key、結構化JSON、即時更新。

核心邏輯:
  對你已經在追蹤的23家公司(沿用earnings_keywords.py的COMPANIES,
  單一清單來源,不重複維護第二份、避免像IFNNY/POET那樣兩份清單各自
  漂移),查詢SEC EDGAR的Submissions API,抓最近N天內的8-K(重大訊息)
  申報,把Item代碼翻譯成中文分類(高層異動/重大協議/併購完成...),
  性質上就是MOPS重訊(src2)的美股版。

  跟earnings_keywords.py(季度性、只在法說季跑)不同,這支腳本設計成
  每天跑——8-K是「事件觸發」不是「日曆觸發」,公司發生重大事件後
  4個工作天內必須申報,不會等到法說會才講,即時性更高。

資料源:
  股號→CIK對照表: https://www.sec.gov/files/company_tickers.json
  各公司申報清單: https://data.sec.gov/submissions/CIK{10碼補零}.json
  兩者皆為SEC官方免費API,不需API key,但強制要求帶User-Agent header
  (格式:應用名稱+聯絡email,沒帶會被拒絕403)。

額度:速率限制每秒10次請求,23家公司完全在額度內,不用像
earnings_keywords.py那樣煩惱25次/天的問題。

用法:
  python us_8k_events.py
============================================================
"""
import re
import json
import time
import datetime as dt
import requests

from earnings_keywords import COMPANIES  # 單一清單來源,不重複維護

# 2026-10-12:Item 7.01/8.01 讀附件全文做關鍵詞計數(investor day 簡報、技術發表常以
# EX-99 附件申報)。計數規則、investor day 用語、額外追蹤公司都沿用 investor_events.py,
# 只存「詞×次數」,不存全文。
try:
    from investor_events import count_all, EVENT_RE, SEARCH_NAMES
except Exception:
    count_all, EVENT_RE, SEARCH_NAMES = None, None, {}
TEXT_ITEMS = ("7.01", "8.01")   # 讀全文的 Item(2.02 財報稿已有 earnings_keywords 涵蓋)
MAX_EXHIBITS = 3
MAX_DOC_CHARS = 1_500_000

# SEC強制要求User-Agent帶識別資訊+聯絡方式,沒帶會403
UA = {"User-Agent": "Quietfolio-research thomaschko-quietfolio@example.com"}

LOOKBACK_DAYS = 7  # 抓最近幾天內的8-K(比每日執行的排程留一點緩衝,避免漏抓)

# 8-K Item代碼 → 中文分類(對照SEC官方文件)
ITEM_LABELS = {
    "1.01": "訂立重大協議",
    "1.02": "終止重大協議",
    "1.05": "重大資安事件",
    "2.01": "完成收購/處分",
    "2.02": "營運及財務業績",
    "2.03": "創設直接財務義務",
    "2.05": "重整/資產減損成本",
    "2.06": "重大資產減損",
    "3.01": "退市/未符上市標準",
    "4.01": "簽證會計師變更",
    "4.02": "財報不可再依賴",
    "5.01": "控制權變更",
    "5.02": "高層異動(董事/主管)",
    "5.03": "章程修訂",
    "5.07": "股東會表決結果",
    "6.01": "ABS資訊",
    "7.01": "Reg FD揭露",
    "8.01": "其他重大事件",
    "9.01": "財報及附件",
}


def fetch_ticker_to_cik():
    """回傳 {股號: 10碼補零CIK}。單一檔案,一次抓取供全部公司共用。"""
    try:
        r = requests.get("https://www.sec.gov/files/company_tickers.json",
                          headers=UA, timeout=20)
        r.raise_for_status()
        data = r.json()
        # 格式:{"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}, ...}
        mapping = {}
        for entry in data.values():
            ticker = str(entry.get("ticker", "")).upper()
            cik = entry.get("cik_str")
            if ticker and cik is not None:
                mapping[ticker] = str(cik).zfill(10)
        print(f"  股號→CIK對照表: {len(mapping)} 檔")
        return mapping
    except Exception as e:
        print(f"  ⚠ 股號→CIK對照表抓取失敗: {e}")
        return {}


def fetch_recent_8k(cik, since_date):
    """回傳某公司在since_date之後申報的8-K清單,每筆含日期/Item代碼/文件連結。
    失敗或查無資料回傳空list,不中斷主流程。"""
    try:
        r = requests.get(f"https://data.sec.gov/submissions/CIK{cik}.json",
                          headers=UA, timeout=20)
        r.raise_for_status()
        recent = r.json().get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        dates = recent.get("filingDate", [])
        items = recent.get("items", [])
        accessions = recent.get("accessionNumber", [])
        docs = recent.get("primaryDocument", [])

        results = []
        for i, form in enumerate(forms):
            if form not in ("8-K", "8-K/A"):
                continue
            filing_date = dates[i] if i < len(dates) else ""
            if filing_date < since_date:
                continue
            item_str = items[i] if i < len(items) else ""
            accession = accessions[i] if i < len(accessions) else ""
            doc = docs[i] if i < len(docs) else ""
            url = ""
            if accession and doc:
                acc_plain = accession.replace("-", "")
                cik_plain = str(int(cik))  # 網址裡的cik不補零
                url = f"https://www.sec.gov/Archives/edgar/data/{cik_plain}/{acc_plain}/{doc}"
            results.append({
                "form": form, "filing_date": filing_date,
                "items": item_str, "url": url,
            })
        return results
    except Exception as e:
        print(f"    ⚠ 抓取失敗: {e}")
        return []


def _strip_html(raw):
    raw = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"<[^>]+>", " ", raw)
    raw = re.sub(r"&nbsp;|&#160;", " ", raw)
    raw = re.sub(r"&amp;", "&", raw)
    return re.sub(r"\s+", " ", raw)


def read_filing_text(cik, url):
    """8-K 本文 + 最多 MAX_EXHIBITS 個 EX-99 附件(htm/txt;PDF/圖片略過)的純文字。
    回傳 (text, 讀到的文件數)。失敗回 ("", 0)。"""
    if not url:
        return "", 0
    base = url.rsplit("/", 1)[0]
    docs = [url]
    try:
        r = requests.get(base + "/index.json", headers=UA, timeout=20)
        r.raise_for_status()
        names = [it.get("name", "") for it in r.json().get("directory", {}).get("item", [])]
        ex = [n for n in names if re.search(r"ex[-_]?99", n, re.I) and re.search(r"\.(htm|html|txt)$", n, re.I)]
        docs += [f"{base}/{n}" for n in sorted(ex)[:MAX_EXHIBITS]]
    except Exception as e:
        print(f"      ⚠ 附件清單讀取失敗: {e}")
    texts = []
    for d in docs:
        try:
            r = requests.get(d, headers=UA, timeout=30)
            r.raise_for_status()
            texts.append(_strip_html(r.text[:MAX_DOC_CHARS]))
        except Exception as e:
            print(f"      ⚠ 讀取 {d.rsplit('/', 1)[-1]} 失敗: {e}")
        time.sleep(0.15)
    return " ".join(texts), len(texts)


def translate_items(item_str):
    """把逗號分隔的Item代碼轉成中文分類清單,查無對照的代碼原樣保留。"""
    if not item_str:
        return []
    codes = [c.strip() for c in item_str.split(",") if c.strip()]
    return [ITEM_LABELS.get(c, c) for c in codes]


def main():
    print("=" * 50)
    print("美股8-K重訊追蹤 開始")
    now = dt.datetime.now()
    since_date = (now - dt.timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    print(f"  查詢範圍: {since_date} 之後")

    print("[前置] 抓取股號→CIK對照表")
    ticker_to_cik = fetch_ticker_to_cik()
    if not ticker_to_cik:
        print("  ⚠ 對照表抓取完全失敗,本次跳過")
        return

    all_events = []
    # 追蹤清單 = AV法說清單(COMPANIES)+ investor_events 額外追蹤的大廠
    tracked = dict(COMPANIES)
    for t in SEARCH_NAMES:
        tracked.setdefault(t, f"investor day追蹤-{SEARCH_NAMES[t]}")
    for ticker, role in tracked.items():
        cik = ticker_to_cik.get(ticker)
        if not cik:
            print(f"■ {ticker} ({role}) — ⚠ 查無CIK,跳過")
            continue
        filings = fetch_recent_8k(cik, since_date)
        if not filings:
            print(f"■ {ticker} ({role}) — 近{LOOKBACK_DAYS}天無8-K申報")
        else:
            print(f"■ {ticker} ({role})")
            for f in filings:
                labels = translate_items(f["items"])
                label_str = "、".join(labels) if labels else "(無Item代碼)"
                print(f"    {f['filing_date']} {f['form']}: {label_str}")
                ev = {
                    "code": ticker, "role": role, "form": f["form"],
                    "filing_date": f["filing_date"], "items": f["items"],
                    "item_labels": labels, "url": f["url"],
                }
                codes = [c.strip() for c in f["items"].split(",")]
                if count_all and any(c in TEXT_ITEMS for c in codes):
                    text, n_docs = read_filing_text(cik, f["url"])
                    if text:
                        kw = count_all(text)
                        top = sorted(kw.items(), key=lambda x: -x[1]["count"])[:20]
                        ev["exhibits_read"] = n_docs
                        ev["event_terms"] = sorted({m.group(0).lower() for m in EVENT_RE.finditer(text)})
                        ev["exhibit_keywords"] = {k: v for k, v in top}
                        top_str = ", ".join(k + "×" + str(v["count"]) for k, v in top[:6])
                        print(f"      全文 {n_docs} 份,活動用語={ev['event_terms'] or '無'},關鍵詞 {top_str}")
                all_events.append(ev)
        time.sleep(0.15)  # 保守起見,速率限制內留緩衝(SEC上限10次/秒)

    print(f"\n輸出 us_8k_events.json: {len(all_events)} 筆8-K重訊")
    with open("us_8k_events.json", "w", encoding="utf-8") as fp:
        json.dump({
            "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "lookback_days": LOOKBACK_DAYS,
            "events": all_events,
        }, fp, ensure_ascii=False, indent=2)
    print("=" * 50)


if __name__ == "__main__":
    main()
