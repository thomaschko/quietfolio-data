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
import urllib3

# 2026-09-18新增:讓src1的股號抽取也能看到23源(src4)已收集到的標題池,
# 不再只信任cnyes一個搜尋引擎的收錄範圍。根因:實測發現9/17當天MoneyDJ
# 有兩篇明確提及貿聯-KY(3665)+800VDC+Vera Rubin的深度報導,但cnyes搜尋
# 沒收錄到,導致src1完全漏掉這組訊號,即使MoneyDJ本身就是23源之一。
# 注意:只用來擴充「股號抽取」的文字池,暴增比(近3日 vs 前17日基線)
# 的計算仍然只用cnyes資料,不能混入23源去污染這個校準過的比例。
try:
    from news_sources import fetch_titles_by_source
    SRC4_AVAILABLE = True
except Exception as _e:
    SRC4_AVAILABLE = False
    print(f"  ⚠ 23源標題池不可用,股號抽取將只用cnyes: {_e}")

# ============================================================
# 安全性提醒(2026-09-14,已與使用者確認接受此取捨):
# TPEx(www.tpex.org.tw)伺服器憑證鏈缺少中繼憑證,並非客戶端CA包過期
# (已試過pip install --upgrade certifi無效),故下方對TPEx的兩個請求
# 明確關閉SSL驗證(verify=False),範圍嚴格限定在這兩個網址,不影響
# TWSE、鉅亨或任何其他請求的驗證。這裡抓的是公開政府開放資料
# (唯讀GET、無帳密、非敏感個資),風險可控。
# 只抑制"InsecureRequestWarning"這一種警告,其餘警告不受影響。
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

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
NEAR_MISS_LOW = 0.8        # 2026-09-15新增:近期關注區下限,暴增比落在[0.8,1.5)算「有動能但未過門檻」

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
    code2name = {}  # 反向對照:代碼→官方公司簡稱(給顯示用,不含alias變體)
    sources = [
        ("https://openapi.twse.com.tw/v1/opendata/t187ap03_L", "TWSE"),
        ("https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O", "TPEx"),
    ]
    for url, tag in sources:
        try:
            # 2026-09-14:TPEx伺服器憑證鏈缺少中繼憑證(非客戶端CA包過期,升級certifi無效),
            # 已與使用者確認,限定只對tpex.org.tw關閉SSL驗證,其他所有請求(含TWSE)維持驗證。
            verify_ssl = "tpex.org.tw" not in url
            r = requests.get(url, headers=UA, timeout=20, verify=verify_ssl)
            r.raise_for_status()
            data = r.json()
            before = len(name2code)
            for row in data:
                code = str(row.get("公司代號", "")).strip()
                short = str(row.get("公司簡稱", "")).strip()
                if not (code and short and re.match(r"^\d{4,6}$", code)):
                    continue
                name2code[short] = code
                code2name[code] = short  # 用官方公司簡稱當顯示名稱(不是alias)
                # 2026-09-17根因修正:「材料-KY」去掉-KY後變成別名「材料」,
                # 這是極常見的中文詞彙(半導體材料/封裝材料到處都在講),
                # 任何2字以下的別名都有這種「巧合變成常用詞」的高風險,
                # 一律不產生這種別名(完整名稱如「材料-KY」仍在name2code裡,
                # 不影響正常比對,只是不額外產生這個過短、易誤判的捷徑)。
                alias = short.replace("-KY", "").replace("＊", "").replace("*", "").strip()
                if alias and alias != short and len(alias) >= 3 and alias not in name2code:
                    name2code[alias] = code
            print(f"  {tag} 股票清單: +{len(name2code)-before} → 累計 {len(name2code)}")
        except Exception as e:
            print(f"  ⚠ {tag} 股票清單抓取失敗: {e}")
    return name2code, code2name


def extract_codes(text, name2code):
    """回傳 {code: weight}。有股號格式(1234)=明確點名權重2;純股名比對權重1。"""
    if not text:
        return {}
    codes = {}
    # 明確股號格式 (1234) → 高可信度
    for m in re.findall(r"[（(](\d{4})[)）]", text):
        codes[m] = 2
    # 純股名比對 → 低可信度(易誤中,尤其2字股名)
    for nm, cd in name2code.items():
        if len(nm) >= 2 and nm in text:
            if cd not in codes:
                codes[cd] = 1
    return codes


def extract_stock_codes_from_articles(kw, texts, name2code, log_evidence=False):
    """從一批文章文字裡,用「強命中優先、綜述文整篇排除」的安全邏輯抽取
    相關股票代碼。這是2026-09-17一連串除錯後確認的最終版邏輯,被
    event_theme_radar.py(watchlist關鍵字題材)和ai_theme_discovery.py
    (AI自由發現題材)共用,確保兩種題材來源使用同一套已驗證過的安全機制,
    不重複寫、不各自累積不同的bug。

    kw: 用於查詢DISAMBIGUATION/THEME_STOCK_EXCLUDE字典的鍵(watchlist
        關鍵字或AI題材名稱皆可,查無對應項目時自動略過,不影響抽取邏輯本身)。
    texts: 文章文字清單(list of str,通常是title或title+summary)。
    回傳: (codes, evidence) — codes是排序後的股號清單,evidence是
        {code: [文字片段]} 供人工回查用。
    """
    DISAMBIGUATION = {
        "CUBE": ["記憶體", "華邦", "3D堆疊", "TSV", "類HBM",
                 "邊緣AI", "3DCaaS", "堆疊技術", "混合鍵合"],
    }
    THEME_STOCK_EXCLUDE = {
        "sidecar power": {"2395"},
        "power shelf": {"2395"},
        "power rack": {"2395"},
        "HVDC": {"2395"},
        "800V HVDC": {"2395"},
    }
    filtered_texts = texts
    if kw in DISAMBIGUATION:
        filtered_texts = [t for t in texts if any(ctx in t for ctx in DISAMBIGUATION[kw])]
    excluded_for_kw = THEME_STOCK_EXCLUDE.get(kw, set())

    strong_hits, weak_score, evidence = {}, {}, {}
    for text in filtered_texts:
        hits = extract_codes(text, name2code)
        is_roundup = len(hits) > 4  # 單篇命中>4檔視為大盤綜述文,整篇不採計
        if is_roundup:
            if log_evidence:
                print(f"      ⊘ 綜述文排除(命中{len(hits)}檔,不採計): "
                      f"{sorted(hits.keys())} ← {text[:50]}")
            continue
        for cd, w in hits.items():
            if cd in excluded_for_kw:
                continue
            if w == 2:  # 強命中(明確股號格式)
                strong_hits[cd] = strong_hits.get(cd, 0) + 1
            else:  # 弱命中(純股名)
                weak_score[cd] = weak_score.get(cd, 0) + w
            evidence.setdefault(cd, [])
            if len(evidence[cd]) < 3:
                evidence[cd].append(text[:60])

    all_codes = set(strong_hits) | set(weak_score)
    code_score = {cd: strong_hits.get(cd, 0) * 2 + weak_score.get(cd, 0)
                  for cd in all_codes}
    codes = sorted(
        [cd for cd in all_codes
         if strong_hits.get(cd, 0) >= 1 or weak_score.get(cd, 0) >= 2],
        key=lambda c: -code_score[c])
    return codes, evidence


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
def detect_fixed_keywords(name2code, code2name, now_ts, src4_titles=None):
    print("[偵測源1] 固定關鍵字熱度追蹤")
    try:
        with open(WATCHLIST_FILE, "r", encoding="utf-8") as f:
            raw_keywords = [ln.strip() for ln in f
                            if ln.strip() and not ln.startswith("#")]
        # 去重(保留原順序):避免watchlist.txt裡不慎重複的詞被掃描/輸出兩次
        # (2026-09-16修正:發現NAND曾因歷史編輯疏漏重複列在兩個分類段落底下)
        seen_kw = set()
        keywords = []
        for kw in raw_keywords:
            if kw not in seen_kw:
                seen_kw.add(kw)
                keywords.append(kw)
    except FileNotFoundError:
        print(f"  找不到 {WATCHLIST_FILE}")
        return []

    recent_start = now_ts - RECENT_DAYS * 86400
    base_start = now_ts - BASELINE_DAYS * 86400
    results = []
    near_miss_themes = []

    # 補充來源(中央社+MoneyDJ)今日標題,當「當日新聞加成」計入 recent
    try:
        from news_sources import fetch_supplement_titles
        supplement_titles = fetch_supplement_titles()
        print(f"  補充來源(中央社+MoneyDJ): {len(supplement_titles)} 則今日標題")
    except Exception as e:
        print(f"  ⚠ 補充來源抓取失敗(不影響鉅亨): {e}")
        supplement_titles = []

    for kw in keywords:
        news = cnyes_search(kw, base_start)   # 一次抓20日內全部
        recent = [n for n in news if n["publishAt"] >= recent_start]
        # 補充來源:標題含此關鍵字的,計入近期(視為今日新聞)
        supp_hits = [t for t in supplement_titles if kw in t]
        if supp_hits:
            recent = recent + [{"title": t, "summary": "", "publishAt": now_ts} for t in supp_hits]
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
        # 2026-09-15新增:近期關注區(暴增比0.8~1.5之間,真的在成長但還沒過門檻)。
        # 套用跟暴增判斷一樣的防護(enough_base+MIN_RECENT_COUNT),避免eMMC那種
        # 小基數雜訊(暴增比33.33但前段基期太小)被誤列進來;也排除NOR Flash這種
        # 暴增比<0.8的「下降」情況,只留「真的在往上走、只是還沒衝過門檻」的詞。
        near_miss = (not surge and NEAR_MISS_LOW <= ratio < SURGE_RATIO
                     and len(recent) >= MIN_RECENT_COUNT and enough_base)
        flag = "🔥暴增" if surge else ("👀關注中" if near_miss else
                                      ("(基數過小略過)" if ratio >= SURGE_RATIO
                                       and len(recent) >= MIN_RECENT_COUNT else ""))
        print(f"  {kw}: 近{RECENT_DAYS}日={len(recent)} 前段日均={round(earlier_daily,2)} "
              f"暴增比={ratio} {flag}")

        if near_miss:
            near_miss_themes.append({
                "theme": kw, "ratio": ratio, "recent_count": len(recent),
            })

        if surge:
            # 題材→個股關聯,優先順序:
            #   1. My-TW-Coverage 權威主題檔(人工審核過的供應鏈研究)
            #   2. 查無對應主題檔 → 退回新聞內文可信度分數法(舊機制)
            codes_source = "關鍵字暴增"
            coverage_result = None
            try:
                from tw_coverage_lookup import lookup_theme
                coverage_result = lookup_theme(kw)
            except Exception as e:
                print(f"    ⚠ My-TW-Coverage 查詢失敗(退回新聞猜測): {e}")

            if coverage_result:
                codes = coverage_result["codes"]
                names = coverage_result["names"]  # My-TW-Coverage本身就有{code:name}
                codes_source = "my-tw-coverage"
                print(f"    ✓ My-TW-Coverage 找到 {coverage_result['company_count']} 家公司(取代新聞猜測)")
            else:
                print(f"    · My-TW-Coverage 查無對應主題檔,退回新聞猜測法")
                # 退回:累計每檔股票的可信度分數(跨新聞),過濾雜訊
                # 2026-09-16修正:原本「綜述文降權減半+分數>=2保留」的機制,
                # 讓材料-KY(4763)/研華(2395)/國泰金(2882)這類跟題材完全無關的
                # 股票,只因為在多篇文章裡被順帶提及(純股名比對的弱命中),
                # 分數就跨過門檻被誤判成受惠股(人工查證後確認業務完全不相關)。
                # 修正邏輯:
                #   - 強命中(文章裡明確寫出股號,如「(3665)」)永遠不打折,只要
                #     出現1次就視為高可信度,因為這是作者刻意點名,不是巧合
                # 呼叫共用函式(跟ai_theme_discovery.py共用同一套已驗證邏輯,
                # 不再各自維護一份、各自累積不同的bug)
                texts = [n["title"] + " " + n["summary"] for n in recent]
                # 併入23源標題池裡「有出現這個關鍵字」的標題(只做字面比對,
                # 不影響上面的暴增比計算,只補強股號抽取的文字來源廣度)
                if src4_titles:
                    texts += [t for t in src4_titles if kw in t]
                codes, evidence = extract_stock_codes_from_articles(
                    kw, texts, name2code, log_evidence=True)
                names = {cd: code2name.get(cd, "") for cd in codes}  # 2026-09-17新增:補上名稱
                if codes:
                    for cd in codes[:5]:
                        print(f"      · {cd}{names.get(cd,'')} 命中證據: {evidence.get(cd, [])[:2]}")

            results.append({
                "theme": kw,
                "ratio": ratio,
                "recent_count": len(recent),
                "codes": codes,
                "names": names,  # 2026-09-17新增:{code: 股票名稱}
                "source": "關鍵字暴增",
                "codes_source": codes_source,  # 標記這批codes是權威資料庫還是新聞猜測
            })
        time.sleep(0.3)
    return results, near_miss_themes


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


def detect_mops_events(name2code, code2name, today):
    print("[偵測源2] MOPS 重大訊息事件(OpenAPI JSON)")
    sources = [
        ("https://openapi.twse.com.tw/v1/opendata/t187ap04_L", "上市"),
        ("https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap04_O", "上櫃"),
    ]
    today_roc = f"{today.year - 1911:03d}{today.month:02d}{today.day:02d}"  # 1150828
    results = []

    for url, market in sources:
        try:
            # 2026-09-14:同上,只對tpex.org.tw關閉SSL驗證
            verify_ssl = "tpex.org.tw" not in url
            r = requests.get(url, headers=UA, timeout=25, verify=verify_ssl)
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
                        "names": {code: code2name.get(code, "")},  # 2026-09-17新增
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
    name2code, code2name = build_name2code()
    print(f"  對照表共 {len(name2code)} 個名稱")

    # 2026-09-18新增:抓一次23源標題池,供股號抽取補強用(不影響暴增比計算)
    src4_titles = []
    if SRC4_AVAILABLE:
        try:
            print("[前置] 抓取23源標題池(供股號抽取補強,一次抓取全部關鍵字共用)")
            pool = fetch_titles_by_source()
            src4_titles = [t for titles in pool.values() for t in titles]
            print(f"  23源標題池共 {len(src4_titles)} 則")
        except Exception as e:
            print(f"  ⚠ 23源標題池抓取失敗,股號抽取退回只用cnyes: {e}")

    src1, src1_near_miss = detect_fixed_keywords(name2code, code2name, now_ts, src4_titles)
    src2 = detect_mops_events(name2code, code2name, now)

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
        "near_miss_themes": sorted(src1_near_miss, key=lambda x: -x["ratio"]),
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
