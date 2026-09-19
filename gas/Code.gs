/**
 * Life-OS 中継 (ウェブアプリ)。
 *
 * Bot (サービスアカウント) は容量が無く新規ファイルを作れないため、ユーザー権限で動くこのスクリプトに
 * 「新規作成・フォルダ作成・改名・ゴミ箱移動」だけを依頼する。SPEC.md §2.5 を参照。
 *
 * 安全のための設計:
 *  - 操作は create_file / create_bytes / rename / trash の4つだけ。読み取り・更新・一覧は持たない。
 *  - 対象は `06-Life-OS/` 配下のファイルだけ。それ以外のパス、`..` 等は denied を返して何もしない。
 *  - 既存ファイルは上書きしない (create は exists を返す)。フォルダは改名・ゴミ箱移動できない。
 *  - 共有トークン (Script Properties の RELAY_TOKEN) が一致しない依頼は実行しない。
 *  - request_id で再送を検出し、同じ依頼は前回の結果を返す (二重作成しない)。
 *
 * Script Properties:
 *  - RELAY_TOKEN       32バイト以上のランダム文字列 (Bot の GAS_RELAY_TOKEN と同じ値)
 *  - LIFEOS_FOLDER_ID  `06-Life-OS` フォルダの ID
 */

var LIFEOS_DIR = '06-Life-OS';
var MAX_BYTES = 5 * 1024 * 1024;
var CACHE_SECONDS = 21600; // 6時間

// ---------------------------------------------------------------- エントリポイント

function doPost(e) {
  var res;
  try {
    res = handle_(e);
  } catch (err) {
    res = { ok: false, status: 'error', message: String(err && err.message ? err.message : err) };
  }
  return json_(res);
}

// ブラウザで URL を開いたときの確認用。情報は返さない。
function doGet() {
  return json_({ ok: true, status: 'alive' });
}

function json_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}

// ---------------------------------------------------------------- 認証・重複検出・排他

function handle_(e) {
  var props = PropertiesService.getScriptProperties();
  var token = props.getProperty('RELAY_TOKEN');
  var rootId = props.getProperty('LIFEOS_FOLDER_ID');
  if (!token || !rootId) return { ok: false, status: 'error', message: 'not configured' };

  var req;
  try {
    req = JSON.parse(e.postData.contents);
  } catch (x) {
    return { ok: false, status: 'error', message: 'bad json' };
  }
  if (!safeEqual_(String(req.token || ''), token)) {
    return { ok: false, status: 'unauthorized', message: 'unauthorized' };
  }
  var requestId = String(req.request_id || '');
  if (!/^[0-9a-f]{16,64}$/.test(requestId)) {
    return { ok: false, status: 'error', message: 'bad request_id' };
  }

  var lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    var cache = CacheService.getScriptCache();
    var cached = cache.get('req:' + requestId);
    if (cached) return JSON.parse(cached);
    var res = dispatch_(req, rootId);
    if (res.status !== 'error') cache.put('req:' + requestId, JSON.stringify(res), CACHE_SECONDS);
    return res;
  } finally {
    lock.releaseLock();
  }
}

/** 文字列の定数時間比較 (長さの違いも含めて全文字を走査する)。 */
function safeEqual_(a, b) {
  var diff = a.length ^ b.length;
  var n = Math.max(a.length, b.length);
  for (var i = 0; i < n; i++) {
    diff |= (a.charCodeAt(i) || 0) ^ (b.charCodeAt(i) || 0);
  }
  return diff === 0;
}

// ---------------------------------------------------------------- パス検証 (純関数)

function Denied(message) {
  this.message = message;
}
Denied.prototype = Object.create(Error.prototype);
Denied.prototype.constructor = Denied;

/**
 * `06-Life-OS/...` のパスを検証し、`06-Life-OS` を除いた要素の配列を返す。
 * 許可外は Denied を投げる。Bot 側の notes_policy と同じ規則を再検証する (二重防御)。
 */
function validatePath_(path) {
  if (typeof path !== 'string' || path.length === 0) throw new Denied('path required');
  var p = path.normalize('NFC');
  if (/[\\\u0000-\u001f\u007f]/.test(p)) throw new Denied('invalid characters');
  if (p.charAt(0) === '/' || /^[A-Za-z]:/.test(p)) throw new Denied('absolute path');
  var parts = p.split('/');
  for (var i = 0; i < parts.length; i++) {
    if (parts[i] === '' || parts[i] === '.' || parts[i] === '..') throw new Denied('relative segment');
  }
  if (parts[0] !== LIFEOS_DIR) throw new Denied('outside ' + LIFEOS_DIR);
  if (parts.length < 2) throw new Denied('root is not allowed');
  return parts.slice(1);
}

// ---------------------------------------------------------------- 操作

function dispatch_(req, rootId) {
  try {
    switch (req.op) {
      case 'create_file':
        return createFile_(rootId, req.path, req.content == null ? '' : String(req.content), null);
      case 'create_bytes':
        return createFile_(rootId, req.path, null, req);
      case 'rename':
        return rename_(rootId, req.path, req.new_path);
      case 'trash':
        return trash_(rootId, req.path);
      default:
        return { ok: false, status: 'error', message: 'unknown op' };
    }
  } catch (err) {
    if (err instanceof Denied) return { ok: false, status: 'denied', message: err.message };
    throw err;
  }
}

function findChild_(folder, name, wantFolder) {
  var it = wantFolder ? folder.getFoldersByName(name) : folder.getFilesByName(name);
  return it.hasNext() ? it.next() : null;
}

function resolveFolder_(root, parts, create) {
  var cur = root;
  for (var i = 0; i < parts.length; i++) {
    var next = findChild_(cur, parts[i], true);
    if (!next) {
      if (!create) return null;
      next = cur.createFolder(parts[i]);
    }
    cur = next;
  }
  return cur;
}

function createFile_(rootId, path, text, bytesReq) {
  var parts = validatePath_(path);
  var name = parts.pop();
  var blob;
  if (bytesReq) {
    var bytes = Utilities.base64Decode(String(bytesReq.content_base64 || ''));
    if (bytes.length > MAX_BYTES) return { ok: false, status: 'error', message: 'too large' };
    blob = Utilities.newBlob(bytes, String(bytesReq.mime || 'application/octet-stream'), name);
  } else {
    blob = Utilities.newBlob(text, 'text/markdown', name);
    if (blob.getBytes().length > MAX_BYTES) return { ok: false, status: 'error', message: 'too large' };
  }
  var root = DriveApp.getFolderById(rootId);
  var parent = resolveFolder_(root, parts, true);
  var existing = findChild_(parent, name, false);
  if (existing) {
    return { ok: false, status: 'exists', id: existing.getId(), url: existing.getUrl(), message: 'already exists' };
  }
  var file = parent.createFile(blob);
  return { ok: true, status: 'created', id: file.getId(), url: file.getUrl() };
}

function rename_(rootId, path, newPath) {
  var srcParts = validatePath_(path);
  var dstParts = validatePath_(newPath);
  var srcName = srcParts.pop();
  var dstName = dstParts.pop();
  var root = DriveApp.getFolderById(rootId);
  var srcParent = resolveFolder_(root, srcParts, false);
  var file = srcParent ? findChild_(srcParent, srcName, false) : null;
  if (!file) return { ok: false, status: 'not_found', message: 'source not found' };
  var dstParent = resolveFolder_(root, dstParts, true);
  if (findChild_(dstParent, dstName, false)) {
    return { ok: false, status: 'exists', message: 'destination exists' };
  }
  if (dstParent.getId() !== srcParent.getId()) file.moveTo(dstParent);
  if (dstName !== srcName) file.setName(dstName);
  return { ok: true, status: 'renamed', id: file.getId(), url: file.getUrl() };
}

function trash_(rootId, path) {
  var parts = validatePath_(path);
  var name = parts.pop();
  var root = DriveApp.getFolderById(rootId);
  var parent = resolveFolder_(root, parts, false);
  var file = parent ? findChild_(parent, name, false) : null; // ファイルのみ。フォルダは対象外
  if (!file) return { ok: false, status: 'not_found', message: 'file not found' };
  file.setTrashed(true);
  return { ok: true, status: 'trashed', id: file.getId() };
}

// ---------------------------------------------------------------- エディタから実行する補助関数

/** 初回の権限承認用。エディタでこの関数を選んで実行し、Drive へのアクセスを承認する。 */
function authorize() {
  var rootId = PropertiesService.getScriptProperties().getProperty('LIFEOS_FOLDER_ID');
  if (!rootId) throw new Error('Script Properties に LIFEOS_FOLDER_ID を設定してください');
  Logger.log('OK: ' + DriveApp.getFolderById(rootId).getName());
}

/** パス検証の自己テスト。エディタで実行し、ログに FAIL が出ないことを確認する。 */
function selfTest() {
  var ok = [['06-Life-OS/09-idea/Ideas.md', '09-idea/Ideas.md'], ['06-Life-OS/a.md', 'a.md']];
  var bad = ['', '00inbox/x.md', '06-Life-OS', '06-Life-OS/', '06-Life-OS/../00inbox/x.md',
    '../06-Life-OS/x.md', '/06-Life-OS/x.md', 'C:/06-Life-OS/x.md', '06-Life-OS\\x.md',
    '06-Life-OS//x.md', '06-Life-OS/./x.md', '06-Life-OS-evil/x.md', '06-life-os/x.md',
    '01\uD83D\uDDC3Task/01 Life-OS-Task.md', '06-Life-OS/x.md\u0000'];
  var fails = 0;
  ok.forEach(function (c) {
    var got = validatePath_(c[0]).join('/');
    if (got !== c[1]) { fails++; Logger.log('FAIL ok: ' + c[0] + ' -> ' + got); }
  });
  bad.forEach(function (p) {
    try { validatePath_(p); fails++; Logger.log('FAIL bad (許可された): ' + JSON.stringify(p)); } catch (e) { /* 期待どおり */ }
  });
  if (!safeEqual_('abc', 'abc') || safeEqual_('abc', 'abd') || safeEqual_('abc', 'abcd')) {
    fails++; Logger.log('FAIL safeEqual_');
  }
  Logger.log(fails === 0 ? 'selfTest: すべて成功' : 'selfTest: 失敗 ' + fails + ' 件');
}
