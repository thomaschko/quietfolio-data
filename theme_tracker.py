# -*- coding: utf-8 -*-
"""
============================================================
theme_tracker.py — 題材追蹤器(新題材 vs 連續追蹤中)
============================================================
獨立於 watchlist 之外,追蹤 src4(跨源交叉候選)的每日題材,
分辨「今日首見」與「連續追蹤中N天」,解決「不受限於watchlist」的需求。

運作原理:
  1. 讀 new_theme_candidates.json(src4今日輸出的跨源候選)
  2. 存一份精簡快照到 theme_history/YYYYMMDD.json(累積歷史,存在repo)
  3. 往回讀最近 LOOKBACK_DAYS 天的快照,比對:
     - 今日有、過去N天都沒有 → 今日首見
     - 連續2天以上出現(含今天) → 連續追蹤中,附上連續天數
  4. 輸出 theme_tracker.json 供前端「題材追蹤」分頁讀取

用法:接在 new_theme_discovery.py 之後跑(它產出 new_theme_candidates.json)
版權:只存題材詞本身(2-8字的詞),不存新聞全文。
============================================================
"""
import json
import os
import re
import datetime as dt
import glob
import requests

HISTORY_DIR = "theme_history"
LOOKBACK_DAYS = 14        # 往回比對幾天(判斷「首見」的視窗)
MIN_STREAK_TO_SHOW = 2    # 連續追蹤中至少要幾天才顯示(1天=剛冒出,不算追蹤)
ONLY_MULTI_SOURCE = True  # 只追蹤跨源候選(單源雜訊太多,不進歷史)

# ── 自動升格進 themes_watchlist.txt 的門檻(2026-09-10新增)──
# 用「連續追蹤天數」當自動化守門員,比單日AI信心度更可靠(排除曇花一現的誤判)
WATCHLIST_FILE_PATH = "themes_watchlist.txt"
PROMOTE_LOG_FILE = "theme_promotions.json"  # 稽核日誌:記錄每次自動新增,方便事後檢視/撤回
STREAK_HIGH_CONFIDENCE = 3   # AI判high信心,連續3天才自動寫入
STREAK_MEDIUM_CONFIDENCE = 5 # AI判medium信心,連續5天才自動寫入(更保守)
STREAK_JIEBA_ONLY = 999      # 純jieba版(無AI背書)不自動升格,設極大值等於關閉
MAX_PROMOTIONS_PER_RUN = 5   # 單次最多自動新增幾個題材,避免異常大量湧入

# ── 國際大廠反查(2026-09-10新增)──
# 只對「已自動升格進watchlist」的題材做反查,用同一套連續天數邏輯驗證公司候選
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = "gemini-3.5-flash-lite"
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
COMPANY_HISTORY_DIR = "company_history"
COMPANIES_FILE = "earnings_keywords.py"  # src6公司清單所在檔案
COMPANY_STREAK_THRESHOLD = 3  # 反查公司連續出現3天才自動加進src6(同一題材反覆確認同一家公司)
MAX_COMPANY_PROMOTIONS_PER_RUN = 3


# 追蹤層停用詞(補src4沒擋乾淨的通用詞/符號殘留)
TRACKER_STOPWORDS = {
    "...", "…", "新台幣", "發表會", "下半年", "上半年", "今年", "明年",
    "本季", "上季", "下季", "今日", "昨日", "明日", "本週", "上週", "下週",
}


def is_valid_term(term):
    """過濾純符號、過短、明顯通用詞。"""
    t = term.strip()
    if not t or t in TRACKER_STOPWORDS:
        return False
    if all(c in '.,。，…—-·、' for c in t):  # 純符號
        return False
    if len(t) < 2:
        return False
    return True


def _load_one_file(path, key_field="related_stocks"):
    """讀單一候選檔(AI版或jieba版),回傳 {term: {sources, stocks, hits, method}}。"""
    try:
        data = json.load(open(path, encoding="utf-8"))
    except FileNotFoundError:
        return {}
    candidates = data.get("multi_source" if ONLY_MULTI_SOURCE else "candidates", [])
    if not candidates and not ONLY_MULTI_SOURCE:
        candidates = data.get("candidates", [])
    snapshot = {}
    for c in candidates:
        term = c.get("term")
        if not term or not is_valid_term(term):
            continue
        snapshot[term] = {
            "sources": c.get("sources", []),
            "stocks": c.get(key_field, []),
            "hits": c.get("recent_hits", 0),
            "reason": c.get("reason", ""),
            "confidence": c.get("confidence", ""),  # AI版才有;jieba版為空字串
        }
    return snapshot


def load_today_candidates():
    """整合AI語意版(優先,較準)+jieba跨源版(補充),回傳今日題材快照。
    同一詞若兩邊都有,合併來源清單、取較高的hits、保留AI的reason說明。"""
    ai_snap = _load_one_file("ai_theme_candidates.json", key_field="stocks")
    jieba_snap = _load_one_file("new_theme_candidates.json", key_field="related_stocks")
    print(f"  AI語意版:{len(ai_snap)}個題材  jieba跨源版:{len(jieba_snap)}個題材")

    merged = {}
    for term, info in ai_snap.items():
        merged[term] = dict(info)
        merged[term]["method"] = "ai"
    for term, info in jieba_snap.items():
        if term in merged:
            # 兩邊都有:合併來源、取較高hits,保留原本(AI)的reason與confidence
            merged[term]["sources"] = sorted(set(merged[term]["sources"]) | set(info["sources"]))
            merged[term]["hits"] = max(merged[term]["hits"], info["hits"])
            merged[term]["method"] = "both"
        else:
            merged[term] = dict(info)
            merged[term]["method"] = "jieba"
    return merged


def save_today_snapshot(snapshot, date_str):
    """存今天的快照到 theme_history/YYYYMMDD.json(只存詞,精簡)。"""
    os.makedirs(HISTORY_DIR, exist_ok=True)
    path = os.path.join(HISTORY_DIR, f"{date_str}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"date": date_str, "terms": list(snapshot.keys())}, f, ensure_ascii=False)
    print(f"  今日快照已存: {path}({len(snapshot)}個詞)")


def load_history(lookback_days, today_str):
    """讀最近N天的歷史快照(不含今天)。回傳 {date_str: set(terms)},按日期新到舊排。"""
    history = {}
    today = dt.datetime.strptime(today_str, "%Y%m%d").date()
    for i in range(1, lookback_days + 1):
        d = (today - dt.timedelta(days=i)).strftime("%Y%m%d")
        path = os.path.join(HISTORY_DIR, f"{d}.json")
        if os.path.exists(path):
            try:
                data = json.load(open(path, encoding="utf-8"))
                history[d] = set(data.get("terms", []))
            except Exception:
                continue
    return history


def cleanup_old_history(keep_days):
    """清掉超過保留期的舊快照(避免repo無限膨脹)。"""
    files = sorted(glob.glob(os.path.join(HISTORY_DIR, "*.json")))
    if len(files) <= keep_days:
        return
    for f in files[:-keep_days]:
        try:
            os.remove(f)
        except Exception:
            pass


def compute_streak(term, today_str, history_dates_sorted):
    """算某個詞從今天往回連續出現幾天(含今天)。"""
    streak = 1  # 今天算1天
    today = dt.datetime.strptime(today_str, "%Y%m%d").date()
    for i in range(1, LOOKBACK_DAYS + 1):
        d_str = (today - dt.timedelta(days=i)).strftime("%Y%m%d")
        if d_str not in history_dates_sorted:
            break  # 那天沒資料,視為斷了(保守處理)
        if term in history_dates_sorted[d_str]:
            streak += 1
        else:
            break  # 中斷,連續天數到此為止
    return streak


def load_watchlist_terms_raw():
    """讀現有watchlist的所有詞(小寫set,判斷是否已存在,避免重複新增)。"""
    terms = set()
    try:
        with open(WATCHLIST_FILE_PATH, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if ln and not ln.startswith("#"):
                    terms.add(ln.lower())
    except FileNotFoundError:
        pass
    return terms


def auto_promote_to_watchlist(ongoing, today_str):
    """
    自動升格:連續追蹤天數達門檻的AI背書題材,自動寫進 themes_watchlist.txt。
    用「時間持續性」當守門員,而非單日AI信心度——曇花一現的誤判撐不過連續N天。
    純jieba版(無AI驗證過)不自動升格,只有 method 為 'ai' 或 'both' 才考慮。
    每次新增都記錄稽核日誌(theme_promotions.json),方便事後檢視或手動撤回。
    """
    existing = load_watchlist_terms_raw()
    promoted = []
    for e in ongoing:
        if len(promoted) >= MAX_PROMOTIONS_PER_RUN:
            break
        term = e["term"]
        if term.lower() in existing:
            continue  # 已存在,跳過
        method = e.get("method", "jieba")
        confidence = e.get("confidence", "")
        streak = e.get("streak_days", 0)

        if method == "jieba":
            threshold = STREAK_JIEBA_ONLY  # 無AI背書,實質不升格
        elif confidence == "high":
            threshold = STREAK_HIGH_CONFIDENCE
        else:  # medium 或空
            threshold = STREAK_MEDIUM_CONFIDENCE

        if streak >= threshold:
            promoted.append({
                "term": term, "streak_days": streak, "method": method,
                "confidence": confidence, "reason": e.get("reason", ""),
                "promoted_date": today_str,
            })
            existing.add(term.lower())  # 避免同批次重複

    if not promoted:
        return []

    # 寫進 watchlist(附加,不覆蓋既有內容)
    with open(WATCHLIST_FILE_PATH, "a", encoding="utf-8") as f:
        f.write(f"\n# ── AI自動升格題材 {today_str}(連續追蹤驗證通過,見theme_promotions.json)──\n")
        for p in promoted:
            f.write(f"{p['term']}\n")

    # 寫稽核日誌(累積,方便追溯每次自動新增的原因)
    log = []
    try:
        log = json.load(open(PROMOTE_LOG_FILE, encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    log.extend(promoted)
    json.dump(log, open(PROMOTE_LOG_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    return promoted


COMPANY_LOOKUP_PROMPT = """你是國際科技產業研究助理。針對給定的產業技術題材,
找出「美股上市」且與該題材直接相關的主要供應鏈公司(不限龍頭,也可包含中小型專業廠商)。

只回傳美股上市公司(有美股代號的),不要台股/陸股/其他市場公司。
用純JSON格式回傳(不要有其他文字):
{"companies":[{"ticker":"股票代號","name":"公司名稱","role":"在此題材扮演角色(10字內)"}]}

如果找不到明確相關的美股公司,回傳 {"companies":[]}。最多列5家最相關的。"""


def lookup_companies_for_theme(theme_term, reason=""):
    """對一個題材問Gemini:國際上有哪些相關美股公司。回傳 [{ticker,name,role}] 或 []。"""
    if not GEMINI_KEY:
        return []
    prompt = f"題材:{theme_term}\n說明:{reason}" if reason else f"題材:{theme_term}"
    body = {
        "system_instruction": {"parts": [{"text": COMPANY_LOOKUP_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"response_mime_type": "application/json", "temperature": 0.1},
    }
    try:
        r = requests.post(
            GEMINI_URL,
            headers={"Content-Type": "application/json", "x-goog-api-key": GEMINI_KEY},
            json=body, timeout=30,
        )
        if r.status_code != 200:
            print(f"    ⚠ 反查「{theme_term}」失敗: HTTP {r.status_code}")
            return []
        data = r.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        result = json.loads(text)
        return result.get("companies", [])
    except Exception as e:
        print(f"    ⚠ 反查「{theme_term}」錯誤: {e}")
        return []


def load_existing_companies():
    """讀 earnings_keywords.py 裡現有的 COMPANIES 清單(股票代號set)。"""
    tickers = set()
    try:
        content = open(COMPANIES_FILE, encoding="utf-8").read()
        for m in re.finditer(r'"([A-Z]{1,5})":\s*"[^"]*"', content):
            tickers.add(m.group(1))
    except FileNotFoundError:
        pass
    return tickers


def save_company_snapshot(candidates, date_str):
    """存今天反查到的公司候選快照(供連續天數比對)。"""
    os.makedirs(COMPANY_HISTORY_DIR, exist_ok=True)
    path = os.path.join(COMPANY_HISTORY_DIR, f"{date_str}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"date": date_str, "candidates": candidates}, f, ensure_ascii=False)


def load_company_history(lookback_days, today_str):
    """讀最近N天的公司候選快照,回傳 {date: {ticker: info}}。"""
    history = {}
    today = dt.datetime.strptime(today_str, "%Y%m%d").date()
    for i in range(1, lookback_days + 1):
        d = (today - dt.timedelta(days=i)).strftime("%Y%m%d")
        path = os.path.join(COMPANY_HISTORY_DIR, f"{d}.json")
        if os.path.exists(path):
            try:
                data = json.load(open(path, encoding="utf-8"))
                history[d] = {c["ticker"]: c for c in data.get("candidates", [])}
            except Exception:
                continue
    return history


def reverse_lookup_and_promote_companies(promoted_themes, today_str):
    """
    對「今天自動升格的題材」逐一反查Gemini,問國際相關美股公司。
    候選公司連續出現達門檻天數才自動加進 earnings_keywords.py 的 COMPANIES。
    """
    if not promoted_themes:
        return []

    existing_tickers = load_existing_companies()
    today_candidates = []  # 今天反查到的所有候選(供存快照)

    print(f"\n🔍 對 {len(promoted_themes)} 個新升格題材做國際大廠反查:")
    for theme in promoted_themes:
        companies = lookup_companies_for_theme(theme["term"], theme.get("reason", ""))
        for c in companies:
            ticker = c.get("ticker", "").strip().upper()
            if not ticker or not re.match(r'^[A-Z]{1,5}$', ticker):
                continue  # 格式不對,跳過
            if ticker in existing_tickers:
                continue  # 已在src6,不用重複反查
            today_candidates.append({
                "ticker": ticker, "name": c.get("name", ""),
                "role": c.get("role", ""), "source_theme": theme["term"],
            })
        if companies:
            names = [f"{c.get('ticker','?')}" for c in companies]
            print(f"  {theme['term']}: {' '.join(names)}")

    save_company_snapshot(today_candidates, today_str)

    # 比對歷史,算連續出現天數
    history = load_company_history(COMPANY_STREAK_THRESHOLD + 5, today_str)
    promoted_companies = []
    seen_today = {c["ticker"] for c in today_candidates}
    for ticker in seen_today:
        streak = 1
        today = dt.datetime.strptime(today_str, "%Y%m%d").date()
        for i in range(1, COMPANY_STREAK_THRESHOLD + 5):
            d_str = (today - dt.timedelta(days=i)).strftime("%Y%m%d")
            if d_str not in history:
                break
            if ticker in history[d_str]:
                streak += 1
            else:
                break
        if streak >= COMPANY_STREAK_THRESHOLD:
            info = next(c for c in today_candidates if c["ticker"] == ticker)
            promoted_companies.append({**info, "streak_days": streak})

    promoted_companies = promoted_companies[:MAX_COMPANY_PROMOTIONS_PER_RUN]

    if promoted_companies:
        # 寫進 earnings_keywords.py 的 COMPANIES dict(找到 "}" 前插入)
        content = open(COMPANIES_FILE, encoding="utf-8").read()
        insert_lines = "".join(
            '    "%s": "%s(AI反查-%s)",\n' % (
                c["ticker"], c["role"] or c["name"], c["source_theme"]
            )
            for c in promoted_companies
        )
        marker = "COMPANIES = {"
        idx = content.find(marker)
        if idx != -1:
            # 找到COMPANIES字典開頭後,插在第一行後面(維持字典語法正確)
            insert_pos = content.find("\n", idx) + 1
            content = content[:insert_pos] + insert_lines + content[insert_pos:]
            with open(COMPANIES_FILE, "w", encoding="utf-8") as f:
                f.write(content)
            print(f"\n✅ 自動新增進 src6 COMPANIES({len(promoted_companies)}家):")
            for c in promoted_companies:
                print(f"  {c['ticker']} {c['name']}  連續{c['streak_days']}天  源自題材:{c['source_theme']}")

    return promoted_companies


def main():
    now = dt.datetime.now()
    today_str = now.strftime("%Y%m%d")
    print("=" * 60)
    print(f"題材追蹤器 v2025-09-10-filterfix  {today_str}")
    print("=" * 60)

    today_snapshot = load_today_candidates()
    print(f"今日跨源候選:{len(today_snapshot)}個")

    history = load_history(LOOKBACK_DAYS, today_str)
    print(f"歷史快照:{len(history)}天")

    all_history_terms = set()
    for terms in history.values():
        all_history_terms |= terms

    first_seen = []      # 今日首見(過去N天都沒有)
    ongoing = []          # 連續追蹤中(含今天連續>=MIN_STREAK_TO_SHOW天)

    for term, info in today_snapshot.items():
        streak = compute_streak(term, today_str, history)
        entry = {
            "term": term,
            "sources": info["sources"],
            "stocks": info["stocks"],
            "hits": info["hits"],
            "streak_days": streak,
            "method": info.get("method", "jieba"),
            "reason": info.get("reason", ""),
        }
        if term not in all_history_terms:
            first_seen.append(entry)
        elif streak >= MIN_STREAK_TO_SHOW:
            ongoing.append(entry)

    ongoing.sort(key=lambda x: -x["streak_days"])
    first_seen.sort(key=lambda x: -x["hits"])

    print(f"\n🆕 今日首見({len(first_seen)}個):")
    for e in first_seen[:15]:
        stk = " 股:" + " ".join(e["stocks"]) if e["stocks"] else ""
        m = {"ai":"🤖","jieba":"🔤","both":"🤖🔤"}.get(e["method"],"")
        rs = f" ({e['reason']})" if e.get("reason") else ""
        print(f"  {m}{e['term']}  {'/'.join(e['sources'])} 近{e['hits']}次{stk}{rs}")

    print(f"\n📈 連續追蹤中({len(ongoing)}個):")
    for e in ongoing[:15]:
        stk = " 股:" + " ".join(e["stocks"]) if e["stocks"] else ""
        m = {"ai":"🤖","jieba":"🔤","both":"🤖🔤"}.get(e["method"],"")
        print(f"  {m}{e['term']}  連續{e['streak_days']}天  {'/'.join(e['sources'])}{stk}")

    # 存今天快照(供明天比對用)
    save_today_snapshot(today_snapshot, today_str)
    cleanup_old_history(LOOKBACK_DAYS + 5)

    # 自動升格:連續追蹤達門檻的AI背書題材,寫進themes_watchlist.txt
    promoted = auto_promote_to_watchlist(ongoing, today_str)
    if promoted:
        print(f"\n✅ 自動升格進 themes_watchlist.txt({len(promoted)}個):")
        for p in promoted:
            print(f"  {p['term']}  連續{p['streak_days']}天  信心度{p['confidence'] or 'jieba'}")
    else:
        print(f"\n（今日無題材達自動升格門檻:high信心需連續{STREAK_HIGH_CONFIDENCE}天,"
              f"medium需連續{STREAK_MEDIUM_CONFIDENCE}天）")

    # 對「今天新升格的題材」反查國際大廠,連續出現達門檻才自動加進src6
    promoted_companies = reverse_lookup_and_promote_companies(promoted, today_str)

    # 輸出給前端
    output = {
        "generated_at": str(now),
        "date": today_str,
        "first_seen": first_seen,
        "ongoing": ongoing,
        "lookback_days": LOOKBACK_DAYS,
        "promoted_today": promoted,
        "promoted_companies_today": promoted_companies,
    }
    with open("theme_tracker.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n→ theme_tracker.json 已寫出(首見{len(first_seen)}/追蹤中{len(ongoing)})")


if __name__ == "__main__":
    main()
