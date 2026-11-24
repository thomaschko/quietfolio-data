# -*- coding: utf-8 -*-
"""
mood_intl.py — 市場氛圍擴充 Phase 1 + 5:台美日韓指數 MACD(v2026-11-25-mood-intl)
============================================================
規格:「Quietfolio 市場氛圍擴充規格:國際 MACD × 槓桿資金 × 共振警示」Phase 1(MACD)與 Phase 5(只先接 MACD 兩票)。
與規格不同處(以現有架構為準):
  - 規格寫 GAS 每日 15:40 抓 Yahoo、快取在 Sheet mood_macd。現行市場氛圍(分位、極端門檻、整體投票)全部在
    consensus.py(GitHub Actions)算好輸出 consensus.json,GAS 只讀 → MACD 也放在這裡算,投票邏輯只有一份。
    consensus.py 每天於期交所資料後(約 17:00 起)與 23:20 執行,日韓已收盤。
  - 不另做快取:每次直接向 Yahoo 取 10 年 K 棒(5 指數 × 3 週期 = 15 次請求)。抓取失敗 → 沿用上一份 consensus.json
    的該指數結果並標 fetch_failed(卡片顯示),週線資料超過 VOTE_MAX_AGE_DAYS 天則不投票。
資料:Yahoo chart API v8(interval=1d / 1wk / 1mo,range=10y),直接取各週期 K 棒,不從日線重算;用 close(未還原,指數無除權息)。
計算:MACD(12,26,9),DIF% = DIF / close × 100、HIST% = HIST / close × 100;
  歷史分位 = 現值在「同指數、同週期、扣掉前 WARMUP 根暖機、不含當根」DIF% 分布的中位排名百分位;
  極端區沿用 consensus_core.zone(≥ 90 偏熱、≤ 10 過度看空,方向 +1)。
  極端事件統計:逐根用「當時可得」的歷史(擴張視窗,不含當根)算分位 → 不前視;同一段連續極端只取第一根。
只描述,不構成買賣建議。
"""
import datetime as dt
import json
import time
import urllib.parse

import consensus_core as cc

VERSION = "v2026-11-25-mood-intl"
SYMBOLS = [  # (Yahoo 代號, 名稱, 市場, 當地收盤 HHMM)
    ("^TWII", "台股加權", "TW", 1330),
    ("^SOX", "費半", "US", 1600),
    ("^GSPC", "S&P 500", "US", 1600),
    ("^N225", "日經", "JP", 1530),
    ("^KS11", "韓綜", "KR", 1530),
]
INTL = ["^SOX", "^GSPC", "^N225", "^KS11"]
INTERVALS = ["1d", "1wk", "1mo"]
HOSTS = ["https://query1.finance.yahoo.com", "https://query2.finance.yahoo.com"]
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) quietfolio-mood/1.0 (read-only research)"}

MOOD_INTL_CFG = {
    "fast": 12, "slow": 26, "signal": 9,
    "warmup": 35,                     # EMA 暖機,不進分布
    "min_hist": {"1d": 120, "1wk": 120, "1mo": 30},   # 分布樣本不足 → 不給分位
    "low_sample_month": 60,          # 月線樣本 < 60 → lowSample
    "horizons": {"1d": [20, 60], "1wk": [4, 12], "1mo": [3, 6]},
    "event_min_n": 5,                # 事件 n < 5 → lowSample
    "intl_min_same": 3,              # 國際 MACD:4 個指數中 ≥ 3 個週線同邊極端才算一票
    "spark_weeks": 52,
    "vote_max_age_days": 10,         # 週線最後一根超過 10 天(抓取失敗沿用舊值)→ 不投票
    "near_band": 5,                  # 分位距門檻 ≤ 5 → 「接近」提示(不投票)
}
STATE_ZH = {"BULL_EXPAND": "多頭擴張", "BULL_FADE": "多頭收斂", "BEAR_FADE": "空頭收斂", "BEAR_EXPAND": "空頭擴張"}


# ------------------------------------------------------------------ 純計算
def ema(xs, n):
    a, out, e = 2.0 / (n + 1), [], None
    for x in xs:
        e = x if e is None else a * x + (1 - a) * e
        out.append(e)
    return out


def macd(closes, cfg=MOOD_INTL_CFG):
    f, s = ema(closes, cfg["fast"]), ema(closes, cfg["slow"])
    dif = [a - b for a, b in zip(f, s)]
    dea = ema(dif, cfg["signal"])
    hist = [a - b for a, b in zip(dif, dea)]
    return dif, dea, hist


def state_of(dif, hist, prev_hist):
    if prev_hist is None:
        return None
    if dif > 0:
        return "BULL_EXPAND" if hist > 0 and hist > prev_hist else "BULL_FADE"
    return "BEAR_FADE" if hist > prev_hist else "BEAR_EXPAND"


def pctile(value, past, min_n):
    """與 consensus_core.pctile 相同的中位排名百分位,只是最少樣本數可依週期設定"""
    import bisect
    vals = sorted(x for x in past if x is not None)
    if value is None or len(vals) < min_n:
        return None
    lo, hi = bisect.bisect_left(vals, value), bisect.bisect_right(vals, value)
    return round((lo + hi) / 2 / len(vals) * 100, 1)


def rolling_zones(difpct, interval, cfg=MOOD_INTL_CFG):
    """逐根用當時可得的歷史(擴張視窗、不含當根)→ [(pctl, zone)];暖機期與樣本不足為 (None, None)"""
    w, m = cfg["warmup"], cfg["min_hist"][interval]
    out = []
    for i, v in enumerate(difpct):
        if i < w:
            out.append((None, None))
            continue
        p = pctile(v, difpct[w:i], m)
        out.append((p, cc.zone(p, +1)))
    return out


def event_stats(dates, closes, zones, tw_by_key, key_fn, interval, cfg=MOOD_INTL_CFG):
    """進入極端區(連續極端只取第一根)後 h 根的報酬、^TWII 同期報酬、勝率、最大回撤"""
    hs = cfg["horizons"][interval]
    ev = {"hot": [], "cold": []}
    recent = []
    for i, (_, z) in enumerate(zones):
        if z in ("hot", "cold") and (i == 0 or zones[i - 1][1] != z):
            recent.append({"date": dates[i], "zone": z})
            row = {}
            for h in hs:
                if i + h < len(closes):
                    row["ret%d" % h] = closes[i + h] / closes[i] - 1
                    row["mdd%d" % h] = min(closes[i + 1:i + h + 1]) / closes[i] - 1
                    a, b = tw_by_key.get(key_fn(dates[i])), tw_by_key.get(key_fn(dates[i + h]))
                    row["tw%d" % h] = (b / a - 1) if a and b else None
            ev[z].append(row)
    out = {}
    for z, rows in ev.items():
        s = {"n": len(rows), "horizons": hs}
        for h in hs:
            r = [x["ret%d" % h] for x in rows if "ret%d" % h in x]
            t = [x["tw%d" % h] for x in rows if x.get("tw%d" % h) is not None]
            d = [x["mdd%d" % h] for x in rows if "mdd%d" % h in x]
            s["n%d" % h] = len(r)
            s["avgRet%d" % h] = round(sum(r) / len(r) * 100, 2) if r else None
            s["twRet%d" % h] = round(sum(t) / len(t) * 100, 2) if t else None
            s["winRate%d" % h] = round(sum(1 for x in r if x > 0) / len(r) * 100, 1) if r else None
            s["avgMDD%d" % h] = round(sum(d) / len(d) * 100, 2) if d else None
        s["lowSample"] = len(rows) < cfg["event_min_n"]
        out[z] = s
    return out, recent[-5:]


def is_provisional(interval, bar_date, now_local, close_hhmm):
    """最後一根是否屬於尚未結束的日 / 週 / 月(當地時間)"""
    d = dt.date.fromisoformat(bar_date)
    after_close = now_local.hour * 100 + now_local.minute >= close_hhmm
    today = now_local.date()
    if interval == "1d":
        return d == today and not after_close
    if interval == "1wk":           # Yahoo 週線時間戳 = 該週第一天
        friday = d + dt.timedelta(days=4 - d.weekday()) if d.weekday() <= 4 else d
        return today < friday or (today == friday and not after_close)
    if interval == "1mo":
        return (d.year, d.month) == (today.year, today.month)
    return False


def dedupe_period(rows, interval):
    """Yahoo 週 / 月線有時在同一週(月)多給一根進行中的 K 棒(時間戳是當天)→ 合併:日期取該期第一根、收盤取最後一根"""
    if interval == "1d":
        return rows
    kf = week_key if interval == "1wk" else month_key
    out = []
    for d, c in rows:
        if out and kf(out[-1][0]) == kf(d):
            out[-1] = (out[-1][0], c)
        else:
            out.append((d, c))
    return out


def week_key(d):
    y, w, _ = dt.date.fromisoformat(d).isocalendar()
    return (y, w)


def month_key(d):
    return d[:7]


KEY_FN = {"1d": lambda d: d, "1wk": week_key, "1mo": month_key}


def analyze(series, interval, tw_series, now_local, close_hhmm, cfg=MOOD_INTL_CFG):
    """series:[(yyyy-mm-dd, close)] 由舊到新 → 該週期的現值、分位、狀態、事件統計"""
    dates = [d for d, _ in series]
    closes = [c for _, c in series]
    if len(closes) < cfg["warmup"] + 2:
        return {"ok": False, "error": "K 棒不足 %d 根" % len(closes)}
    dif, dea, hist = macd(closes, cfg)
    difpct = [a / c * 100 for a, c in zip(dif, closes)]
    zones = rolling_zones(difpct, interval, cfg)
    p, z = zones[-1]
    st = state_of(dif[-1], hist[-1], hist[-2])
    tw_by = {KEY_FN[interval](d): c for d, c in (tw_series or [])}
    stats, recent = event_stats(dates, closes, zones, tw_by, KEY_FN[interval], interval, cfg)
    n_hist = max(0, len(closes) - 1 - cfg["warmup"])
    out = {
        "ok": True, "date": dates[-1], "close": round(closes[-1], 2),
        "dif": round(dif[-1], 3), "dea": round(dea[-1], 3), "hist": round(hist[-1], 3),
        "difPct": round(difpct[-1], 3), "histPct": round(hist[-1] / closes[-1] * 100, 3),
        "pctl": p, "n_hist": n_hist, "zone": z, "state": st,
        "prevState": state_of(dif[-2], hist[-2], hist[-3]) if len(hist) >= 3 else None,
        "provisional": is_provisional(interval, dates[-1], now_local, close_hhmm),
        "extremeStats": stats, "recentExtremes": recent,
    }
    if interval == "1mo":
        out["lowSample"] = n_hist < cfg["low_sample_month"]
    if p is not None:
        out["near"] = ("hot" if p >= cc.CONSENSUS_CFG["zone_hi"] - cfg["near_band"] and z is None else
                       "cold" if p <= cc.CONSENSUS_CFG["zone_lo"] + cfg["near_band"] and z is None else None)
    if interval == "1wk":
        out["spark"] = [round(x, 3) for x in difpct[-cfg["spark_weeks"]:]]
        out["states2"] = [state_of(dif[i], hist[i], hist[i - 1]) for i in (len(hist) - 2, len(hist) - 1)]
    return out


def votes(macd_block, today, cfg=MOOD_INTL_CFG):
    """Phase 5:台股 MACD 1 票(^TWII 週線極端);國際 MACD 1 票(4 指數中 ≥ 3 個週線同邊極端)"""
    def wk_zone(sym):
        w = ((macd_block.get(sym) or {}).get("intervals") or {}).get("1wk") or {}
        if not w.get("ok") or not w.get("date"):
            return None, "暫缺"
        age = (today - dt.date.fromisoformat(w["date"])).days
        if age > cfg["vote_max_age_days"]:
            return None, "過期"
        return w.get("zone"), None
    tw, tw_note = wk_zone("^TWII")
    zs = [wk_zone(s)[0] for s in INTL]
    hot, cold = zs.count("hot"), zs.count("cold")
    intl = "hot" if hot >= cfg["intl_min_same"] else "cold" if cold >= cfg["intl_min_same"] else None
    return {"twMacd": tw, "twMacd_note": tw_note, "intlMacd": intl,
            "intlMacd_detail": {s: z for s, z in zip(INTL, zs)}, "intl_hot": hot, "intl_cold": cold}


# ------------------------------------------------------------------ 抓取
def fetch_chart(session, sym, interval, sleep=time.sleep):
    """→ ([(yyyy-mm-dd, close)], gmtoffset 秒);失敗回 (None, 錯誤字串)"""
    path = "/v8/finance/chart/%s?interval=%s&range=10y&includePrePost=false" % (urllib.parse.quote(sym), interval)
    err = None
    for h in HOSTS:
        try:
            r = session.get(h + path, headers=UA, timeout=30)
            if r.status_code != 200:
                err = "HTTP %s" % r.status_code
                sleep(1)
                continue
            res = (r.json().get("chart") or {}).get("result") or []
            if not res:
                err = "空結果"
                continue
            res = res[0]
            off = (res.get("meta") or {}).get("gmtoffset") or 0
            ts = res.get("timestamp") or []
            cl = ((res.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
            rows = {}
            for t, c in zip(ts, cl):
                if c is None:
                    continue
                d = dt.datetime.fromtimestamp(t + off, dt.timezone.utc).date().isoformat()
                rows[d] = float(c)            # 同一天多筆(盤中更新)取最後一筆
            return dedupe_period(sorted(rows.items()), interval), off
        except Exception as e:                # noqa: BLE001 — 網路 / 解析錯誤一律記下、換主機
            err = type(e).__name__
            sleep(1)
    return None, err


def build(session=None, prev=None, now_utc=None, sleep=time.sleep):
    """→ {"version", "symbols": {sym: {...}}, "votes": {...}};prev = 上一份 consensus.json 的 mood.macd(抓取失敗時沿用)"""
    import requests
    s = session or requests.Session()
    now_utc = now_utc or dt.datetime.now(dt.timezone.utc)
    raw, err = {}, {}
    for sym, *_ in SYMBOLS:
        for iv in INTERVALS:
            rows, info = fetch_chart(s, sym, iv, sleep)
            if rows is None:
                err[(sym, iv)] = info
            else:
                raw[(sym, iv)] = (rows, info)
            sleep(0.5)
    out = {"version": VERSION, "generated": now_utc.isoformat(timespec="seconds"), "symbols": {}}
    prev_syms = (prev or {}).get("symbols") or {}
    for sym, name, mkt, close_hhmm in SYMBOLS:
        ent = {"name": name, "market": mkt, "intervals": {}}
        for iv in INTERVALS:
            if (sym, iv) in raw:
                rows, off = raw[(sym, iv)]
                now_local = (now_utc + dt.timedelta(seconds=off)).replace(tzinfo=None)
                tw = raw.get(("^TWII", iv), ([], 0))[0]
                ent["intervals"][iv] = analyze(rows, iv, tw, now_local, close_hhmm)
            else:
                old = ((prev_syms.get(sym) or {}).get("intervals") or {}).get(iv)
                if old and old.get("ok"):
                    old = dict(old)
                    old["fetch_failed"] = err[(sym, iv)]
                    ent["intervals"][iv] = old
                else:
                    ent["intervals"][iv] = {"ok": False, "error": "抓取失敗:%s" % err[(sym, iv)]}
        out["symbols"][sym] = ent
    today = (now_utc + dt.timedelta(hours=8)).date()
    out["votes"] = votes(out["symbols"], today)
    out["fetch_errors"] = {"%s %s" % k: v for k, v in err.items()}
    return out


if __name__ == "__main__":
    import sys
    o = build()
    for sym, e in o["symbols"].items():
        w = e["intervals"].get("1wk") or {}
        print(sym, e["name"], {iv: (x.get("date"), x.get("difPct"), x.get("pctl"), x.get("zone"), x.get("state"),
                                    x.get("provisional")) for iv, x in e["intervals"].items()})
    print(json.dumps(o["votes"], ensure_ascii=False), o["fetch_errors"], len(json.dumps(o, ensure_ascii=False)))
    sys.exit(0)
