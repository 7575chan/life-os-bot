/**
 * Garmin と Habitify のレポートメール → スプレッドシート（人生管理OS の体調・習慣の分析用）
 * - 自分の Google アカウントで動く。Gmail は読むだけ（ラベル付け・削除・送信はしない）
 * - 書くのは、このスクリプトが入っているスプレッドシートの「Garmin」「Habitify」タブだけ
 * - 同じメールは2回取り込まない（メールの ID で判定）
 * - Habitify で習慣を増やしても、メールに載っていれば自動で取り込まれる（習慣の名前はメールのまま）
 */
var HEADER = ['日付', '期間', '項目', '値', '受信日時', 'メールID'];
var SOURCES = [
  {tab: 'Garmin', query: 'from:garmin.com (ウェルネスレポート OR フィットネスレポート)',
   parse: function (text, at, subject) { return parseGarmin_(text, at, subject); }},
  {tab: 'Habitify', query: 'from:weekly@insights.habitify.com',
   parse: function (text, at, subject) { return parseHabitify_(text, at); }}
];
var MAX_THREADS = 100;  // 1回に見るメールのまとまりの数（最初の取り込みで多すぎると時間切れになるため）

/** 毎朝の取り込み（トリガーから呼ばれる。手で実行してもよい） */
function importGarmin() {
  SOURCES.forEach(function (src) {
    try {
      importSource_(src);
    } catch (e) {  // 片方が失敗しても、もう片方は取り込む
      console.error(src.tab + ' の取り込みに失敗しました: ' + e);
    }
  });
}

function importSource_(src) {
  var sheet = sheet_(src.tab);
  var done = doneIds_(sheet);
  var out = [];
  var threads = GmailApp.search(src.query, 0, MAX_THREADS);
  console.log(src.tab + ': 見つかったメールのまとまり ' + threads.length + ' 件');
  threads.forEach(function (th) {
    th.getMessages().forEach(function (msg) {
      var id = msg.getId();
      if (done[id]) return;
      var at = msg.getDate();
      var rows = src.parse(msg.getPlainBody(), at, msg.getSubject());
      if (rows === null) return;  // 取り込まない種類のメール（月ごとのレポートなど）
      if (rows.length === 0) {
        console.warn('読み取れなかったメール: ' + msg.getSubject() + '（' + at + '）');
        return;  // 読めなかったメールは記録しない（形式が変わったときに、あとで直して取り込み直せるように）
      }
      rows.forEach(function (r) { out.push(r.concat([at, id])); });
    });
  });
  if (out.length) sheet.getRange(sheet.getLastRow() + 1, 1, out.length, HEADER.length).setValues(out);
  console.log(src.tab + ' に取り込んだ行: ' + out.length);
}

/**
 * 読み取れないときの確認用（手で実行する。シートには何も書かない）。
 * いちばん新しいウェルネス・週のフィットネス・Habitify のメールの、件名・本文の先頭・取り出せた行の数を実行ログに出す
 */
function debugMail() {
  [['ウェルネス', 'from:garmin.com ウェルネスレポート'],
   ['フィットネス（週）', 'from:garmin.com 週ごとの フィットネスレポート'],
   ['Habitify', 'from:weekly@insights.habitify.com']].forEach(function (q) {
    var threads = GmailApp.search(q[1], 0, 1);
    if (threads.length === 0) { console.log(q[0] + ': メールが見つかりませんでした'); return; }
    var msg = threads[0].getMessages().pop();
    var text = msg.getPlainBody(), at = msg.getDate(), subject = msg.getSubject();
    var rows = q[0] === 'Habitify' ? parseHabitify_(text, at) : parseGarmin_(text, at, subject);
    console.log('==== ' + q[0] + ' ====\n件名: ' + subject + '\n取り出せた行: ' + (rows ? rows.length : '対象外')
                + '\n本文の先頭:\n' + text.slice(0, 2500));
  });
}

/** 最初に1回だけ手で実行: 毎朝 7時台に importGarmin が動くようにする */
function setupTrigger() {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'importGarmin') ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger('importGarmin').timeBased().everyDays(1).atHour(7).inTimezone('Asia/Tokyo').create();
}

function sheet_(tab) {
  var book = SpreadsheetApp.getActiveSpreadsheet();
  var sh = book.getSheetByName(tab) || book.insertSheet(tab);
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

/**
 * Garmin のメール → 行の配列。月ごとのレポートは null（取り込まない）。週は件名（無ければ本文）から読む:
 * 「5-11 10月の…ウェルネス」「28 9月 - 4 10月の…ウェルネス」「フィットネスレポート（28 Sep）」「（12 Jul - 18 Jul）」
 */
function parseGarmin_(text, mailDate, subject) {
  text = String(text).replace(/\r/g, '').replace(/[\u00a0\u3000]/g, ' ');
  var head = String(subject || '') + '\n' + text.slice(0, 600);
  if (/^\s*\d{1,2}\s*月のGarmin Connectフィットネスレポート/.test(subject || '')) return null;
  var start = weekFromText_(head, mailDate);
  if (!start) return [];
  if (/ウェルネス/.test(head)) return parseWellness_(text, start);
  if (/フィットネス/.test(head)) return parseFitness_(text, start);
  return [];
}

function weekFromText_(s, mailDate) {
  var m;
  if ((m = s.match(/(\d{1,2})\s*(\d{1,2})\s*月\s*[-–—]\s*\d{1,2}\s*\d{1,2}\s*月/)))  // 28 9月 - 4 10月
    return ymd_(dateNear_(Number(m[2]), Number(m[1]), mailDate));
  if ((m = s.match(/(\d{1,2})\s*[-–—]\s*\d{1,2}\s*(\d{1,2})\s*月/)))  // 5-11 10月
    return ymd_(dateNear_(Number(m[2]), Number(m[1]), mailDate));
  if ((m = s.match(/[（(]\s*(\d{1,2})\s+([A-Za-z]{3})/)) && MONTHS_EN[m[2]])  // （28 Sep） / （12 Jul - 18 Jul）
    return ymd_(dateNear_(MONTHS_EN[m[2]], Number(m[1]), mailDate));
  return null;
}

/** ウィークリーウェルネスレポート → 行の配列 [日付, 期間, 項目, 値]（start は週の月曜） */
function parseWellness_(text, start) {
  var mailDate = new Date(start.replace(/-/g, '/'));
  mailDate.setDate(mailDate.getDate() + 7);
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

/** 週ごとのフィットネスレポート → 行の配列（start は週の月曜） */
function parseFitness_(text, start) {
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

/** Habitify の週次レポート → 行の配列。全体の達成率・習慣ごとの完了率・できた日数・合計（分か回）*/
function parseHabitify_(text, mailDate) {
  text = String(text).replace(/\r/g, '').replace(/[\u00a0\u3000]/g, ' ');
  // 週の初日: 「9月 14 - 9月 20」（無ければ、受信日の前の週の月曜）
  var w = text.match(/(\d{1,2})\s*月\s*(\d{1,2})\s*日?\s*[-–—~〜]\s*(\d{1,2})\s*月\s*(\d{1,2})/);
  var startDate;
  if (w) {
    startDate = dateNear_(Number(w[1]), Number(w[2]), mailDate);
  } else {
    startDate = new Date(mailDate.getFullYear(), mailDate.getMonth(), mailDate.getDate());
    startDate.setDate(startDate.getDate() - ((startDate.getDay() + 6) % 7) - 7);
  }
  var start = ymd_(startDate);
  var rows = [], m;
  if ((m = text.match(/達成率は\s*([\d.]+)\s*%/))) rows.push([start, '週', '全体の達成率(%)', Number(m[1])]);
  // 「29%」→ 習慣名 →「2/7 日 (10 分)」（間に空行やリンクの行があってもよい）
  var re = /(\d+(?:\.\d+)?)\s*%[ \t]*\n(?:[ \t]*\n)*[ \t]*([^\n]+?)[ \t]*\n(?:[ \t]*\n)*[ \t]*(\d+)\s*\/\s*(\d+)\s*日\s*[(（]\s*(\d+)\s*(分|回)\s*[)）]/g;
  while ((m = re.exec(text))) {
    var name = m[2].replace(/-icon$/, '').trim();
    rows.push([start, '週', name + '・完了率(%)', Number(m[1])]);
    rows.push([start, '週', name + '・日数', Number(m[3])]);
    rows.push([start, '週', name + '・' + m[6], Number(m[5])]);
  }
  return rows.length > 1 || (rows.length === 1 && w) ? rows : [];
}
