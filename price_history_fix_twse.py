# -*- coding: utf-8 -*-
"""
price_history_fix_twse.py — 一次性:用證交所官網 MI_INDEX(指定日期)重寫 price_history/YYYYMMDD.json 的上市價格
============================================================
2026-10-30:price_stage.py 原本的上市來源(OpenAPI STOCK_DAY_ALL)在台北 17:30 左右常常還是前一個交易日,
  10/13、10/19~21、10/27~29 等檔案的上市價格其實是前一個交易日的(上櫃是當天的)。
做法:每個檔案的日期向 MI_INDEX 要那一天的全市場行情(請求間隔 ≥ 2 秒),回來的日期等於檔名日期才改;
  只覆蓋檔案裡已經有、而且是上市的代號(上櫃的不動,也不新增代號);回來的日期不同(休市或查無)就不動並列出。
只動 price_history/2*.json;backfill.json(個股月成交,本來就帶日期)不動。
============================================================
"""
import os
import sys
import glob
import json
import time

import price_stage as ps

GAP = 2.5


def fix_file(path, fetch=ps.fetch_mi_index):
    """→ (changed, total_twse, note)"""
    day = os.path.basename(path)[:8]
    with open(path, encoding="utf-8") as f:
        snap = json.load(f)
    d, rows = fetch(day)
    if d != day:
        return 0, 0, f"MI_INDEX 回的是 {d},不動"
    changed = 0
    twse = [c for c in snap if c in rows]          # MI_INDEX 只有上市 → 上櫃代號不會被動到
    for c in twse:
        if snap[c] != rows[c]:
            snap[c] = rows[c]
            changed += 1
    with open(path, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, separators=(",", ":"))
    return changed, len(twse), ""


def main():
    files = sorted(glob.glob(os.path.join(ps.HIST_DIR, "2*.json")))
    for i, p in enumerate(files):
        if i:
            time.sleep(GAP)
        try:
            changed, n, note = fix_file(p)
            print(f"  {os.path.basename(p)}:上市 {n} 檔,改了 {changed} 檔 {note}")
        except Exception as e:
            print(f"  {os.path.basename(p)}:失敗 {e}(不動)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
