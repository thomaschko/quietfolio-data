# -*- coding: utf-8 -*-
"""
us_tw_resonance.py — 美股法說 × 台股籌碼「題材共振」(2026-11-08)
============================================================
把三份已有的資料對在一起,給頁面「題材 → 美股法說」分頁一個檔(us_tw_resonance.json,≤ 45,000 字元):
  美股:us_pr/ 單日檔(財報新聞稿 8-K 2.02,近 30 天 / 前 30 天)+ us_transcripts(法說逐字稿,本季 / 上季)
  台股:intl_theme_baskets.json 的台股代號 → chip_structure_full.json(集保大戶週變化、外資+自營商同一集保週買賣超)
        + consensus.json(槓桿資金 + 法人同向名單)
對應:src6 詞組 → 題材籃子(GROUP_TO_THEMES,固定寫在這裡、頁面上顯示)。
通用詞(GENERIC_TERMS:supply chain、capacity…幾乎每家財報都會出現)只在詞表顯示,不計入詞組的公司數。
這只是「顯示與計數方式」,不修改 src6 詞表、watchlist 或任何雷達邏輯。
門檻在看資料前固定(RES_CFG);全部是描述,不是買賣建議;新聞稿與法說內容為公司陳述,未經查證。
"""
import datetime as dt
import json
import os
import sys

import us_pr_scan as P
import us_transcripts as U

RES_CFG = {
    "us_hot_pr_min": 3,          # 近 30 天財報新聞稿提到(專有詞)的公司數 ≥ 3 → 美股升溫
    "us_hot_tr_delta": 2,        # 或:兩季都有逐字稿的公司中,本季比上季多 ≥ 2 家
    "tr_min_both": 5,            # 兩季都有逐字稿的公司 < 5 家 → 不做季比較
    "tw_big_pp": 0.05,           # 集保大戶週變化 ≥ +0.05pp(同 chip_structure cfg big_delta_min_pp)
    "tw_min_n": 2,               # 台股同向檔數 ≥ 2 且多於反向 → 共振 / 背離
    "recent_days": 14, "recent_max": 30, "tickers_max": 10, "terms_max": 40,
}
GROUP_TO_THEMES = {
    "先進封裝/光互連": ["晶圓代工/封測", "光通訊/CPO"],
    "記憶體": ["記憶體"],
    "AI電源/散熱": ["電源/散熱"],
    "資料中心電源": ["電源/散熱", "重電/電網"],
    "平台/網通/供應鏈": ["AI伺服器/網通", "PCB/CCL/載板"],
    "台廠點名": [],
    "光通訊/CPO": ["光通訊/CPO"],
    "客戶capex/需求": ["AI伺服器/網通"],
    "測試設備": ["半導體設備"],
    "功率化合物半導體": ["功率/化合物半導體"],
    "IC設計架構": ["ASIC/客製晶片", "矽智財/EDA"],
}
GENERIC_TERMS = {"supply chain", "capacity", "yield", "memory", "data center", "datacenter",
                 "capital expenditure", "capex", "Taiwan"}
OUT = "us_tw_resonance.json"
SIZE_LIMIT = 45000


def _read(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def split_term(t):
    g, _, w = t.partition("|")
    return g, w


def us_side(base, today):
    """→ (pr_days, groups_pr {g: {n30, prev30, tickers30{t: n}}}, terms {term: {...}}, recent)"""
    days = P.load_days(base, (today - dt.timedelta(days=90)).isoformat()) if os.path.isdir(os.path.join(base, P.DAY_DIR)) else {}
    c30 = (today - dt.timedelta(days=30)).isoformat()
    c60 = (today - dt.timedelta(days=60)).isoformat()
    groups, terms = {}, {}
    for date, items in days.items():
        win = "n30" if date > c30 else ("prev30" if date > c60 else None)
        for x in items:
            seen_g = set()
            for t, n in x.get("hits") or []:
                g, w = split_term(t)
                tr = terms.setdefault(t, {"n30": set(), "prev30": set()})
                if win:
                    tr[win].add(x["ticker"])
                if w in GENERIC_TERMS or not win:
                    continue
                gg = groups.setdefault(g, {"n30": {}, "prev30": set()})
                if win == "n30":
                    gg["n30"][x["ticker"]] = gg["n30"].get(x["ticker"], 0) + n
                elif x["ticker"] not in seen_g:
                    gg["prev30"].add(x["ticker"])
                seen_g.add(x["ticker"])
    cr = (today - dt.timedelta(days=RES_CFG["recent_days"])).isoformat()
    recent = []
    for date in sorted(days, reverse=True):
        if date <= cr:
            break
        for x in days[date]:
            spec = [[t, n] for t, n in x.get("hits") or [] if split_term(t)[1] not in GENERIC_TERMS]
            if not spec:
                continue
            sn = [[t, s] for t, s in x.get("sn") or [] if split_term(t)[1] not in GENERIC_TERMS][:1] or (x.get("sn") or [])[:1]
            recent.append({"d": date, "t": x["ticker"], "url": x.get("url"), "h": spec[:4], "sn": sn})
    recent.sort(key=lambda r: (r["d"], sum(n for _, n in r["h"])), reverse=True)
    return days, groups, terms, recent[:RES_CFG["recent_max"]]


def transcript_side(base):
    """→ (quarters, {g: {cur: set, prev: set, cur_both: set, prev_both: set}}, {term: [n_cur, n_prev]}, n_both)"""
    summ = _read(os.path.join(base, U.OUT_HITS), {}) or {}
    qs = summ.get("quarters") or []
    if len(qs) < 1:
        return qs, {}, {}, 0
    items = U.load_store(base, qs)
    cur = qs[0]
    prev = qs[1] if len(qs) > 1 else None
    both = {t for t, v in items.items() if cur in v and prev in v and v[cur].get("hits") is not None and v[prev].get("hits") is not None}
    groups, terms = {}, {}
    for t, v in items.items():
        for q, key in ((cur, "cur"), (prev, "prev")):
            for term in ((v.get(q) or {}).get("hits") or {}):
                g, w = split_term(term)
                terms.setdefault(term, [set(), set()])[0 if key == "cur" else 1].add(t)
                if w in GENERIC_TERMS:
                    continue
                gg = groups.setdefault(g, {"cur": set(), "prev": set(), "cur_both": set(), "prev_both": set()})
                gg[key].add(t)
                if t in both:
                    gg[key + "_both"].add(t)
    return qs, groups, {k: [len(a), len(b)] for k, (a, b) in terms.items()}, len(both)


def tw_side(base):
    baskets = (_read(os.path.join(base, "intl_theme_baskets.json"), {}) or {}).get("themes") or {}
    chip = _read(os.path.join(base, "chip_structure_full.json"), {}) or {}
    stocks = chip.get("stocks") or {}
    cons = _read(os.path.join(base, "consensus.json"), {}) or {}
    lists = ((cons.get("lev_inst") or {}).get("lists")) or {}
    cmem = {}
    for k in ("both_up", "both_down", "margin_up_inst_sell", "margin_down_inst_buy"):
        for row in lists.get(k) or []:
            cmem[str(row[0])] = k
    for row in ((cons.get("pullback") or {}).get("items")) or []:
        cmem.setdefault(str(row[0]), "pullback")
    meta = {"tdcc_asof": chip.get("tdcc_asof"), "inst_asof": chip.get("inst_asof"),
            "consensus_window": (cons.get("window") or {}).get("t"), "chip_stale": chip.get("stale")}
    return baskets, stocks, cmem, meta


def stock_row(code, stocks, cmem):
    s = stocks.get(code) or {}
    d1 = s.get("d1") or [None]
    i = s.get("i") or [None] * 5
    big = d1[0]
    fd = i[4] if len(i) > 4 else None
    c = cmem.get(code)
    up = (big is not None and fd is not None and big >= RES_CFG["tw_big_pp"] and fd > 0) or c == "both_up"
    down = (big is not None and fd is not None and big <= -RES_CFG["tw_big_pp"] and fd < 0) or c == "both_down"
    sig = "up" if up and not down else ("down" if down and not up else "")
    return [code, s.get("n") or "", None if big is None else round(big, 3),
            None if fd is None else round(fd / 1000.0), c or "", sig]


def build(base=".", now=None):
    now = now or dt.datetime.now(dt.timezone(dt.timedelta(hours=8)))
    today = now.date()
    cfg = RES_CFG
    pr_days, g_pr, t_pr, recent = us_side(base, today)
    qs, g_tr, t_tr, n_both = transcript_side(base)
    baskets, stocks, cmem, meta = tw_side(base)
    tr_cmp = n_both >= cfg["tr_min_both"]
    rows = []
    for g, themes in GROUP_TO_THEMES.items():
        a = g_pr.get(g) or {"n30": {}, "prev30": set()}
        b = g_tr.get(g) or {"cur": set(), "prev": set(), "cur_both": set(), "prev_both": set()}
        pr_n30, pr_prev = len(a["n30"]), len(a["prev30"])
        tr_delta = (len(b["cur_both"]) - len(b["prev_both"])) if tr_cmp else None
        us_hot = pr_n30 >= cfg["us_hot_pr_min"] or (tr_delta is not None and tr_delta >= cfg["us_hot_tr_delta"])
        codes, seen = [], set()
        for th in themes:
            for c in (baskets.get(th) or {}).get("tw") or []:
                if c not in seen:
                    seen.add(c)
                    codes.append(stock_row(c, stocks, cmem))
        n_up = sum(1 for r in codes if r[5] == "up")
        n_dn = sum(1 for r in codes if r[5] == "down")
        if not themes:
            label = "僅美股"
        elif us_hot and n_up >= cfg["tw_min_n"] and n_up > n_dn:
            label = "共振"
        elif us_hot and n_dn >= cfg["tw_min_n"] and n_dn > n_up:
            label = "背離"
        elif us_hot:
            label = "美股升溫"
        elif n_up >= cfg["tw_min_n"] and n_up > n_dn:
            label = "台股先動"
        else:
            label = ""
        top = sorted(a["n30"], key=lambda k: (-a["n30"][k], k))[:cfg["tickers_max"]]
        g_terms = sorted([(t, len(v["n30"])) for t, v in t_pr.items() if split_term(t)[0] == g and split_term(t)[1] not in GENERIC_TERMS and v["n30"]],
                         key=lambda x: -x[1])[:4]
        rows.append({"g": g, "themes": themes, "label": label, "us_hot": us_hot,
                     "pr_n30": pr_n30, "pr_prev30": pr_prev, "pr_tickers": top,
                     "pr_terms": [[split_term(t)[1], n] for t, n in g_terms],
                     "tr_cur": len(b["cur"]), "tr_cur_both": len(b["cur_both"]) if tr_cmp else None,
                     "tr_prev_both": len(b["prev_both"]) if tr_cmp else None,
                     "tw_n": len(codes), "tw_up": n_up, "tw_down": n_dn, "tw": codes})
    order = {"共振": 0, "背離": 1, "美股升溫": 2, "台股先動": 3, "": 4, "僅美股": 5}
    rows.sort(key=lambda r: (order[r["label"]], -r["pr_n30"], -r["tr_cur"]))
    terms = []
    for t in set(t_pr) | set(t_tr):
        g, w = split_term(t)
        p = t_pr.get(t) or {"n30": set(), "prev30": set()}
        tr = t_tr.get(t) or [0, 0]
        if not (p["n30"] or p["prev30"] or tr[0]):
            continue
        terms.append([g, w, len(p["n30"]), len(p["prev30"]), tr[0], tr[1], 1 if w in GENERIC_TERMS else 0])
    terms.sort(key=lambda r: (r[6], -r[2], -r[4], r[1]))
    doc = {"generated": now.isoformat(timespec="seconds"), "cfg": cfg,
           "asof": dict(meta, pr_days=sorted(pr_days)[-1:] and sorted(pr_days)[-1] or None, pr_n_days=len(pr_days),
                        tr_quarters=qs, tr_n_both=n_both, tr_compare=tr_cmp),
           "map": GROUP_TO_THEMES, "generic": sorted(GENERIC_TERMS),
           "fields": {"tw": "[代號, 名稱, 大戶週變化pp, 外資+自營商同一集保週買賣超(張), 共識頁分類, up/down]",
                      "terms": "[詞組, 詞, 新聞稿近30天公司數, 前30天, 法說本季公司數, 上季, 通用詞1/0]",
                      "recent": "d 申報日, t 代號, h [[詞, 次數]], sn [[詞, 片段]]"},
           "labels": {"共振": "美股升溫,且對應台股有 ≥2 檔大戶增 + 外資自營商買超(或共識頁融資+法人同向買),多於反向",
                      "背離": "美股升溫,但對應台股反向(大戶減 + 外資自營商賣超)的檔數較多",
                      "美股升溫": "美股升溫,台股籌碼沒有明顯同向",
                      "台股先動": "台股籌碼同向,美股法說 / 新聞稿尚未升溫",
                      "僅美股": "這個詞組沒有對應的台股題材籃子"},
           "notes": ["美股新聞稿與法說內容為公司陳述,未經查證;詞命中不等於題材成立",
                     "集保大戶為週資料,不分身分;外資+自營商與大戶同向只是印證,不是因果",
                     "對應表與門檻在看資料前固定;全部是描述,沒有預測效力的證據,不是買賣建議"],
           "groups": rows, "terms": terms[:cfg["terms_max"]], "recent": recent}
    s = json.dumps(doc, ensure_ascii=False, separators=(",", ":"))
    while len(s) > SIZE_LIMIT and doc["recent"]:
        doc["recent"] = doc["recent"][:-3]
        s = json.dumps(doc, ensure_ascii=False, separators=(",", ":"))
    if len(s) > SIZE_LIMIT:
        raise SystemExit("輸出超過 %d 字元,保留舊檔" % SIZE_LIMIT)
    tmp = os.path.join(base, OUT + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(s)
    os.replace(tmp, os.path.join(base, OUT))
    return doc, len(s)


def main():
    doc, n = build()
    print(json.dumps({"chars": n, "labels": [[r["g"], r["label"], r["pr_n30"], r["tr_cur"], r["tw_up"], r["tw_down"], r["tw_n"]]
                                             for r in doc["groups"]]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
