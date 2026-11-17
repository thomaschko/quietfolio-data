# -*- coding: utf-8 -*-
"""
av_spcx_probe.py — SPCX(SpaceX)在 Alpha Vantage 是否有法說逐字稿(2026-11-17,一次性,只讀)v2026-11-17-space2
============================================================
規格:使用者「太空算力競品三條線併入雷達」任務 2(比照 2026-09-22 驗證 LWLG/POET 的做法)。
- 只打 1 次 EARNINGS_CALL_TRANSCRIPT(季別用 src6 自己的 get_recent_quarters 第一季,即 src6 下週一會抓的那季)。
- 週一(src6 執行日,約用 23/25 次)不跑;今天 us_transcripts 最多 20 次,+1 仍在 25 次內。
- 逐字稿內容不印、不存:只印段落數、字元數、src6「太空算力」詞組命中次數。金鑰只讀環境變數。
"""
import datetime as dt
import os
import sys

import requests

import earnings_keywords as E


def main():
    key = os.environ.get("ALPHAVANTAGE_KEY", "")
    if not key:
        print("✗ 沒有 ALPHAVANTAGE_KEY,不呼叫")
        return 0
    if dt.datetime.utcnow().weekday() == 0:
        print("skipped_quota_reserved:週一是 src6 執行日")
        return 0
    q = E.get_recent_quarters(1)[0]
    r = requests.get(E.AV_URL, params={"function": "EARNINGS_CALL_TRANSCRIPT", "symbol": "SPCX", "quarter": q, "apikey": key},
                     timeout=30)
    print("SPCX", q, "http", r.status_code)
    try:
        d = r.json()
    except ValueError:
        print("非 JSON 回應")
        return 0
    segs = d.get("transcript") or []
    note = d.get("Note") or d.get("Information") or d.get("error") or d.get("Error Message")
    if note:
        print("AV 訊息:", str(note)[:120])
    text = " ".join(s.get("content", "") for s in segs if isinstance(s, dict))
    print("段落數", len(segs), "字元數", len(text), "回應欄位", sorted(d.keys())[:8])
    if text:
        hits = {kw: v["count"] for kw, v in E.count_keywords(text).items()}
        print("全部命中詞數", len(hits))
        print("太空算力詞組命中", {k: hits.get(k, 0) for k in E.KEYWORDS["太空算力"]})
    return 0


if __name__ == "__main__":
    sys.exit(main())
