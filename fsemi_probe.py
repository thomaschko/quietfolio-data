# -*- coding: utf-8 -*-
"""
fsemi_probe.py — 一次性探測:fsemi.tech(未來半導體)「新聞快訊」能否當產業新聞觀察源(2026-10-20)
只探測,不接入雷達、不改任何現有檔案。輸出 fsemi_probe_result.json。
規則:誠實 UA、請求間隔 ≥ 2 秒、總請求 ≤ 30;robots 不允許 / 403 / 451 / 驗證碼 / 登入牆 → 停止該路徑並記錄,
不換 header、不走代理/鏡像/快取繞過。只記錄標題、日期、連結與必要欄位,不存文章全文。
"""
import re
import sys
import json
import time
import datetime as dt
import urllib.robotparser
from collections import Counter
from urllib.parse import urljoin, urlparse

import requests

UA = "quietfolio-probe/1.0 (+https://github.com/thomaschko/quietfolio-data; one-off feasibility check)"
BASE = "https://www.fsemi.tech"
LIST = BASE + "/cms/xinwenkuaixun.html"
MIN_GAP = 2.0
MAX_REQ = 30
STABILITY_WAIT = 300
MAX_PAGES = 5
OUT = "fsemi_probe_result.json"
KNOWN_RULES = ["Disallow: /bin/"]
BLOCK_STATUS = {401, 403, 451}
WALL_HINTS = ("验证码", "captcha", "请登录", "登录后查看", "登录后阅读", "滑动验证", "安全验证", "会员登录后")
TERMS_PATHS = ("/about.html", "/cms/about.html", "/copyright.html", "/cms/copyright.html", "/statement.html",
               "/cms/statement.html", "/disclaimer.html", "/terms.html")
TERMS_LINK_WORDS = ("版权", "声明", "免责", "条款", "协议", "关于我们", "法律")
TERMS_KEYS = ("转载", "转贴", "复制", "重制", "复印", "镜像", "抓取", "爬", "采集", "自动化", "机器人", "镜像",
              "版权所有", "未经", "授权", "许可", "侵权", "法律责任")
ART_RE = re.compile(r"/cms/xinwenkuaixun/(\d+)\.html")
DATE_RE = re.compile(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})日?(?:\s*(\d{1,2}):(\d{2})(?::(\d{2}))?)?")

_session = requests.Session()
_session.headers.update({"User-Agent": UA})
_state = {"n": 0, "last": 0.0}
notes = []


def get(url, timeout=20):
    if _state["n"] >= MAX_REQ:
        notes.append(f"已達請求上限 {MAX_REQ},略過 {url}")
        return None
    wait = MIN_GAP - (time.time() - _state["last"])
    if wait > 0:
        time.sleep(wait)
    _state["n"] += 1
    _state["last"] = time.time()
    try:
        r = _session.get(url, timeout=timeout, allow_redirects=True)
    except Exception as e:
        notes.append(f"請求失敗 {url}:{type(e).__name__}: {e}")
        return None
    print(f"[{_state['n']:02d}] {r.status_code} {url} ({len(r.content)} bytes)", flush=True)
    return r


def decode(r):
    hdr = None
    m = re.search(r"charset=([\w-]+)", r.headers.get("Content-Type", ""), re.I)
    if m:
        hdr = m.group(1).lower()
    head = r.content[:4000].decode("ascii", "ignore")
    mm = re.search(r"<meta[^>]+charset=[\"']?([\w-]+)", head, re.I)
    meta = mm.group(1).lower() if mm else None
    enc = meta or hdr or r.apparent_encoding or "utf-8"
    try:
        return r.content.decode(enc, "replace"), f"header={hdr} meta={meta}"
    except LookupError:
        return r.content.decode("utf-8", "replace"), f"header={hdr} meta={meta}"


def walled(r, text):
    if r.status_code in BLOCK_STATUS:
        return f"HTTP {r.status_code}"
    low = (text or "")[:30000].lower()
    for h in WALL_HINTS:
        if h.lower() in low:
            return f"頁面含「{h}」"
    return None


def norm_date(s):
    m = DATE_RE.search(s or "")
    if not m:
        return None, False
    y, mo, d, hh, mi, ss = m.groups()
    try:
        x = dt.datetime(int(y), int(mo), int(d), int(hh or 0), int(mi or 0), int(ss or 0))
    except ValueError:
        return None, False
    return (x.strftime("%Y-%m-%d %H:%M") if hh else x.strftime("%Y-%m-%d")), bool(hh)


def soup_of(html):
    from bs4 import BeautifulSoup
    return BeautifulSoup(html, "html.parser")


def parse_list(html, base):
    """列表頁 → [{id,title,date,has_time,url}]。以文章連結 /cms/xinwenkuaixun/{id}.html 定位,往上找含日期的容器。"""
    s = soup_of(html)
    out, seen = [], set()
    for a in s.find_all("a", href=True):
        m = ART_RE.search(a["href"])
        if not m:
            continue
        aid = int(m.group(1))
        title = a.get("title") or a.get_text(" ", strip=True)
        if aid in seen or len(title) < 4:
            continue
        box, date, has_time = a, None, False
        for _ in range(5):
            box = box.parent
            if box is None:
                break
            date, has_time = norm_date(box.get_text(" ", strip=True))
            if date:
                break
        seen.add(aid)
        out.append({"id": aid, "title": title.strip()[:120], "date": date, "has_time": has_time,
                    "url": urljoin(base, a["href"])})
    return out


def main():
    out = {"checked_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
           "robots": {"status": 0, "rules": [], "changed_from_known": False, "sitemaps": []},
           "terms": {"found": False, "url": "", "excerpts": []},
           "list": {"encoding": "", "per_page": 0, "date_format": "", "has_time": False, "ids_monotonic": None,
                    "page2_overlap": None, "sample": []},
           "orderby_publishtime_same_as_default": None,
           "article_sample": [], "sitemap": {"status": 0, "url_count": 0, "has_lastmod": None, "sample": []},
           "freshness": {"latest_date": "", "avg_posts_per_day_14d": 0.0, "pages_scanned": 0, "days_by_count": {}},
           "stability": {"new_items_after_5min": None}, "verdict": "unknown", "notes": notes, "requests_used": 0}

    def finish(verdict):
        out["verdict"] = verdict
        out["requests_used"] = _state["n"]
        with open(OUT, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    # 1) robots
    r = get(BASE + "/robots.txt")
    rp = urllib.robotparser.RobotFileParser()
    if r is None:
        return finish("unreachable")
    out["robots"]["status"] = r.status_code
    if r.status_code == 200:
        lines = [l.strip() for l in r.text.splitlines() if l.strip() and not l.strip().startswith("#")]
        out["robots"]["rules"] = lines[:40]
        out["robots"]["sitemaps"] = re.findall(r"(?im)^\s*sitemap:\s*(\S+)", r.text)
        dis = [l for l in lines if re.match(r"(?i)(dis)?allow:", l)]
        out["robots"]["changed_from_known"] = sorted(d.replace(" ", "").lower() for d in dis) != \
            sorted(k.replace(" ", "").lower() for k in KNOWN_RULES)
        rp.parse(r.text.splitlines())
    elif r.status_code in (404, 410):
        rp.parse([])
        notes.append(f"robots.txt {r.status_code},視為無限制")
        out["robots"]["changed_from_known"] = True
    else:
        notes.append(f"robots.txt HTTP {r.status_code},保守視為不允許")
        return finish("robots_blocked" if r.status_code not in BLOCK_STATUS else "unreachable")

    def ok(url):
        return urlparse(url).netloc == urlparse(BASE).netloc and rp.can_fetch(UA, url) and rp.can_fetch("*", url)

    if not ok(LIST):
        notes.append("robots 不允許列表頁,停止")
        return finish("robots_blocked")

    # 3) 列表第 1 頁(先抓,頁尾連結順便用來找條款)
    r1 = get(LIST)
    if r1 is None:
        return finish("unreachable")
    h1, enc = decode(r1)
    w = walled(r1, h1)
    if w:
        notes.append(f"列表頁被擋:{w},停止")
        return finish("needs_login" if "HTTP" not in w or r1.status_code == 401 else "unreachable")
    if r1.status_code != 200:
        notes.append(f"列表頁 HTTP {r1.status_code}")
        return finish("unreachable")
    out["list"]["encoding"] = enc
    p1 = parse_list(h1, r1.url)
    pages = {1: p1}

    # 2) 使用條款 / 版權聲明:先看頁尾連結,再試常見路徑(最多 3 個請求)
    s1 = soup_of(h1)
    cand = []
    for a in s1.find_all("a", href=True):
        t = a.get_text(" ", strip=True)
        if any(k in t for k in TERMS_LINK_WORDS):
            u = urljoin(r1.url, a["href"])
            if u not in cand and ok(u):
                cand.append(u)
    page_text = s1.get_text("\n", strip=True)
    foot_hits = [l for l in page_text.splitlines() if any(k in l for k in TERMS_KEYS)]
    if foot_hits:
        out["terms"]["list_page_footer_excerpts"] = foot_hits[:10]
    cand += [BASE + p for p in TERMS_PATHS if BASE + p not in cand]
    tried = 0
    for u in cand:
        if tried >= 3:
            break
        if not ok(u):
            continue
        tried += 1
        tr = get(u)
        if tr is None or tr.status_code != 200:
            continue
        th, _ = decode(tr)
        txt = soup_of(th).get_text("\n", strip=True)
        hits = []
        for line in txt.splitlines():
            for sent in re.split(r"(?<=[。；;！!])", line):
                if any(k in sent for k in TERMS_KEYS) and 4 < len(sent) < 400:
                    hits.append(sent.strip())
        if hits:
            out["terms"].update({"found": True, "url": tr.url, "excerpts": hits[:15]})
            break
    if not out["terms"]["found"]:
        out["terms"]["url"] = "not_found"
        if foot_hits:
            notes.append("條款頁沒找到,但列表頁頁尾有相關字句(見 terms.list_page_footer_excerpts)")

    # 列表第 2 頁
    def page_url(n, extra=""):
        return f"{LIST}?page={n}{extra}"
    r2 = get(page_url(2)) if ok(page_url(2)) else None
    if r2 is not None and r2.status_code == 200 and not walled(r2, r2.text):
        pages[2] = parse_list(decode(r2)[0], r2.url)
    ids1 = [x["id"] for x in p1]
    ids2 = [x["id"] for x in pages.get(2, [])]
    allids = ids1 + ids2
    out["list"]["per_page"] = len(p1)
    out["list"]["ids_monotonic"] = bool(allids) and all(a > b for a, b in zip(allids, allids[1:]))
    out["list"]["page2_overlap"] = sorted(set(ids1) & set(ids2)) if ids2 else None
    dates = [x for x in p1 + pages.get(2, []) if x["date"]]
    out["list"]["has_time"] = any(x["has_time"] for x in dates)
    out["list"]["date_format"] = "YYYY-MM-DD HH:MM" if out["list"]["has_time"] else ("YYYY-MM-DD" if dates else "")
    out["list"]["sample"] = [{k: x[k] for k in ("id", "title", "date", "url")} for x in (p1 + pages.get(2, []))[:20]]
    if not p1:
        notes.append("列表頁沒有解析出任何文章連結")
        return finish("unknown")

    # 4) orderby=publishtime&orderway=desc
    u = page_url(1, "&orderby=publishtime&orderway=desc")
    if ok(u):
        ro = get(u)
        if ro is not None and ro.status_code == 200:
            po = [x["id"] for x in parse_list(decode(ro)[0], ro.url)]
            out["orderby_publishtime_same_as_default"] = po == ids1
            if po != ids1:
                out["orderby_publishtime_ids"] = po[:10]

    # 5) 最新 3 篇文章頁(只記 meta)
    for x in p1[:3]:
        if not ok(x["url"]):
            notes.append(f"robots 不允許 {x['url']}")
            continue
        ar = get(x["url"])
        if ar is None:
            continue
        ah, _ = decode(ar)
        w = walled(ar, ah)
        rec = {"url": x["url"], "status": ar.status_code, "needs_login": bool(w), "published_at": None,
               "source_label": None, "category": None, "tags": None}
        if w:
            rec["blocked"] = w
            out["article_sample"].append(rec)
            notes.append(f"文章頁被擋:{w},停止文章探測")
            break
        sa = soup_of(ah)
        mt = sa.find("meta", attrs={"property": re.compile("published_time", re.I)}) or \
            sa.find("meta", attrs={"name": re.compile("pubdate|publishdate", re.I)})
        txt = sa.get_text("\n", strip=True)
        if mt and mt.get("content"):
            rec["published_at"], rec["published_src"] = mt["content"], "meta"
        else:
            m = re.search(r"(?:发布时间|发布日期|时间|日期)[:：]?\s*(20\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2}日?(?:\s*\d{1,2}:\d{2}(?::\d{2})?)?)", txt)
            if m:
                rec["published_at"], rec["published_src"] = m.group(1), "label"
            else:
                m = DATE_RE.search(txt)
                rec["published_at"], rec["published_src"] = (m.group(0), "body-first-date") if m else (None, None)
        m = re.search(r"(?:来源|出处|作者|转载自|文章来源)[:：]\s*([^\n]{1,40})", txt)
        rec["source_label"] = m.group(0)[:60] if m else None
        rec["original_mark"] = next((k for k in ("原创", "转载", "供稿", "厂商", "来源于网络", "投稿") if k in txt[:3000]), None)
        kw = sa.find("meta", attrs={"name": re.compile("^keywords$", re.I)})
        rec["tags"] = kw["content"][:120] if kw and kw.get("content") else None
        crumbs = [a.get_text(strip=True) for a in sa.select(".breadcrumb a, .position a, .crumbs a, .location a")][:5]
        rec["category"] = " > ".join(crumbs) if crumbs else None
        out["article_sample"].append(rec)

    # 6) sitemap
    sm = (out["robots"]["sitemaps"] or [BASE + "/sitemap.xml"])[0]
    if ok(sm):
        rs = get(sm)
        if rs is not None:
            out["sitemap"]["status"] = rs.status_code
            if rs.status_code == 200:
                locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", rs.text)
                out["sitemap"].update({"url": sm, "url_count": len(locs), "has_lastmod": "<lastmod>" in rs.text,
                                       "is_index": "<sitemapindex" in rs.text, "sample": locs[:5]})

    # 8) 近 14 天發文數(列表日期,最多 5 頁)
    today = dt.date.today()
    for n in range(3, MAX_PAGES + 1):
        dd = [x["date"] for p in pages.values() for x in p if x["date"]]
        if dd and min(dd)[:10] < str(today - dt.timedelta(days=14)):
            break
        u = page_url(n)
        if not ok(u):
            break
        rp_ = get(u)
        if rp_ is None or rp_.status_code != 200:
            break
        pages[n] = parse_list(decode(rp_)[0], rp_.url)
        if not pages[n]:
            break
    out["freshness"]["pages_scanned"] = len(pages)
    alld = [x["date"][:10] for p in pages.values() for x in p if x["date"]]
    if alld:
        cnt = Counter(d for d in alld if d >= str(today - dt.timedelta(days=13)))
        out["freshness"]["latest_date"] = max(alld)
        out["freshness"]["days_by_count"] = dict(sorted(cnt.items(), reverse=True))
        covered = min(14, (today - dt.date.fromisoformat(min(alld))).days + 1)
        out["freshness"]["avg_posts_per_day_14d"] = round(sum(cnt.values()) / max(covered, 1), 2)
        out["freshness"]["days_covered"] = covered
        if covered < 14:
            notes.append(f"翻 {len(pages)} 頁只涵蓋 {covered} 天,平均以 {covered} 天計")

    # 7) 穩定性
    print(f"等待 {STABILITY_WAIT} 秒後重抓第 1 頁…", flush=True)
    time.sleep(STABILITY_WAIT)
    rr = get(LIST)
    if rr is not None and rr.status_code == 200:
        again = [x["id"] for x in parse_list(decode(rr)[0], rr.url)]
        out["stability"] = {"new_items_after_5min": len([i for i in again if i not in ids1]),
                            "top_id_before": ids1[0] if ids1 else None, "top_id_after": again[0] if again else None}

    # 判定
    terms_bad = any(re.search(r"(禁止|不得|严禁).{0,20}(转载|复制|抓取|采集|爬|镜像|自动)", e) for e in out["terms"]["excerpts"]) or \
        any(re.search(r"(禁止|不得|严禁).{0,20}(转载|复制|抓取|采集|爬|镜像|自动)", e)
            for e in out["terms"].get("list_page_footer_excerpts", []))
    if terms_bad:
        notes.append("條款/頁尾有禁止轉載、複製或擷取的字句,請人工判斷")
        return finish("terms_restrict")
    if any(a.get("needs_login") for a in out["article_sample"]):
        return finish("needs_login")
    if dates:
        return finish("ok")
    notes.append("列表沒有可解析的日期")
    return finish("unknown")


if __name__ == "__main__":
    sys.exit(main())
