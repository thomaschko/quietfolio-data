# -*- coding: utf-8 -*-
"""
tdcc_fetch.py — 集保戶股權分散表 每週累積(Phase 1;2026-10-26 新增)
============================================================
2026-10-26 Phase 0 探測(tdcc_probe.py,run 40368721665)結果 ok_latest_only:
  smart.tdcc.com.tw/opendata/getOD.ashx?id=1-5 → robots.txt「Disallow: /」,不抓。
  opendata.tdcc.com.tw/getOD.ashx?id=1-5 → 沒有 robots.txt,GET/POST 內容相同,UTF-8(BOM)CSV 約 2.4 MB,
    欄位:資料日期, 證券代號, 持股分級, 人數, 股數, 占集保庫存數比例%;只有最新一週(約 4,100 檔、69,700 列)。
  分級 1–15 是持股級距、16 是差異數調整、17 是合計。2337 樣本:
    人數 Σ1–15 = 17;股數 Σ1–15 − 第16級 = 17(第 16 級是 2 人、10,000 股)。其他兩檔第 16 級為 0。
  官網歷史查詢頁有驗證碼 → 不自動化;FinMind 沒測(沒有 token)。歷史只能從開始累積的那週算起。
做法:
  1 次 GET 原始 CSV(間隔 ≥ 2 秒;先看 robots.txt,不允許就停),整份原樣存
  tdcc_weekly/YYYY-MM-DD.csv.gz(含 16、17 級,不先聚合)。資料日期已經存過 → 跳過、不覆寫。
  每次執行(含跳過)都記一筆到 tdcc_weekly/fetch_log.json(抓取時間 vs 資料日期),
  累積幾週後就能看出實際更新時間(Phase 0 只跑一次,看不出來)。
  檢查(只標示,不修正、不補值):
    缺欄位、多個資料日期、分級不在 1–17 → 報錯不存
    資料日是週末 → 報錯不存;平日但 repo 內沒有開盤資料 → 存,但標 trading_day=unverified
    每檔 人數 Σ1–15 = 17、股數 Σ1–15 − 16 = 17 → 不符的代號記在 index.json
  tdcc_weekly/ 總大小超過 100 MB 前會在 log 和 index 標示,不自行刪舊週。
============================================================
"""
import os
import io
import csv
import sys
import gzip
import json
import time
import datetime as dt
from urllib import robotparser
from urllib.parse import urlsplit

import requests

import trading_calendar

URL = "https://opendata.tdcc.com.tw/getOD.ashx?id=1-5"
UA = "quietfolio/1.0 (+https://github.com/thomaschko/quietfolio-data; weekly open-data download, 1 file/week)"
OUT_DIR = "tdcc_weekly"
INDEX = os.path.join(OUT_DIR, "index.json")
FETCH_LOG = os.path.join(OUT_DIR, "fetch_log.json")
FETCH_LOG_KEEP = 120
SIZE_WARN_BYTES = 100 * 1024 * 1024
MIN_GAP = 2.0
BLOCK = (401, 403, 451)
TPE = dt.timezone(dt.timedelta(hours=8))
LEVEL_ADJ, LEVEL_TOTAL = 16, 17
COLS = {"date": ("資料日期",), "code": ("證券代號",), "level": ("持股分級",),
        "holders": ("人數",), "shares": ("股數",), "pct": ("占集保庫存數比例",)}


class TdccError(ValueError):
    pass


# ── 解析與檢查(純函式)──
def decode(raw):
    for enc in ("utf-8-sig", "cp950"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise TdccError("CSV 編碼無法辨識(utf-8 / cp950 都失敗)")


def _num(s):
    s = str(s).replace(",", "").strip()
    if s == "":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse(text):
    """CSV 文字 → (data_date 'YYYY-MM-DD', rows [{code, level, holders, shares, pct}])。格式不對 → TdccError。"""
    lines = [r for r in csv.reader(io.StringIO(text)) if any(c.strip() for c in r)]
    if len(lines) < 2:
        raise TdccError("CSV 沒有資料列")
    header = [h.strip() for h in lines[0]]
    idx = {}
    for k, keys in COLS.items():
        hit = [i for i, h in enumerate(header) if any(x in h for x in keys)]
        if not hit:
            raise TdccError(f"缺欄位:{keys[0]}(實際表頭:{header})")
        idx[k] = hit[0]
    dates, rows = set(), []
    for n, r in enumerate(lines[1:], start=2):
        if len(r) <= max(idx.values()):
            raise TdccError(f"第 {n} 列欄位不足:{r}")
        lv = _num(r[idx["level"]])
        if lv is None or lv != int(lv) or not 1 <= lv <= LEVEL_TOTAL:
            raise TdccError(f"第 {n} 列持股分級不在 1–17:{r[idx['level']]!r}")
        dates.add(r[idx["date"]].strip())
        rows.append({"code": r[idx["code"]].strip(), "level": int(lv), "holders": _num(r[idx["holders"]]),
                     "shares": _num(r[idx["shares"]]), "pct": _num(r[idx["pct"]])})
    if len(dates) != 1:
        raise TdccError(f"資料日期不只一個:{sorted(dates)[:5]}")
    d = dates.pop()
    try:
        data_date = dt.datetime.strptime(d, "%Y%m%d").date().isoformat()
    except ValueError:
        raise TdccError(f"資料日期格式不對:{d!r}")
    return data_date, rows


def sum_check(rows):
    """→ {holders_mismatch: [代號], shares_mismatch: [代號], no_total: [代號]}。
    人數:Σ1–15 = 17;股數:Σ1–15 − 16 = 17(Phase 0 的 2337 樣本)。只標示,不修正。"""
    by = {}
    for r in rows:
        by.setdefault(r["code"], {})[r["level"]] = r
    out = {"holders_mismatch": [], "shares_mismatch": [], "no_total": []}
    for code, lv in sorted(by.items()):
        tot = lv.get(LEVEL_TOTAL)
        if tot is None or tot["holders"] is None or tot["shares"] is None:
            out["no_total"].append(code)
            continue
        h = sum(lv[i]["holders"] or 0 for i in range(1, 16) if i in lv)
        s = sum(lv[i]["shares"] or 0 for i in range(1, 16) if i in lv)
        adj = (lv[LEVEL_ADJ]["shares"] or 0) if LEVEL_ADJ in lv else 0
        if h != tot["holders"]:
            out["holders_mismatch"].append(code)
        if s - adj != tot["shares"]:
            out["shares_mismatch"].append(code)
    return out


def week_path(data_date, out_dir=OUT_DIR):
    return os.path.join(out_dir, f"{data_date}.csv.gz")


# ── IO ──
def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write_json(path, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


def dir_bytes(out_dir=OUT_DIR):
    return sum(os.path.getsize(os.path.join(out_dir, n)) for n in os.listdir(out_dir)) if os.path.isdir(out_dir) else 0


class _Http:
    def __init__(self, sleep=time.sleep, clock=time.time):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA})
        self.sleep, self.clock, self.last = sleep, clock, None

    def get(self, url):
        if self.last is not None:
            w = MIN_GAP - (self.clock() - self.last)
            if w > 0:
                self.sleep(w)
        try:
            return self.s.get(url, timeout=60)
        finally:
            self.last = self.clock()


def robots_ok(http, url=URL):
    """robots.txt 不存在(404)= 允許;存在就照規則;抓不到(其他錯誤)= 不抓。"""
    p = urlsplit(url)
    r = http.get(f"{p.scheme}://{p.netloc}/robots.txt")
    if r.status_code == 404:
        return True
    if r.status_code != 200:
        return False
    rp = robotparser.RobotFileParser()
    rp.parse(r.text.splitlines())
    return rp.can_fetch(UA, url) and rp.can_fetch("*", url)


def run(http=None, now=None, out_dir=OUT_DIR, base=".", sleep=time.sleep):
    """→ (status, data_date)。status:saved / skipped_same_date / blocked / failed / invalid。"""
    http = http or _Http(sleep=sleep)
    now = now or dt.datetime.now(TPE)
    index_path, log_path = os.path.join(out_dir, "index.json"), os.path.join(out_dir, "fetch_log.json")
    log = _read_json(log_path, [])
    rec = {"fetched_at": now.strftime("%Y-%m-%d %H:%M"), "url": URL}

    def finish(status, data_date=None, **extra):
        rec.update(status=status, data_date=data_date, **extra)
        _write_json(log_path, (log + [rec])[-FETCH_LOG_KEEP:])
        print(f"  集保分散表:{status} 資料日={data_date} {extra or ''}")
        return status, data_date

    try:
        if not robots_ok(http):
            return finish("blocked", error="robots.txt 不允許或無法讀取")
        raw, err = None, None
        for attempt in range(2):
            if attempt:
                sleep(5)
            r = http.get(URL)
            if r.status_code == 200:
                raw = r.content
                break
            err = f"HTTP {r.status_code}"
            if r.status_code in BLOCK:
                return finish("blocked", error=err)
        if raw is None:
            return finish("failed", error=err)
    except requests.RequestException as e:
        return finish("failed", error=f"{type(e).__name__}: {e}")

    try:
        data_date, rows = parse(decode(raw))
        trading = trading_calendar.check_trading_day(data_date, trading_calendar.known_trading_days(base))
    except (TdccError, ValueError) as e:
        return finish("invalid", error=str(e))

    path = week_path(data_date, out_dir)
    if os.path.exists(path):
        return finish("skipped_same_date", data_date)

    os.makedirs(out_dir, exist_ok=True)
    with gzip.open(path, "wb", compresslevel=9) as f:
        f.write(raw)
    chk = sum_check(rows)
    index = _read_json(index_path, {"weeks": {}})
    index["weeks"][data_date] = {
        "fetched_at": rec["fetched_at"], "file": os.path.basename(path), "bytes_raw": len(raw),
        "rows": len(rows), "stocks": len({r["code"] for r in rows}), "trading_day": trading,
        "holders_mismatch": chk["holders_mismatch"], "shares_mismatch": chk["shares_mismatch"],
        "no_total": chk["no_total"]}
    index["latest"] = max(index["weeks"])
    index["total_bytes"] = dir_bytes(out_dir)
    index["size_warning"] = index["total_bytes"] >= SIZE_WARN_BYTES
    index["source"] = URL
    index["notes"] = ["分級 1–15 為持股級距、16 為差異數調整、17 為合計;原檔不聚合、不修正。",
                      "股數檢查用 Σ1–15 − 第16級 = 第17級;人數檢查用 Σ1–15 = 第17級。不符只標示。",
                      "trading_day=unverified:資料日是平日,但 repo 內沒有那天的開盤資料,無法確認是否為交易日。"]
    _write_json(index_path, index)
    if index["size_warning"]:
        print(f"  ⚠ tdcc_weekly/ 已 {index['total_bytes'] / 1e6:.1f} MB(≥ 100 MB),請決定是否搬移歷史;不自動刪除")
    return finish("saved", data_date, trading_day=trading, rows=len(rows),
                  holders_mismatch=len(chk["holders_mismatch"]), shares_mismatch=len(chk["shares_mismatch"]))


def main():
    status, _ = run()
    # 被擋或格式不對要讓 workflow 顯示紅燈;沒更新(同一週)是正常
    sys.exit(1 if status in ("blocked", "failed", "invalid") else 0)


if __name__ == "__main__":
    main()
