# -*- coding: utf-8 -*-
import datetime as dt
import json

import pytest

import fp_fetch as FF
import foreign_positioning as F

CFG = F.FP_CONFIG


def base_row(**kw):
    r = {"F_net": -30000.0, "arb_ratio": 0.1, "opt_dir": 0, "spot_5d": 1e10, "dF_5d_pct": 50.0, "basis_pct_rank": 50.0,
         "div_season": False, "hedge_pct": 50.0, "F_dir_pct": 50.0, "spot_abs_pct": 70.0, "retail_zone": None}
    r.update(kw)
    return r


def test_label_order_arbitrage_first():
    # 套利比率 ≥ 0.5 且 基差分位 ≥ 80 → ARBITRAGE,期貨票 = 0
    o = F.judge(base_row(arb_ratio=0.6, basis_pct_rank=85, F_dir_pct=0))
    assert o["label"] == "ARBITRAGE" and o["v_fut"] == 0
    # 除權息季也算
    assert F.judge(base_row(arb_ratio=0.6, basis_pct_rank=10, div_season=True))["label"] == "ARBITRAGE"
    # 套利比率高但基差不極端、非除權息季 → 不是套利
    assert F.judge(base_row(arb_ratio=0.6, basis_pct_rank=10))["label"] == "HEDGE"


def test_hedge_bear_bull_diverge():
    assert F.judge(base_row())["label"] == "HEDGE"                                   # 空單 + 現貨買超 + 避險分位 < 80
    assert F.judge(base_row(hedge_pct=90))["label"] == "DIVERGE"                     # 避險比率太高 → 不算避險
    bear = base_row(spot_5d=-1e10, dF_5d_pct=10, opt_dir=-1)
    assert F.judge(bear)["label"] == "BEAR_CONFIRM"
    bull = base_row(F_net=5000.0, spot_5d=1e10, dF_5d_pct=90, opt_dir=2)
    assert F.judge(bull)["label"] == "BULL_CONFIRM"
    assert F.judge(base_row(spot_5d=-1e10, dF_5d_pct=10, opt_dir=0))["label"] == "DIVERGE"


def test_missing_data_no_label():
    o = F.judge(base_row(spot_5d=None))
    assert o["label"] is None and o["score"] is None and "spot_5d" in o["missing"]


def test_votes_and_score():
    # F_dir 分位 90 → round((90−50)/25)=round(1.6)=2 → clamp 1;現貨 |分位| 70 ≥ 60 → sign;選擇權 −2 → −1;散戶偏熱 → −1
    o = F.judge(base_row(F_dir_pct=90, spot_5d=-1e10, spot_abs_pct=70, opt_dir=-2, retail_zone="hot", dF_5d_pct=50))
    assert (o["v_fut"], o["v_spot"], o["v_opt"], o["v_retail"]) == (1, -1, -1, -1) and o["score"] == -2
    # JS 的 round:0.5 進位 → (62.5−50)/25 = 0.5 → 1
    assert F.judge(base_row(F_dir_pct=62.5))["v_fut"] == 1
    assert F.judge(base_row(F_dir_pct=37.5))["v_fut"] == 0                           # −0.5 → 0(JS Math.round)
    # 現貨分位 < 60 → 0
    assert F.judge(base_row(spot_abs_pct=59))["v_spot"] == 0


def test_rolling_pct_no_lookahead():
    vals = list(range(200)) + [1000]
    p = F.rolling_pct(vals)
    assert p[119] is None and p[120] is not None and p[-1] == 100.0
    # 只看前 252 天
    vals = [0.0] * 300 + [1.0]
    assert F.rolling_pct(vals)[-1] == 100.0


def _fut_day(tx, mtx, tmf, inst_mtx=0):
    d = {}
    for c, v in (("TX", tx), ("MTX", mtx), ("TMF", tmf)):
        d[(c, F.FOREIGN)] = {"net_oi": str(v)}
    d[("MTX", "投信")] = {"net_oi": str(inst_mtx)}
    return d


def test_f_net_and_synthetic_arbitrage():
    data = {"fut": {"2026-11-25": _fut_day(-100000, -4000, -2000)},
            "opt": {"2026-11-25": {"CALL": {"long_oi": "30000", "short_oi": "10000", "long_oi_k": "6000", "short_oi_k": "1"},
                                    "PUT": {"long_oi": "5000", "short_oi": "20000", "long_oi_k": "500", "short_oi_k": "1"}}},
            "oi": {("2026-11-25", "MTX"): 50000.0}, "px": {"2026-11-25": 38100.0}, "idx": {"2026-11-25": 38195.0},
            "spot": {}, "hold": []}
    r = F.build_rows(data)[0]
    assert r["F_net"] == -100000 - 1000 - 100                                       # MTX/4、TMF/20
    assert r["C_net"] == 20000 and r["P_net"] == -15000
    assert r["S_syn"] == 15000 and r["A_arb"] == 15000                               # 合成多 = min(買權淨買, 賣權淨賣)
    assert r["arb_ratio"] == pytest.approx(15000 / 101100) and r["F_dir"] == -101100 + 15000
    assert r["C_res"] == 5000 and r["P_res"] == 0 and r["opt_dir"] == 1
    assert r["avg_prem_call"] == pytest.approx(6000 * 1000 / 30000 / 50)            # 40 點
    assert r["basis"] == pytest.approx(-95.0)
    assert r["hedge_ratio"] is None and r["spot_5d"] is None


def test_hold_value_scaled_by_index():
    hold = [("2026-10-30", 1e13)]
    idx = {"2026-10-30": 30000.0, "2026-11-25": 33000.0}
    assert F.hold_on(hold, idx, "2026-11-25") == pytest.approx(1.1e13)
    assert F.hold_on(hold, idx, "2026-10-01") is None


def test_backtest_min_sample():
    rows = [{"label": "HEDGE", "ret5": 1.0, "ret20": -1.0}] * 10 + [{"label": "DIVERGE", "ret5": 1.0, "ret20": 2.0}] * 40
    bt = F.backtest(rows)
    assert bt["HEDGE"]["status"] == "樣本不足" and "winRate5" not in bt["HEDGE"]
    assert bt["DIVERGE"]["status"] == "ok" and bt["DIVERGE"]["winRate20"] == 100.0 and bt["DIVERGE"]["n"] == 40


def test_payload_never_breaks_on_missing():
    p = F.payload([{"date": "2026-11-25", "label": None, "missing": ["spot_5d"]}])
    json.dumps(p, ensure_ascii=False)
    assert p["labelZh"] == "資料缺" and p["vote_zone"] is None and any("資料缺" in c for c in p["caveats"])
    assert F.payload([])["labelZh"] == "資料缺"


def test_vote_zone_threshold():
    rows = [dict(base_row(), date="2026-11-25", label="DIVERGE", score=-2, v_fut=-1, v_spot=-1, v_opt=0, v_retail=0)]
    assert F.payload(rows)["vote_zone"] == "hot"
    rows[0]["score"] = 1
    assert F.payload(rows)["vote_zone"] is None
    rows[0]["score"] = 3
    assert F.payload(rows)["vote_zone"] == "cold"


def test_consensus_add_foreign_positioning(monkeypatch):
    import consensus as C
    m = {"indicators": [], "vote_items": [{"key": "a", "label": "A", "zone": "hot"}, {"key": "b", "label": "B", "zone": "hot"}]}
    monkeypatch.setattr(C.foreign_positioning, "compute", lambda base: ({"label": "BEAR_CONFIRM", "vote_zone": "hot"}, []))
    C.add_foreign_positioning(m, ".")
    assert m["vote_items"][-1]["key"] == "foreign_pos" and m["label"] == "偏熱" and m["hot"] == 3
    monkeypatch.setattr(C.foreign_positioning, "compute", lambda base: (_ for _ in ()).throw(RuntimeError("x")))
    m2 = {"indicators": [], "vote_items": []}
    C.add_foreign_positioning(m2, ".")
    assert "foreignPositioning" not in m2


# ------------------------------------------------------------------ fp_fetch
BFI_OK = {"stat": "OK", "fields": ["單位名稱", "買進金額", "賣出金額", "買賣差額"],
          "data": [["投信", "1", "2", "-1"], ["外資及陸資(不含外資自營商)", "457,832,326,822", "581,487,124,975",
                                              "-123,654,798,153"]]}


def test_parse_bfi_and_qfii():
    assert FF.parse_bfi(BFI_OK) == -123654798153
    assert FF.parse_bfi({"stat": "很抱歉,沒有符合條件的資料!"}) is None
    with pytest.raises(ValueError):
        FF.parse_bfi({"stat": "OK", "data": [["投信", "1", "2", "3"]]})
    q = {"stat": "OK", "fields": ["證券代號", "證券名稱", "全體外資及陸資持有股數"], "data": [["2330", "台積電", "18,000,000,000"]]}
    assert FF.parse_qfii(q) == {"2330": 18e9}
    assert FF.hold_value({"2330": 10.0, "9999": 5.0}, {"2330": 1000.0}) == (10000.0, 1)


class FakeResp:
    def __init__(self, j):
        self._j = j

    def json(self):
        return self._j


def test_fp_fetch_run_no_fill_on_missing(tmp_path):
    margin = {"twse": {"2026-10-30": {"2330": (1, 1000.0)}, "2026-11-24": {"2330": (1, 1100.0)},
                       "2026-11-25": {"2330": (1, 1200.0)}}}

    class Fx:
        def get(self, url):
            if "BFI82U" in url:
                return FakeResp(BFI_OK if "20261125" in url else {"stat": "沒有資料"})
            return FakeResp({"stat": "OK", "fields": ["證券代號", "全體外資及陸資持有股數"], "data": [["2330", "100"]]})
    now = dt.datetime(2026, 11, 25, 20, 0, tzinfo=FF.TPE)
    FF.run(Fx(), str(tmp_path), now, margin)
    spot = FF.read(str(tmp_path / "fp_data" / "foreign_spot.csv"))
    hold = FF.read(str(tmp_path / "fp_data" / "foreign_hold.csv"))
    assert list(spot) == ["2026-11-25"]                          # 沒資料的日子不寫、不補
    assert hold["2026-11-25"]["value"] == "120000" and "2026-10-30" in hold
