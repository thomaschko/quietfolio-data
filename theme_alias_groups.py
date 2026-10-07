# -*- coding: utf-8 -*-
"""
============================================================
theme_alias_groups.py — 題材別名群組表(2026-10-06新增)
============================================================
問題:themes_watchlist.txt裡很多題材其實是同一個敘事的不同說法
(記憶體/記憶體漲價/HBM/HBM4/DDR5/利基型記憶體,或磷化銦/InP),
但event_theme_radar.py對每個watchlist關鍵字各自獨立算暴增比,
這點本身沒問題(各自的新聞量級本來就不同,不該強制合併成一個
數字)。問題出在daily_digest.py的G區比對:只用題材名稱完全相等
去判斷「台股是否已有訊號」,導致HBM已經發酵時,「記憶體」這個
分開追蹤的關鍵詞看起來像「台股還沒反映」,即使兩者講的是同一件事。

這裡提供的是「正規化」用的別名群組,只用在跨題材比對/交叉參照的
場合(目前是daily_digest.py的G區tw_active判斷),不影響、也不會
拿來合併event_theme_radar.py裡每個watchlist關鍵字各自算出的暴增比
——每個關鍵字的新聞量體本來就該分開看,只有「這個敘事今天台股有
沒有動靜」這個判斷需要正規化到同一個群組。

用法:
  from theme_alias_groups import canonical_theme
  canonical_theme("HBM")      -> "記憶體"
  canonical_theme("DDR5")     -> "記憶體"
  canonical_theme("台積電")    -> "台積電"(不在任何群組,原樣回傳)
============================================================
"""

# 群組:key是正規化後的統一名稱,value是這個敘事底下所有分開追蹤的題材名稱
# (含watchlist.txt的關鍵字字面值、wiki_theme_map.py的題材key、KW_TO_THEME
# 的中文題材目標值)。
# 2026-10-07修正:「記憶體」群組原本漏了DRAM(裸詞)、DDR4、南亞科、華邦電
# ——10/06當天DDR4暴增2.43倍、南亞科Q3營收+60%,但因為這四個關鍵字不在
# 成員名單裡,canonical_theme()不會把它們正規化成「記憶體」,G區判斷
# tw_active時完全看不到這兩筆訊號,才會誤判成「國際先行、台股未燃」。
# 同時新增NAND/先進封裝/CPO三個群組(成員依watchlist.txt現有關鍵字),
# 解決同一類「同一敘事被拆成多個獨立關鍵字各自追蹤、互相看不到對方」的問題。
ALIAS_GROUPS = {
    "記憶體": [
        "記憶體", "記憶體漲價", "記憶體模組", "DRAM合約價", "DRAM", "DDR4",
        "HBM", "HBM4", "HBM4E", "DDR5", "LPDDR", "利基型記憶體",
        "南亞科", "華邦電",
    ],
    # 2026-10-07新增(item三查證):企業級SSD/企業級SSD控制晶片併入NAND
    # 家族(慧榮科技MonTitan平台對應的真實市場敘事,是NAND這條供應鏈故事
    # 的延伸,見themes_watchlist.txt同日新增的說明)。
    "NAND": [
        "NAND", "SLC NAND", "eMMC", "UFS", "群聯", "旺宏",
        "企業級SSD", "企業級SSD控制晶片",
    ],
    "先進封裝": [
        "先進封裝", "CoWoS", "CoPoS", "SoIC", "FOPLP",
        "面板級封裝", "2.5D封裝", "3D封裝", "矽中介層",
    ],
    # 2026-10-07新增(item三查證):近封裝光學(NPO)併入CPO家族——兩者是
    # 同一個「AI運算光互連」大敘事下的競爭/替代方案,任一邊發酵都該讓
    # 這個大敘事的tw_active成立。
    "CPO": ["CPO", "共同封裝光學", "矽光子與CPO", "近封裝光學"],
    # 2026-10-07新增(item三查證):Google TPU併入客製化晶片家族——TPU是
    # Google自研客製化ASIC的具體案例,跟XPU/客製化晶片是同一敘事。
    "客製化晶片": ["客製化晶片", "XPU", "Google TPU"],
    "磷化銦": ["磷化銦", "InP", "indium phosphide"],
    "NVIDIA平台": [
        "Vera Rubin", "Rubin", "Rubin Ultra", "Blackwell", "Blackwell Ultra",
        "GB300", "Grace Blackwell", "Grace Hopper",
    ],
}

# 反查表:member題材名稱 -> 正規化群組名稱(同一次import只建一次)
_MEMBER_TO_CANONICAL = {
    member: canonical
    for canonical, members in ALIAS_GROUPS.items()
    for member in members
}


def canonical_theme(theme_name):
    """把題材名稱正規化成群組代表名稱;不在任何群組裡的題材原樣回傳。"""
    return _MEMBER_TO_CANONICAL.get(theme_name, theme_name)


if __name__ == "__main__":
    for canonical, members in ALIAS_GROUPS.items():
        print(f"{canonical}: {', '.join(members)}")
