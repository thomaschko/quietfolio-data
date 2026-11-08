# -*- coding: utf-8 -*-
"""
us_earnings_probe.py — 美股法說資料擴充 Phase 0 探測(2026-11-08;只讀,只記結構)
============================================================
規格:EDGAR 公司範圍 / 8-K Item 2.02 財報新聞稿取得方式 / 逐字稿來源(Alpha Vantage、ROIC.AI、API Ninjas)/ GDELT。
硬性規則:
  - 2026-11-08 使用者決定不新增 API key:只用免金鑰來源(EDGAR、SEC 全文檢索、新聞通訊社 RSS、GDELT)
    與 src6 既有的 ALPHAVANTAGE_KEY;ROIC.AI、API Ninjas 不測。
  - EDGAR User-Agent:有 EDGAR_USER_AGENT secret 就用;沒有就用「Quietfolio research + repo 網址」(使用者同意,
    不含任何 email);SEC 不接受就照實回報。金鑰與 User-Agent 不寫入任何輸出。
  - EDGAR 自我限速 ≤ 4 次/秒、gzip;403/429 指數退避,連續 3 次失敗 → 停止所有 EDGAR 檢查。
  - 其他主機間隔 ≥ 2 秒(GDELT 6 秒),User-Agent quietfolio/1.0;先讀 robots.txt;
    robots 禁止、403/451/429、驗證碼、登入牆、額度訊息 → 停止該來源,不重試、不繞過。
  - 逐字稿、新聞稿、新聞內容一律不寫入結果、log、artifact;只記狀態碼、字元數、欄位名、筆數。
    唯一保存的原文是各來源「服務條款」中相關句子(逐字),供使用者判斷。
  - Alpha Vantage:src6(earnings_keywords.py)每週一 UTC 02:00 用約 23 次 / 25 次額度;
    若今天(UTC)src6 已跑過或今天是週一 → skipped_quota_reserved。
結果:us_earnings_probe_result.json(workflow 以 artifact 上傳並寫入 job summary;不 commit)。
"""
import ast
import datetime as dt
import json
import os
import re
import sys
import time
from html import unescape
from urllib import robotparser
from urllib.parse import urlsplit, quote

import requests

OUT = "us_earnings_probe_result.json"
UA_GENERIC = "quietfolio/1.0 (read-only research probe)"
# 2026-11-08 使用者決定:不新增任何 API key。EDGAR 不需要 key,但 SEC 要求 User-Agent 宣告名稱與聯絡方式;
# 沒有 EDGAR_USER_AGENT secret 時,用專案名稱 + repo 網址(不含任何 email),SEC 不接受(403)就照實回報。
EDGAR_UA_DEFAULT = "Quietfolio research https://github.com/thomaschko/quietfolio-data"
# 新聞通訊社公開 RSS(網址待驗證;只記結構)
NEWSWIRE_FEEDS = {
    "globenewswire_earnings": "https://www.globenewswire.com/RssFeed/subjectcode/13-Earnings%20Releases%20And%20Operating%20Results/feedTitle/GlobeNewswire%20-%20Earnings%20Releases%20And%20Operating%20Results",
    "prnewswire_earnings": "https://www.prnewswire.com/rss/financial-services-latest-news/earnings-list.rss",
    "businesswire_home": "https://feed.businesswire.com/rss/home/?rss=G1QFDERJXkJeEFpRWQ==",
}
NEWSWIRE_TERMS = {
    "globenewswire_earnings": ["https://www.globenewswire.com/terms-of-service", "https://www.globenewswire.com/rss/list"],
    "prnewswire_earnings": ["https://www.prnewswire.com/terms-of-use/", "https://www.prnewswire.com/rss/"],
    "businesswire_home": ["https://www.businesswire.com/portal/site/home/terms-of-use/", "https://www.businesswire.com/portal/site/home/news/rss/"],
}
EDGAR_MIN_GAP = 0.25          # ≤ 4 次/秒(官方上限 10)
OTHER_GAP = 2.0
GDELT_GAP = 12.0          # 2026-11-08 第一次探測 6 秒仍 429,拉長
SIC_CANDIDATES = ["3674", "3679", "3672", "3570", "3571", "3572", "3575", "3576", "3577", "3578", "3579",
                  "3661", "3663", "3669", "3612", "3613", "3621", "3629", "3690", "3825", "3559", "3827"]
TERM_WORDS = re.compile(r"(?i)\b(store|storage|cache|redistribut\w*|derivative|display|republish|public|resell|"
                        r"commercial|automat\w*|scrap\w*|crawl\w*|bulk|copy|copies|reproduc\w*|share|sublicens\w*|"
                        r"personal use|non-commercial|attribution|rate limit|fair access)\b")
NOW = dt.datetime.now(dt.timezone.utc)


class StopSource(Exception):
    pass


class EdgarAbort(Exception):
    pass


class Http:
    def __init__(self, edgar_ua):
        self.s = requests.Session()
        self.edgar_ua = edgar_ua
        self.last = {}
        self.robots = {}
        self.count = {}
        self.max_rate = {}
        self.edgar_fail = 0
        self.times = {}

    def _gap(self, host):
        if host.endswith("sec.gov"):
            return EDGAR_MIN_GAP
        if "gdelt" in host:
            return GDELT_GAP
        return OTHER_GAP

    def _wait(self, host):
        g = self._gap(host)
        if host in self.last:
            w = g - (time.time() - self.last[host])
            if w > 0:
                time.sleep(w)

    def _ua(self, host):
        return self.edgar_ua if host.endswith("sec.gov") else UA_GENERIC

    def _raw(self, url, headers=None, stream_limit=None):
        host = urlsplit(url).netloc
        self._wait(host)
        h = {"User-Agent": self._ua(host), "Accept-Encoding": "gzip, deflate"}
        h.update(headers or {})
        t0 = time.time()
        try:
            r = self.s.get(url, headers=h, timeout=60, allow_redirects=True)
        finally:
            self.last[host] = time.time()
            self.count[host] = self.count.get(host, 0) + 1
            self.times.setdefault(host, []).append(self.last[host])
        r.elapsed_s = round(time.time() - t0, 2)
        return r

    def robots_ok(self, url):
        p = urlsplit(url)
        if p.netloc not in self.robots:
            try:
                r = self._raw("%s://%s/robots.txt" % (p.scheme, p.netloc))
                if r.status_code == 200 and "user-agent" in r.text.lower():
                    rp = robotparser.RobotFileParser()
                    rp.parse(r.text.splitlines())
                    self.robots[p.netloc] = (rp, "HTTP 200")
                else:
                    self.robots[p.netloc] = (None, "HTTP %s(沒有可解析的規則)" % r.status_code)
            except requests.RequestException as e:
                self.robots[p.netloc] = (None, "robots 讀取失敗 %s" % type(e).__name__)
        rp = self.robots[p.netloc][0]
        if rp is None:
            return True
        ua = self._ua(p.netloc)
        return rp.can_fetch(ua, url) and rp.can_fetch("*", url)

    def edgar(self, url, accept=None):
        """EDGAR:403/429 指數退避(1、2、4 秒),連續 3 次失敗 → EdgarAbort"""
        if not self.robots_ok(url):
            raise StopSource("robots.txt 不允許:" + url)
        for i in range(3):
            r = self._raw(url, {"Accept": accept} if accept else None)
            if r.status_code in (403, 429):
                self.edgar_fail += 1
                if self.edgar_fail >= 3:
                    raise EdgarAbort("EDGAR 連續 3 次 HTTP %s,停止所有 EDGAR 檢查:%s" % (r.status_code, url))
                time.sleep(2 ** i)
                continue
            self.edgar_fail = 0
            return r
        raise EdgarAbort("EDGAR 重試 3 次仍失敗:" + url)

    def other(self, url, headers=None):
        if not self.robots_ok(url):
            raise StopSource("robots.txt 不允許:" + url)
        r = self._raw(url, headers)
        if r.status_code in (403, 429, 451):
            raise StopSource("HTTP %s:%s" % (r.status_code, url.split("?")[0]))
        head = r.text[:2000].lower() if "json" not in (r.headers.get("Content-Type") or "") else ""
        if "captcha" in head or "verify you are human" in head:
            raise StopSource("驗證碼頁:" + url.split("?")[0])
        return r

    def rate_report(self):
        out = {}
        for h, ts in self.times.items():
            ts = sorted(ts)
            best = 0
            j = 0
            for i in range(len(ts)):
                while ts[i] - ts[j] > 1.0:
                    j += 1
                best = max(best, i - j + 1)
            out[h] = {"requests": len(ts), "max_per_sec_observed": best}
        return out


def strip_key(url):
    return re.sub(r"(?i)(apikey|api_key|key|token)=[^&]+", r"\1=***", url)


def text_of_html(s):
    s = re.sub(r"(?is)<(script|style).*?</\1>", " ", s)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", unescape(s)).strip()


def term_sentences(html, limit=25):
    txt = text_of_html(html)
    sents = re.split(r"(?<=[.;:])\s+", txt)
    out = []
    for x in sents:
        if TERM_WORDS.search(x) and 30 <= len(x) <= 600:
            out.append(x.strip())
        if len(out) >= limit:
            break
    return out


def classify_terms(sents):
    j = " ".join(sents).lower()
    bad = re.search(r"(may not|must not|shall not|prohibit|not permitted|no right to)[^.]{0,120}"
                    r"(redistribut|store|cache|display|derivative|resell|share|commercial|scrap|automat|copy)", j)
    if bad:
        return "restricts"
    return "unclear"


def src6_companies():
    """從 earnings_keywords.py 讀 COMPANIES(AST,不 import、不修改)"""
    try:
        src = open("earnings_keywords.py", encoding="utf-8").read()
        for n in ast.parse(src).body:
            if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "COMPANIES":
                return ast.literal_eval(n.value)
    except (OSError, SyntaxError, ValueError):
        return None
    return None


# ------------------------------------------------------------------ 1.1 / 1.2 EDGAR
def probe_edgar(http, res, src6):
    ed = res["edgar"] = {"status": "ok", "checks": {}}
    c = ed["checks"]
    tick = {}
    # 1.1-1 公司清單
    for key, url in (("company_tickers", "https://www.sec.gov/files/company_tickers.json"),
                     ("company_tickers_exchange", "https://www.sec.gov/files/company_tickers_exchange.json")):
        try:
            r = http.edgar(url)
            info = {"status": r.status_code, "bytes": len(r.content), "elapsed_s": r.elapsed_s,
                    "content_encoding": r.headers.get("Content-Encoding")}
            if r.status_code == 200:
                j = r.json()
                if key == "company_tickers":
                    rows = list(j.values())
                    info.update({"rows": len(rows), "fields": sorted(rows[0].keys()) if rows else None})
                    for x in rows:
                        tick.setdefault(str(x.get("ticker", "")).upper(), x.get("cik_str"))
                else:
                    fields = j.get("fields")
                    data = j.get("data") or []
                    info.update({"rows": len(data), "fields": fields})
                    if fields and "exchange" in fields:
                        ie = fields.index("exchange")
                        ex = {}
                        for row in data:
                            ex[row[ie]] = ex.get(row[ie], 0) + 1
                        info["by_exchange"] = dict(sorted(ex.items(), key=lambda kv: -(kv[1] or 0)))
                        nyse_nasdaq = sum(v for k, v in ex.items() if k in ("NYSE", "Nasdaq"))
                        info["nyse_nasdaq_rows"] = nyse_nasdaq
                        # 1.1-3 估算全量 submissions 請求數與耗時
                        info["full_sic_build_estimate"] = {
                            "requests": nyse_nasdaq, "minutes_at_4_per_sec": round(nyse_nasdaq / 4 / 60, 1),
                            "note": "一次性、以 CIK 為鍵可續跑;探測中沒有實際全量執行"}
            c[key] = info
        except (StopSource, EdgarAbort) as e:
            c[key] = {"error": str(e)}
            if isinstance(e, EdgarAbort):
                raise
        except Exception as e:
            c[key] = {"error": repr(e)[:200]}

    # 1.1-5 src6 對應 CIK
    if src6 is None:
        c["src6_mapping"] = {"error": "找不到 src6 公司清單(earnings_keywords.py COMPANIES),停止,不猜名單"}
    else:
        cloud = [k for k, v in src6.items() if "客戶" in v]
        c["src6_mapping"] = {
            "n_companies": len(src6), "spec_expected": 16,
            "cloud_customers": cloud, "n_cloud": len(cloud), "spec_expected_cloud": 6,
            "mapped": {k: tick.get(k) for k in src6},
            "unmapped": [k for k in src6 if not tick.get(k)],
            "note": "src6 目前實際為 %d 家(theme_tracker.py 會自動新增);雲端客戶依 COMPANIES 說明含「客戶」者" % len(src6)}

    # 1.1-2 / 1.2(a) 樣本 submissions
    sample = [k for k in (src6 or {}) if tick.get(k)][:14] + ["MPWR", "VICR", "ANET", "SMCI", "DELL", "CIEN"]
    sample = [k for k in sample if tick.get(k)][:20]
    subs = {}
    keys_seen = set()
    sic_seen = {}
    earn8k = []
    for t in sample:
        url = "https://data.sec.gov/submissions/CIK%010d.json" % int(tick[t])
        try:
            r = http.edgar(url)
            if r.status_code != 200:
                subs[t] = {"status": r.status_code}
                continue
            j = r.json()
            keys_seen |= set(j.keys())
            rec = (j.get("filings") or {}).get("recent") or {}
            forms, items, dates = rec.get("form", []), rec.get("items", []), rec.get("filingDate", [])
            n202 = 0
            last202 = None
            for i, f in enumerate(forms):
                if f == "8-K" and "2.02" in str(items[i] if i < len(items) else ""):
                    n202 += 1
                    if last202 is None:
                        last202 = dates[i]
                        earn8k.append((t, int(tick[t]), rec["accessionNumber"][i], rec.get("primaryDocument", [""])[i], dates[i]))
            subs[t] = {"status": 200, "sic": j.get("sic"), "sicDescription": j.get("sicDescription"),
                       "exchanges": j.get("exchanges"), "tickers": j.get("tickers"),
                       "recent_fields": sorted(rec.keys()), "recent_rows": len(forms),
                       "has_items": "items" in rec, "has_primaryDocument": "primaryDocument" in rec,
                       "n_8k_2.02_recent": n202, "last_8k_2.02": last202,
                       "forms_6k_present": "6-K" in forms}
            sic_seen.setdefault(j.get("sic"), j.get("sicDescription"))
        except EdgarAbort:
            raise
        except Exception as e:
            subs[t] = {"error": repr(e)[:200]}
    c["sample_submissions"] = {"n": len(subs), "top_level_keys": sorted(keys_seen), "by_ticker": subs}

    # 1.1-4 候選 SIC(EDGAR 公司瀏覽 atom,依 SIC)
    sic_out = {}
    for code in SIC_CANDIDATES:
        url = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&SIC=%s&type=&dateb=&owner=include"
               "&count=40&output=atom" % code)
        try:
            r = http.edgar(url, accept="application/atom+xml")
            names = re.findall(r"<conformed-name>([^<]+)</conformed-name>|<title>([^<]+)</title>", r.text)
            names = [a or b for a, b in names][1:]
            sic_out[code] = {"status": r.status_code, "n_listed_first_page": len(names),
                             "sample_companies": names[:5], "sicDescription_from_sample": sic_seen.get(code)}
        except StopSource as e:
            sic_out[code] = {"blocked": str(e)}
            break
        except EdgarAbort:
            raise
        except Exception as e:
            sic_out[code] = {"error": repr(e)[:200]}
    c["sic_candidates"] = sic_out

    # 1.2(b) 即時申報 Atom(8-K)
    try:
        url = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&company=&dateb=&owner=include&count=100&output=atom"
        r = http.edgar(url, accept="application/atom+xml")
        ups = re.findall(r"<updated>([^<]+)</updated>", r.text)
        titles = re.findall(r"<title>([^<]+)</title>", r.text)[1:]
        n202 = len(re.findall(r"Item 2\.02", r.text))
        c["current_atom_8k"] = {"status": r.status_code, "entries": len(titles), "item_2_02_mentions": n202,
                                "window": [min(ups[1:]) if len(ups) > 1 else None, max(ups[1:]) if len(ups) > 1 else None],
                                "entry_tags": sorted(set(re.findall(r"<(\w+)[ >]", r.text)))[:30]}
    except StopSource as e:
        c["current_atom_8k"] = {"blocked": str(e)}
    except EdgarAbort:
        raise
    except Exception as e:
        c["current_atom_8k"] = {"error": repr(e)[:200]}

    # 1.2(c) 每日索引(旺季日 / 平靜日)
    di = {}
    for label, d in (("peak", dt.date(2026, 10, 30)), ("quiet", dt.date(2026, 9, 16))):
        q = (d.month - 1) // 3 + 1
        url = "https://www.sec.gov/Archives/edgar/daily-index/%d/QTR%d/form.%s.idx" % (d.year, q, d.strftime("%Y%m%d"))
        try:
            r = http.edgar(url)
            if r.status_code != 200:
                di[label] = {"date": str(d), "status": r.status_code, "availability": "not_available"}
                continue
            lines = r.text.splitlines()
            n8k = sum(1 for x in lines if x.startswith("8-K ") or x.startswith("8-K/A "))
            di[label] = {"date": str(d), "status": 200, "bytes": len(r.content), "lines": len(lines),
                         "n_8k": n8k, "last_modified": r.headers.get("Last-Modified"),
                         "header_sample": [x for x in lines[:12] if x.strip()][:6],
                         "note": "索引只有表單類型,沒有 Item;2.02 需再查申報索引或 submissions"}
        except StopSource as e:
            di[label] = {"blocked": str(e)}
        except EdgarAbort:
            raise
        except Exception as e:
            di[label] = {"error": repr(e)[:200]}
    c["daily_index"] = di

    # 1.2 樣本申報的 EX-99.x(只記結構)
    ex = []
    for t, cik, acc, prim, fdate in earn8k[:5]:
        accn = acc.replace("-", "")
        url = "https://www.sec.gov/Archives/edgar/data/%d/%s/index.json" % (cik, accn)
        try:
            r = http.edgar(url)
            items = ((r.json().get("directory") or {}).get("item") or []) if r.status_code == 200 else []
            files = [{"name": x.get("name"), "type": x.get("type"), "size": x.get("size")} for x in items]
            exh = [x for x in files if re.search(r"(?i)ex-?99|ex99", str(x["name"]))]
            rec = {"ticker": t, "filed": fdate, "status": r.status_code, "n_files": len(files), "exhibits_99": exh[:4]}
            if exh:
                u2 = "https://www.sec.gov/Archives/edgar/data/%d/%s/%s" % (cik, accn, exh[0]["name"])
                r2 = http.edgar(u2)
                ext = exh[0]["name"].rsplit(".", 1)[-1].lower()
                txt = text_of_html(r2.text) if ext in ("htm", "html") else (r2.text if ext == "txt" else "")
                rec["ex99_1"] = {"ext": ext, "status": r2.status_code, "bytes": len(r2.content),
                                 "text_chars": len(txt), "extract_ok": len(txt) > 500}
                del txt
            ex.append(rec)
        except EdgarAbort:
            raise
        except Exception as e:
            ex.append({"ticker": t, "error": repr(e)[:200]})
    c["sample_exhibits"] = ex
    sizes = [x["ex99_1"]["bytes"] for x in ex if x.get("ex99_1")]
    c["exhibit_avg_bytes"] = round(sum(sizes) / len(sizes)) if sizes else None
    return tick


# ------------------------------------------------------------------ 1.3 逐字稿
def terms_check(http, res_src, urls):
    ex = []
    for u in urls:
        try:
            r = http.other(u)
            ss = term_sentences(r.text) if r.status_code == 200 else []
            ex.append({"url": u, "status": r.status_code, "sentences": ss})
        except StopSource as e:
            ex.append({"url": u, "blocked": str(e)})
        except Exception as e:
            ex.append({"url": u, "error": repr(e)[:200]})
    res_src["terms_excerpts"] = ex
    res_src["terms_class"] = classify_terms([s for x in ex for s in x.get("sentences", [])])


def probe_alphavantage(http, res, targets):
    out = res["alphavantage"] = {"checks": {}}
    terms_check(http, out, ["https://www.alphavantage.co/terms_of_service/"])
    key = os.environ.get("ALPHAVANTAGE_KEY", "")
    if not key:
        out["status"] = "skipped_no_key"
        return
    # 為 src6 保留額度:src6 每週一 UTC 02:00 跑(約 23 次 / 25 次)
    last = None
    try:
        last = (json.load(open("earnings_keywords.json", encoding="utf-8")).get("generated_at") or "")[:10]
    except (OSError, ValueError):
        pass
    out["src6_last_run"] = last
    if NOW.weekday() == 0 or last == NOW.date().isoformat():
        out["status"] = "skipped_quota_reserved"
        out["note"] = "今天(UTC)是 src6 執行日或 src6 已跑過,不動用額度"
        return
    status = "ok"
    for label, t in targets[:3]:
        url = "https://www.alphavantage.co/query?function=EARNINGS_CALL_TRANSCRIPT&symbol=%s&quarter=%s&apikey=%s" % (
            t, quarter_guess(), key)
        try:
            r = http.other(url)
            j = r.json() if r.status_code == 200 else {}
            msg = j.get("Note") or j.get("Information") or j.get("Error Message")
            tr = j.get("transcript")
            out["checks"][label] = {"ticker": t, "quarter": quarter_guess(), "status": r.status_code,
                                    "elapsed_s": r.elapsed_s, "keys": sorted(j.keys()) if isinstance(j, dict) else None,
                                    "n_segments": len(tr) if isinstance(tr, list) else None,
                                    "segment_fields": sorted(tr[0].keys()) if isinstance(tr, list) and tr else None,
                                    "chars": sum(len(str(x.get("content", ""))) for x in tr) if isinstance(tr, list) else 0,
                                    "quota_message": msg[:300] if msg else None}
            del tr
            if msg and re.search(r"(?i)rate limit|requests per day|premium", msg):
                status = "partial"
                break
        except StopSource as e:
            out["checks"][label] = {"blocked": str(e)}
            status = "blocked"
            break
        except Exception as e:
            out["checks"][label] = {"error": repr(e)[:200]}
    out["status"] = status
    out["transcript_requests_used"] = len(out["checks"])


def quarter_guess():
    """最近一個已結束的日曆季(AV 用 YYYYQn)"""
    q = (NOW.month - 1) // 3
    y = NOW.year
    if q == 0:
        y, q = y - 1, 4
    return "%dQ%d" % (y, q)


def probe_roic(http, res, targets):
    out = res["roic"] = {"checks": {}}
    terms_check(http, out, ["https://roic.ai/terms", "https://roic.ai/terms-of-service"])
    # 從官方文件頁找逐字稿端點(不猜網址)
    doc = {}
    for u in ("https://roic.ai/api", "https://docs.roic.ai/"):
        try:
            r = http.other(u)
            paths = sorted(set(re.findall(r"(?i)(?:https?://api\.roic\.ai)?(/v\d+/[A-Za-z0-9_\-/{}]*transcript[A-Za-z0-9_\-/{}]*)", r.text)))
            doc[u] = {"status": r.status_code, "transcript_paths_found": paths[:10]}
        except StopSource as e:
            doc[u] = {"blocked": str(e)}
        except Exception as e:
            doc[u] = {"error": repr(e)[:200]}
    out["checks"]["docs"] = doc
    key = os.environ.get("ROIC_API_KEY", "")
    if not key:
        out["status"] = "skipped_no_key"
        return
    paths = [p for v in doc.values() for p in v.get("transcript_paths_found", [])]
    if not paths:
        out["status"] = "needs_user_action"
        out["note"] = "官方文件頁找不到逐字稿端點;不猜網址,請提供文件連結"
        return
    p = paths[0]
    for label, t in targets[:3]:
        url = "https://api.roic.ai" + p.replace("{ticker}", t).replace("{symbol}", t)
        url += ("&" if "?" in url else "?") + "apikey=" + key
        try:
            r = http.other(url)
            j = None
            try:
                j = r.json()
            except ValueError:
                pass
            out["checks"][label] = {"ticker": t, "endpoint": p, "status": r.status_code, "elapsed_s": r.elapsed_s,
                                    "chars": len(r.text), "json_keys": sorted(j.keys())[:20] if isinstance(j, dict) else
                                    (sorted(j[0].keys())[:20] if isinstance(j, list) and j and isinstance(j[0], dict) else None),
                                    "rate_headers": {k: v for k, v in r.headers.items() if re.search(r"(?i)rate|limit|quota", k)}}
            del j
        except StopSource as e:
            out["checks"][label] = {"blocked": str(e)}
            out["status"] = "blocked"
            return
        except Exception as e:
            out["checks"][label] = {"error": repr(e)[:200]}
    out["status"] = "ok"


def probe_ninjas(http, res, targets):
    out = res["api_ninjas"] = {"checks": {}}
    terms_check(http, out, ["https://api-ninjas.com/terms"])
    key = os.environ.get("API_NINJAS_KEY", "")
    if not key:
        out["status"] = "skipped_no_key"
        return
    y, q = quarter_guess().split("Q")
    status = "ok"
    for label, t in targets[:3]:
        url = "https://api.api-ninjas.com/v1/earningstranscript?ticker=%s&year=%s&quarter=%s" % (t, y, q)
        try:
            r = http.other(url, headers={"X-Api-Key": key})
            j = None
            try:
                j = r.json()
            except ValueError:
                pass
            tr = j.get("transcript") if isinstance(j, dict) else None
            out["checks"][label] = {"ticker": t, "year": y, "quarter": q, "status": r.status_code, "elapsed_s": r.elapsed_s,
                                    "keys": sorted(j.keys()) if isinstance(j, dict) else None,
                                    "chars": len(tr) if isinstance(tr, str) else (len(json.dumps(tr)) if tr else 0),
                                    "split_fields": [k for k in (j or {}) if isinstance(j, dict) and re.search(r"(?i)split|section|qa|prepared|speaker", k)],
                                    "error_message": (j.get("error") or "")[:300] if isinstance(j, dict) else None,
                                    "rate_headers": {k: v for k, v in r.headers.items() if re.search(r"(?i)rate|limit|quota", k)}}
            del tr, j
        except StopSource as e:
            out["checks"][label] = {"blocked": str(e)}
            status = "blocked"
            break
        except Exception as e:
            out["checks"][label] = {"error": repr(e)[:200]}
    out["status"] = status


# ------------------------------------------------------------------ 1.4 GDELT
def probe_gdelt(http, res):
    out = res["gdelt"] = {"checks": {}}
    terms_check(http, out, ["https://www.gdeltproject.org/about.html"])
    doc = "https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/"
    try:
        r = http.other(doc)
        txt = text_of_html(r.text)
        out["checks"]["doc_page"] = {"url": doc, "status": r.status_code,
                                     "mentions": {k: bool(re.search(k, txt, re.I)) for k in
                                                  ("timespan", "maxrecords", "format=json", "sourcelang", "domain", "3 months")}}
    except StopSource as e:
        out["checks"]["doc_page"] = {"blocked": str(e)}
    except Exception as e:
        out["checks"]["doc_page"] = {"error": repr(e)[:200]}
    status = "ok"
    for q in ("Vertiv", "Monolithic Power Systems"):
        url = "https://api.gdeltproject.org/api/v2/doc/doc?query=%s&mode=artlist&format=json&timespan=7d&maxrecords=250" % quote('"%s"' % q)
        try:
            r = http.other(url)
            j = {}
            try:
                j = r.json()
            except ValueError:
                pass
            arts = j.get("articles") or []
            titles = [a.get("title", "") for a in arts]
            out["checks"][q] = {"status": r.status_code, "n": len(arts), "fields": sorted(arts[0].keys()) if arts else None,
                                "unique_domains": len({a.get("domain") for a in arts}),
                                "dup_title_ratio": round(1 - len(set(titles)) / len(titles), 3) if titles else None,
                                "non_json_head": None if j else r.text[:120]}
        except StopSource as e:
            out["checks"][q] = {"blocked": str(e)}
            status = "blocked"
            break
        except Exception as e:
            out["checks"][q] = {"error": repr(e)[:200]}
    out["status"] = status


# ------------------------------------------------------------------ SEC 全文檢索(efts)
def probe_efts(http, res):
    out = res["sec_full_text"] = {"checks": {}}
    end = NOW.date()
    start = end - dt.timedelta(days=30)
    for q in ('"power management"', '"800V"', '"lead time"'):
        url = ("https://efts.sec.gov/LATEST/search-index?q=%s&forms=8-K&dateRange=custom&startdt=%s&enddt=%s"
               % (quote(q), start, end))
        try:
            r = http.edgar(url)
            j = {}
            try:
                j = r.json()
            except ValueError:
                pass
            hits = (j.get("hits") or {})
            arr = hits.get("hits") or []
            src = arr[0].get("_source", {}) if arr else {}
            items = {}
            for h in arr:
                for it in (h.get("_source", {}).get("items") or []):
                    items[it] = items.get(it, 0) + 1
            out["checks"][q] = {"status": r.status_code, "total": (hits.get("total") or {}).get("value"),
                                "returned": len(arr), "source_fields": sorted(src.keys()),
                                "file_types": sorted({h.get("_source", {}).get("file_type") for h in arr if h.get("_source")})[:10],
                                "items_counts": dict(sorted(items.items(), key=lambda kv: -kv[1])[:8]),
                                "non_json_head": None if j else r.text[:120]}
        except StopSource as e:
            out["checks"][q] = {"blocked": str(e)}
            out["status"] = "blocked"
            return
    out["status"] = "ok"
    out["note"] = "只記筆數與欄位;可否依 Item 2.02 / EX-99 篩選見 items_counts、file_types"


# ------------------------------------------------------------------ 新聞通訊社 RSS
def probe_newswires(http, res):
    out = res["newswires"] = {"checks": {}}
    ok = 0
    for k, url in NEWSWIRE_FEEDS.items():
        rec = {"url": url}
        try:
            r = http.other(url)
            body = r.text
            items = re.findall(r"(?is)<item\b.*?</item>", body)
            titles = [re.sub(r"<[^>]+>", "", (re.search(r"(?is)<title>(.*?)</title>", x) or [None, ""])[1]) for x in items]
            tick = sum(1 for x in items if re.search(r"(?i)\((NASDAQ|NYSE)\s*:\s*[A-Z.]+\)", x))
            dates = re.findall(r"(?i)<pubDate>([^<]+)</pubDate>", body)
            rec.update({"status": r.status_code, "ctype": r.headers.get("Content-Type"), "items": len(items),
                        "item_tags": sorted(set(re.findall(r"<(\w+)[ >]", items[0])))[:20] if items else None,
                        "items_with_exchange_ticker": tick, "pubdate_range": [dates[-1], dates[0]] if dates else None,
                        "earnings_like_titles": sum(1 for t in titles if re.search(r"(?i)result|earnings|quarter", t))})
            ok += 1 if items else 0
        except StopSource as e:
            rec["blocked"] = str(e)
        except Exception as e:
            rec["error"] = repr(e)[:200]
        sub = {}
        terms_check(http, sub, NEWSWIRE_TERMS.get(k, []))
        rec["terms_excerpts"], rec["terms_class"] = sub["terms_excerpts"], sub["terms_class"]
        out["checks"][k] = rec
    out["status"] = "ok" if ok == len(NEWSWIRE_FEEDS) else ("partial" if ok else "unreachable")
    out["note"] = "RSS 網址為候選,以 status / items 為準;只記結構,不存標題以外內容(標題也只計數)"


# ------------------------------------------------------------------ 判定與輸出
def summarize(res):
    s = {"usable_now": [], "usable_with_conditions": [], "not_usable": [], "needs_user_action": []}
    ed = res.get("edgar", {})
    if ed.get("status") == "blocked" and res.get("edgar_ua_source") == "repo_url_default":
        s["needs_user_action"].append("edgar: SEC 不接受以 repo 網址當聯絡方式的 User-Agent,需要你提供 EDGAR_USER_AGENT(名稱 + email)")
    elif ed.get("status") == "needs_user_action":
        s["needs_user_action"].append("edgar: 缺 EDGAR_USER_AGENT(請在 GitHub Secrets 設定「專案名稱 聯絡方式」)")
    elif ed.get("status") == "ok":
        s["usable_with_conditions"].append("edgar: 技術可用;SEC 公開資料,須遵守 Fair Access(≤10 次/秒、宣告 User-Agent),條款原文見 terms_excerpts")
    else:
        s["not_usable"].append("edgar: " + str(ed.get("status")))
    for k in ("sec_full_text", "newswires", "alphavantage", "gdelt"):
        v = res.get(k, {})
        st = v.get("status")
        if st in ("skipped", "not_tested"):
            continue
        if st in ("skipped_no_key", "needs_user_action"):
            s["needs_user_action"].append("%s: %s" % (k, st))
        elif st in ("blocked", "unreachable"):
            s["not_usable"].append("%s: %s" % (k, st))
        elif st == "skipped_quota_reserved":
            s["needs_user_action"].append("%s: 今天為 src6 保留額度,改天再測" % k)
        else:
            s["usable_with_conditions"].append("%s: 技術可行(%s);條款分類 %s,原文見 terms_excerpts,由使用者決定" %
                                               (k, st, v.get("terms_class")))
    s["not_adopted"] = ["ROIC.AI、API Ninjas(需要 key,使用者決定不新增)", "Finnhub 逐字稿(付費)", "EarningsCalls.dev(免費僅約 250 字元預覽)", "FMP(免費方案是否含逐字稿待確認,未測)"]
    return s


def main():
    edgar_ua = os.environ.get("EDGAR_USER_AGENT", "").strip()
    ua_source = "secret" if edgar_ua else "repo_url_default"
    edgar_ua = edgar_ua or EDGAR_UA_DEFAULT
    res = {"generated": NOW.isoformat(timespec="seconds"), "secrets_present": {
        "EDGAR_USER_AGENT": ua_source == "secret", "ALPHAVANTAGE_KEY": bool(os.environ.get("ALPHAVANTAGE_KEY")),
        "ROIC_API_KEY": bool(os.environ.get("ROIC_API_KEY")), "API_NINJAS_KEY": bool(os.environ.get("API_NINJAS_KEY"))}, "edgar_ua_source": ua_source}
    http = Http(edgar_ua)
    src6 = src6_companies()
    tick = {}
    if not edgar_ua:
        res["edgar"] = {"status": "needs_user_action", "error": "缺 EDGAR_USER_AGENT,EDGAR 檢查停止(沒有發出任何 SEC 請求)"}
    else:
        try:
            tick = probe_edgar(http, res, src6)
        except EdgarAbort as e:
            res.setdefault("edgar", {})["status"] = "blocked"
            res["edgar"]["error"] = str(e)
        except StopSource as e:
            res.setdefault("edgar", {})["status"] = "blocked"
            res["edgar"]["error"] = str(e)
        try:
            terms_check(http, res["edgar"], ["https://www.sec.gov/os/accessing-edgar-data",
                                             "https://www.sec.gov/privacy"])
        except Exception as e:
            res["edgar"]["terms_error"] = repr(e)[:200]
    # 逐字稿目標:src6 一家、非 src6 大型股、小型股
    targets = [("src6_company", "NVDA"), ("non_src6_large", "MSFT"), ("small_cap", "VICR")]
    res["roic"] = {"status": "not_tested", "note": "使用者決定不新增 API key"}
    res["api_ninjas"] = {"status": "not_tested", "note": "使用者決定不新增 API key"}
    # 2026-11-08:Alpha Vantage 已由 us_transcripts.py 每天使用,探測不再動用額度(避免同時請求觸發每秒限制)
    res["alphavantage"] = {"status": "not_tested", "note": "由 us_transcripts.py 使用;探測不動用額度"}
    for fn in ():
        try:
            fn(http, res, targets)
        except Exception as e:
            res[fn.__name__.replace("probe_", "")] = {"status": "unreachable", "error": repr(e)[:200]}
    if res.get("edgar", {}).get("status") == "ok":
        try:
            probe_efts(http, res)
        except EdgarAbort as e:
            res["sec_full_text"] = {"status": "blocked", "error": str(e)}
        except Exception as e:
            res["sec_full_text"] = {"status": "unreachable", "error": repr(e)[:200]}
    else:
        res["sec_full_text"] = {"status": "skipped", "note": "EDGAR 主檢查未通過"}
    try:
        probe_newswires(http, res)
    except Exception as e:
        res["newswires"] = {"status": "unreachable", "error": repr(e)[:200]}
    try:
        probe_gdelt(http, res)
    except Exception as e:
        res["gdelt"] = {"status": "unreachable", "error": repr(e)[:200]}
    res["request_rates"] = http.rate_report()
    res["summary"] = summarize(res)
    res["quota_plan"] = {
        "alphavantage": {"free_per_day": "25(earnings_keywords.py 註記;以 checks 中的額度訊息為準)",
                         "src6_weekly_use": len(src6 or {}), "suggested_daily_budget": "週一 0;其他日 ≤ 20,保留 ≥ 5 給 src6 手動重跑"},
        "roic": {"measured": res.get("roic", {}).get("status")},
        "api_ninjas": {"measured": res.get("api_ninjas", {}).get("status")}}
    s = json.dumps(res, ensure_ascii=False, indent=1)
    # 保險:結果裡不得出現任何金鑰值
    for k in ("ALPHAVANTAGE_KEY", "ROIC_API_KEY", "API_NINJAS_KEY", "EDGAR_USER_AGENT"):
        v = os.environ.get(k, "")
        if v and len(v) >= 6:
            s = s.replace(v, "***")
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(s)
    print(json.dumps({"secrets_present": res["secrets_present"], "summary": res["summary"]}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
