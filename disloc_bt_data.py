# -*- coding: utf-8 -*-
"""
disloc_bt_data.py — 錯殺回檔(路徑 A / B)回測的基本面資料管線(2026-11-15)
============================================================
依 docs/prereg_dislocation_path_b.md。逐檔向 FinMind 抓 2017 年起的:
  rev  月營收        TaiwanStockMonthRevenue       → year, month, revenue
  fin  季財報        TaiwanStockFinancialStatements → 季末日, Revenue, GrossProfit(與 Dislocation.js analyzeGrossMargin 同科目)
  cf   現金流量表    TaiwanStockCashFlowsStatement  → 季末日, 資本支出(年初至今累計;科目順序同 QualityGrowth.js QG_CAPEX_KEYS)
股票池:FinMind TaiwanStockInfo 的上市 / 上櫃 4 碼普通股(只有現存股票 → 倖存者偏差,報告需註明)。
輸出 bt_disloc/{universe,rev,fin,cf}.csv.gz 與 progress.json;可中斷續跑,全部抓完後再執行會直接結束。
額度:FinMind token 與 GAS 共用每小時 600 次 → 每次請求間隔 GAP 秒(預設 8 秒 ≈ 450 次/小時);
  402(額度超限)→ 等 15 分鐘再試,連續 3 次就停;執行時間超過 --max-minutes 就存檔結束。
價格(還原日線)不在這裡抓,回測時用 yfinance(與急跌反轉回測相同)。
"""
import argparse
import csv
import gzip
import io
import json
import os
import re
import sys
import time

import requests

FINMIND = "https://api.finmindtrade.com/api/v4/data"
OUT_DIR = "bt_disloc"
START = "2017-01-01"
CAPEX_KEYS = ("PropertyAndPlantAndEquipment", "AcquisitionOfPropertyPlantAndEquipment",
              "PaymentsToAcquirePropertyPlantAndEquipment", "PurchaseOfPropertyPlantAndEquipment")
DATASETS = ("rev", "fin", "cf")
HEADERS = {"universe": ["code", "name", "market", "industry"], "rev": ["code", "year", "month", "revenue"],
           "fin": ["code", "date", "revenue", "gross_profit"], "cf": ["code", "date", "capex_ytd"]}


class Quota(Exception):
    pass


class Client:
    def __init__(self, token, gap=8.0, session=None, sleep=time.sleep, clock=time.time):
        self.token, self.gap, self.s, self.sleep, self.clock = token, gap, session or requests.Session(), sleep, clock
        self.used, self._last, self.quota_hits = 0, 0.0, 0

    def get(self, dataset, data_id=None, start=None):
        """→ list(列)或 None(非 200 / 狀態異常);402 連續 3 次 → Quota"""
        while True:
            w = self.gap - (self.clock() - self._last)
            if w > 0:
                self.sleep(w)
            params = {"dataset": dataset}
            if data_id:
                params["data_id"] = data_id
            if start:
                params["start_date"] = start
            self._last = self.clock()
            self.used += 1
            r = self.s.get(FINMIND, params=params, headers={"Authorization": "Bearer " + self.token}, timeout=90)
            if r.status_code == 402:
                self.quota_hits += 1
                if self.quota_hits >= 3:
                    raise Quota("FinMind 額度連續超限 3 次")
                self.sleep(900)
                continue
            self.quota_hits = 0
            if r.status_code != 200:
                return None
            j = r.json()
            return j.get("data") if j.get("status") == 200 else None


def parse_universe(rows):
    out, seen = [], set()
    for r in rows or []:
        c = str(r.get("stock_id") or "")
        if not re.fullmatch(r"\d{4}", c) or c in seen or r.get("type") not in ("twse", "tpex"):
            continue
        ind = r.get("industry_category") or ""
        if "ETF" in ind or "ETN" in ind or "受益證券" in ind or "存託憑證" in ind:
            continue
        seen.add(c)
        out.append([c, r.get("stock_name") or "", r["type"], ind])
    return sorted(out)


def parse_rev(code, rows):
    out = {}
    for r in rows or []:
        try:
            out[(int(r["revenue_year"]), int(r["revenue_month"]))] = float(r["revenue"])
        except (KeyError, TypeError, ValueError):
            continue
    return [[code, y, m, v] for (y, m), v in sorted(out.items())]


def parse_fin(code, rows):
    q = {}
    for r in rows or []:
        t = r.get("type")
        if t in ("Revenue", "GrossProfit"):
            try:
                q.setdefault(r["date"], {})[t] = float(r["value"])
            except (KeyError, TypeError, ValueError):
                continue
    return [[code, d, v.get("Revenue"), v.get("GrossProfit")] for d, v in sorted(q.items())]


def parse_cf(code, rows):
    by = {}
    for r in rows or []:
        try:
            by.setdefault(r["date"], {})[r["type"]] = float(r["value"])
        except (KeyError, TypeError, ValueError):
            continue
    out = []
    for d, raw in sorted(by.items()):
        v = next((raw[k] for k in CAPEX_KEYS if k in raw), None)
        if v is not None:
            out.append([code, d, v])
    return out


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


def run(client, out_dir=OUT_DIR, max_minutes=330, clock=time.time, save_every=40):
    t0 = clock()
    os.makedirs(out_dir, exist_ok=True)
    pp = os.path.join(out_dir, "progress.json")
    try:
        with open(pp, encoding="utf-8") as f:
            prog = json.load(f)
    except (OSError, ValueError):
        prog = {"done": {}, "empty": {}}
    log = {"fetched": 0, "requests_before": client.used, "stopped": None}
    uni = load_rows("universe", out_dir)
    if not uni:
        uni = parse_universe(client.get("TaiwanStockInfo"))
        if len(uni) < 1000:
            log["stopped"] = "股票清單只有 %d 檔,格式可能改了" % len(uni)
            print(json.dumps(log, ensure_ascii=False))
            return log
        save_rows("universe", uni, out_dir)
    data = {}
    for k in DATASETS:                                        # {資料集: {代號: [列]}}(逐檔替換,避免每檔掃整張表)
        data[k] = {}
        for r in load_rows(k, out_dir):
            data[k].setdefault(r[0], []).append(r)
    todo = [u[0] for u in uni if set(prog["done"].get(u[0], [])) != set(DATASETS)]
    log["todo_before"] = len(todo)

    def flush():
        for k in DATASETS:
            save_rows(k, [r for c in sorted(data[k]) for r in data[k][c]], out_dir)
        with open(pp, "w", encoding="utf-8") as f:
            json.dump(prog, f)

    try:
        for i, code in enumerate(todo):
            if (clock() - t0) / 60 > max_minutes:
                log["stopped"] = "時間上限"
                break
            done = set(prog["done"].get(code, []))
            for k, ds, parse in (("rev", "TaiwanStockMonthRevenue", parse_rev), ("fin", "TaiwanStockFinancialStatements", parse_fin),
                                 ("cf", "TaiwanStockCashFlowsStatement", parse_cf)):
                if k in done:
                    continue
                rows = client.get(ds, code, START)
                if rows is None:
                    continue                                  # 這次失敗,下次再試
                parsed = parse(code, rows)
                data[k][code] = parsed
                done.add(k)
                if not parsed:
                    prog["empty"].setdefault(k, []).append(code)
            prog["done"][code] = sorted(done)
            log["fetched"] += 1
            if (i + 1) % save_every == 0:
                flush()
    except Quota as e:
        log["stopped"] = str(e)
    flush()
    log["requests"] = client.used - log.pop("requests_before")
    log["complete"] = sum(1 for u in uni if set(prog["done"].get(u[0], [])) == set(DATASETS))
    log["universe"] = len(uni)
    print(json.dumps(log, ensure_ascii=False))
    return log


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-minutes", type=float, default=330)
    ap.add_argument("--gap", type=float, default=8.0)
    a = ap.parse_args()
    tok = os.environ.get("FINMIND_TOKEN", "").strip()
    if not tok:
        print("缺 FINMIND_TOKEN,停止")
        return 2
    run(Client(tok, gap=a.gap), max_minutes=a.max_minutes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
