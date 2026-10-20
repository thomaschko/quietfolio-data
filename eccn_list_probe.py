# -*- coding: utf-8 -*-
"""
eccn_list_probe.py — 一次性補探測:m.eccn.com「芯闻」列表頁 /consultation/news.html 與分頁(2026-10-20)
首頁只有 5 則、新聞約每 3~20 分鐘一則,每天抓首頁會漏掉大部分 → 確認列表頁每頁幾則、能否翻頁、
「展开更多」(get_ajax_more_list)的實際請求格式。只探測,不接入、不改現有檔案。
規則同 eccn_probe.py:誠實 UA、間隔 ≥ 2 秒、總請求 ≤ 12;robots 不允許 / 401 / 403 / 451 / 驗證碼 / 登入牆 → 停止;
不換 header、不繞過;只記錄標題、網址、由網址推得的時間。輸出 eccn_list_probe_result.json。
"""
import re
import sys
import json
import time
import datetime as dt
import urllib.robotparser
from urllib.parse import urljoin, urlparse

import requests

UA = "quietfolio-probe/1.0 (+https://github.com/thomaschko/quietfolio-data; one-off feasibility check)"
BASE = "https://m.eccn.com"
LIST = BASE + "/consultation/news.html"
JS = BASE + "/Store/Consultation/js/get_ajax_list.js?d=26"
MIN_GAP = 2.0
MAX_REQ = 12
OUT = "eccn_list_probe_result.json"
BLOCK = {401, 403, 451}
WALL_HINTS = ("验证码", "captcha", "请登录", "登录后", "滑动验证", "安全验证")
ART_RE = re.compile(r"/consultation/news/(\d{14})(\d*)\.html")

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
        r = _s.get(url, timeout=20, **kw)
    except Exception as e:
        notes.append(f"請求失敗 {url}:{type(e).__name__}: {e}")
        return None
    r.encoding = r.encoding or "utf-8"
    print(f"[{_st['n']:02d}] {r.status_code} {r.url} ({len(r.content)} bytes)", flush=True)
    return r


def walled(r):
    if r.status_code in BLOCK:
        return f"HTTP {r.status_code}"
    low = r.text[:20000].lower()
    return next((f"頁面含「{h}」" for h in WALL_HINTS if h.lower() in low), None)


def items_in(html, base):
    """文章連結 → [{title, url, ts}](ts 由網址前 14 碼推得,YYYYmmddHHMMSS)。"""
    out, seen = [], set()
    for m in re.finditer(r"""<a[^>]+href=["']([^"']*?/consultation/news/\d{14}\d*\.html)["'][^>]*>(.*?)</a>""", html, re.S | re.I):
        url = urljoin(base, m.group(1))
        title = re.sub(r"<[^>]+>", " ", m.group(2))
        title = re.sub(r"\s+", " ", title).strip()
        if url in seen or len(title) < 6:
            continue
        seen.add(url)
        ts = ART_RE.search(url).group(1)
        try:
            ts = dt.datetime.strptime(ts, "%Y%m%d%H%M%S").strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            ts = None
        out.append({"title": title[:120], "url": url, "ts": ts})
    return out


def main():
    out = {"checked_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
           "robots_allows_list": None, "list_page": {}, "pagination": {}, "ajax": {}, "verdict": "unknown",
           "notes": notes, "requests_used": 0}

    def finish(v):
        out["verdict"] = v
        out["requests_used"] = _st["n"]
        with open(OUT, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    r = get(BASE + "/robots.txt")
    if r is None:
        return finish("unreachable")
    rp = urllib.robotparser.RobotFileParser()
    rp.parse(r.text.splitlines() if r.status_code == 200 else ([] if r.status_code in (404, 410) else ["User-agent: *", "Disallow: /"]))

    def ok(u):
        return urlparse(u).netloc == "m.eccn.com" and rp.can_fetch(UA, u) and rp.can_fetch("*", u)
    out["robots_allows_list"] = ok(LIST)
    if not ok(LIST):
        return finish("robots_blocked")

    # 1) 列表頁第 1 頁
    r = get(LIST)
    if r is None:
        return finish("unreachable")
    w = walled(r)
    if w:
        notes.append(f"列表頁被擋:{w}")
        return finish("login_wall" if "HTTP" not in w else "unreachable")
    if r.status_code != 200:
        notes.append(f"列表頁 HTTP {r.status_code}")
        return finish("unreachable")
    html = r.text
    p1 = items_in(html, r.url)
    ts = [x["ts"] for x in p1 if x["ts"]]
    out["list_page"] = {"status": r.status_code, "final_url": r.url, "per_page": len(p1),
                        "newest": max(ts) if ts else None, "oldest": min(ts) if ts else None, "sample": p1[:10]}

    # 2) 分頁:HTML 裡的分頁連結 / 「展开更多」/ 載入更多的 data 屬性
    page_links = sorted(set(urljoin(r.url, h) for h in re.findall(
        r"""href=["']([^"']*(?:news(?:_|/|-)\d+\.html|news\.html\?[^"']*(?:page|p)=\d+)[^"']*)["']""", html, re.I)))
    more = re.findall(r"""onclick=["']([^"']*get_ajax_more_list[^"']*)["']""", html, re.I)
    var_defs = re.findall(r"""var\s+(more_\w+_url)\s*=\s*["']([^"']+)["']""", html)
    out["pagination"] = {"page_links": page_links[:10], "more_onclick": more[:3], "more_url_vars": var_defs[:5]}

    # 3) get_ajax_list.js:看 get_ajax_more_list 怎麼組參數(只讀,不執行)
    if ok(JS.split("?")[0]):
        jr = get(JS)
        if jr is not None and jr.status_code == 200:
            js = jr.text
            fn = re.search(r"function\s+get_ajax_more_list\s*\([^)]*\)\s*\{(.{0,1500})", js, re.S)
            out["ajax"]["js_snippet"] = (fn.group(0)[:1500] if fn else js[:800])
            out["ajax"]["params_seen"] = sorted(set(re.findall(r"""[?&]?(\w+)\s*[:=]\s*(?:page|p\b|next|num|size|last|offset)""", js)))[:10]
            out["ajax"]["data_keys"] = sorted(set(re.findall(r"""data\s*:\s*\{([^}]{0,300})\}""", js)))[:3]
            out["ajax"]["type"] = "POST" if re.search(r"type\s*:\s*['\"]post", js, re.I) else "GET"

    # 4) 第 2 頁:只用 HTML 裡明確出現的分頁連結(不猜參數)
    nxt = next((u for u in page_links if re.search(r"(?:_|/|-|page=|p=)2\b", u)), None)
    if nxt and ok(nxt):
        r2 = get(nxt)
        if r2 is not None and r2.status_code == 200 and not walled(r2):
            p2 = items_in(r2.text, r2.url)
            t2 = [x["ts"] for x in p2 if x["ts"]]
            out["pagination"].update({"page2_url": nxt, "page2_count": len(p2),
                                      "page2_overlap": len({x["url"] for x in p1} & {x["url"] for x in p2}),
                                      "page2_newest": max(t2) if t2 else None, "page2_oldest": min(t2) if t2 else None})
    else:
        notes.append("HTML 裡沒有明確的第 2 頁連結(分頁只能靠 AJAX;依規則不猜參數)")

    # 判定:列表頁一頁就能涵蓋一天 → ok;否則要靠分頁
    hours = None
    if ts and len(ts) >= 2:
        hours = (dt.datetime.fromisoformat(max(ts)) - dt.datetime.fromisoformat(min(ts))).total_seconds() / 3600
    out["list_page"]["span_hours"] = round(hours, 1) if hours is not None else None
    if not p1:
        return finish("unknown")
    covered = (hours or 0) + (0 if "page2_count" not in out["pagination"] else 24)
    return finish("ok" if covered >= 24 or "page2_count" in out["pagination"] else "list_too_short")


if __name__ == "__main__":
    sys.exit(main())
