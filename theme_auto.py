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
    for c in chosen:
        days = sorted(st.get(c["id"], {}).get("days") or [])
        streak, d0 = 0, dt.date.fromisoformat(day)
        while d0.isoformat() in days or (d0.weekday() >= 5 and streak):
            if d0.isoformat() in days:
                streak += 1
            d0 -= dt.timedelta(days=1)
        c["auto"]["streak_days"] = streak
        c["auto"]["first_seen"] = st.get(c["id"], {}).get("first")
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


NOTE_VERSION = "v2"           # 2026-11-09:改帶更多美股新聞稿片段 → 版本變了會重寫一次草稿
MAX_SNIPPETS = 8


def us_snippets(theme, base=".", days=30, asof=None):
    """近 30 天財報新聞稿(us_pr 單日檔)中,詞屬於題材的美股詞組、或詞 / 片段含題材別名的片段;每家公司最多 2 段,最多 8 段"""
    import glob
    asof = asof or dt.date.today()
    groups = set(theme.get("us_groups") or [])
    import re
    pats = [re.compile(r"(?<![A-Za-z0-9])" + re.escape(a) + r"(?![A-Za-z0-9])", re.I)
            for a in theme.get("radar_terms") or [] if len(a) >= 3]       # 完整單字才算(避免 OCS 配到 focus)
    cutoff = (asof - dt.timedelta(days=days)).isoformat()
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
                if g in groups or any(p.search(w) or p.search(txt) for p in pats):
                    if per.get(x["ticker"], 0) >= 2 or any(txt == o[2] for o in out):
                        continue
                    per[x["ticker"]] = per.get(x["ticker"], 0) + 1
                    out.append([date, x["ticker"], txt, w])
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
