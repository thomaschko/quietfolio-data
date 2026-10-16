# -*- coding: utf-8 -*-
"""
test_events_calendar.py — events_calendar.py 單元測試(不需網路)

用法:python test_events_calendar.py
"""
import datetime as dt
import os

import events_calendar as ec

D = dt.date
OFC = {"id": "ofc-2027", "name": "OFC 2027", "short": "OFC", "start": "2027-03-07", "end": "2027-03-11",
       "location": "Los Angeles", "kind": "expo", "themes": ["CPO", "矽光子"], "tw_relevance": "high",
       "date_status": "confirmed", "source": "", "post_event_urls": [], "note": ""}
GTC = {**OFC, "id": "gtc-2027", "name": "NVIDIA GTC 2027", "short": "GTC", "start": "2027-03-14",
       "end": "2027-03-18", "location": "San Jose", "themes": ["Vera Rubin", "CPO"], "date_status": "tentative"}
MWC = {**OFC, "id": "mwc-2027", "name": "MWC Barcelona 2027", "short": "MWC", "start": "2027-03-01",
       "end": "2027-03-04", "location": "Barcelona", "themes": ["低軌衛星"]}


def phases(today, events):
    return [(e["id"], e["phase"]) for e in ec.window(today, events=events)]


def test_before_boundary():
    assert phases(D(2027, 2, 21), [OFC]) == [("ofc-2027", "before")]      # start 前 14 天命中
    assert phases(D(2027, 2, 20), [OFC]) == []                            # 前 15 天不命中


def test_during_inclusive():
    assert phases(D(2027, 3, 7), [OFC]) == [("ofc-2027", "during")]       # start 當天
    assert phases(D(2027, 3, 11), [OFC]) == [("ofc-2027", "during")]      # end 當天


def test_after_boundary():
    assert phases(D(2027, 3, 25), [OFC]) == [("ofc-2027", "after")]       # end 後 14 天命中
    assert phases(D(2027, 3, 26), [OFC]) == []                            # 後 15 天不命中


def test_sort_order():
    """期間 → 會前(近的在前)→ 會後(近的在前)。"""
    near_before = {**OFC, "id": "b1", "start": "2027-03-12", "end": "2027-03-13"}
    far_before = {**OFC, "id": "b2", "start": "2027-03-20", "end": "2027-03-21"}
    near_after = {**OFC, "id": "a1", "start": "2027-03-05", "end": "2027-03-08"}
    far_after = {**OFC, "id": "a2", "start": "2027-03-01", "end": "2027-03-02"}
    during = {**OFC, "id": "d1", "start": "2027-03-09", "end": "2027-03-11"}
    got = [e["id"] for e in ec.window(D(2027, 3, 10), events=[far_after, far_before, near_after, during, near_before])]
    assert got == ["d1", "b1", "b2", "a1", "a2"], got


def test_load_missing_file_returns_empty():
    out = ec.load("no_such_calendar_file.json")
    assert out == {"events": [], "archive": []}
    with open("tmp_bad_calendar.json", "w", encoding="utf-8") as f:
        f.write("{not json")
    try:
        assert ec.load("tmp_bad_calendar.json") == {"events": [], "archive": []}
    finally:
        os.remove("tmp_bad_calendar.json")


def test_tag_for_theme():
    evs = [OFC, GTC, MWC]
    assert ec.tag_for_theme(["HVDC", "液冷"], D(2027, 3, 9), events=evs) is None       # 無交集
    assert ec.tag_for_theme(["矽光子"], D(2027, 3, 4), events=evs) == "📅OFC前3日"
    assert ec.tag_for_theme(["cpo"], D(2027, 3, 9), events=evs) == "📅OFC期間"          # 不分大小寫
    # 3/12:OFC 後1日、GTC 前2日 → 取最近的 OFC
    assert ec.tag_for_theme(["CPO"], D(2027, 3, 12), events=evs) == "📅OFC後1日"
    assert ec.tag_for_theme(["CPO"], D(2027, 3, 15), events=evs) == "📅GTC期間"


def test_tentative_mark_in_output():
    line = ec.format_line(ec.window(D(2027, 3, 15), events=[GTC])[0])
    assert "⚠暫定" in line and "期間(3/14–3/18)" in line
    assert "⚠" not in ec.format_line(ec.window(D(2027, 3, 9), events=[OFC])[0])
    assert "⚠未驗證" in ec.format_line(ec.window(D(2027, 3, 9), events=[{**OFC, "date_status": "unverified"}])[0])


def test_real_calendar_validates():
    errors, _ = ec.validate()
    assert errors == [], errors


if __name__ == "__main__":
    import sys
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ✓ {name}")
            except AssertionError as e:
                fails += 1
                print(f"  ✗ {name}: {e}")
    print("全部通過" if not fails else f"{fails} 項失敗")
    sys.exit(1 if fails else 0)
