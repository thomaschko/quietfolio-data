# -*- coding: utf-8 -*-
"""
============================================================
earnings_keywords.py — 偵測源6:國際大廠法說關鍵詞頻率
============================================================
資料源:Alpha Vantage EARNINGS_CALL_TRANSCRIPT(正規API,免費key)
  優於抓 fool.com:結構化JSON、無反爬風險、無版權疑慮、附情緒分數。

核心邏輯:
  抓 NVDA上下游生態系 各家最新季法說逐字稿
  → 統計你關心的關鍵詞頻率(CoWoS/HBM/CPO/供應商...)
  → 季度對比:這季 vs 上季,哪些詞頻率上升 = 客戶端/供應鏈訊號升溫
  → 只輸出「詞×次數」數字(版權安全,不存原文)

節奏:季度性。法說一季一次,平時休眠,財報季跑。
額度:AV免費25次/天。五家×1次=5次,綽綽有餘。

設定:
  API key 存 GitHub Secret,用環境變數 ALPHAVANTAGE_KEY 讀(勿寫死在碼裡)
  公司清單、詞表 見下方,可自由增減

用法:
  export ALPHAVANTAGE_KEY=你的key
  python earnings_keywords.py
============================================================
"""

import os
import re
import json
import time
import datetime as dt
import requests

AV_KEY = os.environ.get("ALPHAVANTAGE_KEY", "")
AV_URL = "https://www.alphavantage.co/query"

# ── 監控公司:NVDA 生態系(上下游+競爭+客戶)──
# 角色標註幫你解讀:不同角色講的話領先性不同
COMPANIES = {
    # ── 核心供應鏈(你持股的上游/同業)──
    "NVDA": "本尊-需求指引",
    "TSM":  "上游-晶圓封裝(CoWoS)",
    "AMD":  "競爭-GPU/ASIC",
    "AVGO": "競爭-ASIC/網通",
    "MRVL": "競爭-ASIC/光通訊",
    # ── 光通訊/CPO 美國同業(對應你的CPO/矽光子研究)──
    "LITE": "光通訊-雷射(Lumentum)",
    "COHR": "光通訊-元件(Coherent)",
    "AAOI": "光通訊-光模組(AAOI)",
    "TSEM": "矽光子代工(Tower,對應CPO/矽光子研究)",
    # ── 雲端客戶(capex 指引,需求面領先)──
    "AAPL": "客戶-蘋果",
    "AMZN": "客戶-亞馬遜(AWS)",
    "GOOGL": "客戶-Alphabet(capex)",
    # ── 測試設備(對應CPO瓶頸=晶圓級測試產能的洞察)──
    "TER":  "測試設備-泰瑞達(CPO測試瓶頸)",
    "AEHR": "測試設備-Aehr(晶圓級burn-in測試)",
    "KEYS": "測試設備-是德科技(量測儀器)",
    # ── 半導體設備(對應High-NA EUV題材,鄰接沉積/蝕刻)──
    "AMAT": "設備-應用材料(沉積/蝕刻)",
    # ── 化合物/功率半導體(對應碳化矽/氮化鎵題材)──
    "ON":   "功率半導體-安森美(SiC)",
    "AXTI": "化合物半導體基板-AXT",
    # 2026-09-22移除IFNNY(英飛凌,OTC ADR,外國私人發行人):Alpha Vantage對
    # 這類ADR的逐字稿涵蓋本來就不穩定,而且原本的註解自己就標記「需驗證」,
    # 一直沒有真的驗證過;既然額度已經吃緊,先讓出這個位置給下面這家確認
    # 過真的有法說逐字稿(Seeking Alpha/Yahoo Finance都有完整紀錄)的矽光子
    # 材料股,不再讓一個自帶不確定性的位置持續佔額度。
    # ── 矽光子/CPO材料層(2026-09-22新增,對應PhotonCap研究方向:材料戰)──
    "LWLG": "矽光子材料-Lightwave Logic(電光聚合物)",
    # 2026-09-22移除POET(POET Technologies):9/22當天實跑log證實,Alpha
    # Vantage對這檔完全查無逐字稿(fetch_transcript()回傳None,None),第三方
    # 平台(Seeking Alpha等)雖然有收錄,但AV自己的資料庫沒有,白白佔一次
    # API呼叫額度卻拿不到任何資料,故移除。LWLG同批加入的,AV確認有收錄
    # (2026Q2:3440字,命中13個關鍵詞),予以保留。
    # ── IC設計/處理器架構 ──
    "QCOM": "IC設計-高通(RF/行動運算)",
    "ARM":  "IC設計-Arm(處理器架構授權)",
    "ADI":  "IC設計-亞德諾(類比/訊號鏈)",
    # ── 資料中心電源(對應HVDC題材)──
    "VRT":  "資料中心電源-Vertiv(HVDC)",
    # 註:SpaceX 未上市無法說,無法納入
}

# 額度控制:免費 AV 25次/天。目前23家 × 1季 = 23次(2026-09-22移除POET後更新;
# 之前這行寫16家已經跟實際清單脫節很久,新增/移除公司時記得同步更新這裡)。
QUARTERS_PER_COMPANY = 1

# ── 五層關鍵詞表(對應你的持股與研究)──
KEYWORDS = {
    "先進封裝/光互連": [
        "CoWoS", "SoIC", "CoPoS", "CPO", "co-packaged", "silicon photonics",
        "advanced packaging", "2.5D", "3D packaging", "interposer",
        "glass substrate", "panel-level", "chiplet",
    ],
    "記憶體": [
        "HBM", "HBM4", "HBM4E", "DRAM", "LPDDR", "NAND", "custom HBM",
        "base die", "memory", "high bandwidth memory",
        # 2026-10-07新增:CXL(使用者提議查證,查證確認三星/SK海力士2026年底
        # CXL系統大規模量產、中國長鑫存儲也在開發CXL DRAM模組,國際面是真實
        # 且活躍的記憶體擴充敘事。但目前查無任何台灣供應鏈廠商有對應敘事
        # (查到的瀾起科技MXC晶片是中國A股掛牌,非台股),所以只加在這裡
        # (國際法說端),不加進themes_watchlist.txt(台股端),避免放一個
        # 在台股新聞永遠不會觸發的死關鍵字)
        "CXL",
        # 2026-10-07新增:enterprise SSD(企業級SSD,使用者提議查證,查證
        # 確認慧榮科技(SIMOS)企業級SSD控制晶片2026Q2營收創歷史新高,對應
        # 「記憶體市場企業級強、消費級弱」雙軌格局的真實市場敘事。惟現有
        # COMPANIES清單裡沒有NAND控制晶片廠(慧榮本身也不在清單上),這個
        # 詞比較可能是在AMAT等設備廠、NVDA等資料中心客戶的capex討論裡
        # 間接提到,信心不如上面幾個對應明確公司的詞高,先加著觀察)
        "enterprise SSD",
    ],
    "AI電源/散熱": [
        "800V", "HVDC", "power shelf", "sidecar", "liquid cooling",
        "immersion", "BBU", "power delivery",
    ],
    "平台/網通/供應鏈": [
        "Vera Rubin", "Rubin", "Blackwell", "NVLink", "co-design",
        "supply chain", "foundry", "capacity", "lead time", "bottleneck",
        "yield",
        # 2026-10-01新增:COT/SerDes/交換器晶片(使用者提議查詢海外美股
        # 對應詞,查證確認AVGO自家法說會逐字稿執行長Hock Tan本人反覆親口
        # 使用「COT」「customer-owned tooling」,分析師每季必問;SerDes跟
        # Tomahawk也是逐字稿原文用詞(200G/400G SerDes,Tomahawk 6 switch)。
        # AVGO已在COMPANIES清單(標註「競爭-ASIC/網通」),直接補關鍵字即可。
        # 用複合詞Ethernet switch/switch ASIC,不用裸詞switch避免誤判
        # (法說會逐字稿常有「switch to」「switch gears」這類無關用法)
        "COT", "customer-owned tooling", "customer owned tooling",
        "SerDes", "Ethernet switch", "switch ASIC", "Tomahawk", "Trident",
        "Jericho",
    ],
    "台廠點名": [
        "TSMC", "Taiwan", "Alchip", "eMemory", "MediaTek",
    ],
    "光通訊/CPO": [
        "optical", "photonics", "laser", "transceiver", "EML", "DFB",
        "800G", "1.6T", "linear drive", "LPO", "optical engine",
        # 2026-10-07修正:原本的裸詞「coherent」會誤判成COHR(Coherent Inc.)
        # 公司名本身被提及(例如「COHR coherent」這類跟技術詞完全無關的
        # 命中,10/06真實資料裡已確認發生),不是「相干光學」技術詞本身被
        # 討論。改用「coherent optics」「coherent detection」兩個複合詞
        # (800G-ZR等長距光模組的真實技術名詞),跟下面DSP同一行註解裡
        # 「不用裸詞避免誤判」的既有慣例一致,不再複用裸詞coherent。
        "coherent optics", "coherent detection",
        "datacom", "InP", "indium phosphide",
        "DSP",  # 2026-09-23新增:野村專家會議指出1.6T DSP缺口延續到2027下半年,
                # 供應緊張推升單價,DSP雙寡頭正是已追蹤的MRVL/AVGO,補上關鍵詞
    ],
    "客戶capex/需求": [
        "capex", "capital expenditure", "data center", "datacenter",
        "AI infrastructure", "custom silicon", "TPU", "accelerator",
        "training", "inference", "cluster", "GPU",
    ],
    # 測試設備(對應新增TER/AEHR/KEYS,CPO測試瓶頸洞察)
    "測試設備": [
        "wafer-level test", "burn-in", "probe card", "test time",
        "ATE", "automated test equipment", "test capacity", "yield",
    ],
    # 功率/化合物半導體(對應新增ON/IFNNY/AXTI,SiC/GaN題材)
    "功率化合物半導體": [
        "SiC", "GaN", "power semiconductor", "wide bandgap",
        "compound semiconductor", "gallium arsenide", "indium phosphide",
    ],
    # IC設計/處理器架構(對應新增QCOM/ARM/ADI)
    "IC設計架構": [
        "RF front-end", "processor architecture", "IP licensing",
        "analog", "signal chain", "mixed-signal", "instruction set",
        # 2026-10-01新增:資安晶片技術詞(使用者提議,ARM已在COMPANIES清單
        # 標註「IC設計-Arm(處理器架構授權)」,TrustZone是ARM最知名的硬體
        # 資安IP品牌,長期是其安全架構核心產品線,PUF/root of trust/secure
        # boot則是較通用的硬體資安術語,可能出現在ARM或其他IC設計廠法說會
        "TrustZone", "root of trust", "secure boot", "PUF",
    ],
    # 資料中心電源(對應新增VRT,HVDC題材)
    "資料中心電源": [
        "HVDC", "800V", "power shelf", "liquid cooling", "immersion cooling",
        "power density", "thermal management", "UPS",
    ],
}
# 攤平成單一 list 供統計,同時保留分類供輸出
FLAT_KEYWORDS = [(cat, kw) for cat, kws in KEYWORDS.items() for kw in kws]


def get_recent_quarters(n=2):
    """回傳最近 n 季的 YYYYQM 標籤(如 2026Q2)。用當前日期推算。"""
    today = dt.date.today()
    # 粗略季度推算(財報通常落後一季,取當前季往前推)
    q = (today.month - 1) // 3 + 1
    y = today.year
    quarters = []
    for _ in range(n + 1):  # 多取一季緩衝(財報有延遲)
        q -= 1
        if q < 1:
            q = 4
            y -= 1
        quarters.append(f"{y}Q{q}")
    return quarters


def fetch_transcript(symbol, quarter):
    """抓某公司某季逐字稿。回傳 (transcript_text, avg_sentiment) 或 (None, None)。"""
    params = {"function": "EARNINGS_CALL_TRANSCRIPT", "symbol": symbol,
              "quarter": quarter, "apikey": AV_KEY}
    try:
        r = requests.get(AV_URL, params=params, timeout=30)
        if r.status_code != 200:
            return None, None
        data = r.json()
        # AV 回傳結構:{"symbol":..,"quarter":..,"transcript":[{content, sentiment,..}]}
        segs = data.get("transcript")
        if not segs:
            # 額度用盡或無資料時,AV 會回 Note/Information
            note = data.get("Note") or data.get("Information") or data.get("error")
            if note:
                print(f"    ⚠ {symbol} {quarter}: {str(note)[:80]}")
            return None, None
        # 合併所有段落文字(只用於本地統計,不儲存)
        texts, sents = [], []
        for seg in segs:
            if isinstance(seg, dict):
                texts.append(seg.get("content", ""))
                s = seg.get("sentiment")
                if s is not None:
                    try:
                        sents.append(float(s))
                    except (ValueError, TypeError):
                        pass
            elif isinstance(seg, str):
                texts.append(seg)
        full = " ".join(texts)
        avg_sent = round(sum(sents) / len(sents), 3) if sents else None
        return full, avg_sent
    except Exception as e:
        print(f"    ⚠ {symbol} {quarter} 抓取錯誤: {str(e)[:60]}")
        return None, None


def count_keywords(text):
    """統計每個關鍵詞出現次數(大小寫不敏感)。只回數字,不留原文。
    2026-09-14修正:純英文字母關鍵字加\\b單字邊界,避免短詞(如ATE)
    誤命中英文常見詞字尾(operate/generate/estimate/rate等),造成大量假訊號。
    含數字/符號的關鍵字(如"1.6T""800G")維持原本子字串比對,因為這類不會有此問題。"""
    counts = {}
    for cat, kw in FLAT_KEYWORDS:
        if re.fullmatch(r'[A-Za-z\s\-]+', kw):
            # 純英文字母(可含空白/連字號)的關鍵字,加單字邊界避免子字串誤判
            pattern = r'\b' + re.escape(kw) + r'\b'
        else:
            pattern = re.escape(kw)
        n = len(re.findall(pattern, text, re.IGNORECASE))
        if n > 0:
            counts[kw] = {"count": n, "category": cat}
    return counts


def analyze_company(symbol, role, quarters):
    """抓最近 N 季(QUARTERS_PER_COMPANY 控制額度),做關鍵詞頻率。"""
    print(f"\n■ {symbol} ({role})")
    results = {}
    sentiments = {}
    for q in quarters[:QUARTERS_PER_COMPANY]:  # 額度控制:預設只抓1季
        text, sent = fetch_transcript(symbol, q)
        time.sleep(1)  # 尊重 AV 頻率限制
        if text:
            results[q] = count_keywords(text)
            sentiments[q] = sent
            print(f"    {q}: {len(text.split())}字, 情緒{sent}, "
                  f"命中{len(results[q])}個關鍵詞")
        else:
            results[q] = {}
    return results, sentiments


def main():
    if not AV_KEY:
        print("✗ 找不到 ALPHAVANTAGE_KEY 環境變數。請設定後再跑。")
        print("  export ALPHAVANTAGE_KEY=你的key")
        return

    quarters = get_recent_quarters(2)
    print("=" * 60)
    print(f"國際大廠法說關鍵詞追蹤  查詢季度: {quarters[:2]}")
    print("=" * 60)

    all_data = {}
    for symbol, role in COMPANIES.items():
        res, sents = analyze_company(symbol, role, quarters)
        all_data[symbol] = {"role": role, "quarters": res, "sentiments": sents}

    # ── 季度對比:找頻率上升的詞 ──
    print("\n" + "=" * 60)
    print("季度對比:關鍵詞頻率上升(客戶端/供應鏈訊號升溫)")
    print("=" * 60)
    # 2026-10-06新增status欄位:prev_q是否為空字串,區分三種完全不同意義的狀態
    # (QUARTERS_PER_COMPANY=1導致絕大多數公司只抓得到單季,prev_q==""不代表
    # 「上一季沒提到」,只代表「沒有上一季可比較」——原本下游只看prev_count==0
    # 就說「新提及」,把這兩種狀態混為一談,也把它算進summary_counts.earnings_rising,
    # 造成法說關鍵詞「升溫」數字虛高):
    #   rising            — 有基期(兩季都抓到)且這季次數確實比上季高
    #   new_mention       — 有基期、上季次數是0、這季首次提到(真正的「新提及」)
    #   no_baseline_count — 單季額度模式,沒有基期可比,只是這季的絕對提及次數
    rising = []
    for symbol, d in all_data.items():
        qs = list(d["quarters"].keys())
        if len(qs) >= 2:
            # 兩季:對比找上升
            qs_sorted = sorted(qs)
            prev_q, this_q = qs_sorted[0], qs_sorted[-1]
            this_c = d["quarters"][this_q]
            prev_c = d["quarters"][prev_q]
            for kw, info in this_c.items():
                now_n = info["count"]
                prev_n = prev_c.get(kw, {}).get("count", 0)
                if now_n > prev_n and now_n >= 2:
                    rising.append({
                        "symbol": symbol, "keyword": kw, "category": info["category"],
                        "this_count": now_n, "prev_count": prev_n,
                        "delta": now_n - prev_n, "this_q": this_q, "prev_q": prev_q,
                        "status": "new_mention" if prev_n == 0 else "rising",
                    })
        elif len(qs) == 1:
            # 單季(額度模式):絕對頻率,提到≥2次就算訊號(但沒有基期可比,
            # 不算「上升」,status標記清楚,下游不得描述成上升/升溫)
            this_q = qs[0]
            this_c = d["quarters"][this_q]
            for kw, info in this_c.items():
                if info["count"] >= 2:
                    rising.append({
                        "symbol": symbol, "keyword": kw, "category": info["category"],
                        "this_count": info["count"], "prev_count": 0,
                        "delta": info["count"], "this_q": this_q, "prev_q": "",
                        "status": "no_baseline_count",
                    })
    rising.sort(key=lambda x: -x["delta"])
    for r in rising[:25]:
        if r["status"] == "no_baseline_count":
            newflag = "提及×" + str(r["this_count"])
        elif r["status"] == "new_mention":
            newflag = "🆕新提及"
        else:
            newflag = f"↑{r['prev_count']}→{r['this_count']}"
        qinfo = f"({r['this_q']})" if not r["prev_q"] else f"({r['prev_q']}→{r['this_q']})"
        print(f"  {r['symbol']:5s} {r['keyword']:20s} [{r['category']}] {newflag} {qinfo}")

    # ── 輸出(只有數字,版權安全)──
    # ── 國際新聞每日熱度(CNBC+Yahoo)——補法說季度空檔 ──
    intl_heat = []
    try:
        from intl_news import fetch_intl_titles
        intl_titles = fetch_intl_titles()
        print(f"\n[國際新聞] CNBC+Yahoo: {len(intl_titles)} 則標題")
        joined = " ".join(intl_titles)
        for cat, kw in FLAT_KEYWORDS:
            c = len(re.findall(re.escape(kw), joined, re.IGNORECASE))
            if c > 0:
                intl_heat.append({"keyword": kw, "category": cat, "count": c})
        intl_heat.sort(key=lambda x: -x["count"])
        print(f"  國際新聞技術詞命中: {len(intl_heat)} 個")
        for h in intl_heat[:12]:
            print(f"    {h['keyword']}: {h['count']}")
    except Exception as e:
        print(f"  ⚠ 國際新聞抓取失敗(不影響法說): {e}")

    out = {
        "generated_at": str(dt.datetime.now()),
        "quarters_queried": quarters[:2],
        "companies": {s: {"role": d["role"],
                          "sentiments": d["sentiments"],
                          "keyword_counts": {q: {k: v["count"] for k, v in c.items()}
                                             for q, c in d["quarters"].items()}}
                      for s, d in all_data.items()},
        "rising_keywords": rising,
        "intl_news_heat": intl_heat,   # 國際新聞每日熱度(CNBC+Yahoo)
    }
    with open("earnings_keywords.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n→ earnings_keywords.json 已寫出({len(rising)}個上升關鍵詞)")


if __name__ == "__main__":
    main()
