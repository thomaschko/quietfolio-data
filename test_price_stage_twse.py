# -*- coding: utf-8 -*-
"""2026-10-30:price_stage 上市改用 MI_INDEX、不混兩天價格,以及 price_history_fix_twse 的測試(不需網路)。"""
import os
import json
import tempfile

import price_stage as ps
import price_history_fix_twse as fx

MI = {"stat": "OK", "date": "20261029", "tables": [
    {"fields": ["指數", "收盤指數"], "data": [["發行量加權股價指數", "27,000"]]},
    {"fields": ["證券代號", "證券名稱", "成交股數", "成交筆數", "成交金額", "開盤價", "最高價", "最低價", "收盤價"],
     "data": [["2330", "台積電", "21,866,835", "1", "1", "1", "1", "1", "2,415.00"],
              ["0050", "元大台灣50", "1,000", "1", "1", "1", "1", "1", "190.5"],
              ["00981A", "主動統一台股增長", "2,000", "1", "1", "1", "1", "1", "15.2"],
              ["030001", "某權證", "5,000", "1", "1", "1", "1", "1", "1.2"],
              ["2344", "華邦電", "70,853,471", "1", "1", "1", "1", "1", "--"]]}]}


def test_parse_mi_index_stocks_and_etfs_only():
    d, rows = ps.parse_mi_index(MI)
    assert d == "20261029"
    assert rows == {"2330": [2415.0, 21866835.0], "0050": [190.5, 1000.0], "00981A": [15.2, 2000.0]}


def test_merge_markets_drops_stale_market():
    date, snap, market = ps.merge_markets({"TWSE": ("20261029", {"2330": [2415.0, 1]}),
                                           "TPEx": ("20261028", {"6274": [1375.0, 1]})})
    assert date == "20261029" and snap == {"2330": [2415.0, 1]} and market == {"2330": "TWSE"}
    date, snap, market = ps.merge_markets({"TWSE": ("20261029", {"2330": [1, 1]}), "TPEx": ("20261029", {"6274": [2, 2]})})
    assert set(snap) == {"2330", "6274"} and market["6274"] == "TPEx"


def test_save_snapshot_merges_same_day(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setattr(ps, "HIST_DIR", d)
        with open(os.path.join(d, "20261028.json"), "w") as f:
            json.dump({"1101": [1, 1]}, f)
        with open(os.path.join(d, "20261029.json"), "w") as f:
            json.dump({"6274": [1390.0, 10]}, f)
        ps.save_snapshot("20261029", {"2330": [2415.0, 5]})
        assert json.load(open(os.path.join(d, "20261029.json"))) == {"6274": [1390.0, 10], "2330": [2415.0, 5]}


def test_fix_file_only_twse_codes_and_matching_date():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "20261029.json")
        with open(p, "w") as f:
            json.dump({"2330": [2425.0, 1], "6274": [1390.0, 2]}, f)
        changed, n, note = fx.fix_file(p, fetch=lambda day: ("20261029", {"2330": [2415.0, 9], "9999": [1, 1]}))
        assert (changed, n, note) == (1, 1, "")
        assert json.load(open(p)) == {"2330": [2415.0, 9], "6274": [1390.0, 2]}      # 上櫃不動、不新增代號
        changed, n, note = fx.fix_file(p, fetch=lambda day: ("20261028", {"2330": [1, 1]}))
        assert changed == 0 and "不動" in note
