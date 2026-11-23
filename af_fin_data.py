# -*- coding: utf-8 -*-
"""
af_fin_data.py — 反脆弱選股模組:FinMind 財報 / 估值歷史資料管線(2026-11-23)v2026-11-23-init
============================================================
規格 §2:官方彙總表(t187ap06/07)只有最新一季且沒有現金、借款、利息、營業現金流 → 改用 FinMind 逐檔(免費 token)。
逐檔抓 4 個資料集,只留需要的科目,存成 af_data/*.csv.gz(長格式 code,date,type,value):
  is   TaiwanStockFinancialStatements  營收、毛利、營業利益、EPS、稅後淨利(單季)
  bs   TaiwanStockBalanceSheet         總資產、總負債、現金、短 / 長期借款等(季末時點數)
  cf   TaiwanStockCashFlowsStatement   營業現金流、利息費用、資本支出(年初至今累計)
  per  TaiwanStockPER                  本益比、股價淨值比、殖利率(只在首次回補 5 年;之後由 af_daily.py 每日累積)
       為控制檔案大小,每檔每週只留最後一個交易日(週取樣;5 年約 260 點,算自身百分位足夠)。
股票池:沿用 bt_disloc/universe.csv.gz(FinMind TaiwanStockInfo 上市 / 上櫃 4 碼普通股)。
更新策略:is/bs/cf 第一次全部回補;之後只在「上次抓取後又過了一個季報法定期限」才重抓該檔。
額度:FinMind token 與 GAS 共用每小時 600 次 → 每次請求間隔 GAP 秒(預設 8 秒);402 連續 3 次就停;超過 --max-minutes 存檔結束。
"""
import argparse
import csv
import datetime as dt
import gzip
import json
import os
import re
import sys
import time

from disloc_bt_data import Client, Quota

VERSION = "v2026-11-23-init"
OUT_DIR = "af_data"
UNIVERSE = os.path.join("bt_disloc", "universe.csv.gz")
FIN_START = "2020-01-01"          # 近 5 個完整年度營收年增 / 虧損年數 + 12 季毛利 + 8 季 FCF
PER_YEARS = 5
DATASETS = {"is": "TaiwanStockFinancialStatements", "bs": "TaiwanStockBalanceSheet", "cf": "TaiwanStockCashFlowsStatement"}
KEEP = {
    "is": re.compile(r"^(Revenue|GrossProfit|OperatingIncome|EPS|IncomeAfterTaxes|EquityAttributableToOwnersOfParent)$"),
    "bs": re.compile(r"^(TotalAssets|Liabilities|Equity|CurrentAssets|CurrentLiabilities|CashAndCashEquivalents|"
                     r"\w*Borrowings\w*|\w*BillsPayable\w*|BondsPayable|LongtermLiabilitiesCurrentPortion)$"),
    "cf": re.compile(r"^(CashFlowsFromOperatingActivities|NetCashInflowFromOperatingActivities|InterestExpense|PayTheInterest|"
                     r"PropertyAndPlantAndEquipment|AcquisitionOfPropertyPlantAndEquipment|"
                     r"PaymentsToAcquirePropertyPlantAndEquipment|PurchaseOfPropertyPlantAndEquipment)$"),
}
HEADERS = {"is": ["code", "date", "type", "value"], "bs": ["code", "date", "type", "value"],
           "cf": ["code", "date", "type", "value"], "per": ["code", "date", "per", "pbr", "dy"]}


def deadlines(today):
    """今天以前(含)最近的季報法定期限日:Q1 5/15、Q2 8/14、Q3 11/14、Q4 隔年 3/31。"""
    out = []
    for y in (today.year - 1, today.year):
        out += [dt.date(y, 3, 31), dt.date(y, 5, 15), dt.date(y, 8, 14), dt.date(y, 11, 14)]
    return max(d for d in out if d <= today)


def stale(last_fetch, today):
    """上次抓取日在最近一個季報期限之前 → 需要重抓。"""
    if not last_fetch:
        return True
    return dt.date.fromisoformat(last_fetch) < deadlines(today)


def keep_rows(kind, code, rows):
    out = {}
    for r in rows or []:
        t = r.get("type") or ""
        if t.endswith("_per") or not KEEP[kind].match(t):
            continue
        try:
            out[(r["date"], t)] = float(r["value"])
        except (KeyError, TypeError, ValueError):
            continue
    return [[code, d, t, v] for (d, t), v in sorted(out.items())]


def week_key(d):
    y, w, _ = dt.date.fromisoformat(d).isocalendar()
    return (y, w)


def weekly_per(code, rows):
    """每週只留最後一個交易日。"""
    wk = {}
    for r in rows or []:
        d = r.get("date")
        if not d:
            continue
        wk[week_key(d)] = [code, d, r.get("PER"), r.get("PBR"), r.get("dividend_yield")]
    return [wk[k] for k in sorted(wk)]


# ── 儲存(與 disloc_bt_data 同格式)──
def _path(name, out_dir):
    return os.path.join(out_dir, name + ".csv.gz")


def load_rows(name, out_dir=OUT_DIR):
    p = _path(name, out_dir)
    if not os.path.exists(p):
        return []
    with gzip.open(p, "rt", encoding="utf-8") as f:
        rd = csv.reader(f)
        next(rd, None)
        return [r for r in rd]


def save_rows(name, rows, out_dir=OUT_DIR):
    p = _path(name, out_dir)
    tmp = p + ".tmp"
    with gzip.open(tmp, "wt", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(HEADERS[name])
        w.writerows(rows)
    os.replace(tmp, p)


def load_universe(path=UNIVERSE):
    with gzip.open(path, "rt", encoding="utf-8") as f:
        rd = csv.reader(f)
        next(rd)
        return [r for r in rd]


def run(client, out_dir=OUT_DIR, max_minutes=330, clock=time.time, today=None, save_every=40, universe=None):
    t0 = clock()
    today = today or (dt.datetime.utcnow() + dt.timedelta(hours=8)).date()
    os.makedirs(out_dir, exist_ok=True)
    pp = os.path.join(out_dir, "progress.json")
    try:
        with open(pp, encoding="utf-8") as f:
            prog = json.load(f)
    except (OSError, ValueError):
        prog = {"fetched": {}, "empty": {}, "types_seen": {}}
    uni = universe if universe is not None else load_universe()
    codes = [u[0] for u in uni]
    data = {}
    for k in list(DATASETS) + ["per"]:
        by = {}
        for r in load_rows(k, out_dir):
            by.setdefault(r[0], []).append(r)
        data[k] = by
    log = {"version": VERSION, "fetched": 0, "requests": 0, "stopped": None}
    used0 = client.used

    def save():
        for k in data:
            save_rows(k, [r for c in sorted(data[k]) for r in data[k][c]], out_dir)
        prog["updated"] = today.isoformat()
        tmp = pp + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(prog, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, pp)

    # 先補沒抓過的,再補過期的
    todo = []
    for c in codes:
        f = prog["fetched"].get(c, {})
        need = [k for k in DATASETS if stale(f.get(k), today)]
        if "per" not in f:
            need.append("per")
        if need:
            todo.append((0 if not f else 1, c, need))
    todo.sort()
    print(f"af_fin_data {VERSION}:股票池 {len(codes)} 檔,待抓 {len(todo)} 檔(未抓過 {sum(1 for t in todo if t[0] == 0)})")
    n = 0
    try:
        for _, c, need in todo:
            if (clock() - t0) / 60 > max_minutes:
                log["stopped"] = "time"
                break
            f = prog["fetched"].setdefault(c, {})
            for k in need:
                if k == "per":
                    start = (today - dt.timedelta(days=365 * PER_YEARS + 10)).isoformat()
                    rows = client.get("TaiwanStockPER", c, start)
                    if rows is None:
                        continue
                    data["per"][c] = weekly_per(c, rows)
                else:
                    rows = client.get(DATASETS[k], c, FIN_START)
                    if rows is None:
                        continue
                    for r in rows:
                        t = r.get("type")
                        if t and not t.endswith("_per"):
                            prog["types_seen"].setdefault(k, {})
                            if len(prog["types_seen"][k]) < 400:
                                prog["types_seen"][k][t] = prog["types_seen"][k].get(t, 0) + 1
                    data[k][c] = keep_rows(k, c, rows)
                if not data[k].get(c):
                    prog["empty"].setdefault(k, [])
                    if c not in prog["empty"][k]:
                        prog["empty"][k].append(c)
                f[k] = today.isoformat()
            n += 1
            if n % save_every == 0:
                save()
                print(f"  已處理 {n}/{len(todo)},請求 {client.used - used0}")
    except Quota as e:
        log["stopped"] = str(e)
    log["fetched"] = n
    log["requests"] = client.used - used0
    prog["last_run"] = log
    save()
    print(json.dumps(log, ensure_ascii=False))
    return log


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-minutes", type=float, default=330)
    ap.add_argument("--gap", type=float, default=8.0)
    a = ap.parse_args()
    tok = os.environ.get("FINMIND_TOKEN", "")
    if not tok:
        print("沒有 FINMIND_TOKEN")
        return 1
    run(Client(tok, gap=a.gap), max_minutes=a.max_minutes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
