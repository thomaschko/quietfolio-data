# -*- coding: utf-8 -*-
"""
============================================================
intl_rank_probe.py — 美/日/韓 成交值排行榜 資料源可行性驗證(Phase 0)
============================================================
目的:在【你的 GitHub Actions】上跑一次(沙盒連不到這些站,無法在別處驗證),確認:
  1. 三地「股票清單」來源抓不抓得到、欄位長怎樣
       US  nasdaqtrader.com SymDir(nasdaqlisted / otherlisted)
       JP  JPX 東証上場銘柄一覧 data_j.xlsx
       KR  KRX KIND 上市公司清單(corpList)
  2. yfinance 對 US / JP(.T) / KR(.KS .KQ) 的日線:日期、收盤、成交量單位是否合理
  3. 批次下載速度與失敗率(推算全市場一次要多久)
  4. 匯率 JPY=X、KRW=X
  5. 最後一根日線是否為「已收盤」(避免把盤中不完整成交量當成全日)
  6. (選配)yfinance 內建 screener、pykrx 在沒有 KRX 帳密時行不行

純驗證:不寫入任何排行榜檔,只輸出 intl_rank_probe_result.json 與文字摘要。
每個檢查獨立 try/except,任何一項失敗都不影響其他項。
============================================================
"""
import io, json, re, sys, time, datetime as dt
import requests
import pandas as pd

UA = {"User-Agent": "quietfolio-intl-rank-probe/1.0 (research; https://github.com/thomaschko)"}
RESULT = {"run_at_utc": dt.datetime.utcnow().isoformat(timespec="seconds"), "checks": {}}


def check(name):
    def deco(fn):
        t0 = time.time()
        try:
            out = fn()
            RESULT["checks"][name] = {"ok": True, "sec": round(time.time() - t0, 1), **(out or {})}
        except Exception as e:  # noqa
            RESULT["checks"][name] = {"ok": False, "sec": round(time.time() - t0, 1),
                                      "error": f"{type(e).__name__}: {str(e)[:300]}"}
        c = RESULT["checks"][name]
        print(("✓" if c["ok"] else "✗"), name, "|", {k: v for k, v in c.items() if k not in ("ok",)})
        return fn
    return deco


def get(url, **kw):
    r = requests.get(url, headers=UA, timeout=60, **kw)
    r.raise_for_status()
    return r


# ───────────────────────── 1. 股票清單 ─────────────────────────
US_SYMS = []; JP_SYMS = []; KR_SYMS = []

@check("us_symbol_directory")
def _():
    base = "https://www.nasdaqtrader.com/dynamic/SymDir/"
    a = pd.read_csv(io.StringIO(get(base + "nasdaqlisted.txt").text), sep="|", dtype=str)
    b = pd.read_csv(io.StringIO(get(base + "otherlisted.txt").text), sep="|", dtype=str)
    a = a[~a.iloc[:, 0].str.startswith("File Creation", na=False)]
    b = b[~b.iloc[:, 0].str.startswith("File Creation", na=False)]
    na = a[(a["Test Issue"] == "N") & (a["ETF"] == "N")]
    nb = b[(b["Test Issue"] == "N") & (b["ETF"] == "N")]
    syms = list(na["Symbol"]) + list(nb["ACT Symbol"])
    syms = [s.replace(".", "-") for s in syms if isinstance(s, str) and re.fullmatch(r"[A-Z.\-]{1,6}", s)]
    US_SYMS.extend(sorted(set(syms)))
    return {"nasdaqlisted_cols": list(a.columns), "otherlisted_cols": list(b.columns),
            "rows_nasdaq": len(a), "rows_other": len(b), "non_etf_symbols": len(US_SYMS),
            "sample": US_SYMS[:5]}

@check("jp_jpx_listed_issues")
def _():
    url = "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xlsx"
    df = pd.read_excel(io.BytesIO(get(url).content), dtype=str)
    seg_col = [c for c in df.columns if "市場" in str(c)][0]
    code_col = [c for c in df.columns if "コード" in str(c) and "業種" not in str(c) and "規模" not in str(c)][0]
    dom = df[df[seg_col].str.contains("内国株式", na=False)]
    JP_SYMS.extend(sorted({f"{c.strip()}.T" for c in dom[code_col].dropna()}))
    return {"columns": list(df.columns), "rows": len(df), "domestic_stocks": len(dom),
            "segments": df[seg_col].value_counts().to_dict(), "sample": JP_SYMS[:5]}

KR_META = {}

@check("kr_krx_kind_corplist")
def _():
    url = "https://kind.krx.co.kr/corpgeneral/corpList.do?method=download&searchType=13"
    r = get(url)
    html = r.content.decode("euc-kr", errors="replace")
    df = pd.read_html(io.StringIO(html), header=0, dtype=str)[0]
    mk = [c for c in df.columns if "시장" in str(c)][0]
    cd = [c for c in df.columns if "종목코드" in str(c)][0]
    nm = [c for c in df.columns if "회사명" in str(c)][0]
    for _, row in df.iterrows():
        code = str(row[cd]).strip().zfill(6)
        m = str(row[mk])
        suf = ".KS" if "유가" in m else (".KQ" if "코스닥" in m else None)
        if suf:
            KR_SYMS.append(code + suf); KR_META[code + suf] = str(row[nm])
    return {"columns": list(df.columns), "rows": len(df), "markets": df[mk].value_counts().to_dict(),
            "usable_symbols": len(KR_SYMS), "sample": KR_SYMS[:5]}

# ───────────────────────── 2. yfinance 日線抽樣 ─────────────────────────
try:
    import yfinance as yf
    HAVE_YF = True
except Exception as e:  # noqa
    HAVE_YF = False
    RESULT["checks"]["yfinance_import"] = {"ok": False, "error": str(e)[:200]}

SAMPLES = ["NVDA", "MU", "6857.T", "285A.T", "005930.KS", "000660.KS", "247540.KQ"]

@check("yfinance_daily_samples")
def _():
    df = yf.download(SAMPLES, period="15d", interval="1d", auto_adjust=False,
                     group_by="ticker", threads=True, progress=False)
    out = {}
    for s in SAMPLES:
        try:
            d = df[s].dropna(subset=["Close"])
            last = d.tail(3)
            out[s] = {"last_dates": [str(i.date()) for i in last.index],
                      "close": [round(float(x), 2) for x in last["Close"]],
                      "volume": [int(x) for x in last["Volume"]],
                      "value_last_local_100m": round(float(last["Close"].iloc[-1] * last["Volume"].iloc[-1]) / 1e8, 1)}
        except Exception as e:  # noqa
            out[s] = {"error": str(e)[:120]}
    return {"samples": out}

@check("fx_rates")
def _():
    df = yf.download(["JPY=X", "KRW=X"], period="7d", interval="1d", progress=False, group_by="ticker")
    return {"JPY_per_USD": round(float(df["JPY=X"]["Close"].dropna().iloc[-1]), 2),
            "KRW_per_USD": round(float(df["KRW=X"]["Close"].dropna().iloc[-1]), 2)}

@check("session_complete_check")
def _():
    from zoneinfo import ZoneInfo
    spec = {"US": ("NVDA", "America/New_York", dt.time(16, 20)),
            "JP": ("6857.T", "Asia/Tokyo", dt.time(15, 40)),
            "KR": ("005930.KS", "Asia/Seoul", dt.time(15, 40))}
    out = {}
    for mk, (sym, tz, close_t) in spec.items():
        d = yf.download(sym, period="7d", interval="1d", auto_adjust=False, progress=False)
        if isinstance(d.columns, pd.MultiIndex):
            d.columns = d.columns.get_level_values(0)
        d = d.dropna(subset=["Close"])
        now = dt.datetime.now(ZoneInfo(tz))
        last = d.index[-1].date()
        out[mk] = {"now_local": now.strftime("%Y-%m-%d %H:%M"), "last_bar": str(last),
                   "last_bar_is_today_local": last == now.date(),
                   "market_should_be_closed": now.time() >= close_t or now.weekday() >= 5,
                   "last_volume": int(d["Volume"].iloc[-1])}
    return out

# ───────────────────────── 3. 批次速度與失敗率 ─────────────────────────
def bulk_speed(label, syms, n=300, period="30d"):
    if not syms:
        return {"skipped": "沒有股票清單"}
    sub = syms[:: max(1, len(syms) // n)][:n]
    t0 = time.time()
    df = yf.download(sub, period=period, interval="1d", auto_adjust=False, group_by="ticker",
                     threads=True, progress=False)
    sec = time.time() - t0
    got = 0
    for s in sub:
        try:
            if df[s].dropna(subset=["Close"]).shape[0] >= 15:
                got += 1
        except Exception:
            pass
    return {"requested": len(sub), "with_15plus_days": got, "fail_rate": round(1 - got / len(sub), 3),
            "sec": round(sec, 1), "est_full_universe_min": round(sec / len(sub) * len(syms) / 60, 1),
            "universe": len(syms)}

@check("bulk_speed_us")
def _(): return bulk_speed("US", US_SYMS)

@check("bulk_speed_jp")
def _(): return bulk_speed("JP", JP_SYMS)

@check("bulk_speed_kr")
def _(): return bulk_speed("KR", KR_SYMS)

# ───────────────────────── 4. 選配 ─────────────────────────
@check("yfinance_screener_optional")
def _():
    # 不同 yfinance 版本介面不同;失敗不代表不能做排行榜,只代表不能用「先篩再抓」的優化
    res = yf.screen("most_actives", count=25)
    quotes = res.get("quotes", [])
    return {"rows": len(quotes), "first_keys": sorted(list(quotes[0].keys()))[:25] if quotes else []}

@check("pykrx_without_credentials_optional")
def _():
    from pykrx import stock
    d = dt.date.today()
    for _ in range(7):
        s = d.strftime("%Y%m%d")
        try:
            df = stock.get_market_ohlcv(s, market="ALL")
            if df is not None and len(df) > 100:
                top = df.sort_values("거래대금", ascending=False).head(3)
                return {"date": s, "rows": len(df), "columns": list(df.columns),
                        "top3": [[str(i), int(v)] for i, v in zip(top.index, top["거래대금"])]}
        except Exception as e:  # noqa
            last_err = str(e)[:200]
        d -= dt.timedelta(days=1)
    raise RuntimeError("7 天內都取不到(可能需要 KRX_ID/KRX_PW):" + locals().get("last_err", ""))

json.dump(RESULT, open("intl_rank_probe_result.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print("\n=== 摘要 ===")
for k, v in RESULT["checks"].items():
    print(("✓" if v.get("ok") else "✗"), k, "" if v.get("ok") else v.get("error", ""))
