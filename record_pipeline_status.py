# -*- coding: utf-8 -*-
"""
============================================================
record_pipeline_status.py — 管線執行狀態記錄(2026-10-06新增)
============================================================
event-theme.yml裡每個步驟都是continue-on-error:true(單一步驟失敗不該
擋住其他獨立來源),但這代表GitHub Actions的workflow本身即使所有步驟
都失敗,只要最後的commit步驟本身沒出錯,整個run還是會顯示綠色成功——
使用者完全看不出「其實今天event_theme_radar.py失敗了,digest用的是
舊資料」這種靜默失敗。

這支腳本在所有步驟跑完後執行(workflow裡的最後一步,commit之前),做
兩件事:
  1. 把每個步驟的outcome(success/failure/cancelled/skipped)寫進
     daily_digest.json的pipeline_status欄位,供Page.html或你自己事後
     檢查用。
  2. 檢查daily_digest.json的date欄位是不是今天——如果不是(代表
     daily_digest.py本身沒跑成功,或讀到的是舊資料),印出明確錯誤
     訊息並以非0結束碼結束,讓這次workflow run在GitHub Actions介面
     上顯示失敗,不再靜默吃掉「資料其實是舊的」這個事實。

用法(見.github/workflows/event-theme.yml):
  STEP_RADAR=${{ steps.radar.outcome }} ... python record_pipeline_status.py
============================================================
"""
import json
import os
import sys
import datetime as dt

DIGEST_FILE = "daily_digest.json"

# 環境變數名稱 -> pipeline_status裡要用的key(跟workflow裡的step id對應)
STEP_ENV_MAP = {
    "STEP_RADAR": "radar",
    "STEP_NEW_THEME": "new_theme_discovery",
    "STEP_AI_THEME": "ai_theme_discovery",
    "STEP_THEME_TRACKER": "theme_tracker",
    "STEP_PRICE_STAGE": "price_stage",
    "STEP_DIGEST": "daily_digest",
    "STEP_REPORT": "daily_report",
    "STEP_LEDGER": "verification_ledger",
    "STEP_US8K": "us_8k_events",
}


def main():
    now = dt.datetime.now(dt.timezone.utc)
    today_str = now.strftime("%Y%m%d")

    step_status = {}
    for env_key, label in STEP_ENV_MAP.items():
        step_status[label] = os.environ.get(env_key, "unknown")

    try:
        with open(DIGEST_FILE, "r", encoding="utf-8") as f:
            digest = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        # digest根本沒寫出來(daily_digest.py失敗到連檔案都沒產生),
        # 這本身就是最嚴重的失敗狀態,直接印出所有步驟狀態並失敗結束。
        print(f"⚠ 讀取{DIGEST_FILE}失敗: {e}")
        print(f"[pipeline_status] {json.dumps(step_status, ensure_ascii=False)}")
        print(f"✗ {DIGEST_FILE}不存在或無法解析,daily_digest.py本次執行失敗")
        sys.exit(1)

    digest["pipeline_status"] = {
        "checked_at_utc": now.strftime("%Y-%m-%d %H:%M:%S"),
        "steps": step_status,
    }
    with open(DIGEST_FILE, "w", encoding="utf-8") as f:
        json.dump(digest, f, ensure_ascii=False, indent=2)

    digest_date = digest.get("date", "")
    print(f"[pipeline_status] 各步驟狀態: {json.dumps(step_status, ensure_ascii=False)}")
    print(f"[pipeline_status] digest.date={digest_date}  今天(UTC)={today_str}")

    failed_steps = [label for label, outcome in step_status.items()
                     if outcome not in ("success", "skipped")]
    if failed_steps:
        print(f"⚠ 以下步驟未成功: {', '.join(failed_steps)}(continue-on-error,"
              f"不會讓workflow失敗,但資料可能不完整)")

    if digest_date != today_str:
        print(f"✗ daily_digest.json的date({digest_date})跟今天(UTC {today_str})不符,"
              f"代表這次抓到/用到的是舊資料(可能是schedule觸發延遲,或daily_digest.py"
              f"本身讀到的event_theme_raw.json等來源沒有更新)。workflow本次標記失敗,"
              f"供下游(Page.html/GAS)偵測資料是否過期。")
        sys.exit(1)

    print("✓ pipeline_status已寫入,digest日期與今日相符")


if __name__ == "__main__":
    main()
