# -*- coding: utf-8 -*-
"""
theme_auto.py — 建議關注趨勢題材:自動選題材與個股、共振熱度、AI 三句話草稿、系統預設認錯條件(2026-11-09)
============================================================
一、自動選題材(select_themes)
  雷達 daily_digest.json 的詞 → theme_auto_map.json 的固定題材(對不到的詞列在 unmatched,不自行發明題材):
    發酵前(B)、關鍵字暴增(D、D2 surge/new)、雙邊確認(G dual_confirmed)、新題材(E)
  美股:us_tw_resonance.json 詞組標「共振 / 美股升溫」或近 30 天新聞稿 ≥ 3 家
  台股籌碼:題材個股中「集保大戶增 + 外資自營商買超(或共識頁融資+法人同向買)」≥ 2 檔且多於反向(同 us_tw_resonance 規則)
  共振熱度 = 以上 6 個來源中有幾個成立(0–6,逐項列出,不是加權分數;只用來排序題材與熱力圖上色)。
  固定題材(800V HVDC、CPO、ASIC)一定保留;其他需至少 1 個雷達或美股來源,依熱度、暴增倍數排序,總數 ≤ max_themes。
  個股:題材籃子 → My-TW-Coverage → 雷達點名,去重後最多 max_stocks_per_theme 檔。
  連續天數:auto_state.json 記錄每個題材有雷達 / 美股訊號的日期。
二、AI 三句話草稿(draft_notes):沿用 llm_client(Gemini → Groq → OpenRouter,免費額度);
  只給本頁已整理的資料(命中詞、個股名稱、美股詞組詞),不讓模型上網補數字;頁面標「AI 草稿,需人工核對」。
  輸入沒變且 7 天內 → 不重算。含禁用字眼(買進、賣出、推薦、目標價、主力、必漲)→ 丟棄。
三、系統預設認錯條件(auto_kill):個股第一次被分到「已證明 / 開始出現」時建立版本 0 兩條(月營收年增連 2 月 < 0、毛利率連 2 季下降),
  只新增不修改(kill_auto.json append-only);使用者在 Sheet 寫的任何版本(≥ 1)會取代,系統預設留在舊版本紀錄。
全部是研究排序用的描述,不是買賣訊號。
"""
import datetime as dt
import hashlib
import json
import os

FORBIDDEN = ("買進", "賣出", "推薦", "目標價", "主力", "必漲", "加碼", "減碼")
SIGNALS = [("preferment", "發酵前"), ("surge", "關鍵字暴增"), ("dual", "雙邊確認"), ("new", "新題材"),
           ("us", "美股升溫"), ("tw_chip", "台股籌碼同向")]
AI_REFRESH_DAYS = 7
AUTO_DIR = "theme_page"


def _read(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def match_terms(term, tmap):
    """雷達詞 → 題材 id 列表:完全相同(不分大小寫),或別名(≥ 3 字元)包含在詞中"""
    t = str(term or "").strip().lower()
    if not t:
        return []
    out = []
    for tid, d in tmap.items():
        for a in [d["name"]] + d.get("aliases", []):
            al = a.lower()
            if t == al or (len(al) >= 3 and al in t):
                out.append(tid)
                break
    return out


def radar_hits(digest, tmap):
    """→ ({theme_id: {signal: [詞]}}, {theme_id: [代號]}, {theme_id: 最大暴增倍數}, unmatched)"""
    hits, codes, ratio, unmatched = {}, {}, {}, []
    if not digest:
        return hits, codes, ratio, unmatched

    def add(sig, term, cs=(), r=None):
        ids = match_terms(term, tmap)
        if not ids:
            unmatched.append(term)
        for i in ids:
            hits.setdefault(i, {}).setdefault(sig, []).append(term)
            codes.setdefault(i, [])
            codes[i] += [c for c in cs or [] if c not in codes[i]]
            if r:
                ratio[i] = max(ratio.get(i, 0), r)
    for x in digest.get("B_preferment_themes") or []:
        add("preferment", x.get("theme"), x.get("codes"), x.get("ratio"))
    for x in digest.get("D_surge_themes") or []:
        add("surge", x.get("theme"), x.get("codes"), x.get("ratio"))
    for x in digest.get("D2_term_heat") or []:
        if x.get("status") in ("surge", "new"):
            add("surge", x.get("term"), x.get("codes"), x.get("ratio"))
    for x in digest.get("G_cross_confirmed") or []:
        if x.get("dual_confirmed"):
            add("dual", x.get("theme"))
    for x in digest.get("E_new_candidates") or []:
        add("new", x.get("term"), x.get("stocks"))
    return hits, codes, ratio, sorted(set(u for u in unmatched if u))


def tw_chip_signal(codes, base):
    """題材個股的籌碼同向檔數(沿用 us_tw_resonance 的規則)"""
    import us_tw_resonance as R
    _, stocks, cmem, _ = R.tw_side(base)
    rows = [R.stock_row(c, stocks, cmem) for c in codes]
    up = sum(1 for r in rows if r[5] == "up")
    dn = sum(1 for r in rows if r[5] == "down")
    return up, dn, up >= R.RES_CFG["tw_min_n"] and up > dn


def select_themes(base=".", asof=None):
    asof = asof or dt.date.today()
    cfg = _read(os.path.join(base, "theme_auto_map.json"), {}) or {}
    tmap = cfg.get("themes") or {}
    digest = _read(os.path.join(base, "daily_digest.json"))
    cov = (_read(os.path.join(base, "margin_share_themes.json"), {}) or {}).get("themes") or {}
    bas = (_read(os.path.join(base, "intl_theme_baskets.json"), {}) or {}).get("themes") or {}
    res = _read(os.path.join(base, "us_tw_resonance.json"), {}) or {}
    groups = {g["g"]: g for g in res.get("groups") or []}
    hits, rcodes, ratio, unmatched = radar_hits(digest, tmap)
    cap = int(cfg.get("max_stocks_per_theme", 12))
    cands = []
    for tid, d in tmap.items():
        # 2026-11-09:順序改為 題材籃子(人工挑的核心公司)→ My-TW-Coverage → 雷達點名。
        # My-TW-Coverage 的分類常偏下游(例如 HBM 分類多為伺服器組裝廠),放前面會把核心公司擠出 12 檔上限
        stocks, seen = [], set()
        for name in d.get("baskets") or []:
            for c in (bas.get(name) or {}).get("tw") or []:
                if c not in seen:
                    seen.add(c)
                    stocks.append((c, "題材籃子 " + name))
        for name in d.get("coverage") or []:
            for c in cov.get(name) or []:
                if c not in seen:
                    seen.add(c)
                    stocks.append((c, "My-TW-Coverage " + name))
        for c in rcodes.get(tid) or []:
            if c not in seen:
                seen.add(c)
                stocks.append((c, "雷達點名"))
        stocks = stocks[:cap]
        sig = {k: v for k, v in (hits.get(tid) or {}).items()}
        ug = [groups[g] for g in d.get("us_groups") or [] if g in groups]
        us_on = [g["g"] for g in ug if g.get("label") in ("共振", "美股升溫") or (g.get("pr_n30") or 0) >= 3]
        if us_on:
            sig["us"] = us_on
        up, dn, chip_on = tw_chip_signal([c for c, _ in stocks], base) if stocks else (0, 0, False)
        if chip_on:
            sig["tw_chip"] = ["同向 %d / 反向 %d" % (up, dn)]
        cands.append({"id": tid, "name": d["name"], "radar_terms": d.get("aliases") or [], "us_groups": d.get("us_groups") or [],
                      "coverage": d.get("coverage") or [], "baskets": d.get("baskets") or [], "extra": [],
                      "_auto_stocks": stocks,
                      "auto": {"signals": {k: sig[k][:6] for k, _ in SIGNALS if k in sig}, "heat": sum(1 for k, _ in SIGNALS if k in sig),
                               "max_ratio": ratio.get(tid), "tw_up": up, "tw_down": dn, "n_stocks": len(stocks),
                               "us": [[g["g"], g.get("pr_n30"), g.get("tr_cur"), g.get("label")] for g in ug]}})
    fixed = cfg.get("fixed") or []
    chosen = [c for c in cands if c["id"] in fixed]
    rest = [c for c in cands if c["id"] not in fixed and any(k in c["auto"]["signals"] for k in ("preferment", "surge", "dual", "new", "us"))]
    rest.sort(key=lambda c: (-c["auto"]["heat"], -(c["auto"]["max_ratio"] or 0), c["id"]))
    chosen += rest[:max(0, int(cfg.get("max_themes", 15)) - len(chosen))]
    # 連續天數(有雷達或美股訊號的日子)
    sp = os.path.join(base, AUTO_DIR, "auto_state.json")
    st = _read(sp, {}) or {}
    day = (digest or {}).get("date")
    day = "%s-%s-%s" % (day[:4], day[4:6], day[6:]) if day and len(day) == 8 else asof.isoformat()
    for c in cands:
        s = st.setdefault(c["id"], {"first": None, "days": []})
        on = any(k in c["auto"]["signals"] for k in ("preferment", "surge", "dual", "new", "us"))
        if on and day not in s["days"]:
            s["days"] = (s["days"] + [day])[-90:]
            s["first"] = s["first"] or day
        hh = s.setdefault("heat", {})                       # 每天的訊號來源數(生命週期的熱度趨勢用)
        hh[day] = c["auto"]["heat"]
        for k in sorted(hh)[:-60]:
            del hh[k]
    for c in chosen:
        days = sorted(st.get(c["id"], {}).get("days") or [])
        streak, d0 = 0, dt.date.fromisoformat(day)
        while d0.isoformat() in days or (d0.weekday() >= 5 and streak):
            if d0.isoformat() in days:
                streak += 1
            d0 -= dt.timedelta(days=1)
        c["auto"]["streak_days"] = streak
        c["auto"]["first_seen"] = st.get(c["id"], {}).get("first")
        c["auto"]["heat_hist"] = sorted((st.get(c["id"], {}).get("heat") or {}).items())[-20:]
        c["auto"]["days_on"] = len(st.get(c["id"], {}).get("days") or [])
        c["auto"]["selected"] = "固定" if c["id"] in fixed else "自動"
    os.makedirs(os.path.join(base, AUTO_DIR), exist_ok=True)
    _write(sp, st)
    meta = {"digest_date": day, "unmatched": unmatched[:40], "n_candidates": len(cands),
            "signals": [list(s) for s in SIGNALS]}
    return chosen, meta


# ------------------------------------------------------------------ AI 三句話草稿
SYSTEM = None


def load_prompt(base="."):
    p = os.path.join(base, "prompts", "theme_three_sentences.md")
    try:
        with open(p, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


NOTE_VERSION = "v4"           # 2026-11-09:片段排除通用詞與易誤配詞、加法說逐字稿片段 → 版本變了會重寫一次草稿
MAX_SNIPPETS = 8
# 通用詞(幾乎每家財報都有)與易誤配詞(coherent 常是一般英文「一致的」)不當作題材證據片段
SNIPPET_SKIP_WORDS = {"supply chain", "capacity", "yield", "memory", "data center", "datacenter", "capital expenditure",
                      "capex", "taiwan", "coherent", "lead time", "analog"}
THEME_CORE_WORDS = {"memory": ("memory",), "ai_server": ("data center", "datacenter")}   # 這些題材的核心詞,不排除


def us_snippets(theme, base=".", days=30, asof=None):
    """近 30 天財報新聞稿(us_pr 單日檔)中,詞屬於題材的美股詞組、或詞 / 片段含題材別名的片段;每家公司最多 2 段,最多 8 段"""
    import glob
    asof = asof or dt.date.today()
    groups = set(theme.get("us_groups") or [])
    import re
    pats = [re.compile(r"(?<![A-Za-z0-9])" + re.escape(a) + r"(?![A-Za-z0-9])", re.I)
            for a in theme.get("radar_terms") or [] if len(a) >= 3]       # 完整單字才算(避免 OCS 配到 focus)
    cutoff = (asof - dt.timedelta(days=days)).isoformat()
    # 題材自己的核心詞不算通用詞(例如記憶體題材的 memory、AI 伺服器題材的 data center)
    own = {a.lower() for a in theme.get("radar_terms") or []} | {str(theme.get("name") or "").lower()}
    own |= set(THEME_CORE_WORDS.get(theme.get("id"), ()))
    skip = SNIPPET_SKIP_WORDS - own
    out, per = [], {}
    for fn in sorted(glob.glob(os.path.join(base, "us_pr", "*.json")), reverse=True):
        date = os.path.basename(fn)[:-5]
        if date < cutoff:
            break
        d = _read(fn, {}) or {}
        terms = d.get("terms")
        for x in d.get("items") or []:
            for t, s in x.get("sn") or []:
                term = terms[t] if terms is not None and isinstance(t, int) else t
                g, _, w = str(term).partition("|")
                txt = str(s)
                if w.lower() in skip:
                    continue
                if g in groups or any(p.search(w) for p in pats):
                    if per.get(x["ticker"], 0) >= 2 or any(txt == o[2] for o in out):
                        continue
                    per[x["ticker"]] = per.get(x["ticker"], 0) + 1
                    out.append([date, x["ticker"], txt, w])
    # 法說逐字稿片段(us_transcripts/snippets_<季>.json,有就加;公司陳述 / 分析師問答)
    for fn in sorted(glob.glob(os.path.join(base, "us_transcripts", "snippets_*.json")), reverse=True)[:1]:
        q = os.path.basename(fn)[len("snippets_"):-5]
        for t, lst in ((_read(fn, {}) or {}).get("items") or {}).items():
            for term, txt in lst:
                g, _, w = str(term).partition("|")
                if w.lower() in skip or per.get(t, 0) >= 2:
                    continue
                if g in groups or any(p.search(w) for p in pats):
                    per[t] = per.get(t, 0) + 1
                    out.append(["法說 " + q, t, str(txt), w])
    # 公司分散:先每家 1 段,再補第 2 段
    first = [o for i, o in enumerate(out) if [p[1] for p in out].index(o[1]) == i]
    rest = [o for o in out if o not in first]
    return (first + rest)[:MAX_SNIPPETS]


def note_inputs(theme, names, base=".", asof=None):
    res = _read(os.path.join(base, "us_tw_resonance.json"), {}) or {}
    gs = [g for g in res.get("groups") or [] if g.get("g") in theme.get("us_groups") or []]
    snips = us_snippets(theme, base, asof=asof)
    return {"題材": theme["name"], "題材關鍵詞": (theme.get("radar_terms") or [])[:12],
            "雷達命中詞": theme.get("auto", {}).get("signals", {}),
            "美股詞組(近30天財報新聞稿提到的公司數與詞)": [[g["g"], g.get("pr_n30"), g.get("pr_terms"), g.get("pr_tickers", [])[:8]] for g in gs],
            "美股財報新聞稿片段(公司陳述,未經查證)": ["%s %s [%s]:%s" % (d, t, w, s) for d, t, s, w in snips],
            "台股個股": ["%s %s(%s)" % (c, names.get(c, ""), src) for c, src in theme.get("_auto_stocks") or []][:12]}


def valid_note(o):
    if not isinstance(o, dict):
        return None
    out = {}
    for k in ("who_pays", "bottleneck", "revenue_path", "segment"):
        v = str(o.get(k) or "").strip()[:80]
        if v:
            out[k] = v
    ch = o.get("chain")
    if isinstance(ch, list):
        out["chain"] = " → ".join(str(x).strip()[:20] for x in ch[:10] if str(x).strip())
    elif isinstance(ch, str):
        out["chain"] = ch.strip()[:200]
    txt = json.dumps(out, ensure_ascii=False)
    if any(w in txt for w in FORBIDDEN) or not out.get("who_pays"):
        return None
    return out


def draft_notes(themes, names, base=".", asof=None, chain=None, max_calls=15):
    """→ {theme_id: note};快取 theme_page/ai_notes.json;沒有 AI 金鑰時只回傳快取"""
    asof = asof or dt.date.today()
    cp = os.path.join(base, AUTO_DIR, "ai_notes.json")
    cache = _read(cp, {}) or {}
    prompt = load_prompt(base)
    if chain is None:
        try:
            import llm_client
            chain = llm_client.LLMChain()
        except Exception:
            chain = None
    calls = 0
    for th in themes:
        inp = note_inputs(th, names, base, asof)
        key = {"v": NOTE_VERSION, "題材": inp["題材"], "台股個股": inp["台股個股"],
               "片段公司": sorted({x.split(" ")[1] for x in inp["美股財報新聞稿片段(公司陳述,未經查證)"]})}
        h = hashlib.sha1(json.dumps(key, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:12]
        old = cache.get(th["id"])
        fresh = old and old.get("hash") == h and (asof - dt.date.fromisoformat(old["date"])).days < AI_REFRESH_DAYS
        if fresh or not prompt or chain is None or not getattr(chain, "available", None) or calls >= max_calls:
            continue
        calls += 1
        out, provider = chain.ask_json(prompt, json.dumps(inp, ensure_ascii=False))
        note = valid_note(out)
        if note:
            cache[th["id"]] = dict(note, date=asof.isoformat(), hash=h, provider=provider)
    os.makedirs(os.path.join(base, AUTO_DIR), exist_ok=True)
    _write(cp, cache)
    return {k: v for k, v in cache.items() if k in {t["id"] for t in themes}}


# ------------------------------------------------------------------ 系統預設認錯條件
DEFAULT_KILL = [("月營收年增率連續 2 個月 < 0", "rev_yoy_neg", 2, "每月 10 日後"),
                ("毛利率連續 2 季下降", "gm_down", 2, "每季財報後")]


def auto_kill(tiers_by_code, base=".", asof=None):
    """tiers_by_code: {code: 機械分層}。第一次進入「已證明 / 開始出現」→ 建立版本 0 兩條(只新增,不修改舊列)"""
    asof = asof or dt.date.today()
    p = os.path.join(base, AUTO_DIR, "kill_auto.json")
    rows = _read(p, []) or []
    have = {str(r["code"]) for r in rows}
    for code, tier in sorted(tiers_by_code.items()):
        if tier in ("已證明", "開始出現") and code not in have:
            for i, (cond, metric, th, check) in enumerate(DEFAULT_KILL, 1):
                rows.append({"code": code, "version": 0, "idx": i, "created": asof.isoformat(), "condition": cond,
                             "metric": metric, "threshold": th, "check": check, "mode": "自動", "source": "系統預設"})
            have.add(code)
    os.makedirs(os.path.join(base, AUTO_DIR), exist_ok=True)
    _write(p, rows)
    return rows


# ------------------------------------------------------------------ 題材生命週期(描述用,不是買賣訊號)
LIFECYCLE_CFG = {
    "note": "門檻為未經驗證的描述用預設值;登記後不得看結果調整。階段只描述題材目前的狀態,不是買賣訊號",
    "early_max_days": 10,           # 雷達出現 ≤ 10 天
    "early_max_ret": 0.10,          # 且自第一次出現以來等權報酬 < +10%
    "crowd_pctile": 90,             # 融資占比歷史分位 ≥ 90 → 擁擠
    "crowd_near_high": 0.60,        # 或 ≥ 60% 個股距一年高點 < 10% 且近 20 日等權報酬 ≥ +20%
    "crowd_ret20": 0.20,
    "cool_heat_drop": 1.0,          # 訊號來源數比前 5 天平均少 ≥ 1
    "cool_above20": 0.40,           # 且站上 20 日均線的個股 < 40% → 降溫
    "pullback_above20": 0.40,       # 訊號沒有減少,但站上 20 日均線的個股 < 40% 且近 20 日等權報酬 < 0 → 回檔中
    "min_stocks": 3,                # 有價格資料的個股 < 3 檔 → 不判斷
}
STAGES = {"萌芽": "雷達剛出現不久、題材股還沒明顯上漲",
          "發酵": "訊號持續出現,題材股開始反應",
          "擁擠": "融資堆積在歷史高檔,或多數題材股貼近一年高點且近期漲幅大",
          "降溫": "訊號來源減少,且多數題材股跌破 20 日均線",
          "回檔中": "訊號仍在,但多數題材股跌破 20 日均線、近 20 日下跌",
          "資料不足": "有價格資料的個股太少,不判斷"}


def _close_on_or_after(rows, d):
    for dd, c in rows:
        if dd >= d and c:
            return c
    return None


def lifecycle(auto, px_by_code, idx_px, crowd_pctile=None, tracking_start=None, cfg=LIFECYCLE_CFG):
    """auto:select_themes 的 auto 欄位;px_by_code:{代號: [(date, close)]}(依日期);idx_px:[(date, close)]
    → 各項描述指標 + 階段(依 LIFECYCLE_CFG 固定門檻)"""
    first = auto.get("first_seen")
    rows = {c: [(d, v) for d, v in px if v] for c, px in px_by_code.items()}
    rows = {c: r for c, r in rows.items() if len(r) >= 21}
    out = {"first_seen": first, "days_on": auto.get("days_on"), "streak": auto.get("streak_days"),
           "n_px": len(rows), "crowd_pctile": crowd_pctile}
    if len(rows) < cfg["min_stocks"]:
        out["stage"] = "資料不足"
        return out
    fd = dt.date.fromisoformat(first) if first else None
    r_first, r20, above, near, gaps = [], [], [], [], []
    for c, r in rows.items():
        last = r[-1][1]
        if fd:
            b = _close_on_or_after(r, fd)
            if b:
                r_first.append(last / b - 1)
        r20.append(last / r[-21][1] - 1)
        ma20 = sum(v for _, v in r[-20:]) / 20
        above.append(last > ma20)
        hi = max(v for _, v in r[-250:])
        g = 1 - last / hi
        gaps.append(g)
        near.append(g < 0.10)
    avg = lambda xs: sum(xs) / len(xs) if xs else None
    out.update(ret_since_first=avg(r_first), ret20=avg(r20), above20=avg([1.0 if x else 0.0 for x in above]),
               near_high=avg([1.0 if x else 0.0 for x in near]), avg_gap=avg(gaps))
    ip = [(d, v) for d, v in idx_px if v]
    if len(ip) >= 21:
        out["idx_ret20"] = ip[-1][1] / ip[-21][1] - 1
        if fd:
            b = _close_on_or_after(ip, fd)
            out["idx_since_first"] = ip[-1][1] / b - 1 if b else None
    hh = [h for _, h in auto.get("heat_hist") or []]
    out["heat_now"] = auto.get("heat")
    out["heat_prev5"] = avg(hh[-6:-1]) if len(hh) >= 2 else None
    out["heat_trend"] = None if out["heat_prev5"] is None else out["heat_now"] - out["heat_prev5"]
    # 階段(依序判斷)
    crowded = (crowd_pctile is not None and crowd_pctile >= cfg["crowd_pctile"]) or \
              (out["near_high"] >= cfg["crowd_near_high"] and out["ret20"] >= cfg["crowd_ret20"])
    cooling = out["heat_trend"] is not None and out["heat_trend"] <= -cfg["cool_heat_drop"] and out["above20"] < cfg["cool_above20"]
    # 追蹤開始那天就已經出現的題材,「第一次出現」不是題材真正的起點 → 不判萌芽
    new_theme = bool(first and tracking_start and first > tracking_start)
    out["since_tracking_start"] = not new_theme
    early = new_theme and (out["days_on"] or 0) <= cfg["early_max_days"] and \
        (out["ret_since_first"] is None or out["ret_since_first"] < cfg["early_max_ret"])
    pulling = out["above20"] < cfg["pullback_above20"] and out["ret20"] < 0
    out["stage"] = "擁擠" if crowded else "降溫" if cooling else "萌芽" if early else "回檔中" if pulling else "發酵"
    out["why"] = STAGES[out["stage"]]
    for k in ("ret_since_first", "ret20", "above20", "near_high", "avg_gap", "idx_ret20", "idx_since_first", "heat_prev5", "heat_trend"):
        if isinstance(out.get(k), float):
            out[k] = round(out[k], 4)
    return out
