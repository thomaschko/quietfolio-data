# -*- coding: utf-8 -*-
"""
============================================================
intl_rank.py — 美股/日股/韓股 成交值排行(2026-10-19 新增)
============================================================
資料流:本程式(GitHub Actions)→ intl_rank.json → GAS IntlRankFetch.js → Page.html「排行榜」切換。
計算全部交給 intl_rank_core(已有單元測試,這裡不改其邏輯);本檔只負責:股票清單、
分批下載、收盤判定、匯率、標籤、合併輸出。

資料源(2026-10-19 Phase 0 在 GHA 實測後決定,三市場都可全市場一次抓):
  US  nasdaqtrader SymDir nasdaqlisted + otherlisted(非 ETF、非 Test Issue)    約 4 分鐘
  JP  JPX 東証上場銘柄一覧 data_j.xlsx「内国株式」                                約 2.5 分鐘
  KR  KRX KIND corpList(유가→.KS、코스닥→.KQ,排除 코넥스)                       約 1.5 分鐘
  日線一律 yfinance(auto_adjust=False:成交值用原始收盤 × 成交量,不用還原價)

用法:python intl_rank.py --markets us,jp,kr [--top-n 50] [--out intl_rank.json]
規則:
  - 各市場用「當地時區 + 收盤後緩衝」判斷最後一根是否已收盤(不用台北時間)
  - 本次成功的市場覆蓋;失敗或未指定的市場沿用舊資料並標 stale(intl_rank_core.merge_preserve)
  - 本次指定的市場全部失敗 → 不覆寫輸出檔、結束碼 1
  - 抓不到匯率就不放 usd 欄;抓不到的成交量不補
============================================================
"""
import io
import os
import re
import sys
import json
import time
import argparse
import datetime as dt
from zoneinfo import ZoneInfo

import pandas as pd

import intl_rank_core as core

UA = {"User-Agent": "quietfolio-intl-rank/1.0 (research; https://github.com/thomaschko)"}
OUT_FILE = "intl_rank.json"
TOTALS_FILE = "intl_market_totals.json"   # 各市場全市場每日總成交值(億,當地幣別),給 group_flow.py 算族群占比
TOTALS_KEEP_DAYS = 60
WATCH_FILE = "intl_watch.json"
PERIOD = "45d"
CHUNK = 350
BATCH_SLEEP = 1.5
RETRY_WAITS = (5, 15)
MIN_VALID_DAYS = 15
SIZE_LIMIT = 45000

MARKETS = {
    "us": {"label": "美股", "currency": "USD", "unit_short": "美元", "price_dec": 2,
           "tz": "America/New_York", "close": dt.time(16, 20), "fx": None},
    "jp": {"label": "日股", "currency": "JPY", "unit_short": "日圓", "price_dec": 0,
           "tz": "Asia/Tokyo", "close": dt.time(15, 40), "fx": "JPY=X"},
    "kr": {"label": "韓股", "currency": "KRW", "unit_short": "韓元", "price_dec": 0,
           "tz": "Asia/Seoul", "close": dt.time(18, 10), "fx": "KRW=X"},
    # 韓股收盤判定 18:10(2026-10-19 改,原 15:40):16:00~18:00 盤後單一價交易(시간외 단일가)的量
    # 也算進當日日線;10/19 首爾 17:40 跑的成交值比 18:20 少約 1.8%(SK 海力士 48,849 → 49,710)
}

US_BASE = "https://www.nasdaqtrader.com/dynamic/SymDir/"
JPX_URL = "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xlsx"
KIND_URL = "https://kind.krx.co.kr/corpgeneral/corpList.do?method=download&searchType=13"
US_EXCLUDE_NAME = re.compile(r"\b(Warrants?|Rights?|Units?|Preferred|Notes)\b", re.I)
US_SYMBOL = re.compile(r"[A-Z]{1,5}([.\-][A-Z]{1,2})?")


def log(msg):
    print(msg, flush=True)


# ============================================================
# 股票清單(解析與下載分開,解析可用假檔測試)
# ============================================================
def _symdir(text):
    df = pd.read_csv(io.StringIO(text), sep="|", dtype=str)
    return df[~df.iloc[:, 0].str.startswith("File Creation", na=False)]


def parse_us(nasdaq_text, other_text):
    """→ {ticker: {"code","name"}}。ticker 用 Yahoo 格式(BRK.B → BRK-B),code 保留交易所寫法。"""
    out = {}
    a, b = _symdir(nasdaq_text), _symdir(other_text)
    a = a[(a["Test Issue"] == "N") & (a["ETF"] == "N") & (a["Financial Status"] == "N")]
    b = b[(b["Test Issue"] == "N") & (b["ETF"] == "N")]
    rows = [(r["Symbol"], r["Security Name"]) for _, r in a.iterrows()] + \
           [(r["ACT Symbol"], r["Security Name"]) for _, r in b.iterrows()]
    for sym, name in rows:
        if not isinstance(sym, str) or not isinstance(name, str):
            continue
        sym = sym.strip()
        if not US_SYMBOL.fullmatch(sym) or US_EXCLUDE_NAME.search(name):
            continue
        out[sym.replace(".", "-")] = {"code": sym, "name": name.split(" - ")[0].strip()[:40]}
    return out


def parse_jp(df):
    """data_j.xlsx 的 DataFrame → {"7203.T": {...}}。只取「内国株式」;代碼一律字串(有 285A 這種)。"""
    seg = [c for c in df.columns if "市場" in str(c)][0]
    code_col = [c for c in df.columns if "コード" in str(c) and "業種" not in str(c) and "規模" not in str(c)][0]
    name_col = [c for c in df.columns if "銘柄名" in str(c)][0]
    dom = df[df[seg].astype(str).str.contains("内国株式", na=False)]
    out = {}
    for _, r in dom.iterrows():
        code = str(r[code_col]).strip()
        if code.endswith(".0"):
            code = code[:-2]
        if code and code != "nan":
            out[f"{code}.T"] = {"code": code, "name": str(r[name_col]).strip()[:40]}
    return out


def parse_kr(html):
    """KIND corpList(HTML 表格)→ {"005930.KS": {...}}。排除 코넥스;代碼補零到 6 碼。
    pandas 3 的 read_html 不收 dtype(10/19 探測踩到),代碼會被讀成數字 → zfill 補回前導 0。"""
    df = pd.read_html(io.StringIO(html), header=0)[0]
    mk = [c for c in df.columns if "시장" in str(c)][0]
    cd = [c for c in df.columns if "종목코드" in str(c)][0]
    nm = [c for c in df.columns if "회사명" in str(c)][0]
    out = {}
    for _, r in df.iterrows():
        m = str(r[mk])
        suf = ".KS" if "유가" in m else (".KQ" if "코스닥" in m else None)
        if not suf:
            continue
        code = str(r[cd]).strip()
        if code.endswith(".0"):
            code = code[:-2]
        code = code.zfill(6)
        out[code + suf] = {"code": code, "name": str(r[nm]).strip()[:40]}
    return out


def fetch_universe(market, get=None):
    import requests
    get = get or (lambda u: requests.get(u, headers=UA, timeout=60))

    def ok(r):
        r.raise_for_status()
        return r
    if market == "us":
        return parse_us(ok(get(US_BASE + "nasdaqlisted.txt")).text, ok(get(US_BASE + "otherlisted.txt")).text)
    if market == "jp":
        return parse_jp(pd.read_excel(io.BytesIO(ok(get(JPX_URL)).content), dtype=str))
    if market == "kr":
        return parse_kr(ok(get(KIND_URL)).content.decode("euc-kr", errors="replace"))
    raise ValueError(market)


# ============================================================
# 分批下載
# ============================================================
def _yf_download(chunk):
    import yfinance as yf
    return yf.download(chunk, period=PERIOD, interval="1d", auto_adjust=False, group_by="ticker",
                       threads=True, progress=False)


def _split(df, tickers):
    """yfinance 寬表(ticker, field)→ {ticker: (close Series, volume Series)}。"""
    out = {}
    if df is None or len(df) == 0:
        return out
    multi = isinstance(df.columns, pd.MultiIndex)
    for t in tickers:
        try:
            sub = df[t] if multi else df
            c, v = sub["Close"], sub["Volume"]
        except Exception:
            continue
        out[t] = (c, v)
    return out


def download_panel(tickers, fetch=_yf_download, sleep=time.sleep, clock=time.time, deadline=None, chunk=CHUNK):
    """分批下載 → (close 寬表, volume 寬表, stats)。失敗批重試 2 次(5s、15s);超過 deadline 停止,
    用已下載的部分(stats["timed_out"]=True)。有效日數 < MIN_VALID_DAYS 的 ticker 靜默略過。"""
    closes, vols = {}, {}
    stats = {"requested": len(tickers), "batches": 0, "failed_batches": 0, "timed_out": False,
             "scanned": 0, "no_data": 0}
    for i in range(0, len(tickers), chunk):
        if deadline is not None and clock() > deadline:
            stats["timed_out"] = True
            break
        part = tickers[i:i + chunk]
        stats["batches"] += 1
        df = None
        for attempt, wait in enumerate((0,) + RETRY_WAITS):
            if wait:
                sleep(wait)
            try:
                df = fetch(part)
                break
            except Exception as e:
                log(f"    批次 {stats['batches']} 第 {attempt + 1} 次失敗:{type(e).__name__} {str(e)[:80]}")
        if df is None:
            stats["failed_batches"] += 1
        else:
            stats["scanned"] += len(part)
            for t, (c, v) in _split(df, part).items():
                ok = c.notna() & v.notna()
                if int(ok.sum()) >= MIN_VALID_DAYS:
                    closes[t], vols[t] = c, v
        if i + chunk < len(tickers):
            sleep(BATCH_SLEEP)
    stats["got"] = len(closes)
    stats["no_data"] = stats["scanned"] - stats["got"]
    if not closes:
        return None, None, stats
    close = pd.DataFrame(closes).sort_index()
    volume = pd.DataFrame(vols).reindex(close.index)
    idx = pd.to_datetime(close.index)
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    close.index = volume.index = idx
    return close, volume, stats


def fx_on(fx_frame, ticker, asof):
    """asof 當日(找不到就取之前最近一個)的匯率收盤;沒有就回 None。"""
    try:
        s = fx_frame[ticker]["Close"] if isinstance(fx_frame.columns, pd.MultiIndex) else fx_frame["Close"]
        s = s.dropna()
        idx = pd.to_datetime(s.index)
        s.index = idx.tz_localize(None) if idx.tz is not None else idx
        s = s[s.index.normalize() <= pd.Timestamp(asof)]
        return round(float(s.iloc[-1]), 2) if len(s) else None
    except Exception:
        return None


def _yf_fx():
    import yfinance as yf
    return yf.download(["JPY=X", "KRW=X"], period="60d", interval="1d", progress=False, group_by="ticker")


# ============================================================
# 標籤
# ============================================================
def load_tags(path=WATCH_FILE):
    try:
        with open(path, encoding="utf-8") as f:
            w = json.load(f)
    except Exception:
        w = {}
    tags = {k: dict(w.get(k) or {}) for k in MARKETS}
    try:   # 美股以 earnings_keywords.COMPANIES(src6)為準
        from earnings_keywords import COMPANIES
        extra = [s for s in tags["us"] if s not in COMPANIES]
        if extra:
            log(f"  標籤:{extra} 不在 src6 COMPANIES,忽略")
        tags["us"] = {s: tags["us"].get(s) or re.split(r"[(（]", role.split("-", 1)[-1])[0].strip()
                      for s, role in COMPANIES.items()}
    except Exception as e:
        log(f"  ⚠ 讀不到 earnings_keywords.COMPANIES,美股標籤照 intl_watch.json:{e}")
    return tags


# ============================================================
# 單一市場
# ============================================================
def build_market(key, *, universe_fn=fetch_universe, download_fn=download_panel, fx_frame=None,
                 now_utc=None, tags=None, top_n=50, deadline=None):
    """→ (block 或 None, info dict)。任何一步失敗都回 None,不丟例外。"""
    cfg = MARKETS[key]
    info = {"market": key}
    t0 = time.time()
    try:
        meta = universe_fn(key)
        info["universe"] = len(meta)
        if not meta:
            raise RuntimeError("股票清單是空的")
        close, volume, st = download_fn(sorted(meta), deadline=deadline)
        info.update(st)
        if close is None:
            raise RuntimeError("沒有任何股票取得日線")
        now_local = (now_utc or dt.datetime.now(dt.timezone.utc)).astimezone(ZoneInfo(cfg["tz"])).replace(tzinfo=None)
        close, volume, notes = core.drop_incomplete(close, volume, now_local, cfg["close"])
        info["drop_notes"] = notes
        if len(close) < 2:
            raise RuntimeError("可用交易日不足")
        asof = close.index[-1].date()
        # 全市場每日總成交值(族群占比的分母);掃描不完整時不記,避免分母偏小
        if not st.get("timed_out") and not st.get("failed_batches"):
            tot = (close * volume).where(volume.fillna(0) > 0).sum(axis=1) / 1e8
            info["totals"] = {str(d.date()): round(float(v), 1) for d, v in tot.items() if v > 0}
        fx = fx_on(fx_frame, cfg["fx"], asof) if (cfg["fx"] and fx_frame is not None) else None
        note_parts = []
        if st.get("timed_out"):
            note_parts.append(f"時間預算用完,只掃描 {st['scanned']}/{st['requested']} 檔")
        if st.get("failed_batches"):
            note_parts.append(f"{st['failed_batches']} 批下載失敗,掃描檔數不完整")
        note_parts += [n for n in notes if "覆蓋率" in n]
        block = core.build_market_block(
            close, volume, meta, label=cfg["label"], currency=cfg["currency"], unit_short=cfg["unit_short"],
            price_dec=cfg["price_dec"], top_n=top_n, fx_per_usd=fx, tags=(tags or {}).get(key),
            note="；".join(note_parts) or None)
        if block is None:
            raise RuntimeError("build_market_block 沒有結果")
        block["fetched_at"] = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
        if cfg["fx"]:
            info["fx"] = fx
        info.update(asof=str(asof), sec=round(time.time() - t0, 1),
                    fail_rate=round(1 - st["got"] / max(1, st["scanned"]), 3))
        return block, info
    except Exception as e:
        info.update(error=f"{type(e).__name__}: {str(e)[:200]}", sec=round(time.time() - t0, 1))
        return None, info


# ============================================================
# 主流程
# ============================================================
def run(markets, out=OUT_FILE, top_n=50, build=build_market, fx_loader=_yf_fx, budget_min=None, tags=None):
    """回傳結束碼。指定的市場全部失敗 → 不寫檔、回 1。"""
    budget_min = float(budget_min if budget_min is not None else os.environ.get("INTL_BUDGET_MIN", 25))
    deadline = time.time() + budget_min * 60
    generated = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    old = None
    try:
        with open(out, encoding="utf-8") as f:
            old = json.load(f)
    except Exception:
        pass
    tags = tags if tags is not None else load_tags()
    fx_frame = None
    if any(MARKETS[m]["fx"] for m in markets):
        try:
            fx_frame = fx_loader()
        except Exception as e:
            log(f"  ⚠ 匯率下載失敗(usd 欄位略過):{e}")
    new, infos = {}, []
    for m in markets:
        log(f"\n== {MARKETS[m]['label']} ==")
        block, info = build(m, fx_frame=fx_frame, tags=tags, top_n=top_n, deadline=deadline)
        infos.append(info)
        old_asof = ((old or {}).get("markets", {}).get(m) or {}).get("asof")
        if block and old_asof and block.get("asof") and block["asof"] < old_asof:
            # 資料日不可倒退(例如 Yahoo 最後一天資料不完整被丟掉):沿用舊的、標失敗
            info["error"] = f"新資料日 {block['asof']} 比現有的 {old_asof} 舊,不覆寫"
            block = None
        new[m] = block
        if block:
            log(f"  清單 {info.get('universe')} 檔、取得 {info.get('got')}/{info.get('scanned')}(無資料 {info.get('no_data')})"
                f"、失敗率 {info.get('fail_rate')}、批次失敗 {info.get('failed_batches')}、asof {info.get('asof')}"
                f"、匯率 {info.get('fx')}、{info.get('sec')} 秒")
            for n in info.get("drop_notes") or []:
                log(f"  · {n}")
            for r in block["by_value"][:5]:
                log(f"    #{r['rank']} {r['code']} {r['name']} 收盤 {r['close']} 成交值 {r['value']} 億{block['unit_short']}"
                    + (f"(≈{r['usd']} 億美元)" if 'usd' in r else ""))
        else:
            log(f"  ✗ 失敗:{info.get('error')}({info.get('sec')} 秒)")
    if not any(new.values()):
        log("\n✗ 指定的市場全部失敗,不覆寫輸出檔")
        return 1
    save_totals({i["market"]: i["totals"] for i in infos if i.get("totals")},
                path=os.path.join(os.path.dirname(out) or ".", TOTALS_FILE))
    payload = core.merge_preserve(old, new, generated)
    payload["last_run"] = {"at": generated, "requested": list(markets),
                           "ok": [m for m in markets if new.get(m)], "failed": [m for m in markets if not new.get(m)]}
    payload, n = core.fit_size(payload, limit=SIZE_LIMIT)
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    with open(out, "w", encoding="utf-8") as f:
        f.write(text)
    log(f"\n→ {out}:{len(text)} 字元、top_n {n}、市場 "
        + "、".join(f"{k}{'(舊)' if v.get('stale') else ''}" for k, v in payload["markets"].items()))
    return 0


def save_totals(new_totals, path=TOTALS_FILE, keep=TOTALS_KEEP_DAYS):
    """{market: {date: total}} 併入既有檔,每市場只留最近 keep 個日期。"""
    if not new_totals:
        return
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}
    for m, series in new_totals.items():
        merged = {**data.get(m, {}), **series}
        data[m] = dict(sorted(merged.items())[-keep:])
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))


def expected_asof(market, now_utc=None):
    """該市場「最近一個應已收盤的平日」(不認得國定假日:假日那天會多補跑一次,結果資料日不變)。"""
    cfg = MARKETS[market]
    now = (now_utc or dt.datetime.now(dt.timezone.utc)).astimezone(ZoneInfo(cfg["tz"])).replace(tzinfo=None)
    d = now.date() if now.time() >= cfg["close"] else now.date() - dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d.isoformat()


def stale_markets(candidates, out=OUT_FILE, now_utc=None):
    """candidates 中「檔案裡的資料日早於應有資料日」的市場(檔案讀不到 → 全部算落後)。
    2026-10-22:10/21 晚上 22:00 的日韓排程被延到台北 02:20 才跑,Yahoo 這時段前一日收盤價是空的,
    10/21 被覆蓋率保護丟掉、日韓停在 10/20 → 早上美股那次順便補跑落後的市場。"""
    try:
        with open(out, encoding="utf-8") as f:
            have = {k: (v or {}).get("asof") for k, v in json.load(f).get("markets", {}).items()}
    except Exception:
        have = {}
    out_m = []
    for m in candidates:
        exp = expected_asof(m, now_utc)
        if not have.get(m) or have[m] < exp:
            log(f"  補跑 {MARKETS[m]['label']}:檔案資料日 {have.get(m)} < 應有 {exp}")
            out_m.append(m)
        else:
            log(f"  {MARKETS[m]['label']} 資料日 {have[m]} 已是最新,不補跑")
    return out_m


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--markets", default="us,jp,kr")
    ap.add_argument("--catchup", default="", help="資料日落後才加跑的市場(逗號分隔),例:jp,kr")
    ap.add_argument("--top-n", type=int, default=50)
    ap.add_argument("--out", default=OUT_FILE)
    a = ap.parse_args(argv)
    markets = [m.strip() for m in a.markets.split(",") if m.strip()]
    catch = [m.strip() for m in a.catchup.split(",") if m.strip() and m.strip() not in markets]
    bad = [m for m in markets + catch if m not in MARKETS]
    if not bad and catch:
        markets += stale_markets(catch, out=a.out)
    if bad or not markets:
        log(f"未知市場:{bad}")
        return 2
    return run(markets, out=a.out, top_n=a.top_n)


if __name__ == "__main__":
    sys.exit(main())
