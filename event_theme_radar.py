# -*- coding: utf-8 -*-
"""
============================================================
event_theme_radar.py — 事件/題材交易雷達 (GitHub Actions 端)
============================================================
三個偵測源:
  1. 固定關鍵字追蹤:讀 themes_watchlist.txt,用鉅亨 search API 算熱度暴增
  2. 自動偵測新題材:掃鉅亨分類新聞,jieba 斷詞找爆量新詞
  3. MOPS 重訊事件:抓公開資訊觀測站當日重訊,過濾硬事件關鍵字

輸出:
  event_theme_raw.json  → 推到 quietfolio-data repo,供 GAS 端讀取

依賴: requests, jieba  (requirements 只這兩個,Actions 秒裝)

資料源(全部免費、免金鑰):
  鉅亨分類新聞  https://api.cnyes.com/media/api/v1/newslist/category/{cat}
  鉅亨關鍵字搜  https://api.cnyes.com/media/api/v1/search/news?q={kw}&page={n}
  TWSE 股票清單 https://openapi.twse.com.tw/v1/opendata/t187ap03_L
  TPEx 股票清單 https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O
  MOPS 重訊     https://mopsov.twse.com.tw/mops/web/ajax_t05st01 (POST)

注意: 鉅亨新聞內文 API 只給標題+摘要,完整內文需另抓
      https://news.cnyes.com/news/id/{newsId} 的 HTML。本程式用
      標題+摘要(summary/content 欄)做股號萃取,已足夠涵蓋多數點名個股;
      若要更完整可開啟 FETCH_FULL_BODY(較慢,預設關閉)。
============================================================
"""

import requests
import jieba
import re
import json
import time
import datetime as dt
from collections import Counter

# ============================================================
# 設定
# ============================================================
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
CNYES_BASE = "https://api.cnyes.com/media/api/v1"
OUT_FILE = "event_theme_raw.json"

# 固定關鍵字清單檔(每行一個題材關鍵字)。Actions 會從 repo 讀。
WATCHLIST_FILE = "themes_watchlist.txt"

# 掃描的鉅亨新聞分類
CNYES_CATEGORIES = ["tw_stock", "tw_quo", "headline"]

# 自動偵測新題材:近N天 vs 基線
RECENT_DAYS = 3
BASELINE_DAYS = 20

# 熱度暴增門檻(近期日均 / 基線日均 >= 此值 才算暴增)
SURGE_RATIO = 2.0

# MOPS 硬事件關鍵字
MOPS_EVENT_KEYWORDS = [
    "接獲訂單", "取得訂單", "承接", "擴產", "擴充產能", "新增產能",
    "合作", "簽約", "簽署", "策略聯盟", "技術授權", "調升", "調漲",
    "漲價", "投資", "併購", "取得", "認證通過", "量產", "開發成功"
]

# 金融/科技題材種子詞(補進jieba,避免被切碎;也當自動偵測的白名單參考)
THEME_SEED_WORDS = [
    "矽光子", "CPO", "玻璃基板", "HVDC", "記憶體", "漲價", "擴產", "接單",
    "InP", "散熱", "液冷", "AI伺服器", "先進封裝", "CoWoS", "矽中介層",
    "SiC", "氮化鎵", "GaN", "低軌衛星", "機器人", "人形機器人", "固態電池",
    "光通訊", "矽光子", "共同封裝光學", "電源管理", "PMIC", "DrMOS",
    "ABF", "載板", "銅箔基板", "AI PC", "邊緣運算", "量子", "無晶片",
]

FETCH_FULL_BODY = False   # 預設關閉(較慢);開啟會多抓新聞完整內文提升股號覆蓋率


# ============================================================
# 股號↔股名對照表(自動從 TWSE/TPEx 建)
# ============================================================
def build_name2code():
    """從 TWSE + TPEx 官方清單建 {股名: 股號}。含 KY 股別名。"""
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
            for row in data:
                code = str(row.get("公司代號", "")).strip()
                short = str(row.get("公司簡稱", "")).strip()
                if not (code and short and re.match(r"^\d{4,6}$", code)):
                    continue
                name2code[short] = code
                # 別名:去 -KY / * 等後綴
                alias = short.replace("-KY", "").replace("＊", "").replace("*", "").strip()
                if alias and alias != short and alias not in name2code:
                    name2code[alias] = code
            print(f"  {tag} 股票清單: 累計 {len(name2code)} 名稱")
        except Exception as e:
            print(f"  ⚠ {tag} 股票清單抓取失敗: {e}")

    # 把所有股名加進 jieba,避免內文股名被切碎
    for nm in name2code:
        if len(nm) >= 2:
            jieba.add_word(nm)
    for w in THEME_SEED_WORDS:
        jieba.add_word(w)

    return name2code


# ============================================================
# 從文字萃取股號(括號法 + 股名反查)
# ============================================================
def extract_codes(text, name2code):
    if not text:
        return set()
    codes = set()
    # 括號內 4 位數股號
    for m in re.findall(r"[（(](\d{4})[)）]", text):
        codes.add(m)
    # 股名反查
    for nm, cd in name2code.items():
        if nm in text:
            codes.add(cd)
    return codes


# ============================================================
# 鉅亨 API
# ============================================================
def cnyes_category(cat, start_ts, end_ts, limit=30, max_pages=5):
    """抓某分類在時間區間的新聞。回傳 list of dict(title, summary, publishAt, newsId)。"""
    out = []
    for page in range(1, max_pages + 1):
        url = (f"{CNYES_BASE}/newslist/category/{cat}"
               f"?startAt={start_ts}&endAt={end_ts}&limit={limit}&page={page}")
        try:
            r = requests.get(url, headers=UA, timeout=20)
            if r.status_code != 200:
                break
            j = r.json()
            items = (j.get("items") or {}).get("data") or []
            if not items:
                break
            for it in items:
                out.append({
                    "newsId": it.get("newsId"),
                    "title": it.get("title", ""),
                    "summary": it.get("summary", "") or it.get("content", ""),
                    "publishAt": it.get("publishAt"),
                    "category": cat,
                })
            # 沒有下一頁就停
            last_page = (j.get("items") or {}).get("last_page", 1)
            if page >= last_page:
                break
            time.sleep(0.3)
        except Exception as e:
            print(f"  ⚠ 分類 {cat} 第 {page} 頁抓取失敗: {e}")
            break
    return out


def cnyes_search_count(keyword, start_ts, end_ts, max_pages=10):
    """搜尋某關鍵字,回傳落在時間區間內的新聞則數(用來算熱度)。"""
    cnt = 0
    for page in range(1, max_pages + 1):
        url = f"{CNYES_BASE}/search/news?q={requests.utils.quote(keyword)}&page={page}"
        try:
            r = requests.get(url, headers=UA, timeout=20)
            if r.status_code != 200:
                break
            j = r.json()
            items = (j.get("items") or {}).get("data") or []
            if not items:
                break
            stop = False
            for it in items:
                pub = it.get("publishAt") or 0
                if pub < start_ts:
                    stop = True
                    continue
                if start_ts <= pub <= end_ts:
                    cnt += 1
            if stop:
                break
            last_page = (j.get("items") or {}).get("last_page", 1)
            if page >= last_page:
                break
            time.sleep(0.3)
        except Exception as e:
            print(f"  ⚠ 搜尋 {keyword} 第 {page} 頁失敗: {e}")
            break
    return cnt


# ============================================================
# 偵測源 1: 固定關鍵字熱度暴增
# ============================================================
def detect_fixed_keywords(name2code, now_ts):
    print("[偵測源1] 固定關鍵字熱度追蹤")
    try:
        with open(WATCHLIST_FILE, "r", encoding="utf-8") as f:
            keywords = [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
    except FileNotFoundError:
        print(f"  找不到 {WATCHLIST_FILE},跳過偵測源1")
        return []

    recent_start = now_ts - RECENT_DAYS * 86400
    base_start = now_ts - BASELINE_DAYS * 86400

    results = []
    for kw in keywords:
        recent_cnt = cnyes_search_count(kw, recent_start, now_ts)
        base_cnt = cnyes_search_count(kw, base_start, now_ts)
        recent_daily = recent_cnt / RECENT_DAYS
        base_daily = base_cnt / BASELINE_DAYS if base_cnt else 0.01
        ratio = round(recent_daily / base_daily, 2) if base_daily else 0

        surge = ratio >= SURGE_RATIO and recent_cnt >= 3
        print(f"  {kw}: 近{RECENT_DAYS}日={recent_cnt} 基線日均={round(base_daily,2)} "
              f"暴增比={ratio} {'🔥暴增' if surge else ''}")

        if surge:
            # 抓這個關鍵字近期新聞內文,萃取受惠股
            codes = set()
            for page in range(1, 4):
                url = f"{CNYES_BASE}/search/news?q={requests.utils.quote(kw)}&page={page}"
                try:
                    r = requests.get(url, headers=UA, timeout=20)
                    if r.status_code != 200:
                        break
                    items = (r.json().get("items") or {}).get("data") or []
                    for it in items:
                        pub = it.get("publishAt") or 0
                        if pub < recent_start:
                            continue
                        txt = (it.get("title", "") + " " +
                               (it.get("summary", "") or it.get("content", "")))
                        codes |= extract_codes(txt, name2code)
                    time.sleep(0.3)
                except Exception:
                    break
            results.append({
                "theme": kw,
                "ratio": ratio,
                "recent_count": recent_cnt,
                "codes": sorted(codes),
                "source": "固定關鍵字",
            })
        time.sleep(0.3)
    return results


# ============================================================
# 偵測源 2: 自動偵測爆量新題材(jieba 斷詞)
# ============================================================
def detect_emerging_themes(name2code, now_ts):
    print("[偵測源2] 自動偵測爆量新題材")
    recent_start = now_ts - RECENT_DAYS * 86400
    base_start = now_ts - BASELINE_DAYS * 86400

    recent_tokens = Counter()
    base_tokens = Counter()
    # 收集每個關鍵詞對應到的新聞文字(供之後萃取股號)
    token_texts = {}

    stopwords = set(["公司", "表示", "指出", "今日", "昨日", "目前", "以及", "由於",
                     "個股", "股價", "市場", "投資", "台股", "美股", "受到", "包括",
                     "持續", "上漲", "下跌", "漲幅", "跌幅", "成長", "營收", "法人",
                     "分析", "預估", "看好", "元月", "本週", "今年", "去年", "季度"])

    for cat in CNYES_CATEGORIES:
        news = cnyes_category(cat, base_start, now_ts, limit=30, max_pages=6)
        for it in news:
            pub = it.get("publishAt") or 0
            txt = it.get("title", "") + " " + it.get("summary", "")
            tokens = set(t for t in jieba.cut(txt)
                         if len(t) >= 2 and not t.isdigit()
                         and t not in stopwords
                         and not re.match(r"^\d+$", t))
            for t in tokens:
                if base_start <= pub <= now_ts:
                    base_tokens[t] += 1
                if recent_start <= pub <= now_ts:
                    recent_tokens[t] += 1
                    token_texts.setdefault(t, "")
                    if len(token_texts[t]) < 2000:
                        token_texts[t] += " " + txt
        time.sleep(0.3)

    results = []
    for tok, rcnt in recent_tokens.most_common(200):
        if rcnt < 3:
            continue
        bcnt = base_tokens.get(tok, 0)
        recent_daily = rcnt / RECENT_DAYS
        base_daily = bcnt / BASELINE_DAYS if bcnt else 0.01
        ratio = round(recent_daily / base_daily, 2) if base_daily else 999

        # 爆量:近期日均遠高於基線,且至少3則
        if ratio >= SURGE_RATIO and rcnt >= 3:
            codes = extract_codes(token_texts.get(tok, ""), name2code)
            # 過濾:題材詞通常不會是純粹的股名本身
            if tok in name2code:
                continue
            results.append({
                "theme": tok,
                "ratio": ratio,
                "recent_count": rcnt,
                "codes": sorted(codes),
                "source": "自動偵測",
            })

    # 依暴增比排序,取前 30
    results.sort(key=lambda x: (x["ratio"], x["recent_count"]), reverse=True)
    for r in results[:30]:
        print(f"  🆕 {r['theme']}: 近期={r['recent_count']} 暴增比={r['ratio']} "
              f"受惠股={r['codes'][:8]}")
    return results[:30]


# ============================================================
# 偵測源 3: MOPS 重訊硬事件
# ============================================================
def detect_mops_events(name2code, today):
    print("[偵測源3] MOPS 重大訊息事件")
    roc_y = today.year - 1911
    date_str = f"{roc_y:03d}{today.month:02d}{today.day:02d}"

    url = "https://mopsov.twse.com.tw/mops/web/ajax_t05st01"
    results = []
    for market in ["sii", "otc"]:   # 上市, 上櫃
        try:
            payload = {
                "encodeURIComponent": "1", "step": "1", "firstin": "1",
                "off": "1", "TYPEK": market, "year": str(roc_y),
                "month": f"{today.month:02d}", "day": f"{today.day:02d}",
            }
            r = requests.post(url, data=payload, headers=UA, timeout=25)
            if r.status_code != 200:
                print(f"  ⚠ MOPS {market} HTTP {r.status_code}")
                continue
            html = r.text
            # 重訊表格:每列含 公司代號 / 公司名稱 / 主旨
            # 用寬鬆 regex 抓出 (股號, 主旨) 配對
            rows = re.findall(
                r"<tr[^>]*>.*?(\d{4}).*?</tr>", html, re.S)
            # 更精準:抓含主旨的列
            subj_rows = re.findall(
                r"(\d{4})</td>\s*<td[^>]*>([^<]+)</td>.*?<td[^>]*>([^<]{4,})</td>",
                html, re.S)
            for code, name, subject in subj_rows:
                subject = re.sub(r"\s+", "", subject)
                hit = [kw for kw in MOPS_EVENT_KEYWORDS if kw in subject]
                if hit:
                    results.append({
                        "theme": "重訊:" + "/".join(hit),
                        "ratio": None,
                        "recent_count": 1,
                        "codes": [code],
                        "subject": subject[:60],
                        "source": "MOPS重訊",
                    })
            print(f"  MOPS {market}: 命中 {len([x for x in results if x['source']=='MOPS重訊'])} 筆硬事件")
        except Exception as e:
            print(f"  ⚠ MOPS {market} 失敗: {e}")
    return results


# ============================================================
# 主流程
# ============================================================
def main():
    print("=" * 50)
    print("EventThemeRadar 開始")
    now = dt.datetime.now()
    now_ts = int(now.timestamp())

    print("[前置] 建立股號↔股名對照表")
    name2code = build_name2code()
    print(f"  對照表共 {len(name2code)} 個名稱")

    src1 = detect_fixed_keywords(name2code, now_ts)
    src2 = detect_emerging_themes(name2code, now_ts)
    src3 = detect_mops_events(name2code, now)

    # 彙總:以「股號」為中心,記錄它被哪些題材/事件點名
    code_hits = {}   # code -> {themes:set, sources:set, subjects:list}
    all_themes = src1 + src2 + src3
    for t in all_themes:
        for cd in t["codes"]:
            entry = code_hits.setdefault(cd, {"themes": set(), "sources": set(),
                                              "subjects": []})
            entry["themes"].add(t["theme"])
            entry["sources"].add(t["source"])
            if t.get("subject"):
                entry["subjects"].append(t["subject"])

    # 輸出結構
    out = {
        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "date": now.strftime("%Y%m%d"),
        "themes": all_themes,   # 每個題材/事件 + 受惠股
        "stocks": [             # 以個股為中心的彙總(給GAS交集用)
            {
                "code": cd,
                "themes": sorted(v["themes"]),
                "sources": sorted(v["sources"]),
                "hit_count": len(v["themes"]),
                "subjects": v["subjects"][:3],
            }
            for cd, v in sorted(code_hits.items(),
                                key=lambda kv: len(kv[1]["themes"]), reverse=True)
        ],
    }

    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n輸出 {OUT_FILE}: {len(out['themes'])} 個題材, "
          f"{len(out['stocks'])} 檔受惠股")
    print("=" * 50)


if __name__ == "__main__":
    main()
