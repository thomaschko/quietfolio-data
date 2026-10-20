# -*- coding: utf-8 -*-
"""
eccn_page_probe.py — 一次性補探測:驗證 m.eccn.com「芯闻」列表的「展开更多」翻頁(2026-10-20)
2026-10-20 eccn_list_probe.py:列表頁 10 則只涵蓋 2.8 小時,HTML 沒有分頁連結;站方 get_ajax_list.js 的
get_ajax_more_list() 寫明翻頁方式 = POST {more_news_url},data {p: 頁碼, title: 按鈕 myattr}。
這裡照 JS 寫法送 p=2、p=3 兩個請求(不改參數、不猜其他端點),確認回傳內容、每頁則數、與第 1 頁是否重複、
時間是否連續往前。誠實 UA、間隔 ≥ 2 秒、總請求 ≤ 6;robots 不允許 / 401 / 403 / 451 / 驗證碼 → 停止。
只記錄標題、網址、由網址推得的時間。輸出 eccn_page_probe_result.json。只探測,不接入。
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
MIN_GAP = 2.0
MAX_REQ = 6
OUT = "eccn_page_probe_result.json"
BLOCK = {401, 403, 451}
WALL_HINTS = ("验证码", "captcha", "请登录", "登录后", "滑动验证", "安全验证")
ITEM_RE = re.compile(r"""<a[^>]+href=["']([^"']*?/consultation/news/(\d{14})\d*\.html)["'][^>]*>(.*?)</a>""", re.S | re.I)

_s = requests.Session()
_s.headers.update({"User-Agent": UA})
_st = {"n": 0, "last": 0.0}
notes = []


def req(method, url, **kw):
    if _st["n"] >= MAX_REQ:
        notes.append(f"已達請求上限 {MAX_REQ}")
        return None
    w = MIN_GAP - (time.time() - _st["last"])
    if w > 0:
        time.sleep(w)
    _st["n"] += 1
    _st["last"] = time.time()
    try:
        r = _s.request(method, url, timeout=20, **kw)
    except Exception as e:
        notes.append(f"請求失敗 {method} {url}:{type(e).__name__}: {e}")
        return None
    r.encoding = r.encoding or "utf-8"
    print(f"[{_st['n']:02d}] {r.status_code} {method} {r.url} {kw.get('data') or ''} ({len(r.content)} bytes)", flush=True)
    return r


def walled(r):
    if r.status_code in BLOCK:
        return f"HTTP {r.status_code}"
    low = r.text[:20000].lower()
    return next((f"頁面含「{h}」" for h in WALL_HINTS if h.lower() in low), None)


def items(html, base):
    out, seen = [], set()
    for m in ITEM_RE.finditer(html):
        url = urljoin(base, m.group(1))
        title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(3))).strip()
        if url in seen or len(title) < 6:
            continue
        seen.add(url)
        try:
            ts = dt.datetime.strptime(m.group(2), "%Y%m%d%H%M%S").strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            ts = None
        out.append({"title": title[:120], "url": url, "ts": ts})
    return out


def summary(lst):
    ts = [x["ts"] for x in lst if x["ts"]]
    return {"count": len(lst), "newest": max(ts) if ts else None, "oldest": min(ts) if ts else None,
            "sample": lst[:3]}


def main():
    out = {"checked_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "robots_allows_ajax": None,
           "more_button": {}, "pages": {}, "verdict": "unknown", "notes": notes, "requests_used": 0}

    def finish(v):
        out["verdict"] = v
        out["requests_used"] = _st["n"]
        with open(OUT, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    r = req("GET", BASE + "/robots.txt")
    if r is None:
        return finish("unreachable")
    rp = urllib.robotparser.RobotFileParser()
    rp.parse(r.text.splitlines() if r.status_code == 200 else ([] if r.status_code in (404, 410) else ["User-agent: *", "Disallow: /"]))

    r = req("GET", LIST)
    if r is None or r.status_code != 200 or walled(r):
        notes.append(f"列表頁無法取得:{None if r is None else (walled(r) or r.status_code)}")
        return finish("unreachable")
    html = r.text
    p1 = items(html, r.url)
    out["pages"]["1"] = summary(p1)
    # 「展开更多」按鈕:myattr 與 onclick 裡的 URL 變數
    btn = re.search(r"""<a[^>]*get_ajax_more_list\(([^)]*)\)[^>]*>""", html, re.I)
    myattr = re.search(r"""myattr=["']([^"']*)["']""", btn.group(0)) if btn else None
    args = [a.strip().strip("'\"") for a in btn.group(1).split(",")] if btn else []
    var = args[2] if len(args) >= 3 else "more_news_url"
    vm = re.search(r"""var\s+%s\s*=\s*["']([^"']+)["']""" % re.escape(var), html)
    ajax_url = urljoin(r.url, vm.group(1)) if vm else None
    out["more_button"] = {"found": bool(btn), "args": args, "myattr": myattr.group(1) if myattr else None,
                          "url_var": var, "ajax_url": ajax_url}
    if not (btn and ajax_url):
        notes.append("列表頁沒有找到「展开更多」或它的 URL 變數,停止")
        return finish("unknown")
    allowed = urlparse(ajax_url).netloc == "m.eccn.com" and rp.can_fetch(UA, ajax_url) and rp.can_fetch("*", ajax_url)
    out["robots_allows_ajax"] = allowed
    if not allowed:
        return finish("robots_blocked")

    seen = {x["url"] for x in p1}
    prev_oldest = out["pages"]["1"]["oldest"]
    chain_ok = True
    for p in (2, 3):
        data = {"p": p, "title": myattr.group(1) if myattr else ""}
        rr = req("POST", ajax_url, data=data)
        if rr is None:
            chain_ok = False
            break
        w = walled(rr)
        if w or rr.status_code != 200:
            notes.append(f"p={p}:{w or 'HTTP ' + str(rr.status_code)},停止")
            chain_ok = False
            break
        lst = items(rr.text, LIST)
        s = summary(lst)
        s["content_type"] = rr.headers.get("Content-Type", "")
        s["bytes"] = len(rr.content)
        s["overlap_with_previous"] = len({x["url"] for x in lst} & seen)
        s["continues_back_in_time"] = bool(s["newest"] and prev_oldest and s["newest"] <= prev_oldest)
        out["pages"][str(p)] = s
        if not lst:
            notes.append(f"p={p} 沒有解析出任何條目(回傳開頭:{rr.text[:120]!r})")
            chain_ok = False
            break
        seen |= {x["url"] for x in lst}
        prev_oldest = s["oldest"]
    if not chain_ok:
        return finish("paging_failed")
    ps = [out["pages"][k] for k in ("2", "3")]
    good = all(x["count"] > 0 and x["overlap_with_previous"] == 0 and x["continues_back_in_time"] for x in ps)
    newest, oldest = out["pages"]["1"]["newest"], out["pages"]["3"]["oldest"]
    if newest and oldest:
        hours = (dt.datetime.fromisoformat(newest) - dt.datetime.fromisoformat(oldest)).total_seconds() / 3600
        out["span_hours_3_pages"] = round(hours, 1)
        per_page_h = hours / 3
        out["pages_needed_for_24h"] = int(-(-24 // per_page_h)) if per_page_h > 0 else None
    return finish("ok" if good else "paging_inconsistent")


if __name__ == "__main__":
    sys.exit(main())
