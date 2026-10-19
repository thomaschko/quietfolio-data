# -*- coding: utf-8 -*-
"""
============================================================
group_flow.py — 台/美/日/韓 供應鏈族群金流共識(2026-10-19 新增,Phase 1.5)
============================================================
題材籃子:intl_theme_baskets.json(人工維護;2026-10-19 審核 10 個,同日晚擴充為 28 個產業族群)
資料:籃子成分股的 yfinance 日線(台股 .TW/.TWO、韓股 .KS/.KQ 兩種後綴都試,取有資料的那個),
      auto_adjust=False;全市場總成交值(占比的分母):美日韓取 intl_rank.py 寫的
      intl_market_totals.json,台股取 price_history/YYYYMMDD.json(price_stage.py)
輸出:group_flow.json → GAS IntlRankFetch.js 一併讀 → 排行榜頁「族群金流」

每個題材 × 市場(各市場用自己最後一個已收盤交易日,收盤判定同 intl_rank_core.drop_incomplete):
  value_vs_avg  籃子成交值(成員加總)對比前 20 個交易日均值的增減 %
  ret_1d/ret_5d 成員等權平均漲跌 %
  up / n        今日上漲成員數 / 有資料成員數
  share_vs_avg  籃子占全市場成交值比例,對比前 20 日平均占比的增減 %(分母資料不足 → None)
  value_5d_vs_avg  近 5 日籃子成交值均值,對比再之前 20 日均值的增減 %(比單日穩定)
  weight / rot_vs_avg / rot_5d(2026-10-19 晚新增,板塊移動)
                籃子占「該市場所有追蹤族群成分股(不重複)合計成交值」的比重 %;
                rot_vs_avg = 今日比重對比前 20 日平均比重的增減 %,
                rot_5d = 近 5 日平均比重對比再之前 20 日平均比重的增減 %。
                分母只用籃子成分股,四個市場口徑一致(台股沒有當日全市場總額也能算),
                代表的是「資金在追蹤的族群之間移動」,不是對全市場。
族群狀態(初版門檻,累積 2~3 週資料後再校準):
  熱(↑/↓)= value_vs_avg ≥ +30% 且平均漲跌同向(>0 為 ↑、<0 為 ↓)
  共識 = 有資料的市場中至少 3 個(只有 2 個市場的題材則 2/2)同向「熱」
  海外先動 = 美日韓至少 2 個市場「熱↑」,但台股 value_vs_avg < +10%
  國際平均 = 有資料市場的等權平均(flow_avg / flow5_avg / rot5_avg)
  板塊移動共識 = 有資料的市場中至少 3 個(只有 2 個市場則 2/2)rot_5d ≥ +10%(移入)或 ≤ −10%(移出)
時差:美股收盤在亞洲之後,所以各市場一律用「自己最新已收盤日」,美股自然是前一個美股交易日。
只是閱讀用的彙總:不進雷達、不影響排序、A 區或任何門檻;籃子人工挑選,有選股偏差。
============================================================
"""
import os
import re
import sys
import glob
import json
import datetime as dt
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

import intl_rank_core as core

BASKET_FILE = "intl_theme_baskets.json"
TOTALS_FILE = "intl_market_totals.json"
TW_HIST_DIR = "price_history"
OUT_FILE = "group_flow.json"
PERIOD = "60d"
WINDOW = 20
MIN_PRIOR = 10
MIN_VALID_DAYS = 15
HOT_VS_AVG = 30
TW_QUIET_VS_AVG = 10
ROT_TH = 10
RECENT = 5
MARKET_ORDER = ("tw", "us", "jp", "kr")
CFG = {
    "tw": {"label": "台股", "tz": "Asia/Taipei", "close": dt.time(14, 40), "suffixes": (".TW", ".TWO")},
    "us": {"label": "美股", "tz": "America/New_York", "close": dt.time(16, 20), "suffixes": ("",)},
    "jp": {"label": "日股", "tz": "Asia/Tokyo", "close": dt.time(15, 40), "suffixes": (".T",)},
    # 韓股 18:10:盤後單一價交易(16:00~18:00)的量也算進當日日線,同 intl_rank.py
    "kr": {"label": "韓股", "tz": "Asia/Seoul", "close": dt.time(18, 10), "suffixes": (".KS", ".KQ")},
}


def log(msg):
    print(msg, flush=True)


def tickers_for(market, code):
    code = str(code).strip()
    if market == "us":
        return [code.replace(".", "-")]
    return [code + s for s in CFG[market]["suffixes"]]


# ============================================================
# 純計算
# ============================================================
def _pct(a, b):
    return None if (a is None or b is None or not np.isfinite(a) or not np.isfinite(b) or b <= 0) \
        else round(float(a / b - 1) * 100, 1)


def _valid_cols(close, volume, codes):
    return [c for c in codes if c in close.columns and int((close[c].notna() & volume[c].notna()).sum()) >= MIN_VALID_DAYS]


def _values(close, volume, cols):
    c, v = close[cols], volume[cols]
    return (c * v).where(v.fillna(0) > 0).fillna(0.0) / 1e8          # 億,當地幣別


def universe_value(close, volume):
    """該市場所有籃子成分股(不重複、有足夠資料)每日合計成交值 → 板塊比重的分母。"""
    if close is None or len(close) < 2:
        return None
    cols = _valid_cols(close, volume, list(close.columns))
    return _values(close, volume, cols).sum(axis=1) if cols else None


def recent_vs_prior(ser):
    """近 RECENT 日均值 vs 再之前 WINDOW 日均值(只取 >0 的日子)的增減 %。"""
    recent = ser.iloc[-RECENT:]
    prior = ser.iloc[-(WINDOW + RECENT):-RECENT]
    prior = prior[prior > 0]
    if len(recent) < RECENT or len(prior) < MIN_PRIOR:
        return None
    return _pct(float(recent.mean()), float(prior.mean()))


def basket_stats(close, volume, codes, totals=None, universe=None):
    """close/volume:index=日期、columns=代碼(已去掉後綴),已丟掉未收盤日。
    totals:{date字串: 全市場總成交值(億)} 或 None;universe:universe_value() 的結果或 None。
    回傳 dict 或 None(沒有可用成員)。"""
    if close is None or len(close) < 2:
        return None
    cols = _valid_cols(close, volume, codes)
    if not cols:
        return None
    c = close[cols]
    val = _values(close, volume, cols)
    bval = val.sum(axis=1)
    today = bval.iloc[-1]
    prior = bval.iloc[-(WINDOW + 1):-1]
    prior = prior[prior > 0]
    vs = _pct(today, prior.mean()) if len(prior) >= MIN_PRIOR else None
    rets, rets5, up, members = [], [], 0, []
    for k in cols:
        s = c[k].dropna()
        r1 = _pct(float(s.iloc[-1]), float(s.iloc[-2])) if len(s) >= 2 and s.index[-1] == c.index[-1] else None
        r5 = _pct(float(s.iloc[-1]), float(s.iloc[-6])) if len(s) >= 6 and s.index[-1] == c.index[-1] else None
        if r1 is not None:
            rets.append(r1)
            up += r1 > 0
        if r5 is not None:
            rets5.append(r5)
        mv = val[k]
        mprior = mv.iloc[-(WINDOW + 1):-1]
        mprior = mprior[mprior > 0]
        members.append({"code": k, "chg": r1, "value": round(float(mv.iloc[-1]), 1),
                        "vsAvg": _pct(float(mv.iloc[-1]), mprior.mean()) if len(mprior) >= MIN_PRIOR else None})
    share = share_vs = None
    if totals:
        ser = pd.Series({pd.Timestamp(d): t for d, t in totals.items() if t and t > 0})
        ser = ser.reindex(bval.index)
        sh = (bval / ser).dropna()
        if len(sh) and sh.index[-1] == bval.index[-1]:
            share = round(float(sh.iloc[-1]) * 100, 2)
            sp = sh.iloc[:-1].tail(WINDOW)
            share_vs = _pct(float(sh.iloc[-1]), float(sp.mean())) if len(sp) >= 5 else None
    weight = rot = rot5 = None
    if universe is not None:
        u = universe.reindex(bval.index)
        w = (bval / u.where(u > 0)).dropna()
        if len(w) and w.index[-1] == bval.index[-1]:
            weight = round(float(w.iloc[-1]) * 100, 2)
            wp = w.iloc[-(WINDOW + 1):-1]
            rot = _pct(float(w.iloc[-1]), float(wp.mean())) if len(wp) >= MIN_PRIOR else None
            rot5 = recent_vs_prior(w)
    members.sort(key=lambda m: -m["value"])
    return {"asof": str(c.index[-1].date()), "n": len(cols), "value": round(float(today), 1), "value_vs_avg": vs,
            "value_5d_vs_avg": recent_vs_prior(bval), "weight": weight, "rot_vs_avg": rot, "rot_5d": rot5,
            "ret_1d": round(float(np.mean(rets)), 2) if rets else None,
            "ret_5d": round(float(np.mean(rets5)), 2) if rets5 else None,
            "up": int(up), "share": share, "share_vs_avg": share_vs, "members": members}


def heat(st):
    """→ 'up' / 'down' / None。"""
    if not st or st.get("value_vs_avg") is None or st.get("ret_1d") is None:
        return None
    if st["value_vs_avg"] >= HOT_VS_AVG and st["ret_1d"] != 0:
        return "up" if st["ret_1d"] > 0 else "down"
    return None


def theme_status(markets):
    """markets:{tw/us/jp/kr: stats 或 None}。回傳 {status, hot, direction, lead}。"""
    have = [m for m in MARKET_ORDER if markets.get(m)]
    hot = {m: heat(markets[m]) for m in have}
    ups = [m for m in have if hot[m] == "up"]
    downs = [m for m in have if hot[m] == "down"]
    need = 2 if len(have) == 2 else 3
    if len(have) >= 2 and len(ups) >= need:
        status, direction = "consensus", "up"
    elif len(have) >= 2 and len(downs) >= need:
        status, direction = "consensus", "down"
    elif ups or downs:
        status, direction = "partial", ("up" if len(ups) >= len(downs) else "down")
    else:
        status, direction = "quiet", None
    intl_up = [m for m in ("us", "jp", "kr") if hot.get(m) == "up"]
    tw = markets.get("tw")
    lead = bool(len(intl_up) >= 2 and tw and tw.get("value_vs_avg") is not None and tw["value_vs_avg"] < TW_QUIET_VS_AVG)
    r5 = {m: markets[m].get("rot_5d") for m in have if markets[m].get("rot_5d") is not None}
    rin = [m for m in MARKET_ORDER if r5.get(m) is not None and r5[m] >= ROT_TH]
    rout = [m for m in MARKET_ORDER if r5.get(m) is not None and r5[m] <= -ROT_TH]
    rneed = 2 if len(r5) == 2 else 3
    if len(r5) >= 2 and len(rin) >= rneed:
        rot_status = "inflow"
    elif len(r5) >= 2 and len(rout) >= rneed:
        rot_status = "outflow"
    elif rin and rout:
        rot_status = "mixed"
    elif rin or rout:
        rot_status = "partial_in" if len(rin) >= len(rout) else "partial_out"
    else:
        rot_status = "flat"

    def avg(key):
        xs = [markets[m][key] for m in have if markets[m].get(key) is not None]
        return round(float(np.mean(xs)), 1) if xs else None
    return {"status": status, "direction": direction, "hot_up": ups, "hot_down": downs,
            "markets_with_data": len(have), "need": need, "overseas_lead": lead,
            "flow_avg": avg("value_vs_avg"), "flow5_avg": avg("value_5d_vs_avg"), "rot5_avg": avg("rot_5d"),
            "rot_status": rot_status, "rot_in": rin, "rot_out": rout}


# ============================================================
# I/O
# ============================================================
def load_baskets(path=BASKET_FILE):
    with open(path, encoding="utf-8") as f:
        return json.load(f)["themes"]


def tw_totals(hist_dir=TW_HIST_DIR):
    """price_history/YYYYMMDD.json({code:[close, volume]})→ {YYYY-MM-DD: 全市場總成交值(億元)}。"""
    out = {}
    for p in sorted(glob.glob(os.path.join(hist_dir, "2*.json")))[-60:]:
        m = re.search(r"(\d{8})\.json$", p)
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
            tot = sum(float(v[0]) * float(v[1]) for v in d.values()
                      if isinstance(v, list) and len(v) >= 2 and v[0] and v[1])
            if tot > 0:
                s = m.group(1)
                out[f"{s[:4]}-{s[4:6]}-{s[6:]}"] = round(tot / 1e8, 1)
        except Exception:
            continue
    return out


def _yf_download(tickers):
    import yfinance as yf
    return yf.download(tickers, period=PERIOD, interval="1d", auto_adjust=False, group_by="ticker",
                       threads=True, progress=False)


def fetch_market(market, codes, download=_yf_download, now_utc=None):
    """→ (close, volume)(columns=原始代碼)與 log 資訊。兩種後綴都試,取有效日數多的那個。"""
    want = {t: c for c in codes for t in tickers_for(market, c)}
    df = download(sorted(want))
    multi = isinstance(df.columns, pd.MultiIndex)
    best = {}
    for t, code in want.items():
        try:
            sub = df[t] if multi else df
            cl, vo = sub["Close"], sub["Volume"]
        except Exception:
            continue
        n = int((cl.notna() & vo.notna()).sum())
        if n >= MIN_VALID_DAYS and n > best.get(code, (0,))[0]:
            best[code] = (n, cl, vo)
    missing = [c for c in codes if c not in best]
    if not best:
        return None, None, missing
    close = pd.DataFrame({c: b[1] for c, b in best.items()}).sort_index()
    volume = pd.DataFrame({c: b[2] for c, b in best.items()}).reindex(close.index)
    idx = pd.to_datetime(close.index)
    close.index = volume.index = idx.tz_localize(None) if idx.tz is not None else idx
    cfg = CFG[market]
    now_local = (now_utc or dt.datetime.now(dt.timezone.utc)).astimezone(ZoneInfo(cfg["tz"])).replace(tzinfo=None)
    close, volume, _ = core.drop_incomplete(close, volume, now_local, cfg["close"])
    return close, volume, missing


def build(baskets, fetch=fetch_market, totals=None, tw_tot=None):
    totals = totals or {}
    by_market = {m: sorted({c for th in baskets.values() for c in th.get(m, [])}) for m in MARKET_ORDER}
    panels, missing = {}, {}
    for m, codes in by_market.items():
        if not codes:
            continue
        try:
            close, volume, miss = fetch(m, codes)
        except Exception as e:
            log(f"  ✗ {CFG[m]['label']} 下載失敗:{e}")
            close, volume, miss = None, None, codes
        panels[m] = (close, volume)
        if miss:
            missing[m] = miss
            log(f"  ⚠ {CFG[m]['label']} 取不到(不計入):{miss}")
    univ = {m: universe_value(*p) for m, p in panels.items()}
    themes = []
    for name, mk in baskets.items():
        stats = {}
        for m in MARKET_ORDER:
            codes = mk.get(m) or []
            close, volume = panels.get(m, (None, None))
            if not codes or close is None:
                continue
            tot = tw_tot if m == "tw" else totals.get(m)
            stats[m] = basket_stats(close, volume, [str(c) for c in codes], tot, univ.get(m))
        themes.append({"theme": name, "markets": {m: s for m, s in stats.items() if s}, **theme_status(stats)})
    order = {"consensus": 0, "partial": 1, "quiet": 2}
    themes.sort(key=lambda t: (order[t["status"]], -len(t["hot_up"]) - len(t["hot_down"])))
    asof = {m: next((t["markets"][m]["asof"] for t in themes if m in t["markets"]), None) for m in MARKET_ORDER}
    return {"generated": dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S"),
            "asof": asof, "rules": {"hot_vs_avg": HOT_VS_AVG, "tw_quiet_vs_avg": TW_QUIET_VS_AVG, "window": WINDOW,
                                    "rot_th": ROT_TH, "recent": RECENT},
            "missing": missing, "themes": themes}


def main():
    baskets = load_baskets()
    try:
        with open(TOTALS_FILE, encoding="utf-8") as f:
            totals = json.load(f)
    except Exception:
        totals = {}
    out = build(baskets, totals=totals, tw_tot=tw_totals())
    if not any(t["markets"] for t in out["themes"]):
        log("✗ 所有題材都沒有資料,不覆寫 group_flow.json")
        return 1
    text = json.dumps(out, ensure_ascii=False, separators=(",", ":"))
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        f.write(text)
    log(f"→ {OUT_FILE}:{len(text)} 字元、asof {out['asof']}")
    for t in out["themes"]:
        cells = "  ".join(f"{CFG[m]['label']} {s['value_vs_avg']}%/{s['ret_1d']}%"
                          + (f"(比重{s['weight']}% 5日移動{s['rot_5d']}%)" if s.get("weight") is not None else "")
                          for m, s in t["markets"].items())
        log(f"  [{t['status']}{'↑' if t['direction'] == 'up' else '↓' if t['direction'] == 'down' else ''}"
            f"{' 海外先動' if t['overseas_lead'] else ''} 板塊{t['rot_status']}] {t['theme']}:{cells}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
