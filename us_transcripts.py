# -*- coding: utf-8 -*-
"""
us_transcripts.py — 美股法說逐字稿追蹤(2026-11-08,自製;只用 src6 既有的 Alpha Vantage key)
============================================================
目的:把 src6 固定名單(earnings_keywords.py)擴大成「題材籃子 + src6 + 使用者種子清單」,
     在 Alpha Vantage 免費額度內,每天依固定優先順序抓「財報已過、這季還沒抓過」的公司逐字稿,
     只輸出題材詞命中次數與短片段。全部是描述 / 題材候選用途,不影響任何收錄、排序、門檻,不自動加入 watchlist。
規則:
  - 金鑰只讀環境變數 ALPHAVANTAGE_KEY;不寫入輸出、log。
  - 逐字稿原文只在記憶體中處理,不寫入 repo、log、結果檔;輸出只有詞命中次數與每詞最多 3 個 ≤ 200 字元片段。
  - 額度:每天最多 DAILY_BUDGET 次(含 1 次財報行事曆);UTC 週一不跑、src6 當天已跑也不跑(為 src6 保留)。
    回應出現額度 / 限速訊息 → 當天立即停止,不重試、不換來源。
  - 詞表:沿用 src6 的 KEYWORDS(使用者維護,讀取不修改);之後若有 theme_terms_bilingual.json 再改讀它。
  - 優先順序(先登記,不事後調整):行事曆顯示已發表財報 → 出現在幾個題材籃子(多者先)→ 屬於 src6 → 代號字母序。
輸出:
  us_transcript_hits.json   每家最近 2 季:詞命中(公司講稿 / 分析師問答分開計數)、片段;各詞跨公司彙總(本季 vs 上季)
  us_transcript_state.json  每家每季的抓取狀態(ok / empty / quota / error)、財報行事曆快照
兩者 ≤ 45,000 字元;超過先刪片段、再刪較舊的季;失敗保留舊檔。
"""
import ast
import csv
import datetime as dt
import io
import json
import os
import re
import sys
import time

import requests

AV_URL = "https://www.alphavantage.co/query"
OUT_HITS = "us_transcript_hits.json"
OUT_STATE = "us_transcript_state.json"
SIZE_LIMIT = 45000
DAILY_BUDGET = 20           # 免費 25 次 / 天,保留 5 次給 src6 手動重跑
GAP = 2.0                   # 每次請求間隔(AV 免費版另有每分鐘限制,保守)
EMPTY_RETRY_DAYS = 7        # 這季還沒有逐字稿 → 7 天後再試
MAX_SNIPPETS = 3
SNIPPET_LEN = 200
KEEP_QUARTERS = 2
UA = "quietfolio/1.0"
QUOTA_RE = re.compile(r"(?i)rate limit|requests per day|premium|frequency|thank you for using alpha vantage")


# ------------------------------------------------------------------ 名單與詞表(讀取,不修改)
def _ast_assign(path, name):
    try:
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
    except (OSError, SyntaxError):
        return None
    for n in tree.body:
        if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == name:
            try:
                return ast.literal_eval(n.value)
            except ValueError:
                return None
    return None


def load_universe(src6_path="earnings_keywords.py", baskets_path="intl_theme_baskets.json", seed_path="us_seed_tickers.txt"):
    """→ {ticker: {"src6": bool, "baskets": [題材], "seed": bool}}"""
    uni = {}
    for t in (_ast_assign(src6_path, "COMPANIES") or {}):
        uni.setdefault(t.upper(), {"src6": False, "baskets": [], "seed": False})["src6"] = True
    try:
        with open(baskets_path, encoding="utf-8") as f:
            for theme, v in (json.load(f).get("themes") or {}).items():
                for t in v.get("us") or []:
                    uni.setdefault(t.upper(), {"src6": False, "baskets": [], "seed": False})["baskets"].append(theme)
    except (OSError, ValueError):
        pass
    if os.path.exists(seed_path):
        with open(seed_path, encoding="utf-8") as f:
            for line in f:
                t = line.split("#")[0].strip().upper()
                if t:
                    uni.setdefault(t, {"src6": False, "baskets": [], "seed": False})["seed"] = True
    return uni


def load_terms(src6_path="earnings_keywords.py"):
    """→ {群組: [詞]}(src6 KEYWORDS)"""
    kw = _ast_assign(src6_path, "KEYWORDS")
    if not kw:
        raise SystemExit("找不到詞表(earnings_keywords.py KEYWORDS),停止;不自行發明詞表")
    return kw


# ------------------------------------------------------------------ 詞比對
def term_regex(term):
    """詞邊界 + 大小寫:全大寫且 ≤ 4 字元的縮寫(CPO、ATE、DSP…)區分大小寫,避免一般英文誤判;
    其餘不分大小寫;空白 / 連字號互通(800V ↔ 800 V ↔ 800-V)。"""
    t = term.strip()
    m = re.fullmatch(r"(\d+)([A-Za-z]+)", t)
    if m:                                        # 800V ↔ 800 V ↔ 800-V
        body = re.escape(m.group(1)) + r"[\s\-]?" + re.escape(m.group(2))
    else:
        body = r"[\s\-]?".join(re.escape(p) for p in re.split(r"[\s\-]+", t))
    acronym = t.isupper() and len(t) <= 4
    flags = 0 if acronym else re.IGNORECASE
    plural = (r"s?" if acronym else r"(?:e?s)?") if t[-1:].isalpha() else ""   # GPUs、lead times
    return re.compile(r"(?<![A-Za-z0-9])" + body + plural + r"(?![A-Za-z0-9])", flags)


def compile_terms(terms):
    return {(g, w): term_regex(w) for g, words in terms.items() for w in words}


def split_sections(segments):
    """AV 段落 → (公司講稿文字列表, 問答文字列表):第一個職稱含 analyst 的發言之後算問答"""
    prepared, qa, in_qa = [], [], False
    for s in segments or []:
        title = str(s.get("title") or "")
        if not in_qa and re.search(r"(?i)analyst", title):
            in_qa = True
        (qa if in_qa else prepared).append(str(s.get("content") or ""))
    return prepared, qa


def snippet(text, m):
    a = max(0, m.start() - 90)
    b = min(len(text), m.end() + 90)
    return re.sub(r"\s+", " ", text[a:b]).strip()[:SNIPPET_LEN]


def match_transcript(segments, compiled):
    """→ {"群組|詞": {"p": n, "q": n, "s": [片段]}};原文不保留"""
    prepared, qa = split_sections(segments)
    out = {}
    for (g, w), rx in compiled.items():
        p = q = 0
        snips = []
        for sec, texts in (("p", prepared), ("q", qa)):
            for t in texts:
                for m in rx.finditer(t):
                    if sec == "p":
                        p += 1
                    else:
                        q += 1
                    if len(snips) < MAX_SNIPPETS:
                        snips.append(snippet(t, m))
        if p or q:
            out["%s|%s" % (g, w)] = {"p": p, "q": q, "s": snips}
    return out


# ------------------------------------------------------------------ 季別與優先順序
def recent_quarters(today, n=2):
    """最近 n 個已結束的日曆季(YYYYQn),新到舊"""
    y, q = today.year, (today.month - 1) // 3
    out = []
    for _ in range(n):
        if q == 0:
            y, q = y - 1, 4
        out.append("%dQ%d" % (y, q))
        q -= 1
    return out


def reported_since(calendar_hist, ticker, today, days=45):
    """行事曆快照中出現過、且財報日已過(近 days 天內)"""
    d = calendar_hist.get(ticker)
    if not d:
        return False
    try:
        rd = dt.date.fromisoformat(d)
    except ValueError:
        return False
    return rd <= today and (today - rd).days <= days


def candidates(universe, state, calendar_hist, today, quarter):
    out = []
    for t, info in universe.items():
        st = (state.get(t) or {}).get(quarter)
        if st and st.get("status") in ("ok",):
            continue
        if st and st.get("status") == "empty":
            last = dt.date.fromisoformat(st.get("at", "2000-01-01")[:10])
            if (today - last).days < EMPTY_RETRY_DAYS:
                continue
        rep = reported_since(calendar_hist, t, today)
        out.append((0 if rep else 1, -len(info["baskets"]), 0 if info["src6"] else 1, t))
    out.sort()
    return [x[3] for x in out]


# ------------------------------------------------------------------ Alpha Vantage
class Quota(Exception):
    pass


class AV:
    def __init__(self, key, session=None, sleep=time.sleep):
        self.key = key
        self.s = session or requests.Session()
        try:
            self.s.headers.update({"User-Agent": UA})
        except AttributeError:
            pass
        self.sleep = sleep
        self.used = 0
        self.last = None

    def _get(self, params):
        if self.last is not None:
            self.sleep(GAP)
        params = dict(params, apikey=self.key)
        r = self.s.get(AV_URL, params=params, timeout=60)
        self.last = time.time()
        self.used += 1
        if r.status_code in (403, 429):
            raise Quota("HTTP %s" % r.status_code)
        return r

    def calendar(self):
        r = self._get({"function": "EARNINGS_CALENDAR", "horizon": "3month"})
        txt = r.text
        if txt.lstrip().startswith("{"):
            msg = json.loads(txt)
            m = msg.get("Note") or msg.get("Information") or msg.get("Error Message") or ""
            raise Quota(m[:200])
        rows = list(csv.DictReader(io.StringIO(txt)))
        return {x["symbol"].upper(): x.get("reportDate") for x in rows if x.get("symbol")}

    def transcript(self, ticker, quarter):
        r = self._get({"function": "EARNINGS_CALL_TRANSCRIPT", "symbol": ticker, "quarter": quarter})
        try:
            j = r.json()
        except ValueError:
            return None
        m = j.get("Note") or j.get("Information")
        if m and QUOTA_RE.search(m):
            raise Quota(m[:200])
        tr = j.get("transcript")
        return tr if isinstance(tr, list) else []


# ------------------------------------------------------------------ 彙總與輸出
def aggregate(items, quarters):
    """各詞:本季提到的公司數、上季提到的公司數(同一批公司兩季都有資料才比較)"""
    cur, prev = quarters[0], quarters[1] if len(quarters) > 1 else None
    by = {}
    for t, qs in items.items():
        for q, rec in qs.items():
            for k in rec.get("hits", {}):
                d = by.setdefault(k, {"cur": [], "prev": []})
                if q == cur:
                    d["cur"].append(t)
                elif q == prev:
                    d["prev"].append(t)
    rows = []
    for k, d in by.items():
        g, w = k.split("|", 1)
        rows.append({"group": g, "term": w, "n_cur": len(d["cur"]), "n_prev": len(d["prev"]),
                     "new_this_q": sorted(set(d["cur"]) - set(d["prev"]))[:10]})
    rows.sort(key=lambda r: (-(r["n_cur"] - r["n_prev"]), -r["n_cur"], r["term"]))
    return rows


def fit(doc):
    s = json.dumps(doc, ensure_ascii=False, separators=(",", ":"))
    if len(s) <= SIZE_LIMIT:
        return s, None
    for t in doc["items"].values():
        for rec in t.values():
            for h in rec.get("hits", {}).values():
                h["s"] = h["s"][:1]
    s = json.dumps(doc, ensure_ascii=False, separators=(",", ":"))
    if len(s) <= SIZE_LIMIT:
        return s, "片段縮為每詞 1 個"
    for t in doc["items"].values():
        for rec in t.values():
            for h in rec.get("hits", {}).values():
                h["s"] = []
    s = json.dumps(doc, ensure_ascii=False, separators=(",", ":"))
    if len(s) <= SIZE_LIMIT:
        return s, "超過字數,片段全部省略"
    cur = doc["quarters"][0]
    for t in doc["items"]:
        doc["items"][t] = {q: v for q, v in doc["items"][t].items() if q == cur}
    s = json.dumps(doc, ensure_ascii=False, separators=(",", ":"))
    return s, "超過字數,只留本季、不含片段"


def write_atomic(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def src6_ran_today(today, path="earnings_keywords.json"):
    try:
        with open(path, encoding="utf-8") as f:
            return (json.load(f).get("generated_at") or "")[:10] == today.isoformat()
    except (OSError, ValueError):
        return False


def run(av=None, today=None, budget=DAILY_BUDGET, base="."):
    j = lambda p: os.path.join(base, p)
    today = today or dt.datetime.now(dt.timezone.utc).date()
    log = {"date": today.isoformat(), "used": 0, "fetched": [], "stopped": None}
    if today.weekday() == 0 or src6_ran_today(today, j("earnings_keywords.json")):
        log["stopped"] = "今天為 src6 保留 Alpha Vantage 額度,不執行"
        print(json.dumps(log, ensure_ascii=False))
        return 0, log
    terms = load_terms(j("earnings_keywords.py"))
    compiled = compile_terms(terms)
    universe = load_universe(j("earnings_keywords.py"), j("intl_theme_baskets.json"), j("us_seed_tickers.txt"))
    try:
        with open(j(OUT_STATE), encoding="utf-8") as f:
            st_doc = json.load(f)
    except (OSError, ValueError):
        st_doc = {"tickers": {}, "calendar": {}, "runs": []}
    try:
        with open(j(OUT_HITS), encoding="utf-8") as f:
            hits_doc = json.load(f)
    except (OSError, ValueError):
        hits_doc = {"items": {}}
    if av is None:
        key = os.environ.get("ALPHAVANTAGE_KEY", "")
        if not key:
            log["stopped"] = "skipped_no_key"
            print(json.dumps(log, ensure_ascii=False))
            return 0, log
        av = AV(key)
    quarters = recent_quarters(today, KEEP_QUARTERS)
    cur = quarters[0]
    state = st_doc.setdefault("tickers", {})
    items = hits_doc.setdefault("items", {})
    try:
        cal = av.calendar()
        # 行事曆只列未來的;記下每家最新一次看到的財報日,過了之後就知道「已發表」
        for t, d in cal.items():
            if t in universe:
                st_doc["calendar"][t] = d
        todo = candidates(universe, state, st_doc["calendar"], today, cur)
        for t in todo:
            if av.used >= budget:
                break
            segs = av.transcript(t, cur)
            rec = state.setdefault(t, {})
            if segs is None:
                rec[cur] = {"status": "error", "at": today.isoformat()}
                continue
            if not segs:
                rec[cur] = {"status": "empty", "at": today.isoformat()}
                continue
            hits = match_transcript(segs, compiled)
            n_chars = sum(len(str(s.get("content") or "")) for s in segs)
            del segs
            rec[cur] = {"status": "ok", "at": today.isoformat(), "chars": n_chars}
            items.setdefault(t, {})[cur] = {"fetched": today.isoformat(), "chars": n_chars, "hits": hits}
            log["fetched"].append(t)
    except Quota as e:
        log["stopped"] = "額度 / 限速:%s" % e
    except requests.RequestException as e:
        log["stopped"] = "連線失敗 %s" % type(e).__name__
    log["used"] = av.used
    # 只留最近 KEEP_QUARTERS 季
    for t in list(items):
        items[t] = {q: v for q, v in items[t].items() if q in quarters}
        if not items[t]:
            del items[t]
    hits_doc.update({"generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                     "quarters": quarters, "universe_size": len(universe),
                     "universe": {t: {"src6": v["src6"], "baskets": v["baskets"]} for t, v in sorted(universe.items())
                                  if t in items},
                     "terms_source": "earnings_keywords.py KEYWORDS",
                     "fields": {"p": "公司講稿命中次數", "q": "分析師問答命中次數", "s": "片段(≤3,≤200 字元)"},
                     "notes": ["法說內容為公司陳述,未經查證;詞命中不等於題材成立,不是買賣建議",
                               "第一個職稱含 analyst 的發言之後算問答;來源 Alpha Vantage EARNINGS_CALL_TRANSCRIPT"]})
    hits_doc["aggregate"] = aggregate(items, quarters)[:80]
    s_hits, note = fit(hits_doc)
    if note:
        hits_doc["size_note"] = note
        s_hits, _ = fit(hits_doc)
    st_doc["runs"] = (st_doc.get("runs") or [])[-30:] + [log]
    s_state = json.dumps(st_doc, ensure_ascii=False, separators=(",", ":"))
    if len(s_state) > SIZE_LIMIT:   # 狀態檔太大:只留近兩季
        for t in state:
            state[t] = {q: v for q, v in state[t].items() if q in quarters}
        s_state = json.dumps(st_doc, ensure_ascii=False, separators=(",", ":"))
    if len(s_hits) > SIZE_LIMIT or len(s_state) > SIZE_LIMIT:
        log["stopped"] = (log["stopped"] or "") + ";輸出超過字數,保留舊檔"
        print(json.dumps(log, ensure_ascii=False))
        return 1, log
    write_atomic(j(OUT_HITS), s_hits)
    write_atomic(j(OUT_STATE), s_state)
    print(json.dumps(log, ensure_ascii=False))   # 只印代號與次數,不印逐字稿
    return 0, log


if __name__ == "__main__":
    sys.exit(run()[0])
