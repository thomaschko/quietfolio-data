# -*- coding: utf-8 -*-
"""
trading_calendar.py — 台股交易日曆(只讀 repo 內現有檔案,不連網;2026-10-26 新增)
============================================================
repo 沒有正式的交易日曆(intl_rank_core / price_stage 只排除週末,不認得國定假日),
改用「實際有開盤資料的日子」:
  price_history/YYYYMMDD.json(price_stage.py 每天的全市場收盤快照,保留約 45 天)
  price_history/backfill.json 裡出現過的日期(個股補抓的月成交資料,回溯較久)
  insti_daily/YYYYMMDD.json(GAS ChipDataPush 推的 T86/TPEx 法人買賣超,只有交易日才有)
有檔案的日子 = 交易日(確定)。
沒有檔案的平日 = 「無法確認」,不是「非交易日」:price_history 會缺天
  (例如 2026-10-23 週五當天快照就沒存到),所以不能把缺檔當成休市。
週末 = 非交易日(台股沒有週末補開盤以外的例外;補班日若真的開盤,快照會有檔案 → 仍判為交易日)。
============================================================
"""
import os
import re
import glob
import json
import datetime as dt

DAY_RE = re.compile(r"^(20\d{6})\.json$")


def _norm(d):
    """'YYYYMMDD' / 'YYYY-MM-DD' / date → 'YYYYMMDD'。"""
    if isinstance(d, dt.date):
        return d.strftime("%Y%m%d")
    s = re.sub(r"\D", "", str(d or ""))
    if len(s) != 8:
        raise ValueError(f"日期格式不對:{d!r}")
    return s


def known_trading_days(base="."):
    """→ set('YYYYMMDD'):repo 內有實際開盤資料的日子。"""
    days = set()
    for sub in ("price_history", "insti_daily"):
        for p in glob.glob(os.path.join(base, sub, "*.json")):
            m = DAY_RE.match(os.path.basename(p))
            if m:
                days.add(m.group(1))
    try:
        with open(os.path.join(base, "price_history", "backfill.json"), encoding="utf-8") as f:
            for per_code in json.load(f).values():
                days.update(k for k in per_code if re.fullmatch(r"20\d{6}", k))
    except (OSError, ValueError, AttributeError):
        pass
    return days


def check_trading_day(d, days):
    """→ 'ok'(有開盤資料)/ 'unverified'(平日但沒有資料,無法確認)。週末 → ValueError。"""
    s = _norm(d)
    if s in days:
        return "ok"
    if dt.datetime.strptime(s, "%Y%m%d").weekday() >= 5:
        raise ValueError(f"{s} 是週末,不是交易日")
    return "unverified"


def trading_days_between(after, upto, days):
    """(after, upto] 之間 repo 內確定的交易日(由舊到新)。after 不含、upto 含。"""
    a, b = _norm(after), _norm(upto)
    return sorted(x for x in days if a < x <= b)
