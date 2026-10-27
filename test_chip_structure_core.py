# -*- coding: utf-8 -*-
import datetime as dt

import pytest

import chip_structure_core as cs

CFG = cs.CHIP_CFG


def levels(big=400_000, mid=300_000, retail=300_000, adj=0, holders=(10, 5, 2)):
    """1–8 散戶、9–11 中實戶、12–15 大戶;合計 = Σ1–15 − 第16級(與集保實際關係一致)。"""
    lv = {i: {"holders": 0, "shares": 0, "pct": 0} for i in range(1, 16)}
    lv[1].update(holders=holders[0], shares=retail)
    lv[9].update(holders=holders[1], shares=mid)
    lv[15].update(holders=holders[2], shares=big)
    lv[16] = {"holders": 1 if adj else 0, "shares": adj, "pct": 0}
    lv[17] = {"holders": sum(holders), "shares": big + mid + retail - adj, "pct": 100}
    return lv


def fridays(n, start=dt.date(2026, 1, 2)):
    return [(start + dt.timedelta(days=7 * i)).isoformat() for i in range(n)]


def weekdays_cal(start=dt.date(2025, 12, 20), end=dt.date(2026, 12, 31), holidays=()):
    out, d = set(), start
    while d <= end:
        if d.weekday() < 5 and d.isoformat() not in holidays:
            out.add(d.strftime("%Y%m%d"))
        d += dt.timedelta(days=1)
    return out


def insti_for(cal, code="2408", f=0, t=0, d=0, fh=None, market="twse"):
    return {day: {market: {code: [f, t, d, f + t + d]}, "foreign_pct": {market: {code: fh} if fh is not None else {}}}
            for day in cal}


# ── 分層 ──
def test_tiers_sum_to_total_and_level16_separate():
    m = cs.week_metrics(levels(adj=10_000))
    assert m["shares_total"] == 990_000
    # 三層加總 − 第16級 = 合計;第 16 級另列,不進任何一層
    assert m["shares_big"] + m["shares_mid"] + m["shares_retail"] - m["adj_shares"] == m["shares_total"]
    assert m["adj_shares"] == 10_000
    assert m["pct_big"] == round(400_000 / 990_000 * 100, 4)
    assert m["holders_big"] + m["holders_mid"] + m["holders_retail"] == m["holders_total"]


def test_tier_boundaries_come_from_cfg():
    cfg = dict(CFG, tiers={"retail": list(range(1, 10)), "mid": [10, 11], "big": list(range(12, 16))})
    m = cs.week_metrics(levels(), cfg)
    assert m["shares_retail"] == 600_000 and m["shares_mid"] == 0


def test_no_total_returns_none():
    lv = levels()
    del lv[17]
    assert cs.week_metrics(lv) is None


# ── 單位 ──
def test_unit_conversion():
    assert cs.to_shares(12, "lots") == 12_000
    assert cs.to_shares(-3_500, "shares") == -3_500
    assert cs.to_shares(None, "lots") is None
    with pytest.raises(ValueError):
        cs.to_shares(1, "張")


# ── 集保週交易日 ──
def test_week_days_with_holiday():
    cal = weekdays_cal(holidays={"2026-10-09"})                       # 週五放假
    days = cs.tdcc_week_days("2026-10-02", "2026-10-08", cal)
    assert days == ["20261005", "20261006", "20261007", "20261008"]   # 4 天,不是固定 5 天
    nxt = cs.tdcc_week_days("2026-10-08", "2026-10-16", cal)
    assert nxt[0] == "20261012" and len(nxt) == 5


def test_week_of_and_date_mismatch():
    dates = ["2026-10-15", "2026-10-22"]
    assert cs.week_of("20261020", dates) == "2026-10-22"
    assert cs.week_of("20261027", dates) is None
    assert cs.date_mismatch("2026-10-22", "20261022", dates) is False
    assert cs.date_mismatch("2026-10-22", "20261027", dates) is True
    assert cs.date_mismatch("2026-10-22", None, dates) is True


# ── 法人 ──
def test_inst_week_sums_and_missing_day_blanks():
    cal = weekdays_cal()
    days = cs.tdcc_week_days("2026-01-02", "2026-01-09", cal)
    ins = insti_for(days, f=100, t=50, d=-20)
    w = cs.inst_week("2408", days, ins)
    assert w["foreign"] == 500 and w["trust"] == 250 and w["dealer"] == -100
    assert w["net_for_agreement"] == 400 and w["net"] == 650
    del ins[days[2]]
    w = cs.inst_week("2408", days, ins)
    assert w["net"] is None and w["missing_days"] == [days[2]]       # 缺一天就留空,不補


def test_inst_market_section_null_counts_as_missing():
    ins = {"20260105": {"twse": {}, "tpex": None}}
    assert cs.inst_week("6488", ["20260105"], ins)["missing_days"] == ["20260105"]


# ── agreement ──
def test_agreement_basic_and_threshold():
    tot = 1_000_000_000
    assert cs.agreement(0.3, 5_000_000, tot) == "confirm_up"
    assert cs.agreement(-0.3, -5_000_000, tot) == "confirm_down"
    assert cs.agreement(-0.3, 5_000_000, tot) == "diverge_inst_buy_big_down"
    assert cs.agreement(0.3, -5_000_000, tot) == "diverge_inst_sell_big_up"
    assert cs.agreement(0.01, 5_000_000, tot) == "unclear"           # 大戶變化太小
    assert cs.agreement(0.3, 10_000, tot) == "unclear"               # 法人太小
    assert cs.agreement(0.3, None, tot) == "unclear"


def test_trust_only_buying_never_diverge():
    """投信單獨買超、大戶未增 → 不得是 diverge_*(agreement 只看外資+自營商)。"""
    weeks = [(d, {"2408": levels(big=400_000 - 2_000 * i)}) for i, d in enumerate(fridays(3))]
    cal = weekdays_cal()
    ins = insti_for(cal, t=1_000_000)                                 # 只有投信大買
    s = cs.stock_series("2408", weeks, ins, cal)
    assert all(not r["agreement"].startswith("diverge") for r in s)
    assert s[-1]["inst"]["trust"] > 0 and s[-1]["inst"]["net_for_agreement"] == 0


# ── 品質旗標 ──
def test_capital_change_forces_unclear_and_unreliable():
    weeks = [("2026-01-02", {"2408": levels()}),
             ("2026-01-09", {"2408": levels(big=600_000)})]          # 合計 +20% → 股本變動嫌疑
    cal = weekdays_cal()
    s = cs.stock_series("2408", weeks, insti_for(cal, f=10_000), cal)
    r = s[-1]
    assert "capital_change_suspect" in r["flags"] and r["d1w_unreliable"] and r["d4w_unreliable"]
    assert r["agreement"] == "unclear" and r["explain_ratio"] is None
    assert r["d1w"]["big"] is not None                               # 數值照算、不修正,只標記


def test_missing_week_flag():
    weeks = [("2026-01-02", {"2408": levels()}), ("2026-01-16", {"2408": levels()})]
    cal = weekdays_cal()
    s = cs.stock_series("2408", weeks, insti_for(cal), cal)
    assert "missing_week" in s[1]["flags"] and "missing_week" not in s[0]["flags"]
    assert not cs.missing_week("2026-10-15", "2026-10-22")
    assert cs.missing_week("2026-10-08", "2026-10-22")


# ── explain_ratio ──
def test_explain_ratio_null_near_zero():
    tot = 1_000_000_000
    assert cs.explain_ratio(1_000_000, 100, tot) is None
    assert cs.explain_ratio(1_000_000, 2_000_000, tot) == 0.5
    assert cs.explain_ratio(None, 2_000_000, tot) is None


# ── big_trend / stabilizing ──
def test_big_trend_labels():
    assert cs.big_trend([30.0] * 7) == "insufficient_history"
    assert cs.big_trend([30 + 0.3 * i for i in range(8)]) == "rising"
    assert cs.big_trend([30 - 0.3 * i for i in range(8)]) == "falling_trend"
    assert cs.big_trend([30, 30.4, 30.1, 30.5, 30.2, 30.6, 30.3, 30.5]) == "oscillating"
    assert cs.big_trend([30, 30.05, 30.1, 30.1, 30.12, 30.15, 30.16, 30.2]) == "flat"
    assert cs.big_trend([30 + 0.3 * i for i in range(8)], unreliable=[False] * 6 + [True]) == "unreliable"


def test_stabilizing():
    assert cs.stabilizing([30.0] * 11, [0.0] * 11) is None
    down_then_hold = [32, 31.5, 31, 30.5, 30, 29.5, 29.2, 29.0, 29.1, 29.2, 29.3, 29.4]
    assert cs.stabilizing(down_then_hold, [0.1, 0.1]) is True
    new_low = [32, 31.5, 31, 30.5, 30, 29.5, 29.2, 29.0, 29.1, 28.5, 28.7, 28.9]
    assert cs.stabilizing(new_low, [0.2, 0.2]) is False
    assert cs.stabilizing(down_then_hold, [-0.1, 0.1]) is False


# ── 無 look-ahead ──
def test_no_lookahead():
    dates = fridays(14)
    weeks = [(d, {"2408": levels(big=400_000 + (i % 3) * 5_000 - i * 3_000)}) for i, d in enumerate(dates)]
    cal = weekdays_cal()
    ins = insti_for(cal, f=20_000, d=-5_000, fh=40.0)
    full = cs.stock_series("2408", weeks, ins, cal)
    for t in range(len(weeks)):
        part = cs.stock_series("2408", weeks[:t + 1], ins, cal)
        assert part[t] == full[t]


# ── 外資持股 ──
def test_foreign_pct_missing_is_null_and_isolated():
    weeks = [(d, {"2408": levels()}) for d in fridays(5)]
    cal = weekdays_cal()
    ins = insti_for(cal, f=10_000)                                   # 沒有外資持股
    s = cs.stock_series("2408", weeks, ins, cal)
    assert all(r["foreign_pct"] is None and r["foreign_pct_d4w"] is None for r in s)
    assert s[-1]["inst"]["foreign"] is not None and s[-1]["d4w"]["big"] == 0

    ins = insti_for(cal, f=10_000, fh=40.0)
    ins[cs.tdcc_week_days(weeks[3][0], weeks[4][0], cal)[-1]]["foreign_pct"]["twse"]["2408"] = 41.5
    s = cs.stock_series("2408", weeks, ins, cal)
    assert s[-1]["foreign_pct"] == 41.5 and s[-1]["foreign_pct_d4w"] == 1.5


# ── 輸出用語 ──
def test_notes_have_no_forbidden_words():
    banned = ("主力", "洗盤", "吸籌", "看多", "看空", "建議買進")
    text = "".join(cs.NOTES)
    assert not any(w in text for w in banned)
