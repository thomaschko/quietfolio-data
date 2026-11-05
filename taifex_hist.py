# -*- coding: utf-8 -*-
"""
taifex_hist.py — 期交所期貨 / 選擇權歷史與每日更新(2026-11-04,共識頁 Phase 1)
============================================================
目的:市場氛圍面板用來觀察「散戶行為(微型臺指)」與「外資動態」,判斷偏熱或過度看空(只描述,不給買賣建議)。
來源(全部期交所官網公開 CSV 下載,Phase 0 探測確認可指定日期區間、回溯到 2024/08 微臺上市):
  futContractsDateDown   三大法人 期貨分契約(TXF 臺指期、MXF 小臺、TMF 微臺)
  callsAndPutsDateDown   三大法人 選擇權買賣權分計(TXO)
  futDataDown            期貨每日行情(TX / MTX / TMF 各月份未沖銷 → 全市場未平倉)
  pcRatioDown            臺指選擇權 Put/Call Ratio
  largeTraderFutDown     大額交易人未沖銷部位(只留 TX / MTX / TMF)
做法:以「月」為單位下載(pcRatio 一次最多約 1 個月),2024-08 起;已完成的月份不重抓,本月與上月每次重抓。
規則:robots.txt 先讀;同一主機請求間隔 ≥ 3 秒;誠實 User-Agent;403 / 429 / 驗證碼 / robots 不允許 → 停止(結束碼 2);
      連線中斷等 60 秒重抓 1 次,仍失敗 → 結束碼 4;回應是 HTML 錯誤頁 → 這個月份記為錯誤、停止這次(結束碼 2),不換來源。
      缺日不補、不插值。
輸出(taifex_hist/,CSV,依鍵去重排序):
  fut_inst.csv   date,contract,who,long_oi,short_oi,net_oi,net_oi_k,net_trade
  opt_inst.csv   date,cp,who,long_oi,short_oi,net_oi,net_oi_k
  fut_oi.csv     date,contract,total_oi(一般交易時段各月份未沖銷合計)
  pcr.csv        date,put_vol,call_vol,pc_vol_pct,put_oi,call_oi,pc_oi_pct
  large.csv      date,contract,month,trader_type,top5_buy,top5_sell,top10_buy,top10_sell,market_oi
  progress.json  已完成月份、每次執行紀錄
結束碼:0 = 完成;2 = 被擋 / 錯誤頁而停止;4 = 連線中斷而停止。
"""
import csv
import datetime as dt
import io
import json
import os
import re
import sys
import time
from urllib import robotparser
from urllib.parse import urlsplit

import requests

UA = "quietfolio-taifex/1.0 (read-only research; github.com/thomaschko/quietfolio-data)"
OUT_DIR = "taifex_hist"
BASE = "https://www.taifex.com.tw/cht/3/"
GAP = 3.0
NET_RETRY_WAIT = 60
START = (2024, 8)
TPE = dt.timezone(dt.timedelta(hours=8))

# (dataset, 端點, 表單 commodity 參數名, [commodity 值])
DATASETS = [
    ("fut_inst", "futContractsDateDown", "commodityId", ["TXF", "MXF", "TMF"]),
    ("opt_inst", "callsAndPutsDateDown", "commodityId", ["TXO"]),
    ("fut_oi", "futDataDown", "commodity_id", ["TX", "MTX", "TMF"]),
    ("pcr", "pcRatioDown", None, [None]),
    ("large", "largeTraderFutDown", None, [None]),
]
FUT_CODE = {"TXF": "TX", "MXF": "MTX", "TMF": "TMF"}
LARGE_KEEP = {"TX", "MTX", "TMF"}
HEADERS = {
    "fut_inst": (["date", "contract", "who", "long_oi", "short_oi", "net_oi", "net_oi_k", "net_trade"], ["date", "contract", "who"]),
    "opt_inst": (["date", "cp", "who", "long_oi", "short_oi", "net_oi", "net_oi_k"], ["date", "cp", "who"]),
    "fut_oi": (["date", "contract", "total_oi"], ["date", "contract"]),
    "pcr": (["date", "put_vol", "call_vol", "pc_vol_pct", "put_oi", "call_oi", "pc_oi_pct"], ["date"]),
    "large": (["date", "contract", "month", "trader_type", "top5_buy", "top5_sell", "top10_buy", "top10_sell", "market_oi"],
              ["date", "contract", "month", "trader_type"]),
}


class Blocked(Exception):
    pass


class NetError(Blocked):
    pass


class Http:
    def __init__(self, session=None, sleep=time.sleep, clock=time.time):
        self.s = session or requests.Session()
        try:
            self.s.headers.update({"User-Agent": UA})
        except AttributeError:
            pass
        self.sleep, self.clock = sleep, clock
        self.last, self.robots = {}, {}
        self.count, self.net_retries = 0, 0

    def _raw(self, method, url, data=None):
        host = urlsplit(url).netloc
        if host in self.last:
            w = GAP - (self.clock() - self.last[host])
            if w > 0:
                self.sleep(w)
        try:
            return self.s.request(method, url, data=data, timeout=90)
        finally:
            self.last[host] = self.clock()
            self.count += 1

    def _robots_ok(self, url):
        p = urlsplit(url)
        if p.netloc not in self.robots:
            r = self._raw("GET", "%s://%s/robots.txt" % (p.scheme, p.netloc))
            if r.status_code == 200:
                rp = robotparser.RobotFileParser()
                rp.parse(r.text.splitlines())       # 期交所回 HTML 404 頁 → 沒有任何規則
                self.robots[p.netloc] = rp
            elif r.status_code == 404:
                self.robots[p.netloc] = None
            else:
                self.robots[p.netloc] = False
        rp = self.robots[p.netloc]
        if rp is None:
            return True
        if rp is False:
            return False
        return rp.can_fetch(UA, url) and rp.can_fetch("*", url)

    def post_csv(self, url, form):
        """→ 解碼後的 CSV 文字;擋 / 錯誤頁 → Blocked;連線中斷重抓 1 次仍失敗 → NetError"""
        try:
            ok = self._robots_ok(url)
        except requests.RequestException as e:
            raise NetError("連線中斷 %s(robots.txt)" % type(e).__name__)
        if not ok:
            raise Blocked("robots.txt 不允許:" + url)
        try:
            r = self._raw("POST", url, form)
        except requests.RequestException as e:
            self.net_retries += 1
            self.sleep(NET_RETRY_WAIT)
            try:
                r = self._raw("POST", url, form)
            except requests.RequestException as e2:
                raise NetError("連線中斷 %s(重抓 1 次仍失敗,第一次 %s):%s" % (type(e2).__name__, type(e).__name__, url))
        if r.status_code in (403, 429) or r.status_code != 200:
            raise Blocked("HTTP %s:%s" % (r.status_code, url))
        text = r.content.decode("cp950", errors="replace")
        head = text.lstrip()[:400].lower()
        if "captcha" in head or "驗證碼" in head:
            raise Blocked("驗證碼頁:" + url)
        if head.startswith(("<!doctype", "<html", "<head")):
            raise Blocked("回應是 HTML 錯誤頁(可能日期區間不被接受):%s %s" % (url, json.dumps(form, ensure_ascii=False)))
        return text


# ------------------------------------------------------------------ 解析
def num(x):
    s = str(x or "").replace(",", "").replace("%", "").strip()
    if s in ("", "-", "--"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def iso(d):
    a = re.findall(r"\d+", str(d))
    return "%04d-%02d-%02d" % (int(a[0]), int(a[1]), int(a[2])) if len(a) >= 3 else None


def rows_of(text):
    out = []
    for r in csv.reader(io.StringIO(text)):
        r = [x.strip() for x in r]
        if r and iso(r[0]) and re.match(r"^\d{4}/\d{1,2}/\d{1,2}$", r[0]):
            out.append(r)
    return out


def _f(v):
    if v is None:
        return ""
    return str(int(v)) if float(v).is_integer() else repr(float(v))


def parse_fut_inst(text, commodity):
    code = FUT_CODE[commodity]
    return [(iso(r[0]), code, r[2], _f(num(r[9])), _f(num(r[11])), _f(num(r[13])), _f(num(r[14])), _f(num(r[7])))
            for r in rows_of(text) if len(r) >= 15]


def parse_opt_inst(text, commodity=None):
    return [(iso(r[0]), r[2].upper(), r[3], _f(num(r[10])), _f(num(r[12])), _f(num(r[14])), _f(num(r[15])))
            for r in rows_of(text) if len(r) >= 16]


def parse_fut_oi(text, commodity):
    tot = {}
    for r in rows_of(text):
        if len(r) < 18 or r[1] != commodity or r[17] != "一般" or "/" in r[2]:
            continue
        v = num(r[11])
        if v is not None:
            tot[iso(r[0])] = tot.get(iso(r[0]), 0) + v
    return [(d, commodity, _f(v)) for d, v in sorted(tot.items())]


def parse_pcr(text, commodity=None):
    return [(iso(r[0]),) + tuple(_f(num(x)) for x in r[1:7]) for r in rows_of(text) if len(r) >= 7]


def parse_large(text, commodity=None):
    out = []
    for r in rows_of(text):
        if len(r) < 10 or r[1].strip() not in LARGE_KEEP:
            continue
        out.append((iso(r[0]), r[1].strip(), r[3].strip(), r[4].strip()) + tuple(_f(num(x)) for x in r[5:10]))
    return out


def drop_unpublished(rows, first_num=3):
    """2026-11-04:當天三大法人資料公布前(約 15:00)下載,官網會回該日每列都是 0 → 視為尚未公布,不寫入(下次重抓)。"""
    by = {}
    for r in rows:
        by.setdefault(r[0], []).append(r)
    bad = {d for d, rs in by.items() if all(all(x in ("", "0") for x in r[first_num:]) for r in rs)}
    return [r for r in rows if r[0] not in bad], sorted(bad)


PARSERS = {"fut_inst": parse_fut_inst, "opt_inst": parse_opt_inst, "fut_oi": parse_fut_oi, "pcr": parse_pcr, "large": parse_large}


# ------------------------------------------------------------------ 儲存
def read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def merge_csv(path, header, keys, rows, drop_dates=()):
    drop = set(drop_dates)
    m = {tuple(r[k] for k in keys): [r[h] for h in header] for r in read_csv(path) if r["date"] not in drop}
    idx = [header.index(k) for k in keys]
    for r in rows:
        r = [str(x) for x in r]
        m[tuple(r[i] for i in idx)] = r
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(header)
        w.writerows(m[k] for k in sorted(m))
    os.replace(tmp, path)
    return len(m)


def months(start, today):
    y, m = start
    out = []
    while (y, m) <= (today.year, today.month):
        out.append((y, m))
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def month_range(y, m, today):
    a = dt.date(y, m, 1)
    b = (dt.date(y + (m == 12), m % 12 + 1, 1) - dt.timedelta(days=1))
    return a, min(b, today)


PUBLISH_HOUR = 15   # 期交所盤後資料約 15:00 後公布


def last_available(now):
    """查詢結束日:15 點前用前一天。2026-11-05:午夜後執行時結束日 = 當天(還沒有資料)→ 官網回 HTML 錯誤頁,整批停止。"""
    return now.date() if now.hour >= PUBLISH_HOUR else now.date() - dt.timedelta(days=1)


def run(http=None, out_dir=OUT_DIR, today=None, max_months=None, now=None):
    http = http or Http()
    if today is None:
        today = last_available(now or dt.datetime.now(TPE))
    os.makedirs(out_dir, exist_ok=True)
    pp = os.path.join(out_dir, "progress.json")
    try:
        with open(pp, encoding="utf-8") as f:
            prog = json.load(f)
    except (OSError, ValueError):
        prog = {"done": {}, "runs": []}
    log = {"started": dt.datetime.now(TPE).isoformat(timespec="seconds"), "fetched": [], "rows": {}}
    ms = months(START, today)
    recent = set(ms[-2:])            # 本月、上月每次重抓(月中資料會增加)
    code = 0
    todo = [(y, m) for (y, m) in ms if (y, m) in recent or any(
        "%04d-%02d" % (y, m) not in prog["done"].get(ds, []) for ds, *_ in DATASETS)]
    if max_months:
        todo = todo[:max_months]
    try:
        for y, m in todo:
            key = "%04d-%02d" % (y, m)
            a, b = month_range(y, m, today)
            for ds, ep, pname, values in DATASETS:
                if key in prog["done"].get(ds, []) and (y, m) not in recent:
                    continue
                rows = []
                for v in values:
                    form = {"queryStartDate": a.strftime("%Y/%m/%d"), "queryEndDate": b.strftime("%Y/%m/%d")}
                    if ds == "fut_oi":
                        form["down_type"] = "1"
                    if pname:
                        form[pname] = v
                    text = http.post_csv(BASE + ep, form)
                    rows += PARSERS[ds](text, v)
                bad = []
                if ds in ("fut_inst", "opt_inst"):
                    rows, bad = drop_unpublished(rows)
                    if bad:
                        log.setdefault("unpublished", {})[ds] = bad
                header, keys = HEADERS[ds]
                merge_csv(os.path.join(out_dir, ds + ".csv"), header, keys, rows, drop_dates=bad)   # 先前存的 0 也清掉
                log["rows"][ds] = log["rows"].get(ds, 0) + len(rows)
                if key not in prog["done"].setdefault(ds, []):
                    prog["done"][ds].append(key)
            log["fetched"].append(key)
    except Blocked as e:
        log["stopped"] = str(e)
        code = 4 if isinstance(e, NetError) else 2
    log.update({"requests": http.count, "net_retries": http.net_retries})
    prog["runs"] = (prog.get("runs") or [])[-60:] + [log]
    for ds in prog["done"]:
        prog["done"][ds] = sorted(set(prog["done"][ds]))
    tmp = pp + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(prog, f, ensure_ascii=False, indent=1)
    os.replace(tmp, pp)
    print(json.dumps(log, ensure_ascii=False))
    return code, log


if __name__ == "__main__":
    sys.exit(run()[0])
