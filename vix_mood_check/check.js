// 一次性驗證(2026-11-19,v2026-11-19-vixmood):用期交所 / FinMind 真實資料驗證 GAS VixMood.js 的純計算函式。只印 log,不存檔。
const fs = require('fs'), vm = require('vm');
vm.runInThisContext(fs.readFileSync(__dirname + '/VixMood.js', 'utf8'));
const UA = { 'User-Agent': 'quietfolio-vix-check/1.0 (read-only research)' };
const sleep = ms => new Promise(r => setTimeout(r, ms));
(async () => {
  const now = new Date(Date.now() + 8 * 3600e3), ymd = d => d.toISOString().slice(0, 10).replace(/-/g, '/');
  const prev = new Date(now.getTime() - 86400e3);
  let r = await fetch('https://www.taifex.com.tw/indes/index.aspx/GetStockDayPrices', { method: 'POST',
    headers: Object.assign({ 'Content-Type': 'application/json; charset=utf-8' }, UA),
    body: JSON.stringify({ syid: 'TAIWANVIX', flag: 'MS', startDate: '2021/07/01', endDate: ymd(prev) }) });
  const hist = vmParseHist(await r.text());
  console.log('hist', hist && hist.length, hist && hist[0], hist && hist[hist.length - 1]);
  await sleep(3000);
  let month = [];
  for (const ym of [ymd(new Date(now.getFullYear(), now.getMonth() - 1, 15)).slice(0, 7).replace('/', ''), ymd(now).slice(0, 7).replace('/', '')]) {
    r = await fetch('https://www.taifex.com.tw/file/taifex/Dailydownload/vix/log2data/' + ym + 'new.txt', { headers: UA });
    const buf = Buffer.from(await r.arrayBuffer()); const txt = new TextDecoder('big5').decode(buf);
    const rows = vmParseMonthFile(txt); console.log('month', ym, r.status, rows.length, rows[rows.length - 1]); month = month.concat(rows);
    await sleep(3000);
  }
  // 歷史與當月檔重疊日期是否一致
  const hm = Object.fromEntries(hist); let diff = 0, ov = 0;
  month.forEach(([d, v]) => { if (hm[d] != null) { ov++; if (Math.abs(hm[d] - v) > 0.005) { diff++; console.log(' mismatch', d, hm[d], v); } } });
  console.log('overlap', ov, 'mismatch', diff);
  const series = vmMerge(hist, month);
  r = await fetch('https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockTotalReturnIndex&data_id=TAIEX&start_date=2021-06-01',
    { headers: { Authorization: 'Bearer ' + process.env.FINMIND_TOKEN } });
  const taiex = ((await r.json()).data || []).map(x => [x.date, x.price]);
  console.log('taiex', taiex.length, taiex[taiex.length - 1]);
  const ind = vmIndicators(series), byD = Object.fromEntries(ind.map(o => [o.date, o]));
  console.log('=== 驗證值(±0.2) ===');
  [['2026-11-19', 49.29, 99.6, 99.2], ['2025-04-07', 42.69, 99.6, 84.4], ['2024-11-01', 28.26, 95.6, 44.6], ['2024-08-05', 48.05, 100, null]].forEach(([d, v, p, q]) => {
    const o = byD[d] || {};
    const ok = o.close === v && Math.abs(o.ivp252 - p) <= 0.2 && (q == null || Math.abs(o.ivr252 - q) <= 0.2);
    console.log((ok ? 'OK  ' : 'FAIL'), d, 'vix', o.close, 'ivp252', vmR_(o.ivp252, 2), 'ivr252', vmR_(o.ivr252, 2), 'want', v, p, q);
  });
  const rp = vmEpisodes(ind, taiex);
  console.log('=== 重播觸發日 ===');
  const want = ['2024-08-05', '2025-04-07', '2026-03-03', '2026-05-05', '2026-07-20', '2026-11-06'];
  const got = rp.episodes.map(e => e.start);
  console.log(JSON.stringify(got) === JSON.stringify(want) ? 'OK  ' : 'FAIL', 'got', JSON.stringify(got));
  rp.episodes.forEach(e => console.log(' ep', JSON.stringify(e)));
  console.log('=== 轉換 log ===');
  rp.log.forEach(l => console.log(' ', l.date, l.state, l.vix, l.ivp, l.why));
  // 加權指數乖離分位:與 GAS analyze() 相同(LOOKBACK 500 天、60MA)
  const cut = new Date(now.getTime() - 500 * 86400e3).toISOString().slice(0, 10);
  const tx = taiex.filter(x => x[0] >= cut).map(x => x[1]); const bias = [];
  for (let i = 59; i < tx.length; i++) { let s = 0; for (let j = i - 59; j <= i; j++) s += tx[j]; bias.push((tx[i] - s / 60) / (s / 60) * 100); }
  const cur = bias[bias.length - 1], pct = Math.round(bias.filter(b => b <= cur).length / bias.length * 1000) / 10;
  console.log('bias len', bias.length, 'cur', cur.toFixed(2), 'pctile', pct);
  const mood = vmBuildMood(series, taiex, pct); delete mood._log;
  const show = JSON.parse(JSON.stringify(mood)); show.vix.series = show.vix.series.slice(-3).concat([['… 共 ' + mood.vix.series.length + ' 筆']]);
  console.log('=== market_mood ===\n' + JSON.stringify(show, null, 1));
})().catch(e => { console.error(e); process.exit(1); });
