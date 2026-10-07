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

2026-10-07修正:stage(動能階段)跟price_pos(價格位階)原本是同一件事
「硬套」出來的(STAGE_TO_POS = {潛伏:low, 啟動:mid, 過熱:high}),但stage
只量近20日漲幅/量比/漲停次數,跟股價在一年區間的哪個位置完全是兩件不
相干的事——實測發現台積電(2330)10/06創歷史新高收盤,卻因為近20日沒有
爆量大漲被標成「潛伏/low」,報告裡還被顯示成「🟢底部」;穎崴(6515)只
從高點回檔12%,卻因為同一套邏輯被講成「具安全邊際的切入契機」。
現在兩者完全脫鉤、各自獨立計算:
  stage:維持上面的動能定義不變,門檻數字不動。
  price_pos:改成「距一年收盤新高的跌幅」—— <10%→high(🔴高檔)、
    10~25%→mid(🟡中段)、>25%→low(🟢底部),門檻數字見下方
    POS_HIGH_MAX_PCT/POS_LOW_MIN_PCT。沒有真正涵蓋回溯一年的歷史資料
    (回溯天數不足,或窗口內資料太稀疏)時,price_pos=None,顯示「位階
    未知」——不用stage或短期漲跌幅代打,資料不夠就是不夠。
一年歷史的資料來源:只對「當天雷達實際點名到的個股」(跟backfill()既有
範圍邏輯一樣,不對全市場做一年回補)用TWSE STOCK_DAY逐月自行回補,見
backfill_one_year()。TPEx的st43個股歷史端點目前是壞的(backfill()裡的
電路斷路器會證實這點),上櫃股的一年歷史只能靠每日全市場快照慢慢累積
(約252個交易日),這段時間price_pos照實回None。
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

VERSION = "2026-10-07a"  # price_pos與stage脫鉤+save_snapshot分交易所比對重複

# ── 門檻(初始值,未校準)──
OVERHEAT_CHG20 = 30.0
ACTIVE_CHG20 = 10.0
ACTIVE_CHG5 = 5.0
ACTIVE_VOL_RATIO = 2.0
LIMIT_UP_PCT = 9.5
OVERHEAT_LIMIT_UP_DAYS = 2
MIN_HISTORY_DAYS = 6

# ── 一年新高位階門檻(2026-10-07新增,見price_pos說明)──
POS_HIGH_MAX_PCT = 10.0   # 距一年收盤新高跌幅 <10% → high(🔴高檔)
POS_LOW_MIN_PCT = 25.0    # 距一年收盤新高跌幅 >25% → low(🟢底部),10~25%之間是mid
HIGH_LOOKBACK_DAYS = 365  # 一年新高判斷窗口(日曆天)
MIN_HIGH_SPAN_DAYS = 330  # 該股歷史至少要回溯這麼多天,才敢說「有一年可比」
MIN_HIGH_WINDOW_DAYS = 150  # 一年窗口內至少要有這麼多個交易日資料,避免太稀疏
HIGH_LOOKBACK_MONTHS = 12   # backfill_one_year() 目標月數
HIGH_BACKFILL_MAX_CODES = 60  # 一年新高回補每次最多處理幾檔(只動TWSE,每檔一個月)


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


def save_snapshot(date, snap, market):
    """存今日快照。若與最新一份幾乎完全相同(假日重複抓到上個交易日),不存。

    2026-10-07修正:原本拿整份快照(TWSE+TPEx混在一起)跟上一份比對相同
    比例,門檻0.9。實測10/06當天TWSE資料其實跟前一天100%一字不差(抓到
    還沒更新的舊資料),但因為同一次執行裡TPEx有6599檔是新抓到的,混在
    一起算的相同比例被稀釋到只剩15.9%,遠低於0.9門檻,導致這份「TWSE其實
    是重複的」快照被當成新的一天存了進去,下游chg1=0.0整批出現。改成
    分別比對TWSE、TPEx各自的相同比例,只有兩邊都跟前一天幾乎一樣(都
    ≥0.9)才判定整份重複不存;任一邊有新資料,就代表這份快照有價值,
    整份存下來(不逐檔拆開存,維持原本「一天一個檔案」的簡單格式)。"""
    os.makedirs(HIST_DIR, exist_ok=True)
    hist = load_history()
    if hist:
        last_d, last = hist[-1]

        def _match_ratio(codes):
            codes = [c for c in codes if c in snap]
            if not codes:
                return 1.0  # 這個交易所今天完全沒抓到資料,不構成「有新資料」的理由
            same = sum(1 for c in codes if c in last and last[c][0] == snap[c][0] and last[c][1] == snap[c][1])
            return same / len(codes)

        twse_codes = [c for c, m in market.items() if m == "TWSE"]
        tpex_codes = [c for c, m in market.items() if m == "TPEx"]
        twse_same = _match_ratio(twse_codes)
        tpex_same = _match_ratio(tpex_codes)
        if twse_same >= 0.9 and tpex_same >= 0.9:
            print(f"  快照與 {last_d} 相同(休市日),不重複存"
                  f"(TWSE相同比{twse_same:.0%} TPEx相同比{tpex_same:.0%})")
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


def _load_json_file(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _year_months(n):
    """回傳含當月、往前推n-1個月的'YYYYMM'清單,由新到舊。"""
    out = []
    y, m = dt.date.today().year, dt.date.today().month
    for _ in range(n):
        out.append(f"{y}{m:02d}")
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    return out


def _missing_year_months(code, hist, store):
    """這檔股票近12個月裡,還沒有任何資料覆蓋的月份(新到舊排序)。"""
    series = _series_for(code, hist, store)
    covered = {d[:6] for d, _, _ in series}
    return [ym for ym in _year_months(HIGH_LOOKBACK_MONTHS) if ym not in covered]


def backfill_one_year(codes, market):
    """2026-10-07新增:一年新高的資料來源——自行用TWSE STOCK_DAY逐月回補,
    範圍限縮在當天雷達實際點名到的個股(跟backfill()既有範圍邏輯一樣,
    不對全市場做一年回補)。每次只替每檔補「最舊一個還缺的月份」(不是
    一次補滿12個月),一天進一點,約2週內讓有出現在雷達裡的TWSE股票
    累積滿12個月、算出真正的一年新高,避免單次執行為了一次補滿12個月
    狂打上百個請求、拖垮執行時間。
    只做TWSE——TPEx的st43個股歷史端點目前是壞的(backfill()裡的電路
    斷路器已證實這點),上櫃股的一年歷史只能靠每日全市場快照慢慢累積
    (約252個交易日),這段時間price_pos照實回None、顯示「位階未知」,
    不用短期漲跌幅代打。"""
    hist = load_history()
    store = _load_json_file(BACKFILL_FILE)
    ok = fail = skip = 0
    for code in [c for c in codes if market.get(c) == "TWSE"][:HIGH_BACKFILL_MAX_CODES]:
        missing = _missing_year_months(code, hist, store)
        if not missing:
            skip += 1
            continue
        ym = missing[-1]  # 最舊的缺口先補,由遠到近逐步補滿
        try:
            got = _backfill_twse(code, ym)
            if got:
                store.setdefault(code, {}).update(got)
                ok += 1
        except Exception:
            fail += 1
        time.sleep(1.0)
    with open(BACKFILL_FILE, "w", encoding="utf-8") as f:
        json.dump(store, f, ensure_ascii=False, separators=(",", ":"))
    print(f"  一年新高回補(TWSE,每檔補最舊缺的一個月): 成功 {ok} 檔 / "
          f"失敗 {fail} 檔 / 已滿12個月 {skip} 檔")


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
    shown_fail = 0
    tpex_streak = 0
    tpex_dead = False   # 連續失敗太多次就放棄本輪TPEx個股端點,避免白等+洗版
    for code in list(codes)[:MAX_BACKFILL_CODES]:
        if not force and len(store.get(code, {})) >= 21:
            continue
        known = market.get(code) or learned.get(code)
        order = [known] if known else ["TWSE", "TPEx"]
        got, used = {}, None
        for mk in order:
            if mk == "TPEx" and tpex_dead:
                continue
            fn = _backfill_tpex if mk == "TPEx" else _backfill_twse
            for ym in months:
                try:
                    got.update(fn(code, ym))
                    if mk == "TPEx":
                        tpex_streak = 0
                except Exception as e:
                    fail += 1
                    if shown_fail < 3:
                        shown_fail += 1
                        print(f"    ⚠ 補抓 {code}({mk}) {ym} 失敗: {str(e)[:60]}")
                    if mk == "TPEx":
                        tpex_streak += 1
                        if tpex_streak >= 6 and not tpex_dead:
                            tpex_dead = True
                            print("    ⚠ TPEx個股端點連續失敗6次,本輪放棄(上櫃股歷史改靠每日全市場快照累積)")
                        if tpex_dead:
                            break
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
    print(f"  個股補抓: 成功 {ok} 檔 / 失敗請求 {fail} 次 / 無資料 {empty} 檔")


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


def _price_position(series):
    """距一年收盤新高的跌幅,跟stage(動能)完全獨立計算。
    需要真正涵蓋回溯一年的歷史(見MIN_HIGH_SPAN_DAYS/MIN_HIGH_WINDOW_DAYS),
    不足就回(None, None)——顯示「位階未知」,不用stage或短期漲跌幅代打。
    回傳 (price_pos, pct_below_high)。"""
    if not series:
        return None, None
    today = dt.date.today()
    first_date = dt.datetime.strptime(series[0][0], "%Y%m%d").date()
    if (today - first_date).days < MIN_HIGH_SPAN_DAYS:
        return None, None
    cutoff = today - dt.timedelta(days=HIGH_LOOKBACK_DAYS)
    window = [c for d, c, v in series if dt.datetime.strptime(d, "%Y%m%d").date() >= cutoff]
    if len(window) < MIN_HIGH_WINDOW_DAYS:
        return None, None
    high = max(window)
    last = series[-1][1]
    if not high:
        return None, None
    pct_below = round((high - last) / high * 100, 1)
    if pct_below < POS_HIGH_MAX_PCT:
        pos = "high"
    elif pct_below <= POS_LOW_MIN_PCT:
        pos = "mid"
    else:
        pos = "low"
    return pos, pct_below


def _classify(series, overheat_flag):
    n = len(series)
    closes = [s[1] for s in series]
    vols = [s[2] for s in series]
    res = {"history_days": n, "stage": "未知", "price_pos": None, "pct_below_high": None,
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
    res["price_pos"], res["pct_below_high"] = _price_position(series)
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
    """顯示用短標籤(動能階段),例:'🔴過熱+35.2%' / '🟢潛伏' / '⚪未知'。"""
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


def pos_tag(info):
    """顯示用短標籤(價格位階,跟上面的動能階段tag()完全獨立),例:
    '🔴高檔(距高點-6%)' / '🟢底部(距高點-32%)' / '位階未知'。"""
    if not info or info.get("price_pos") is None:
        return "位階未知"
    icon = {"low": "🟢", "mid": "🟡", "high": "🔴"}.get(info["price_pos"], "⚪")
    label = {"low": "底部", "mid": "中段", "high": "高檔"}.get(info["price_pos"], "")
    pct = info.get("pct_below_high")
    pct_str = f"(距高點-{pct:.0f}%)" if pct is not None else ""
    return f"{icon}{label}{pct_str}"


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
    print(f"price_stage 開始(價量階段標籤) [版本 {VERSION}]")
    print("=" * 50)
    date, snap, market = fetch_market_snapshot()
    if not snap:
        print("  ⚠ 兩邊快照都失敗,略過(不影響其他流程)")
        return
    print(f"  快照資料日期={date or '無Date欄位'}  執行日期={dt.date.today().strftime('%Y%m%d')}"
          "(若資料日期落後,代表交易所資料尚未更新,排程宜再晚一點)")
    save_snapshot(date, snap, market)

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

    # 2026-10-07新增:一年新高回補(見backfill_one_year docstring),只對
    # TWSE股票逐檔補最舊缺的一個月,跟上面tier-1(近21日)的回補分開跑。
    learned_market = _load_json_file(MARKET_FILE)
    twse_targets = [c for c in sorted(targets) if (market.get(c) or learned_market.get(c)) == "TWSE"]
    if twse_targets:
        backfill_one_year(twse_targets, {**learned_market, **market})

    stages = stage_for_codes(targets, overheat)
    with open("price_stage.json", "w", encoding="utf-8") as f:
        json.dump({"generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                   "stages": stages}, f, ensure_ascii=False, indent=1)
    cnt = {}
    pos_cnt = {}
    for v in stages.values():
        cnt[v["stage"]] = cnt.get(v["stage"], 0) + 1
        pos_cnt[v["price_pos"]] = pos_cnt.get(v["price_pos"], 0) + 1
    print(f"  動能階段分布: {cnt}")
    print(f"  價格位階分布(price_pos,None=位階未知): {pos_cnt}")
    for c in sorted(stages, key=lambda x: -(stages[x].get("chg20") or -999))[:15]:
        print(f"    {c} {tag(stages[c])} {pos_tag(stages[c])}  歷史{stages[c]['history_days']}日 "
              f"量比{stages[c].get('vol_ratio')}")


if __name__ == "__main__":
    main()
