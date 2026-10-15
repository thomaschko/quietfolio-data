# -*- coding: utf-8 -*-
"""
============================================================
watchlist_suggester.py — 每日題材 → watchlist 建議(2026-10-15 新增)
============================================================
從 theme_tracker.json(首見 + 連續追蹤中)產出「建議加入 themes_watchlist.txt」
的分級清單,寫成 watchlist_suggestions.json,daily_digest.py 讀成 I 區。

分兩部分:
  (1) 分級建議(strong/suggest/watch + paste_block):只列出,由使用者自行貼上。
  (2) 共識自動加入(2026-10-15 使用者決定):AI 語意萃取的題材片語拆成詞後,
      跟 jieba 斷詞抓到的詞「完全相同」,且近7日有 >=2 天如此,再通過排除規則與
      鉅亨基數檢查,才自動附加到 themes_watchlist.txt(每天最多3個,記
      theme_promotions.json,source="consensus")。加入滿30天都沒在偵測源1
      暴增/近期關注出現過的,列為「建議移除」(不自動刪)。
      為何不用「字串完全相同」:9/16~10/14 實測27天,AI 給「客製化晶片ASIC」這類
      每天措辭不同的片語、jieba 給「ASIC」這類單詞,兩邊完全相同的次數是0。

執行順序(event-theme.yml):theme_tracker.py → watchlist_suggester.py → daily_digest.py

步驟:
  1. 合併 ongoing + first_seen,依 term.lower() 去重,保留 streak_days 較大者
  2. 排除(記在 rejected,附原因):已在 watchlist / 與既有詞互為子字串 /
     公司名 / 雜訊(停用詞、純數字或百分比、中文少於3字)
  3. 分級:strong / suggest / watch(AI 背書判定沿用 daily_digest._method_score)
  4. strong + suggest 前10個查鉅亨,用 src1(detect_fixed_keywords)同一套公式算
     前段日均與暴增比;前段日均 < 0.15 時 src1 的 enough_base 防護會擋掉,
     加入後暫時不會觸發 → cnyes_viable=False(仍保留,加註)
  5. 英文詞若不在 daily_digest.KW_TO_THEME,加註提醒補對應
  6. 共識自動加入(見上);狀態存 watchlist_auto_state.json
============================================================
"""
import re
import json
import time
import datetime as dt

VERSION = "watchlist_suggester v2026-10-15"
TRACKER_FILE = "theme_tracker.json"
WATCHLIST_FILE = "themes_watchlist.txt"
OUT_FILE = "watchlist_suggestions.json"

MIN_OVERLAP_LEN = 3          # 互為子字串時,兩邊都至少這麼長才算重疊(避免 AI ⊂ AI伺服器 這類短詞誤判)
CNYES_MAX_CHECKS = 10        # 每天最多查幾個詞(只查 strong + suggest)
CNYES_MAX_PAGES = 5
CNYES_SLEEP = 0.3
MIN_EARLIER_DAILY = 0.15     # = event_theme_radar.detect_fixed_keywords 的 enough_base 門檻
JIEBA_SUGGEST_STREAK = 5     # 純 jieba 連續幾天才建議
TIER_ORDER = {"strong": 0, "suggest": 1, "watch": 2}

# ── 共識自動加入 ──
STATE_FILE = "watchlist_auto_state.json"
PROMOTE_LOG_FILE = "theme_promotions.json"   # 與 theme_tracker.py 自動升格共用的稽核日誌
CONSENSUS_WINDOW_DAYS = 7    # 近幾個日曆天內
CONSENSUS_MIN_DAYS = 2       # 至少幾天出現共識(含今天)
AUTO_ADD_MAX_PER_DAY = 3
STALE_DAYS = 30              # 自動加入後幾天沒在偵測源1出現 → 建議移除
STATE_KEEP_DAYS = 21
# 國際大廠反查(沿用 theme_tracker.reverse_lookup_and_promote_companies):
# 共識自動加入後的7天內每天反查一次,同一家美股公司連續3天被反查到才加進 src6 COMPANIES。
# (原本 theme_tracker 只在升格當天反查一次,連續3天的門檻永遠達不到。)
REVERSE_LOOKUP_DAYS = 7
REVERSE_LOOKUP_MAX = 5       # 每天最多反查幾個題材(Gemini 額度)
MIN_TOKEN_LEN = 3            # 拆出的詞至少3字元(擋掉 AI/PC/IC/OS 這類會假共識的短詞)
# 拆詞後太泛、不當題材的詞(實測 9/16~10/14 出現過的假共識:自動化/新能源/伺服器/企業級)
GENERIC_TOKENS = {"自動化", "新能源", "伺服器", "企業級", "供應鏈", "半導體", "半导体", "概念股",
                  "下一代", "新一代", "投資人", "生態系", "規格", "供需", "材料", "架構", "升級",
                  "漲價潮", "算力", "晶片", "產業", "應用", "技術", "市場", "需求"}
TIER_MARK = {"strong": "⭐strong", "suggest": "➕suggest", "watch": "👀watch"}

NOTE_CNYES_LOW = "cnyes基數不足，加入後src1暫時不會觸發"
NOTE_NO_KW_THEME = "G區無對應，加入後建議同步補 KW_TO_THEME"

_CJK = re.compile(r"[㐀-鿿豈-﫿]")
_NUMERIC = re.compile(r"^[+\-]?[\d,.]+\s*%?$|^[+\-]?[\d,.]+\s*(?:成|倍)$")


# ============================================================
# 讀取
# ============================================================
def load_tracker(path=TRACKER_FILE):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"  ⚠ 讀不到 {path}: {e}")
        return {}


def load_watchlist(path=WATCHLIST_FILE):
    """回傳 {lower: 原字}。忽略空行與 # 開頭行(只讀,不寫)。"""
    out = {}
    try:
        with open(path, encoding="utf-8") as f:
            for ln in f:
                w = ln.strip()
                if w and not w.startswith("#"):
                    out.setdefault(w.lower(), w)
    except FileNotFoundError:
        print(f"  ⚠ 找不到 {path},視為空清單")
    return out


def merge_terms(tracker):
    """ongoing + first_seen,依 term.lower() 去重,保留 streak_days 較大者。"""
    merged = {}
    for e in (tracker.get("ongoing") or []) + (tracker.get("first_seen") or []):
        term = (e.get("term") or "").strip()
        if not term:
            continue
        key = term.lower()
        if key not in merged or (e.get("streak_days") or 0) > (merged[key].get("streak_days") or 0):
            merged[key] = {**e, "term": term}
    return list(merged.values())


# ============================================================
# 排除與分級
# ============================================================
def is_noise(term, stopwords):
    t = term.strip()
    if t.lower() in stopwords:
        return True
    if _NUMERIC.match(t):
        return True
    # 中文少於3字:純中文(不含英數)的詞
    if _CJK.search(t) and not re.search(r"[A-Za-z0-9]", t) and len(t) < 3:
        return True
    return False


def reject_reason(term, watchlist, name2code, stopwords):
    """回傳排除原因;不排除回 None。順序:已在清單 → 重疊 → 公司名 → 雜訊。"""
    low = term.lower()
    if low in watchlist:
        return "already_in_watchlist"
    if len(term) >= MIN_OVERLAP_LEN:
        for wl_low, wl in watchlist.items():
            if len(wl) >= MIN_OVERLAP_LEN and (wl_low in low or low in wl_low):
                return f"overlaps:{wl}"
    if term in name2code or low in name2code:
        return "is_company_name"
    if is_noise(term, stopwords):
        return "noise"
    return None


def ai_backed(method, method_score):
    """含 search 或 ai 即為 AI 背書(_method_score 第一碼 0/1/2;3 = 純 jieba)。"""
    return method_score(method)[0] < 3


def classify(e, method_score):
    """回傳 (tier, note) 或 (None, rejected_reason)。"""
    m = e.get("method") or "jieba"
    streak = e.get("streak_days") or 1
    stocks = e.get("stocks") or []
    if ai_backed(m, method_score):
        if streak >= 3 or ("search" in m and "ai" in m):
            return "strong", None
        if streak == 2 and stocks:
            return "suggest", None
        if streak == 1:
            return "watch", None
        # 任務說明未涵蓋:AI 背書、連續2天但沒有對應個股 → 先放 watch,不丟掉
        return "watch", "連續2天但無對應個股,先觀察"
    if streak >= JIEBA_SUGGEST_STREAK:
        return "suggest", None
    return None, "jieba_insufficient_streak"


def is_english(term):
    return not _CJK.search(term) and re.search(r"[A-Za-z]", term) is not None


# ============================================================
# 鉅亨可用性(與 detect_fixed_keywords 同一套公式)
# ============================================================
def cnyes_stats(term, now_ts, recent_start, base_start, search):
    news = search(term, base_start, max_pages=CNYES_MAX_PAGES)
    recent = [n for n in news if n["publishAt"] >= recent_start]
    recent_daily = len(recent) / RECENT_DAYS
    earlier = [n for n in news if n["publishAt"] < recent_start]
    earlier_daily = len(earlier) / (BASELINE_DAYS - RECENT_DAYS) if earlier else 0.01
    ratio = round(recent_daily / earlier_daily, 2) if earlier_daily else 999
    return {"cnyes_recent": len(recent), "cnyes_earlier_daily": round(earlier_daily, 3),
            "cnyes_ratio": ratio, "cnyes_viable": earlier_daily >= MIN_EARLIER_DAILY,
            "_n_news": len(news)}


# ============================================================
# 共識(AI 片語拆詞 = jieba 詞)
# ============================================================
def phrase_tokens(phrase):
    """「Google TPU架構」→ [Google, TPU, 架構];「AI晶片/自研ASIC」→ [AI, 晶片, 自研, ASIC]。"""
    out = []
    for part in re.split(r"[/、與及和&+,，\s()（）]+", phrase or ""):
        out += re.findall(r"[A-Za-z][A-Za-z0-9.\-]*|[0-9.]+[A-Za-z]+[A-Za-z0-9.\-]*|[\u4e00-\u9fff]+", part)
    return out


def consensus_today(entries, method_score, stopwords):
    """回傳 {token: {"term": jieba 原字, "ai_phrases": [...], "hits", "stocks", "names"}}。"""
    jieba = {}
    for e in entries:
        if "jieba" in (e.get("method") or "jieba"):
            jieba.setdefault(e["term"].lower(), e)
    out = {}
    for e in entries:
        if not ai_backed(e.get("method"), method_score):
            continue
        for tok in phrase_tokens(e["term"]):
            low = tok.lower()
            if (len(tok) < MIN_TOKEN_LEN or low in stopwords or tok in GENERIC_TOKENS
                    or low not in jieba):
                continue
            j = jieba[low]
            c = out.setdefault(j["term"], {"term": j["term"], "ai_phrases": [], "hits": j.get("hits") or 0,
                                           "stocks": j.get("stocks") or [], "names": j.get("names") or {}})
            if e["term"] not in c["ai_phrases"]:
                c["ai_phrases"].append(e["term"])
    return out


def _d(s):
    return dt.datetime.strptime(s, "%Y%m%d").date()


def consensus_days(term, today, history):
    """近 CONSENSUS_WINDOW_DAYS 個日曆天(含今天)有幾天出現共識。"""
    t0 = _d(today)
    return sum(1 for day, terms in history.items()
               if 0 <= (t0 - _d(day)).days < CONSENSUS_WINDOW_DAYS
               and term.lower() in {x.lower() for x in terms})


def update_last_triggered(state, today, event_raw):
    """偵測源1 今天暴增或近期關注的詞 → 記錄日期(判斷自動加入的詞有沒有在用)。"""
    lt = state.setdefault("last_triggered", {})
    for t in (event_raw or {}).get("themes", []):
        if t.get("theme") and t.get("source") not in ("wiki", "MOPS"):
            lt[t["theme"].lower()] = today
    for t in (event_raw or {}).get("near_miss_themes", []):
        if t.get("theme"):
            lt[t["theme"].lower()] = today


def stale_auto_added(state, today, promotions):
    lt = state.get("last_triggered", {})
    out = []
    for p in promotions:
        if p.get("source") != "consensus":
            continue
        age = (_d(today) - _d(p["promoted_date"])).days
        last = lt.get(p["term"].lower())
        if age >= STALE_DAYS and (last is None or (_d(today) - _d(last)).days >= STALE_DAYS):
            out.append({"term": p["term"], "promoted_date": p["promoted_date"], "last_triggered": last})
    return out


# ============================================================
# 主流程
# ============================================================
def build(tracker, watchlist, name2code, stopwords, method_score, kw_to_theme,
          now_ts=None, window=None, search=None, state=None, promotions=None, event_raw=None):
    """window = (recent_start_ts, base_start_ts);search=None 時不查鉅亨(測試用)。
    state 會被就地更新(共識歷史、偵測源1觸發日);回傳的 auto_added 由 main() 寫入 watchlist。"""
    now_ts = now_ts or int(time.time())
    kw_lower = {k.lower() for k in kw_to_theme}
    suggestions, rejected = [], []
    state = state if state is not None else {}
    today = tracker.get("date") or dt.date.today().strftime("%Y%m%d")
    entries = merge_terms(tracker)

    # ── 共識:今天的拆詞共識寫進歷史,再算近7日天數 ──
    cons = consensus_today(entries, method_score, stopwords)
    hist = state.setdefault("consensus_history", {})
    hist[today] = sorted(cons)
    for day in [d for d in hist if (_d(today) - _d(d)).days >= STATE_KEEP_DAYS]:
        del hist[day]
    consensus = []
    for term, c in cons.items():
        c = {**c, "consensus_days": consensus_days(term, today, hist),
             "cnyes_recent": None, "cnyes_earlier_daily": None, "cnyes_ratio": None,
             "cnyes_viable": None, "status": None}
        why = reject_reason(term, watchlist, name2code, stopwords)
        if why:
            c["status"] = f"rejected:{why}"
        elif c["consensus_days"] < CONSENSUS_MIN_DAYS:
            c["status"] = f"pending:共識{c['consensus_days']}天(需{CONSENSUS_MIN_DAYS}天)"
        consensus.append(c)
    consensus.sort(key=lambda c: (-c["consensus_days"], -c["hits"]))

    for e in entries:
        term = e["term"]
        why = reject_reason(term, watchlist, name2code, stopwords)
        if why:
            rejected.append({"term": term, "reason": why})
            continue
        tier, note = classify(e, method_score)
        if tier is None:
            rejected.append({"term": term, "reason": note})
            continue
        s = {
            "term": term, "tier": tier, "method": e.get("method") or "jieba",
            "streak_days": e.get("streak_days") or 1, "hits": e.get("hits") or 0,
            "reason": e.get("reason") or "", "stocks": e.get("stocks") or [],
            "names": e.get("names") or {},
            "cnyes_recent": None, "cnyes_earlier_daily": None, "cnyes_ratio": None,
            "cnyes_viable": None, "notes": [note] if note else [],
        }
        if "confidence" in e:
            s["confidence"] = e["confidence"]
        if is_english(term) and term.lower() not in kw_lower:
            s["notes"].append(NOTE_NO_KW_THEME)
        suggestions.append(s)

    # 鉅亨檢查:共識候選優先,再來 strong + suggest(依 tier → 連續天數 → 次數),合計前 N 個
    cons_check = [c for c in consensus if c["status"] is None]
    to_check = cons_check + sorted([s for s in suggestions if s["tier"] in ("strong", "suggest")],
                                   key=lambda s: (TIER_ORDER[s["tier"]], -s["streak_days"], -s["hits"]))
    for s in to_check:
        s.setdefault("notes", [])
    checked = empty = 0
    for i, s in enumerate(to_check):
        if search is None or window is None:
            s["notes"].append("未檢查cnyes(無法取得鉅亨資料)")
            continue
        if i >= CNYES_MAX_CHECKS:
            s["notes"].append(f"未檢查cnyes(超過每日{CNYES_MAX_CHECKS}個上限)")
            continue
        st = cnyes_stats(s["term"], now_ts, window[0], window[1], search)
        checked += 1
        empty += st.pop("_n_news") == 0
        s.update(st)
        if not s["cnyes_viable"]:
            s["notes"].append(NOTE_CNYES_LOW)
        time.sleep(CNYES_SLEEP)

    # 全部查詢都 0 則時多半是鉅亨連不上,不是真的沒新聞 → 改標「未知」,不誤判為基數不足
    all_empty = checked > 0 and empty == checked
    if all_empty:
        for s in to_check:
            if s["cnyes_viable"] is False:
                s["cnyes_viable"] = None
                s["notes"] = [n for n in s["notes"] if n != NOTE_CNYES_LOW] + ["cnyes查詢全部0則(疑似連線失敗),可用性未知"]

    # ── 共識自動加入:鉅亨基數夠才加;鉅亨疑似連不上的那天一律不加 ──
    auto_added = []
    for c in cons_check:
        if c["cnyes_viable"] is True and len(auto_added) < AUTO_ADD_MAX_PER_DAY:
            c["status"] = "auto_added"
            auto_added.append({"term": c["term"], "promoted_date": today, "source": "consensus",
                               "consensus_days": c["consensus_days"], "ai_phrases": c["ai_phrases"],
                               "cnyes_earlier_daily": c["cnyes_earlier_daily"], "cnyes_ratio": c["cnyes_ratio"],
                               "stocks": c["stocks"], "names": c["names"]})
        elif c["cnyes_viable"] is True:
            c["status"] = f"pending:超過每日{AUTO_ADD_MAX_PER_DAY}個上限"
        elif c["cnyes_viable"] is False:
            c["status"] = "rejected:cnyes基數不足"
        else:
            c["status"] = "pending:cnyes未查或可用性未知"
    added_low = {a["term"].lower() for a in auto_added}
    suggestions = [s for s in suggestions if s["term"].lower() not in added_low]

    update_last_triggered(state, today, event_raw)
    stale = stale_auto_added(state, today, list(promotions or []) + auto_added)

    viable_rank = {True: 0, None: 1, False: 2}
    suggestions.sort(key=lambda s: (TIER_ORDER[s["tier"]], viable_rank[s["cnyes_viable"]],
                                    -s["streak_days"], -s["hits"]))
    paste = [s["term"] for s in suggestions
             if s["tier"] in ("strong", "suggest") and s["cnyes_viable"] is True]
    return {
        "generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "version": VERSION,
        "tracker_date": tracker.get("date"),
        "counts": {t: sum(1 for s in suggestions if s["tier"] == t) for t in TIER_ORDER},
        "cnyes_checked": checked,
        "cnyes_all_empty": all_empty,
        "suggestions": suggestions,
        "rejected": rejected,
        "paste_block": "\n".join(paste),
        "consensus": consensus,
        "auto_added": auto_added,
        "stale_auto_added": stale,
    }


def recent_consensus_themes(promotions, today):
    """近 REVERSE_LOOKUP_DAYS 天內共識自動加入的詞(新的在前),給國際大廠反查用。"""
    rec = [p for p in promotions if p.get("source") == "consensus"
           and 0 <= (_d(today) - _d(p["promoted_date"])).days < REVERSE_LOOKUP_DAYS]
    rec.sort(key=lambda p: p["promoted_date"], reverse=True)
    return [{"term": p["term"], "reason": "、".join(p.get("ai_phrases", []))} for p in rec[:REVERSE_LOOKUP_MAX]]


def reverse_lookup_recent(promotions, today):
    themes = recent_consensus_themes(promotions, today)
    if not themes:
        return []
    try:
        from theme_tracker import reverse_lookup_and_promote_companies
        return reverse_lookup_and_promote_companies(themes, today)
    except Exception as e:
        print(f"  ⚠ 國際大廠反查略過: {e}")
        return []


def append_to_watchlist(terms, today, path=WATCHLIST_FILE):
    """附加到 watchlist 末端(不改動既有內容)。"""
    if not terms:
        return
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"\n# ── 共識自動加入 {today}(AI片語拆詞=jieba詞,近{CONSENSUS_WINDOW_DAYS}日>="
                f"{CONSENSUS_MIN_DAYS}天;watchlist_suggester.py,見theme_promotions.json)──\n")
        for t in terms:
            f.write(f"{t}\n")


def _load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def main():
    print(VERSION)
    from daily_digest import KW_TO_THEME, _method_score
    try:
        from new_theme_discovery import STOPWORDS
    except Exception as e:
        print(f"  ⚠ 停用詞載入失敗(雜訊過濾只剩數字/短中文規則): {e}")
        STOPWORDS = set()
    stopwords = {w.lower() for w in STOPWORDS}

    tracker = load_tracker()
    watchlist = load_watchlist()
    print(f"  theme_tracker {tracker.get('date')}: 追蹤中 {len(tracker.get('ongoing') or [])}"
          f"、首見 {len(tracker.get('first_seen') or [])};watchlist {len(watchlist)} 詞")

    window = search = None
    name2code = {}
    try:
        import event_theme_radar as etr
        name2code, _ = etr.build_name2code()
        now_ts = int(time.time())
        recent_start, base_start, _win = etr.window_bounds(now_ts, etr.load_tw_holidays())
        window, search = (recent_start, base_start), etr.cnyes_search
        global RECENT_DAYS, BASELINE_DAYS
        RECENT_DAYS, BASELINE_DAYS = etr.RECENT_DAYS, etr.BASELINE_DAYS
    except Exception as e:
        print(f"  ⚠ event_theme_radar 載入失敗(不查公司名/鉅亨): {e}")
        now_ts = int(time.time())
    if not name2code:
        print("  ⚠ 股名對照為空,本次無法排除公司名")

    state = _load(STATE_FILE, {})
    promotions = _load(PROMOTE_LOG_FILE, [])
    event_raw = _load("event_theme_raw.json", {})
    out = build(tracker, watchlist, name2code, stopwords, _method_score, KW_TO_THEME,
                now_ts=now_ts, window=window, search=search,
                state=state, promotions=promotions, event_raw=event_raw)
    today = tracker.get("date") or dt.date.today().strftime("%Y%m%d")
    if out["auto_added"]:
        append_to_watchlist([a["term"] for a in out["auto_added"]], today)
        with open(PROMOTE_LOG_FILE, "w", encoding="utf-8") as f:
            json.dump(promotions + out["auto_added"], f, ensure_ascii=False, indent=2)
    out["companies_promoted"] = reverse_lookup_recent(promotions + out["auto_added"], today)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    c = out["counts"]
    print(f"  建議 {len(out['suggestions'])}(strong {c['strong']} / suggest {c['suggest']} / "
          f"watch {c['watch']}),排除 {len(out['rejected'])},查鉅亨 {out['cnyes_checked']} 個")
    if out["cnyes_all_empty"]:
        print("  ⚠ 查詢的詞鉅亨全部 0 則,可能是連線問題,cnyes_viable 已標為未知")
    for s in out["suggestions"]:
        if s["tier"] != "watch":
            print(f"    {TIER_MARK[s['tier']]} {s['term']} 連{s['streak_days']}天 "
                  f"暴增比{s['cnyes_ratio']} 前段日均{s['cnyes_earlier_daily']} {'、'.join(s['notes'])}")
    print(f"  共識候選 {len(out['consensus'])}:")
    for c in out["consensus"]:
        print(f"    {c['term']} 共識{c['consensus_days']}天 ← {'、'.join(c['ai_phrases'])}  {c['status']}")
    if out.get("companies_promoted"):
        print(f"  🌐 反查新增 src6:{'、'.join(c['ticker'] for c in out['companies_promoted'])}")
    if out["auto_added"]:
        print(f"  ✅ 自動加入 themes_watchlist.txt:{'、'.join(a['term'] for a in out['auto_added'])}")
    for x in out["stale_auto_added"]:
        print(f"  🗑 建議移除 {x['term']}(加入於 {x['promoted_date']},{STALE_DAYS}天未在偵測源1出現)")
    print(f"→ {OUT_FILE}、{STATE_FILE} 已寫出")


RECENT_DAYS, BASELINE_DAYS = 3, 20   # 由 main() 改用 event_theme_radar 的值


if __name__ == "__main__":
    main()
