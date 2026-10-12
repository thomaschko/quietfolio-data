# -*- coding: utf-8 -*-
"""
============================================================
investor_events.py — 國際大廠 Investor Day / 技術大會 題材偵測(2026-10-12新增)
============================================================
起因:Marvell 2026/10/6 Investor Day 講了一堆題材(custom XPU、scale-up、
光互連…),三道既有防線全漏——
  1. earnings_keywords.py 用 Alpha Vantage,只有季度法說逐字稿,沒有 investor day
  2. us_8k_events.py 那幾天 Marvell 沒有申報 8-K(就算有,也只記 Item 類別)
  3. 新聞來源以中文為主,英文 recap(Substack 等)沒收
但 investor day 通常「事前就公告日期」(Marvell 8/3 財報新聞稿就公布 10/6),
可以先排進行事曆,活動前後集中收集。

三層:
  1. 行事曆:Google News 美國版 RSS 查「公司名 + investor day/analyst day/…」,
     從公告標題解析活動日期,累積在 investor_calendar.json(跨日保留)
  2. 活動當天~後5天:同一批查詢的報導 + Substack/電子報 recap RSS
     (+ 大廠固定技術大會:GTC、OFC、Hot Chips、OCP…,近3日有報導就算進行中)
  3. 關鍵詞計分:沿用 earnings_keywords.py 的詞表(count_keywords),
     再加 investor day 常見詞(scale-up、AEC、XPU…);只存「詞×次數」與標題,
     不存全文(與其他偵測源同一套版權原則)

輸出:investor_day_signals.json(daily_digest.py 讀進 F2 區,並算進 G 區國際背書)
     investor_calendar.json(行事曆狀態)
資料源全部免費、不需 API key;每天跑(含週末),任一來源失敗只記錄、不中斷。
============================================================
"""
import os
import re
import json
import time
import html
import datetime as dt
import xml.etree.ElementTree as ET
import requests

try:
    from earnings_keywords import COMPANIES, count_keywords
except Exception:  # 單獨測試時也能跑
    COMPANIES = {}

    def count_keywords(text):
        return {}

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120 Safari/537.36"}
TPE = dt.timezone(dt.timedelta(hours=8))
OUT_FILE = "investor_day_signals.json"
CALENDAR_FILE = "investor_calendar.json"

GNEWS_US = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
COMPANY_LOOKBACK_DAYS = 14   # 公司查詢的 when: 窗口(每天跑,公告發出 14 天內都抓得到)
CONF_LOOKBACK_DAYS = 3       # 技術大會查詢窗口(近3日有報導才算進行中)
ACTIVE_AFTER_DAYS = 5        # 活動日起算幾天內視為「進行中」,集中收集 recap
KEEP_PAST_DAYS = 60          # 行事曆保留多久以前的活動
MIN_CONF_ITEMS = 3           # 技術大會近3日至少幾則報導才列入
REQ_SLEEP = 1.0

EVENT_TERMS = ["investor day", "analyst day", "financial analyst day", "capital markets day",
               "investor meeting", "investor event", "ai day", "technology day", "tech day",
               "strategy day", "investor update"]
EVENT_RE = re.compile("|".join(re.escape(t) for t in EVENT_TERMS), re.I)
ANNOUNCE_RE = re.compile(r"\bannounc|\bto host\b|\bwill host\b|\bto hold\b|\bschedul|\binvit|\bwebcast\b", re.I)

# 查詢用公司名(Google News 用引號精確比對;太短或太泛的名稱改用全名)。
# 涵蓋 earnings_keywords.COMPANIES 的 23 家,另加 investor day/技術大會常點名題材的大廠。
SEARCH_NAMES = {
    "NVDA": "Nvidia", "TSM": "TSMC", "AMD": "AMD", "AVGO": "Broadcom", "MRVL": "Marvell",
    "LITE": "Lumentum", "COHR": "Coherent Corp", "AAOI": "Applied Optoelectronics",
    "TSEM": "Tower Semiconductor", "AAPL": "Apple", "AMZN": "Amazon", "GOOGL": "Alphabet",
    "TER": "Teradyne", "AEHR": "Aehr Test", "KEYS": "Keysight", "AMAT": "Applied Materials",
    "ON": "onsemi", "AXTI": "AXT Inc", "LWLG": "Lightwave Logic", "QCOM": "Qualcomm",
    "ARM": "Arm Holdings", "ADI": "Analog Devices", "VRT": "Vertiv",
    # 額外(不在 AV 法說清單,但 investor day 常有題材)
    "MU": "Micron", "INTC": "Intel", "CSCO": "Cisco", "ANET": "Arista", "MSFT": "Microsoft",
    "META": "Meta Platforms", "ORCL": "Oracle", "SMCI": "Supermicro", "DELL": "Dell Technologies",
    "CRDO": "Credo Technology", "ALAB": "Astera Labs", "CIEN": "Ciena", "LRCX": "Lam Research",
    "KLAC": "KLA Corp", "ASML": "ASML", "SNPS": "Synopsys", "CDNS": "Cadence Design",
}

# 大廠固定技術大會(沒有單一公告日期;近3日有足夠報導就算進行中)
CONFERENCES = {
    "Nvidia GTC": "NVDA", "AMD Advancing AI": "AMD", "Computex": "",
    "OCP Global Summit": "", "Hot Chips": "", "OFC conference": "",
    "AWS re:Invent": "AMZN", "Google I/O": "GOOGL", "Microsoft Build": "MSFT",
    "Intel Foundry Direct Connect": "INTC", "SEMICON West": "", "TSMC Technology Symposium": "TSM",
}

# Recap 電子報 RSS(Substack 的 /feed 免費文章含全文 content:encoded,只拿來計數)
# semianalysis 已在 news_sources.py 實測可用;其餘首次上線請看輸出的 sources_status 確認。
RECAP_FEEDS = {
    "semiconalpha": "https://semiconalpha.substack.com/feed",
    "semianalysis": "https://newsletter.semianalysis.com/feed",
    "fabricatedknowledge": "https://www.fabricatedknowledge.com/feed",
    "chipstrat": "https://www.chipstrat.com/feed",
}

# investor day 常見、但 earnings_keywords 詞表沒有的詞
EXTRA_KEYWORDS = {
    "互連/網通": ["scale-up", "scale-out", "scale-across", "AEC", "active electrical cable",
                "retimer", "PCIe", "CXL", "UALink", "Ultra Ethernet", "NVLink Fusion",
                "co-packaged copper", "near-packaged optics", "NPO", "electro-optics",
                "optical interconnect"],
    "客製化晶片": ["XPU", "custom XPU", "custom ASIC", "custom accelerator", "custom compute",
              "design win", "attach"],
    "製程/封裝": ["2nm", "3nm", "A16", "backside power", "HBM base die", "3D stacking"],
    "市場規模": ["TAM", "serviceable addressable market", "long-term model", "target model"],
}
FLAT_EXTRA = [(cat, kw) for cat, kws in EXTRA_KEYWORDS.items() for kw in kws]

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
DATE_RE = re.compile(
    r"\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|"
    r"Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?\s+(\d{1,2})(?:st|nd|rd|th)?"
    r"(?:,?\s+(20\d{2}))?\b", re.I)

STATUS = {}   # 各來源抓取狀態,寫進輸出


# ============================================================
# 抓取
# ============================================================
def _strip_html(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def _pubdate(s):
    """RFC822 → aware datetime;失敗回 None。"""
    try:
        from email.utils import parsedate_to_datetime
        d = parsedate_to_datetime(s)
        return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
    except Exception:
        return None


def parse_rss(xml_text, with_content=False):
    """回傳 [{title, pub(datetime|None), text}]。text = 標題+描述(+全文),只供計數。"""
    out = []
    root = ET.fromstring(xml_text)
    ns_content = "{http://purl.org/rss/1.0/modules/content/}encoded"
    for it in root.iter("item"):
        title = _strip_html(it.findtext("title") or "")
        desc = _strip_html(it.findtext("description") or "")
        body = _strip_html(it.findtext(ns_content) or "") if with_content else ""
        out.append({"title": title, "pub": _pubdate(it.findtext("pubDate") or ""),
                    "text": " ".join(x for x in (title, desc, body) if x)})
    return out


def fetch_rss(url, label, with_content=False):
    try:
        r = requests.get(url, headers=UA, timeout=20)
        if r.status_code != 200:
            STATUS[label] = {"ok": False, "http": r.status_code}
            return []
        items = parse_rss(r.text, with_content)
        STATUS[label] = {"ok": True, "items": len(items)}
        return items
    except Exception as e:
        STATUS[label] = {"ok": False, "error": str(e)[:100]}
        return []


def gnews(query):
    return fetch_rss(GNEWS_US.format(q=requests.utils.quote(query)), f"gnews:{query}")


# ============================================================
# 解析
# ============================================================
def parse_event_date(text, pub):
    """從公告文字抓活動日期。沒寫年份時,取離發布日最近、且不早於發布日30天前的年份。"""
    ref = (pub or dt.datetime.now(TPE)).date()
    for m in DATE_RE.finditer(text):
        mon = MONTHS[m.group(1)[:3].lower()]
        day = int(m.group(2))
        years = [int(m.group(3))] if m.group(3) else [ref.year, ref.year + 1]
        for y in years:
            try:
                d = dt.date(y, mon, day)
            except ValueError:
                continue
            if -30 <= (d - ref).days <= 300:
                return d
    return None


def count_all(text):
    """earnings_keywords 詞表 + EXTRA_KEYWORDS,同一套單字邊界規則。"""
    counts = {kw: v for kw, v in count_keywords(text).items()}
    for cat, kw in FLAT_EXTRA:
        if re.fullmatch(r"[A-Za-z\s\-]+", kw):
            pattern = r"\b" + re.escape(kw) + r"\b"
        else:
            pattern = re.escape(kw)
        n = len(re.findall(pattern, text, re.I))
        if n:
            counts[kw] = {"count": counts.get(kw, {}).get("count", 0) + n, "category": cat}
    return counts


def merge_counts(acc, counts):
    for kw, v in counts.items():
        e = acc.setdefault(kw, {"count": 0, "category": v["category"]})
        e["count"] += v["count"]


def name_hit(sym, text):
    name = SEARCH_NAMES.get(sym, sym)
    return re.search(r"\b" + re.escape(name) + r"\b", text, re.I) is not None


def event_label(text):
    m = EVENT_RE.search(text)
    return m.group(0).title().replace("Ai ", "AI ") if m else "Investor Event"


# ============================================================
# 主流程
# ============================================================
def load_calendar():
    try:
        with open(CALENDAR_FILE, encoding="utf-8") as f:
            return json.load(f).get("events", [])
    except Exception:
        return []


def run(now=None, fetch_company=None, fetch_conf=None, fetch_feed=None):
    """fetch_* 可替換成假資料(測試用)。回傳 (signals, calendar)。"""
    now = now or dt.datetime.now(TPE)
    today = now.astimezone(TPE).date()
    fetch_company = fetch_company or (lambda sym, name: gnews(
        f'"{name}" ("investor day" OR "analyst day" OR "capital markets day" OR "investor meeting" '
        f'OR "AI day" OR "technology day") when:{COMPANY_LOOKBACK_DAYS}d'))
    fetch_conf = fetch_conf or (lambda conf: gnews(f'"{conf}" when:{CONF_LOOKBACK_DAYS}d'))
    fetch_feed = fetch_feed or (lambda label, url: fetch_rss(url, f"feed:{label}", with_content=True))

    calendar = {(e["symbol"], e["date"]): e for e in load_calendar() if e.get("date")}
    per_symbol_items = {}

    # ── 1. 公司查詢:公告(→行事曆)+ 報導 ──
    for sym, name in SEARCH_NAMES.items():
        items = [it for it in fetch_company(sym, name)
                 if EVENT_RE.search(it["text"]) and name_hit(sym, it["text"])]
        per_symbol_items[sym] = items
        for it in items:
            if not ANNOUNCE_RE.search(it["title"]):
                continue
            d = parse_event_date(it["text"], it["pub"])
            if d:
                key = (sym, d.isoformat())
                calendar.setdefault(key, {
                    "symbol": sym, "name": name, "event": event_label(it["text"]),
                    "date": d.isoformat(), "first_seen": today.isoformat(),
                    "announce_title": it["title"][:160]})
        time.sleep(REQ_SLEEP)

    # ── 2. Recap 電子報:比對到公司名 + (活動詞 或 takeaways/recap) ──
    feed_items = []
    for label, url in RECAP_FEEDS.items():
        for it in fetch_feed(label, url):
            it["feed"] = label
            feed_items.append(it)
        time.sleep(REQ_SLEEP)
    recap_re = re.compile(r"takeaway|recap|highlights|investor day|analyst day|keynote", re.I)

    # ── 3. 進行中的公司活動:活動日 ~ +ACTIVE_AFTER_DAYS ──
    events = []
    for (sym, dstr), ev in sorted(calendar.items(), key=lambda kv: kv[0][1]):
        d = dt.date.fromisoformat(dstr)
        delta = (today - d).days
        ev["status"] = "upcoming" if delta < 0 else ("active" if delta <= ACTIVE_AFTER_DAYS else "past")
        if ev["status"] != "active":
            continue
        start = dt.datetime(d.year, d.month, d.day, tzinfo=TPE) - dt.timedelta(days=1)
        news = [it for it in per_symbol_items.get(sym, [])
                if it["pub"] and it["pub"] >= start and not ANNOUNCE_RE.search(it["title"])]
        # recap 必須「標題」就點名這家公司:內文順帶提到(例如 Marvell recap 裡講 TSMC)不算
        recaps = [it for it in feed_items
                  if it["pub"] and it["pub"] >= start and name_hit(sym, it["title"])
                  and recap_re.search(it["text"])]
        kw = {}
        for it in news + recaps:
            merge_counts(kw, count_all(it["text"]))
        events.append({
            "type": "investor_day", "symbol": sym, "name": ev["name"], "event": ev["event"],
            "event_date": dstr, "day_offset": delta,
            "news_count": len(news), "recap_count": len(recaps),
            "recap_sources": sorted({it["feed"] for it in recaps}),
            "titles": [it["title"][:160] for it in (recaps + news)][:6],
            "keywords": dict(sorted(kw.items(), key=lambda x: -x[1]["count"])),
        })

    # 沒排進行事曆、但近期 recap 直接寫到某公司 investor day 的(公告漏抓的保險)
    listed = {e["symbol"] for e in events}
    for sym in SEARCH_NAMES:
        if sym in listed:
            continue
        recaps = [it for it in feed_items
                  if it["pub"] and (now - it["pub"]).days <= ACTIVE_AFTER_DAYS
                  and name_hit(sym, it["title"]) and EVENT_RE.search(it["title"])]
        if recaps:
            kw = {}
            for it in recaps:
                merge_counts(kw, count_all(it["text"]))
            events.append({
                "type": "investor_day", "symbol": sym, "name": SEARCH_NAMES[sym],
                "event": event_label(recaps[0]["title"]), "event_date": None, "day_offset": None,
                "news_count": 0, "recap_count": len(recaps),
                "recap_sources": sorted({it["feed"] for it in recaps}),
                "titles": [it["title"][:160] for it in recaps][:6],
                "keywords": dict(sorted(kw.items(), key=lambda x: -x[1]["count"])),
            })

    # ── 4. 技術大會 ──
    for conf, sym in CONFERENCES.items():
        key_word = conf.replace(" conference", "")
        items = [it for it in fetch_conf(conf) if key_word.lower() in it["text"].lower()]
        if len(items) >= MIN_CONF_ITEMS:
            kw = {}
            for it in items:
                merge_counts(kw, count_all(it["text"]))
            events.append({
                "type": "conference", "symbol": sym, "name": conf, "event": conf,
                "event_date": None, "day_offset": None, "news_count": len(items), "recap_count": 0,
                "recap_sources": [], "titles": [it["title"][:160] for it in items][:6],
                "keywords": dict(sorted(kw.items(), key=lambda x: -x[1]["count"])),
            })
        time.sleep(REQ_SLEEP)

    # 行事曆:丟掉太舊的
    cal_list = [e for e in calendar.values()
                if (today - dt.date.fromisoformat(e["date"])).days <= KEEP_PAST_DAYS]
    for e in cal_list:
        delta = (today - dt.date.fromisoformat(e["date"])).days
        e["status"] = "upcoming" if delta < 0 else ("active" if delta <= ACTIVE_AFTER_DAYS else "past")
    cal_list.sort(key=lambda e: e["date"])

    keyword_hits = []
    for e in events:
        # 顯示用來源標籤:公司活動「MRVL(Investor Day)」;技術大會「OCP Global Summit」
        if e["type"] == "conference":
            label = e["name"]
        else:
            label = f"{e['symbol']}({e['event']})"
        for kw, v in e["keywords"].items():
            keyword_hits.append({"symbol": e["symbol"] or e["name"], "label": label,
                                 "event": e["event"], "type": e["type"],
                                 "keyword": kw, "category": v["category"], "count": v["count"]})
    signals = {
        "generated_at": now.astimezone(TPE).strftime("%Y-%m-%d %H:%M:%S"),
        "date": today.strftime("%Y%m%d"),
        "events": events,
        "upcoming": [e for e in cal_list if e["status"] == "upcoming"][:20],
        "keyword_hits": keyword_hits,
        "sources_status": {
            "gnews_ok": sum(1 for k, v in STATUS.items() if k.startswith("gnews:") and v.get("ok")),
            "gnews_total": sum(1 for k in STATUS if k.startswith("gnews:")),
            "feeds": {k[5:]: v for k, v in STATUS.items() if k.startswith("feed:")},
        },
    }
    return signals, {"generated_at": signals["generated_at"], "events": cal_list}


def main():
    print("=" * 50)
    print("Investor Day / 技術大會 題材偵測 開始")
    signals, cal = run()
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(signals, f, ensure_ascii=False, indent=1)
    with open(CALENDAR_FILE, "w", encoding="utf-8") as f:
        json.dump(cal, f, ensure_ascii=False, indent=1)
    st = signals["sources_status"]
    feeds = ", ".join(k + ":" + ("OK" if v.get("ok") else "失敗") for k, v in st["feeds"].items())
    print(f"  Google News 查詢成功 {st['gnews_ok']}/{st['gnews_total']};電子報 {feeds}")
    print(f"  行事曆:即將舉行 {len(signals['upcoming'])} 場")
    for e in signals["upcoming"][:8]:
        print(f"    {e['date']} {e['symbol']} {e['event']}")
    print(f"  進行中活動 {len(signals['events'])} 場")
    for e in signals["events"]:
        top = ", ".join(f"{k}×{v['count']}" for k, v in list(e["keywords"].items())[:6])
        print(f"    [{e['type']}] {e['name']} {e['event']} {e['event_date'] or ''} "
              f"報導{e['news_count']} recap{e['recap_count']}  {top}")
    print(f"→ {OUT_FILE}、{CALENDAR_FILE} 已寫出")


if __name__ == "__main__":
    main()
