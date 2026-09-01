# -*- coding: utf-8 -*-
"""
============================================================
event_theme_radar.py — 事件/題材交易雷達 (GitHub Actions 端)
============================================================
兩個偵測源(v2:砍掉純高頻詞自動偵測,那產出全是泛詞雜訊):
  1. 固定關鍵字熱度暴增:讀 themes_watchlist.txt,用鉅亨 search API
     算「近3日日均 vs 20日基線日均」的暴增比,>=門檻即題材發酵
  2. MOPS 重訊硬事件:改用 TWSE/TPEx OpenAPI 的 keyless JSON gateway
     (t187ap04_L / t187ap04_O),過濾接單/擴產/簽約等硬事件關鍵字

輸出: event_theme_raw.json → 推到 quietfolio-data repo,供 GAS 端讀取
依賴: requests (jieba 已不需要,但留著也無妨)

資料源(全部免費、免金鑰):
  鉅亨關鍵字搜  https://api.cnyes.com/media/api/v1/search/news?q={kw}&page={n}
  TWSE 股票清單 https://openapi.twse.com.tw/v1/opendata/t187ap03_L
  TPEx 股票清單 https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O
  TWSE 重大訊息 https://openapi.twse.com.tw/v1/opendata/t187ap04_L
  TPEx 重大訊息 https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap04_O
============================================================
"""

import requests
import re
import json
import time
import datetime as dt

# ============================================================
# 設定
# ============================================================
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
CNYES_BASE = "https://api.cnyes.com/media/api/v1"
OUT_FILE = "event_theme_raw.json"
WATCHLIST_FILE = "themes_watchlist.txt"

RECENT_DAYS = 3
BASELINE_DAYS = 20
SURGE_RATIO = 1.5          # 放寬:近期日均 >= 基線日均的1.5倍即算暴增
MIN_RECENT_COUNT = 3       # 且近3日至少要有這麼多則,避免小基數假訊號

# MOPS 硬事件關鍵字(出現在重訊主旨中才算)
MOPS_EVENT_KEYWORDS = [
    "接獲訂單", "取得訂單", "承接", "接單", "擴產", "擴充產能", "新增產能",
    "產能", "合作", "簽約", "簽署", "策略聯盟", "技術授權", "授權",
    "調升", "調漲", "漲價", "投資", "併購", "收購", "取得",
    "認證", "通過", "量產", "出貨", "開發成功", "訂單", "增資",
    "私募", "處分", "重大", "得標", "標案",
]
# 排除純例行公告(這些主旨含上面關鍵字但無交易意義)
MOPS_EXCLUDE = [
    "董事會決議", "股利", "股東會", "更正", "澄清", "本公司代",
    "代子公司", "財務報告", "現金股利", "召開", "受益人",
]


# ============================================================
# 股號↔股名對照表
# ============================================================
def build_name2code():
    name2code = {}
    sources = [
        ("https://openapi.twse.com.tw/v1/opendata/t187ap03_L", "TWSE"),
        ("https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O", "TPEx"),
    ]
    for url, tag in sources:
        try:
            r = requests.get(url, headers=UA, timeout=20)
            r.raise_for_status()
            data = r.json()
            before = len(name2code)
            for row in data:
                code = str(row.get("公司代號", "")).strip()
                short = str(row.get("公司簡稱", "")).strip()
                if not (code and short and re.match(r"^\d{4,6}$", code)):
                    continue
                name2code[short] = code
                alias = short.replace("-KY", "").replace("＊", "").replace("*", "").strip()
                if alias and alias != short and alias not in name2code:
                    name2code[alias] = code
            print(f"  {tag} 股票清單: +{len(name2code)-before} → 累計 {len(name2code)}")
        except Exception as e:
            print(f"  ⚠ {tag} 股票清單抓取失敗: {e}")
    return name2code


def extract_codes(text, name2code):
    if not text:
        return set()
    codes = set()
    for m in re.findall(r"[（(](\d{4})[)）]", text):
        codes.add(m)
    for nm, cd in name2code.items():
        if len(nm) >= 2 and nm in text:
            codes.add(cd)
    return codes


# ============================================================
# 鉅亨關鍵字搜尋
# ============================================================
def cnyes_search(keyword, start_ts, max_pages=10):
    """回傳 keyword 近期新聞 list(只取 publishAt >= start_ts)。"""
    out = []
    for page in range(1, max_pages + 1):
        url = f"{CNYES_BASE}/search/news?q={requests.utils.quote(keyword)}&page={page}"
        try:
            r = requests.get(url, headers=UA, timeout=20)
            if r.status_code != 200:
                break
            items = (r.json().get("items") or {}).get("data") or []
            if not items:
                break
            stop = False
            for it in items:
                pub = it.get("publishAt") or 0
                if pub < start_ts:
                    stop = True
                    continue
                out.append({
                    "publishAt": pub,
                    "title": it.get("title", ""),
                    "summary": it.get("summary", "") or it.get("content", ""),
                })
            if stop:
                break
            last_page = (r.json().get("items") or {}).get("last_page", 1)
            if page >= last_page:
                break
            time.sleep(0.3)
        except Exception as e:
            print(f"  ⚠ 搜尋 {keyword} 第{page}頁失敗: {e}")
            break
    return out


# ============================================================
# 偵測源 1: 固定關鍵字熱度暴增
# ============================================================
def detect_fixed_keywords(name2code, now_ts):
    print("[偵測源1] 固定關鍵字熱度追蹤")
    try:
        with open(WATCHLIST_FILE, "r", encoding="utf-8") as f:
            keywords = [ln.strip() for ln in f
                        if ln.strip() and not ln.startswith("#")]
    except FileNotFoundError:
        print(f"  找不到 {WATCHLIST_FILE}")
        return []

    recent_start = now_ts - RECENT_DAYS * 86400
    base_start = now_ts - BASELINE_DAYS * 86400
    results = []

    for kw in keywords:
        news = cnyes_search(kw, base_start)   # 一次抓20日內全部
        recent = [n for n in news if n["publishAt"] >= recent_start]
        base = news   # 20日內全部(含近3日)
        recent_daily = len(recent) / RECENT_DAYS
        base_daily = len(base) / BASELINE_DAYS if base else 0.01
        # 用「近期 vs 前段(20日扣掉近3日)」比,基期較純
        earlier = [n for n in news if n["publishAt"] < recent_start]
        earlier_daily = len(earlier) / (BASELINE_DAYS - RECENT_DAYS) if earlier else 0.01
        ratio = round(recent_daily / earlier_daily, 2) if earlier_daily else 999

        # 小基數假訊號防護:前段幾乎沒新聞(日均<0.15,約20日內<3則)時,
        # 近期冒幾則就會爆表,這種不算真暴增,需前段有基礎量才採計
        enough_base = earlier_daily >= 0.15
        surge = (ratio >= SURGE_RATIO and len(recent) >= MIN_RECENT_COUNT
                 and enough_base)
        flag = "🔥暴增" if surge else ("(基數過小略過)" if ratio >= SURGE_RATIO
                                       and len(recent) >= MIN_RECENT_COUNT else "")
        print(f"  {kw}: 近{RECENT_DAYS}日={len(recent)} 前段日均={round(earlier_daily,2)} "
              f"暴增比={ratio} {flag}")

        if surge:
            codes = set()
            for n in recent:
                codes |= extract_codes(n["title"] + " " + n["summary"], name2code)
            results.append({
                "theme": kw,
                "ratio": ratio,
                "recent_count": len(recent),
                "codes": sorted(codes),
                "source": "關鍵字暴增",
            })
        time.sleep(0.3)
    return results


# ============================================================
# 偵測源 2: MOPS 重訊(改用 OpenAPI keyless JSON)
# ============================================================
def _mops_index(fields_sample):
    """依欄位名動態定位 代號/名稱/主旨 欄。
    上市用中文欄名(公司代號/主旨),上櫃用英文欄名(SecuritiesCompanyCode/CompanyName),
    兩者都要涵蓋。"""
    idx = {"code": None, "name": None, "subject": None, "date": None}
    for k in fields_sample:
        kk = str(k).replace(" ", "")
        kl = kk.lower()
        # 股號:中文「公司代號」或英文 SecuritiesCompanyCode / CompanyCode / Code
        if idx["code"] is None and ("公司代號" in kk or "代號" in kk
                or "securitiescompanycode" in kl or "companycode" in kl
                or kl == "code"):
            idx["code"] = k
        # 公司名:中文或英文 CompanyName
        if idx["name"] is None and ("公司名稱" in kk or "公司簡稱" in kk or "名稱" in kk
                or "companyname" in kl):
            idx["name"] = k
        # 主旨
        if idx["subject"] is None and ("主旨" in kk or "說明" in kk or "標題" in kk
                or "subject" in kl or "title" in kl):
            idx["subject"] = k
        # 日期
        if idx["date"] is None and ("發言日期" in kk or "日期" in kk or kl == "date"):
            idx["date"] = k
    return idx


def detect_mops_events(name2code, today):
    print("[偵測源2] MOPS 重大訊息事件(OpenAPI JSON)")
    sources = [
        ("https://openapi.twse.com.tw/v1/opendata/t187ap04_L", "上市"),
        ("https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap04_O", "上櫃"),
    ]
    today_roc = f"{today.year - 1911:03d}{today.month:02d}{today.day:02d}"  # 1150828
    results = []

    for url, market in sources:
        try:
            r = requests.get(url, headers=UA, timeout=25)
            if r.status_code != 200:
                print(f"  ⚠ MOPS {market} HTTP {r.status_code}")
                continue
            data = r.json()
            if not data:
                print(f"  MOPS {market}: 回傳空")
                continue

            idx = _mops_index(data[0].keys())
            if not idx["code"] or not idx["subject"]:
                print(f"  ⚠ MOPS {market} 欄位定位失敗,實際欄位: {list(data[0].keys())}")
                continue

            hit_cnt = 0
            for row in data:
                code = str(row.get(idx["code"], "")).strip()
                subject = str(row.get(idx["subject"], "")).strip()
                if not re.match(r"^\d{4,6}$", code) or not subject:
                    continue
                subj_clean = re.sub(r"\s+", "", subject)

                # 只留當日(若有日期欄);OpenAPI 通常就是最新一批,無日期欄則全收
                if idx["date"]:
                    d = str(row.get(idx["date"], "")).strip()
                    if d and today_roc not in d and d not in today_roc:
                        # 日期不符當日就跳過(容忍格式差異)
                        pass  # OpenAPI多為最新快照,放寬不強制過濾

                # 排除例行公告
                if any(ex in subj_clean for ex in MOPS_EXCLUDE):
                    continue
                # 命中硬事件關鍵字
                hit = [kw for kw in MOPS_EVENT_KEYWORDS if kw in subj_clean]
                if hit:
                    results.append({
                        "theme": "重訊:" + "/".join(hit[:2]),
                        "ratio": None,
                        "recent_count": 1,
                        "codes": [code],
                        "subject": subj_clean[:80],
                        "source": "MOPS重訊",
                    })
                    hit_cnt += 1
            print(f"  MOPS {market}: 命中 {hit_cnt} 筆硬事件 (共{len(data)}則公告)")
        except Exception as e:
            print(f"  ⚠ MOPS {market} 失敗: {e}")
    return results


# ============================================================
# 主流程
# ============================================================
def main():
    print("=" * 50)
    print("EventThemeRadar 開始 (v2: 關鍵字暴增 + MOPS重訊)")
    now = dt.datetime.now()
    now_ts = int(now.timestamp())

    print("[前置] 建立股號↔股名對照表")
    name2code = build_name2code()
    print(f"  對照表共 {len(name2code)} 個名稱")

    src1 = detect_fixed_keywords(name2code, now_ts)
    src2 = detect_mops_events(name2code, now)

    # 偵測源3:維基題材關注度(發酵前緣)。獨立檔,抓不到不影響前兩源。
    try:
        from wiki_detector import detect_wiki_attention
        src3 = detect_wiki_attention()
    except Exception as e:
        print(f"  ⚠ 維基偵測源3失敗(不影響其他源): {e}")
        src3 = []

    all_themes = src1 + src2 + src3
    code_hits = {}
    for t in all_themes:
        for cd in t["codes"]:
            entry = code_hits.setdefault(cd, {"themes": set(), "sources": set(),
                                              "subjects": [], "wiki_stages": set()})
            entry["themes"].add(t["theme"])
            entry["sources"].add(t["source"])
            if t.get("subject"):
                entry["subjects"].append(t["subject"])
            if t.get("wiki_stage"):
                entry["wiki_stages"].add(t["wiki_stage"])

    # 清洗:所有 theme 的 codes 統一轉 sorted list(src3 產出的是 set,JSON 不能存 set)
    for t in all_themes:
        if isinstance(t.get("codes"), set):
            t["codes"] = sorted(t["codes"])

    out = {
        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "date": now.strftime("%Y%m%d"),
        "themes": all_themes,
        "stocks": [
            {
                "code": cd,
                "themes": sorted(v["themes"]),
                "sources": sorted(v["sources"]),
                "hit_count": len(v["themes"]),
                "subjects": v["subjects"][:3],
                # 發酵標記:pre-ferment 優先(還沒發酵、最有價值),否則取有的第一個
                "fermentation": ("pre-ferment" if "pre-ferment" in v.get("wiki_stages", set())
                                 else ("active" if "active" in v.get("wiki_stages", set())
                                       else None)),
            }
            for cd, v in sorted(code_hits.items(),
                                key=lambda kv: len(kv[1]["themes"]), reverse=True)
        ],
    }

    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n輸出 {OUT_FILE}: {len(all_themes)} 個題材/事件, "
          f"{len(out['stocks'])} 檔受惠股")
    print("=" * 50)


if __name__ == "__main__":
    main()
