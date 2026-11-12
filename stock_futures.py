# -*- coding: utf-8 -*-
"""
stock_futures.py — 個股期貨未平倉(依標的股票加總)每日歷史(2026-11-12)
============================================================
用途:題材擁擠度的「股期籌碼」—— 每檔股票的個股期貨未平倉,跟自己歷史比的分位(theme_page_build 取題材中位數)。
來源(stock_futures_probe.py 2026-11-12 探測):
  契約 ↔ 股票:期交所 stockLists 頁(商品代碼前 2 碼、證券代號、標準型每口股數)
  每日未沖銷:futDataDown(down_type=1、commodity_id=all,一次一個月),只取「一般」交易時段、非價差列
股數換算:契約代碼第 3 碼為 F → 標準型(每口股數取 stockLists);為數字 → 小型(每口 100 股)。
  ※ 小型契約的判斷是依代碼規則推定,log 會列出各類契約數量供核對。
三大法人只有「股票期貨」合計,沒有分個股,不使用。
輸出 stock_futures/oi.csv.gz:date, code, oi_shares;progress.json 記錄已完成月份(當月與上月每次重抓)。
規則同 taifex_hist.py:robots.txt、同一主機間隔 ≥ 3 秒、誠實 User-Agent;403 / 429 / HTML 錯誤頁 → 停止,保留已存資料。
"""
import csv
import datetime as dt
import gzip
import io
import json
import os
import re
import sys

import taifex_hist as TH

OUT_DIR = "stock_futures"
LISTS_URL = "https://www.taifex.com.tw/cht/2/stockLists"
START = (2024, 8)
MINI_UNIT = 100


def parse_stock_lists(html):
    """→ {商品代碼前 2 碼: (證券代號, 標準型每口股數)}"""
    out = {}
    for row in re.findall(r"(?is)<tr[^>]*>(.*?)</tr>", html):
        c = [re.sub(r"<[^>]+>|\s+", " ", x).strip() for x in re.findall(r"(?is)<t[dh][^>]*>(.*?)</t[dh]>", row)]
        if len(c) < 12 or not re.fullmatch(r"[A-Z0-9]{2}", c[0] or "") or not re.fullmatch(r"\d{4,6}[A-Z]?", c[2] or ""):
            continue
        if "股票期貨" not in c[4]:
            continue
        try:
            unit = int(c[11].replace(",", ""))
        except ValueError:
            continue
        out[c[0]] = (c[2], unit)
    return out


def contract_shares(contract, lists):
    """契約代碼 → (股票代號, 每口股數) 或 None"""
    base, kind = contract[:2], contract[2:3]
    if base not in lists or len(contract) != 3:
        return None
    code, unit = lists[base]
    if kind == "F":
        return code, unit
    if kind.isdigit():
        return code, MINI_UNIT
    return None


def parse_fut_all(text, lists):
    """futDataDown CSV → ({(date, code): oi_shares}, {類別: 契約數})"""
    agg, kinds = {}, {"standard": set(), "mini": set(), "other": set()}
    for r in csv.reader(io.StringIO(text)):
        if len(r) < 18 or r[0] in ("交易日期", "") or "/" in r[2] or r[17].strip() != "一般":
            continue
        cs = contract_shares(r[1].strip(), lists)
        if not cs:
            kinds["other"].add(r[1].strip())
            continue
        try:
            oi = float(r[11].replace(",", "") or 0)
        except ValueError:
            continue
        kinds["standard" if r[1].strip()[2] == "F" else "mini"].add(r[1].strip())
        d = r[0].replace("/", "-")
        agg[(d, cs[0])] = agg.get((d, cs[0]), 0) + oi * cs[1]
    return agg, {k: len(v) for k, v in kinds.items()}


def load_store(path):
    out = {}
    if os.path.exists(path):
        with gzip.open(path, "rt", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                out[(r["date"], r["code"])] = float(r["oi_shares"])
    return out


def save_store(path, store):
    tmp = path + ".tmp"
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date", "code", "oi_shares"])
        for (d, c), v in sorted(store.items()):
            w.writerow([d, c, int(v)])
    os.replace(tmp, path)


def run(http=None, out_dir=OUT_DIR, now=None, max_months=None):
    http = http or TH.Http()
    now = now or dt.datetime.now(TH.TPE)
    today = TH.last_available(now)
    os.makedirs(out_dir, exist_ok=True)
    pp = os.path.join(out_dir, "progress.json")
    try:
        with open(pp, encoding="utf-8") as f:
            prog = json.load(f)
    except (OSError, ValueError):
        prog = {"done": []}
    log = {"started": now.isoformat(timespec="seconds"), "fetched": [], "rows": 0, "stopped": None, "kinds": {}}
    store_path = os.path.join(out_dir, "oi.csv.gz")
    store = load_store(store_path)
    try:
        if not http._robots_ok(LISTS_URL):
            raise TH.Blocked("robots.txt 不允許:" + LISTS_URL)
        r = http._raw("GET", LISTS_URL)
        if r.status_code != 200:
            raise TH.Blocked("stockLists HTTP %s" % r.status_code)
        lists = parse_stock_lists(r.text)
        if len(lists) < 100:
            raise TH.Blocked("stockLists 對照只有 %d 筆,格式可能改了" % len(lists))
        log["n_underlyings"] = len(lists)
        y, m = START
        months = []
        while (y, m) <= (today.year, today.month):
            months.append((y, m))
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        recent = set(months[-2:])
        todo = [x for x in months if "%04d-%02d" % x not in prog["done"] or x in recent]
        if max_months:
            todo = todo[:max_months]
        for y, m in todo:
            a, b = TH.month_range(y, m, today)
            text = http.post_csv(TH.BASE + "futDataDown", {"down_type": "1", "commodity_id": "all",
                                                           "queryStartDate": a.strftime("%Y/%m/%d"),
                                                           "queryEndDate": b.strftime("%Y/%m/%d")})
            agg, kinds = parse_fut_all(text, lists)
            for k, v in kinds.items():
                log["kinds"][k] = max(log["kinds"].get(k, 0), v)
            key = "%04d-%02d" % (y, m)
            store = {k: v for k, v in store.items() if not k[0].startswith(key)}
            store.update(agg)
            log["rows"] += len(agg)
            log["fetched"].append([key, len({d for d, _ in agg})])
            if key not in prog["done"] and agg:
                prog["done"].append(key)
            save_store(store_path, store)
            with open(pp, "w", encoding="utf-8") as f:
                json.dump(prog, f)
    except TH.Blocked as e:
        log["stopped"] = str(e)
    log["requests"] = http.count
    log["n_days"] = len({d for d, _ in store})
    print(json.dumps(log, ensure_ascii=False))
    return 2 if log["stopped"] and not log["fetched"] else 0


def main():
    mm = None
    if len(sys.argv) > 1 and sys.argv[1].startswith("--max-months="):
        mm = int(sys.argv[1].split("=", 1)[1])
    return run(max_months=mm)


if __name__ == "__main__":
    sys.exit(main())
