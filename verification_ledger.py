# -*- coding: utf-8 -*-
"""
============================================================
verification_ledger.py — 驗證中心(2026-09-21新增)
============================================================
發想來源:compassingai.com的PhotonCap頁面「驗證中心」——每句公開判斷
都記一筆台帳,附裁決日期,到期用硬資料(13F/股價)自動判✅/❌/⏳,
不拿待驗判斷湊戰績。

Quietfolio沒有SEC那種細緻公開資料,所以簡化成最小可行版本:
  1. A區(多源共振)、D區(熱度暴增題材)每次標記出新股票,自動記一筆claim,
     附上標記當天收盤價、detection_type(跨來源共振/題材群聚)、裁決日期
     (標記後30天)。
  2. 每天先檢查有沒有到期的舊claim,到期就查當天收盤價,算報酬率,
     判定✅印證(>=+10%)/❌反駁(<+10%)/⏳審判中(還沒到期)。
  3. 同一檔股票+同一題材,7天內不重複記claim(避免題材持續發酵時,
     每天都開一筆新的,造成台帳灌水)。
  4. 股價來源:TWSE openapi的STOCK_DAY_ALL(上市)+ TPEx openapi的
     tpex_mainboard_daily_close_quotes(上櫃),各一次API呼叫拿到當天
     全市場收盤價,新股票標記、舊股票裁決都從同一份資料查,不用
     每檔股票各打一次API。

⚠️ G區(雙邊確認)目前只有題材層級的資料,沒有個別股票代碼清單,
   這版暫時不處理,只處理A區、D區。
============================================================
"""
import json
import re
import datetime as dt
import requests

UA = {"User-Agent": "Mozilla/5.0"}
LEDGER_FILE = "verification_ledger.json"
DIGEST_FILE = "daily_digest.json"

JUDGE_DAYS = 30          # 標記後N天做裁決
DEDUPE_DAYS = 7          # 同一檔股票+同一題材,N天內不重複記claim
WIN_THRESHOLD_PCT = 10.0  # 報酬率>=此門檻判✅印證,否則❌反駁
# 2026-11-13 審查 Q1:原始報酬受大盤影響很大(9/22–10/15 標記的 568 筆遇上急跌,各類平均 −16% 以上),
#   另記「加權指數同期報酬」與「超額報酬」,並依標記日分群統計;印證/反駁的判定規則不變。
#   指數用 theme_page 已快取的 FinMind TAIEX(不新增來源);快取還沒涵蓋裁決日時先留空,之後的執行再補。
TAIEX_CACHE = "theme_page/cache/_TAIEX.json"


def _row_date(row):
    """openapi 列的資料日期 → YYYYMMDD(民國 1151113 / 115/11/13 / 西元 20261113 都接受);沒有就 None"""
    v = str(row.get("Date") or row.get("date") or "").replace("/", "").replace("-", "").strip()
    if re.fullmatch(r"\d{7}", v):                       # 民國年 3 碼
        return "%04d%s" % (int(v[:3]) + 1911, v[3:])
    if re.fullmatch(r"\d{8}", v):
        return v
    return None


PRICE_DATES = {}   # {code: 收盤價的資料日期 YYYYMMDD};fetch_all_close_prices 填入


def fetch_all_close_prices():
    """回傳 {code: close_price(float)},上市+上櫃合併,單一失敗不擋另一邊。
    2026-11-13:同時把每檔收盤價的資料日期記在 PRICE_DATES(證交所 STOCK_DAY_ALL 曾落後一天)。"""
    prices = {}
    PRICE_DATES.clear()
    try:
        r = requests.get("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL",
                          headers=UA, timeout=20)
        r.raise_for_status()
        rows = r.json()
        for row in rows:
            code = str(row.get("Code", "")).strip()
            close_str = str(row.get("ClosingPrice", "")).replace(",", "").strip()
            if code and close_str:
                try:
                    prices[code] = float(close_str)
                    PRICE_DATES[code] = _row_date(row)
                except ValueError:
                    pass
        print(f"  TWSE收盤價: {len(prices)} 檔")
    except Exception as e:
        print(f"  ⚠ TWSE收盤價抓取失敗: {e}")

    try:
        before = len(prices)
        r = requests.get("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes",
                          headers=UA, timeout=20, verify=False)
        r.raise_for_status()
        rows = r.json()
        for row in rows:
            code = str(row.get("SecuritiesCompanyCode", "")).strip()
            close_str = str(row.get("Close", "")).replace(",", "").strip()
            if code and close_str and re.match(r"^\d{4,6}$", code):
                try:
                    prices[code] = float(close_str)
                    PRICE_DATES[code] = _row_date(row)
                except ValueError:
                    pass
        print(f"  TPEx收盤價: +{len(prices)-before} 檔")
    except Exception as e:
        print(f"  ⚠ TPEx收盤價抓取失敗: {e}")

    return prices


def load_ledger():
    try:
        with open(LEDGER_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_ledger(ledger):
    with open(LEDGER_FILE, "w", encoding="utf-8") as f:
        json.dump(ledger, f, ensure_ascii=False, indent=2)


def judge_pending_claims(ledger, prices, today_str):
    """檢查所有審判中的claim,到期的用今天收盤價裁決。"""
    judged_count = 0
    for claim in ledger:
        if claim["status"] != "pending":
            continue
        if today_str < claim["judge_date"]:
            continue  # 還沒到期
        code = claim["code"]
        judged_price = prices.get(code)
        if judged_price is None:
            # 今天查無這檔股價(可能停牌/下市),暫時跳過,下次再試
            continue
        flagged_price = claim["flagged_price"]
        if flagged_price <= 0:
            claim["status"] = "invalid"
            claim["note"] = "標記當天股價異常,無法計算報酬率"
            continue
        return_pct = round((judged_price - flagged_price) / flagged_price * 100, 2)
        claim["judged_date"] = today_str
        claim["judged_price"] = judged_price
        claim["judged_price_date"] = PRICE_DATES.get(code)
        claim["return_pct"] = return_pct
        claim["status"] = "confirmed" if return_pct >= WIN_THRESHOLD_PCT else "refuted"
        judged_count += 1
    return judged_count


def already_logged_recently(ledger, code, theme, today, dedupe_days):
    """同一檔股票+同一題材,DEDUPE_DAYS天內是否已經記過claim。"""
    cutoff = (today - dt.timedelta(days=dedupe_days)).strftime("%Y%m%d")
    for claim in ledger:
        if claim["code"] == code and claim["theme"] == theme and claim["flagged_date"] >= cutoff:
            return True
    return False


def log_new_claims(ledger, digest, prices, today, today_str):
    """從daily_digest.json的A區、D區抓出新標記的股票,記claim。"""
    judge_date = (today + dt.timedelta(days=JUDGE_DAYS)).strftime("%Y%m%d")
    new_count = 0

    # A區:多源共振(跨來源類型共振 / 題材群聚共識)
    for r in digest.get("A_multi_source_resonance", []):
        code = r.get("code")
        if not code:
            continue
        theme = "、".join(r.get("themes", [])[:3]) or "多源共振"
        if already_logged_recently(ledger, code, theme, today, DEDUPE_DAYS):
            continue
        price = prices.get(code)
        if price is None:
            continue
        detection_type = ("dual" if (r.get("via_source_diversity") and r.get("via_theme_cluster"))
                           else "theme_cluster" if r.get("via_theme_cluster")
                           else "source_diversity")
        ledger.append({
            "code": code, "name": r.get("name", ""), "theme": theme,
            "region": "A", "detection_type": detection_type,
            "flagged_date": today_str, "flagged_price": price, "flagged_price_date": PRICE_DATES.get(code),
            "judge_date": judge_date, "status": "pending",
        })
        new_count += 1

    # D區:熱度暴增題材(用stock_positions或codes+names)
    for t in digest.get("D_surge_themes", []):
        theme = t.get("theme", "")
        codes = t.get("codes", [])
        names = t.get("names", {})
        for code in codes[:5]:  # 每個題材只記前5檔,避免長尾雜訊灌爆台帳
            if already_logged_recently(ledger, code, theme, today, DEDUPE_DAYS):
                continue
            price = prices.get(code)
            if price is None:
                continue
            ledger.append({
                "code": code, "name": names.get(code, ""), "theme": theme,
                "region": "D", "detection_type": "keyword_surge",
                "flagged_date": today_str, "flagged_price": price, "flagged_price_date": PRICE_DATES.get(code),
                "judge_date": judge_date, "status": "pending",
            })
            new_count += 1

    return new_count


def load_index(path=TAIEX_CACHE):
    """→ [(YYYYMMDD, close)] 依日期排序;讀不到 → []"""
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return []
    rows = d.get("px") if isinstance(d, dict) else d
    out = []
    for r in rows or []:
        try:
            out.append((str(r[0]).replace("-", ""), float(r[1])))
        except (TypeError, ValueError, IndexError):
            continue
    return sorted(out)


def _idx_on_or_before(idx, d8):
    best = None
    for d, v in idx:
        if d <= d8:
            best = v
        else:
            break
    return best


def add_index_returns(ledger, idx):
    """已裁決、還沒有指數報酬的 claim → 補 idx_return_pct / excess_pct(百分點)。
    起點用 flagged_price_date(沒有就用 flagged_date),終點用 judged_price_date(沒有就用 judged_date);
    指數快取最新日期還沒到終點 → 先不填。回傳補了幾筆。"""
    if not idx:
        return 0
    last = idx[-1][0]
    n = 0
    for c in ledger:
        if c.get("status") not in ("confirmed", "refuted") or c.get("idx_return_pct") is not None or c.get("return_pct") is None:
            continue
        a = c.get("flagged_price_date") or c.get("flagged_date")
        b = c.get("judged_price_date") or c.get("judged_date")
        if not a or not b or b > last:
            continue
        i0, i1 = _idx_on_or_before(idx, a), _idx_on_or_before(idx, b)
        if not i0 or not i1:
            continue
        c["idx_return_pct"] = round((i1 / i0 - 1) * 100, 2)
        c["excess_pct"] = round(c["return_pct"] - c["idx_return_pct"], 2)
        n += 1
    return n


def _median(xs):
    xs = sorted(xs)
    if not xs:
        return None
    m = len(xs) // 2
    return xs[m] if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2


def build_summary(ledger):
    """按detection_type分開統計命中率,只用已裁決的claim計算,審判中不計入。"""
    by_type = {}
    for claim in ledger:
        if claim["status"] not in ("confirmed", "refuted"):
            continue
        dt_key = claim["detection_type"]
        by_type.setdefault(dt_key, {"confirmed": 0, "refuted": 0})
        by_type[dt_key][claim["status"]] += 1

    summary = {}
    for k, v in by_type.items():
        total = v["confirmed"] + v["refuted"]
        judged = [c for c in ledger if c["status"] in ("confirmed", "refuted") and c["detection_type"] == k]
        ex = [c for c in judged if c.get("excess_pct") is not None]
        by_day = {}
        for c in ex:
            by_day.setdefault(c["flagged_date"], []).append(c["excess_pct"])
        day_means = [sum(x) / len(x) for x in by_day.values()]
        summary[k] = {
            "confirmed": v["confirmed"], "refuted": v["refuted"], "total_judged": total,
            "win_rate_pct": round(v["confirmed"] / total * 100, 1) if total else None,
            # 2026-11-13 審查 Q1:標記日數、超額報酬(相對加權指數)中位數與「依標記日等權」平均
            "n_dates": len({c["flagged_date"] for c in judged}),
            "n_excess": len(ex),
            "raw_median_pct": round(_median([c["return_pct"] for c in judged]), 2) if judged else None,
            "excess_median_pct": round(_median([c["excess_pct"] for c in ex]), 2) if ex else None,
            "excess_date_mean_pct": round(sum(day_means) / len(day_means), 2) if day_means else None,
        }
    pending_count = sum(1 for c in ledger if c["status"] == "pending")
    return {"by_detection_type": summary, "pending_count": pending_count,
            "total_claims": len(ledger)}


def main():
    print("=" * 50)
    print("驗證中心 開始")
    now = dt.datetime.now()
    today_str = now.strftime("%Y%m%d")

    print("[前置] 抓取全市場收盤價(供標記新claim+裁決舊claim共用)")
    prices = fetch_all_close_prices()
    if not prices:
        print("  ⚠ 股價抓取完全失敗,本次跳過(不影響其他既有claim)")
        return

    ledger = load_ledger()
    print(f"  現有台帳: {len(ledger)} 筆")

    judged = judge_pending_claims(ledger, prices, today_str)
    print(f"[裁決] 本次到期並裁決: {judged} 筆")
    filled = add_index_returns(ledger, load_index())
    print(f"[超額報酬] 補上加權指數同期報酬: {filled} 筆")

    try:
        with open(DIGEST_FILE, "r", encoding="utf-8") as f:
            digest = json.load(f)
    except Exception as e:
        print(f"  ⚠ 讀取{DIGEST_FILE}失敗,本次不記新claim: {e}")
        digest = {}

    new_count = log_new_claims(ledger, digest, prices, now, today_str)
    print(f"[新增] 本次新記claim: {new_count} 筆")

    save_ledger(ledger)

    summary = build_summary(ledger)
    print(f"\n[統計] 累計台帳{summary['total_claims']}筆,審判中{summary['pending_count']}筆")
    for dt_key, s in summary["by_detection_type"].items():
        label = {"dual": "雙重確認", "theme_cluster": "題材群聚",
                  "source_diversity": "跨來源共振", "keyword_surge": "關鍵字暴增"}.get(dt_key, dt_key)
        if s["win_rate_pct"] is not None:
            print(f"  {label}: 已裁決{s['total_judged']}筆,勝率{s['win_rate_pct']}% "
                  f"(✅{s['confirmed']} ❌{s['refuted']})")
        else:
            print(f"  {label}: 尚無已裁決案例")

    # 寫回daily_digest.json,供Page.html之後顯示用
    try:
        digest["H_verification_summary"] = summary
        with open(DIGEST_FILE, "w", encoding="utf-8") as f:
            json.dump(digest, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"  ⚠ 寫回{DIGEST_FILE}失敗(不影響台帳本身): {e}")

    print("=" * 50)


if __name__ == "__main__":
    main()
