# -*- coding: utf-8 -*-
"""
consensus.py — 共識頁每日計算(2026-11-04,Phase 2)
============================================================
只讀 repo 既有資料,不另外抓網路:
  margin_hist/(融資張數、收盤價、指數;margin_backfill.py / margin_share.py)
  taifex_hist/(期交所三大法人期貨 / 選擇權、未平倉、Put/Call、大額交易人;taifex_hist.py)
  insti_daily/(三大法人個股買賣超;Apps Script 推送)
  chip_structure_full.json(集保大戶比重週變化;chip_structure_build.py)
  margin_share_classes.json(產業別 / ETF;margin_share.py)
輸出 consensus.json(≤ 45,000 字元;失敗保留舊檔)。門檻在 consensus_core.CONSENSUS_CFG。
"""
import csv
import datetime as dt
import glob
import json
import os
import sys

import consensus_core as cc
import margin_backfill as mb
import margin_share_core as ms

OUT = "consensus.json"
SIZE_LIMIT = 45000
TPE = dt.timezone(dt.timedelta(hours=8))
SERIES_DAYS = 60


def read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def num(x):
    try:
        return float(x) if x not in (None, "") else None
    except ValueError:
        return None


# ------------------------------------------------------------------ 載入
def load_margin(d="margin_hist"):
    """→ {mkt: {date: {code: (lots, close)}}}(只留近 300 天,夠算 20 日與 A 的歷史)"""
    data = {"twse": {}, "tpex": {}}
    for f in sorted(glob.glob(os.path.join(d, "2*.csv.gz"))):
        for r in mb.read_csv(f, gz=True):
            data.setdefault(r["market"], {}).setdefault(r["date"], {})[r["code"]] = (num(r["margin_lots"]), num(r["close"]))
    return data


def load_index(d="margin_hist"):
    out = {}
    for r in read_csv(os.path.join(d, "index.csv")):
        out.setdefault(r["market"], {})[r["date"]] = num(r["close"])
    return out


def load_taifex(d="taifex_hist"):
    fut = {}
    for r in read_csv(os.path.join(d, "fut_inst.csv")):
        fut.setdefault(r["date"], {})[(r["contract"], r["who"])] = r
    # 尚未公布(全部 0)的日子不用(taifex_hist.py 之後重抓會清掉)
    blank = lambda rows: all(num(r["long_oi"]) in (0, None) and num(r["short_oi"]) in (0, None) for r in rows)
    fut = {k: v for k, v in fut.items() if not blank(v.values())}
    oi = {(r["date"], r["contract"]): num(r["total_oi"]) for r in read_csv(os.path.join(d, "fut_oi.csv"))}
    opt = {}
    for r in read_csv(os.path.join(d, "opt_inst.csv")):
        opt.setdefault(r["date"], {})[(r["cp"], r["who"])] = r
    opt = {k: v for k, v in opt.items() if not blank(v.values())}
    pcr = {r["date"]: r for r in read_csv(os.path.join(d, "pcr.csv"))}
    large = {}
    for r in read_csv(os.path.join(d, "large.csv")):
        if r["contract"] == "TX" and r["month"] == "999999" and r["trader_type"] == "0":
            large[r["date"]] = r
    return fut, oi, opt, pcr, large


def load_insti(d="insti_daily"):
    """→ {date: {mkt: {code: [f, t, d, n]}}}(單位股)"""
    out = {}
    for f in sorted(glob.glob(os.path.join(d, "2*.json"))):
        try:
            with open(f, encoding="utf-8") as fh:
                j = json.load(fh)
        except (OSError, ValueError):
            continue
        ds = str(j.get("date") or os.path.basename(f)[:8])
        out["%s-%s-%s" % (ds[:4], ds[4:6], ds[6:8])] = {m: j.get(m) or {} for m in ("twse", "tpex")}
    return out


# ------------------------------------------------------------------ A 市場氛圍
def _series_from(dates, fn):
    return [(d, fn(d)) for d in dates]


def mood(margin, index, taifex, insti, cls):
    fut, oi, opt, pcr, large = taifex
    tdates = sorted(fut)
    mdates = sorted(set(margin.get("twse", {})) & set(margin.get("tpex", {})))
    ind = []

    def add(key, label, unit, direction, series, note=""):
        series = [(d, v) for d, v in series if v is not None]
        if not series:
            return
        d, v = series[-1]
        p = cc.pctile(v, [x for _, x in series[:-1]])
        ind.append({"key": key, "label": label, "unit": unit, "dir": direction, "date": d, "value": v,
                    "pctile": p, "zone": cc.zone(p, direction), "n_hist": len(series) - 1, "note": note,
                    "spark": [x for _, x in series[-SERIES_DAYS:]]})

    def inst_sum(d, c):
        rows = fut.get(d, {})
        vals = [num(rows[k]["net_oi"]) for k in rows if k[0] == c]
        return sum(v for v in vals if v is not None) if vals else None

    add("retail_tmf", "微型臺指 散戶淨多單比", "%", +1,
        _series_from(tdates, lambda d: cc.retail_net_pct(inst_sum(d, "TMF"), oi.get((d, "TMF")))),
        "−三大法人淨未平倉 ÷ 全市場未平倉;越高 = 散戶越偏多")
    add("retail_mtx", "小型臺指 散戶淨多單比", "%", +1,
        _series_from(tdates, lambda d: cc.retail_net_pct(inst_sum(d, "MTX"), oi.get((d, "MTX")))))

    def fx_tx(d):
        r = fut.get(d, {}).get(("TX", "外資及陸資"))
        return num(r["net_oi"]) if r else None
    add("foreign_tx", "外資 臺指期淨未平倉", "口", 0, _series_from(tdates, fx_tx), "正 = 淨多單、負 = 淨空單")

    def fx_tx_chg(d):
        i = tdates.index(d)
        if i < 1:
            return None
        a, b = fx_tx(tdates[i - 1]), fx_tx(d)
        return b - a if a is not None and b is not None else None
    add("foreign_tx_chg", "外資 臺指期淨未平倉 單日增減", "口", 0, _series_from(tdates, fx_tx_chg))

    def fx_opt(d):
        rows = opt.get(d, {})
        c, p = rows.get(("CALL", "外資及陸資")), rows.get(("PUT", "外資及陸資"))
        if not c or not p:
            return None
        a, b = num(c["net_oi_k"]), num(p["net_oi_k"])
        return round((a - b) / 1e5, 2) if a is not None and b is not None else None   # 千元 → 億元
    add("foreign_txo", "外資 選擇權 買權淨額 − 賣權淨額", "億元", 0, _series_from(tdates, fx_opt),
        "未平倉契約金額;正 = 偏多部位、負 = 偏空部位(買權與賣權方向的粗略合計)")

    add("pcr_oi", "臺指選擇權 Put/Call 未平倉比", "%", 0,
        _series_from(sorted(pcr), lambda d: num(pcr[d]["pc_oi_pct"])))

    def top10(d):
        r = large.get(d)
        if not r or not num(r["market_oi"]):
            return None
        return round((num(r["top10_buy"]) - num(r["top10_sell"])) / num(r["market_oi"]) * 100, 2)
    add("top10_tx", "臺指期 前十大交易人淨部位", "% of OI", 0, _series_from(sorted(large), top10), "所有到期月份合計")

    def lots_total(d):
        a = [sum(l for l, _ in margin[m][d].values() if l) for m in ("twse", "tpex")]
        return sum(a)
    totals = [(d, lots_total(d)) for d in mdates]
    chg20 = [(totals[i][0], cc.pct(totals[i][1], totals[i - 20][1])) for i in range(20, len(totals))]
    add("margin_20d", "融資總張數 20 日變化", "%", +1, chg20, "上市 + 上櫃")

    etf = set(cls["twse"]["etf"])
    names = cls["twse"]["names"]

    def etf_share(d, kind):
        rows = margin["twse"][d]
        tot = sum(l * p for l, p in rows.values() if l and p)
        sub = sum(l * p for c, (l, p) in rows.items() if l and p and c in etf and ms.etf_type(c, names.get(c, "")) == kind)
        return round(sub / tot * 100, 3) if tot else None
    add("lev_etf", "槓桿型 ETF 融資占比(上市)", "%", +1, _series_from(mdates, lambda d: etf_share(d, "槓桿")))
    add("inv_etf", "反向型 ETF 融資占比(上市)", "%", -1, _series_from(mdates, lambda d: etf_share(d, "反向")),
        "越高 = 押空的槓桿資金越多")

    idx = index.get("twse", {})
    idates = sorted(idx)
    add("drawdown", "加權指數 距 20 日高點", "%", +1,
        [(idates[i], cc.drawdown([idx[x] for x in idates[:i + 1]], 20)) for i in range(len(idates))], "越低 = 回檔越深")

    def breadth(i):
        if i < 5:
            return None
        a, b = margin["twse"][mdates[i - 5]], margin["twse"][mdates[i]]
        ups = [1 if b[c][1] > a[c][1] else 0 for c in b if c in a and a[c][1] and b[c][1] and c not in etf]
        return round(sum(ups) / len(ups) * 100, 1) if ups else None
    add("breadth5", "上市 5 日上漲家數比", "%", +1, [(mdates[i], breadth(i)) for i in range(len(mdates))], "有融資資料的股票")

    # 三大法人現貨合計(insti_daily 只有近期,先不給百分位)
    idays = sorted(insti)

    def inst_total(d):
        tot = 0.0
        for m in ("twse", "tpex"):
            closes = margin.get(m, {}).get(d)
            if not closes:
                return None
            for c, v in (insti[d].get(m) or {}).items():
                p = closes.get(c, (None, None))[1]
                if p and v and v[3] is not None:
                    tot += v[3] * p
        return round(tot / 1e8, 1)
    add("inst_cash", "三大法人 現貨買賣超(估算)", "億元", 0, [(d, inst_total(d)) for d in idays], "股數 × 收盤價")

    label, hot, cold = cc.mood_label([x["zone"] for x in ind])
    return {"label": label, "hot": hot, "cold": cold, "indicators": ind}


# ------------------------------------------------------------------ B / C 個股
def window_dates(margin, insti, n):
    mdates = sorted(set(margin["twse"]) & set(margin["tpex"]))
    idays = [d for d in sorted(insti) if d in set(mdates)]
    if len(idays) < n:
        return None, None, mdates
    t = idays[-1]
    i = mdates.index(t)
    if i < n:
        return None, None, mdates
    return t, mdates[i - n], mdates


def stock_rows(margin, insti, cls, chips, n):
    t, t0, mdates = window_dates(margin, insti, n)
    if not t:
        return None
    wdays = mdates[mdates.index(t0) + 1: mdates.index(t) + 1]
    rows = []
    for m in ("twse", "tpex"):
        cur, prev = margin[m][t], margin[m][t0]
        ind, etf, names = cls[m]["industry"], set(cls[m]["etf"]), cls[m]["names"]
        hist = [margin[m][d] for d in mdates[max(0, mdates.index(t) - cc.CONSENSUS_CFG["c_hi_days"] + 1): mdates.index(t) + 1]]
        for c, (l1, p1) in cur.items():
            l0 = prev.get(c, (None, None))[0]
            if l1 is None or l0 is None or not p1:
                continue
            inst_n = inst_f = 0.0
            ok = True
            for d in wdays:
                day = insti.get(d, {}).get(m) or {}
                if not day:                    # 這天這個市場整份沒有 → 不算
                    ok = False
                    break
                v = day.get(c) or [0, 0, 0, 0]  # 當天不在法人名單 = 0 股(同籌碼結構的做法)
                if v[3] is None:
                    ok = False
                    break
                inst_n += v[3]
                inst_f += v[0] or 0
            closes = [h.get(c, (None, None))[1] for h in hist]
            p0 = prev.get(c, (None, None))[1]
            ch = (chips or {}).get(c) or {}
            d1 = ch.get("d1")
            rows.append({"code": c, "name": names.get(c, ""), "mkt": m, "grp": ms.industry_of(c, ind, etf),
                         "close": p1, "ret": cc.pct(p1, p0), "d_lots": int(l1 - l0), "d_pct": cc.pct(l1, l0) if l0 else None,
                         "lots": int(l1), "inst": inst_n * p1 if ok else None, "foreign": inst_f * p1 if ok else None,
                         "dd": cc.drawdown(closes, cc.CONSENSUS_CFG["c_hi_days"]) if None not in closes else None,
                         "big_pp": d1[0] if d1 else None, "big_d4": (ch.get("d4") or [None])[0], "fl": ch.get("fl") or []})
    return {"t": t, "t0": t0, "days": wdays, "rows": rows}


def _fmt(r, keys):
    e8 = lambda v: None if v is None else round(v / 1e8, 2)
    m = {"code": r["code"], "name": r["name"], "mkt": "上市" if r["mkt"] == "twse" else "上櫃", "grp": r["grp"],
         "close": r["close"], "ret": r["ret"], "d_lots": r["d_lots"], "d_pct": r["d_pct"], "inst_e8": e8(r["inst"]),
         "foreign_e8": e8(r["foreign"]), "dd": r["dd"], "big_pp": r["big_pp"], "big_d4": r["big_d4"]}
    return [m[k] for k in keys]


B_KEYS = ["code", "name", "mkt", "grp", "close", "ret", "d_lots", "d_pct", "inst_e8", "foreign_e8"]
C_KEYS = ["code", "name", "mkt", "grp", "close", "dd", "d_lots", "d_pct", "big_pp", "big_d4", "inst_e8"]


def leverage_inst(sr, top):
    cfg = cc.CONSENSUS_CFG
    cats = {"both_up": [], "both_down": [], "margin_up_inst_sell": [], "margin_down_inst_buy": []}
    n_no_inst = 0
    for r in sr["rows"]:
        if r["inst"] is None:
            n_no_inst += 1
            continue
        k = cc.classify_b(r["d_lots"], r["d_pct"], r["inst"])
        if k:
            cats[k].append(r)
    out = {k: [_fmt(r, B_KEYS) for r in sorted(v, key=lambda r: -abs(r["inst"]))[:top]] for k, v in cats.items()}
    counts = {k: len(v) for k, v in cats.items()}
    # 產業層級
    groups = {}
    for r in sr["rows"]:
        g = groups.setdefault((r["mkt"], r["grp"]), {"d_lots": 0, "lots0": 0, "inst": 0.0, "n": 0})
        g["d_lots"] += r["d_lots"]
        g["lots0"] += r["lots"] - r["d_lots"]
        if r["inst"] is not None:
            g["inst"] += r["inst"]
        g["n"] += 1
    grp_rows = []
    for (m, gname), g in groups.items():
        dp = round(g["d_lots"] / g["lots0"] * 100, 2) if g["lots0"] else None
        same = None
        if dp is not None and abs(dp) >= 1 and abs(g["inst"]) >= cfg["b_min_inst"] * 5:
            same = "同向" if (dp > 0) == (g["inst"] > 0) else "反向"
        grp_rows.append(["上市" if m == "twse" else "上櫃", gname, g["n"], g["d_lots"], dp, round(g["inst"] / 1e8, 2), same])
    grp_rows.sort(key=lambda x: -abs(x[5]))
    return {"lists": out, "counts": counts, "no_inst": n_no_inst, "groups": grp_rows}


def pullback(sr, chips_meta, top):
    hits = [r for r in sr["rows"] if cc.passes_c(r["dd"], r["d_lots"], r["d_pct"], r["big_pp"], r["fl"])]
    hits.sort(key=lambda r: (-r["big_pp"], r["dd"]))
    return {"items": [_fmt(r, C_KEYS) for r in hits[:top]], "count": len(hits),
            "tdcc_weeks": (chips_meta or {}).get("weeks"), "tdcc_asof": (chips_meta or {}).get("tdcc_asof"),
            "low_confidence": len((chips_meta or {}).get("weeks") or []) < 4}


# ------------------------------------------------------------------ 主流程
def build(base="."):
    j = lambda p: os.path.join(base, p)
    with open(j("margin_share_classes.json"), encoding="utf-8") as f:
        cls = json.load(f)
    margin = load_margin(j("margin_hist"))
    index = load_index(j("margin_hist"))
    taifex = load_taifex(j("taifex_hist"))
    insti = load_insti(j("insti_daily"))
    chips_doc = {}
    if os.path.exists(j("chip_structure_full.json")):
        with open(j("chip_structure_full.json"), encoding="utf-8") as f:
            chips_doc = json.load(f)
    chips = chips_doc.get("stocks") or {}
    cfg = cc.CONSENSUS_CFG
    out = {"generated": dt.datetime.now(TPE).isoformat(timespec="seconds"), "cfg": cfg, "notes": cc.NOTES}
    out["mood"] = mood(margin, index, taifex, insti, cls)
    sr = stock_rows(margin, insti, cls, chips, cfg["window"])
    if sr:
        out["window"] = {"t": sr["t"], "t0": sr["t0"], "days": sr["days"]}
        out["fields_b"], out["fields_c"] = B_KEYS, C_KEYS
        for top in (cfg["b_top"], 12, 8):
            out["lev_inst"] = leverage_inst(sr, top)
            out["pullback"] = pullback(sr, chips_doc, max(top * 2, 16))
            s = json.dumps(out, ensure_ascii=False, separators=(",", ":"))
            if len(s) <= SIZE_LIMIT:
                break
    else:
        out["window"] = None
        out["window_note"] = "三大法人個股資料不足 %d 個交易日" % cfg["window"]
    return out


def main():
    try:
        out = build()
    except Exception as e:
        print("共識計算失敗,保留舊檔:%r" % e)
        return 1
    s = json.dumps(out, ensure_ascii=False, separators=(",", ":"))
    if len(s) > SIZE_LIMIT:
        print("consensus.json %d 字超過上限,保留舊檔" % len(s))
        return 1
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(s)
    os.replace(tmp, OUT)
    print(json.dumps({"size": len(s), "mood": out["mood"]["label"], "window": out.get("window")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
