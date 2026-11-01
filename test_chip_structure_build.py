# -*- coding: utf-8 -*-
"""chip_structure_build.py 測試(不需網路)。"""
import os
import json
import datetime as dt

import chip_structure_build as cb
from test_chip_structure_core import levels, weekdays_cal, insti_for

NOW = dt.datetime(2026, 10, 31, 17, 0, tzinfo=cb.TPE)


def _weeks(n=3):
    start = dt.date(2026, 10, 9)
    return [((start + dt.timedelta(days=7 * i)).isoformat(), {"2408": levels(big=400_000 + 5_000 * i), "9999": levels()})
            for i in range(n)]


def test_build_only_listed_and_compact_fields():
    cal = weekdays_cal()
    ins = insti_for(cal, code="2408", f=50_000, d=10_000, fh=15.0)
    for doc in ins.values():
        doc["names"] = {"2408": "南亞科"}
    out = cb.build(_weeks(), ins, cal, now=NOW)
    assert list(out["stocks"]) == ["2408"]                       # 9999 沒有法人資料(興櫃等)→ 不放
    s = out["stocks"]["2408"]
    assert s["n"] == "南亞科" and len(s["w"]) == 3 and s["w"][-1][0] == "10-23"
    assert s["d1"][0] > 0 and s["i"][4] == s["i"][0] + s["i"][2]
    assert s["ag"] == "confirm_up" and s["fp"][0] == 15.0
    assert out["tdcc_asof"] == "2026-10-23" and out["count"] == 1
    cb.check_text(out)


def test_weeks_capped():
    cal = weekdays_cal()
    out = cb.build(_weeks(11), insti_for(cal), cal, now=NOW)
    assert len(out["stocks"]["2408"]["w"]) == cb.WEEKS_KEEP and len(out["weeks"]) == cb.WEEKS_KEEP


def test_check_text_rejects_banned_words():
    import pytest
    with pytest.raises(ValueError):
        cb.check_text({"notes": ["大戶吸籌"], "fields": {}})


def test_failure_keeps_old_file_marked_stale(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / cb.OUT_FILE).write_text(json.dumps({"tdcc_asof": "2026-10-22", "stocks": {"1": {}}, "stale": False}))
    os.makedirs(tmp_path / cb.TDCC_DIR)                           # 沒有任何週資料 → 失敗
    assert cb.main() == 1
    old = json.loads((tmp_path / cb.OUT_FILE).read_text())
    assert old["stale"] is True and old["stocks"] == {"1": {}} and "stale_reason" in old


def test_unchanged_ignores_generated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cal = weekdays_cal()
    out = cb.build(_weeks(), insti_for(cal), cal, now=NOW)
    cb.write(out)
    again = cb.build(_weeks(), insti_for(cal), cal, now=NOW + dt.timedelta(hours=3))
    assert cb.unchanged(again)


def test_etf_excluded():
    cal = weekdays_cal()
    weeks = [(d, dict(lv, **{"0050": levels()})) for d, lv in _weeks()]
    ins = insti_for(cal, code="0050")
    for doc in ins.values():
        doc["twse"]["2408"] = [1, 0, 0, 1]
    out = cb.build(weeks, ins, cal, now=NOW)
    assert "0050" not in out["stocks"] and "2408" in out["stocks"]
