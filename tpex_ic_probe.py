# -*- coding: utf-8 -*-
"""
tpex_ic_probe.py — 一次性探測:ic.tpex.org.tw(產業價值鏈資訊平台)能否取得「產業 → 上中下游 → 公司名單」(2026-10-21)
只探測,不接入雷達、不建立 industry_chain.json、不改任何現有檔案。輸出 tpex_ic_probe_result.json。
規則:誠實 UA、請求間隔 ≥ 2 秒、總請求 ≤ 40;robots 不允許 / 401 / 403 / 451 / 驗證碼 / 登入牆 → 停止該路徑並記錄,
不換 header、不走代理/鏡像/快取繞過。不存頁面全文或圖片,只記錄結構與少量欄位樣本。
網址格式事先不知道:從首頁連結歸納(含 ic= 之類參數的連結),不猜路徑。
"""
import re
import sys
import json
import time
import datetime as dt
import urllib.robotparser
from collections import Counter
from urllib.parse import urljoin, urlparse, parse_qs

import requests

UA = "quietfolio-probe/1.0 (+https://github.com/thomaschko/quietfolio-data; one-off feasibility check)"
BASE = "https://ic.tpex.org.tw"
HOME = BASE + "/index.php"
MIN_GAP = 2.0
MAX_REQ = 40
OUT = "tpex_ic_probe_result.json"
BLOCK = {401, 403, 451}
WALL_HINTS = ("验证码", "驗證碼", "captcha", "請登入", "请登录", "登入後", "會員登入")
TERMS_WORDS = ("使用條款", "版權", "著作權", "隱私", "資料使用", "免責", "網站聲明", "政府網站資料開放宣告", "開放資料")
TERMS_KEYS = ("重製", "轉載", "引用", "擷取", "自動", "程式", "爬", "再利用", "商業", "授權", "著作權", "禁止", "不得",
              "開放資料", "政府資料開放授權", "姓名標示")
TERMS_FALLBACK = ("https://www.tpex.org.tw/zh-tw/announce/use.html", "https://www.tpex.org.tw/web/about/use.php")
SAMPLE_PICKS = ("半導體", "生技", "汽車", "金融", "電動車", "食品", "觀光", "電腦")
CODE_RE = re.compile(r"(?<![\d.])(\d{4}[A-Z]?)(?![\d.])")

_s = requests.Session()
_s.headers.update({"User-Agent": UA})
_st = {"n": 0, "last": 0.0}
notes = []


def get(url, **kw):
    if _st["n"] >= MAX_REQ:
        notes.append(f"已達請求上限 {MAX_REQ},略過 {url}")
        return None
    w = MIN_GAP - (time.time() - _st["last"])
    if w > 0:
        time.sleep(w)
    _st["n"] += 1
    _st["last"] = time.time()
    try:
        r = _s.get(url, timeout=25, **kw)
    except Exception as e:
        notes.append(f"請求失敗 {url}:{type(e).__name__}: {e}")
        return None
    if not r.encoding or r.encoding.lower() == "iso-8859-1":
        r.encoding = r.apparent_encoding or "utf-8"
    print(f"[{_st['n']:02d}] {r.status_code} {r.url} ({len(r.content)} bytes)", flush=True)
    return r


def walled(r):
    if r.status_code in BLOCK:
        return f"HTTP {r.status_code}"
    low = r.text[:30000].lower()
    return next((f"頁面含「{h}」" for h in WALL_HINTS if h.lower() in low), None)


def soup(html):
    from bs4 import BeautifulSoup
    return BeautifulSoup(html, "html.parser")


_robots = {}


def allowed_other(url):
    """ic.tpex.org.tw 以外的主機(www.tpex.org.tw、data.gov.tw):各自先讀一次 robots.txt。"""
    host = urlparse(url).netloc
    if host not in _robots:
        rp = urllib.robotparser.RobotFileParser()
        r = get(f"https://{host}/robots.txt")
        if r is not None and r.status_code == 200 and "<html" not in r.text[:500].lower():
            rp.parse(r.text.splitlines())
        elif r is not None and (r.status_code in (404, 410) or r.status_code == 200):
            rp.parse([])
        else:
            rp.parse(["User-agent: *", "Disallow: /"])
        _robots[host] = rp
    rp = _robots[host]
    okk = rp.can_fetch(UA, url) and rp.can_fetch("*", url)
    if not okk:
        notes.append(f"robots 不允許 {url},略過")
    return okk


def pattern_of(url):
    """網址 → 結構樣板(path + 參數名,值以 {} 代替)。"""
    p = urlparse(url)
    qs = parse_qs(p.query)
    return p.path + ("?" + "&".join(f"{k}={{}}" for k in sorted(qs)) if qs else "")


def text_lines(s, keys, maxlen=300):
    out = []
    for line in s.get_text("\n", strip=True).splitlines():
        for sent in re.split(r"(?<=[。；;！!])", line):
            sent = sent.strip()
            if 4 < len(sent) < maxlen and any(k in sent for k in keys):
                out.append(sent)
    return list(dict.fromkeys(out))


def main():
    out = {"checked_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
           "robots": {"status": 0, "allows": None, "notes": ""},
           "terms": {"found": False, "url": "", "excerpts": []},
           "home": {"status": 0, "encoding": "", "title": "", "industries_count": 0, "url_pattern": "", "sample": []},
           "industries_sampled": [], "stock_code_available": None, "needs_js": None, "updated_date_shown": None,
           "open_data_alternative": {"found": False, "url": "", "license": "", "candidates": []},
           "verdict": "unknown", "notes": notes, "requests_used": 0}

    def finish(v):
        out["verdict"] = v
        out["requests_used"] = _st["n"]
        with open(OUT, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    # 6) 官方開放資料(先查,不管網站本身結果如何都要回報)
    def check_open_data():
        cands = []
        u = "https://www.tpex.org.tw/openapi/swagger.json"
        r = get(u) if allowed_other(u) else None
        if r is not None and r.status_code == 200:
            try:
                sw = r.json()
                for path, ops in (sw.get("paths") or {}).items():
                    for op in ops.values() if isinstance(ops, dict) else []:
                        txt = json.dumps(op, ensure_ascii=False)
                        if any(k in txt for k in ("產業價值鏈", "產業鏈", "上中下游", "價值鏈")):
                            cands.append({"source": "TPEx OpenAPI", "path": path, "summary": op.get("summary", "")[:80]})
                out["open_data_alternative"]["tpex_openapi_paths"] = len(sw.get("paths") or {})
            except ValueError:
                notes.append("TPEx OpenAPI swagger.json 不是 JSON")
        else:
            notes.append(f"TPEx OpenAPI swagger.json:{None if r is None else r.status_code}")
        u = "https://data.gov.tw/api/v2/rest/dataset"
        r = get(u, params={"q": "產業價值鏈"}) if allowed_other(u + "?q=x") else None
        if r is not None and r.status_code == 200:
            try:
                d = r.json()
                rows = (d.get("result") or {}).get("results") or d.get("results") or []
                for x in rows[:10]:
                    title = x.get("title") or x.get("name") or ""
                    if any(k in title for k in ("產業價值鏈", "產業鏈", "上中下游")):
                        cands.append({"source": "data.gov.tw", "title": title[:80],
                                      "id": x.get("identifier") or x.get("id"),
                                      "publisher": x.get("publisher") or x.get("organization"),
                                      "license": x.get("license") or x.get("license_name")})
                out["open_data_alternative"]["data_gov_hits_raw"] = len(rows)
            except ValueError:
                notes.append("data.gov.tw 搜尋回傳不是 JSON")
        else:
            notes.append(f"data.gov.tw 搜尋:{None if r is None else r.status_code}")
        out["open_data_alternative"]["candidates"] = cands[:10]
        if cands:
            c = cands[0]
            out["open_data_alternative"].update({"found": True, "url": c.get("path") or f"https://data.gov.tw/dataset/{c.get('id')}",
                                                 "license": c.get("license") or ""})
            notes.append("建議改用官方資料集")

    # 1) robots
    r = get(BASE + "/robots.txt")
    if r is None:
        check_open_data()
        return finish("unreachable")
    rp = urllib.robotparser.RobotFileParser()
    out["robots"]["status"] = r.status_code
    if r.status_code == 200 and "<html" not in r.text[:500].lower():
        rp.parse(r.text.splitlines())
        rules = [l.strip() for l in r.text.splitlines() if re.match(r"\s*(user-agent|allow|disallow|sitemap|crawl-delay)", l, re.I)]
        out["robots"]["notes"] = " | ".join(rules)[:600]
    elif r.status_code in (404, 410) or (r.status_code == 200 and "<html" in r.text[:500].lower()):
        rp.parse([])
        out["robots"]["notes"] = f"沒有 robots.txt(HTTP {r.status_code}{',回傳的是 HTML 頁' if r.status_code == 200 else ''}),視為無限制"
    else:
        rp.parse(["User-agent: *", "Disallow: /"])
        out["robots"]["notes"] = f"robots.txt HTTP {r.status_code},保守視為不允許"

    def ok(u):
        return urlparse(u).netloc == urlparse(BASE).netloc and rp.can_fetch(UA, u) and rp.can_fetch("*", u)
    out["robots"]["allows"] = ok(HOME)
    if not out["robots"]["allows"]:
        check_open_data()
        return finish("robots_blocked")

    # 3) 首頁
    r = get(HOME)
    if r is None or r.status_code != 200:
        notes.append(f"首頁:{None if r is None else r.status_code}")
        check_open_data()
        return finish("unreachable")
    w = walled(r)
    if w:
        notes.append(f"首頁被擋:{w}")
        check_open_data()
        return finish("unreachable" if "HTTP" in w else "unknown")
    hs = soup(r.text)
    out["home"].update({"status": r.status_code, "encoding": r.encoding,
                        "title": (hs.title.get_text(strip=True) if hs.title else "")[:80]})
    links = [(a.get_text(" ", strip=True), urljoin(r.url, a["href"])) for a in hs.find_all("a", href=True)]
    ind = [(t, u) for t, u in links if urlparse(u).netloc == urlparse(BASE).netloc and "ic=" in urlparse(u).query and t]
    seen, inds = set(), []
    for t, u in ind:
        if u not in seen:
            seen.add(u)
            inds.append({"name": t[:30], "url": u})
    pats = Counter(pattern_of(x["url"]) for x in inds)
    out["home"].update({"industries_count": len(inds), "url_pattern": pats.most_common(1)[0][0] if pats else "",
                        "url_patterns": dict(pats.most_common(5)), "sample": inds[:12]})
    out["needs_js"] = not inds
    if not inds:
        notes.append("首頁 HTML 沒有含 ic= 參數的產業連結(可能要 JavaScript 才會出現);列出前 15 個站內連結樣板供人工判讀")
        out["home"]["link_patterns"] = dict(Counter(pattern_of(u) for _, u in links
                                                    if urlparse(u).netloc == urlparse(BASE).netloc).most_common(15))
    dm = re.search(r"(?:更新|資料日期|資料時間)[^0-9]{0,8}(\d{2,4}[/-]\d{1,2}[/-]\d{1,2})", hs.get_text(" ", strip=True))
    out["updated_date_shown"] = dm.group(0)[:40] if dm else False

    # 2) 條款:首頁頁尾連結 → tpex.org.tw 常見頁(最多 3 個請求)
    cand = [u for t, u in links if any(k in t for k in TERMS_WORDS)]
    cand = list(dict.fromkeys(cand)) + [u for u in TERMS_FALLBACK]
    tried = 0
    for u in cand:
        if tried >= 3:
            break
        host = urlparse(u).netloc
        if host == urlparse(BASE).netloc and not ok(u):
            continue
        if not host.endswith("tpex.org.tw"):
            continue
        if host != urlparse(BASE).netloc and not allowed_other(u):
            continue
        tried += 1
        tr = get(u)
        if tr is None or tr.status_code != 200 or walled(tr):
            continue
        ex = text_lines(soup(tr.text), TERMS_KEYS)
        if ex:
            out["terms"] = {"found": True, "url": tr.url, "excerpts": ex[:20]}
            break
    if not out["terms"]["found"]:
        out["terms"]["url"] = "not_found"

    # 4) 抽 3 個產業(必含半導體)
    picks = []
    for key in SAMPLE_PICKS:
        x = next((i for i in inds if key in i["name"] and i not in picks), None)
        if x:
            picks.append(x)
        if len(picks) >= 3:
            break
    if inds and not any("半導體" in p["name"] for p in picks):
        notes.append("首頁產業清單裡找不到「半導體」")
    any_code = False
    for p in picks:
        rec = {"name": p["name"], "url": p["url"], "stages": [], "stage_url_pattern": "", "company_fields": [],
               "company_count_sample": 0}
        if not ok(p["url"]):
            notes.append(f"robots 不允許 {p['url']}")
            continue
        pr = get(p["url"])
        if pr is None or pr.status_code != 200:
            rec["error"] = None if pr is None else pr.status_code
            out["industries_sampled"].append(rec)
            continue
        w = walled(pr)
        if w:
            rec["error"] = w
            out["industries_sampled"].append(rec)
            notes.append(f"產業頁被擋:{w},停止")
            break
        ps = soup(pr.text)
        txt = ps.get_text(" ", strip=True)
        rec["has_upstream_words"] = {k: (k in txt) for k in ("上游", "中游", "下游")}
        plinks = [(a.get_text(" ", strip=True), urljoin(pr.url, a["href"])) for a in ps.find_all("a", href=True)]
        stage_links = [(t, u) for t, u in plinks if urlparse(u).netloc == urlparse(BASE).netloc and t
                       and u != p["url"] and pattern_of(u) not in ("/index.php",)]
        # 環節:同產業頁內、含 ic/chain/stage 參數的連結
        st = [(t, u) for t, u in stage_links if re.search(r"(ic|chain|stage|sub|cat)=", urlparse(u).query, re.I)]
        rec["stages"] = list(dict.fromkeys(t[:20] for t, _ in st))[:30]
        sp = Counter(pattern_of(u) for _, u in st)
        rec["stage_url_pattern"] = sp.most_common(1)[0][0] if sp else ""
        rec["stage_url_patterns"] = dict(sp.most_common(4))
        # 公司:連結文字或網址含股票代號
        comp = [(t, u) for t, u in plinks if re.search(r"(stk|stock|code|co_id|comp)", urlparse(u).query + urlparse(u).path, re.I)]
        codes_in_links = sorted({m for t, u in comp for m in CODE_RE.findall(t + " " + urlparse(u).query)})
        rec["company_link_pattern"] = dict(Counter(pattern_of(u) for _, u in comp).most_common(3))
        rec["company_count_sample"] = len(codes_in_links)
        rec["company_sample"] = [t[:30] for t, _ in comp[:6]]
        tables = ps.find_all("table")
        if tables:
            heads = [th.get_text(" ", strip=True)[:15] for th in tables[0].find_all(["th"])][:10]
            rec["company_fields"] = heads
        rec["xhr_hints"] = sorted(set(re.findall(r"""["']([^"']*(?:ajax|api|json|\.php\?[^"']*=)[^"']*)["']""", pr.text)))[:8]
        rec["foreign_mark"] = any(k in txt for k in ("外國", "F-", "-KY", "KY"))
        rec["listing_mark"] = {k: (k in txt) for k in ("上市", "上櫃", "興櫃")}
        any_code = any_code or bool(codes_in_links)
        out["industries_sampled"].append(rec)
    out["stock_code_available"] = any_code
    semi = next((x for x in out["industries_sampled"] if "半導體" in x["name"]), None)
    if semi:
        out["semiconductor_summary"] = {"stages": len(semi["stages"]), "companies_on_page": semi["company_count_sample"]}

    check_open_data()

    # 判定
    if out["needs_js"]:
        return finish("needs_js")
    bad_terms = [e for e in out["terms"]["excerpts"] if re.search(r"(禁止|不得|嚴禁).{0,30}(重製|轉載|擷取|自動|程式|爬|再利用|商業)", e)]
    if bad_terms:
        notes.append("條款有限制重製/擷取/再利用的字句,請人工判斷(見 terms.excerpts)")
        return finish("terms_restrict")
    if not any_code:
        return finish("no_code")
    return finish("ok")


if __name__ == "__main__":
    sys.exit(main())
