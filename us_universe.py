# -*- coding: utf-8 -*-
"""
us_universe.py — 美股法說擴充 Phase 1:以產業代碼決定公司範圍(2026-11-08)
============================================================
來源:SEC company_tickers_exchange.json(代號、交易所)+ 每家 data.sec.gov submissions(sic、sicDescription)。
SIC 對照一次性建立並快取(us_sic_cache.json,以 CIK 為鍵、可續跑);之後只補新出現的 CIK,
已有的每 SIC_REFRESH_DAYS 天重抓一批(偵測代碼變動)。公司數不設上限;超過 universe_warn 只警告、列出各 SIC 家數,不截斷。
輸出 us_universe.json:每家 [cik, ticker, name, sic, exchange, source(sic / seed / core)],另附各 SIC 的說明文字與家數
(說明文字取自 SEC 實際資料,供核對規格中的候選代碼)。
"""
import ast
import datetime as dt
import json
import os
import sys
import time

import edgar_http as eh

US_UNIVERSE_CFG = {
    # 規格候選代碼;2026-11-08 探測以 sicDescription 核對過的:3674 3679 3571 3576 3661 3669 3825 3827,
    # 其餘在建立對照時依實際說明文字列在 sic_report,由使用者決定是否保留
    "sic_include": ["3674", "3679", "3672", "3570", "3571", "3572", "3575", "3576", "3577", "3578", "3579",
                    "3661", "3663", "3669", "3612", "3613", "3621", "3629", "3690", "3825", "3559", "3827"],
    "sic_exclude": [],
    "exchanges": ["NYSE", "Nasdaq"],
    "seed_file": "us_seed_tickers.txt",
    "core_files": ["earnings_keywords.py", "intl_theme_baskets.json"],   # 讀取,不修改
    "universe_warn": 1500,
    "time_budget_min": 40,
}
CACHE = "us_sic_cache.json"
OUT = "us_universe.json"
TICKERS_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
SUB_URL = "https://data.sec.gov/submissions/CIK%010d.json"


def core_tickers(base="."):
    out = set()
    try:
        with open(os.path.join(base, "earnings_keywords.py"), encoding="utf-8") as f:
            for n in ast.parse(f.read()).body:
                if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "COMPANIES":
                    out |= {t.upper() for t in ast.literal_eval(n.value)}
    except (OSError, SyntaxError, ValueError):
        pass
    try:
        with open(os.path.join(base, "intl_theme_baskets.json"), encoding="utf-8") as f:
            for v in (json.load(f).get("themes") or {}).values():
                out |= {t.upper() for t in v.get("us") or []}
    except (OSError, ValueError):
        pass
    return out


def seed_tickers(base="."):
    p = os.path.join(base, US_UNIVERSE_CFG["seed_file"])
    if not os.path.exists(p):
        return set()
    with open(p, encoding="utf-8") as f:
        return {l.split("#")[0].strip().upper() for l in f if l.split("#")[0].strip()}


def build(edgar, base=".", now=None, clock=time.time):
    cfg = US_UNIVERSE_CFG
    now = now or dt.datetime.now(dt.timezone.utc)
    t0 = clock()
    j = lambda p: os.path.join(base, p)
    try:
        with open(j(CACHE), encoding="utf-8") as f:
            cache = json.load(f)
    except (OSError, ValueError):
        cache = {}
    r = edgar.get(TICKERS_URL)
    if r.status_code != 200:
        raise eh.EdgarAbort("company_tickers_exchange HTTP %s" % r.status_code)
    doc = r.json()
    f = doc["fields"]
    ic, it, inm, iex = f.index("cik"), f.index("ticker"), f.index("name"), f.index("exchange")
    rows = [x for x in doc["data"] if x[iex] in cfg["exchanges"]]
    core, seed = core_tickers(base), seed_tickers(base)
    want = {}
    for x in rows:
        want.setdefault(str(x[ic]), x)
    # core / seed 若不在 NYSE、Nasdaq 清單(例如 OTC ADR),也照代號找 CIK
    by_ticker = {str(x[it]).upper(): x for x in doc["data"]}
    for t in core | seed:
        if t in by_ticker:
            want.setdefault(str(by_ticker[t][ic]), by_ticker[t])
    todo = [c for c in want if c not in cache]
    done = 0
    stopped = None
    for c in todo:
        if clock() - t0 > cfg["time_budget_min"] * 60:
            stopped = "時間預算用完,下次續跑(剩 %d 家)" % (len(todo) - done)
            break
        rr = edgar.get(SUB_URL % int(c))
        if rr.status_code == 200:
            s = rr.json()
            cache[c] = [s.get("sic") or "", s.get("sicDescription") or "", now.date().isoformat()]
        else:
            cache[c] = ["", "", now.date().isoformat(), "HTTP %s" % rr.status_code]
        done += 1
        if done % 200 == 0:
            _write(j(CACHE), cache)
    _write(j(CACHE), cache)
    inc, exc = set(cfg["sic_include"]), set(cfg["sic_exclude"])
    out, sic_report = [], {}
    for c, x in want.items():
        sic = (cache.get(c) or ["", ""])[0]
        desc = (cache.get(c) or ["", ""])[1]
        t = str(x[it]).upper()
        src = None
        if sic in inc and sic not in exc and x[iex] in cfg["exchanges"]:
            src = "sic"
        if t in core:
            src = src or "core"
        if t in seed:
            src = src or "seed"
        if not src:
            continue
        out.append([int(c), t, x[inm], sic, x[iex], src])
        if src == "sic":
            r_ = sic_report.setdefault(sic, {"desc": desc, "n": 0})
            r_["n"] += 1
    out.sort(key=lambda r: (r[3], r[1]))
    for code in cfg["sic_include"]:
        sic_report.setdefault(code, {"desc": None, "n": 0, "note": "清單中沒有這個代碼的公司(說明文字無法核對)"})
    res = {"generated": now.isoformat(timespec="seconds"), "fields": ["cik", "ticker", "name", "sic", "exchange", "source"],
           "n": len(out), "pending_sic_lookup": len([c for c in want if c not in cache]), "stopped": stopped,
           "sic_report": dict(sorted(sic_report.items())), "companies": out}
    if len(out) > cfg["universe_warn"]:
        res["warning"] = "公司數 %d 超過 %d;不截斷,請檢查 sic_report" % (len(out), cfg["universe_warn"])
    _write(j(OUT), res)
    return res


def _write(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def main():
    try:
        res = build(eh.Edgar())
    except eh.MissingUA as e:
        print(str(e))
        return 2
    except eh.EdgarAbort as e:
        print("EDGAR 停止:%s" % e)
        return 2
    print(json.dumps({k: res[k] for k in ("n", "pending_sic_lookup", "stopped", "sic_report")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
