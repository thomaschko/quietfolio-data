# -*- coding: utf-8 -*-
"""
us_pr_scan.py — 美股法說擴充 Phase 1:EDGAR 財報新聞稿(8-K Item 2.02)題材詞掃描(2026-11-08)
============================================================
取得方式(依 Phase 0 探測結論,只用 EDGAR 官方端點):
  1. 每日索引 Archives/edgar/daily-index/YYYY/QTRn/form.YYYYMMDD.idx(美東當晚發布,約台北隔天 10:00)
     → 列出當天所有 8-K / 8-K/A,只留 us_universe.json 內的 CIK。
  2. 該公司 submissions(data.sec.gov)→ 這份申報的 items 是否含 2.02(每次執行每家最多查 1 次)。
  3. 申報明細頁 {accession}-index.htm → 依 Type 欄找 EX-99.x(探測發現檔名不一定含 ex99,用 Type 判斷)。
  4. 取 EX-99.x:htm / txt 抽文字;pdf 或抽不出 → unparsed;單份 > max_bytes → too_large。
詞比對:有 theme_terms_bilingual.json(使用者維護)就用它的 en 詞;沒有就沿用 src6 KEYWORDS(讀取,不修改)。
       都沒有 → 停止,不自行發明詞表。比對規則同 us_transcripts.term_regex(詞邊界、縮寫區分大小寫、空白 / 連字號)。
輸出 us_pr_hits.json(保留 90 天、≤ 45,000 字元;超過先縮片段、再刪最舊):
  filed, ticker, cik, acc, url, hits [[詞, 次數, [片段 ≤200 字元 × ≤3]]]
狀態 us_pr_state.json:水位(已讀到的索引日期)、已處理 accession(去重)、backlog(時間不夠時留到下次,由新到舊處理)。
新聞稿是公司自述;詞命中不等於題材成立;不輸出買賣建議,不自動加入 watchlist。
"""
import datetime as dt
import json
import os
import re
import sys
import time
from html import unescape

import edgar_http as eh
import us_transcripts as ut

CFG = {"max_bytes": 2 * 1024 * 1024, "time_budget_min": 25, "keep_days": 90, "first_run_days": 7,
       "max_snippets": 3, "snippet_len": 200}
OUT = "us_pr_hits.json"
STATE = "us_pr_state.json"
UNIVERSE = "us_universe.json"
SIZE_LIMIT = 45000
IDX_URL = "https://www.sec.gov/Archives/edgar/daily-index/%d/QTR%d/form.%s.idx"
SUB_URL = "https://data.sec.gov/submissions/CIK%010d.json"
FILING_INDEX = "https://www.sec.gov/Archives/edgar/data/%d/%s/%s-index.htm"
DOC_URL = "https://www.sec.gov/Archives/edgar/data/%d/%s/%s"


# ------------------------------------------------------------------ 詞表
def load_terms(base="."):
    p = os.path.join(base, "theme_terms_bilingual.json")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            arr = json.load(f)
        return {x["id"]: list(x.get("en") or []) for x in arr if x.get("id") and x.get("en")}, "theme_terms_bilingual.json"
    return ut.load_terms(os.path.join(base, "earnings_keywords.py")), "earnings_keywords.py KEYWORDS"


# ------------------------------------------------------------------ 解析
def parse_daily_index(text):
    """form.YYYYMMDD.idx → [(form, company, cik, date, filename)](只留 8-K、8-K/A)"""
    out = []
    started = False
    for line in text.splitlines():
        if line.startswith("-----"):
            started = True
            continue
        if not started or not line.strip():
            continue
        m = re.match(r"^(8-K(?:/A)?)\s+(.*?)\s+(\d{1,10})\s+(\d{8})\s+(\S+)\s*$", line)
        if m:
            out.append((m.group(1), m.group(2).strip(), int(m.group(3)), m.group(4), m.group(5)))
    return out


def accession_of(filename):
    """edgar/data/320193/0000320193-26-000101.txt → 0000320193-26-000101"""
    m = re.search(r"(\d{10}-\d{2}-\d{6})", filename)
    return m.group(1) if m else None


def items_202(sub_json, acc):
    rec = (sub_json.get("filings") or {}).get("recent") or {}
    accs = rec.get("accessionNumber") or []
    if acc not in accs:
        return None                     # 不在 recent(太舊或太新)→ 未知
    i = accs.index(acc)
    return "2.02" in str((rec.get("items") or [""] * (i + 1))[i])


def exhibits_from_index(html):
    """申報明細頁 → [(type, filename)],只留 EX-99.x"""
    out = []
    for row in re.findall(r"(?is)<tr[^>]*>(.*?)</tr>", html):
        cells = re.findall(r"(?is)<td[^>]*>(.*?)</td>", row)
        if len(cells) < 4:
            continue
        typ = re.sub(r"<[^>]+>", "", cells[3]).strip().upper()
        href = re.search(r'(?i)href="([^"]+)"', cells[2])
        if typ.startswith("EX-99") and href:
            out.append((typ, href.group(1).rsplit("/", 1)[-1]))
    return out


def html_text(s):
    s = re.sub(r"(?is)<(script|style).*?</\1>", " ", s)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", unescape(s)).strip()


def match_text(text, compiled):
    """→ [[詞, 次數, [片段]]];原文不保留"""
    out = []
    for (g, w), rx in compiled.items():
        ms = list(rx.finditer(text))
        if ms:
            out.append(["%s|%s" % (g, w), len(ms), [ut.snippet(text, m)[:CFG["snippet_len"]] for m in ms[:CFG["max_snippets"]]]])
    return out


# ------------------------------------------------------------------ 主流程
def _read(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def fit(items):
    doc_items = list(items)
    for step in ("full", "one_snippet", "no_snippet", "trim"):
        if step == "one_snippet":
            for x in doc_items:
                for h in x["hits"]:
                    h[2] = h[2][:1]
        elif step == "no_snippet":
            for x in doc_items:
                for h in x["hits"]:
                    h[2] = []
        elif step == "trim":
            while doc_items and len(json.dumps(doc_items, ensure_ascii=False, separators=(",", ":"))) > SIZE_LIMIT - 3000:
                doc_items.pop()          # 由新到舊排序,刪最舊
        s = json.dumps(doc_items, ensure_ascii=False, separators=(",", ":"))
        if len(s) <= SIZE_LIMIT - 3000:
            return doc_items, (None if step == "full" else step)
    return doc_items, "trim"


def run(edgar=None, base=".", now=None, clock=time.time):
    j = lambda p: os.path.join(base, p)
    now = now or dt.datetime.now(dt.timezone.utc)
    today = now.date()
    t0 = clock()
    log = {"started": now.isoformat(timespec="seconds"), "indexes": [], "filings_seen": 0, "is_202": 0,
           "processed": 0, "unparsed": 0, "too_large": 0, "stopped": None}
    terms, terms_src = load_terms(base)
    compiled = ut.compile_terms(terms)
    uni = _read(j(UNIVERSE), None)
    if not uni or not uni.get("companies"):
        raise SystemExit("沒有 us_universe.json,先執行 us_universe.py")
    by_cik = {c[0]: c for c in uni["companies"]}
    edgar = edgar or eh.Edgar()
    st = _read(j(STATE), {"watermark": None, "processed": {}, "backlog": [], "runs": []})
    out = _read(j(OUT), {"items": []})
    items = out.get("items") or []
    # 1. 讀水位之後的每日索引(還沒發布的日子留到下次)
    wm = dt.date.fromisoformat(st["watermark"]) if st.get("watermark") else today - dt.timedelta(days=CFG["first_run_days"] + 1)
    pending = list(st.get("backlog") or [])
    d = wm + dt.timedelta(days=1)
    try:
        while d < today:
            if d.weekday() < 5:
                q = (d.month - 1) // 3 + 1
                r = edgar.get(IDX_URL % (d.year, q, d.strftime("%Y%m%d")))
                if r.status_code == 404:
                    if (today - d).days <= 2:
                        break            # 可能還沒發布 → 不前進水位
                    log["indexes"].append([d.isoformat(), "no_index"])
                elif r.status_code == 200:
                    rows = [x for x in parse_daily_index(r.text) if x[2] in by_cik]
                    log["indexes"].append([d.isoformat(), len(rows)])
                    for form, comp, cik, fdate, fn in rows:
                        acc = accession_of(fn)
                        if acc and acc not in st["processed"] and not any(p[1] == acc for p in pending):
                            pending.append([fdate, acc, cik])
                else:
                    log["stopped"] = "每日索引 HTTP %s(%s)" % (r.status_code, d)
                    break
            st["watermark"] = d.isoformat()
            d += dt.timedelta(days=1)
        # 2. 由新到舊處理;時間不夠就留 backlog
        pending.sort(key=lambda p: (p[0], p[1]), reverse=True)
        log["filings_seen"] = len(pending)
        subs = {}
        left = []
        for k, (fdate, acc, cik) in enumerate(pending):
            if clock() - t0 > CFG["time_budget_min"] * 60:
                left = pending[k:]
                log["stopped"] = "時間預算用完,%d 份留到下次" % len(left)
                break
            if cik not in subs:
                rr = edgar.get(SUB_URL % cik)
                subs[cik] = rr.json() if rr.status_code == 200 else {}
            flag = items_202(subs[cik], acc)
            if flag is None:
                st["processed"][acc] = [today.isoformat(), "unknown_items"]
                continue
            if not flag:
                st["processed"][acc] = [today.isoformat(), "not_2.02"]
                continue
            log["is_202"] += 1
            nodash = acc.replace("-", "")
            ri = edgar.get(FILING_INDEX % (cik, nodash, acc))
            exs = exhibits_from_index(ri.text) if ri.status_code == 200 else []
            hits, status, url = [], "no_ex99", None
            for typ, name in exs[:2]:
                url = DOC_URL % (cik, nodash, name)
                ext = name.rsplit(".", 1)[-1].lower()
                if ext not in ("htm", "html", "txt"):
                    status = "unparsed"
                    continue
                rd = edgar.get(url)
                if rd.status_code != 200:
                    status = "http_%s" % rd.status_code
                    continue
                if len(rd.content) > CFG["max_bytes"]:
                    status = "too_large"
                    continue
                text = html_text(rd.text) if ext != "txt" else rd.text
                if len(text) < 200:
                    status = "unparsed"
                    continue
                hits += match_text(text, compiled)
                status = "ok"
                del text
            log["processed"] += 1
            log["unparsed"] += status == "unparsed"
            log["too_large"] += status == "too_large"
            st["processed"][acc] = [today.isoformat(), status]
            c = by_cik[cik]
            merged = {}
            for k_, n, sn in hits:
                m = merged.setdefault(k_, [k_, 0, []])
                m[1] += n
                m[2] = (m[2] + sn)[:CFG["max_snippets"]]
            items.append({"filed": "%s-%s-%s" % (fdate[:4], fdate[4:6], fdate[6:]), "ticker": c[1], "cik": cik,
                          "acc": acc, "url": url, "status": status,
                          "hits": sorted(merged.values(), key=lambda h: (-h[1], h[0]))})
        st["backlog"] = left
    except eh.EdgarAbort as e:
        log["stopped"] = "EDGAR 停止:%s" % e
        st["backlog"] = [p for p in pending if p[1] not in st["processed"]]
    # 3. 輸出(保留 90 天、去重、由新到舊)
    cutoff = (today - dt.timedelta(days=CFG["keep_days"])).isoformat()
    seen, keep = set(), []
    for x in sorted(items, key=lambda x: (x["filed"], x["acc"]), reverse=True):
        if x["filed"] >= cutoff and x["acc"] not in seen:
            seen.add(x["acc"])
            keep.append(x)
    keep, note = fit(keep)
    # 去重只需涵蓋 backlog 與重跑同一天的情況(水位已防止重讀),保留 14 天即可,避免狀態檔變大
    cut_proc = (today - dt.timedelta(days=14)).isoformat()
    st["processed"] = {a: v for a, v in st["processed"].items() if v[0] >= cut_proc}
    log["requests"] = edgar.count
    st["runs"] = (st.get("runs") or [])[-9:] + [log]
    doc = {"generated": now.isoformat(timespec="seconds"), "terms_source": terms_src, "watermark": st["watermark"],
           "backlog": len(st["backlog"]), "size_note": note,
           "fields": {"hits": "[詞, 次數, [片段 ≤200 字元]]", "status": "ok / no_ex99 / unparsed / too_large / http_xxx"},
           "notes": ["財報新聞稿(8-K Item 2.02 附件 EX-99.x)為公司自述,未經查證;詞命中不等於題材成立,不是買賣建議",
                     "外國私人發行人(6-K,例如 TSM)不在範圍內"],
           "items": keep}
    s = json.dumps(doc, ensure_ascii=False, separators=(",", ":"))
    s_state = json.dumps(st, ensure_ascii=False, separators=(",", ":"))
    if len(s) > SIZE_LIMIT:
        log["stopped"] = (log["stopped"] or "") + ";輸出超過字數,保留舊檔"
        print(json.dumps(log, ensure_ascii=False))
        return 1, log
    _write(j(OUT), s)
    _write(j(STATE), s_state)
    print(json.dumps(log, ensure_ascii=False))       # 只印計數,不印新聞稿內文
    return 0, log


def main():
    try:
        code, _ = run()
    except eh.MissingUA as e:
        print(str(e))
        return 2
    return code


if __name__ == "__main__":
    sys.exit(main())
