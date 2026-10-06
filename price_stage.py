# -*- coding: utf-8 -*-
"""
============================================================
price_stage.py — 價量階段標籤(潛伏 / 啟動 / 過熱)
============================================================
2026-10-06 新增。起因:大甲(2221)案例
  4/21 題材出現 → 9/3 漲停 → 9/22 雷達首見(轉債注意交易)→ 10/1 共振。
  雷達看到時股價已噴一大段,卻沒有任何機制告訴你「這檔已經漲多少」。

做三件事:
  1. main():每天抓全市場收盤價+成交量,存成 price_history/YYYYMMDD.json
     (沿用 verification_ledger 已驗證可用的 TWSE STOCK_DAY_ALL +
      TPEx tpex_mainboard_daily_close_quotes)。
  2. main():對雷達裡出現的股票,若歷史不足,best-effort 補抓個股月成交資料
     (TWSE STOCK_DAY / TPEx st43)。⚠ 這兩個端點無法在開發環境實測,
     失敗只會印警告,之後靠每天累積快照自然補齊(約20個交易日)。
  3. stage_for_codes(codes):供 daily_digest.py 呼叫,純讀本地檔,不碰網路。

階段定義(門檻是初始值,【尚未校準】,調整請改下面常數):
  過熱:雷達有「注意交易/處置」旗標,或近20日漲幅>=30%,或近10日漲停>=2次
  啟動:近20日漲幅>=10%,或(近5日漲幅>=5% 且 量比>=2)
  潛伏:以上皆非,且有足夠歷史可判斷
  未知:歷史不足(<6個交易日),只有單日漲跌可看

對應 daily_report.py 既有的 price_pos 欄位:
  潛伏→low(🟢底部)  啟動→mid(🟡中段)  過熱→high(🔴高檔)
============================================================
"""

import os
import re
import json
import time
import glob
import datetime as dt
import requests
import urllib3

urllib3.disable_warnings()

UA = {"User-Agent": "Mozilla/5.0 (quietfolio-radar)"}
HIST_DIR = "price_history"
BACKFILL_FILE = os.path.join(HIST_DIR, "backfill.json")   # {code: {YYYYMMDD: [close, vol]}}
KEEP_DAYS = 45
MAX_BACKFILL_CODES = 130

# ── 門檻(初始值,未校準)──
OVERHEAT_CHG20 = 30.0
ACTIVE_CHG20 = 10.0
ACTIVE_CHG5 = 5.0
ACTIVE_VOL_RATIO = 2.0
LIMIT_UP_PCT = 9.5
OVERHEAT_LIMIT_UP_DAYS = 2
MIN_HISTORY_DAYS = 6

STAGE_TO_POS = {"潛伏": "low", "啟動": "mid", "過熱": "high"}


# ============================================================
# 抓取
# ============================================================
def _get_json(url, verify=True, timeout=25, tries=4):
    """帶重試的JSON抓取。
    2026-10-06 實跑發現:TPEx openapi 會間歇性回 "Response ended prematurely"
    (IncompleteRead,verification_ledger同一天也中招),導致當天快照缺上櫃股。
    重試時改用 Accept-Encoding: identity(避開壓縮串流被截斷),每次間隔拉長。"""
    last = None
    for i in range(tries):
        try:
            h = dict(UA)
            if i >= 1:
                h["Accept-Encoding"] = "identity"
            r = requests.get(url, headers=h, timeout=timeout, verify=verify)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
            if i < tries - 1:
                time.sleep(3 * (i + 1))
    raise last


def _num(x):
    try:
        return float(str(x).replace(",", "").replace("+", "").strip())
    except (ValueError, TypeError):
        return None


def _parse_row_date(v):
    """'1151001'(民國7碼)或'20261001' → 'YYYYMMDD';失敗回 None。"""
    v = re.sub(r"\D", "", str(v or ""))
    if len(v) == 7:
        return f"{int(v[:3]) + 1911}{v[3:]}"
    if len(v) == 8:
        return v
    return None


def fetch_market_snapshot():
    """回傳 (date_str or None, {code: [close, volume]}, {code: 'TWSE'|'TPEx'})"""
    snap, market, dates = {}, {}, []

    try:
        for row in _get_json("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"):
            code = str(row.get("Code", "")).strip()
            close = _num(row.get("ClosingPrice"))
            vol = _num(row.get("TradeVolume"))
            if re.match(r"^\d{4,6}$", code) and close and close > 0:
                snap[code] = [close, vol or 0]
                market[code] = "TWSE"
                d = _parse_row_date(row.get("Date"))
                if d:
                    dates.append(d)
        print(f"  TWSE 快照: {len(snap)} 檔")
    except Exception as e:
        print(f"  ⚠ TWSE 快照失敗: {e}")

    try:
        before = len(snap)
        for row in _get_json("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes",
                             verify=False):
            code = str(row.get("SecuritiesCompanyCode", "")).strip()
            close = _num(row.get("Close"))
            vol = _num(row.get("TradingShares") or row.get("TradingVolume") or row.get("TradeVolume"))
            if re.match(r"^\d{4,6}$", code) and close and close > 0:
                snap[code] = [close, vol or 0]
                market[code] = "TPEx"
                d = _parse_row_date(row.get("Date"))
                if d:
                    dates.append(d)
        print(f"  TPEx 快照: +{len(snap) - before} 檔")
    except Exception as e:
        print(f"  ⚠ TPEx 快照失敗: {e}")

    date = max(dates) if dates else None
    return date, snap, market


def load_history():
    """回傳 [(date, {code:[close,vol]})] 依日期排序。"""
    out = []
    for f in sorted(glob.glob(os.path.join(HIST_DIR, "2*.json"))):
        try:
            d = os.path.basename(f)[:8]
            with open(f, encoding="utf-8") as fh:
                out.append((d, json.load(fh)))
        except Exception:
            pass
    return out


def save_snapshot(date, snap):
    """存今日快照。若與最新一份幾乎完全相同(假日重複抓到上個交易日),不存。"""
    os.makedirs(HIST_DIR, exist_ok=True)
    hist = load_history()
    if hist:
        last_d, last = hist[-1]
        same = sum(1 for c, v in snap.items() if c in last and last[c][0] == v[0] and last[c][1] == v[1])
        if snap and same / len(snap) >= 0.9:
            print(f"  快照與 {last_d} 相同(休市日),不重複存")
            return None
        if date is None:
            date = dt.date.today().strftime("%Y%m%d")
    elif date is None:
        date = dt.date.today().strftime("%Y%m%d")

    path = os.path.join(HIST_DIR, f"{date}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, separators=(",", ":"))
    print(f"  已存 {path}({len(snap)} 檔)")

    # 只留最近 KEEP_DAYS 份
    files = sorted(glob.glob(os.path.join(HIST_DIR, "2*.json")))
    for old in files[:-KEEP_DAYS]:
        os.remove(old)
    return date


# ── 個股月資料補抓(best-effort,未實測)──
def _backfill_twse(code, ym):
    url = (f"https://www.twse.com.tw/exchangeReport/STOCK_DAY?response=json"
           f"&date={ym}01&stockNo={code}")
    j = _get_json(url, timeout=20, tries=2)
    out = {}
    for row in j.get("data", []):
        d = _parse_row_date(row[0].replace("/", ""))
        close, vol = _num(row[6]), _num(row[1])
        if d and close:
            out[d] = [close, vol or 0]
    return out


def _backfill_tpex(code, ym):
    roc = f"{int(ym[:4]) - 1911}/{ym[4:6]}"
    url = ("https://www.tpex.org.tw/web/stock/aftertrading/daily_trading_info/"
           f"st43_result.php?l=zh-tw&d={roc}&stkno={code}")
    j = _get_json(url, verify=False, timeout=20, tries=2)
    out = {}
    _backfill_tpex.last_keys = list(j.keys()) if isinstance(j, dict) else type(j).__name__
    rows = j.get("aaData")
    if not rows and isinstance(j.get("tables"), list) and j["tables"]:
        rows = j["tables"][0].get("data")
    for row in rows or []:
        d = _parse_row_date(str(row[0]).replace("/", ""))
        close, vol = _num(row[6]), _num(row[1])
        if d and close:
            out[d] = [close, (vol or 0) * 1000]   # 仟股→股
    return out


def backfill(codes, market):
    try:
        with open(BACKFILL_FILE, encoding="utf-8") as f:
            store = json.load(f)
    except Exception:
        store = {}
    today = dt.date.today()
    months = [today.strftime("%Y%m"),
              (today.replace(day=1) - dt.timedelta(days=1)).strftime("%Y%m")]
    ok = fail = 0
    empty = {"TWSE": 0, "TPEx": 0}
    shown = 0
    for code in list(codes)[:MAX_BACKFILL_CODES]:
        if len(store.get(code, {})) >= 21:
            continue
        got = {}
        for ym in months:
            try:
                fn = _backfill_tpex if market.get(code) == "TPEx" else _backfill_twse
                got.update(fn(code, ym))
            except Exception as e:
                fail += 1
                print(f"    ⚠ 補抓 {code} {ym} 失敗: {str(e)[:60]}")
            time.sleep(1.0)
        if got:
            store.setdefault(code, {}).update(got)
            ok += 1
        else:
            mk = market.get(code, "TWSE")
            empty[mk] = empty.get(mk, 0) + 1
            if shown < 3:   # 診斷:前3檔抓不到資料的,印出市場與TPEx回傳結構
                shown += 1
                print(f"    · {code}({mk}) 補抓無資料"
                      + (f" TPEx回傳結構={getattr(_backfill_tpex, 'last_keys', None)}" if mk == "TPEx" else ""))
    os.makedirs(HIST_DIR, exist_ok=True)
    with open(BACKFILL_FILE, "w", encoding="utf-8") as f:
        json.dump(store, f, ensure_ascii=False, separators=(",", ":"))
    print(f"  個股補抓: 成功 {ok} 檔 / 失敗請求 {fail} 次 / 無資料 {empty}")


# ============================================================
# 計算
# ============================================================
def _series_for(code, hist, store):
    """合併全市場快照與個股補抓,回傳 [(date, close, vol)] 依日期排序。"""
    merged = {}
    for d, snap in hist:
        if code in snap:
            merged[d] = snap[code]
    for d, v in store.get(code, {}).items():
        merged.setdefault(d, v)
    return [(d, v[0], v[1]) for d, v in sorted(merged.items())]


def _classify(series, overheat_flag):
    n = len(series)
    closes = [s[1] for s in series]
    vols = [s[2] for s in series]
    res = {"history_days": n, "stage": "未知", "price_pos": None,
           "chg1": None, "chg5": None, "chg20": None, "vol_ratio": None,
           "limit_up_10d": 0, "overheat_notice": bool(overheat_flag)}
    if n >= 2 and closes[-2] > 0:
        res["chg1"] = round((closes[-1] / closes[-2] - 1) * 100, 1)
    if n >= 6:
        res["chg5"] = round((closes[-1] / closes[-6] - 1) * 100, 1)
    if n >= 21:
        res["chg20"] = round((closes[-1] / closes[-21] - 1) * 100, 1)
    elif n >= 11:   # 歷史不足20日:用現有最長區間近似,標註 chg20_days
        res["chg20"] = round((closes[-1] / closes[0] - 1) * 100, 1)
        res["chg20_days"] = n - 1
    if n >= 6:
        base = [v for v in vols[max(0, n - 21):-1] if v]
        if base and vols[-1]:
            res["vol_ratio"] = round(vols[-1] / (sum(base) / len(base)), 1)
    lu = 0
    for i in range(max(1, n - 10), n):
        if closes[i - 1] > 0 and (closes[i] / closes[i - 1] - 1) * 100 >= LIMIT_UP_PCT:
            lu += 1
    res["limit_up_10d"] = lu

    chg20, chg5, vr = res["chg20"], res["chg5"], res["vol_ratio"]
    if overheat_flag or (chg20 is not None and chg20 >= OVERHEAT_CHG20) or lu >= OVERHEAT_LIMIT_UP_DAYS:
        stage = "過熱"
    elif n < MIN_HISTORY_DAYS:
        stage = "未知"
    elif (chg20 is not None and chg20 >= ACTIVE_CHG20) or \
         (chg5 is not None and chg5 >= ACTIVE_CHG5 and vr is not None and vr >= ACTIVE_VOL_RATIO):
        stage = "啟動"
    else:
        stage = "潛伏"
    res["stage"] = stage
    res["price_pos"] = STAGE_TO_POS.get(stage)
    return res


def stage_for_codes(codes, overheat=None):
    """供 daily_digest.py 呼叫。純讀本地檔。回傳 {code: {...}}(只含有價格資料的股票)。"""
    overheat = overheat or {}
    hist = load_history()
    try:
        with open(BACKFILL_FILE, encoding="utf-8") as f:
            store = json.load(f)
    except Exception:
        store = {}
    out = {}
    for code in set(codes):
        series = _series_for(code, hist, store)
        if not series:
            continue
        out[code] = _classify(series, overheat.get(code))
    return out


def tag(info):
    """顯示用短標籤,例:'🔴過熱+35.2%' / '🟢潛伏' / '⚪未知'。"""
    if not info:
        return ""
    icon = {"潛伏": "🟢", "啟動": "🟡", "過熱": "🔴"}.get(info["stage"], "⚪")
    s = f"{icon}{info['stage']}"
    if info.get("chg20") is not None:
        s += f"{info['chg20']:+.0f}%"
    elif info.get("chg1") is not None:
        s += f"(日{info['chg1']:+.1f}%)"
    if info.get("overheat_notice"):
        s += "⚠注意交易"
    return s


# ============================================================
# 主流程
# ============================================================
def _collect_targets():
    """雷達輸出裡出現的股票代號(補抓與除錯輸出用)。"""
    codes = set()
    try:
        with open("event_theme_raw.json", encoding="utf-8") as f:
            ev = json.load(f)
        for s in ev.get("stocks", []):
            codes.add(s.get("code"))
        for t in ev.get("themes", []):
            for c in t.get("codes", []):
                codes.add(c)
        overheat = ev.get("overheat", {})
    except Exception:
        overheat = {}
    try:
        with open("new_theme_candidates.json", encoding="utf-8") as f:
            nt = json.load(f)
        for c in nt.get("candidates", []):
            for cd in c.get("related_stocks", c.get("stocks", [])):
                codes.add(cd)
    except Exception:
        pass
    codes.discard(None)
    return {c for c in codes if re.match(r"^\d{4,6}$", str(c))}, overheat


def main():
    print("=" * 50)
    print("price_stage 開始(價量階段標籤)")
    print("=" * 50)
    date, snap, market = fetch_market_snapshot()
    if not snap:
        print("  ⚠ 兩邊快照都失敗,略過(不影響其他流程)")
        return
    save_snapshot(date, snap)

    targets, overheat = _collect_targets()
    hist = load_history()
    try:
        with open(BACKFILL_FILE, encoding="utf-8") as f:
            store = json.load(f)
    except Exception:
        store = {}
    need = [c for c in sorted(targets) if len(_series_for(c, hist, store)) < 21]
    print(f"  雷達股票 {len(targets)} 檔,歷史不足21日 {len(need)} 檔")
    if need:
        backfill(need, market)

    stages = stage_for_codes(targets, overheat)
    with open("price_stage.json", "w", encoding="utf-8") as f:
        json.dump({"generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                   "stages": stages}, f, ensure_ascii=False, indent=1)
    cnt = {}
    for v in stages.values():
        cnt[v["stage"]] = cnt.get(v["stage"], 0) + 1
    print(f"  階段分布: {cnt}")
    for c in sorted(stages, key=lambda x: -(stages[x].get("chg20") or -999))[:15]:
        print(f"    {c} {tag(stages[c])}  歷史{stages[c]['history_days']}日 "
              f"量比{stages[c].get('vol_ratio')}")


if __name__ == "__main__":
    main()
