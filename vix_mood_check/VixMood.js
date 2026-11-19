/****************************************************************
 * VixMood.js — 乖離追蹤「市場氛圍」:臺指 VIX(VIXTWN)× IVP / IVR 極端值觀察
 * 版本 v2026-11-19-vixmood
 * ============================================================
 * 規格:使用者 2026-11-19「乖離追蹤加入市場氛圍」。規則依據 2026-11-19 對 0050、00881 的回測(2021-07~2026-11)。
 * 資料:
 *   - 歷史日收盤:期交所 POST indes/index.aspx/GetStockDayPrices(syid TAIWANVIX,伺服器最早 2021-07-01)。
 *     endDate 不能 ≥ 今天(伺服器回「結束日期不能大於或等於今天！」),所以一律用前一日;當日值用當月檔補。
 *   - 當日值:file/taifex/Dailydownload/vix/log2data/YYYYMMnew.txt(Big5,tab 分隔:交易日期、時間、收盤指數、收盤前1分鐘平均)。
 *   - 加權指數:FinMind TaiwanStockTotalReturnIndex TAIEX(與乖離追蹤同源,為報酬指數)只用於事件 T3(收盤 > 20 日均線)。
 *   - 失敗一律保留舊資料,不覆寫。
 * 工作表:vix_history(date yyyy-MM-dd, close)、vix_episode_log(每次狀態轉換;每天依完整歷史重播重寫,結果可重現)。
 * 指標全部用收盤值(盤中 VIX 有 2.5% 跳動限制、換月會跳動)。
 * 純計算函式(vm 開頭、不呼叫 GAS 服務)與 quietfolio-data 的驗證腳本共用同一份程式碼。
 ****************************************************************/

var VM_VERSION = 'v2026-11-19-vixmood';
var VM_CFG = {
  ivp_win: 252, ivp_win2: 126, ma_win: 20, chg_days: 5, med_win: 60, series_len: 275,
  // 氛圍分級(由上往下取第一個符合)
  panic_ivp: 95, vix_hi: 30, fear_ivp: 90, elevated_ivp: 70, complacent_ivp: 10,
  // 附加旗標
  diverge_ivp: 90, diverge_ivr: 50, high_vol_med60: 30, retreat_ratio: 0.85,
  // 事件狀態機
  trig_ivp: 95, trig_vix: 30, t2_ratio: 0.85, taiex_ma: 20, timeout_days: 60, rearm_ivp: 80
};
var VM_BACKTEST_REF = { period: '2022-07~2026-11', n60: 5, sig_mean60_0050: 0.166, base_mean60_0050: 0.096,
  worst_mae120_0050: -0.123, note: '樣本少且均為多頭期間' };
var VM_REGIME = {
  panic_extreme: { label: '極端恐慌', color: 'red',
    note: 'VIX 在過去一年 95% 以上的日子之上且 ≥ 30。歷史回測中屬高期望值區(分批承接的參考),樣本少。' },
  fear: { label: '恐慌', color: 'orange', note: 'VIX 偏高(過去一年 90% 分位以上或 ≥ 30)。' },
  elevated: { label: '偏高', color: 'yellow', note: 'VIX 高於一年中多數日子(70~90% 分位)。' },
  normal: { label: '中性', color: 'gray', note: 'VIX 在一年分布的中段。' },
  complacent: { label: '安逸', color: 'blue',
    note: 'VIX 在一年最低的 10%。可考慮停止加碼、檢視停利;回測顯示之後的報酬和任意日相同,不是賣出訊號。' }
};
var VM_FLAG_NOTE = {
  ivp_ivr_divergence: 'IVP ≥ 90 但 IVR < 50:一年內有更高的前高,這次的「相對極端」不夠極端(回測最差的一次 2024-11-01 即此型,0050 之後 120 日最大回撤 -24.8%)。',
  high_vol_regime: '近 60 日 VIX 中位數 ≥ 30:高波動已成常態,極端訊號之後的報酬會被稀釋(2026/5、2026/7 兩次 60 日報酬僅個位數)。',
  retreating: '事件進行中且 VIX 已從本輪高點回落 15% 以上:恐慌消退中,但可能出現第二波(例如 2026/11/16)。'
};
var VM_DISCLAIMER = '歷史回測,非投資建議';

// ───────────────────────── 純計算 ─────────────────────────
function vmR_(x, d) { if (x == null || !isFinite(x)) return null; var m = Math.pow(10, d == null ? 2 : d); return Math.round(x * m) / m; }
function vmMedian_(a) { var s = a.slice().sort(function (x, y) { return x - y; }); var n = s.length; if (!n) return null;
  return n % 2 ? s[(n - 1) / 2] : (s[n / 2 - 1] + s[n / 2]) / 2; }

/** series:[[yyyy-MM-dd, close], ...] 依日期排序 → 每日指標陣列(與 series 對齊) */
function vmIndicators(series) {
  var c = series.map(function (r) { return r[1]; }), out = [], C = VM_CFG;
  for (var i = 0; i < c.length; i++) {
    var o = { date: series[i][0], close: c[i] };
    if (i >= C.ivp_win) {                                   // IVP:過去 252 日(不含當日)收盤 < 當日 的比例
      var lt = 0; for (var j = i - C.ivp_win; j < i; j++) if (c[j] < c[i]) lt++;
      o.ivp252 = lt / C.ivp_win * 100;
    }
    if (i >= C.ivp_win - 1) {                               // IVR:含當日的 252 日 min / max
      var mn = Infinity, mx = -Infinity;
      for (var k = i - C.ivp_win + 1; k <= i; k++) { if (c[k] < mn) mn = c[k]; if (c[k] > mx) mx = c[k]; }
      o.ivr252 = mx > mn ? (c[i] - mn) / (mx - mn) * 100 : null;
    }
    if (i >= C.ivp_win2) { var lt2 = 0; for (var j2 = i - C.ivp_win2; j2 < i; j2++) if (c[j2] < c[i]) lt2++; o.ivp126 = lt2 / C.ivp_win2 * 100; }
    if (i >= C.ma_win - 1) { var s = 0; for (var m = i - C.ma_win + 1; m <= i; m++) s += c[m]; o.ma20_ratio = c[i] / (s / C.ma_win); }
    if (i >= C.chg_days) o.chg5d = (c[i] / c[i - C.chg_days] - 1) * 100;
    if (i >= C.med_win - 1) o.med60 = vmMedian_(c.slice(i - C.med_win + 1, i + 1));
    out.push(o);
  }
  return out;
}

function vmRegime(o, episode) {
  var C = VM_CFG, v = o.close, p = o.ivp252, code;
  if (p == null) code = v >= C.vix_hi ? 'fear' : 'normal';
  else if (p >= C.panic_ivp && v >= C.vix_hi) code = 'panic_extreme';
  else if (p >= C.fear_ivp || v >= C.vix_hi) code = 'fear';
  else if (p >= C.elevated_ivp) code = 'elevated';
  else if (p > C.complacent_ivp) code = 'normal';
  else code = 'complacent';
  var flags = [];
  if (p != null && p >= C.diverge_ivp && o.ivr252 != null && o.ivr252 < C.diverge_ivr) flags.push('ivp_ivr_divergence');
  if (o.med60 != null && o.med60 >= C.high_vol_med60) flags.push('high_vol_regime');
  if (episode && (episode.state === 'triggered' || episode.state === 't2') && episode.peak && v <= episode.peak * C.retreat_ratio) flags.push('retreating');
  var R = VM_REGIME[code];
  return { code: code, label: R.label, color: R.color, flags: flags,
    note: R.note + ' ' + flags.map(function (f) { return VM_FLAG_NOTE[f]; }).join(' ') + '(' + VM_DISCLAIMER + ')' };
}

/** 加權指數 [[yyyy-MM-dd, close]] → {date: close > MA20} */
function vmAboveMa_(taiex) {
  var m = {}, n = VM_CFG.taiex_ma, s = 0;
  for (var i = 0; i < taiex.length; i++) {
    s += taiex[i][1]; if (i >= n) s -= taiex[i - n][1];
    if (i >= n - 1) m[taiex[i][0]] = taiex[i][1] > s / n;
  }
  return m;
}

/** 事件狀態機重播:idle → triggered(T1)→ t2 → t3;逾時 → done;t3 / done 之後 IVP < 80 才回到 idle */
function vmEpisodes(ind, taiex) {
  var C = VM_CFG, above = vmAboveMa_(taiex || []), st = { state: 'idle' }, log = [], eps = [], cur = null;
  ind.forEach(function (o, i) {
    var v = o.close, p = o.ivp252, s = st.state, move = function (to, why) {
      st.state = to; log.push({ date: o.date, state: to, vix: v, ivp: vmR_(p, 1), why: why || '' }); };
    if (s === 'idle') {
      if (p != null && p >= C.trig_ivp && v >= C.trig_vix) {
        cur = { start: o.date, t1: o.date, t2: null, t3: null, peak: v, startIdx: i, end: null };
        eps.push(cur); move('triggered', 'IVP ≥ ' + C.trig_ivp + ' 且 VIX ≥ ' + C.trig_vix);
      }
      return;
    }
    if (s === 'triggered' || s === 't2') {
      if (v > cur.peak) cur.peak = v;
      if (i - cur.startIdx > C.timeout_days) { cur.end = 'timeout'; move('done', '超過 ' + C.timeout_days + ' 個交易日未到 T3'); return; }
      if (s === 'triggered' && v <= cur.peak * C.t2_ratio) { cur.t2 = o.date; move('t2', 'VIX ≤ 高點 ' + cur.peak + ' × ' + C.t2_ratio); return; }
      if (s === 't2' && above[o.date] === true) { cur.t3 = o.date; cur.end = 't3'; move('t3', '加權指數站上 ' + C.taiex_ma + ' 日均線'); return; }
      return;
    }
    if ((s === 't3' || s === 'done') && p != null && p < C.rearm_ivp) move('idle', 'IVP < ' + C.rearm_ivp + ' 重新待命');
  });
  var last = ind.length - 1;
  var episode = { state: st.state };
  if (cur && (st.state !== 'idle')) {
    episode = { state: st.state, start: cur.start, peak: cur.peak, t1: cur.t1, t2: cur.t2, t3: cur.t3,
      days: last - cur.startIdx, end: cur.end };
  }
  return { episode: episode, log: log, episodes: eps.map(function (e) { return { start: e.start, peak: e.peak, t2: e.t2, t3: e.t3, end: e.end }; }) };
}

function vmConfluence(biasPctile, ivp) {
  var P = biasPctile, V = ivp, label = '無共振';
  if (P != null && V != null) {
    if (P <= 5 && V >= 95) label = '雙極端（恐慌超跌）';
    else if (P <= 10 && V >= 90) label = '超跌＋恐慌';
    else if (P >= 95 && V <= 10) label = '過熱＋安逸';
  }
  var note = { '雙極端（恐慌超跌）': '回測中屬高期望值區,但樣本少', '過熱＋安逸': '停止加碼,不是賣出訊號' }[label] || '';
  return { bias_pctile: P == null ? null : vmR_(P, 1), ivp: V == null ? null : vmR_(V, 1), label: label, note: note };
}

/** 組 market_mood(series = 最近 275 個交易日 [MM/DD, VIX, IVP, IVR]) */
function vmBuildMood(series, taiex, biasPctile, opt) {
  opt = opt || {};
  var ind = vmIndicators(series);
  if (!ind.length) return null;
  var o = ind[ind.length - 1], rp = vmEpisodes(ind, taiex);
  var tail = ind.slice(-VM_CFG.series_len).map(function (x) {
    return [x.date.slice(5).replace('-', '/'), x.close, vmR_(x.ivp252, 1), vmR_(x.ivr252, 1)]; });
  return {
    version: VM_VERSION, as_of: o.date, stale: !!opt.stale, stale_note: opt.stale_note || null,
    vix: { close: o.close, ivp252: vmR_(o.ivp252, 1), ivr252: vmR_(o.ivr252, 1), ivp126: vmR_(o.ivp126, 1),
      ma20_ratio: vmR_(o.ma20_ratio, 2), chg5d: vmR_(o.chg5d, 2), med60: vmR_(o.med60, 2), series: tail },
    regime: vmRegime(o, rp.episode),
    episode: rp.episode,
    confluence: vmConfluence(biasPctile, o.ivp252),
    chip: null,   // MarketRegime.gs(籌碼結論)尚未建立 → 前端顯示「未啟用」
    backtest_ref: VM_BACKTEST_REF,
    disclaimer: VM_DISCLAIMER,
    _log: rp.log
  };
}

/** 當月檔文字 → [[yyyy-MM-dd, close]] */
function vmParseMonthFile(text) {
  var out = [];
  String(text || '').split(/\r?\n/).forEach(function (l) {
    var m = l.match(/^\s*(\d{8})\s+\d+\s+([\d.]+)/);
    if (m) out.push([m[1].slice(0, 4) + '-' + m[1].slice(4, 6) + '-' + m[1].slice(6), Number(m[2])]);
  });
  return out;
}

/** GetStockDayPrices 回應文字 → [[yyyy-MM-dd, close]];格式不對回傳 null */
function vmParseHist(text) {
  try {
    var d = JSON.parse(JSON.parse(text).d);
    var td = d && d.TrendData;
    if (!td || !td.length) return null;
    return td.filter(function (x) { return /^\d{8}$/.test(String(x.Time)) && isFinite(x.Price); })
      .map(function (x) { var t = String(x.Time); return [t.slice(0, 4) + '-' + t.slice(4, 6) + '-' + t.slice(6), Number(x.Price)]; });
  } catch (e) { return null; }
}

/** 依日期合併(後者覆蓋前者)並排序 */
function vmMerge(a, b) {
  var m = {}; (a || []).concat(b || []).forEach(function (r) { if (r && r[0] && isFinite(r[1])) m[r[0]] = Number(r[1]); });
  return Object.keys(m).sort().map(function (k) { return [k, m[k]]; });
}
