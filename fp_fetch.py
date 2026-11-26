# -*- coding: utf-8 -*-
"""
fp_fetch.py — 外資期權綜合判讀:TWSE 外資現貨與持股資料(v2026-11-26-init)
============================================================
規格 D5、D6(2026-11-26 探測確認):
  D5 外資現貨買賣超金額:TWSE BFI82U(GET /rwd/zh/fund/BFI82U?type=day&dayDate=YYYYMMDD&response=json)
     取「外資及陸資(不含外資自營商)」的買賣差額(元)。只能逐日查詢。
  D6 外資持股市值:TWSE MI_QFIIS(GET /rwd/zh/fund/MI_QFIIS?response=json&date=YYYYMMDD&selectType=ALLBUT0999)
     「全體外資及陸資持有股數」× 當日收盤價(margin_hist 的上市個股收盤)加總。
     歷史回補只抓每月最後一個交易日的快照(約 25 次請求);之後每天抓最新一天。
     判讀時:某日持股市值 ≈ 最近一次快照市值 × 加權指數(當日 ÷ 快照日)。
交易日曆:margin_hist 的上市日期(官方),加上「今天」(15:00 後且為平日)。
抓取規則:先讀 robots.txt、同主機間隔 ≥ 3 秒、誠實 User-Agent;403 / 429 / 驗證碼 → 停止。
抓不到 → 不寫該日(不用前一日補)。輸出 fp_data/foreign_spot.csv、fp_data/foreign_hold.csv。
"""
import csv
import datetime as dt
import json
import os
import sys

VERSION = "v2026-11-26-init"
OUT = "fp_data"
BFI = "https://www.twse.com.tw/rwd/zh/fund/BFI82U?type=day&dayDate=%s&response=json"
QFII = "https://www.twse.com.tw/rwd/zh/fund/MI_QFIIS?response=json&date=%s&selectType=ALLBUT0999"
SPOT_DAYS = 300          # 規格:先回補 260 個交易日;多抓一些讓 5 日累計與 252 日分位都夠
HOLD_MONTHS = 26
FOREIGN_ROW = "外資及陸資(不含外資自營商)"
TPE = dt.timezone(dt.timedelta(hours=8))


def num(x):
    try:
        return float(str(x).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def read(path):
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8", newline="") as f:
        return {r["date"]: r for r in csv.DictReader(f)}


def write(path, header, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=header, lineterminator="\n")
        w.writeheader()
        for k in sorted(rows):
            w.writerow({h: rows[k].get(h, "") for h in header})
    os.replace(tmp, path)


def parse_bfi(j):
    """BFI82U JSON → 外資買賣差額(元);非交易日或格式不符 → None"""
    if not j or j.get("stat") != "OK":
        return None
    for r in j.get("data") or []:
        if r and str(r[0]).strip() == FOREIGN_ROW:
            return num(r[3])
    raise ValueError("BFI82U 找不到「%s」列" % FOREIGN_ROW)


def parse_qfii(j):
    """MI_QFIIS JSON → {代號: 外資持有股數}"""
    if not j or j.get("stat") != "OK":
        return None
    f = j.get("fields") or []
    try:
        ic, ih = f.index("證券代號"), f.index("全體外資及陸資持有股數")
    except ValueError:
        raise ValueError("MI_QFIIS 欄位改變:%s" % f)
    out = {}
    for r in j.get("data") or []:
        v = num(r[ih])
        if v is not None:
            out[str(r[ic]).strip()] = v
    return out


def hold_value(shares, closes):
    """Σ 外資持股 × 收盤價(元)→ (市值, 有價格的檔數)"""
    tot, n = 0.0, 0
    for c, s in shares.items():
        px = closes.get(c)
        if px:
            tot += s * px
            n += 1
    return tot, n


def trading_days(margin, now):
    days = sorted(margin.get("twse", {}))
    t = now.date().isoformat()
    if now.weekday() < 5 and now.hour >= 15 and t not in days:
        days.append(t)
    return days


def month_ends(days):
    out = {}
    for d in days:
        out[d[:7]] = d
    return sorted(out.values())


def run(fetcher=None, base=".", now=None, margin=None):
    import consensus as C
    import mood_leverage as L
    now = now or dt.datetime.now(TPE)
    f = fetcher or L.Fetcher()
    margin = margin if margin is not None else C.load_margin(os.path.join(base, "margin_hist"))
    days = trading_days(margin, now)
    sp_path, hd_path = os.path.join(base, OUT, "foreign_spot.csv"), os.path.join(base, OUT, "foreign_hold.csv")
    spot, hold = read(sp_path), read(hd_path)
    log = {"version": VERSION, "at": now.isoformat(timespec="seconds"), "spot_added": 0, "hold_added": 0, "errors": []}

    want = [d for d in days[-SPOT_DAYS:] if d not in spot]
    for d in want:
        try:
            v = parse_bfi(f.get(BFI % d.replace("-", "")).json())
        except Exception as e:                       # noqa: BLE001
            log["errors"].append("BFI82U %s:%s" % (d, str(e)[:120]))
            if "被擋" in str(e) or "robots" in str(e):
                break
            continue
        if v is None:
            continue
        spot[d] = {"date": d, "foreign_net": "%.0f" % v}
        log["spot_added"] += 1
    write(sp_path, ["date", "foreign_net"], spot)

    snaps = month_ends(days)[-HOLD_MONTHS:]
    if days and days[-1] not in snaps:
        snaps.append(days[-1])
    for d in [x for x in snaps if x not in hold]:
        closes = {c: v[1] for c, v in (margin.get("twse", {}).get(d) or {}).items() if v and v[1]}
        if not closes:
            continue                                 # 今天的收盤還沒進 margin_hist → 下次再抓
        try:
            sh = parse_qfii(f.get(QFII % d.replace("-", "")).json())
        except Exception as e:                       # noqa: BLE001
            log["errors"].append("MI_QFIIS %s:%s" % (d, str(e)[:120]))
            if "被擋" in str(e) or "robots" in str(e):
                break
            continue
        if not sh:
            continue
        v, n = hold_value(sh, closes)
        hold[d] = {"date": d, "value": "%.0f" % v, "n_priced": n, "n_held": len(sh)}
        log["hold_added"] += 1
    write(hd_path, ["date", "value", "n_priced", "n_held"], hold)
    log["spot_rows"], log["hold_rows"] = len(spot), len(hold)
    print(json.dumps(log, ensure_ascii=False))
    return 0 if not log["errors"] else 2


if __name__ == "__main__":
    sys.exit(run())
