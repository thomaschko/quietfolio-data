import datetime as dt, json
import numpy as np, pandas as pd
import intl_rank_core as c

def frame(days, tick, fn_close, fn_vol, start="2026-09-01"):
    idx = pd.bdate_range(start, periods=days)
    close = pd.DataFrame({t: [fn_close(t, i) for i in range(days)] for t in tick}, index=idx)
    vol = pd.DataFrame({t: [fn_vol(t, i) for i in range(days)] for t in tick}, index=idx)
    return close, vol

T = [f"S{i}" for i in range(12)]

def test_value_rank_and_units():
    # S0 成交值最大;value = close*volume/1e8
    close, vol = frame(30, T, lambda t,i: 100.0, lambda t,i: (12-int(t[1:]))*1_000_000)
    b = c.build_market_block(close, vol, {t:{"code":t,"name":t} for t in T}, label="美股", currency="USD",
                             unit_short="美元", price_dec=2, top_n=5)
    assert [r["code"] for r in b["by_value"]] == ["S0","S1","S2","S3","S4"]
    assert b["by_value"][0]["value"] == round(100*12_000_000/1e8, 1) == 12.0
    assert "usd" not in b["by_value"][0]

def test_fx_and_non_usd_rounding():
    close, vol = frame(30, T, lambda t,i: 1000.0, lambda t,i: 5_000_000 if t=="S0" else 1000)
    b = c.build_market_block(close, vol, {t:{"code":t,"name":t} for t in T}, label="日股", currency="JPY",
                             unit_short="日圓", price_dec=0, top_n=3, fx_per_usd=150)
    r = b["by_value"][0]
    assert r["value"] == 50 and r["usd"] == round(50/150,1)

def test_vsavg_streak_isnew():
    # S1 前 29 天都小量,最後一天爆量成為第1名 → isNew、streak=1、vsAvg≈+X
    def vol(t,i):
        if t=="S1": return 1_000_000 if i==29 else 10_000
        return 100_000
    close, v = frame(30, T[:4], lambda t,i: 10.0, vol)
    b = c.build_market_block(close, v, {t:{"code":t,"name":t} for t in T[:4]}, label="x", currency="USD",
                             unit_short="美元", price_dec=2, top_n=2)
    r = b["by_value"][0]
    assert r["code"]=="S1" and r["isNew"] is True and r["streak"]==1
    assert r["vsAvg"] == int(round((1_000_000/10_000 - 1)*100))   # +9900
    r2 = b["by_value"][1]; assert r2["streak"] >= 29 and r2["isNew"] is False

def test_chg_pct_uses_previous_valid_close():
    close, vol = frame(30, T[:3], lambda t,i: 100.0, lambda t,i: 1000)
    close.iloc[-1, 0] = 110.0
    b = c.build_market_block(close, vol, {t:{"code":t,"name":t} for t in T[:3]}, label="x", currency="USD",
                             unit_short="美元", price_dec=2, top_n=3)
    s0 = [r for r in b["by_value"] if r["code"]=="S0"][0]
    assert s0["chgPct"] == 10.0

def test_drop_partial_today_bar():
    close, vol = frame(10, T[:5], lambda t,i: 10.0, lambda t,i: 1000)
    today = close.index[-1].date()
    now_open = dt.datetime.combine(today, dt.time(10, 0))            # 盤中
    cc, vv, notes = c.drop_incomplete(close, vol, now_open, dt.time(16, 20))
    assert cc.index[-1].date() < today and any("尚未收盤" in n for n in notes)
    now_closed = dt.datetime.combine(today, dt.time(17, 0))          # 已收盤
    cc2, vv2, notes2 = c.drop_incomplete(close, vol, now_closed, dt.time(16, 20))
    assert cc2.index[-1].date() == today and not notes2

def test_future_date_dropped():
    close, vol = frame(10, T[:5], lambda t,i: 10.0, lambda t,i: 1000)
    now = dt.datetime.combine(close.index[-3].date(), dt.time(23, 0))
    cc, vv, notes = c.drop_incomplete(close, vol, now, dt.time(16, 20))
    assert cc.index[-1] == close.index[-3] and any("未來" in n for n in notes)

def test_coverage_guard_drops_sparse_last_day():
    close, vol = frame(12, T, lambda t,i: 10.0, lambda t,i: 1000)
    vol.iloc[-1, 2:] = np.nan                                       # 最後一天只剩 2 檔有資料
    now = dt.datetime.combine(close.index[-1].date(), dt.time(23, 0))
    cc, vv, notes = c.drop_incomplete(close, vol, now, dt.time(16, 20))
    assert cc.index[-1] == close.index[-2] and any("覆蓋率" in n for n in notes)

def test_zero_volume_excluded():
    close, vol = frame(30, T[:3], lambda t,i: 10.0, lambda t,i: 0 if t=="S0" else 1000)
    b = c.build_market_block(close, vol, {t:{"code":t,"name":t} for t in T[:3]}, label="x", currency="USD",
                             unit_short="美元", price_dec=2, top_n=3)
    assert "S0" not in [r["code"] for r in b["by_value"]]

def test_fit_size_shrinks():
    rows = [{"rank":i,"code":f"C{i}","name":"長名稱"*10,"close":1.0,"chgPct":0.1,"value":1.0,"vsAvg":1,"streak":1,"isNew":False} for i in range(50)]
    p = {"markets":{k:{"by_value":list(rows)} for k in ("us","jp","kr")}}
    p, n = c.fit_size(p, limit=12000)
    assert n < 50 and len(json.dumps(p, ensure_ascii=False, separators=(",",":"))) <= 12000
    assert p["top_n"] == n

def test_merge_preserve_marks_stale():
    old = {"markets":{"us":{"asof":"2026-10-15","by_value":[1]},"jp":{"asof":"2026-10-15","by_value":[1]}}}
    out = c.merge_preserve(old, {"jp":{"asof":"2026-10-16","by_value":[2]}, "kr":None}, "g")
    assert out["markets"]["jp"]["asof"]=="2026-10-16" and "stale" not in out["markets"]["jp"]
    assert out["markets"]["us"]["stale"] is True and "kr" not in out["markets"]

def test_no_lookahead_in_streak():
    # 截斷最後一天後,倒數第二天的結果不應受影響(橫斷面名次只用當日資料)
    close, vol = frame(30, T[:6], lambda t,i: 10.0, lambda t,i: (6-int(t[1:]))*1000 + (i%3))
    meta = {t:{"code":t,"name":t} for t in T[:6]}
    full = c.build_market_block(close, vol, meta, label="x", currency="USD", unit_short="美元", price_dec=2, top_n=3)
    cut  = c.build_market_block(close.iloc[:-1], vol.iloc[:-1], meta, label="x", currency="USD", unit_short="美元", price_dec=2, top_n=3)
    assert full["by_value"][0]["streak"] == cut["by_value"][0]["streak"] + 1
