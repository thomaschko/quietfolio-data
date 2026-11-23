# -*- coding: utf-8 -*-
"""
af_daily.py — 反脆弱選股模組:官方免費資料每日累積(2026-11-23)v2026-11-23-init
============================================================
探測結果(2026-11-23,af_probe.py):下列官方 OpenAPI 只提供「當期 / 當日」,沒有歷史 → 每個交易日累積:
  insider_monthly  董監事持股餘額明細(證交所 t187ap11_L、櫃買 mopsfin_t187ap11_O):每月一次快照
                   → 每檔董事、監察人(含本人與法人)目前持股合計、設質股數合計
  insider_transfer 內部人持股轉讓事前申報(t187ap12_L、mopsfin_t187ap12_O):當日申報 → 累積(去重)
  buyback          重大訊息(t187ap04_L、mopsfin_t187ap04_O)標題含「買回本公司股份」→ 累積(去重)
  per              每日本益比 / 股價淨值比 / 殖利率(BWIBBU_ALL、tpex_mainboard_peratio_analysis)
                   → af_data/per_daily.csv.gz(每檔每週留最後一個交易日;計算時與 FinMind 回補的 per.csv.gz 合併)
  company          公司基本資料(t187ap03_L、mopsfin_t187ap03_O):成立日期、上市日期、產業別、已發行股數(覆蓋)
累積起始日記在 af_data/daily_meta.json;「近 60 日 / 近 12 個月」類指標在累積期間不足前一律輸出 null。
限速:請求間隔 3 秒;HTTP 非 200 或解析失敗 → 該來源今天跳過、保留舊資料。
"""
import csv
import datetime as dt
import gzip
import json
import os
import re
import sys
import time

import requests

from af_fin_data import OUT_DIR, load_rows, save_rows, HEADERS as FIN_HEADERS, week_key

VERSION = "v2026-11-23-init"
UA = {"User-Agent": "quietfolio-af/1.0 (read-only research)"}
TW = "https://openapi.twse.com.tw/v1/"
TP = "https://www.tpex.org.tw/openapi/v1/"
HEADERS = {
    "insider_monthly": ["ym", "code", "dir_shares", "dir_pledged", "n_people"],
    "insider_transfer": ["date", "code", "role", "name", "method", "shares"],
    "buyback": ["date", "code", "title"],
    "company": ["code", "market", "founded", "listed", "industry_code", "issued_shares"],
    "per_daily": ["code", "date", "per", "pbr", "dy"],
}
DIR_ROLE = re.compile(r"董事|監察人")


def _get(s, url, sleep=time.sleep):
    sleep(3)
    try:
        r = s.get(url, timeout=60)
        if r.status_code != 200:
            print("  HTTP", r.status_code, url)
            return None
        return r.json()
    except Exception as e:
        print("  ERR", type(e).__name__, url)
        return None


def num(v):
    try:
        s = str(v).replace(",", "").replace("%", "").strip()
        return float(s) if s not in ("", "-", "－") else None
    except (TypeError, ValueError):
        return None


def roc_date(s):
    """1151120 / 115/11/20 → 2026-11-20"""
    s = re.sub(r"\D", "", str(s or ""))
    if len(s) == 7:
        return f"{int(s[:3]) + 1911:04d}-{s[3:5]}-{s[5:7]}"
    if len(s) == 8:
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return None


def pick(r, *keys):
    for k in keys:
        if k in r and r[k] not in (None, ""):
            return r[k]
    return None


def parse_insider(rows):
    """→ {(ym, code): [dir_shares, dir_pledged, n_people]}(只計董事、監察人;本人與法人都算,不含關係人欄)"""
    out = {}
    for r in rows or []:
        code = str(pick(r, "公司代號", "SecuritiesCompanyCode") or "").strip()
        role = str(pick(r, "職稱", "Title") or "")
        if not re.fullmatch(r"\d{4}", code) or not DIR_ROLE.search(role):
            continue
        ym = re.sub(r"\D", "", str(pick(r, "資料年月", "DataYearMonth") or ""))
        if len(ym) == 5:
            ym = f"{int(ym[:3]) + 1911:04d}-{ym[3:5]}"
        else:
            continue
        sh = num(pick(r, "目前持股", "CurrentShareholding")) or 0
        pl = num(pick(r, "設質股數", "PledgedShares")) or 0
        a = out.setdefault((ym, code), [0.0, 0.0, 0])
        a[0] += sh
        a[1] += pl
        a[2] += 1
    return out


def parse_transfer(rows):
    out = set()
    for r in rows or []:
        code = str(pick(r, "公司代號", "SecuritiesCompanyCode") or "").strip()
        if not re.fullmatch(r"\d{4}", code):
            continue
        d = roc_date(pick(r, "出表日期", "Date"))
        role = str(pick(r, "申報人身分", "IdentityOfDeclarant") or "")
        name = str(pick(r, "姓名", "Name") or "")
        method = str(pick(r, "預定轉讓方式及股數-轉讓方式", "TransferMethod") or "")
        sh = num(pick(r, "預定轉讓總股數-自有持股", "預定轉讓方式及股數-轉讓股數"))
        if d and sh:
            out.add((d, code, role, name, method, sh))
    return out


def _field(keys, *subs):
    """依欄位名(中英文、忽略空白)找第一個符合的欄位(與 event_theme_radar._mops_index 同法)"""
    for k in keys:
        kk = str(k).replace(" ", "").lower()
        if any(s in kk for s in subs):
            return k
    return None


def parse_buyback(rows):
    out = set()
    if not rows:
        return out
    keys = list(rows[0].keys())
    kc = _field(keys, "公司代號", "securitiescompanycode", "companycode")
    ks = _field(keys, "主旨", "subject", "title", "說明")
    kd = _field(keys, "發言日期", "spokesmandate", "date", "日期")
    for r in rows:
        code = str(r.get(kc) or "").strip()
        title = str(r.get(ks) or "")
        d = roc_date(r.get(kd))
        if re.fullmatch(r"\d{4}", code) and d and "買回本公司股份" in title:
            out.add((d, code, title.strip()[:100]))
    return out


def parse_per(rows, market):
    """→ [(code, date, per, pbr, dy)];本益比空白(虧損)記 None"""
    out = []
    for r in rows or []:
        if market == "twse":
            code, d = r.get("Code"), roc_date(r.get("Date"))
            pe, pb, dy = num(r.get("PEratio")), num(r.get("PBratio")), num(r.get("DividendYield"))
        else:
            code, d = r.get("SecuritiesCompanyCode"), roc_date(r.get("Date"))
            pe, pb, dy = num(r.get("PriceEarningRatio")), num(r.get("PriceBookRatio")), num(r.get("YieldRatio"))
        if code and re.fullmatch(r"\d{4}", str(code)) and d:
            out.append((str(code), d, pe, pb, dy))
    return out


def parse_company(rows, market):
    out = {}
    for r in rows or []:
        code = str(pick(r, "公司代號", "SecuritiesCompanyCode") or "").strip()
        if not re.fullmatch(r"\d{4}", code):
            continue
        out[code] = [code, market, roc_date(pick(r, "成立日期", "DateOfIncorporation")) or "",
                     roc_date(pick(r, "上市日期", "DateOfListing")) or "",
                     str(pick(r, "產業別", "SecuritiesIndustryCode") or ""),
                     num(pick(r, "已發行普通股數或TDR原股發行股數", "IssueShares")) or ""]
    return out


def merge_per(existing, new_rows):
    """併入每日估值,每檔每週只留最後一個交易日。existing: [[code,date,per,pbr,dy]]"""
    by = {}
    for r in existing:
        by[(r[0], week_key(r[1]))] = r
    for c, d, pe, pb, dy in new_rows:
        k = (c, week_key(d))
        if k not in by or by[k][1] <= d:
            by[k] = [c, d, "" if pe is None else pe, "" if pb is None else pb, "" if dy is None else dy]
    return [by[k] for k in sorted(by, key=lambda k: (k[0], k[1]))]


def run(out_dir=OUT_DIR, session=None, sleep=time.sleep, today=None):
    s = session or requests.Session()
    s.headers.update(UA)
    today = today or (dt.datetime.utcnow() + dt.timedelta(hours=8)).date()
    os.makedirs(out_dir, exist_ok=True)
    mp = os.path.join(out_dir, "daily_meta.json")
    try:
        meta = json.load(open(mp, encoding="utf-8"))
    except (OSError, ValueError):
        meta = {}
    log = {"version": VERSION, "date": today.isoformat()}

    # 董監持股(月)
    ins = {(r[0], r[1]): r for r in load_rows("insider_monthly", out_dir)}
    got = {}
    for url in (TW + "opendata/t187ap11_L", TP + "mopsfin_t187ap11_O"):
        got.update(parse_insider(_get(s, url, sleep)))
    for (ym, c), (sh, pl, n) in got.items():
        ins[(ym, c)] = [ym, c, sh, pl, n]
    if got:
        save_rows("insider_monthly", [ins[k] for k in sorted(ins)], out_dir)
        meta.setdefault("insider_monthly_since", min(k[0] for k in got))
    log["insider_monthly"] = len(got)

    # 內部人轉讓申報(日)
    tr = {tuple(r) for r in load_rows("insider_transfer", out_dir)}
    new = set()
    for url in (TW + "opendata/t187ap12_L", TP + "mopsfin_t187ap12_O"):
        j = _get(s, url, sleep)
        if j is not None:
            new |= {tuple(str(x) for x in t) for t in parse_transfer(j)}
            meta.setdefault("insider_transfer_since", today.isoformat())
    tr |= new
    save_rows("insider_transfer", sorted(tr), out_dir)
    log["insider_transfer_new"] = len(new)

    # 庫藏股(重大訊息標題)
    bb = {tuple(r) for r in load_rows("buyback", out_dir)}
    new = set()
    for url in (TW + "opendata/t187ap04_L", TP + "mopsfin_t187ap04_O"):
        j = _get(s, url, sleep)
        if j is not None:
            new |= parse_buyback(j)
            meta.setdefault("buyback_since", today.isoformat())
    bb |= new
    save_rows("buyback", sorted(bb), out_dir)
    log["buyback_new"] = len(new)

    # 每日估值 → per(週取樣)
    per_new = parse_per(_get(s, TW + "exchangeReport/BWIBBU_ALL", sleep), "twse") + \
        parse_per(_get(s, TP + "tpex_mainboard_peratio_analysis", sleep), "tpex")
    if per_new:
        save_rows("per_daily", merge_per(load_rows("per_daily", out_dir), per_new), out_dir)   # 與 af_fin_data 的 per 分檔,避免兩條排程互相衝突
    log["per_rows"] = len(per_new)

    # 公司基本資料(覆蓋)
    comp = {}
    comp.update(parse_company(_get(s, TW + "opendata/t187ap03_L", sleep), "twse"))
    comp.update(parse_company(_get(s, TP + "mopsfin_t187ap03_O", sleep), "tpex"))
    if len(comp) > 1500:
        save_rows("company", [comp[c] for c in sorted(comp)], out_dir)
    log["company"] = len(comp)

    meta["last_run"] = log
    json.dump(meta, open(mp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps(log, ensure_ascii=False))
    return log


# 與 af_fin_data 共用 save_rows / load_rows 的欄位表
FIN_HEADERS.update(HEADERS)

if __name__ == "__main__":
    run()
    sys.exit(0)
