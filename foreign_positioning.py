# -*- coding: utf-8 -*-
"""
foreign_positioning.py — 外資期權綜合判讀(ForeignPositioning v2026-11-26-init)
============================================================
規格:「外資期權綜合判讀(ForeignPositioning.gs)→ 市場氛圍卡片」(2026-11-26)。
目的:外資台指期淨空單常是現貨避險或期權套利 → 拆成 避險 / 套利 / 方向性,只拿方向性投票。
與規格不同處(以現有架構為準,使用者 2026-11-26 同意):
  - 用 Python(GitHub Actions)而非 GAS:市場氛圍全在 consensus.py 算、GAS 只讀,投票邏輯只有一份。
    結果併入 consensus.json 的 mood.foreignPositioning;GAS 的 ?data=1 另外附上 marketMood.foreignPositioning。
  - 每日歷史存 fp_history.csv(取代 sheet fp_history)。排程跟著 taifex-hist.yml(16:40、21:10 備援)
    與每小時資料新鮮度檢查,不另建一次性觸發器。
  - D6 外資持股市值:歷史用每月快照 × 加權指數比例推估(fp_fetch.py),卡片註明。
  - v_retail:規格「已有散戶邏輯就直接用它的輸出」→ 用共識頁「小型臺指 散戶淨多單比」的極端區
    (偏熱 → −1、過度看空 → +1;門檻為共識頁的 90 / 10)。
  - 進整體投票:分數 ≤ −2 → 一票偏熱(風險)邊、≥ +2 → 一票過度看空(反彈)邊(FP_CONFIG.vote_threshold)。
資料:taifex_hist/(fut_inst、opt_inst 含金額、fut_oi、fut_px 近月結算價)、margin_hist/index.csv(加權收盤,官方)、
     fp_data/foreign_spot.csv(BFI82U)、fp_data/foreign_hold.csv(MI_QFIIS)。缺資料的欄位為 None,不用前一日補。
百分位:過去 252 個交易日(不含當日)的中位排名,樣本 < min_hist 不給。全部門檻在 FP_CONFIG。只描述,不構成買賣建議。
"""
import bisect
import csv
import datetime as dt
import math
import os

import consensus_core as cc

VERSION = "v2026-11-26-init"
FOREIGN = "外資及陸資"
FP_CONFIG = {
    "pct_window": 252, "min_hist": 120,
    "arb_ratio": 0.5, "basis_pct": 80, "div_months": [6, 7, 8, 9],
    "hedge_pct": 80,
    "dF5_bear_pct": 20, "dF5_bull_pct": 80,
    "fut_center": 50, "fut_step": 25,
    "spot_pct": 60,
    "vote_threshold": 2,              # |score| ≥ 2 才進整體投票
    "mtx_per_tx": 4, "tmf_per_tx": 20, "multiplier_tx": 200, "multiplier_opt": 50,
    "bt_horizons": [5, 20], "bt_min_n": 30,
}
LABEL_ZH = {"ARBITRAGE": "套利主導", "HEDGE": "避險型空單", "BEAR_CONFIRM": "偏空確認", "BULL_CONFIRM": "偏多確認",
            "DIVERGE": "分歧"}
HIST_COLS = ["date", "F_net", "dF_1d", "dF_5d", "C_net", "P_net", "S_syn", "A_arb", "arb_ratio", "F_dir", "C_res", "P_res",
             "opt_dir", "avg_prem_call", "avg_prem_put", "settle", "taiex", "basis", "div_season", "hold_value",
             "hedge_ratio", "spot_net", "spot_5d", "retail_ratio", "dF_5d_pct", "F_dir_pct", "basis_pct_rank", "hedge_pct",
             "spot_abs_pct", "retail_pct", "retail_zone", "label", "v_fut", "v_spot", "v_opt", "v_retail", "score",
             "ret5", "ret20"]


# ------------------------------------------------------------------ 共用
def num(x):
    if x is None or x == "":
        return None
    try:
        return float(str(x).replace(",", ""))
    except ValueError:
        return None


def read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def sign(x):
    return 0 if not x else (1 if x > 0 else -1)


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def js_round(x):
    """規格的 round 為 JavaScript Math.round(0.5 進位)"""
    return int(math.floor(x + 0.5))


def rolling_pct(values, cfg=FP_CONFIG):
    """每天的值在「前 252 個交易日(不含當日)」中的中位排名百分位;None 不計"""
    out = []
    for i, v in enumerate(values):
        past = [x for x in values[max(0, i - cfg["pct_window"]):i] if x is not None]
        if v is None or len(past) < cfg["min_hist"]:
            out.append(None)
            continue
        past.sort()
        lo, hi = bisect.bisect_left(past, v), bisect.bisect_right(past, v)
        out.append(round((lo + hi) / 2 / len(past) * 100, 1))
    return out


def expanding_pct(values, min_n):
    """與共識頁 mood() 相同:對「之前全部」歷史的百分位(散戶淨多單比沿用共識頁算法)"""
    out = []
    for i, v in enumerate(values):
        past = [x for x in values[:i] if x is not None]
        out.append(cc.pctile(v, past) if v is not None and len(past) >= min_n else None)
    return out


# ------------------------------------------------------------------ 讀資料
def load(base="."):
    j = lambda *p: os.path.join(base, *p)
    fut = {}
    for r in read_csv(j("taifex_hist", "fut_inst.csv")):         # 全部身份別(散戶 = −三大法人合計)
        fut.setdefault(r["date"], {})[(r["contract"], r["who"])] = r
    opt = {}
    for r in read_csv(j("taifex_hist", "opt_inst.csv")):
        if r["who"] == FOREIGN:
            opt.setdefault(r["date"], {})[r["cp"]] = r
    oi = {(r["date"], r["contract"]): num(r["total_oi"]) for r in read_csv(j("taifex_hist", "fut_oi.csv"))}
    px = {r["date"]: num(r["settle"]) for r in read_csv(j("taifex_hist", "fut_px.csv")) if r["contract"] == "TX"}
    idx = {r["date"]: num(r["close"]) for r in read_csv(j("margin_hist", "index.csv")) if r["market"] == "twse"}
    spot = {r["date"]: num(r["foreign_net"]) for r in read_csv(j("fp_data", "foreign_spot.csv"))}
    hold = sorted((r["date"], num(r["value"])) for r in read_csv(j("fp_data", "foreign_hold.csv")) if num(r["value"]))
    return {"fut": fut, "opt": opt, "oi": oi, "px": px, "idx": idx, "spot": spot, "hold": hold}


# ------------------------------------------------------------------ 每日衍生指標
def f_net_of(day, cfg=FP_CONFIG):
    """外資期貨淨未平倉(大台口數);三個商品缺一 → None"""
    v = []
    for c, k in (("TX", 1), ("MTX", cfg["mtx_per_tx"]), ("TMF", cfg["tmf_per_tx"])):
        r = (day or {}).get((c, FOREIGN))
        n = num(r["net_oi"]) if r else None
        if n is None:
            return None
        v.append(n / k)
    return sum(v)


def inst_net(day, contract):
    vals = [num(r["net_oi"]) for (c, w), r in (day or {}).items() if c == contract]
    vals = [x for x in vals if x is not None]
    return sum(vals) if vals else None


def hold_on(hold, idx, d):
    """d 日外資持股市值 ≈ 最近一次快照市值 × 加權(d ÷ 快照日);沒有快照 → None"""
    ds = [x for x, _ in hold]
    i = bisect.bisect_right(ds, d) - 1
    if i < 0:
        return None
    sd, sv = hold[i]
    a, b = idx.get(sd), idx.get(d)
    return sv * b / a if a and b else None


def build_rows(data, cfg=FP_CONFIG):
    fut, opt, oi, px, idx, spot, hold = (data[k] for k in ("fut", "opt", "oi", "px", "idx", "spot", "hold"))
    dates = sorted(d for d in fut if d in idx)
    rows = []
    for i, d in enumerate(dates):
        r = {"date": d}
        F = f_net_of(fut.get(d), cfg)
        r["F_net"] = F
        prev = lambda k: f_net_of(fut.get(dates[i - k]), cfg) if i >= k else None
        p1, p5 = prev(1), prev(5)
        r["dF_1d"] = F - p1 if F is not None and p1 is not None else None
        r["dF_5d"] = F - p5 if F is not None and p5 is not None else None
        o = opt.get(d) or {}
        c, p = o.get("CALL"), o.get("PUT")
        C = num(c["long_oi"]) - num(c["short_oi"]) if c and num(c["long_oi"]) is not None and num(c["short_oi"]) is not None else None
        P = num(p["long_oi"]) - num(p["short_oi"]) if p and num(p["long_oi"]) is not None and num(p["short_oi"]) is not None else None
        r["C_net"], r["P_net"] = C, P
        if C is not None and P is not None:
            S = min(max(C, 0), max(-P, 0))
            A = min(S, max(-F, 0)) if F is not None else None
            r["S_syn"], r["A_arb"] = S, A
            r["arb_ratio"] = (A / abs(F) if F < 0 else 0.0) if F is not None and A is not None else None
            r["F_dir"] = F + A if F is not None and A is not None else None
            r["C_res"], r["P_res"] = C - S, P + S
            r["opt_dir"] = sign(C - S) - sign(P + S)
        else:
            for k in ("S_syn", "A_arb", "arb_ratio", "F_dir", "C_res", "P_res", "opt_dir"):
                r[k] = None
        prem = lambda x: (num(x.get("long_oi_k")) * 1000 / num(x["long_oi"]) / cfg["multiplier_opt"]
                          if x and num(x.get("long_oi_k")) is not None and num(x.get("long_oi")) else None)
        r["avg_prem_call"], r["avg_prem_put"] = prem(c), prem(p)
        r["settle"], r["taiex"] = px.get(d), idx.get(d)
        r["basis"] = r["settle"] - r["taiex"] if r["settle"] is not None and r["taiex"] is not None else None
        r["div_season"] = int(d[5:7]) in cfg["div_months"]
        hv = hold_on(hold, idx, d)
        r["hold_value"] = hv
        r["hedge_ratio"] = (max(-F, 0) * r["taiex"] * cfg["multiplier_tx"] / hv) if hv and F is not None and r["taiex"] else None
        r["spot_net"] = spot.get(d)
        last5 = [spot.get(x) for x in dates[max(0, i - 4):i + 1]]
        r["spot_5d"] = sum(last5) if len(last5) == 5 and None not in last5 else None
        r["retail_ratio"] = cc.retail_net_pct(inst_net(fut.get(d), "MTX"), oi.get((d, "MTX")))
        rows.append(r)
    # 百分位(252 日,不含當日)
    col = lambda k: [x[k] for x in rows]
    for k, src in (("dF_5d_pct", col("dF_5d")), ("F_dir_pct", col("F_dir")), ("hedge_pct", col("hedge_ratio")),
                   ("basis_pct_rank", [abs(x) if x is not None else None for x in col("basis")]),
                   ("spot_abs_pct", [abs(x) if x is not None else None for x in col("spot_5d")])):
        for x, v in zip(rows, rolling_pct(src, cfg)):
            x[k] = v
    # 散戶:沿用共識頁「小型臺指 散戶淨多單比」(對之前全部歷史的百分位,90 / 10)
    for x, v in zip(rows, expanding_pct(col("retail_ratio"), cc.CONSENSUS_CFG["min_hist"])):
        x["retail_pct"] = v
        x["retail_zone"] = cc.zone(v, +1)
    for x in rows:
        x.update(judge(x, cfg))
    add_forward_returns(rows, cfg)
    return rows


# ------------------------------------------------------------------ 判讀
def judge(r, cfg=FP_CONFIG):
    """規格 §4:依序比對,第一個符合的就是標籤;必要欄位缺 → label None(資料缺)"""
    need = ("F_net", "arb_ratio", "opt_dir", "spot_5d", "dF_5d_pct")
    out = {"label": None, "v_fut": None, "v_spot": None, "v_opt": None, "v_retail": None, "score": None, "missing": []}
    miss = [k for k in need if r.get(k) is None]
    if miss:
        out["missing"] = miss
        return out
    arb = r["arb_ratio"] >= cfg["arb_ratio"] and ((r.get("basis_pct_rank") or 0) >= cfg["basis_pct"] or r["div_season"])
    hedge_ok = r.get("hedge_pct") is None or r["hedge_pct"] < cfg["hedge_pct"]
    if arb:
        lab = "ARBITRAGE"
    elif r["F_net"] < 0 and r["spot_5d"] >= 0 and hedge_ok:
        lab = "HEDGE"
    elif r["dF_5d_pct"] <= cfg["dF5_bear_pct"] and r["spot_5d"] < 0 and r["opt_dir"] <= -1:
        lab = "BEAR_CONFIRM"
    elif r["dF_5d_pct"] >= cfg["dF5_bull_pct"] and r["spot_5d"] > 0 and r["opt_dir"] >= 1:
        lab = "BULL_CONFIRM"
    else:
        lab = "DIVERGE"
    v_fut = 0 if lab == "ARBITRAGE" or r.get("F_dir_pct") is None else \
        clamp(js_round((r["F_dir_pct"] - cfg["fut_center"]) / cfg["fut_step"]), -1, 1)
    v_spot = sign(r["spot_5d"]) if (r.get("spot_abs_pct") or 0) >= cfg["spot_pct"] else 0
    v_opt = clamp(r["opt_dir"], -1, 1)
    v_ret = -1 if r.get("retail_zone") == "hot" else (1 if r.get("retail_zone") == "cold" else 0)
    out.update({"label": lab, "v_fut": v_fut, "v_spot": v_spot, "v_opt": v_opt, "v_retail": v_ret,
                "score": v_fut + v_spot + v_opt + v_ret})
    return out


def add_forward_returns(rows, cfg=FP_CONFIG):
    for i, r in enumerate(rows):
        for h in cfg["bt_horizons"]:
            j = i + h
            r["ret%d" % h] = (round((rows[j]["taiex"] / r["taiex"] - 1) * 100, 3)
                              if j < len(rows) and r["taiex"] and rows[j]["taiex"] else None)


def backtest(rows, cfg=FP_CONFIG):
    """依標籤統計 T+5 / T+20 加權報酬;n < 30 → 樣本不足、不給勝率"""
    out = {}
    for lab in list(LABEL_ZH):
        s = {"n": 0}
        for h in cfg["bt_horizons"]:
            rs = [r["ret%d" % h] for r in rows if r.get("label") == lab and r.get("ret%d" % h) is not None]
            s["n%d" % h] = len(rs)
            s["n"] = max(s["n"], len(rs))
            if len(rs) >= cfg["bt_min_n"]:
                s["winRate%d" % h] = round(sum(1 for x in rs if x > 0) / len(rs) * 100, 1)
                s["avgRet%d" % h] = round(sum(rs) / len(rs), 2)
        s["status"] = "ok" if s.get("n20", 0) >= cfg["bt_min_n"] or s.get("n5", 0) >= cfg["bt_min_n"] else "樣本不足"
        s["count"] = sum(1 for r in rows if r.get("label") == lab)
        out[lab] = s
    base = {}
    for h in cfg["bt_horizons"]:
        rs = [r["ret%d" % h] for r in rows if r.get("label") and r.get("ret%d" % h) is not None]
        if rs:
            base["winRate%d" % h] = round(sum(1 for x in rs if x > 0) / len(rs) * 100, 1)
            base["avgRet%d" % h] = round(sum(rs) / len(rs), 2)
            base["n%d" % h] = len(rs)
    out["_all"] = base
    return out


# ------------------------------------------------------------------ 輸出
def reasons_of(r, cfg=FP_CONFIG):
    out = []
    s5 = r.get("spot_5d")
    if s5 is not None:
        out.append("現貨 5 日%s %.0f 億" % ("買超" if s5 >= 0 else "賣超", abs(s5) / 1e8))
    if r.get("dF_5d") is not None:
        d = r["dF_5d"]
        out.append("期貨淨部位 5 日%s %.0f 口(分位 %s)" % ("增加" if d >= 0 else "減少", abs(d),
                                                     "—" if r.get("dF_5d_pct") is None else int(round(r["dF_5d_pct"]))))
    if r.get("arb_ratio") is not None:
        a = r["arb_ratio"]
        out.append("套利比率%s(%.0f%%)" % ("高" if a >= cfg["arb_ratio"] else "中等" if a >= 0.2 else "低", a * 100))
    if r.get("opt_dir") is not None:
        out.append("選擇權剩餘方向%s" % ({2: "偏多", 1: "略偏多", 0: "中性", -1: "略偏空", -2: "偏空"}[r["opt_dir"]]))
    if r.get("retail_zone"):
        out.append("散戶小台%s(反向)" % ("偏多擁擠" if r["retail_zone"] == "hot" else "偏空擁擠"))
    return out


def diverge_detail(r):
    sides = {"期貨": sign(r.get("dF_5d") or 0), "現貨": sign(r.get("spot_5d") or 0), "選擇權": sign(r.get("opt_dir") or 0)}
    pos = [k for k, v in sides.items() if v > 0]
    neg = [k for k, v in sides.items() if v < 0]
    if pos and neg:
        return "%s偏多、%s偏空" % ("、".join(pos), "、".join(neg))
    return "條件未同時成立(%s)" % "、".join("%s%s" % (k, "+" if v > 0 else "−" if v < 0 else "0") for k, v in sides.items())


def r1(x, n=0):
    return None if x is None else (round(x, n) if n else int(round(x)))


def payload(rows, cfg=FP_CONFIG):
    if not rows:
        return {"version": VERSION, "date": None, "label": None, "labelZh": "資料缺", "caveats": ["沒有歷史資料"]}
    r = rows[-1]
    bt = backtest(rows, cfg)
    lab = r.get("label")
    caveats = ["選擇權無履約價明細,delta 未計算", "套利口數為推估上限(假設合成多單全部對應期貨空單)",
               "平均權利金 = 買方未平倉金額 ÷ 口數 ÷ 50,只能粗估價內外程度"]
    if r.get("hold_value") is None:
        caveats.append("外資持股市值缺,避險比率未計算")
    else:
        caveats.append("外資持股市值為月快照 × 加權指數推估(只含上市)")
    if r.get("missing"):
        caveats.append("資料缺:%s" % "、".join(r["missing"]))
    cur_bt = bt.get(lab) or {"n": 0, "status": "樣本不足"}
    out = {
        "version": VERSION, "date": r["date"], "label": lab, "labelZh": LABEL_ZH.get(lab, "資料缺"),
        "score": r.get("score"),
        "votes": {"fut": r.get("v_fut"), "spot": r.get("v_spot"), "opt": r.get("v_opt"), "retail": r.get("v_retail")},
        "F_net": r1(r.get("F_net")), "F_dir": r1(r.get("F_dir")),
        "arb_ratio": None if r.get("arb_ratio") is None else round(r["arb_ratio"], 3),
        "dF_1d": r1(r.get("dF_1d")), "dF_5d": r1(r.get("dF_5d")), "dF_5d_pct": r.get("dF_5d_pct"),
        "F_dir_pct": r.get("F_dir_pct"),
        "spot_bn": None if r.get("spot_net") is None else round(r["spot_net"] / 1e8, 1),
        "spot_5d_bn": None if r.get("spot_5d") is None else round(r["spot_5d"] / 1e8, 1),
        "spot_abs_pct": r.get("spot_abs_pct"),
        "basis": r1(r.get("basis")), "basis_pct_rank": r.get("basis_pct_rank"), "settle": r.get("settle"),
        "taiex": r.get("taiex"), "div_season": r.get("div_season"),
        "hedge_ratio": None if r.get("hedge_ratio") is None else round(r["hedge_ratio"], 4), "hedge_pct": r.get("hedge_pct"),
        "retail_ratio": r.get("retail_ratio"), "retail_pct": r.get("retail_pct"),
        "opt": {"C_net": r1(r.get("C_net")), "P_net": r1(r.get("P_net")), "S_syn": r1(r.get("S_syn")),
                "A_arb": r1(r.get("A_arb")), "C_res": r1(r.get("C_res")), "P_res": r1(r.get("P_res")),
                "opt_dir": r.get("opt_dir"),
                "avg_prem_call": r1(r.get("avg_prem_call"), 1), "avg_prem_put": r1(r.get("avg_prem_put"), 1)},
        "reasons": reasons_of(r, cfg),
        "diverge": diverge_detail(r) if lab == "DIVERGE" else None,
        "caveats": caveats,
        "backtest": dict(cur_bt, label=lab),
        "backtestAll": bt,
        "spark": [x.get("score") for x in rows[-60:]],
        "history_days": sum(1 for x in rows if x.get("label")),
        "cfg": {k: FP_CONFIG[k] for k in ("vote_threshold", "arb_ratio", "basis_pct", "hedge_pct", "dF5_bear_pct",
                                          "dF5_bull_pct", "spot_pct", "pct_window")},
    }
    t = cfg["vote_threshold"]
    s = r.get("score")
    out["vote_zone"] = None if s is None else ("hot" if s <= -t else "cold" if s >= t else None)
    return out


def write_history(rows, path="fp_history.csv"):
    tmp = path + ".tmp"
    fmt = lambda v: "" if v is None else (("%.4f" % v).rstrip("0").rstrip(".") if isinstance(v, float) else str(v))
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(HIST_COLS)
        for r in rows:
            w.writerow([fmt(r.get(k)) for k in HIST_COLS])
    os.replace(tmp, path)


def compute(base=".", write=True):
    rows = build_rows(load(base))
    if write:
        write_history(rows, os.path.join(base, "fp_history.csv"))
    return payload(rows), rows


def report(rows, dates=None):
    """驗收:版本、指定日期原始數字、各標籤歷史分布(只印)"""
    print("ForeignPositioning %s" % VERSION)
    labeled = [r for r in rows if r.get("label")]
    print("歷史:%d 個交易日,其中可判讀 %d 天(%s → %s)" % (len(rows), len(labeled),
                                                     labeled[0]["date"] if labeled else "-", labeled[-1]["date"] if labeled else "-"))
    for d in dates or []:
        r = next((x for x in rows if x["date"] == d), None)
        if r:
            print("核對 %s F_net %s(TX/MTX/TMF 換算大台)C_net %s P_net %s spot_net %s 億 spot_5d %s 億 settle %s TAIEX %s basis %s"
                  % (d, r1(r.get("F_net")), r1(r.get("C_net")), r1(r.get("P_net")),
                     None if r.get("spot_net") is None else round(r["spot_net"] / 1e8, 2),
                     None if r.get("spot_5d") is None else round(r["spot_5d"] / 1e8, 2), r.get("settle"), r.get("taiex"),
                     r1(r.get("basis"))))
    bt = backtest(rows)
    for lab, s in bt.items():
        print("標籤 %-13s %s" % (lab, s))
    from collections import Counter
    print("分數分布", sorted(Counter(r["score"] for r in labeled).items()))


if __name__ == "__main__":
    import json
    import sys
    p, rows = compute()
    report(rows, sys.argv[1:])
    q = dict(p)
    q.pop("backtestAll", None)
    print(json.dumps(q, ensure_ascii=False))
