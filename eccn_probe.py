# -*- coding: utf-8 -*-
"""
eccn_probe.py — 一次性探測:m.eccn.com(電子產品世界)「芯闻」能否當產業新聞觀察源(2026-10-20)
只探測,不接入雷達、不改任何現有檔案。輸出 eccn_probe_result.json。
規則:誠實 UA、請求間隔 ≥ 2 秒、總請求 ≤ 30;robots 不允許 / 403 / 451 / 驗證碼 / 登入牆 → 停止該路徑並記錄,
不換 header、不走代理/鏡像/快取繞過。只記錄標題、日期、連結、欄目,不存文章全文。
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
MIN_GAP = 2.0
MAX_REQ = 30
STABILITY_WAIT = 300
OUT = "eccn_probe_result.json"
BLOCK_STATUS = {401, 403, 451}
WALL_HINTS = ("验证码", "captcha", "请登录", "登录后", "滑动验证", "安全验证", "访问验证")
SECTION_NAMES = ("芯闻", "芯品")
DATE_RE = re.compile(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})日?(?:\s*(\d{1,2}):(\d{2})(?::(\d{2}))?)?")

_session = requests.Session()
_session.headers.update({"User-Agent": UA})
_state = {"n": 0, "last": 0.0}
notes = []


def get(url, timeout=20):
    """節流 + 計數;超過上限回 None。"""
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
    """header charset → meta charset → apparent。回傳 (text, header_charset, meta_charset)。"""
    hdr = None
    m = re.search(r"charset=([\w-]+)", r.headers.get("Content-Type", ""), re.I)
    if m:
        hdr = m.group(1).lower()
    head = r.content[:4000].decode("ascii", "ignore")
    mm = re.search(r"<meta[^>]+charset=[\"']?([\w-]+)", head, re.I)
    meta = mm.group(1).lower() if mm else None
    enc = meta or hdr or r.apparent_encoding or "utf-8"
    try:
        return r.content.decode(enc, "replace"), hdr, meta
    except LookupError:
        return r.content.decode("utf-8", "replace"), hdr, meta


def walled(status, text):
    if status in BLOCK_STATUS:
        return f"HTTP {status}"
    low = (text or "")[:20000].lower()
    for h in WALL_HINTS:
        if h.lower() in low:
            return f"頁面含「{h}」"
    return None


def parse_date(s):
    if not s:
        return None
    m = DATE_RE.search(s)
    if not m:
        return None
    y, mo, d, hh, mi, ss = m.groups()
    try:
        x = dt.datetime(int(y), int(mo), int(d), int(hh or 0), int(mi or 0), int(ss or 0))
    except ValueError:
        return None
    return x.strftime("%Y-%m-%d %H:%M:%S") if hh else x.strftime("%Y-%m-%d")


# ============================================================
def check_robots(host):
    url = f"https://{host}/robots.txt"
    r = get(url)
    res = {"status": r.status_code if r is not None else 0, "allows_list": None, "notes": "", "sitemaps": []}
    if r is None:
        res["notes"] = "無法連線"
        return res, None
    rp = urllib.robotparser.RobotFileParser()
    if r.status_code == 200:
        text = r.text
        rp.parse(text.splitlines())
        res["sitemaps"] = re.findall(r"(?im)^\s*sitemap:\s*(\S+)", text)
        res["notes"] = "rules: " + " | ".join(l.strip() for l in text.splitlines()
                                              if re.match(r"\s*(user-agent|allow|disallow|crawl-delay)", l, re.I))[:600]
        cd = rp.crawl_delay(UA) or rp.crawl_delay("*")
        if cd:
            res["notes"] += f" | crawl-delay {cd}"
    elif r.status_code in (404, 410):
        rp.parse([])                                  # 沒有 robots.txt → 依慣例視為允許
        res["notes"] = f"robots.txt {r.status_code},視為無限制"
    else:
        res["notes"] = f"robots.txt HTTP {r.status_code},保守視為不允許"
        rp.parse(["User-agent: *", "Disallow: /"])
    return res, rp


def allowed(rp, url):
    return bool(rp and rp.can_fetch(UA, url) and rp.can_fetch("*", url))


def extract_sections(html, base):
    """找『芯闻』『芯品』區塊:以標題文字定位,往後抓到下一個區塊標題前的文章連結。"""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        notes.append("沒有 bs4,區塊解析略過")
        return [], []
    soup = BeautifulSoup(html, "html.parser")
    sections, items = [], []
    heads = [t for t in soup.find_all(string=True) if t.strip() in SECTION_NAMES]
    for h in heads:
        name = h.strip()
        box = h.parent
        # 往上找到含多個連結的容器
        for _ in range(6):
            if box is None:
                break
            if len(box.find_all("a", href=True)) >= 3:
                break
            box = box.parent
        if box is None:
            continue
        links = []
        for a in box.find_all("a", href=True):
            title = a.get_text(" ", strip=True)
            href = urljoin(base, a["href"])
            if len(title) < 6 or title in SECTION_NAMES or "展开" in title:
                continue
            ctx = a.parent.get_text(" ", strip=True) if a.parent else ""
            rc = re.search(r"(\d[\d,]*)\s*(?:阅读|浏览|次)", ctx)
            links.append({"title": title[:120], "url": href, "published_at": parse_date(ctx),
                          "read_count": int(rc.group(1).replace(",", "")) if rc else None, "category": name})
        more = box.find(string=re.compile("展开更多"))
        more_attrs = {}
        if more is not None and more.parent is not None:
            el = more.parent
            more_attrs = {k: (v if isinstance(v, str) else " ".join(v)) for k, v in el.attrs.items()}
        sections.append({"name": name, "items_in_html": len(links), "more_button": more is not None,
                         "more_attrs": more_attrs})
        items.extend(links)
    # 去重
    seen, uniq = set(), []
    for it in items:
        if it["url"] not in seen:
            seen.add(it["url"]); uniq.append(it)
    return sections, uniq


def find_ajax(html, base):
    """在 HTML 與內嵌 script 裡找『展开更多』可能用的請求 URL(不跑瀏覽器)。"""
    cands = set()
    for m in re.finditer(r"""(?:url\s*[:=]\s*|\$\.(?:get|post|ajax|getJSON)\(\s*|fetch\(\s*|data-(?:url|href|api|src)\s*=\s*)["']([^"']+)["']""", html, re.I):
        u = m.group(1)
        if any(k in u.lower() for k in ("ajax", "api", "json", "page", "more", "list", "load")):
            cands.add(urljoin(base, u))
    scripts = [urljoin(base, s) for s in re.findall(r"""<script[^>]+src=["']([^"']+)["']""", html, re.I)]
    own = [s for s in scripts if urlparse(s).netloc.endswith("eccn.com")]
    return sorted(cands), own


def main():
    out = {"checked_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
           "robots": {}, "home": {"status": 0, "encoding": "", "sections": []},
           "list_source": {"type": "unknown", "url_pattern": "", "params": {}, "fields": []},
           "sample": [], "date": {"in_list": False, "in_article": False, "format": "", "timezone": ""},
           "stability": {"top_changed_between_runs": None}, "verdict": "unknown", "notes": notes,
           "requests_used": 0}

    def finish(verdict):
        out["verdict"] = verdict
        out["requests_used"] = _state["n"]
        with open(OUT, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    # 1) robots
    rw, rp_www = check_robots("www.eccn.com")
    rm, rp = check_robots("m.eccn.com")
    home_url = "https://m.eccn.com/"
    rm["allows_list"] = allowed(rp, home_url) if rp else None
    rw["allows_list"] = allowed(rp_www, "https://www.eccn.com/") if rp_www else None

    def allowed_any(url):
        host = urlparse(url).netloc
        if host == "m.eccn.com":
            return allowed(rp, url)
        if host == "www.eccn.com":
            return allowed(rp_www, url)
        return False                                  # 站外連結不抓
    out["robots"] = {"www": rw, "m": rm}
    if rm["status"] == 0:
        notes.append("m.eccn.com 無法連線")
        return finish("unreachable")
    if not rm["allows_list"]:
        notes.append("m.eccn.com robots.txt 不允許首頁,停止")
        return finish("robots_blocked")

    # 2) 首頁
    r = get(home_url)
    if r is None:
        return finish("unreachable")
    html, hdr, meta = decode(r)
    out["home"]["status"] = r.status_code
    out["home"]["encoding"] = f"header={hdr} meta={meta}"
    t = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    out["home"]["title"] = t.group(1).strip()[:120] if t else ""
    out["home"]["final_url"] = r.url
    w = walled(r.status_code, html)
    if w:
        notes.append(f"首頁被擋:{w},停止")
        return finish("unreachable" if r.status_code in (403, 451) else "login_wall")
    if r.status_code != 200:
        notes.append(f"首頁 HTTP {r.status_code}")
        return finish("unreachable")
    sections, items = extract_sections(html, r.url)
    out["home"]["sections"] = sections
    top_first = [it["url"] for it in items[:5]]

    # 3) 列表來源
    ajax, own_js = find_ajax(html, r.url)
    out["list_source"]["ajax_candidates_in_html"] = ajax[:10]
    out["list_source"]["own_scripts"] = own_js[:10]
    if items:
        out["list_source"]["type"] = "html"
        out["list_source"]["fields"] = ["title", "url", "category"] + \
            (["published_at"] if any(i["published_at"] for i in items) else []) + \
            (["read_count"] if any(i["read_count"] is not None for i in items) else [])
    if not ajax:
        for js in own_js[:3]:                         # 只看站內 script,最多 3 個
            if not allowed_any(js):
                notes.append(f"robots 不允許 {js},略過")
                continue
            jr = get(js)
            if jr is None or jr.status_code != 200:
                continue
            a2, _ = find_ajax(jr.text, js)
            ajax += a2
        out["list_source"]["ajax_candidates_in_js"] = sorted(set(ajax))[:10]

    # 4) 芯闻前 2 頁(找得到 JSON/分頁端點才抓)
    page_tpl = None
    for u in ajax:
        if re.search(r"(page|p|pageindex|pageno)=\d+", u, re.I) or "{page}" in u:
            page_tpl = u
            break
    if page_tpl:
        out["list_source"]["url_pattern"] = page_tpl
        for pg in (1, 2):
            u = re.sub(r"((?:page|p|pageindex|pageno)=)\d+", lambda m: m.group(1) + str(pg), page_tpl, flags=re.I)
            if not allowed_any(u):
                notes.append(f"robots 不允許(或站外){u}")
                break
            jr = get(u)
            if jr is None:
                break
            w = walled(jr.status_code, jr.text)
            if w:
                notes.append(f"列表端點被擋:{w},停止")
                break
            try:
                data = jr.json()
                out["list_source"]["type"] = "json"
                out["list_source"].setdefault("json_top_keys", list(data.keys())[:20] if isinstance(data, dict) else "list")
                rows = data if isinstance(data, list) else next((v for v in data.values() if isinstance(v, list)), [])
                if rows and isinstance(rows[0], dict):
                    out["list_source"]["fields"] = list(rows[0].keys())[:30]
                for row in rows[:20]:
                    if not isinstance(row, dict):
                        continue
                    title = next((row[k] for k in ("title", "Title", "name") if k in row), "")
                    link = next((row[k] for k in ("url", "link", "href", "Url") if k in row), "")
                    dstr = next((str(row[k]) for k in row if re.search(r"time|date|pub", k, re.I)), None)
                    items.append({"title": str(title)[:120], "url": urljoin(home_url, str(link)), "category": f"芯闻 p{pg}",
                                  "published_at": parse_date(dstr), "read_count": next(
                                      (row[k] for k in row if re.search(r"read|view|click|hit", k, re.I)), None)})
            except ValueError:
                more_html, _, _ = decode(jr)
                _, more_items = extract_sections("<div>芯闻" + more_html + "</div>", u)
                for it in more_items:
                    it["category"] = f"芯闻 p{pg}"
                items.extend(more_items)
    else:
        notes.append("沒有找到『展开更多』的分頁端點(只取首頁 HTML 內的條目)")

    out["sample"] = items[:20]
    out["date"]["in_list"] = any(i.get("published_at") for i in items)

    # 5) 文章頁日期(最多 3 篇)
    arts = [i for i in items if i.get("category", "").startswith("芯闻")][:3] or items[:3]
    fmts = []
    for it in arts:
        if not allowed_any(it["url"]):
            notes.append(f"robots 不允許(或站外)文章頁 {it['url']},略過")
            continue
        ar = get(it["url"])
        if ar is None:
            continue
        ahtml, _, _ = decode(ar)
        w = walled(ar.status_code, ahtml)
        if w:
            notes.append(f"文章頁被擋:{w}({it['url']}),停止文章探測")
            break
        found = None
        for pat in (r"""<meta[^>]+(?:property|name)=["'](?:article:published_time|pubdate|publishdate|og:release_date)["'][^>]+content=["']([^"']+)""",
                    r"""<time[^>]*datetime=["']([^"']+)""",
                    r"(?:发布时间|发布日期|时间)[:：]?\s*(20\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2}日?(?:\s*\d{1,2}:\d{2}(?::\d{2})?)?)"):
            m = re.search(pat, ahtml, re.I)
            if m:
                found = (pat[:25], m.group(1))
                break
        if not found:
            body = re.sub(r"<[^>]+>", " ", ahtml)
            m = DATE_RE.search(body)
            if m:
                found = ("body-first-date", m.group(0))
        it["article_date_raw"] = found[1] if found else None
        it["article_date_src"] = found[0] if found else None
        if found and not it.get("published_at"):
            it["published_at"] = parse_date(found[1])
        if found:
            fmts.append(found[1])
    out["date"]["in_article"] = any(i.get("article_date_raw") for i in arts)
    out["date"]["format"] = " / ".join(sorted(set(re.sub(r"\d", "9", f) for f in fmts)))[:120]
    out["date"]["timezone"] = "未標示,推定 Asia/Shanghai(UTC+8)" if fmts and not any("+" in f or "Z" in f for f in fmts) else \
        ("含時區" if fmts else "")
    out["sample"] = items[:20]

    # 6) 穩定性:5 分鐘後再抓一次首頁
    print(f"等待 {STABILITY_WAIT} 秒後重抓首頁…", flush=True)
    time.sleep(STABILITY_WAIT)
    r2 = get(home_url)
    if r2 is not None and r2.status_code == 200:
        h2, _, _ = decode(r2)
        _, items2 = extract_sections(h2, r2.url)
        top2 = [it["url"] for it in items2[:5]]
        out["stability"] = {"top_changed_between_runs": top_first != top2, "first_top": top_first[:3], "second_top": top2[:3]}

    ok = out["robots"]["m"]["allows_list"] and items and (out["date"]["in_list"] or out["date"]["in_article"])
    if not items:
        notes.append("首頁沒有解析出任何條目")
        return finish("unknown")
    return finish("ok" if ok else "no_date")


if __name__ == "__main__":
    sys.exit(main())
