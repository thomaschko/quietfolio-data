# -*- coding: utf-8 -*-
"""
margin_share.py — 族群 / ETF 融資占比 每日計算(2026-11-02,Phase 2)
============================================================
只描述槓桿資金分布;不改任何雷達的收錄、排序、門檻,不輸出買賣建議。

資料(全部沿用 repo 既有來源,不另寫第二套融資抓取):
  個股融資餘額:margin_daily/YYYYMMDD.json(Apps Script runMarginDrop 每晚推送,含上市信用交易統計)
  歷史:margin_hist/(margin_backfill.py 回補);當日資料以 margin_daily 為主寫進 margin_hist
  收盤價、上櫃官方融資合計、指數:沿用 margin_backfill.py 的官方端點與 Http(robots、間隔 3 秒、遇擋即停)
  margin_daily 某天沒有推送 → 用 margin_backfill.run_batch(最多 5 天)補抓缺的交易日,同一套規則
  產業別 / ETF:證交所 ISIN 公開頁(margin_share_classes.json 快取,7 天更新一次;抓不到沿用快取)
  題材:My-TW-Coverage themes/*.md(workflow 以 git 淺層 clone 取得;沒有就沿用 margin_share_themes.json)

輸出(每個 ≤ 45,000 字元;任何一個失敗 → 三個都保留舊檔,結束碼 1):
  margin_share.json          最新一日:各市場產業 / ETF / 未分類(互斥)與題材(可重疊)占比、5/20 日變化(百分點)、
                             歷史分位、集中度、核對比、資料旗標
  margin_share_history.json  近 260 個交易日占比序列(萬分比整數;字數不夠時改為每 N 日取樣並註明)
  margin_share_events.json   指數大漲 / 大跌日的產業融資變化(事件數 < 10 不輸出中位數)
"""
import datetime as dt
import glob
import json
import os
import re
import sys

import margin_backfill as mb
import margin_share_core as ms

OUT = "margin_share.json"
HIST_OUT = "margin_share_history.json"
EV_OUT = "margin_share_events.json"
CLASS_CACHE = "margin_share_classes.json"
THEME_CACHE = "margin_share_themes.json"
SIZE_LIMIT = 45000
HIST_DAYS = 260
CLASS_MAX_AGE_DAYS = 7
GAP_FILL_DAYS = 5
ISIN_URL = "https://isin.twse.com.tw/isin/C_public.jsp?strMode=%d"
MKT_NAME = {"twse": "上市", "tpex": "上櫃"}


def now():
    return dt.datetime.now(mb.TPE)


def iso(ymd):
    s = str(ymd)
    return "%s-%s-%s" % (s[:4], s[4:6], s[6:8])


# ------------------------------------------------------------------ 分類
def parse_isin(html):
    """→ [{code, name, industry, section}];section = 表格分類標題列(股票 / ETF / …)"""
    rows, section = [], None
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, flags=re.S | re.I):
        tds = [re.sub(r"<[^>]+>", "", x).replace("&nbsp;", " ").strip()
               for x in re.findall(r"<td[^>]*>(.*?)</td>", tr, flags=re.S | re.I)]
        if len(tds) == 1 or (tds and all(not x for x in tds[1:])):
            if tds and tds[0]:
                section = tds[0].strip()
            continue
        if len(tds) < 6 or "有價證券代號" in tds[0]:
            continue
        m = re.match(r"^(\S+)\s+(.+)$", tds[0].replace("　", " "))
        if m:
            rows.append({"code": m.group(1), "name": m.group(2).strip(), "industry": tds[4], "section": section})
    return rows


def classes_from_isin(rows):
    ind, etf, names = {}, [], {}
    for x in rows:
        sec = (x["section"] or "").upper()
        if "ETF" in sec:
            etf.append(x["code"])
        elif "股票" in sec and "特別" not in sec and x["industry"]:
            ind[x["code"]] = x["industry"]
        names[x["code"]] = x["name"]
    return ind, sorted(set(etf)), names


def fetch_text(http, url, encoding):
    if not http._robots_ok(url):
        raise mb.Blocked("robots.txt 不允許:" + url)
    r = http._raw(url)
    if r.status_code != 200:
        raise mb.Blocked("HTTP %s:%s" % (r.status_code, url))
    return r.content.decode(encoding, errors="replace")


def load_classes(http, today, path=CLASS_CACHE):
    """→ (cache dict, note);ISIN 抓不到或解析不到 → 沿用快取"""
    cache = mb._read_json(path, None)
    fresh = cache and (today - dt.date.fromisoformat(cache["asof"])).days < CLASS_MAX_AGE_DAYS
    if fresh:
        return cache, "cache"
    try:
        out = {"asof": today.isoformat(), "source": "isin.twse.com.tw C_public.jsp strMode=2/4"}
        for mkt, mode in (("twse", 2), ("tpex", 4)):
            ind, etf, names = classes_from_isin(parse_isin(fetch_text(http, ISIN_URL % mode, "cp950")))
            if len(ind) < 300:
                raise mb.Blocked("ISIN %s 只解析到 %d 檔股票" % (mkt, len(ind)))
            out[mkt] = {"industry": ind, "etf": etf, "names": names}
        mb._write_json(path, out)
        return out, "refreshed"
    except mb.Blocked as e:
        if cache:
            return cache, "cache_stale(%s)" % e
        raise


def parse_theme_md(text):
    return sorted({m.group(1) for m in re.finditer(r"\*\*(\d{4,6}[A-Z]?)\s+[^\*]+?\*\*", text)})


def load_themes(themes_dir, path=THEME_CACHE, sha=None):
    """themes_dir = My-TW-Coverage 的 themes/(git clone);沒有 → 沿用快取"""
    files = sorted(glob.glob(os.path.join(themes_dir, "*.md"))) if themes_dir else []
    themes = {}
    for f in files:
        if os.path.basename(f).lower() == "readme.md":
            continue
        with open(f, encoding="utf-8", errors="replace") as fh:
            codes = parse_theme_md(fh.read())
        if codes:
            themes[os.path.splitext(os.path.basename(f))[0]] = codes
    if len(themes) >= 5:
        out = {"asof": now().date().isoformat(), "source": "github.com/Timeverse/My-TW-Coverage themes/*.md(MIT)",
               "commit": sha, "themes": themes}
        mb._write_json(path, out)
        return out, "refreshed"
    cache = mb._read_json(path, None)
    if cache:
        return cache, "cache"
    return {"themes": {}, "source": None}, "missing"


# ------------------------------------------------------------------ 當日資料寫進 margin_hist
def latest_margin_daily(d="margin_daily"):
    fs = sorted(glob.glob(os.path.join(d, "2*.json")))
    return mb._read_json(fs[-1], None) if fs else None


def _store_day(store, mkt, date, margin, close, tl, tk, status):
    for c, lots in margin.items():
        p = close.get(c)
        store.rows.append((date, mkt, c, mb._fmt(lots), mb._fmt(p)))
        if lots is None:
            store.missing.append((date, mkt, c, "no_margin_value"))
        elif lots > 0 and p is None:
            store.missing.append((date, mkt, c, "no_close"))
    store.official.append((date, mkt, mb._fmt(tl), mb._fmt(tk)))
    store.mark(mkt, date, status)


def ingest_daily(http, store, md):
    """margin_daily → margin_hist(已有的不重寫)。收盤價抓不到 → 這個市場先不寫,留給下次。→ log dict"""
    log = {}
    if not md:
        return {"margin_daily": "none"}
    date = iso(md["date"])
    d = dt.date.fromisoformat(date)
    log["margin_daily"] = date
    todo = []
    if md.get("twse"):
        todo.append("twse")
    if md.get("tpex") and str(md.get("tpex_date")) == str(md["date"]):
        todo.append("tpex")
    for mkt in todo:
        if store.done(mkt, date):
            log[mkt] = "already"
            continue
        margin = {c: mb.num(v[0]) if v and v[0] is not None else None for c, v in md[mkt].items()}
        try:
            if mkt == "twse":
                jc = http.json(mb.TWSE_CLOSE.format(ymd=d.strftime("%Y%m%d")))
                close = mb.parse_twse_close(jc) if mb.resp_date(jc) == date else {}
                _, tl, tk = mb.parse_twse_margin({"tables": [md["twse_summary"]]}) if md.get("twse_summary") else ({}, None, None)
            else:
                slash = d.strftime("%Y/%m/%d")
                jm = http.json(mb.TPEX_MARGIN.format(slash=slash))
                tl, tk = (mb.parse_tpex_margin(jm)[1:] if mb.resp_date(jm) == date else (None, None))
                jc = http.json(mb.TPEX_CLOSE.format(slash=slash))
                close = mb.parse_tpex_close(jc) if mb.resp_date(jc) == date else {}
        except mb.Blocked as e:
            log[mkt] = "stopped:" + str(e)
            break
        if not close:
            log[mkt] = "no_close_yet"
            continue
        _store_day(store, mkt, date, margin, close, tl, tk, "ok_daily")
        log[mkt] = "ok"
    store.flush()
    return log


# ------------------------------------------------------------------ 讀歷史、逐日計算
def load_hist(d=mb.OUT_DIR):
    data = {m: {} for m in mb.MARKETS}
    for f in sorted(glob.glob(os.path.join(d, "2*.csv.gz"))):
        for r in mb.read_csv(f, gz=True):
            day = data.setdefault(r["market"], {}).setdefault(r["date"], {})
            day[r["code"]] = (mb.num(r["margin_lots"]), mb.num(r["close"]))
    official = {(r["date"], r["market"]): (mb.num(r["total_lots"]), mb.num(r["official_margin_k"]))
                for r in mb.read_csv(os.path.join(d, "official.csv"))}
    index = {m: [] for m in mb.MARKETS}
    for r in mb.read_csv(os.path.join(d, "index.csv")):
        if mb.num(r["close"]):
            index.setdefault(r["market"], []).append((r["date"], mb.num(r["close"])))
    for m in index:
        index[m].sort()
    prog = mb._read_json(os.path.join(d, "progress.json"), {"done": {}})
    return data, official, index, prog.get("done", {})


def compute_day(rows, cls, themes):
    margin = {c: l for c, (l, _) in rows.items()}
    close = {c: p for c, (_, p) in rows.items() if p}
    est, lots, exc = ms.est_values(margin, close)
    total, ind = ms.by_industry(est, lots, cls["industry"], set(cls["etf"]), cls["names"])
    th, cov, unm = ms.by_theme(est, lots, themes, set(cls["etf"]), cls["names"])
    return {"total": total, "lots_total": sum(lots.values()), "excluded": exc, "industry": ind, "theme": th,
            "coverage_union": cov, "unmapped_share": unm, "lots": lots, "n_codes": len(margin)}


def r2(x, k=2):
    return None if x is None else round(x, k)


def e8(x):
    return None if x is None else round(x / 1e8, 2)


def fmt_row(name, row, hist, date, past_shares, top_names=True):
    out = {
        "name": name,
        "share_pct": r2(row["share"] * 100 if row["share"] is not None else None),
        "est_value_e8": e8(row["est_value"]),
        "lots": int(row["lots"]),
        "n_stocks": row["n_stocks"],
        "n_with_margin": row["n_with_margin"],
        "concentration_pct": r2(row["concentration"] * 100 if row["concentration"] is not None else None, 1),
    }
    for n in ms.MARGIN_SHARE_CFG["change_windows"]:
        out["d%d_pp" % n] = ms.share_change(hist, date, name, n)
    out["share_pctile"] = ms.pctile(row["share"], past_shares)
    out["top3"] = [[t["code"], t["name"], r2(t["w"] * 100 if t["w"] is not None else None, 1)] if top_names
                   else [t["code"], r2(t["w"] * 100 if t["w"] is not None else None, 1)] for t in row["top3"]]
    if "n_multi" in row:
        out["n_multi"] = row["n_multi"]
    return out


def market_report(mkt, days, official, done, md, top_names=True):
    """days:[(date, computed)] 由舊到新 → 最新一日報表 dict"""
    date, cur = days[-1]
    n_days = len(days)
    out = {"date": date, "history_days": n_days, "history_level": ms.history_level(n_days),
           "total_est_value_e8": e8(cur["total"]), "total_lots": int(cur["lots_total"]), "n_codes": cur["n_codes"]}
    tl, tk = official.get((date, mkt), (None, None))
    ratio = ms.recon_ratio(cur["total"], tk)
    past_ratios = [ms.recon_ratio(c["total"], official.get((d, mkt), (None, None))[1]) for d, c in days[:-1]]
    out["official"] = {"total_lots": tl, "margin_k": tk, "source": "TWSE 信用交易統計" if mkt == "twse" else "TPEx 融資融券餘額 合計"}
    out["recon_ratio"] = ratio
    out["recon_off"] = ms.recon_off(ratio, past_ratios)
    out["recon_note"] = "est_value 總額 ÷ 官方融資金額;只供核對,不用來調整占比"
    notes = {}
    if md and iso(md["date"]) == date and md.get(mkt):
        notes = {c: (v[3] if len(v) > 3 else "") for c, v in md[mkt].items()}
    close_date = None if str(done.get(date, "")).startswith("ok_no_close") else date
    flags, unrel = ms.market_flags(date, close_date, cur["excluded"], notes)
    out["flags"], out["unreliable"] = flags, unrel
    for kind in ("industry", "theme"):
        hist = [(d, {g: r["share"] for g, r in c[kind].items()}) for d, c in days]
        rows = []
        for g, r in cur[kind].items():
            past = [c[kind][g]["share"] for _, c in days[:-1] if g in c[kind]]
            rows.append(fmt_row(g, r, hist, date, past, top_names))
        rows.sort(key=lambda x: (-(x["share_pct"] or 0), x["name"]))
        out[kind] = rows
    out["theme_coverage_union_pct"] = r2(cur["coverage_union"] * 100 if cur["coverage_union"] is not None else None)
    out["theme_unmapped_pct"] = r2(cur["unmapped_share"] * 100 if cur["unmapped_share"] is not None else None)
    return out


def history_payload(series, step):
    """series {mkt: [(date, computed)]} → 近 260 日、每 step 日取樣(由最新一天往回數)"""
    out = {"unit": "share 萬分比整數(1234 = 12.34%)", "step_days": step, "markets": {}}
    for mkt, days in series.items():
        days = days[-HIST_DAYS:]
        pick = days[::-1][::step][::-1]
        m = {"dates": [d for d, _ in pick], "total_est_value_e8": [e8(c["total"]) for _, c in pick]}
        for kind in ("industry", "theme"):
            groups = sorted({g for _, c in pick for g in c[kind]})
            m[kind] = {g: [(round(c[kind][g]["share"] * 10000) if g in c[kind] and c[kind][g]["share"] is not None else None)
                           for _, c in pick] for g in groups}
        out["markets"][mkt] = m
    return out


def events_payload(mkt, days, index, names, n_stocks=5):
    """事件日:指數日報酬在可得歷史最低 / 最高 5% 且 |報酬| ≥ 1.5%(margin_share_core.event_days)"""
    pos = {d: i for i, (d, _) in enumerate(days)}
    rets = [(d, r) for d, r in ms.index_returns(index) if d in pos and pos[d] > 0]
    ev = ms.event_days(rets)
    per, rows = {}, []
    fwd_n = ms.MARGIN_SHARE_CFG["event_fwd_days"]
    for d, kind, ret in ev:
        i = pos[d]
        prev, cur = days[i - 1][1], days[i][1]
        fwd = days[i + fwd_n][1]["industry"] if i + fwd_n < len(days) else None
        ch = ms.event_group_changes(prev["industry"], cur["industry"], fwd=fwd or {})   # 沒有 t+5 → None
        per[d] = ch
        ranked = sorted(((g, v) for g, v in ch.items() if v["d_lots_pct"] is not None), key=lambda x: (x[1]["d_lots_pct"], x[0]))
        rows.append({
            "date": d, "dir": kind, "index_ret_pct": ret,
            "groups_lots_down3": [[g, v["d_lots_pct"], v["d_share_pp"], v["d_lots_5d_pct"]] for g, v in ranked[:3]],
            "stocks_lots_drop": [[x["code"], x["name"], int(x["drop_lots"]), x["pct_of_prev"]]
                                 for x in ms.top_lot_drops(prev["lots"], cur["lots"], names, n=n_stocks)],
        })
    s = ms.summarize_events(ev, per)
    s.pop("events", None)
    s["return_days"] = len(rets)
    s["events"] = rows
    s["fields"] = {"groups_lots_down3": "[產業, 融資張數變化%, 占比變化(百分點), t→t+5 張數變化%(回溯描述;最近 5 日內的事件為空)]",
                   "stocks_lots_drop": "[代號, 名稱, 減少張數, 占前一日餘額%]"}
    return s


def dumps(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def build(series, official, index, done, cls, themes_meta, md, sources, status):
    names = {}
    for m in mb.MARKETS:
        names.update(cls[m]["names"])
    gen = now().isoformat(timespec="seconds")
    reports = {}
    for top_names in (True, False):
        reports = {m: market_report(m, series[m], official, done.get(m, {}), md, top_names) for m in series}
        main = {"generated": gen, "status": status["status"], "status_notes": status["notes"],
                "date": max(r["date"] for r in reports.values()), "markets": reports,
                "fields": {"share_pct": "占該市場 est_value 總額 %", "d5_pp/d20_pp": "與 5 / 20 個交易日前相比(百分點)",
                           "share_pctile": "當日占比在過去可得歷史(不含當日)的百分位;歷史 < 120 日為空",
                           "top3": "[代號, 名稱, 占該組 %]" if top_names else "[代號, 占該組 %](名稱因字數限制省略)",
                           "concentration_pct": "前 3 檔占該組 %", "n_multi": "同時屬於其他題材的檔數",
                           "est_value_e8": "億元(擔保股票市值估算,不是官方融資金額)"},
                "notes": ms.NOTES + ["產業別 / ETF / 未分類互斥,加總 = 100%;題材可重疊,ETF 不計入題材",
                                     "官方註記(suspended_or_special)只列原文與檔數,這些股票預設仍計入"],
                "sources": sources}
        s_main = dumps(main)
        if len(s_main) <= SIZE_LIMIT:
            break
    s_hist = None
    for step in (1, 2, 3, 5, 10):
        h = history_payload(series, step)
        h.update({"generated": gen, "notes": ms.NOTES[3:4]})
        s_hist = dumps(h)
        if len(s_hist) <= SIZE_LIMIT:
            break
    s_ev = None
    for n_stocks in (5, 3, 0):
        ev = {"generated": gen, "cfg": {k: ms.MARGIN_SHARE_CFG[k] for k in ("event_tail", "min_abs_ret", "event_min_for_stats", "event_fwd_days")},
              "markets": {m: events_payload(m, series[m], index.get(m, []), names, n_stocks) for m in series},
              "notes": ["事件定義在看資料前固定:指數日報酬落在可得歷史最低 / 最高 5%,且 |報酬| ≥ 1.5%",
                        "產業別為現行分類回推;t→t+5 為回溯描述,不代表可預測"] + ms.NOTES[4:5]}
        s_ev = dumps(ev)
        if len(s_ev) <= SIZE_LIMIT:
            break
    return s_main, s_hist, s_ev


def write_outputs(pairs):
    """全部 ≤ 上限才寫;任何一個超過 → 全部保留舊檔"""
    bad = [p for p, s in pairs if s is None or len(s) > SIZE_LIMIT]
    if bad:
        return bad
    for p, s in pairs:
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(s)
        os.replace(tmp, p)
    return []


def run(http=None, today=None, themes_dir=None, themes_sha=None, hist_dir=mb.OUT_DIR, daily_dir="margin_daily",
        gap_fill=GAP_FILL_DAYS):
    http = http or mb.Http()
    today = today or now().date()
    notes = []
    store = mb.Store(hist_dir)
    md = latest_margin_daily(daily_dir)
    ing = ingest_daily(http, store, md)
    if any(str(v).startswith(("stopped", "no_close")) for v in ing.values()):
        notes.append("當日寫入:" + json.dumps(ing, ensure_ascii=False))
    if gap_fill:
        code, blog = mb.run_batch(http=http, store=mb.Store(hist_dir), today=today, batch_days=gap_fill)
        if code not in (0, 10):
            notes.append("缺日補抓停止:" + str(blog.get("stopped")))
        elif blog.get("dates"):
            notes.append("margin_daily 沒有的交易日改由官方端點補抓 %d 天" % blog["dates"])
    try:
        cls, cls_note = load_classes(http, today)
    except mb.Blocked as e:
        print("產業別抓不到,也沒有快取:%s" % e)
        return 1, {"stopped": str(e)}
    th, th_note = load_themes(themes_dir, sha=themes_sha)
    if th_note == "missing":
        notes.append("題材對照表抓不到也沒有快取,題材部分為空")
    data, official, index, done = load_hist(hist_dir)
    series = {}
    for m in mb.MARKETS:
        dates = sorted(data.get(m, {}))
        if not dates:
            continue
        mcls = {"industry": cls[m]["industry"], "etf": cls[m]["etf"], "names": cls[m]["names"]}
        series[m] = [(d, compute_day(data[m][d], mcls, th["themes"])) for d in dates]
    if not series:
        print("margin_hist 沒有資料")
        return 1, {"stopped": "no data"}
    last = {m: s[-1][0] for m, s in series.items()}
    if len(set(last.values())) > 1:
        notes.append("上市、上櫃最新資料日不同:" + json.dumps(last))
    status = {"status": "ok" if not notes else "partial", "notes": notes}
    sources = {"margin": "margin_daily(Apps Script runMarginDrop)+ margin_hist(官方回補)",
               "close": "TWSE MI_INDEX / TPEx dailyQuotes(核對回應日期)",
               "industry": cls.get("source"), "industry_asof": cls.get("asof"), "industry_load": cls_note,
               "themes": th.get("source"), "themes_commit": th.get("commit"), "themes_load": th_note,
               "ingest": ing}
    s_main, s_hist, s_ev = build(series, official, index, done, cls, th, md, sources, status)
    bad = write_outputs([(OUT, s_main), (HIST_OUT, s_hist), (EV_OUT, s_ev)])
    log = {"status": status, "last": last, "sizes": {OUT: len(s_main), HIST_OUT: len(s_hist), EV_OUT: len(s_ev)},
           "requests": http.count, "net_retries": http.net_retries}
    if bad:
        log["kept_old"] = bad
        print(json.dumps(log, ensure_ascii=False))
        return 1, log
    print(json.dumps(log, ensure_ascii=False))
    return 0, log


def main():
    themes_dir = os.environ.get("TW_COVERAGE_THEMES") or None
    sha = os.environ.get("TW_COVERAGE_SHA") or None
    code, _ = run(themes_dir=themes_dir, themes_sha=sha)
    return code


if __name__ == "__main__":
    sys.exit(main())
