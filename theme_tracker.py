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
import datetime as dt
import glob

HISTORY_DIR = "theme_history"
LOOKBACK_DAYS = 14        # 往回比對幾天(判斷「首見」的視窗)
MIN_STREAK_TO_SHOW = 2    # 連續追蹤中至少要幾天才顯示(1天=剛冒出,不算追蹤)
ONLY_MULTI_SOURCE = True  # 只追蹤跨源候選(單源雜訊太多,不進歷史)


def load_today_candidates():
    """讀今天 src4 的跨源候選,回傳精簡的 {term: {sources, stocks}} 快照。"""
    try:
        data = json.load(open("new_theme_candidates.json", encoding="utf-8"))
    except FileNotFoundError:
        print("  ⚠ 找不到 new_theme_candidates.json,今日快照為空")
        return {}
    candidates = data.get("multi_source" if ONLY_MULTI_SOURCE else "candidates", [])
    if not candidates and not ONLY_MULTI_SOURCE:
        candidates = data.get("candidates", [])
    snapshot = {}
    for c in candidates:
        term = c.get("term")
        if not term:
            continue
        snapshot[term] = {
            "sources": c.get("sources", []),
            "stocks": c.get("related_stocks", []),
            "hits": c.get("recent_hits", 0),
        }
    return snapshot


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


def main():
    now = dt.datetime.now()
    today_str = now.strftime("%Y%m%d")
    print("=" * 60)
    print(f"題材追蹤器  {today_str}")
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
        print(f"  {e['term']}  {'/'.join(e['sources'])} 近{e['hits']}次{stk}")

    print(f"\n📈 連續追蹤中({len(ongoing)}個):")
    for e in ongoing[:15]:
        stk = " 股:" + " ".join(e["stocks"]) if e["stocks"] else ""
        print(f"  {e['term']}  連續{e['streak_days']}天  {'/'.join(e['sources'])}{stk}")

    # 存今天快照(供明天比對用)
    save_today_snapshot(today_snapshot, today_str)
    cleanup_old_history(LOOKBACK_DAYS + 5)

    # 輸出給前端
    output = {
        "generated_at": str(now),
        "date": today_str,
        "first_seen": first_seen,
        "ongoing": ongoing,
        "lookback_days": LOOKBACK_DAYS,
    }
    with open("theme_tracker.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n→ theme_tracker.json 已寫出(首見{len(first_seen)}/追蹤中{len(ongoing)})")


if __name__ == "__main__":
    main()
