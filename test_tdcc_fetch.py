# -*- coding: utf-8 -*-
import os
import gzip
import json
import datetime as dt

import pytest

import tdcc_fetch as tf
import trading_calendar as tc

HEADER = "﻿資料日期,證券代號,持股分級,人數,股數,占集保庫存數比例%\n"


def csv_text(date="20261022", code="2337", adj=(2, 10000), drop_col=False):
    """15 級各 1 人 / 100 股;第 16 級 adj;第 17 級合計 = Σ1–15 − 第16級股數(與 Phase 0 的 2337 同型)。"""
    rows = [(i, 1, 100) for i in range(1, 16)]
    rows.append((16, adj[0], adj[1]))
    rows.append((17, 15, 1500 - adj[1]))
    head = HEADER if not drop_col else HEADER.replace(",占集保庫存數比例%", "")
    body = "".join(f"{date},{code},{lv},{h},{s}" + ("" if drop_col else ",1.00") + "\n" for lv, h, s in rows)
    return head + body


class Resp:
    def __init__(self, status=200, body=b"", text=""):
        self.status_code, self.content, self.text = status, body, text


class FakeHttp:
    def __init__(self, csv_body, robots=404, csv_status=200):
        self.csv_body, self.robots, self.csv_status, self.urls = csv_body, robots, csv_status, []

    def get(self, url):
        self.urls.append(url)
        if url.endswith("/robots.txt"):
            if isinstance(self.robots, str):
                return Resp(200, text=self.robots)
            return Resp(self.robots)
        return Resp(self.csv_status, self.csv_body.encode("utf-8"))


NOW = dt.datetime(2026, 10, 24, 11, 0, tzinfo=tf.TPE)


def base_with_days(tmp_path, days):
    os.makedirs(tmp_path / "price_history", exist_ok=True)
    for d in days:
        (tmp_path / "price_history" / f"{d}.json").write_text("{}")
    return str(tmp_path)


def test_parse_ok_and_level16_sum_rule():
    d, rows = tf.parse(csv_text())
    assert d == "2026-10-22" and len(rows) == 17
    chk = tf.sum_check(rows)
    assert chk == {"holders_mismatch": [], "shares_mismatch": [], "no_total": []}


def test_sum_check_flags_mismatch_without_fixing():
    text = csv_text().replace("20261022,2337,17,15,", "20261022,2337,17,14,")
    _, rows = tf.parse(text)
    chk = tf.sum_check(rows)
    assert chk["holders_mismatch"] == ["2337"]
    assert [r["holders"] for r in rows if r["level"] == 17] == [14]      # 原值不動


def test_missing_column_raises():
    with pytest.raises(tf.TdccError, match="缺欄位"):
        tf.parse(csv_text(drop_col=True))


def test_multiple_dates_raise():
    text = csv_text() + csv_text(date="20261015", code="2408").split("\n", 1)[1]
    with pytest.raises(tf.TdccError, match="不只一個"):
        tf.parse(text)


def test_bad_level_raises():
    with pytest.raises(tf.TdccError, match="1–17"):
        tf.parse(HEADER + "20261022,2337,18,1,1,1.0\n")


def test_trading_day_check(tmp_path):
    days = tc.known_trading_days(base_with_days(tmp_path, ["20261022"]))
    assert tc.check_trading_day("2026-10-22", days) == "ok"
    assert tc.check_trading_day("20261023", days) == "unverified"     # 平日沒資料 ≠ 休市
    with pytest.raises(ValueError, match="週末"):
        tc.check_trading_day("20261024", days)


def test_calendar_reads_backfill_and_insti(tmp_path):
    base = base_with_days(tmp_path, [])
    (tmp_path / "price_history" / "backfill.json").write_text(json.dumps({"2337": {"20260901": [1, 2]}}))
    os.makedirs(tmp_path / "insti_daily")
    (tmp_path / "insti_daily" / "20261023.json").write_text("{}")
    days = tc.known_trading_days(base)
    assert days == {"20260901", "20261023"}
    assert tc.trading_days_between("20260901", "20261023", days) == ["20261023"]


def test_run_saves_then_dedupes_by_date(tmp_path):
    base = base_with_days(tmp_path, ["20261022"])
    out = str(tmp_path / "tdcc_weekly")
    body = csv_text()
    st, d = tf.run(http=FakeHttp(body), now=NOW, out_dir=out, base=base, sleep=lambda s: None)
    assert (st, d) == ("saved", "2026-10-22")
    with gzip.open(os.path.join(out, "2026-10-22.csv.gz")) as f:
        assert f.read().decode("utf-8") == body                        # 原檔原樣保存
    idx = json.load(open(os.path.join(out, "index.json"), encoding="utf-8"))
    assert idx["latest"] == "2026-10-22" and idx["weeks"]["2026-10-22"]["trading_day"] == "ok"
    first = os.path.getmtime(os.path.join(out, "2026-10-22.csv.gz"))

    st2, _ = tf.run(http=FakeHttp(body.replace(",1.00", ",9.99")), now=NOW, out_dir=out, base=base,
                    sleep=lambda s: None)
    assert st2 == "skipped_same_date"
    assert os.path.getmtime(os.path.join(out, "2026-10-22.csv.gz")) == first   # 不覆寫
    log = json.load(open(os.path.join(out, "fetch_log.json"), encoding="utf-8"))
    assert [x["status"] for x in log] == ["saved", "skipped_same_date"]


def test_run_weekend_date_not_saved(tmp_path):
    out = str(tmp_path / "tdcc_weekly")
    st, _ = tf.run(http=FakeHttp(csv_text(date="20261024")), now=NOW, out_dir=out,
                   base=base_with_days(tmp_path, []), sleep=lambda s: None)
    assert st == "invalid"
    assert not os.path.exists(os.path.join(out, "2026-10-24.csv.gz"))


def test_run_respects_robots_and_block(tmp_path):
    out = str(tmp_path / "tdcc_weekly")
    http = FakeHttp(csv_text(), robots="User-agent: *\nDisallow: /\n")
    assert tf.run(http=http, now=NOW, out_dir=out, base=str(tmp_path), sleep=lambda s: None)[0] == "blocked"
    assert len(http.urls) == 1                                          # 沒去抓 CSV
    http = FakeHttp(csv_text(), csv_status=403)
    assert tf.run(http=http, now=NOW, out_dir=out, base=str(tmp_path), sleep=lambda s: None)[0] == "blocked"
    assert len(http.urls) == 2                                          # 403 不重試
