# -*- coding: utf-8 -*-
"""
margin_backfill.py — 融資占比歷史回補(2026-11-01,Phase 1.5)
============================================================
來源(全部官方、免費;Phase 0 探測確認可回溯到 2024-11):
  交易日與指數:上市 TWSE rwd TAIEX/MI_5MINS_HIST(月)、上櫃 TPEx www indexInfo/inx(月)
  上市:rwd marginTrading/MI_MARGN(個股融資餘額 + 信用交易統計)、rwd afterTrading/MI_INDEX type=ALLBUT0999(收盤)
  上櫃:www margin/balance(個股融資餘額 + 合計 / 融資金)、www afterTrading/dailyQuotes(收盤)
  ※ 櫃買舊版 stk_quote_result.php 會忽略日期、回最新一天(探測時查 10/23 回 10/30),不用。
    每個回應都核對回應內的資料日,和查詢日不同 → 當作缺日,不存。
規則:
  - 每個主機先讀 robots.txt;請求間隔 ≥ 3 秒;誠實 User-Agent。
  - 403 / 429 / 驗證碼 / 回應不是 JSON → 停止這一批、寫進度,不重試、不換來源。
  - 每批 ≤ 150 個交易日;以 (market, date) 為鍵斷點續跑,已完成(含確認缺日)的跳過。
  - 缺日不補:缺融資或缺收盤的 (date, market, code) 寫進 missing.csv;整天缺 code 記為 *。
  - margin_hist 累計超過 50 MB 前停止並回報,不自行刪減。
輸出(margin_hist/):
  YYYY.csv.gz   date,market,code,margin_lots,close(close 缺 → 空白)
  official.csv  date,market,total_lots,official_margin_k(官方合計;找不到 → 空白)
  index.csv     date,market,close(加權指數 / 櫃買指數)
  missing.csv   date,market,code,reason
  progress.json 已完成的 (market, date)、每批紀錄、停止原因
結束碼:0 = 這批完成(可能還有剩)、10 = 全部完成、2 = 被限流 / 封鎖而停止、3 = 超過大小上限、
      4 = 連線中斷(回應傳到一半被切斷、逾時、連線重置;同一網址等 60 秒重抓 1 次仍失敗)而停止這一批
  2026-11-01 使用者同意:連線中斷不視為限流,workflow 等 5 分鐘後接著跑,每次執行最多 3 次;
  403 / 429 / 驗證碼 / robots 不允許仍照規格立即停止、不重試。
"""
import csv
import datetime as dt
import gzip
import io
import json
import os
import re
import sys
import time
from urllib import robotparser
from urllib.parse import urlsplit

import requests

UA = "quietfolio-margin-backfill/1.0 (read-only research; github.com/thomaschko/quietfolio-data)"
OUT_DIR = "margin_hist"
MIN_GAP = 3.0
NET_RETRY_WAIT = 60
BATCH_DAYS = 150
TARGET_DAYS = 490
MAX_BYTES = 50 * 1024 * 1024
TPE = dt.timezone(dt.timedelta(hours=8))
MARKETS = ("twse", "tpex")

TWSE_INDEX = "https://www.twse.com.tw/rwd/zh/TAIEX/MI_5MINS_HIST?response=json&date={ym}01"
TPEX_INDEX = "https://www.tpex.org.tw/www/zh-tw/indexInfo/inx?response=json&date={y}/{m:02d}/01"
TWSE_MARGIN = "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?response=json&selectType=ALL&date={ymd}"
TWSE_CLOSE = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?response=json&type=ALLBUT0999&date={ymd}"
TPEX_MARGIN = "https://www.tpex.org.tw/www/zh-tw/margin/balance?response=json&date={slash}"
TPEX_CLOSE = "https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes?response=json&date={slash}"


class Blocked(Exception):
    pass


class NetError(Blocked):
    """連線中斷(不是限流):這一批停止,結束碼 4"""
    pass


class Http:
    def __init__(self, session=None, sleep=time.sleep, clock=time.time, gap=MIN_GAP):
        self.s = session or requests.Session()
        try:
            self.s.headers.update({"User-Agent": UA})
        except AttributeError:
            pass
        self.sleep, self.clock, self.gap = sleep, clock, gap
        self.last = {}
        self.robots = {}
        self.robots_info = {}
        self.count = 0
        self.net_retries = 0

    def _wait(self, host):
        if host in self.last:
            w = self.gap - (self.clock() - self.last[host])
            if w > 0:
                self.sleep(w)

    def _raw(self, url, follow=False):
        host = urlsplit(url).netloc
        self._wait(host)
        try:
            return self.s.get(url, timeout=60, allow_redirects=follow)
        finally:
            self.last[host] = self.clock()
            self.count += 1

    def _robots_ok(self, url):
        p = urlsplit(url)
        if p.netloc not in self.robots:
            # 2026-11-01:robots.txt 要跟隨轉址(櫃買的 robots.txt 會轉址;不跟隨 → 3xx 被誤判成不允許)
            r = self._raw(f"{p.scheme}://{p.netloc}/robots.txt", follow=True)
            self.robots_info[p.netloc] = "HTTP %s" % r.status_code
            if r.status_code == 404:
                self.robots[p.netloc] = None
            elif r.status_code == 200:
                rp = robotparser.RobotFileParser()
                rp.parse(r.text.splitlines())
                self.robots[p.netloc] = rp
                self.robots_info[p.netloc] += ";" + " | ".join(
                    x.strip() for x in r.text.splitlines() if x.strip().lower().startswith(("user-agent", "disallow")))[:300]
            else:
                self.robots[p.netloc] = False
        rp = self.robots[p.netloc]
        if rp is None:
            return True
        if rp is False:
            return False
        return rp.can_fetch(UA, url) and rp.can_fetch("*", url)

    def json(self, url):
        """→ dict;403/429/驗證碼/非 JSON/robots 不允許 → Blocked(呼叫端停止整批)"""
        try:
            ok = self._robots_ok(url)
        except requests.RequestException as e:
            raise NetError("連線中斷 %s(robots.txt):%s" % (type(e).__name__, url))
        if not ok:
            raise Blocked("robots.txt 不允許(%s):%s" % (self.robots_info.get(urlsplit(url).netloc), url))
        try:
            r = self._raw(url)
        except requests.RequestException as e:
            # 2026-11-01 使用者同意:連線中斷(不是限流)→ 等 60 秒、同一網址只重抓 1 次;再失敗才停止這一批
            self.net_retries += 1
            self.sleep(NET_RETRY_WAIT)
            try:
                r = self._raw(url)
            except requests.RequestException as e2:
                raise NetError("連線中斷 %s(已重抓 1 次仍失敗,第一次 %s):%s" % (type(e2).__name__, type(e).__name__, url))
        if r.status_code in (403, 429):
            raise Blocked("HTTP %s:%s" % (r.status_code, url))
        if r.status_code != 200:
            raise Blocked("HTTP %s:%s" % (r.status_code, url))
        try:
            return r.json()
        except ValueError:
            head = r.text[:300]
            raise Blocked(("驗證碼頁" if ("captcha" in head.lower() or "驗證碼" in head) else "回應不是 JSON") + ":" + url)


# ------------------------------------------------------------------ 解析
def num(x):
    if x is None:
        return None
    s = re.sub(r"<[^>]+>", "", str(x)).replace(",", "").strip()
    if s in ("", "--", "---", "----", "除權息", "X"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def roc_date(s):
    """'115/10/01' 或 '2026/10/01' → '2026-10-01'"""
    a = [int(x) for x in re.findall(r"\d+", str(s))[:3]]
    if len(a) != 3:
        return None
    y = a[0] + 1911 if a[0] < 1911 else a[0]
    return "%04d-%02d-%02d" % (y, a[1], a[2])


def resp_date(j):
    d = str(j.get("date") or "")
    return "%s-%s-%s" % (d[:4], d[4:6], d[6:8]) if re.fullmatch(r"\d{8}", d) else None


def parse_twse_index(j):
    if str(j.get("stat", "")).upper() != "OK":
        return []
    return [(roc_date(r[0]), num(r[4])) for r in j.get("data") or [] if roc_date(r[0]) and num(r[4])]


def parse_tpex_index(j):
    out = []
    for t in j.get("tables") or []:
        f = t.get("fields") or []
        if "日期" in f and "收市" in f:
            i, k = f.index("日期"), f.index("收市")
            out += [(roc_date(r[i]), num(r[k])) for r in t.get("data") or [] if roc_date(r[i]) and num(r[k])]
    return out


def parse_twse_margin(j):
    """→ (margin {代號: 張}, total_lots, official_k);沒有個股表 → ({}, None, None)"""
    margin, tl, tk = {}, None, None
    for t in j.get("tables") or []:
        f = t.get("fields") or []
        fs = ",".join(f)
        if "代號" in fs and "今日餘額" in fs:
            ic = next(i for i, x in enumerate(f) if "代號" in x)
            it = next(i for i, x in enumerate(f) if "今日餘額" in x)     # 第一個是融資
            for r in t.get("data") or []:
                c = str(r[ic]).strip()
                if c:
                    margin[c] = num(r[it])
        elif "項目" in fs and "今日餘額" in fs:
            it = f.index("今日餘額")
            for r in t.get("data") or []:
                if "融資" in str(r[0]) and "交易單位" in str(r[0]):
                    tl = num(r[it])
                elif "融資金額" in str(r[0]):
                    tk = num(r[it])
    return margin, tl, tk


def parse_twse_close(j):
    out = {}
    for t in j.get("tables") or []:
        f = t.get("fields") or []
        if "證券代號" in f and "收盤價" in f:
            ic, ix = f.index("證券代號"), f.index("收盤價")
            for r in t.get("data") or []:
                v = num(r[ix])
                if v:
                    out[str(r[ic]).strip()] = v
    return out


def parse_tpex_margin(j):
    margin, tl, tk = {}, None, None
    for t in j.get("tables") or []:
        f = t.get("fields") or []
        if "代號" in f and "資餘額" in f:
            ic, ib = f.index("代號"), f.index("資餘額")
            for r in t.get("data") or []:
                c = str(r[ic]).strip()
                if c:
                    margin[c] = num(r[ib])
            for r in t.get("summary") or []:
                label = "".join(str(x) for x in r[:2])
                if "合計" in label:
                    tl = num(r[ib])
                elif "融資金" in label:
                    tk = num(r[ib])
    return margin, tl, tk


def parse_tpex_close(j):
    out = {}
    for t in j.get("tables") or []:
        f = t.get("fields") or []
        if "代號" in f and "收盤" in f:
            ic, ix = f.index("代號"), f.index("收盤")
            for r in t.get("data") or []:
                v = num(r[ix])
                if v:
                    out[str(r[ic]).strip()] = v
    return out


# ------------------------------------------------------------------ 儲存
def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def read_csv(path, gz=False):
    if not os.path.exists(path):
        return []
    op = gzip.open if gz else open
    with op(path, "rt", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path, header, rows, gz=False):
    tmp = path + ".tmp"
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    w.writerows(rows)
    data = buf.getvalue().encode("utf-8")
    if gz:
        with open(tmp, "wb") as f:
            with gzip.GzipFile(fileobj=f, mode="wb", mtime=0) as g:
                g.write(data)
    else:
        with open(tmp, "wb") as f:
            f.write(data)
    os.replace(tmp, path)


def _fmt(v):
    if v is None:
        return ""
    return str(int(v)) if float(v).is_integer() else repr(float(v))


def merge_rows(path, header, key_cols, new_rows, gz=False):
    """以 key_cols 去重合併(新資料覆蓋同鍵舊資料),依鍵排序寫回。"""
    old = read_csv(path, gz)
    idx = [header.index(k) for k in key_cols]
    m = {tuple(r[k] for k in key_cols): [r[h] for h in header] for r in old}
    for r in new_rows:
        r = [str(x) for x in r]
        m[tuple(r[i] for i in idx)] = r
    write_csv(path, header, [m[k] for k in sorted(m)], gz)
    return len(m)


def dir_bytes(d):
    return sum(os.path.getsize(os.path.join(d, n)) for n in os.listdir(d)) if os.path.isdir(d) else 0


# ------------------------------------------------------------------ 主流程
class Store:
    def __init__(self, out_dir=OUT_DIR):
        self.dir = out_dir
        os.makedirs(out_dir, exist_ok=True)
        self.p = os.path.join(out_dir, "progress.json")
        self.prog = _read_json(self.p, {"done": {m: {} for m in MARKETS}, "batches": [], "index_months": []})
        self.rows, self.official, self.index, self.missing = [], [], [], []

    def done(self, market, date):
        return date in self.prog["done"].setdefault(market, {})

    def mark(self, market, date, status):
        self.prog["done"].setdefault(market, {})[date] = status

    def flush(self):
        by_year = {}
        for r in self.rows:
            by_year.setdefault(r[0][:4], []).append(r)
        for y, rows in by_year.items():
            merge_rows(os.path.join(self.dir, y + ".csv.gz"), ["date", "market", "code", "margin_lots", "close"],
                       ["date", "market", "code"], rows, gz=True)
        if self.official:
            merge_rows(os.path.join(self.dir, "official.csv"), ["date", "market", "total_lots", "official_margin_k"],
                       ["date", "market"], self.official)
        if self.index:
            merge_rows(os.path.join(self.dir, "index.csv"), ["date", "market", "close"], ["date", "market"], self.index)
        if self.missing:
            merge_rows(os.path.join(self.dir, "missing.csv"), ["date", "market", "code", "reason"],
                       ["date", "market", "code"], self.missing)
        self.rows, self.official, self.index, self.missing = [], [], [], []
        _write_json(self.p, self.prog)


def months_back(today, n):
    y, m = today.year, today.month
    out = []
    for _ in range(n):
        out.append((y, m))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return out


def load_calendar(http, store, today):
    """月指數 → 交易日。已抓過的完整月份不重抓;本月與上月每次都重抓。"""
    have = set(store.prog.get("index_months") or [])
    for y, m in months_back(today, 25):
        key = "%04d-%02d" % (y, m)
        recent = (y, m) in months_back(today, 2)
        if key in have and not recent:
            continue
        for mkt, url in (("twse", TWSE_INDEX.format(ym="%04d%02d" % (y, m))), ("tpex", TPEX_INDEX.format(y=y, m=m))):
            j = http.json(url)
            rows = parse_twse_index(j) if mkt == "twse" else parse_tpex_index(j)
            store.index += [(d, mkt, _fmt(c)) for d, c in rows if d[:7] == key]
        have.add(key)
    store.prog["index_months"] = sorted(have)
    store.flush()
    cal = {}
    for r in read_csv(os.path.join(store.dir, "index.csv")):
        cal.setdefault(r["date"], set()).add(r["market"])
    return sorted(d for d in cal if d < today.isoformat())


def fetch_day(http, market, date):
    """→ (margin, close, total_lots, official_k, reason);reason 不為 None 代表整天缺。"""
    d = dt.date.fromisoformat(date)
    if market == "twse":
        jm = http.json(TWSE_MARGIN.format(ymd=d.strftime("%Y%m%d")))
        if resp_date(jm) not in (None, date) and jm.get("tables"):
            return None, None, None, None, "margin_date_mismatch:%s" % resp_date(jm)
        margin, tl, tk = parse_twse_margin(jm)
        if not margin:
            return None, None, None, None, "no_margin"
        jc = http.json(TWSE_CLOSE.format(ymd=d.strftime("%Y%m%d")))
        close = parse_twse_close(jc) if resp_date(jc) == date else {}
    else:
        slash = d.strftime("%Y/%m/%d")
        jm = http.json(TPEX_MARGIN.format(slash=slash))
        if resp_date(jm) != date:
            return None, None, None, None, "margin_date_mismatch:%s" % resp_date(jm)
        margin, tl, tk = parse_tpex_margin(jm)
        if not margin:
            return None, None, None, None, "no_margin"
        jc = http.json(TPEX_CLOSE.format(slash=slash))
        close = parse_tpex_close(jc) if resp_date(jc) == date else {}
    if not close:
        return margin, {}, tl, tk, None   # 收盤整天缺:融資照存,每檔記 no_close
    return margin, close, tl, tk, None


def run_batch(http=None, store=None, today=None, batch_days=BATCH_DAYS, target_days=TARGET_DAYS, max_bytes=MAX_BYTES):
    http = http or Http()
    store = store or Store()
    today = today or dt.datetime.now(TPE).date()
    t0 = time.time()
    log = {"started": dt.datetime.now(TPE).isoformat(timespec="seconds"), "dates": 0, "requests_before": http.count}
    if dir_bytes(store.dir) > max_bytes:
        log["stopped"] = "margin_hist 已超過 %d MB,停止並回報(不自行刪減)" % (max_bytes // 1048576)
        store.prog["batches"].append(log)
        store.flush()
        return 3, log
    try:
        cal = load_calendar(http, store, today)
    except Blocked as e:
        log["stopped"] = "交易日曆:" + str(e)
        store.prog["batches"].append(log)
        store.flush()
        return (4 if isinstance(e, NetError) else 2), log
    target = cal[-target_days:]
    todo = [d for d in reversed(target) if any(not store.done(m, d) for m in MARKETS)]
    log["calendar_days"] = len(cal)
    log["target_range"] = [target[0], target[-1]] if target else None
    code = 0
    for date in todo[:batch_days]:
        try:
            for mkt in MARKETS:
                if store.done(mkt, date):
                    continue
                margin, close, tl, tk, reason = fetch_day(http, mkt, date)
                if reason:
                    store.missing.append((date, mkt, "*", reason))
                    store.mark(mkt, date, "missing:" + reason)
                    continue
                for c, lots in margin.items():
                    p = close.get(c)
                    store.rows.append((date, mkt, c, _fmt(lots), _fmt(p)))
                    if lots is None:
                        store.missing.append((date, mkt, c, "no_margin_value"))
                    elif lots > 0 and p is None:
                        store.missing.append((date, mkt, c, "no_close"))
                store.official.append((date, mkt, _fmt(tl), _fmt(tk)))
                store.mark(mkt, date, "ok" if close else "ok_no_close")
            log["dates"] += 1
        except Blocked as e:
            log["stopped"] = str(e)
            code = 4 if isinstance(e, NetError) else 2
            break
    remaining = len([d for d in target if any(not store.done(m, d) for m in MARKETS)])
    log.update({"remaining": remaining, "requests": http.count - log.pop("requests_before"), "net_retries": http.net_retries,
                "seconds": round(time.time() - t0)})
    store.prog["batches"].append(log)
    store.flush()
    if code == 0 and remaining == 0:
        code = 10
    return code, log


def main():
    code, log = run_batch()
    print(json.dumps(log, ensure_ascii=False))
    print("margin_hist 大小:%.1f MB" % (dir_bytes(OUT_DIR) / 1048576))
    return code


if __name__ == "__main__":
    sys.exit(main())
