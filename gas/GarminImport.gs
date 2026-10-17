/**
 * Garmin のレポートメール → スプレッドシート（人生管理OS の体調分析用）
 * - 自分の Google アカウントで動く。Gmail は読むだけ（ラベル付け・削除・送信はしない）
 * - 書くのは、このスクリプトが入っているスプレッドシートの「Garmin」タブだけ
 * - 同じメールは2回取り込まない（メールの ID で判定）
 */
var TAB = 'Garmin';
var HEADER = ['日付', '期間', '項目', '値', '受信日時', 'メールID'];
var QUERY = 'from:garmin.com (ウェルネスレポート OR フィットネスレポート)';
var MAX_THREADS = 100;  // 1回に見るメールのまとまりの数（最初の取り込みで多すぎると時間切れになるため）

/** 毎朝の取り込み（トリガーから呼ばれる。手で実行してもよい） */
function importGarmin() {
  var sheet = sheet_();
  var done = doneIds_(sheet);
  var out = [];
  var threads = GmailApp.search(QUERY, 0, MAX_THREADS);
  threads.forEach(function (th) {
    th.getMessages().forEach(function (msg) {
      var id = msg.getId();
      if (done[id]) return;
      var text = msg.getPlainBody();
      var at = msg.getDate();
      var rows = parseWellness_(text, at).concat(parseFitness_(text, at));
      if (rows.length === 0) {
        console.warn('読み取れなかったメール: ' + msg.getSubject() + '（' + at + '）');
        return;  // 読めなかったメールは記録しない（形式が変わったときに、あとで直して取り込み直せるように）
      }
      rows.forEach(function (r) { out.push(r.concat([at, id])); });
    });
  });
  if (out.length) sheet.getRange(sheet.getLastRow() + 1, 1, out.length, HEADER.length).setValues(out);
  console.log('取り込んだ行: ' + out.length);
}

/** 最初に1回だけ手で実行: 毎朝 7時台に importGarmin が動くようにする */
function setupTrigger() {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'importGarmin') ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger('importGarmin').timeBased().everyDays(1).atHour(7).inTimezone('Asia/Tokyo').create();
}

function sheet_() {
  var book = SpreadsheetApp.getActiveSpreadsheet();
  var sh = book.getSheetByName(TAB) || book.insertSheet(TAB);
  if (sh.getLastRow() === 0) sh.appendRow(HEADER);
  return sh;
}

function doneIds_(sheet) {
  var ids = {};
  var n = sheet.getLastRow() - 1;
  if (n > 0) sheet.getRange(2, 6, n, 1).getValues().forEach(function (r) { ids[r[0]] = true; });
  return ids;
}

var MONTHS_EN = {Jan:1,Feb:2,Mar:3,Apr:4,May:5,Jun:6,Jul:7,Aug:8,Sep:9,Oct:10,Nov:11,Dec:12};

function minutes_(h, m) { return (h ? Number(h) : 0) * 60 + Number(m); }
function num_(s) { return Number(String(s).replace(/,/g, '')); }
function ymd_(d) { return Utilities.formatDate(d, 'Asia/Tokyo', 'yyyy-MM-dd'); }

// メールの日付から年を決める（年をまたぐ週の対策: 受信日より先の日付なら前の年）
function dateNear_(month, day, mailDate) {
  var d = new Date(mailDate.getFullYear(), month - 1, day);
  if (d.getTime() > mailDate.getTime() + 7 * 864e5) d = new Date(mailDate.getFullYear() - 1, month - 1, day);
  return d;
}

function dur_(text, label) {  // 「8h 32m」「16m」→ 分
  var m = text.match(new RegExp(label + '\\s*(?:(\\d+)h\\s*)?(\\d+)m'));
  return m ? minutes_(m[1], m[2]) : null;
}

/** ウィークリーウェルネスレポート → 行の配列 [日付, 期間, 項目, 値] */
function parseWellness_(text, mailDate) {
  var w = text.match(/(\d{1,2})-(\d{1,2})\s*(\d{1,2})月のGarmin Connectウィークリーウェルネスレポート/);
  if (!w) return [];
  var start = ymd_(dateNear_(Number(w[3]), Number(w[1]), mailDate));
  var rows = [], m;
  if ((m = text.match(/安静時心拍数[\s\S]*?(\d+)\s*安静時平均/))) rows.push([start, '週', '安静時心拍', num_(m[1])]);
  if ((m = text.match(/ストレス（一日の平均）[\s\S]*?(\d+)\s*全体的なストレスレベル/))) rows.push([start, '週', 'ストレス', num_(m[1])]);
  if ((m = text.match(/(?:(\d+)h\s*)?(\d+)m\s*平均全睡眠時間/))) rows.push([start, '週', '睡眠(分)', minutes_(m[1], m[2])]);
  if ((m = text.match(/(\d+)\s*brpm\s*睡眠時の平均/))) rows.push([start, '週', '呼吸・睡眠時', num_(m[1])]);
  if ((m = text.match(/(\d+)\s*brpm\s*非睡眠時の平均/))) rows.push([start, '週', '呼吸・非睡眠時', num_(m[1])]);
  var bb = /[月火水木金土日]曜日,\s*(\d{1,2})\s*(\d{1,2})月\s*チャージ\s*\+(\d+)\s*消費\s*-(\d+)/g;
  while ((m = bb.exec(text))) {  // Body Battery は日ごと
    var day = ymd_(dateNear_(Number(m[2]), Number(m[1]), mailDate));
    rows.push([day, '日', 'BodyBatteryチャージ', num_(m[3])]);
    rows.push([day, '日', 'BodyBattery消費', num_(m[4])]);
  }
  return rows;
}

/** 週ごとのフィットネスレポート → 行の配列 */
function parseFitness_(text, mailDate) {
  var w = text.match(/フィットネスレポート（(\d{1,2})\s+([A-Za-z]{3})）/);
  if (!w || !MONTHS_EN[w[2]]) return [];
  var start = ymd_(dateNear_(MONTHS_EN[w[2]], Number(w[1]), mailDate));
  var rows = [], m;
  // アクティビティ: 「2 ウォーキング 1.23 km … タイム 19:00」
  var act = /(\d+)\s+([^\s\d][^\n]*?)\s+([\d.]+)\s*km[\s\S]*?タイム\s*([\d:]+)/g;
  var head = text.split('健康統計情報')[0];
  while ((m = act.exec(head))) {
    var t = m[4].split(':').map(Number);  // 「19:00」=19分、「1:05:00」=1時間5分
    var mins = t.length === 3 ? t[0] * 60 + t[1] + t[2] / 60 : t[0] + t[1] / 60;
    rows.push([start, '週', m[2].trim() + '・回数', num_(m[1])]);
    rows.push([start, '週', m[2].trim() + '・km', num_(m[3])]);
    rows.push([start, '週', m[2].trim() + '・分', Math.round(mins)]);
  }
  var stats = text.split('健康統計情報')[1] || '';
  if ((m = stats.match(/ステップ数\s*([\d,]+)/))) rows.push([start, '週', '歩数', num_(m[1])]);
  if ((m = stats.match(/週間運動量\s*([\d,]+)/))) rows.push([start, '週', '週間運動量', num_(m[1])]);
  if ((m = stats.match(/カロリー\s*([\d,]+)/))) rows.push([start, '週', 'カロリー', num_(m[1])]);
  [['睡眠(分)', '(?:^|\\n|%)\\s*睡眠'], ['レム睡眠(分)', 'レム睡眠'], ['深い睡眠(分)', '深い睡眠'],
   ['浅い睡眠(分)', '浅い睡眠'], ['起床(分)', '起床'], ['ストレス低(分)', 'ストレス低'],
   ['ストレス中(分)', '中ストレス'], ['ストレス高(分)', 'ストレス高'], ['安静時ストレス(分)', '安静時ストレス']
  ].forEach(function (p) { var v = dur_(stats, p[1]); if (v !== null) rows.push([start, '週', p[0], v]); });
  if ((m = stats.match(/安静時心拍\s*(\d+)\s*bpm/))) rows.push([start, '週', '安静時心拍', num_(m[1])]);
  if ((m = stats.match(/(?:^|\n|%)\s*ストレス\s*(\d+)/))) rows.push([start, '週', 'ストレス', num_(m[1])]);
  return rows;
}
