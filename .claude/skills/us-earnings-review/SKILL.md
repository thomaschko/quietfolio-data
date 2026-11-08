---
name: us-earnings-review
description: 解讀美股法說逐字稿的題材詞命中(us_transcript_hits.json、earnings_keywords.json),比較本季與上季、區分公司講稿與分析師問答,對照台股供應鏈與共識頁。使用者要求「看某家美股法說」「這季法說哪些題材升溫」「美股法說對台股的影響」時使用。只描述,不給買賣建議。
---

# 美股法說解讀(us-earnings-review)

## 資料來源(都在 quietfolio-data repo;先 `git fetch origin main` 再讀 `origin/main` 的版本)
- `us_transcript_hits.json`:us_transcripts.py 每天產生。`items[代號][季別].hits["群組|詞"] = {p: 公司講稿次數, q: 分析師問答次數, s: 片段(≤3,≤200 字元)}`;`aggregate` 是各詞本季 vs 上季提到的公司數;`universe` 是這家屬於 src6 與哪些題材籃子。
- `us_transcript_state.json`:每家每季抓取狀態(ok / empty / quota / error)。
- `earnings_keywords.json`:src6(earnings_keywords.py)每週一的結果,23 家固定名單。
- 對照台股:`intl_theme_baskets.json`(題材 ↔ 台美股)、My-TW-Coverage 題材(`margin_share_themes.json`)、`consensus.json`(市場氛圍、槓桿 + 法人)、`chip_structure_full.json`(集保大戶、法人週資料)。
- 逐字稿原文不在 repo,也不要另外去抓或貼出原文;只用片段。

## 步驟
1. 確認資料日期:`us_transcript_hits.json.generated`、`quarters`,以及這家在 state 的狀態。若是 empty / quota,直接說「這季還沒有逐字稿或額度用完」,不要推測內容。
2. 單一公司:列出本季命中的詞,依「問答次數」排序在前(分析師追問比公司自述更有資訊);和上季比較:新出現的詞、消失的詞、次數明顯變化的詞。
3. 跨公司:用 `aggregate` 找「本季提到的公司數 − 上季」增加最多的詞,以及 `new_this_q`(本季才提到的公司)。
4. 對照台股:詞所屬題材 → `intl_theme_baskets.json` 的台股代號與 My-TW-Coverage 題材;再看這些台股在 `consensus.json`(融資 / 法人分類)與 `chip_structure_full.json`(大戶週變化)的現況。只並列,不推論因果。
5. 片段只能原樣引用(≤ 200 字元),並標註「公司陳述,未經查證」。

## 輸出格式
- 一句話摘要(資料日期、幾家公司、哪些詞變化最大)。
- 表格:詞 | 本季公司數 | 上季公司數 | 講稿 / 問答次數 | 對應台股。
- 每個重點詞最多 1 個片段,標「公司陳述」。
- 最後一行固定:「詞命中不等於題材成立;本整理沒有預測效力的證據,不是買賣建議。」

## 規則
- 不給買進、賣出、加碼建議,不用「看多 / 看空 / 主力」等字。
- 不修改任何資料檔、詞表(earnings_keywords.py KEYWORDS)、watchlist 或雷達邏輯;建議新增的詞只列成清單,由使用者決定。
- 外國私人發行人(TSM、ASML 等)逐字稿可能缺漏,缺就說缺。
- 前瞻性說法(量產時程、產能、供不應求)一律標「公司陳述」。
