# -*- coding: utf-8 -*-
"""
mood_leverage.py — 市場氛圍擴充 Phase 2:國際槓桿資金(v2026-11-25-mood-lev)
============================================================
規格:「Quietfolio 市場氛圍擴充規格」Phase 2。資料來源已於 2026-11-25 探測(mood_lev_probe.py,PR #106):
  US  FINRA Margin Statistics Excel(finra.org/investors/margin-statistics 頁上的 .xlsx 連結)
      欄位 Debit Balances in Customers' Securities Margin Accounts(百萬美元,月,1997-01 起)
  JP  JPX 信用取引現在高(二市場 = 東京 + 名古屋,制度 + 一般 合計):
        週表 margin/04.html 的 YYYYMMDD_mtcurrent.xlsx(只列最近 5 週)
        過去推移表 margin/05.html 的 *_mtsk.xls(2002 起)與當年 .xlsx
      分母 = 東證 時価総額 合計(misc/02.html historical-jika.xlsx,月底,百萬円)
  KR  KOFIA freesis 신용거래융자 잔고(STATSCU0100000070BO,日,2016-01 起,百萬韓元)
      TMPV2 = TMPV3(KOSPI)+ TMPV4(KOSDAQ),逐日核對相加相等。欄名 API 不提供,依數值結構判斷。
與規格不同處:
  1. 韓國:規格為「融資 ÷ 市值」,但 KRX 市值要登入才能取得 → 使用者 2026-11-25 決定改用「融資餘額年增率」(與美國同算法)。
  2. 日本分母:規格寫 Prime 市值,但 Prime 2022-04 才有,10 年分位會斷;且信用取引現在高涵蓋所有市場 →
     改用東證「合計」市值(2022-03 以前 = 一部 + 二部 + マザーズ + JASDAQ + PRO),口徑前後一致。
  3. 過期判斷:用「我們第一次抓到這筆資料的日期」算天數(週表週二才公布上週五資料、FINRA 次月第三週才公布,
     用資料日期算會在正常公布前就過期)。另設資料日期上限(美 80、日 20、韓 10 天)防止來源停更卻沒被發現。
  4. 輸出:歷史在 leverage_hist/*.csv,最新結果 leverage_latest.json;consensus.py 讀 CSV 重算(以當天日期判斷過期)後
     併入 consensus.json 的 mood.leverage(規格 Phase 6)。
  5. Phase 3(美國槓桿 ETF):投票用 ProShares TQQQ/UPRO vs SQQQ/SPXU(官網 2010 年起每日淨值、流通股數、AUM);
     Direxion SOXL/SOXS 官網只有當日快照 → 自 2026-11-25 起每日累積,另卡只顯示、不投票(使用者 2026-11-25 決定)。
     美國狀態 = FINRA 與 ETF 合成(規格 3.4)。20 日淨申購 = Σ(AUM_t − AUM_{t−1} ×(1 + 當日淨值報酬)),不受反分割影響。
抓取:先讀 robots.txt,同一主機間隔 ≥ 3 秒,誠實 User-Agent;403 / 429 / 驗證碼 → 停止該來源。
任何來源本次抓取失敗 → 該市場卡片「資料暫缺」、不參與投票(不用舊值硬算)。只描述,不構成買賣建議。
"""
import csv
import datetime as dt
import io
import json
import os
import re
import sys
import time
from urllib import robotparser
from urllib.parse import urljoin, urlsplit

VERSION = "v2026-11-25-mood-lev-etf"
HIST = "leverage_hist"
LATEST = "leverage_latest.json"
UA = "quietfolio-research/1.0 (read-only, low-frequency; github.com/thomaschko/quietfolio-data)"

FINRA_PAGE = "https://www.finra.org/investors/margin-statistics"
JPX = "https://www.jpx.co.jp/markets/statistics-equities/"
JPX_WEEKLY = JPX + "margin/04.html"
JPX_HISTORY = JPX + "margin/05.html"
JPX_MCAP = JPX + "misc/02.html"
KOFIA_URL = "https://freesis.kofia.or.kr/meta/getMetaDataList.do"
KOFIA_OBJ = "STATSCU0100000070BO"

LEV_CFG = {
    "years": 10,                                   # 歷史分位用最近 10 年
    "min_hist": {"US": 60, "JP": 150, "KR": 500, "ETF": 250},  # 樣本不足 → 不給分位、不投票
    "stale_seen_days": {"US": 50, "JP": 10, "KR": 4, "ETF": 4},   # 規格 staleAfterDays(以第一次抓到的日期起算)
    "stale_data_days": {"US": 80, "JP": 20, "KR": 10, "ETF": 10},  # 資料日期本身的上限
    "min_same": 2,                                 # 美 / 日 / 韓 有效市場中 ≥ 2 個同邊才算一票
    "spark": {"US": 36, "JP": 52, "KR": 120, "ETF": 120},
    "etf_flow_days": 20,                           # 20 日淨申購(只顯示)
    "etf_suspect_move": 30.0,                      # 單日流通股數(扣除淨值變動後)變動 > 30% → 可疑,不採用
    "etf_sox_min_days": 250,                       # SOXL/SOXS 自上線日累積,滿 250 日才給分位(只顯示,不投票)
    "etf_keep_from": "2014-01-01",
    "overlap_tol_pct": 0.5,                        # 不同檔案重疊週的差異 > 0.5% → 記錄警告
}
FILES = {"US": "us_finra.csv", "JP": "jp_margin.csv", "JP_MCAP": "jp_mcap.csv", "KR": "kr_credit.csv",
         "ETF": "us_lev_etf.csv", "SOX": "us_sox_etf.csv"}
COLS = {"US": ["month", "debit_musd", "first_seen"],
        "JP": ["date", "sell_kshs", "sell_myen", "buy_kshs", "buy_myen", "src", "first_seen"],
        "JP_MCAP": ["month_end", "mcap_myen", "first_seen"],
        "KR": ["date", "credit_mkrw", "kospi_mkrw", "kosdaq_mkrw", "first_seen"],
        "ETF": ["key", "date", "ticker", "nav", "shares_k", "aum", "navchg_pct", "first_seen"],
        "SOX": ["key", "date", "ticker", "shares", "nav", "nav_date", "first_seen"]}


# ------------------------------------------------------------------ 共用
def pctile(value, past, min_n):
    """與 consensus_core.pctile 相同的中位排名百分位(不含當期)"""
    import bisect
    vals = sorted(x for x in past if x is not None)
    if value is None or len(vals) < min_n:
        return None
    lo, hi = bisect.bisect_left(vals, value), bisect.bisect_right(vals, value)
    return round((lo + hi) / 2 / len(vals) * 100, 1)


def zone(p):
    import consensus_core as cc
    return cc.zone(p, +1)                          # 槓桿越高 = 偏熱


def num(x):
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x).replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def to_date(x, datemode=0):
    """datetime / date / Excel 序號 / 'YYYY-MM-DD' / 'YYYYMMDD' → date;不是日期 → None"""
    if isinstance(x, dt.datetime):
        return x.date()
    if isinstance(x, dt.date):
        return x
    if isinstance(x, (int, float)) and 30000 < x < 60000:
        return (dt.datetime(1899, 12, 30) + dt.timedelta(days=float(x))).date()
    s = str(x or "").strip()
    m = re.match(r"^(\d{4})-?(\d{2})-?(\d{2})", s)
    if m:
        try:
            return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        try:
            return dt.date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
        except ValueError:
            return None
    try:
        return dt.datetime.strptime(s[:12].strip(), "%b %d, %Y").date()
    except ValueError:
        return None


def read_csv(base, key):
    p = os.path.join(base, HIST, FILES[key])
    if not os.path.exists(p):
        return []
    with open(p, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_csv(base, key, rows):
    d = os.path.join(base, HIST)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, FILES[key])
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS[key])
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in COLS[key]})
    os.replace(tmp, p)


def merge(old_rows, new_rows, key_col, today, warn=None, cmp_col=None, tol=LEV_CFG["overlap_tol_pct"]):
    """以 key_col 合併;新資料覆蓋數值,但保留舊的 first_seen(第一次抓到的日期)。
    cmp_col:重疊時新舊差 > tol% 就記到 warn(不同檔案口徑不一致的提示,不擋)。"""
    by = {r[key_col]: dict(r) for r in old_rows}
    added = 0
    for r in new_rows:
        k = r[key_col]
        o = by.get(k)
        r = {kk: ("" if v is None else v) for kk, v in r.items()}
        if o is None:
            r["first_seen"] = today.isoformat()
            added += 1
        else:
            r["first_seen"] = o.get("first_seen") or today.isoformat()
            if cmp_col and warn is not None:
                a, b = num(o.get(cmp_col)), num(r.get(cmp_col))
                if a and b and abs(b / a - 1) * 100 > tol:
                    warn.append("%s %s %s 舊 %s 新 %s" % (key_col, k, cmp_col, a, b))
        by[k] = r
    return [by[k] for k in sorted(by)], added


# ------------------------------------------------------------------ 解析(純函式,輸入為「列的串列」方便測試)
def parse_finra(rows):
    """FINRA 'Customer Margin Balances' → [{'month': 'YYYY-MM', 'debit_musd': float}]"""
    out, col = [], None
    for r in rows:
        r = list(r or [])
        if col is None:
            for i, c in enumerate(r):
                if c and str(c).strip().lower().startswith("debit balances"):
                    col = i
            continue
        m = re.match(r"^(\d{4})-(\d{2})$", str(r[0] if r else "").strip())
        v = num(r[col]) if len(r) > col else None
        if m and v:
            out.append({"month": m.group(0), "debit_musd": v})
    if col is None:
        raise ValueError("FINRA:找不到 Debit Balances 欄")
    return sorted(out, key=lambda x: x["month"])


def parse_jpx_history(rows, src):
    """過去推移表(*_mtsk.xls 或當年 .xlsx):表頭第一個「合計」欄 c → 売株 c、売金額 c+1、買株 c+2、買金額 c+3。
    只讀第一個區塊(東京・名古屋 二市場);遇到第二個「[」區塊標題就停。"""
    c, out, started = None, [], False
    for r in rows:
        r = list(r or [])
        if c is None:
            for i, x in enumerate(r):
                if i > 0 and str(x or "").strip().startswith("合計"):
                    c = i
                    break
            continue
        head = str(r[0] if r else "").strip()
        if head.startswith("["):
            if started:
                break
            continue
        d = to_date(r[0]) if r else None
        if d is None:
            continue
        vals = [num(r[j]) if len(r) > j else None for j in (c, c + 1, c + 2, c + 3)]
        if None in vals:
            continue
        started = True
        out.append({"date": d.isoformat(), "sell_kshs": vals[0], "sell_myen": vals[1], "buy_kshs": vals[2],
                    "buy_myen": vals[3], "src": src})
    if c is None:
        raise ValueError("JPX 過去推移表:找不到「合計」欄(%s)" % src)
    return out


def parse_jpx_weekly(rows, src):
    """週表 YYYYMMDD_mtcurrent.xlsx:第 2 列 B 欄 = 資料日;「二市場」欄 c → 売 c、買 c+2;
    「信用取引残高合計」列 = 株数,下一列 = 金額。"""
    d, c, tot = None, None, None
    for i, r in enumerate(rows):
        r = list(r or [])
        if d is None and len(r) > 1:
            d = to_date(r[1])
        for j, x in enumerate(r):
            s = str(x or "")
            if c is None and s.startswith("二市場"):
                c = j
            if tot is None and s.startswith("信用取引残高合計"):
                tot = i
    if d is None or c is None or tot is None or tot + 1 >= len(rows):
        raise ValueError("JPX 週表格式不符(%s)" % src)
    sh, val = list(rows[tot]), list(rows[tot + 1])
    v = [num(sh[c]), num(val[c]), num(sh[c + 2]), num(val[c + 2])]
    if None in v:
        raise ValueError("JPX 週表數值缺漏(%s)" % src)
    return {"date": d.isoformat(), "sell_kshs": v[0], "sell_myen": v[1], "buy_kshs": v[2], "buy_myen": v[3], "src": src}


def parse_jpx_mcap(sheets):
    """historical-jika.xlsx 兩張表(新:プライム…合計;舊:一部…合計),各取表頭「合計」欄 → [{'month_end', 'mcap_myen'}]"""
    out = {}
    for rows in sheets:
        c = None
        for r in rows:
            r = list(r or [])
            if c is None:
                for j, x in enumerate(r):
                    if j > 0 and str(x or "").strip().startswith("合計"):
                        c = j
                continue
            d = to_date(r[0]) if r else None
            v = num(r[c]) if len(r) > c else None
            if d and v:
                out[d.isoformat()] = v
    if not out:
        raise ValueError("JPX 時価総額:沒有資料")
    return [{"month_end": k, "mcap_myen": out[k]} for k in sorted(out)]


def parse_kofia(j):
    """KOFIA JSON → [{'date', 'credit_mkrw', 'kospi_mkrw', 'kosdaq_mkrw'}];總額 ≠ KOSPI + KOSDAQ(差 > 1)→ 該日丟棄"""
    out, bad = [], 0
    for r in (j or {}).get("ds1") or []:
        d = to_date(r.get("TMPV1"))
        t, a, b = num(r.get("TMPV2")), num(r.get("TMPV3")), num(r.get("TMPV4"))
        if not d or not t or a is None or b is None:
            continue
        if abs(a + b - t) > 1:
            bad += 1
            continue
        out.append({"date": d.isoformat(), "credit_mkrw": t, "kospi_mkrw": a, "kosdaq_mkrw": b})
    if not out:
        raise ValueError("KOFIA:沒有資料")
    if bad > len(out) * 0.05:
        raise ValueError("KOFIA:%d 筆總額 ≠ KOSPI + KOSDAQ,欄位可能改變" % bad)
    return sorted(out, key=lambda x: x["date"])


ETF_BULL = ["TQQQ", "UPRO"]                      # 投票用:ProShares(官網有 2010 年起每日淨值與流通股數)
ETF_BEAR = ["SQQQ", "SPXU"]
SOX_BULL, SOX_BEAR = "SOXL", "SOXS"               # Direxion:官網只有當日快照 → 自上線日起每日累積,另卡只顯示


def parse_proshares(text, ticker):
    """ProShares {T}-historical_nav.csv → [{'date','ticker','nav','shares_k','aum','navchg_pct'}](由舊到新)"""
    rows = list(csv.reader(io.StringIO(text)))
    if not rows or "Shares Outstanding (000)" not in rows[0]:
        raise ValueError("ProShares %s:欄位不符 %s" % (ticker, rows[0][:9] if rows else None))
    h = {k: i for i, k in enumerate(rows[0])}
    out = []
    for r in rows[1:]:
        if len(r) < len(h):
            continue
        d = to_date(r[h["Date"]])
        if not d or r[h["Ticker"]].strip().upper() != ticker:
            continue
        nav, sh, aum = num(r[h["NAV"]]), num(r[h["Shares Outstanding (000)"]]), num(r[h["Assets Under Management"]])
        if not nav or aum is None:
            continue
        out.append({"key": "%s|%s" % (d.isoformat(), ticker), "date": d.isoformat(), "ticker": ticker, "nav": nav,
                    "shares_k": sh, "aum": aum, "navchg_pct": num(r[h["NAV Change (%)"]])})
    if not out:
        raise ValueError("ProShares %s:沒有資料" % ticker)
    return sorted(out, key=lambda x: x["date"])


def parse_direxion_holdings(text, ticker):
    """Direxion holdings/{T}.csv → (資料日, 流通股數);第 3 行 'Shares Outstanding:N',表格 TradeDate 為資料日"""
    m = re.search(r"Shares Outstanding:\s*([\d,\.]+)", text)
    d = re.search(r'"(\d{1,2}/\d{1,2}/\d{4})[^"]*","%s"' % ticker, text)
    if not m or not d:
        raise ValueError("Direxion %s holdings 格式不符" % ticker)
    return to_date(d.group(1)), num(m.group(1))


def parse_direxion_nav(html):
    """Direxion 多空合併產品頁 → {'SOXL': (nav, 日期), 'SOXS': (nav, 日期)}(頁面依序列出 Bull、Bear)"""
    txt = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))
    hits = re.findall(r"NAV \(Net Asset Value\) \$ ?([\d,\.]+) As of ([A-Z][a-z]{2} \d{1,2}, \d{4})", txt)
    if len(hits) < 2:
        raise ValueError("Direxion 產品頁找不到兩檔 NAV(找到 %d)" % len(hits))
    return {SOX_BULL: (num(hits[0][0]), to_date(hits[0][1])), SOX_BEAR: (num(hits[1][0]), to_date(hits[1][1]))}


# ------------------------------------------------------------------ 指標計算(純函式)
def yoy_series(points, lag_days=365, tol_days=10):
    """[(date, v)] → [(date, yoy%)];一年前 = 「≤ date − 365 天」的最後一筆,且距離不超過 tol_days"""
    import bisect
    ds = [d for d, _ in points]
    out = []
    for d, v in points:
        t = d - dt.timedelta(days=lag_days)
        i = bisect.bisect_right(ds, t) - 1
        if i < 0 or (t - ds[i]).days > tol_days or not points[i][1]:
            continue
        out.append((d, round((v / points[i][1] - 1) * 100, 2)))
    return out


def month_end(ym):
    y, m = int(ym[:4]), int(ym[5:7])
    nxt = dt.date(y + (m == 12), m % 12 + 1, 1)
    return nxt - dt.timedelta(days=1)


def jp_ratio_series(jp_rows, mcap_rows):
    """買残金額 ÷ 「資料日之前(含)最近一個月底」的東證合計市值 × 100(%)"""
    import bisect
    mc = sorted((to_date(r["month_end"]), num(r["mcap_myen"])) for r in mcap_rows)
    mds = [d for d, _ in mc]
    out = []
    for r in jp_rows:
        d = to_date(r["date"])
        i = bisect.bisect_right(mds, d) - 1
        if i < 0 or (d - mds[i]).days > 45:
            continue
        out.append((d, round(num(r["buy_myen"]) / mc[i][1] * 100, 4), mds[i]))
    return out


def window(series, years):
    """最後一筆往前 years 年(含)的序列"""
    if not series:
        return series
    cut = series[-1][0] - dt.timedelta(days=int(365.25 * years))
    return [x for x in series if x[0] > cut]


def card(mkt, series, today, seen, fetch_err, extra=None, cfg=LEV_CFG):
    """series = [(date, value)](已排序);seen = 最新一筆第一次抓到的日期 → 卡片 dict"""
    c = {"market": mkt, "ok": False, "stale": False, "vote": None, "note": None}
    if extra:
        c.update(extra)
    if not series:
        c["note"] = "資料暫缺" + ("(%s)" % fetch_err if fetch_err else "")
        return c
    w = window(series, cfg["years"])
    d, v = w[-1]
    p = pctile(v, [x for _, x in w[:-1]], cfg["min_hist"][mkt])
    c.update({"ok": True, "asOf": d.isoformat(), "value": v, "pctl": p, "zone": zone(p), "n_hist": len(w) - 1,
              "first_seen": seen.isoformat() if seen else None,
              "spark": [x for _, x in w[-cfg["spark"][mkt]:]]})
    age_seen = (today - seen).days if seen else None
    age_data = (today - d).days
    if fetch_err:
        c["note"] = "資料暫缺(本次抓取失敗:%s;最新 %s)" % (fetch_err, d.isoformat())
        c["fetch_failed"] = True
    elif (age_seen is not None and age_seen > cfg["stale_seen_days"][mkt]) or age_data > cfg["stale_data_days"][mkt]:
        c["stale"] = True
        c["note"] = "資料過期(最新 %s)" % d.isoformat()
    elif p is None:
        c["note"] = "歷史不足(%d 筆)" % (len(w) - 1)
    else:
        c["vote"] = c["zone"]
    return c


def etf_flows(rows, cfg=LEV_CFG):
    """單檔每日淨申購(美元)= AUM_t − AUM_{t−1} ×(1 + 當日淨值報酬)→ 不受反分割影響。
    |淨申購| > AUM_{t−1} 的 30% → 可疑(規格 3.2),不採用並記錄 → ({date: flow}, [可疑日期])"""
    out, sus, prev = {}, [], None
    for r in rows:
        if prev is not None and prev["aum"] and r.get("navchg_pct") is not None:
            f = r["aum"] - prev["aum"] * (1 + r["navchg_pct"] / 100.0)
            if abs(f) > prev["aum"] * cfg["etf_suspect_move"] / 100.0:
                sus.append(r["date"])
            else:
                out[r["date"]] = f
        prev = r
    return out, sus


def etf_series(rows, cfg=LEV_CFG):
    """ProShares 4 檔 → ([(date, bullRatio%)], 最新一天的 {bull_aum, bear_aum, flow20Bull, flow20Bear}, 可疑)"""
    by_t = {}
    for r in rows:
        x = {"date": r["date"], "aum": num(r["aum"]), "navchg_pct": num(r.get("navchg_pct"))}
        by_t.setdefault(r["ticker"], []).append(x)
    for t in by_t:
        by_t[t].sort(key=lambda x: x["date"])
    if any(t not in by_t for t in ETF_BULL + ETF_BEAR):
        return [], None, []
    aum = {t: {x["date"]: x["aum"] for x in by_t[t]} for t in by_t}
    flows, sus = {}, []
    for t in ETF_BULL + ETF_BEAR:
        flows[t], s_ = etf_flows(by_t[t], cfg)
        sus += ["%s %s" % (t, d) for d in s_]
    dates = sorted(set.intersection(*[set(aum[t]) for t in ETF_BULL + ETF_BEAR]))
    ser = []
    for d in dates:
        b = sum(aum[t][d] for t in ETF_BULL)
        e = sum(aum[t][d] for t in ETF_BEAR)
        if b + e > 0:
            ser.append((to_date(d), round(b / (b + e) * 100, 3)))
    if not ser:
        return [], None, sus
    last = dates[-cfg["etf_flow_days"]:]
    side = lambda ts: round(sum(flows[t].get(d, 0.0) for t in ts for d in last) / 1e9, 3)
    d = dates[-1]
    info = {"bull_aum_b": round(sum(aum[t][d] for t in ETF_BULL) / 1e9, 2),
            "bear_aum_b": round(sum(aum[t][d] for t in ETF_BEAR) / 1e9, 2),
            "flow20Bull": side(ETF_BULL), "flow20Bear": side(ETF_BEAR), "flowDays": len(last),
            "suspect": [x for x in sus if x.split()[1] >= last[0]]}
    return ser, info, sus


def sox_card(rows, today, fetch_err, cfg=LEV_CFG):
    """SOXL / SOXS(自上線日累積,只顯示):AUM ≈ 流通股數 × 最近一筆淨值(淨值日可能比股數日早 1 天)"""
    by = {}
    for r in rows:
        by.setdefault(r["date"], {})[r["ticker"]] = r
    days = [d for d in sorted(by) if SOX_BULL in by[d] and SOX_BEAR in by[d]]
    c = {"market": "US_SOX", "ok": False, "label": "美國 費半槓桿 ETF 多空比(SOXL / SOXS)", "unit": "%", "freq": "日",
         "historyDays": len(days), "needDays": cfg["etf_sox_min_days"], "vote": None, "stale": False,
         "note": None, "methodNote": "Direxion 官網只有當日快照,自 2026-11-25 起每日累積;只顯示、不投票"}
    if not days:
        c["note"] = "資料暫缺" + ("(%s)" % fetch_err if fetch_err else "")
        return c
    ser = []
    for d in days:
        b = num(by[d][SOX_BULL]["shares"]) * num(by[d][SOX_BULL]["nav"])
        e = num(by[d][SOX_BEAR]["shares"]) * num(by[d][SOX_BEAR]["nav"])
        ser.append((to_date(d), round(b / (b + e) * 100, 3), b, e))
    d, v, b, e = ser[-1]
    p = pctile(v, [x[1] for x in ser[:-1]], cfg["etf_sox_min_days"])
    c.update({"ok": True, "asOf": d.isoformat(), "value": v, "pctl": p, "zone": zone(p),
              "bull_aum_b": round(b / 1e9, 2), "bear_aum_b": round(e / 1e9, 2),
              "spark": [x[1] for x in ser[-cfg["spark"]["ETF"]:]]})
    if len(ser) > cfg["etf_flow_days"]:
        win = days[-cfg["etf_flow_days"] - 1:]
        fl = lambda t: round(sum((num(by[b2][t]["shares"]) - num(by[a][t]["shares"])) * num(by[b2][t]["nav"])
                                 for a, b2 in zip(win, win[1:])) / 1e9, 3)
        c["flow20Bull"], c["flow20Bear"] = fl(SOX_BULL), fl(SOX_BEAR)
    if fetch_err:
        c["note"] = "本次抓取失敗(%s);最新 %s" % (fetch_err, d.isoformat())
        c["fetch_failed"] = True
    elif (today - d).days > cfg["stale_data_days"]["ETF"]:
        c["stale"] = True
        c["note"] = "資料過期(最新 %s)" % d.isoformat()
    elif p is None:
        c["note"] = "累積中(%d/%d 日)" % (len(days), cfg["etf_sox_min_days"])
    return c


def valid_card(c):
    return bool(c and c.get("ok") and not c.get("stale") and not c.get("fetch_failed") and c.get("pctl") is not None)


def us_composite(finra, etf):
    """規格 3.4:都有效 → 同邊取該邊、一邊極端一邊正常取極端、方向相反無方向;只有一個有效 → 用它;都無效 → 不參與"""
    zs = [c["zone"] for c in (finra, etf) if valid_card(c)]
    if not zs:
        return {"valid": False, "zone": None, "note": "FINRA 與槓桿 ETF 都無效,美國不參與"}
    if len(zs) == 1:
        return {"valid": True, "zone": zs[0], "note": "只有%s有效" % ("FINRA" if valid_card(finra) else "槓桿 ETF")}
    a, b = zs
    if a == b:
        z = a
    elif a is None or b is None:
        z = a or b
    else:
        z = None
    return {"valid": True, "zone": z, "note": "FINRA 與槓桿 ETF 方向相反,無方向" if (a and b and a != b) else None}


def compute(base=".", today=None, status=None, cfg=LEV_CFG):
    """讀 leverage_hist/*.csv → {'version', 'US_FINRA', 'JP', 'KR', 'votes'};status = 各來源本次抓取結果 {mkt: 錯誤字串或 None}"""
    today = today or (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=8)).date()
    if status is None:
        try:
            with open(os.path.join(base, HIST, "fetch_status.json"), encoding="utf-8") as f:
                status = {k: (v or {}).get("err") for k, v in json.load(f).items()}
        except (OSError, ValueError):
            status = {}
    out = {"version": VERSION, "today": today.isoformat()}

    us = [(month_end(r["month"]), num(r["debit_musd"]), to_date(r.get("first_seen"))) for r in read_csv(base, "US")]
    us = [x for x in us if x[1]]
    us_y = yoy_series([(d, v) for d, v, _ in us], tol_days=5)
    seen = {d: s for d, _, s in us}
    mom = round((us[-1][1] / us[-2][1] - 1) * 100, 2) if len(us) >= 2 else None
    out["US_FINRA"] = card("US", us_y, today, seen.get(us_y[-1][0]) if us_y else None, status.get("US"),
                           {"metric": "yoy", "label": "美國 融資餘額年增率", "unit": "%", "freq": "月",
                            "fixedNote": "月資料,延遲約 3–7 週",
                            "mom": mom, "debit_busd": round(us[-1][1] / 1000, 1) if us else None})

    jp, mc = read_csv(base, "JP"), read_csv(base, "JP_MCAP")
    jr = jp_ratio_series(jp, mc)
    jseen = {to_date(r["date"]): to_date(r.get("first_seen")) for r in jp}
    last = jp[-1] if jp else None
    cr = round(num(last["buy_kshs"]) / num(last["sell_kshs"]), 2) if last and num(last["sell_kshs"]) else None
    jp_err = status.get("JP") or status.get("JP_MCAP")
    out["JP"] = card("JP", [(d, v) for d, v, _ in jr], today, jseen.get(jr[-1][0]) if jr else None, jp_err,
                     {"metric": "buy_to_mcap", "label": "日本 信用買残/市值", "unit": "%", "freq": "週",
                      "creditRatio": cr, "buy_tyen": round(num(last["buy_myen"]) / 1e6, 2) if last else None,
                      "mcap_month": jr[-1][2].isoformat() if jr else None})
    if last and jr and to_date(last["date"]) != jr[-1][0]:
        out["JP"]["note"] = (out["JP"]["note"] or "") + " 最新一週(%s)尚無對應月底市值" % last["date"]

    kr = [(to_date(r["date"]), num(r["credit_mkrw"]), to_date(r.get("first_seen"))) for r in read_csv(base, "KR")]
    kr = [x for x in kr if x[1]]
    kr_y = yoy_series([(d, v) for d, v, _ in kr], tol_days=7)
    kseen = {d: s for d, _, s in kr}
    out["KR"] = card("KR", kr_y, today, kseen.get(kr_y[-1][0]) if kr_y else None, status.get("KR"),
                     {"metric": "yoy", "label": "韓國 信用融資年增率", "unit": "%", "freq": "日",
                      "balance_tkrw": round(kr[-1][1] / 1e6, 2) if kr else None,
                      "methodNote": "韓國市值無免費來源,改用年增率(2026-11-25 決定)"})

    # Phase 3:美國槓桿 ETF(ProShares 4 檔投票用;SOXL / SOXS 另卡只顯示)
    er = read_csv(base, "ETF")
    ser, info, _ = etf_series(er, cfg)
    eseen = {}
    for r in er:
        d = to_date(r["date"])
        s_ = to_date(r.get("first_seen"))
        if s_ and (d not in eseen or s_ > eseen[d]):
            eseen[d] = s_
    out["US_ETF"] = card("ETF", ser, today, eseen.get(ser[-1][0]) if ser else None, status.get("ETF"),
                         dict({"metric": "bull_ratio", "label": "美國 槓桿 ETF 多空比", "unit": "%", "freq": "日",
                               "tickers": {"bull": ETF_BULL, "bear": ETF_BEAR},
                               "historyDays": len(ser),
                               "methodNote": "看多 AUM ÷(看多 + 看空);ProShares TQQQ/UPRO vs SQQQ/SPXU"}, **(info or {})))
    out["US_SOX"] = sox_card(read_csv(base, "SOX"), today, status.get("SOX"), cfg)
    comp = us_composite(out["US_FINRA"], out["US_ETF"])
    out["US_composite"] = comp
    zs = {"US": comp["zone"], "JP": out["JP"]["vote"], "KR": out["KR"]["vote"]}
    valid = int(comp["valid"]) + sum(1 for m in ("JP", "KR") if valid_card(out[m]))
    hot, cold = list(zs.values()).count("hot"), list(zs.values()).count("cold")
    v = "hot" if hot >= cfg["min_same"] else "cold" if cold >= cfg["min_same"] else None
    out["votes"] = {"intlLeverage": v, "markets": zs, "valid": valid, "hot": hot, "cold": cold,
                    "note": None if valid >= cfg["min_same"] else "有效市場 < 2,無方向"}
    return out


# ------------------------------------------------------------------ 抓取
class Fetcher:
    def __init__(self, session=None, sleep=time.sleep, gap=3.0):
        import requests
        self.s = session or requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept-Language": "en,ja;q=0.8,ko;q=0.6"})
        self.sleep, self.gap, self.last, self.robots, self.blocked = sleep, gap, {}, {}, set()

    def _wait(self, host):
        w = self.gap - (time.time() - self.last.get(host, 0))
        if w > 0:
            self.sleep(w)
        self.last[host] = time.time()

    def _robots_ok(self, url):
        p = urlsplit(url)
        base = "%s://%s" % (p.scheme, p.netloc)
        if base not in self.robots:
            self._wait(p.netloc)
            rp = None
            try:
                r = self.s.get(base + "/robots.txt", timeout=30)
                if r.status_code == 200 and "<html" not in r.text[:200].lower():
                    rp = robotparser.RobotFileParser()
                    rp.parse(r.text.splitlines())
            except Exception:                     # noqa: BLE001
                rp = None
            self.robots[base] = rp
        rp = self.robots[base]
        return rp is None or (rp.can_fetch(UA, url) and rp.can_fetch("*", url))

    def get(self, url, method="GET", **kw):
        host = urlsplit(url).netloc
        if host in self.blocked:
            raise RuntimeError("%s 已被擋,停止" % host)
        if not self._robots_ok(url):
            raise RuntimeError("robots.txt 不允許 %s" % url)
        self._wait(host)
        r = self.s.request(method, url, timeout=90, **kw)
        if r.status_code in (403, 429) or b"captcha" in r.content[:3000].lower():
            self.blocked.add(host)
            raise RuntimeError("HTTP %s(被擋或驗證碼)%s" % (r.status_code, url))
        if r.status_code != 200:
            raise RuntimeError("HTTP %s %s" % (r.status_code, url))
        return r


def links(html, base, pat):
    out = []
    for m in re.finditer(r'href=["\']([^"\']+)["\']', html, re.I):
        u = urljoin(base, m.group(1))
        if re.search(pat, u, re.I) and u not in out:
            out.append(u)
    return out


def xlsx_sheets(content):
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    return [[list(r) for r in ws.iter_rows(values_only=True)] for ws in wb.worksheets]


def xls_sheets(content):
    import xlrd
    wb = xlrd.open_workbook(file_contents=content)
    return [[sh.row_values(i) for i in range(sh.nrows)] for sh in wb.sheets()]


def sheets_of(content, url):
    u = url.lower().split("?")[0]
    return xls_sheets(content) if u.endswith(".xls") else xlsx_sheets(content)


def fetch_us(f):
    r = f.get(FINRA_PAGE)
    xl = links(r.text, FINRA_PAGE, r"margin-statistics.*\.xlsx($|\?)") or links(r.text, FINRA_PAGE, r"\.xlsx($|\?)")
    if not xl:
        raise RuntimeError("FINRA 頁面找不到 Excel 連結")
    x = f.get(xl[0])
    sh = xlsx_sheets(x.content)
    return parse_finra(sh[0])


def fetch_jp(f, have_dates, backfill=False, warn=None):
    """→ (列, 用到的檔案);週表永遠抓(最近 5 週);缺口或 backfill → 再抓過去推移表"""
    rows, used = [], []
    page = f.get(JPX_WEEKLY)
    wk = sorted(links(page.text, JPX_WEEKLY, r"_mtcurrent\.xlsx($|\?)"))
    for u in wk:
        fn = u.rsplit("/", 1)[-1]
        d8 = re.match(r"(\d{8})_", fn)
        if d8 and not backfill and to_date(d8.group(1)).isoformat() in have_dates:
            continue
        rows.append(parse_jpx_weekly(xlsx_sheets(f.get(u).content)[0], fn))
        used.append(fn)
    oldest = min((to_date(re.match(r"(\d{8})", u.rsplit("/", 1)[-1]).group(1)) for u in wk
                  if re.match(r"(\d{8})", u.rsplit("/", 1)[-1])), default=None)
    latest_have = max((to_date(d) for d in have_dates), default=None)
    gap = latest_have is None or (oldest and (oldest - latest_have).days > 7)
    if backfill or gap:
        hp = f.get(JPX_HISTORY)
        files = links(hp.text, JPX_HISTORY, r"\.(xlsx|xls)($|\?)")
        files = [u for u in files if "mtgk" not in u]              # 一般/制度 別表不需要,合計在 mtsk
        for u in sorted(files, key=lambda u: (u.lower().endswith(".xlsx"), u)):   # 舊 .xls 先、新 .xlsx 後覆蓋
            fn = u.rsplit("/", 1)[-1]
            sh = sheets_of(f.get(u).content, u)
            rows.extend(parse_jpx_history(sh[0], fn))
            used.append(fn)
    # 優先順序:週表 > 當年 .xlsx > 舊 .xls(同一天後者覆蓋前者)
    pri = lambda r: 2 if "mtcurrent" in r["src"] else 1 if r["src"].endswith(".xlsx") else 0
    by = {}
    for r in sorted(rows, key=pri):
        o = by.get(r["date"])
        if o and warn is not None and o["buy_myen"] and abs(r["buy_myen"] / o["buy_myen"] - 1) * 100 > LEV_CFG["overlap_tol_pct"]:
            warn.append("JP %s 買残 %s(%s)vs %s(%s)" % (r["date"], o["buy_myen"], o["src"], r["buy_myen"], r["src"]))
        by[r["date"]] = r
    return [by[k] for k in sorted(by)], used


def fetch_jp_mcap(f):
    page = f.get(JPX_MCAP)
    xl = links(page.text, JPX_MCAP, r"historical-jika\.xlsx($|\?)")
    if not xl:
        raise RuntimeError("JPX 時価総額頁找不到 historical-jika.xlsx")
    return parse_jpx_mcap(xlsx_sheets(f.get(xl[0]).content))


def fetch_kr(f, start, end):
    body = {"dmSearch": {"tmpV40": "1000000", "tmpV41": "1", "tmpV1": "D", "tmpV45": start.strftime("%Y%m%d"),
                         "tmpV46": end.strftime("%Y%m%d"), "OBJ_NM": KOFIA_OBJ}}
    r = f.get(KOFIA_URL, method="POST", json=body,
              headers={"Content-Type": "application/json; charset=UTF-8", "Referer": "https://freesis.kofia.or.kr/"})
    return parse_kofia(r.json())


PROSHARES_CSV = "https://accounts.profunds.com/etfdata/ByFund/%s-historical_nav.csv"
DIREXION_HOLD = "https://www.direxion.com/holdings/%s.csv"
DIREXION_PAGE = "https://www.direxion.com/product/daily-semiconductor-bull-bear-3x-etfs"


def fetch_etf(f, keep_from=LEV_CFG["etf_keep_from"]):
    out = []
    for t in ETF_BULL + ETF_BEAR:
        out += [r for r in parse_proshares(f.get(PROSHARES_CSV % t).text, t) if r["date"] >= keep_from]
    return out


def fetch_sox(f):
    """→ 每檔一列:資料日 = holdings 的 TradeDate,淨值取產品頁(記下淨值日期)"""
    navs = parse_direxion_nav(f.get(DIREXION_PAGE).text)
    out = []
    for t in (SOX_BULL, SOX_BEAR):
        d, sh = parse_direxion_holdings(f.get(DIREXION_HOLD % t).text, t)
        nav, nd = navs[t]
        if not d or not sh or not nav:
            raise ValueError("Direxion %s 資料不完整" % t)
        if (d - nd).days > 5:
            raise ValueError("Direxion %s 淨值日 %s 與股數日 %s 相差太多" % (t, nd, d))
        out.append({"key": "%s|%s" % (d.isoformat(), t), "date": d.isoformat(), "ticker": t, "shares": sh, "nav": nav,
                    "nav_date": nd.isoformat()})
    return out


def update(base=".", fetcher=None, today=None, backfill=None):
    """抓各來源、合併進 leverage_hist/*.csv、寫 fetch_status.json → (status, warnings)"""
    today = today or (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=8)).date()
    f = fetcher or Fetcher()
    status, warn, info = {}, [], {}

    def run(key, fn):
        try:
            info[key] = fn()
            status[key] = None
        except Exception as e:                     # noqa: BLE001
            status[key] = str(e)[:200]
            print("✗ %s 抓取失敗:%s" % (key, e))

    def us():
        old = read_csv(base, "US")
        rows, n = merge(old, [{"month": r["month"], "debit_musd": r["debit_musd"]} for r in fetch_us(f)],
                        "month", today, warn, "debit_musd")
        write_csv(base, "US", rows)
        return {"rows": len(rows), "added": n, "last": rows[-1]["month"]}

    def jp():
        old = read_csv(base, "JP")
        bf = backfill if backfill is not None else not old
        new, used = fetch_jp(f, {r["date"] for r in old}, bf, warn)
        rows, n = merge(old, new, "date", today, warn, "buy_myen")
        write_csv(base, "JP", rows)
        return {"rows": len(rows), "added": n, "last": rows[-1]["date"], "files": used}

    def jp_mcap():
        old = read_csv(base, "JP_MCAP")
        rows, n = merge(old, fetch_jp_mcap(f), "month_end", today, warn, "mcap_myen")
        write_csv(base, "JP_MCAP", rows)
        return {"rows": len(rows), "added": n, "last": rows[-1]["month_end"]}

    def kr():
        old = read_csv(base, "KR")
        bf = backfill if backfill is not None else not old
        start = dt.date(2016, 1, 1) if bf else today - dt.timedelta(days=40)
        rows, n = merge(old, fetch_kr(f, start, today), "date", today, warn, "credit_mkrw")
        write_csv(base, "KR", rows)
        return {"rows": len(rows), "added": n, "last": rows[-1]["date"]}

    def etf():
        old = read_csv(base, "ETF")
        rows, n = merge(old, fetch_etf(f), "key", today, warn, "aum")
        write_csv(base, "ETF", rows)
        return {"rows": len(rows), "added": n, "last": max(r["date"] for r in rows)}

    def sox():
        old = read_csv(base, "SOX")
        new = fetch_sox(f)
        # 規格 3.2:單日流通股數變動 > 30% → 可疑,不採用(保留前一筆)
        last = {}
        for r in old:
            if r["date"] < new[0]["date"]:
                last[r["ticker"]] = r
        keep = []
        for r in new:
            o = last.get(r["ticker"])
            if o and num(o["shares"]) and abs(r["shares"] / num(o["shares"]) - 1) * 100 > LEV_CFG["etf_suspect_move"]:
                warn.append("SOX %s %s 流通股數 %s → %s 變動 > 30%%,不採用" % (r["ticker"], r["date"], o["shares"], r["shares"]))
                continue
            keep.append(r)
        rows, n = merge(old, keep, "key", today, warn, "shares")
        write_csv(base, "SOX", rows)
        return {"rows": len(rows), "added": n, "last": max(r["date"] for r in rows) if rows else None}

    run("US", us)
    run("ETF", etf)
    run("SOX", sox)
    run("JP", jp)
    run("JP_MCAP", jp_mcap)
    run("KR", kr)
    st = {k: {"at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "err": status[k], "info": info.get(k)}
          for k in status}
    os.makedirs(os.path.join(base, HIST), exist_ok=True)
    with open(os.path.join(base, HIST, "fetch_status.json"), "w", encoding="utf-8") as fh:
        json.dump(st, fh, ensure_ascii=False, indent=1)
    return status, warn, info


def main(argv):
    base = "."
    bf = True if "--backfill" in argv else None
    status, warn, info = update(base, backfill=bf)
    out = compute(base, status=status)
    out["fetch"] = {k: {"err": status[k], "info": info.get(k)} for k in status}
    out["warnings"] = warn[:30]
    with open(os.path.join(base, LATEST), "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    for k in ("US_FINRA", "US_ETF", "US_SOX", "JP", "KR"):
        c = out[k]
        print(k, c.get("asOf"), "value", c.get("value"), "pctl", c.get("pctl"), "n", c.get("n_hist"), "zone", c.get("zone"),
              "vote", c.get("vote"), c.get("note") or "")
    print("votes", json.dumps(out["votes"], ensure_ascii=False))
    print("fetch", json.dumps(out["fetch"], ensure_ascii=False))
    if warn:
        print("warnings(%d)" % len(warn), warn[:30])
    return 0 if all(v is None for v in status.values()) else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
