# -*- coding: utf-8 -*-
"""
coverage_business.py — My-TW-Coverage 個股「業務簡介」摘要(2026-11-12)
============================================================
用途:趨勢題材頁的個股鏈條位置(AI 草稿)只能依這裡的業務簡介判斷,避免模型憑公司名稱猜。
來源:github.com/Timeverse/My-TW-Coverage Pilot_Reports/*/{代號}_{名稱}.md(MIT);workflow 以 git 淺層 sparse clone 取得,
      只讀 markdown 文字,不執行對方任何程式。
輸出 coverage_business.json:{"asof", "source", "commit", "stocks": {代號: {"name", "industry", "biz"}}}
  biz = 「## 業務簡介」段落的敘述文字(去掉 [[連結]]、粗體與板塊/市值等欄位),最多 BIZ_MAX 字。
  同時擷取「## 供應鏈位置」的上游 / 下游小標題文字(最多 SUPPLY_MAX 字)當補充。
沒有 clone(環境變數 TW_COVERAGE_REPORTS 未設定)→ 沿用既有檔案。
"""
import datetime as dt
import glob
import json
import os
import re
import sys

OUT = "coverage_business.json"
BIZ_MAX = 160
SUPPLY_MAX = 160


def _clean(t):
    t = re.sub(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]", lambda m: m.group(2) or m.group(1), t)
    t = re.sub(r"\*\*|__|`", "", t)
    return re.sub(r"\s+", " ", t).strip()


def _section(text, title):
    m = re.search(r"(?ms)^##\s*" + re.escape(title) + r"\s*$(.*?)(?=^##\s|\Z)", text)
    return m.group(1) if m else ""


def parse_report(text):
    """→ {"industry", "biz", "supply"} 或 None"""
    sec = _section(text, "業務簡介")
    if not sec:
        return None
    ind = re.search(r"\*\*產業:\*\*\s*(.+)", sec)
    body = [ln for ln in sec.splitlines() if ln.strip() and not re.match(r"\s*\*\*[^*]+:\*\*", ln)]
    biz = _clean(" ".join(body))[:BIZ_MAX]
    sup = _section(text, "供應鏈位置")
    sup_lines = [_clean(ln.lstrip("-* ")) for ln in sup.splitlines() if ln.strip()]
    out = {"industry": _clean(ind.group(1)) if ind else "", "biz": biz}
    if sup_lines:
        out["supply"] = "; ".join(sup_lines)[:SUPPLY_MAX]
    return out if biz else None


def build(reports_dir, sha=None, today=None):
    stocks = {}
    for f in sorted(glob.glob(os.path.join(reports_dir, "*", "*.md"))):
        m = re.match(r"(\d{4,6}[A-Z]?)_(.+)\.md$", os.path.basename(f))
        if not m:
            continue
        with open(f, encoding="utf-8", errors="replace") as fh:
            r = parse_report(fh.read())
        if r:
            stocks[m.group(1)] = dict(name=m.group(2), **r)
    return {"asof": (today or dt.date.today()).isoformat(), "source": "github.com/Timeverse/My-TW-Coverage Pilot_Reports(MIT)",
            "commit": sha, "stocks": stocks}


def main():
    d = os.environ.get("TW_COVERAGE_REPORTS")
    if not d or not os.path.isdir(d):
        print(json.dumps({"status": "skip(no clone)", "kept": os.path.exists(OUT)}, ensure_ascii=False))
        return 0
    out = build(d, os.environ.get("TW_COVERAGE_SHA"))
    if len(out["stocks"]) < 500:                     # 格式改了 → 不覆蓋舊檔
        print(json.dumps({"status": "too_few", "n": len(out["stocks"])}, ensure_ascii=False))
        return 0
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, OUT)
    print(json.dumps({"status": "ok", "n": len(out["stocks"])}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
