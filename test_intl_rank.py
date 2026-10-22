# -*- coding: utf-8 -*-
"""test_intl_rank.py — intl_rank.py 的清單解析、分批下載、主流程(不需網路)。核心計算的測試在 test_intl_rank_core.py。"""
import os
import json
import datetime as dt
import tempfile

import numpy as np
import pandas as pd

import intl_rank as ir

NASDAQ = """Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares
NVDA|NVIDIA Corporation - Common Stock|Q|N|N|100|N|N
QQQ|Invesco QQQ Trust, Series 1|G|N|N|100|Y|N
ZXZZT|NASDAQ TEST STOCK|G|Y|N|100|N|N
ABCDW|ABC Acquisition Corp - Warrants|S|N|N|100|N|N
BADF|Bad Finance Inc - Common Stock|S|N|D|100|N|N
UNTD|United Fire Group - Common Stock|Q|N|N|100|N|N
File Creation Time: 1019202606:01|||||||
"""
OTHER = """ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol
BRK.B|Berkshire Hathaway Inc. Class B|N|BRK.B|N|100|N|BRK.B
TSM|Taiwan Semiconductor Manufacturing Company Ltd. American Depositary Shares|N|TSM|N|100|N|TSM
SPY|SPDR S&P 500 ETF Trust|P|SPY|Y|100|N|SPY
XYZ.U|XYZ Acquisition Corp Units|N|XYZ.U|N|100|N|XYZ.U
ABC$A|ABC Corp 6% Preferred Series A|N|ABC PRA|N|100|N|ABC-A
File Creation Time: 1019202606:01|||||||
"""
KIND = """<table><tr><th>회사명</th><th>시장구분</th><th>종목코드</th><th>업종</th></tr>
<tr><td>삼성전자</td><td>유가</td><td>005930</td><td>반도체</td></tr>
<tr><td>에코프로비엠</td><td>코스닥</td><td>247540</td><td>전지</td></tr>
<tr><td>어떤코넥스</td><td>코넥스</td><td>123456</td><td>기타</td></tr></table>"""


def test_parse_us_filters():
    u = ir.parse_us(NASDAQ, OTHER)
    assert set(u) == {"NVDA", "UNTD", "BRK-B", "TSM"}, u            # ETF/Test/Warrant/非N財務/Unit/優先股 都排除
    assert u["BRK-B"]["code"] == "BRK.B" and u["NVDA"]["name"] == "NVIDIA Corporation"
    assert u["TSM"]["name"].startswith("Taiwan Semiconductor")      # ADR(Depositary Shares)保留


def test_parse_jp_domestic_only():
    df = pd.DataFrame({"日付": ["20261019"] * 4, "コード": ["7203", "285A", "1306", "9999"],
                       "銘柄名": ["トヨタ自動車", "キオクシアＨＤ", "ＴＯＰＩＸ連動型", "外國株"],
                       "市場・商品区分": ["プライム（内国株式）", "プライム（内国株式）", "ETF・ETN", "グロース（外国株式）"],
                       "33業種コード": ["3700", "3650", "-", "-"]})
    j = ir.parse_jp(df)
    assert set(j) == {"7203.T", "285A.T"} and j["285A.T"] == {"code": "285A", "name": "キオクシアＨＤ"}


def test_parse_kr_suffix_and_zfill():
    k = ir.parse_kr(KIND)
    assert set(k) == {"005930.KS", "247540.KQ"} and k["005930.KS"]["code"] == "005930"


def _fake_df(tickers, days=20, start="2026-09-01"):
    idx = pd.bdate_range(start, periods=days)
    cols = pd.MultiIndex.from_product([tickers, ["Close", "Volume"]])
    data = np.ones((days, len(cols)))
    return pd.DataFrame(data * 10, index=idx, columns=cols)


def test_download_retry_and_budget():
    calls, sleeps = [], []

    def flaky(chunk):
        calls.append(tuple(chunk))
        if len(calls) == 1:
            raise RuntimeError("rate limited")
        return _fake_df(chunk)
    c, v, st = ir.download_panel(["A", "B", "C"], fetch=flaky, sleep=sleeps.append, chunk=2)
    assert st["batches"] == 2 and st["failed_batches"] == 0 and st["got"] == 3
    assert 5 in sleeps and list(c.columns) == ["A", "B", "C"]
    # 三次都失敗 → 該批算失敗,其餘照常
    def always_fail_first(chunk):
        if "A" in chunk:
            raise RuntimeError("x")
        return _fake_df(chunk)
    c, v, st = ir.download_panel(["A", "B", "C"], fetch=always_fail_first, sleep=lambda s: None, chunk=2)
    assert st["failed_batches"] == 1 and list(c.columns) == ["C"] and st["scanned"] == 1
    # 時間預算:第二批前就超時
    t = iter([0, 100, 100])
    c, v, st = ir.download_panel(["A", "B", "C"], fetch=_fake_df, sleep=lambda s: None, clock=lambda: next(t),
                                 deadline=50, chunk=2)
    assert st["timed_out"] and list(c.columns) == ["A", "B"]


def test_short_history_skipped():
    c, v, st = ir.download_panel(["A"], fetch=lambda ch: _fake_df(ch, days=10), sleep=lambda s: None)
    assert c is None and st["no_data"] == 1


def _block(key, asof="2026-10-16"):
    return {"label": key, "asof": asof, "currency": "USD", "unit_short": "x", "price_dec": 2, "universe": 1,
            "by_value": [{"rank": 1, "code": "A", "name": "A", "close": 1, "chgPct": 0, "value": 1,
                          "vsAvg": None, "streak": 1, "isNew": False}]}


def test_run_all_fail_keeps_file_and_exits_1():
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "intl_rank.json")
        with open(out, "w") as f:
            f.write('{"generated":"old","markets":{}}')
        code = ir.run(["us", "jp"], out=out, build=lambda m, **k: (None, {"error": "x"}),
                      fx_loader=lambda: None, tags={})
        assert code == 1 and open(out).read() == '{"generated":"old","markets":{}}'


def test_run_one_market_fails_others_update():
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "intl_rank.json")
        with open(out, "w") as f:
            json.dump({"generated": "old", "markets": {"us": _block("us", "2026-10-15"), "kr": _block("kr", "2026-10-15")}}, f)
        build = lambda m, **k: ((_block(m, "2026-10-19"), {}) if m == "jp" else (None, {"error": "x"}))
        assert ir.run(["jp", "kr"], out=out, build=build, fx_loader=lambda: None, tags={}) == 0
        p = json.load(open(out))
        assert p["markets"]["jp"]["asof"] == "2026-10-19" and "stale" not in p["markets"]["jp"]
        assert p["markets"]["kr"]["stale"] is True and p["markets"]["us"]["stale"] is True   # 失敗/未指定 → 沿用舊資料
        assert p["last_run"]["ok"] == ["jp"] and p["last_run"]["failed"] == ["kr"]
        assert "\n" not in open(out).read()                                                  # 精簡格式


def test_build_market_end_to_end_with_fakes():
    idx = pd.bdate_range("2026-09-01", periods=30)
    meta = {"A": {"code": "A", "name": "Alpha"}, "B": {"code": "B", "name": "Beta"}}

    def dl(tickers, deadline=None):
        close = pd.DataFrame({"A": 1000.0, "B": 500.0}, index=idx)
        vol = pd.DataFrame({"A": 3e7, "B": 1e6}, index=idx)
        return close, vol, {"requested": 2, "scanned": 2, "got": 2, "no_data": 0, "batches": 1,
                            "failed_batches": 0, "timed_out": False}
    last = idx[-1]
    now = dt.datetime(last.year, last.month, last.day, 14, 0, tzinfo=dt.timezone.utc)   # 東京 23:00 → 已收盤
    fx = pd.DataFrame({("JPY=X", "Close"): [150.0] * len(idx), ("KRW=X", "Close"): [1350.0] * len(idx)}, index=idx)
    b, info = ir.build_market("jp", universe_fn=lambda m: meta, download_fn=dl, fx_frame=fx, now_utc=now,
                              tags={"jp": {"A": "測試"}}, top_n=2)
    assert b["asof"] == str(last.date()) and b["by_value"][0]["code"] == "A" and b["by_value"][0]["tag"] == "測試"
    assert b["by_value"][0]["value"] == 300 and b["by_value"][0]["usd"] == 2.0 and info["fx"] == 150.0
    # 東京 10:00(盤中)→ 最後一天丟掉
    now2 = dt.datetime(last.year, last.month, last.day, 1, 0, tzinfo=dt.timezone.utc)
    b2, _ = ir.build_market("jp", universe_fn=lambda m: meta, download_fn=dl, fx_frame=None, now_utc=now2, top_n=2)
    assert b2["asof"] < str(last.date()) and "usd" not in b2["by_value"][0]
    # 清單抓不到 → None,不丟例外
    b3, info3 = ir.build_market("kr", universe_fn=lambda m: (_ for _ in ()).throw(OSError("blocked")), download_fn=dl)
    assert b3 is None and "blocked" in info3["error"]


def test_us_tags_follow_src6():
    tags = ir.load_tags()
    from earnings_keywords import COMPANIES
    assert set(tags["us"]) == set(COMPANIES) and tags["us"]["NVDA"] == "AI晶片"
    assert "MU" not in tags["us"]


def test_save_totals_merges_and_trims():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "t.json")
        ir.save_totals({"us": {"2026-10-15": 1.0, "2026-10-16": 2.0}}, path=p, keep=2)
        ir.save_totals({"us": {"2026-10-17": 3.0}, "jp": {"2026-10-17": 9.0}}, path=p, keep=2)
        assert json.load(open(p)) == {"us": {"2026-10-16": 2.0, "2026-10-17": 3.0}, "jp": {"2026-10-17": 9.0}}


def test_kr_close_time_waits_for_after_hours():
    """韓股 16:00~18:00 盤後單一價交易的量算進日線 → 首爾 17:40 不能把當天當成已收盤。"""
    from zoneinfo import ZoneInfo
    import group_flow as gf
    assert ir.MARKETS["kr"]["close"] == gf.CFG["kr"]["close"] == dt.time(18, 10)
    idx = pd.bdate_range("2026-09-01", periods=30)
    meta = {"A": {"code": "A", "name": "A"}, "B": {"code": "B", "name": "B"}}
    dl = lambda t, deadline=None: (pd.DataFrame({"A": 1.0, "B": 1.0}, index=idx), pd.DataFrame({"A": 1e8, "B": 1e8}, index=idx),
                                   {"requested": 2, "scanned": 2, "got": 2, "no_data": 0, "batches": 1, "failed_batches": 0, "timed_out": False})
    last = idx[-1]
    at = lambda h, m: dt.datetime(last.year, last.month, last.day, h, m, tzinfo=ZoneInfo("Asia/Seoul")).astimezone(dt.timezone.utc)
    early, _ = ir.build_market("kr", universe_fn=lambda m: meta, download_fn=dl, now_utc=at(17, 40), top_n=2)
    late, _ = ir.build_market("kr", universe_fn=lambda m: meta, download_fn=dl, now_utc=at(18, 20), top_n=2)
    assert early["asof"] < str(last.date()) and late["asof"] == str(last.date())


def test_run_keeps_old_when_new_asof_is_older():
    # 2026-10-19:Yahoo 最後一天資料不完整被丟掉 → 新結果的資料日比檔案裡的舊 → 不可倒退,沿用舊的並標失敗
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "intl_rank.json")
        with open(out, "w") as f:
            json.dump({"generated": "old", "markets": {"kr": _block("kr", "2026-10-19")}}, f)
        build = lambda m, **k: (_block(m, "2026-10-16"), {})
        assert ir.run(["jp", "kr"], out=out, build=build, fx_loader=lambda: None, tags={}) == 0
        p = json.load(open(out))
        assert p["markets"]["kr"]["asof"] == "2026-10-19" and p["markets"]["kr"]["stale"] is True
        assert p["markets"]["jp"]["asof"] == "2026-10-16"                     # 沒有舊資料 → 照常寫入
        assert p["last_run"]["ok"] == ["jp"] and p["last_run"]["failed"] == ["kr"]


def test_expected_asof_and_catchup():
    # 台北 10/22(四)07:30 = UTC 10/21 23:30:東京/首爾 10/22 08:30 → 應有資料日 10/21(三)
    now = dt.datetime(2026, 10, 21, 23, 30, tzinfo=dt.timezone.utc)
    assert ir.expected_asof("jp", now) == "2026-10-21" and ir.expected_asof("kr", now) == "2026-10-21"
    # 週一早上 → 上週五
    mon = dt.datetime(2026, 10, 25, 23, 30, tzinfo=dt.timezone.utc)
    assert ir.expected_asof("jp", mon) == "2026-10-23"
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "intl_rank.json")
        with open(out, "w") as f:
            json.dump({"markets": {"jp": _block("jp", "2026-10-20"), "kr": _block("kr", "2026-10-21")}}, f)
        assert ir.stale_markets(["jp", "kr"], out=out, now_utc=now) == ["jp"]
        assert ir.stale_markets(["jp"], out=os.path.join(d, "none.json"), now_utc=now) == ["jp"]
