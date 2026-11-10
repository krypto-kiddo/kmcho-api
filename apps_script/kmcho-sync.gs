/**
 * KMCho kitchen sheet -> api.kmcho.co.in schedule sync + nightly billing.
 * Add this as a new file in the SAME Apps Script project as the WhatsApp sender.
 *
 * One-time setup
 *   1. Project Settings > Script properties: add KMCHO_SYNC_SECRET
 *      (same value as SHEET_SYNC_SECRET on Railway).
 *   2. Run installSyncTrigger() once (hourly push + nightly billing at SYNC.billingHour).
 *   3. Nothing to add to the 9 AM sender: sendDailyMeals() already calls
 *      pushCurrentMonth() at the end when this file is present.
 *   4. Keywords tab: add a "Billing" column, one of
 *        Credit  not charged; the day's share stays in the customer's balance (PAUSE)
 *        Skip    not a delivery day; the month is spread over the other days (HOLIDAY)
 *        Charge  billed like a meal
 *      Blank = Credit.
 *   4b. Payments tab: add a "Dog" column (after Send reminder). Amount = that dog's
 *      monthly amount. Dog may be blank only when the number has a single dog.
 *   5. Optional: Script property MONITOR_NUMBERS = comma-separated internal/test
 *      numbers (e.g. "9876543210, 9123456789"). Those rows are never synced or billed.
 *
 * Results go to a "Sync log" tab (created on first run); unmatched rows are listed there.
 */

const SYNC = {
  url: 'https://api.kmcho.co.in/v2/schedule/sync',
  billingUrl: 'https://api.kmcho.co.in/v2/billing/run',
  billingHour: 21,       // IST; bills today's deliveries once the day's deliveries are done
  paymentsTab: 'Payments',
  tz: 'Asia/Kolkata',
  headerRow: 1,          // row holding the real dates
  firstDataRow: 2,
  firstDateCol: 6,       // F
  cols: { owner: 1, dog: 2, phone: 3 },   // A, B, C
  keywordsTab: 'Keywords',
  syncLogTab: 'Sync log',
  syncLogMaxRows: 500,
  // Sender's Log tab: Time | Type | Owner | Dog | Number | Result | Message ID / error
  // Only real daily sends are used (Type "MEAL" or "NOT DELIVERED (…)"); tests,
  // payment reminders and failures are skipped. Set log: null to skip entirely.
  log: { tab: 'Log', firstRow: 2, timestampCol: 1, typeCol: 2, dogCol: 4, phoneCol: 5, resultCol: 6, messageIdCol: 7 },
};

/** Hourly trigger + called after the 9 AM send. Also re-pushes last month on the 1st/2nd. */
function pushCurrentMonth() {
  withLock_(pushCurrentMonth_);
}

/** Nightly trigger: push the latest sheet, then bill today's deliveries. */
function runNightlyBilling() {
  withLock_(() => {
    if (!pushCurrentMonth_()) { writeSyncLog_('Billing', 'skip', 'sync failed, billing not run', ''); return; }
    const res = post_({}, SYNC.billingUrl);
    if (res.code !== 200) { writeSyncLog_('Billing', res.code, res.text.slice(0, 2000), ''); return; }
    const j = res.json;
    const problems = j.results.filter(r => r.result !== 'charged' && r.result !== 'credited')
      .map(r => r.dog + ' ' + r.date + ': ' + r.result);
    if (j.older_unbilled) problems.push(j.older_unbilled + ' older day(s) not billed: admin review');
    if (j.pending_changes) problems.push(j.pending_changes + ' edit(s) to billed days waiting for approval');
    writeSyncLog_('Billing', res.code,
      JSON.stringify({ counts: j.counts, charged_total: j.charged_total }), problems.join('\n'));
  });
}

function withLock_(fn) {
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(30 * 1000)) { console.warn('sync already running'); return; }
  try { fn(); } finally { lock.releaseLock(); }
}

// Returns true when the current month pushed OK.
function pushCurrentMonth_() {
  SpreadsheetApp.flush();  // make sure the sender's Log writes are visible
  const now = new Date();
  const y = Number(Utilities.formatDate(now, SYNC.tz, 'yyyy'));
  const m = Number(Utilities.formatDate(now, SYNC.tz, 'M'));
  const d = Number(Utilities.formatDate(now, SYNC.tz, 'd'));
  const ok = pushMonth(y, m, true);
  if (d <= 2) pushMonth(m === 1 ? y - 1 : y, m === 1 ? 12 : m - 1, false);
  return ok;
}

/**
 * Push one month tab, e.g. pushMonth(2026, 11) -> "Nov 2026". Safe to run by hand.
 * withPayments: send the Payments tab too. Only for the current month: the tab only
 * ever holds this month's amounts (the server ignores it for other months anyway).
 */
function pushMonth(year, month, withPayments) {
  const ss = SpreadsheetApp.getActive();
  const tab = Utilities.formatDate(new Date(year, month - 1, 15), SYNC.tz, 'MMM yyyy');
  const sh = ss.getSheetByName(tab);
  if (!sh) { writeSyncLog_(tab, 'skip', 'tab not found', ''); return false; }

  const monthKey = year + '-' + ('0' + month).slice(-2);
  const lastRow = sh.getLastRow(), lastCol = sh.getLastColumn();
  if (lastRow < SYNC.firstDataRow || lastCol < SYNC.firstDateCol) return false;

  // Date columns: header cells that are real dates inside this month.
  const header = sh.getRange(SYNC.headerRow, SYNC.firstDateCol, 1, lastCol - SYNC.firstDateCol + 1).getValues()[0];
  const dateCols = [];   // 0-based index into a full data row
  const dates = [];
  header.forEach((h, i) => {
    if (!(h instanceof Date)) return;
    const iso = Utilities.formatDate(h, SYNC.tz, 'yyyy-MM-dd');
    if (iso.slice(0, 7) !== monthKey || dates.indexOf(iso) !== -1) return;
    dateCols.push(SYNC.firstDateCol - 1 + i);
    dates.push(iso);
  });
  if (!dates.length) { writeSyncLog_(tab, 'skip', 'no date headers found', ''); return false; }

  // Display values = exactly what the kitchen sees (formulas resolved, errors as "#N/A").
  const grid = sh.getRange(SYNC.firstDataRow, 1, lastRow - SYNC.firstDataRow + 1, lastCol).getDisplayValues();
  const monitors = monitorNumbers_();
  const rows = [];
  let skippedMonitors = 0;
  grid.forEach((r, i) => {
    const dog = String(r[SYNC.cols.dog - 1]).trim();
    const phone = String(r[SYNC.cols.phone - 1]).trim();
    if (!dog && !phone) return;
    // Same normalisation the WhatsApp sender uses, so phones match the Log tab.
    const phoneNorm = normalisePhone_(phone);
    if (monitors[phoneNorm]) { skippedMonitors++; return; }   // internal monitor rows: never billed
    rows.push({
      row: SYNC.firstDataRow + i,
      owner: String(r[SYNC.cols.owner - 1]).trim(),
      dog: dog,
      phone: phone,
      cells: dateCols.map(c => String(r[c])),
    });
  });

  const payload = {
    month: monthKey,
    sheet_tab: tab,
    dates: dates,
    keywords: readKeywords_(ss),
    rows: rows,
    messages: readMessageIds_(ss, dates, monitors),
    payments: withPayments ? readPayments_(ss, monitors) : [],
  };
  const res = post_(payload, SYNC.url);
  if (res.code !== 200) { writeSyncLog_(tab, res.code, res.text.slice(0, 2000), ''); return false; }
  writeSyncLog_(tab, res.code, JSON.stringify(res.json.stats), formatIssues_(res.json.issues));
  return true;
}

// Payments: Owner | WhatsApp number | Amount due (₹) | Send reminder | Dog  (found by header)
function readPayments_(ss, monitors) {
  const sh = ss.getSheetByName(SYNC.paymentsTab);
  if (!sh || sh.getLastRow() < 2) return [];
  const range = sh.getDataRange();
  const shown = range.getDisplayValues(), raw = range.getValues();
  const h = shown[0].map(x => String(x).trim().toLowerCase());
  const col = test => h.findIndex(test);
  const oCol = col(x => x.indexOf('owner') === 0), pCol = col(x => x.indexOf('whatsapp') === 0 || x.indexOf('number') === 0);
  const aCol = col(x => x.indexOf('amount') === 0), dCol = col(x => x === 'dog');
  if (pCol === -1 || aCol === -1) return [];
  const out = [];
  raw.slice(1).forEach((r, i) => {
    const phone = String(shown[i + 1][pCol]).trim();
    if (!phone || monitors[normalisePhone_(phone)]) return;
    const a = r[aCol];
    const amount = typeof a === 'number' ? a : (String(a).replace(/[^0-9.]/g, '') === '' ? null : Number(String(a).replace(/[^0-9.]/g, '')));
    out.push({
      row: i + 2,
      owner: oCol === -1 ? '' : String(shown[i + 1][oCol]).trim(),
      phone: phone,
      dog: dCol === -1 ? '' : String(shown[i + 1][dCol]).trim(),
      amount: amount,
    });
  });
  return out;
}

function readKeywords_(ss) {
  const sh = ss.getSheetByName(SYNC.keywordsTab);
  if (!sh || sh.getLastRow() < 2) return [];
  const v = sh.getDataRange().getDisplayValues();
  const h = v[0].map(x => String(x).trim().toLowerCase());
  // Our Keywords header is 'Reason (finishes: "…because ___")', so match by prefix.
  const kCol = h.indexOf('keyword'), rCol = h.findIndex(x => x.indexOf('reason') === 0);
  const bCol = h.findIndex(x => x.replace('?', '') === 'billable');
  const mCol = h.findIndex(x => x === 'billing');
  if (kCol === -1) return [];
  return v.slice(1)
    .filter(r => String(r[kCol]).trim())
    .map(r => ({
      keyword: String(r[kCol]).trim(),
      reason: rCol === -1 ? null : String(r[rCol]).trim(),
      billable: bCol !== -1 && /^(y|yes|true|1)$/i.test(String(r[bCol]).trim()),
      billing: mCol === -1 ? null : (String(r[mCol]).trim().toLowerCase() || null),
    }));
}

function readMessageIds_(ss, dates, monitors) {
  if (!SYNC.log) return [];
  const sh = ss.getSheetByName(SYNC.log.tab);
  if (!sh || sh.getLastRow() < SYNC.log.firstRow) return [];
  const L = SYNC.log;
  const want = {};
  dates.forEach(d => want[d] = true);
  const v = sh.getRange(L.firstRow, 1, sh.getLastRow() - L.firstRow + 1, sh.getLastColumn()).getValues();
  const out = [];
  v.forEach(r => {
    const ts = r[L.timestampCol - 1];
    if (!(ts instanceof Date)) return;
    const date = Utilities.formatDate(ts, SYNC.tz, 'yyyy-MM-dd');
    if (!want[date]) return;
    const type = String(r[L.typeCol - 1]).trim();
    if (type !== 'MEAL' && type.indexOf('NOT DELIVERED') !== 0) return;  // skips tests "(test)", reminders, photo/logo rows
    if (String(r[L.resultCol - 1]).trim() !== 'SENT') return;
    const id = String(r[L.messageIdCol - 1]).trim();
    const dog = String(r[L.dogCol - 1]).trim();
    const phone = String(r[L.phoneCol - 1]).trim();   // already normalised by the sender, e.g. 919876543210
    if (id.indexOf('wamid') !== 0 || !dog || dog === '-' || monitors[phone]) return;
    out.push({ phone: phone, dog: dog, date: date, wa_message_id: id });
  });
  return out;
}

// MONITOR_NUMBERS script property -> { "919876543210": true, … }
function monitorNumbers_() {
  const raw = PropertiesService.getScriptProperties().getProperty('MONITOR_NUMBERS') || '';
  const out = {};
  raw.split(/[,;\n]/).map(x => normalisePhone_(x)).filter(Boolean).forEach(p => out[p] = true);
  return out;
}

function post_(payload, url) {
  const secret = PropertiesService.getScriptProperties().getProperty('KMCHO_SYNC_SECRET');
  if (!secret) throw new Error('Script property KMCHO_SYNC_SECRET is not set');
  const body = JSON.stringify(payload);
  const ts = String(Math.floor(Date.now() / 1000));
  const sig = Utilities.computeHmacSha256Signature(ts + '.' + body, secret, Utilities.Charset.UTF_8)
    .map(b => ('0' + (b & 0xff).toString(16)).slice(-2)).join('');

  const r = UrlFetchApp.fetch(url, {
    method: 'post',
    contentType: 'application/json; charset=utf-8',
    payload: body,
    headers: { 'X-KMCho-Timestamp': ts, 'X-KMCho-Signature': sig },
    muteHttpExceptions: true,
  });
  const code = r.getResponseCode();
  const text = r.getContentText();
  let json = null;
  if (code === 200) { try { json = JSON.parse(text); } catch (e) {} }
  return { code: code, text: text, json: json };
}

// NEW = user/dog the sync just created (check once). OK = amount set/changed. Others need fixing.
function formatIssues_(issues) {
  return issues.map(i => {
    const tag = i.issue.indexOf('created') === 0 ? 'NEW ' : (i.level === 'info' ? 'OK ' : '');
    const where = (i.tab ? i.tab + ' ' : '') + 'row ' + i.row + ' ';
    const who = (i.owner ? i.owner + ' / ' : '') + (i.dog || '') + (i.date ? ' ' + i.date : '');
    const extra = i.issue === 'amount_changed' ? ' (' + i.old + ' -> ' + i.new + ')'
      : i.issue === 'amount_set' ? ' (' + i.amount + ')' : '';
    return tag + where + who + ': ' + i.issue + extra;
  }).join('\n');
}

function writeSyncLog_(tab, code, stats, issues) {
  const ss = SpreadsheetApp.getActive();
  let sh = ss.getSheetByName(SYNC.syncLogTab);
  if (!sh) {
    sh = ss.insertSheet(SYNC.syncLogTab);
    sh.appendRow(['Time', 'Tab', 'Status', 'Stats', 'Issues']);
    sh.setFrozenRows(1);
  }
  sh.appendRow([new Date(), tab, code, stats, String(issues).slice(0, 45000)]);
  const extra = sh.getLastRow() - 1 - SYNC.syncLogMaxRows;
  if (extra > 0) sh.deleteRows(2, extra);
}

/** Run once. Replaces any previous sync/billing triggers (leaves the sender's alone). */
function installSyncTrigger() {
  ScriptApp.getProjectTriggers()
    .filter(t => ['pushCurrentMonth', 'runNightlyBilling'].indexOf(t.getHandlerFunction()) >= 0)
    .forEach(t => ScriptApp.deleteTrigger(t));
  ScriptApp.newTrigger('pushCurrentMonth').timeBased().everyHours(1).create();
  ScriptApp.newTrigger('runNightlyBilling')
    .timeBased().everyDays(1).atHour(SYNC.billingHour).nearMinute(0).inTimezone(SYNC.tz).create();
}