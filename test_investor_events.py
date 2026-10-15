# -*- coding: utf-8 -*-
"""
test_investor_events.py — investor_events / earnings_keywords 比對規則的單元測試(不需網路)

用法:python test_investor_events.py
案例取自 2026-10-13 實跑的實際標題。
"""
from earnings_keywords import count_keywords, keyword_regex
from investor_events import count_all, event_owner_hit

JEFFERIES = ("Jefferies Semiconductor Weekly: Recap of Marvell Analyst Day, Preview of OCP Summit, "
             "and Updates on AMD’s Investment in Taiwan - 富途牛牛")


def test_event_belongs_to_nearest_company():
    """週報標題裡的 Analyst Day 屬於 Marvell,不能算給後面才出現的 AMD。"""
    assert event_owner_hit("MRVL", JEFFERIES)
    assert not event_owner_hit("AMD", JEFFERIES)


def test_real_event_titles_still_match():
    cases = [
        ("MRVL", "Marvell: Investor Day Resets The AI Growth Story (NASDAQ:MRVL) - Seeking Alpha"),
        ("MRVL", "Bank of America tweaks Marvell stock target after key Analyst Day - TheStreet"),
        ("MRVL", "Marvell Shows Bullish Tone at Investor Day, Oppenheimer Says - Yahoo Finance"),
        ("SNPS", "Needham raises Synopsys stock price target on investor day outlook By Investing.com"),
        ("SNPS", "Synopsys in spotlight as Needham ups price target after investor day (SNPS:NASDAQ)"),
        ("AMD", "AMD to Host Financial Analyst Day on November 11"),
        ("MRVL", "Upcoming Investor Day: Marvell outlines custom silicon roadmap"),
    ]
    for sym, t in cases:
        assert event_owner_hit(sym, t), (sym, t)


def test_other_company_at_event_not_counted():
    t = "Nvidia executives to speak at Marvell Investor Day"
    assert event_owner_hit("MRVL", t) and not event_owner_hit("NVDA", t)


def test_far_apart_not_counted():
    t = ("AMD shares rose on Tuesday as investors weighed a long list of macro data, rate expectations, "
         "tariff headlines and chip export rules ahead of the company's analyst day")
    assert not event_owner_hit("AMD", t)


def test_uppercase_acronym_case_sensitive():
    """動詞 ups 不是 UPS(不斷電系統);follow-ups 也不算。"""
    t = "Synopsys in spotlight as Needham ups price target after investor day (SNPS:NASDAQ)"
    assert "UPS" not in count_all(t)
    assert "UPS" not in count_keywords("We had several follow-ups and start-ups in the quarter")
    assert count_keywords("Vertiv sees UPS and HVDC demand")["UPS"]["count"] == 1


def test_short_acronym_not_substring():
    """ATE 不能命中 rate / operate(國際新聞熱度原本是純子字串比對)。"""
    assert not keyword_regex("ATE").findall("Fed rate cut helps operate margins; we ate lunch")
    assert len(keyword_regex("ATE").findall("Teradyne ATE demand")) == 1


def test_mixed_case_still_case_insensitive():
    assert count_all("pcie gen6 retimer")["PCIe"]["count"] == 1
    assert count_keywords("Co-Packaged Optics and HBM")  # 非全大寫詞仍不分大小寫


if __name__ == "__main__":
    import sys
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ✓ {name}")
            except AssertionError as e:
                fails += 1
                print(f"  ✗ {name}: {e}")
    print("全部通過" if not fails else f"{fails} 項失敗")
    sys.exit(1 if fails else 0)
