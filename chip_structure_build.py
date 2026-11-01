# -*- coding: utf-8 -*-
"""
chip_structure_build.py — 個股籌碼結構 Phase 3(方案 A):全市場精簡輸出 chip_structure_full.json(2026-10-31 新增)
============================================================
輸入:tdcc_weekly/*.csv.gz(集保週,全部週)、insti_daily/*.json(法人日)、trading_calendar(交易日)
計算:chip_structure_core.stock_series(純函式,無 look-ahead)
輸出:chip_structure_full.json —— 只放有法人資料的上市櫃股票(興櫃/未上市集保有、但 T86/TPEx 不涵蓋,不放)
  頂層:generated / tdcc_asof / inst_asof / date_mismatch / weeks / cfg / notes / stale
  每檔(鍵 = 代號):
    n   名稱(insti_daily 的 names)
    w   近 WEEKS_KEEP 週 [資料日 MM-DD, 大戶%, 千張%, 總人數](由舊到新)
    mr  最新一週 [中實戶%, 散戶%]
    d1  週變化 [大戶 pp, 散戶 pp, 人數];d4 四週變化 [大戶 pp, 人數](不足 → null)
    i   最新集保週法人合計 [外資, 投信, 自營商, 三大法人, 外資+自營商](股;缺任一交易日 → null)
    ag  agreement;bt big_trend;st stabilizing(null = 歷史不足)
    fp  [外資持股%, 4 週變化 pp](缺 → null)
    fl  旗標(capital_change_suspect / missing_week / inst_missing_days / inst_zero_filled_days …)
全部屬「描述」:不是買賣建議,不影響任何雷達的收錄、排序、門檻。
失敗(例如沒有集保檔)→ 保留舊檔、只把 stale 改成 true,不覆寫成空資料。
GAS ChipStructureFetch.js 每小時讀這份,只挑當下頁面要顯示的股票(≤150 檔)存進分頁。
============================================================
"""
import os
import re
import sys
import glob
import gzip
import json
import datetime as dt

import tdcc_fetch
import trading_calendar
import chip_structure_core as cs

OUT_FILE = "chip_structure_full.json"
TDCC_DIR = "tdcc_weekly"
INSTI_DIR = "insti_daily"
WEEKS_KEEP = 8
TPE = dt.timezone(dt.timedelta(hours=8))
BANNED = ("主力", "洗盤", "吸籌", "看多", "看空", "建議買進")


def load_weeks(tdcc_dir=TDCC_DIR):
    """→ [(資料日, {代號: {分級: {...}}})] 由舊到新。"""
    out = []
    for p in sorted(glob.glob(os.path.join(tdcc_dir, "*.csv.gz"))):
        with gzip.open(p, "rb") as f:
            d, rows = tdcc_fetch.parse(tdcc_fetch.decode(f.read()))
        out.append((d, cs.levels_from_rows(rows)))
    return out


def load_insti(insti_dir=INSTI_DIR):
    out = {}
    for p in glob.glob(os.path.join(insti_dir, "2*.json")):
        m = re.match(r"(\d{8})\.json$", os.path.basename(p))
        if m:
            with open(p, encoding="utf-8") as f:
                out[m.group(1)] = json.load(f)
    return out


def _r(x, n=2):
    return None if x is None else round(x, n)


def compact(code, series, names):
    """stock_series 的結果 → 精簡格式;最新一週沒有集保資料 → None。"""
    last = series[-1]
    m = last["m"]
    if m is None:
        return None
    w = [[r["d"][5:], _r(r["m"]["pct_big"]), _r(r["m"]["pct_1000"]), r["m"]["holders_total"]]
         for r in series[-WEEKS_KEEP:] if r["m"]]
    d1, d4, inst = last["d1w"], last["d4w"], last["inst"]
    return {
        "n": names.get(code, ""),
        "w": w,
        "mr": [_r(m["pct_mid"]), _r(m["pct_retail"])],
        "d1": [_r(d1["big"], 3), _r(d1["retail"], 3), d1["holders"]] if d1 and d1["big"] is not None else None,
        "d4": [_r(d4["big"], 3), d4["holders"]] if d4 and d4["big"] is not None else None,
        "i": None if inst["net"] is None else [inst["foreign"], inst["trust"], inst["dealer"], inst["net"],
                                               inst["net_for_agreement"]],
        "ag": last["agreement"],
        "bt": last["big_trend"],
        "st": last["stabilizing"],
        "fp": [last["foreign_pct"], last["foreign_pct_d4w"]] if last["foreign_pct"] is not None else None,
        "fl": last["flags"],
    }


def build(weeks, insti, calendar, now=None):
    now = now or dt.datetime.now(TPE)
    tdcc_dates = [d for d, _ in weeks]
    names, listed = {}, set()
    for doc in insti.values():
        names.update({k: v for k, v in (doc.get("names") or {}).items() if v})
        for mk in ("twse", "tpex"):
            listed.update((doc.get(mk) or {}).keys())
    inst_asof = max(insti) if insti else None
    stocks = {}
    for code in sorted(c for c in weeks[-1][1] if c in listed):
        row = compact(code, cs.stock_series(code, weeks, insti, calendar), names)
        if row:
            stocks[code] = row
    return {
        "generated": now.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "tdcc_asof": tdcc_dates[-1],
        "inst_asof": f"{inst_asof[:4]}-{inst_asof[4:6]}-{inst_asof[6:]}" if inst_asof else None,
        "date_mismatch": cs.date_mismatch(tdcc_dates[-1], inst_asof, tdcc_dates),
        "weeks": tdcc_dates[-WEEKS_KEEP:],
        "stale": False,
        "cfg": {k: cs.CHIP_CFG[k] for k in ("big_min_shares", "trend_pp", "flat_pp", "big_delta_min_pp",
                                             "inst_min_pct", "share_jump_pct")},
        "fields": {"w": "[資料日 MM-DD, 大戶%, 千張%, 總人數]", "mr": "[中實戶%, 散戶%]", "d1": "[大戶pp, 散戶pp, 人數]",
                   "d4": "[大戶pp, 人數]", "i": "[外資, 投信, 自營商, 三大法人, 外資+自營商](股,同一集保週)",
                   "fp": "[外資持股%, 4週變化pp]"},
        "notes": cs.NOTES,
        "count": len(stocks),
        "stocks": stocks,
    }


def check_text(payload):
    """輸出文字不得有禁用字眼。"""
    text = json.dumps({k: payload[k] for k in ("notes", "fields")}, ensure_ascii=False)
    bad = [w for w in BANNED if w in text]
    if bad:
        raise ValueError(f"輸出含禁用字眼:{bad}")


def write(payload, path=OUT_FILE):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))


def unchanged(payload, path=OUT_FILE):
    """除了 generated 之外都和現有檔案相同 → True(集保週與法人都沒變時不必再 commit)。"""
    try:
        with open(path, encoding="utf-8") as f:
            old = json.load(f)
    except Exception:
        return False
    strip = lambda d: {k: v for k, v in d.items() if k not in ("generated",)}
    return strip(old) == json.loads(json.dumps(strip(payload), ensure_ascii=False))


def mark_stale(path=OUT_FILE, reason=""):
    try:
        with open(path, encoding="utf-8") as f:
            old = json.load(f)
    except Exception:
        print(f"  ⚠ 沒有舊檔可保留({reason})")
        return
    old["stale"] = True
    old["stale_reason"] = reason
    write(old, path)
    print(f"  ⚠ 保留舊檔(tdcc_asof {old.get('tdcc_asof')}),標 stale:{reason}")


def main():
    try:
        weeks = load_weeks()
        if not weeks:
            raise RuntimeError("tdcc_weekly 沒有任何週資料")
        payload = build(weeks, load_insti(), trading_calendar.known_trading_days("."))
        check_text(payload)
    except Exception as e:
        mark_stale(reason=f"{type(e).__name__}: {e}")
        return 1
    if unchanged(payload):
        print(f"  內容和現有 {OUT_FILE} 相同(只差產生時間),不重寫")
        return 0
    write(payload)
    size = os.path.getsize(OUT_FILE)
    print(f"→ {OUT_FILE}:{payload['count']} 檔、{len(payload['weeks'])} 週、tdcc {payload['tdcc_asof']}、"
          f"法人 {payload['inst_asof']}、date_mismatch {payload['date_mismatch']}、{size / 1024:.0f} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
