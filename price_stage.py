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

「位置」與「階段」是兩個獨立欄位(2026-10-11 拆開):
  stage(潛伏/啟動/過熱):只反映近期漲幅與量能,不代表價格在區間的高低。
  price_pos(low/mid/high → daily_report 顯示🟢底部/🟡中段/🔴高檔):
    最近最多 40 個交易日(含今天)收盤價區間的位置
    range_pos = (今日收盤 - 區間最低) / (區間最高 - 區間最低),最高=最低時為 None;
    range_days >= 20 才給標籤:<=0.33 low、<=0.66 mid、其餘 high;不足 20 日為 None。
  舊版曾把 stage 直接映射成 price_pos(潛伏→底部),會把「近20日沒大漲」誤說成
  「位於底部」,已移除。
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
MARKET_FILE = os.path.join(HIST_DIR, "market.json")       # {code: "TWSE"|"TPEx"} 補抓時學到的上市/上櫃歸屬
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

# ── 價格位置(區間位置,與 stage 無關)──
RANGE_WINDOW = 40          # 取最近最多幾個交易日收盤價(含今天)
RANGE_MIN_DAYS = 20        # 區間至少要有幾日才給 price_pos 標籤
POS_LOW_MAX = 0.33
POS_MID_MAX = 0.66
POS_LABEL = {"low": "底部", "mid": "中段", "high": "高檔"}

# TPEx 個股補抓(st43_result.php)連續失敗幾次就放棄本輪
TPEX_DEAD_STREAK = 6
FAIL_LOG_LIMIT = 3


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


# 2026-10-30:上市改用證交所官網 MI_INDEX(GAS 排行榜/報價用的同一個來源)。
# 原本的 OpenAPI STOCK_DAY_ALL 在台北 17:30 左右常常還是前一個交易日 → price_history 的上市價格間歇性落後一天
# (10/13、10/19~21、10/27~29 都是),而上櫃是當天,兩天的價格存在同一個檔。
MI_INDEX_URLS = ["https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?response=json&type=ALL",
                 "https://www.twse.com.tw/exchangeReport/MI_INDEX?response=json&type=ALL"]


def parse_mi_index(j):
    """MI_INDEX JSON → (date 'YYYYMMDD' or None, {code: [close, volume(股)]})。個股表 = 欄位含「證券代號」「成交金額」的那張。"""
    rows = None
    for t in (j.get("tables") or []):
        f = ",".join(t.get("fields") or [])
        if "證券代號" in f and "成交金額" in f:
            rows = t.get("data")
    if rows is None:
        rows = j.get("data9")
    out = {}
    for row in rows or []:
        code = str(row[0]).strip()
        close = _num(row[8]) if len(row) > 8 else None
        vol = _num(row[2]) if len(row) > 2 else None
        # 只收股票與 ETF(4 碼,或 00 開頭的 5~6 碼);MI_INDEX 全部表含權證(03~08 開頭 6 碼),不收
        if (re.match(r"^\d{4}$", code) or re.match(r"^00\d{3,4}[A-Z]?$", code)) and close and close > 0:
            out[code] = [close, vol or 0]
    return _parse_row_date(j.get("date")), out


def fetch_mi_index(date=None):
    """上市 MI_INDEX(date='YYYYMMDD' 指定日期,None = 最新)→ (date, {code: [close, vol]});新版網址失敗退回舊版。"""
    last = None
    for base in MI_INDEX_URLS:
        url = base + (f"&date={date}" if date else "")
        try:
            r = requests.get(url, headers=UA, timeout=40)
            r.raise_for_status()
            d, rows = parse_mi_index(r.json())
            if rows:
                return d, rows
            last = "沒有個股表"
        except Exception as e:
            last = e
    raise RuntimeError(f"MI_INDEX 失敗: {last}")


def fetch_market_snapshot():
    """回傳 (date_str or None, {code: [close, volume]}, {code: 'TWSE'|'TPEx'})。
    2026-10-30:上市與上櫃的資料日期不同時,只保留日期較新的那個市場(不把兩天的價格存在同一個檔),並印出警告。"""
    snap, market = {}, {}
    by_mkt = {"TWSE": (None, {}), "TPEx": (None, {})}

    try:
        d, rows = fetch_mi_index()
        by_mkt["TWSE"] = (d, rows)
        print(f"  TWSE 快照: {len(rows)} 檔(MI_INDEX {d})")
    except Exception as e:
        print(f"  ⚠ TWSE MI_INDEX 失敗: {e} → 改用 OpenAPI STOCK_DAY_ALL(日期若比上櫃舊,下面會不存)")
        try:
            rows, dates = {}, []
            for row in _get_json("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"):
                code = str(row.get("Code", "")).strip()
                close = _num(row.get("ClosingPrice"))
                if re.match(r"^\d{4,6}$", code) and close and close > 0:
                    rows[code] = [close, _num(row.get("TradeVolume")) or 0]
                    d = _parse_row_date(row.get("Date"))
                    if d:
                        dates.append(d)
            by_mkt["TWSE"] = (max(dates) if dates else None, rows)
            print(f"  TWSE 快照(OpenAPI): {len(rows)} 檔({by_mkt['TWSE'][0]})")
        except Exception as e2:
            print(f"  ⚠ TWSE 快照失敗: {e2}")

    try:
        rows, dates = {}, []
        for row in _get_json("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes",
                             verify=False):
            code = str(row.get("SecuritiesCompanyCode", "")).strip()
            close = _num(row.get("Close"))
            vol = _num(row.get("TradingShares") or row.get("TradingVolume") or row.get("TradeVolume"))
            if re.match(r"^\d{4,6}$", code) and close and close > 0:
                rows[code] = [close, vol or 0]
                d = _parse_row_date(row.get("Date"))
                if d:
                    dates.append(d)
        by_mkt["TPEx"] = (max(dates) if dates else None, rows)
        print(f"  TPEx 快照: {len(rows)} 檔({by_mkt['TPEx'][0]})")
    except Exception as e:
        print(f"  ⚠ TPEx 快照失敗: {e}")

    return merge_markets(by_mkt)


def merge_markets(by_mkt):
    """{market: (date, rows)} → (date, snap, market)。只合併資料日期 = 最新日期的市場。"""
    dated = [d for d, rows in by_mkt.values() if d and rows]
    date = max(dated) if dated else None
    snap, market = {}, {}
    for mk, (d, rows) in by_mkt.items():
        if not rows:
            continue
        if date and d and d != date:
            print(f"  ⚠ {mk} 資料日 {d} 比 {date} 舊 → 這次不存 {mk}(不混兩天的價格)")
            continue
        for code, v in rows.items():
            snap[code] = v
            market[code] = mk
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
    # 2026-10-30:同一天重跑(例如這次只有上市、上櫃還沒更新)→ 和已存的合併,不把另一個市場蓋掉
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                snap = {**json.load(f), **snap}
        except Exception:
            pass
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


def _load_json_file(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def backfill(codes, market, current_month_only=False, force=False):
    """個股月資料補抓。
    2026-10-06 修正(實跑發現):TPEx 全市場快照持續失敗 → market 對照表沒有上櫃股
    → 上櫃股被誤送 TWSE 端點、全部「無資料」(42檔)。現在:歸屬未知的股票
    先試 TWSE、沒資料再試 TPEx,並把學到的歸屬存進 market.json 供之後使用。
    current_month_only+force:給「今天快照缺的股票」每天刷新當月資料用。"""
    store = _load_json_file(BACKFILL_FILE)
    learned = _load_json_file(MARKET_FILE)
    today = dt.date.today()
    months = [today.strftime("%Y%m")]
    if not current_month_only:
        months.append((today.replace(day=1) - dt.timedelta(days=1)).strftime("%Y%m"))
    ok = fail = 0
    empty = 0
    shown = 0
    tpex_skipped = 0
    # 2026-10-11:TPEx 個股端點(st43_result.php)實跑每次都回空內容
    # (JSONDecodeError),一輪白打約 80 次請求、log 洗版。連續失敗
    # TPEX_DEAD_STREAK 次就判定本輪失效,之後的上櫃股直接略過不再發請求;
    # main() 同一輪會呼叫 backfill() 兩次,失效狀態跨兩次呼叫沿用。
    tpex_streak = 0
    tpex_dead = getattr(backfill, "tpex_dead", False)
    for code in list(codes)[:MAX_BACKFILL_CODES]:
        if not force and len(store.get(code, {})) >= 21:
            continue
        known = market.get(code) or learned.get(code)
        order = [known] if known else ["TWSE", "TPEx"]
        got, used = {}, None
        for mk in order:
            if mk == "TPEx" and tpex_dead:
                tpex_skipped += 1
                continue
            fn = _backfill_tpex if mk == "TPEx" else _backfill_twse
            for ym in months:
                if mk == "TPEx" and tpex_dead:
                    break
                try:
                    got.update(fn(code, ym))
                    if mk == "TPEx":
                        tpex_streak = 0
                except Exception as e:
                    fail += 1
                    if fail <= FAIL_LOG_LIMIT:
                        print(f"    ⚠ 補抓 {code}({mk}) {ym} 失敗: {str(e)[:60]}")
                    if mk == "TPEx":
                        tpex_streak += 1
                        if tpex_streak >= TPEX_DEAD_STREAK:
                            tpex_dead = backfill.tpex_dead = True
                            print(f"    TPEx個股端點連續失敗{TPEX_DEAD_STREAK}次,本輪放棄"
                                  "(上櫃股歷史改靠每日全市場快照累積)")
                time.sleep(1.0)
            if got:
                used = mk
                break
        if got:
            store.setdefault(code, {}).update(got)
            if used and learned.get(code) != used:
                learned[code] = used
            ok += 1
        else:
            empty += 1
            if shown < 3:
                shown += 1
                print(f"    · {code} 補抓無資料(試過 {order})"
                      + (f" TPEx回傳結構={getattr(_backfill_tpex, 'last_keys', None)}"
                         if "TPEx" in order else ""))
    os.makedirs(HIST_DIR, exist_ok=True)
    with open(BACKFILL_FILE, "w", encoding="utf-8") as f:
        json.dump(store, f, ensure_ascii=False, separators=(",", ":"))
    with open(MARKET_FILE, "w", encoding="utf-8") as f:
        json.dump(learned, f, ensure_ascii=False, separators=(",", ":"))
    if fail > FAIL_LOG_LIMIT:
        print(f"    …其餘 {fail - FAIL_LOG_LIMIT} 筆失敗訊息省略")
    print(f"  個股補抓: 成功 {ok} 檔 / 失敗請求 {fail} 次 / 無資料 {empty} 檔"
          + (f" / TPEx失效略過 {tpex_skipped} 檔" if tpex_skipped else ""))


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
           "range_pos": None, "range_days": 0,
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

    # ── 價格位置:最近最多 RANGE_WINDOW 日收盤價區間,與 stage 完全獨立 ──
    window = [c for c in closes[-RANGE_WINDOW:] if c]
    res["range_days"] = len(window)
    if window:
        hi, lo = max(window), min(window)
        if hi > lo:
            res["range_pos"] = round((closes[-1] - lo) / (hi - lo), 3)
    rp = res["range_pos"]
    if res["range_days"] >= RANGE_MIN_DAYS and rp is not None:
        res["price_pos"] = "low" if rp <= POS_LOW_MAX else "mid" if rp <= POS_MID_MAX else "high"
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
    """顯示用短標籤,例:'🔴過熱+35% 位置高檔' / '🟢潛伏 位置中段' / '⚪未知'。
    位置(price_pos)沒有資料就不顯示位置。"""
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
    pos = POS_LABEL.get(info.get("price_pos"))
    if pos:
        s += f" 位置{pos}"
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
    print(f"  快照資料日期={date or '無Date欄位'}  執行日期={dt.date.today().strftime('%Y%m%d')}"
          "(若資料日期落後,代表交易所資料尚未更新,排程宜再晚一點)")
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
    # 今天快照裡缺的股票(例如TPEx全市場快照失敗時的上櫃股):逐檔刷新當月資料,
    # 否則它們的序列會停在上次補抓日,階段判斷會用到舊價格。
    missing_today = [c for c in sorted(targets) if c not in snap and c not in need]
    if missing_today:
        print(f"  今日快照缺 {len(missing_today)} 檔,逐檔刷新當月資料")
        backfill(missing_today, market, current_month_only=True, force=True)

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
