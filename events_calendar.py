# -*- coding: utf-8 -*-
"""
============================================================
events_calendar.py — 大會日曆(2026-10-16 新增,v1 僅顯示)
============================================================
讀手動維護的 events_calendar.json,回答「今天前後兩週有哪些半導體 / AI 基礎設施大會」。
大會前後兩週對應題材的新聞密度會自然升高,這是可預期的事件效應,不是新題材、也不是
買進理由;daily_digest 只用來加註背景與 A 區標記,不改變任何偵測門檻、A 區納入與排序。

純函式、無網路。
  load(path)                          → {"events": [...], "archive": [...]};檔案不存在/壞掉回空結構
  window(today, before=14, after=14)  → 與今天相關的事件(加 phase/days_to_start/days_since_end)
  tag_for_theme(keywords, today)      → "📅OFC前5日" / "📅OFC期間" / "📅OFC後3日" 或 None
  status_mark(event)                  → "⚠暫定" / "⚠未驗證" / ""
CLI:python events_calendar.py --validate
============================================================
"""
import json
import re
import sys
import datetime as dt

CALENDAR_FILE = "events_calendar.json"
WATCHLIST_FILE = "themes_watchlist.txt"
DATE_STATUS = ("confirmed", "tentative", "unverified")
KINDS = ("standard", "academic", "expo", "vendor")
RELEVANCE = ("high", "med", "low")
STATUS_MARK = {"tentative": "⚠暫定", "unverified": "⚠未驗證"}
PHASE_ORDER = {"during": 0, "before": 1, "after": 2}
REQUIRED = ("id", "name", "short", "start", "end", "location", "kind", "themes",
            "tw_relevance", "date_status", "source", "post_event_urls", "note")


def load(path=CALENDAR_FILE):
    """讀日曆。檔案不存在或格式錯誤時印一行警告,回傳空結構,不丟例外。"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("最外層不是物件")
        data.setdefault("events", [])
        data.setdefault("archive", [])
        return data
    except Exception as e:
        print(f"  ⚠ 大會日曆讀取失敗({path}),略過: {e}")
        return {"events": [], "archive": []}


def _d(s):
    return dt.date.fromisoformat(s)


def window(today, before=14, after=14, events=None):
    """回傳與 today 相關的事件(copy),每筆加上 phase / days_to_start / days_since_end。
    phase:before = 0 < start-today <= before;during = start <= today <= end;
           after = 0 < today-end <= after;其餘不回傳。
    排序:during → before(近的在前)→ after(近的在前)。日期壞掉的事件略過。"""
    if events is None:
        events = load().get("events", [])
    out = []
    for e in events:
        try:
            start, end = _d(e["start"]), _d(e["end"])
        except Exception:
            continue
        to_start = (start - today).days
        since_end = (today - end).days
        if 0 < to_start <= before:
            phase = "before"
        elif start <= today <= end:
            phase = "during"
        elif 0 < since_end <= after:
            phase = "after"
        else:
            continue
        out.append({**e, "phase": phase, "days_to_start": to_start, "days_since_end": since_end})
    out.sort(key=lambda e: (PHASE_ORDER[e["phase"]],
                            e["days_to_start"] if e["phase"] == "before" else
                            e["days_since_end"] if e["phase"] == "after" else 0))
    return out


def phase_text(e):
    if e["phase"] == "before":
        return f"前{e['days_to_start']}日"
    if e["phase"] == "after":
        return f"後{e['days_since_end']}日"
    return "期間"


def status_mark(e):
    return STATUS_MARK.get(e.get("date_status"), "")


def _md(s):
    d = _d(s)
    return f"{d.month}/{d.day}"


def format_line(e):
    """日報顯示用一行:'  OFC 2027 ｜ 期間(3/7–3/11)｜Los Angeles｜⚠暫定'。"""
    mark = status_mark(e)
    return (f"  {e['name']} ｜ {phase_text(e)}({_md(e['start'])}–{_md(e['end'])})｜{e.get('location', '')}"
            + (f"｜{mark}" if mark else ""))


def _distance(e):
    return 0 if e["phase"] == "during" else (
        e["days_to_start"] if e["phase"] == "before" else e["days_since_end"])


def tag_for_theme(theme_keywords, today, events=None):
    """題材關鍵字與 window 內事件的 themes 有交集(不分大小寫)→ '📅{short}前N日/期間/後N日';
    多個命中取最近一個(期間 > 距離近者)。無交集回傳 None。"""
    kws = {str(k).lower() for k in (theme_keywords or []) if k}
    if not kws:
        return None
    hits = [e for e in window(today, events=events)
            if kws & {str(t).lower() for t in e.get("themes", [])}]
    if not hits:
        return None
    e = min(hits, key=lambda e: (_distance(e), PHASE_ORDER[e["phase"]]))
    return f"📅{e['short']}{phase_text(e)}"


def background_text(events):
    """daily_report 用:大會背景(非投資訊號)。沒有事件回空字串。"""
    if not events:
        return ""
    lines = ["\n【大會背景(非投資訊號)】"]
    for e in events:
        mark = status_mark(e)
        themes = "、".join(e.get("themes", [])[:6]) or "(未對應題材)"
        lines.append(f"- {e['name']}:{phase_text(e)}({e['start']}~{e['end']},{e.get('location', '')})"
                     f" 相關題材:{themes}" + (f" {mark}" if mark else ""))
    return "\n".join(lines)


# ============================================================
# --validate
# ============================================================
def _watchlist_terms(path=WATCHLIST_FILE):
    try:
        with open(path, encoding="utf-8") as f:
            return {ln.strip().lower() for ln in f if ln.strip() and not ln.startswith("#")}
    except FileNotFoundError:
        return set()


def validate(path=CALENDAR_FILE, watchlist_path=WATCHLIST_FILE):
    """回傳 (errors, warnings)。"""
    errors, warnings = [], []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return [f"無法讀取 {path}: {e}"], []
    if not isinstance(data, dict) or not isinstance(data.get("events"), list):
        return ["最外層需為物件且含 events 陣列"], []
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(data.get("last_verified", ""))):
        warnings.append("last_verified 缺少或不是 YYYY-MM-DD")
    wl = _watchlist_terms(watchlist_path)
    seen = set()
    for i, e in enumerate(data["events"]):
        tag = e.get("id", f"#{i}")
        for k in REQUIRED:
            if k not in e:
                errors.append(f"{tag}: 缺少欄位 {k}")
        if e.get("id") in seen:
            errors.append(f"{tag}: id 重複")
        seen.add(e.get("id"))
        try:
            s, en = _d(e["start"]), _d(e["end"])
            if en < s:
                errors.append(f"{tag}: end 早於 start")
        except Exception:
            errors.append(f"{tag}: start/end 不是 YYYY-MM-DD")
        if e.get("date_status") not in DATE_STATUS:
            errors.append(f"{tag}: date_status 不合法({e.get('date_status')})")
        if e.get("kind") not in KINDS:
            errors.append(f"{tag}: kind 不合法({e.get('kind')})")
        if e.get("tw_relevance") not in RELEVANCE:
            errors.append(f"{tag}: tw_relevance 不合法({e.get('tw_relevance')})")
        if not isinstance(e.get("themes", []), list) or not isinstance(e.get("post_event_urls", []), list):
            errors.append(f"{tag}: themes/post_event_urls 需為陣列")
        missing = [t for t in e.get("themes", []) if str(t).lower() not in wl]
        if missing:
            warnings.append(f"{tag}: themes 不在 watchlist → {missing}")
    for i, a in enumerate(data.get("archive", [])):
        tag = a.get("id", f"archive#{i}")
        if a.get("id") in seen:
            errors.append(f"{tag}: id 與 events/archive 重複")
        seen.add(a.get("id"))
        if a.get("date_status") not in DATE_STATUS:
            errors.append(f"{tag}: date_status 不合法({a.get('date_status')})")
        for k in ("start", "end"):
            if a.get(k) is not None and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(a[k])):
                errors.append(f"{tag}: {k} 不是 YYYY-MM-DD 或 null")
        if a.get("start") is None and a.get("date_status") == "confirmed":
            errors.append(f"{tag}: 日期為 null 卻標 confirmed")
    return errors, warnings


def main():
    if "--validate" in sys.argv:
        errors, warnings = validate()
        data = load()
        print(f"events {len(data['events'])} 筆、archive {len(data['archive'])} 筆")
        for w in warnings:
            print(f"  ⚠ {w}")
        for e in errors:
            print(f"  ✗ {e}")
        print(f"結果:error {len(errors)}、warning {len(warnings)} → {'通過' if not errors else '失敗'}")
        sys.exit(1 if errors else 0)
    today = dt.date.today()
    for e in window(today):
        print(format_line(e))


if __name__ == "__main__":
    main()
