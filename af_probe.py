# -*- coding: utf-8 -*-
"""
af_probe.py — 反脆弱選股模組 資料來源探測(2026-11-23,一次性,只讀,只印欄位與筆數,不存資料)v2026-11-23-init
規格 §2:端點名稱先實際打一次確認欄位與可用性。間隔 3 秒;403/429 → 停止該來源。
"""
import json, os, re, sys, time
import requests

UA = {"User-Agent": "quietfolio-af-probe/1.0 (read-only research)"}
S = requests.Session(); S.headers.update(UA)
KW = re.compile(r"董|監|轉讓|庫藏|資產負債|現金流量|損益|本益比|殖利率|基本資料|設質|質押|持股")


def get(url, **kw):
    time.sleep(3)
    try:
        r = S.get(url, timeout=40, **kw)
        return r
    except Exception as e:
        print("  ERR", url, type(e).__name__); return None


def show(name, url, **kw):
    r = get(url, **kw)
    if r is None: return None
    ct = r.headers.get("content-type", "")
    print(f"== {name} HTTP {r.status_code} {ct[:40]} bytes {len(r.content)}")
    try:
        j = r.json()
    except Exception:
        print("  text:", r.text[:200].replace("\n", " ")); return None
    rows = j if isinstance(j, list) else (j.get("data") or j.get("aaData") or j.get("tables") or j)
    if isinstance(rows, list) and rows:
        print("  n", len(rows), "keys", list(rows[0].keys())[:40] if isinstance(rows[0], dict) else rows[0])
        if isinstance(rows[0], dict): print("  sample", json.dumps(rows[0], ensure_ascii=False)[:400])
    else:
        print("  json keys", list(j.keys())[:20] if isinstance(j, dict) else type(j), json.dumps(j, ensure_ascii=False)[:400])
    return j


def swagger(name, url):
    r = get(url)
    if r is None or r.status_code != 200: print("swagger fail", name, r and r.status_code); return
    sw = r.json()
    print(f"== {name} swagger paths {len(sw.get('paths', {}))}")
    for p, v in sw.get("paths", {}).items():
        for m, op in v.items():
            s = (op.get("summary") or "") + " " + (op.get("description") or "")
            if KW.search(s): print("  ", p, "|", s.strip()[:80])


def main():
    swagger("TWSE", "https://openapi.twse.com.tw/v1/swagger.json")
    swagger("TPEx", "https://www.tpex.org.tw/openapi/swagger.json")
    T = "https://openapi.twse.com.tw/v1/opendata/"
    for ep in ("t187ap03_L", "t187ap07_L_ci", "t187ap06_L_ci", "t187ap11_L", "t187ap12_L", "t187ap09_L"):
        show(ep, T + ep)
    show("BWIBBU_ALL", "https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL")
    show("BWIBBU_d 2021-11-22", "https://www.twse.com.tw/rwd/zh/afterTrading/BWIBBU_d", params={"date": "20211122", "selectType": "ALL", "response": "json"})
    P = "https://www.tpex.org.tw/openapi/v1/"
    for ep in ("mopsfin_t187ap03_O", "mopsfin_t187ap07_O_ci", "mopsfin_t187ap06_O_ci", "mopsfin_t187ap11_O", "tpex_mainboard_peratio_analysis"):
        show(ep, P + ep)
    tok = os.environ.get("FINMIND_TOKEN", "")
    if tok:
        for ds, start in (("TaiwanStockBalanceSheet", "2024-01-01"), ("TaiwanStockCashFlowsStatement", "2024-01-01"),
                          ("TaiwanStockFinancialStatements", "2024-01-01"), ("TaiwanStockPER", "2021-11-01"),
                          ("TaiwanStockShareholding", "2026-10-01")):
            time.sleep(2)
            r = requests.get("https://api.finmindtrade.com/api/v4/data", params={"dataset": ds, "data_id": "2330", "start_date": start},
                             headers={"Authorization": "Bearer " + tok}, timeout=60)
            d = r.json().get("data") or [] if r.status_code == 200 else []
            types = sorted({x.get("type") for x in d if x.get("type")})
            print(f"== FinMind {ds} HTTP {r.status_code} n {len(d)} keys {list(d[0].keys()) if d else None}")
            if types: print("  types", [t for t in types if re.search(r"Cash|Asset|Liabilit|Borrow|Interest|Operating|EPS|Equity|Loan|Payable", t)][:60])
            if d and not types: print("  first", d[0], "last", d[-1])
    return 0


if __name__ == "__main__":
    sys.exit(main())
