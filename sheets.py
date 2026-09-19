"""Google スプレッドシート (gspread) ラッパ。すべて同期関数。呼び出し側は asyncio.to_thread 経由で使う。

シート名は Discord のチャンネル名と同じ（SPEC §3.2）。ユーザーが作成済みのシートを使い、
列は位置ではなく**見出し行の列名**で読み書きする。チャンネルに対応しないシートには一切触れない。
"""
import functools
import logging
import re
import threading
import time
import uuid
from datetime import date, timedelta

import gspread
from gspread.utils import rowcol_to_a1

import config
import util

log = logging.getLogger("life-os.sheets")
_C = config.CHANNELS

TASKS = _C["today"]
HEALTH = _C["health"]
DIARY = _C["lookback"]
REPORTS = _C["report"]
PRIVATE = _C["private"]
HOUSEHOLD = _C["household"]
LEDGER = _C["ledger"]
ARTICLES = _C["scrap"]
IDEAS = _C["idea"]
PROJECT_SHEETS = {"novel": _C["novel"], "trpg": _C["trpg"], "others": _C["others"]}  # 部屋ごとに別シート
DIRECTIVES = _C["ceo"]
OPLOG = _C["ai"]

_PROJECT_COLUMNS = ["日時", "作品名", "種別", "内容"]

SCHEMA: dict[str, list[str]] = {
    TASKS: ["ID", "登録日", "内容", "完了", "実行予定日", "期限", "優先度", "出典", "完了日"],
    HEALTH: ["日時", "睡眠時間", "歩数・運動", "客観指標", "主観メモ", "AI判定", "体調スコア"],
    DIARY: ["日付", "ご機嫌度", "天気", "本文", "Obsidianリンク"],
    REPORTS: ["発行日", "種別", "本文"],
    PRIVATE: ["日時", "内容", "タグ"],
    HOUSEHOLD: ["日付", "金額（円）", "ジャンル", "備考", "区分"],
    LEDGER: ["日付", "種別", "金額（円）", "内容", "やよい転記", "分析シート転記"],
    ARTICLES: ["日時", "タイトル", "URL", "3行要約", "タグ", "Obsidianリンク"],
    IDEAS: ["日時", "タグ", "内容"],
    **{name: list(_PROJECT_COLUMNS) for name in PROJECT_SHEETS.values()},
    DIRECTIVES: ["日時", "内容"],
    OPLOG: ["日時", "操作", "シート", "行", "変更前", "変更後"],
}

DELETED = "削除"  # タスクの「完了」列に入れる。Obsidian 側で行が削除されたことを表す


class SheetError(RuntimeError):
    """シートの見出しが想定と違うなど、安全のために書き込みを止めたとき。"""


_lock = threading.RLock()
_book = None
_gc = None
_ok_sheets: set[str] = set()  # 見出しの検証に成功したシート


def locked(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        with _lock:
            return fn(*a, **kw)

    return wrapper


def client():
    """BackOffHTTPClient: 読み取り上限（1分あたり60回）に達して 429 が返ったとき、待ってから自動でやり直す。"""
    global _gc
    if _gc is None:
        _gc = gspread.service_account(filename=config.CREDENTIALS_FILE, http_client=gspread.BackOffHTTPClient)
    return _gc


def book():
    global _book
    if _book is None:
        _book = client().open_by_key(config.GOOGLE_SHEET_ID)
    return _book


# Sheets API の読み取り上限（1分あたり60回。VM の Bot・手元の作業・同期が同じ枠を共有する）を守るため、
# タブの情報（毎回1回の読み取りになる）と見出し行は、しばらく覚えておく。
_WS_TTL = 3600.0
_HEADER_TTL = 300.0
_ws_cache: dict[str, tuple[float, object]] = {}
_header_cache: dict[str, tuple[float, list[str]]] = {}


def ws(name: str):
    hit = _ws_cache.get(name)
    if hit and time.time() - hit[0] < _WS_TTL:
        return hit[1]
    w = book().worksheet(name)
    _ws_cache[name] = (time.time(), w)
    return w


def _header(name: str, refresh: bool = False) -> list[str]:
    """見出し行（シートを開いて確認した列名）。ユーザーが列を足したときのため、5分で入れ替える。"""
    hit = _header_cache.get(name)
    if hit and not refresh and time.time() - hit[0] < _HEADER_TTL:
        return hit[1]
    header = ws(name).row_values(1)
    _header_cache[name] = (time.time(), header)
    return header


def clear_caches() -> None:
    _ws_cache.clear()
    _header_cache.clear()


# ---------------------------------------------------------------- 見出し行（SPEC §3.2 ルール）


def plan_header(name: str, row1: list[str], has_data_below: bool) -> tuple[str, list[str]]:
    """見出し行に対する処置を決める純関数。(action, 書き込む列名)。

    action: "write"（見出しを書く）/ "append"（不足列を末尾に追加）/ "ok" / "refuse"（他の内容があるため書かない）
    """
    cols = SCHEMA[name]
    cells = [c.strip() for c in row1 if c.strip()]
    if not cells:
        return ("refuse", []) if has_data_below else ("write", cols)
    if cells == [name]:  # A1 にシート名だけ（タイトル代わり）→ 見出しに置き換える
        return ("refuse", []) if has_data_below else ("write", cols)
    known = [c for c in cells if c in cols]
    if not known:
        return "refuse", []
    missing = [c for c in cols if c not in cells]
    return ("append", missing) if missing else ("ok", [])


@locked
def ensure_schema() -> dict:
    """チャンネル名のシートの見出し行を整える。ユーザーの既存データは上書きしない。

    戻り値: {"created": [...], "headers_written": [...], "columns_added": {...}, "refused": [...]}
    """
    clear_caches()
    existing = {w.title: w for w in book().worksheets()}
    result = {"created": [], "headers_written": [], "columns_added": {}, "refused": []}
    for name, cols in SCHEMA.items():
        if name not in existing:
            w = book().add_worksheet(title=name, rows=1000, cols=max(len(cols), 10))
            w.append_row(cols, value_input_option="RAW")
            result["created"].append(name)
            _ok_sheets.add(name)
            continue
        w = existing[name]
        values = w.get_all_values()
        row1 = values[0] if values else []
        below = any(any(c.strip() for c in r) for r in values[1:])
        action, write_cols = plan_header(name, row1, below)
        if action == "write":
            w.batch_update([{"range": "A1", "values": [write_cols]}], value_input_option="RAW")
            result["headers_written"].append(name)
            _ok_sheets.add(name)
        elif action == "append":
            start = len(row1)
            w.batch_update([{"range": rowcol_to_a1(1, start + 1), "values": [write_cols]}], value_input_option="RAW")
            result["columns_added"][name] = write_cols
            _ok_sheets.add(name)
        elif action == "ok":
            _ok_sheets.add(name)
        else:
            result["refused"].append(name)
            log.warning("シート %r には想定外の内容があるため、書き込みません（SPEC §3.2）", name)
    return result


def _require(name: str) -> None:
    if name not in SCHEMA:
        raise KeyError(f"未知のシート: {name}")
    if name not in _ok_sheets:
        raise SheetError(f"シート {name!r} は見出しを確認できていないため使えません（ensure_schema の警告を確認してください）")


# ---------------------------------------------------------------- 汎用 CRUD（列名で読み書き）


@locked
def records(name: str) -> list[tuple[int, dict]]:
    """[(シート上の行番号(1始まり・見出し=1), {列名: 値})]"""
    _require(name)
    values = ws(name).get_all_values()
    if not values:
        return []
    idx = {h.strip(): i for i, h in enumerate(values[0]) if h.strip()}
    out = []
    for i, row in enumerate(values[1:], start=2):
        if not any(c.strip() for c in row):
            continue
        rec = {h: (row[j] if j < len(row) else "") for h, j in idx.items()}
        for h in SCHEMA[name]:
            rec.setdefault(h, "")
        out.append((i, rec))
    return out


@locked
def append(name: str, data: dict) -> int:
    """1行追記して行番号を返す。見出し行の列順に合わせる。"""
    _require(name)
    header = _header(name)
    if any(k not in header for k in data):
        header = _header(name, refresh=True)
    row = [""] * len(header)
    for k, v in data.items():
        if k in header:
            row[header.index(k)] = v
    res = ws(name).append_row(row, value_input_option="RAW")
    m = re.search(r"!A(\d+)", res.get("updates", {}).get("updatedRange", ""))
    return int(m.group(1)) if m else -1


@locked
def update_row(name: str, row: int, data: dict) -> None:
    _require(name)
    header = _header(name)
    if any(k not in header for k in data):
        header = _header(name, refresh=True)
    for k in data:
        if k not in header:
            raise KeyError(f"{name} に列 '{k}' はありません。列: {header}")
    ws(name).batch_update(
        [{"range": rowcol_to_a1(row, header.index(k) + 1), "values": [[v]]} for k, v in data.items()],
        value_input_option="RAW",
    )


@locked
def delete_row(name: str, row: int) -> None:
    if row < 2:
        raise ValueError("ヘッダー行は削除できません")
    ws(name).delete_rows(row)


@locked
def between(name: str, start: date, end: date, date_col: str | None = None) -> list[tuple[int, dict]]:
    """先頭列(または date_col)の日付が start <= d < end の行。"""
    col = date_col or SCHEMA[name][0]
    out = []
    for r, rec in records(name):
        d = util.parse_date_any(rec[col])
        if d and start <= d < end:
            out.append((r, rec))
    return out


@locked
def log_operation(op: str, sheet: str, row: int, before, after) -> None:
    append(OPLOG, {"日時": util.fmt_datetime(util.now()), "操作": op, "シート": sheet, "行": row,
                   "変更前": str(before), "変更後": str(after)})


# ---------------------------------------------------------------- タスク

_TASK_COLUMNS = {  # タスク dict のキー -> シートの列名
    "content": "内容", "scheduled": "実行予定日", "due": "期限", "priority": "優先度",
    "done_date": "完了日", "source": "出典", "created": "登録日",
}


def new_task_id(existing: set[str] | None = None) -> str:
    while True:
        tid = "lo-" + uuid.uuid4().hex[:8]  # 8桁の16進数（約43億通り）。既存の6桁の ID もそのまま有効
        if not existing or tid not in existing:
            return tid


def _task(row: int, rec: dict) -> dict:
    flag = rec["完了"].strip()
    return {"row": row, "id": rec["ID"].strip(), "content": rec["内容"].strip(), "done": util.is_done(flag),
            "deleted": flag == DELETED, "scheduled": rec["実行予定日"].strip(), "due": rec["期限"].strip(),
            "priority": rec["優先度"].strip(), "created": rec["登録日"].strip(), "done_date": rec["完了日"].strip(),
            "source": rec["出典"].strip()}


def task_sort_key(t: dict):
    """優先度「高」→ 期限が近い順 → 登録順。"""
    return (0 if t["priority"] == "高" else 1, t["due"] or "9999-99-99", t["row"])


@locked
def all_tasks() -> list[dict]:
    """ID の無い行には ID を付けて書き戻してから返す（シート上で手で追加された行への対応）。"""
    out, seen = [], set()
    rows = records(TASKS)
    ids = {rec["ID"].strip() for _, rec in rows if rec["ID"].strip()}
    for r, rec in rows:
        t = _task(r, rec)
        if not t["content"]:
            continue
        if not t["id"] or t["id"] in seen:
            t["id"] = new_task_id(ids)
            ids.add(t["id"])
            update_row(TASKS, r, {"ID": t["id"]})
        seen.add(t["id"])
        out.append(t)
    return out


@locked
def add_task(content: str, scheduled: str = "", due: str = "", priority: str = "", source: str = "",
             task_id: str | None = None, done: bool = False, done_date: str = "", created: str | None = None) -> str:
    tid = task_id or new_task_id()
    append(TASKS, {"ID": tid, "登録日": created or util.fmt_date(util.today()), "内容": content,
                   "完了": "TRUE" if done else "", "実行予定日": scheduled, "期限": due, "優先度": priority,
                   "出典": source, "完了日": done_date})
    return tid


@locked
def update_task(task_id: str, **fields) -> bool:
    """fields: content / done(bool) / scheduled / due / priority / done_date / source / deleted(bool)"""
    for t in all_tasks():
        if t["id"] == task_id:
            data = {}
            for k, v in fields.items():
                if k == "done":
                    data["完了"] = "TRUE" if v else ""
                elif k == "deleted":
                    data["完了"] = DELETED if v else ""
                else:
                    data[_TASK_COLUMNS[k]] = v
            update_row(TASKS, t["row"], data)
            return True
    return False


@locked
def find_tasks(ids: list[str]) -> list[dict]:
    by_id = {t["id"]: t for t in all_tasks()}
    return [by_id[i] for i in ids if i in by_id]


@locked
def today_tasks(day: date) -> list[dict]:
    """実行予定日が day 以前の未完了タスク（3日以内の持ち越しを含む）。優先度・期限順。"""
    out = []
    for t in all_tasks():
        d = util.parse_date_any(t["scheduled"])
        if d and d <= day and not t["done"] and not t["deleted"]:
            out.append(t)
    return sorted(out, key=task_sort_key)


@locked
def backlog_tasks(limit: int = 10) -> list[dict]:
    out = [t for t in all_tasks() if not t["scheduled"] and not t["done"] and not t["deleted"]]
    return sorted(out, key=task_sort_key)[:limit]


@locked
def schedule_tasks(ids: list[str], day: date) -> list[str]:
    done = []
    for t in find_tasks(ids):
        if not t["done"] and not t["deleted"]:
            update_row(TASKS, t["row"], {"実行予定日": util.fmt_date(day)})
            done.append(t["content"])
    return done


@locked
def complete_tasks(ids: list[str], day: date | None = None) -> list[str]:
    day = day or util.today()
    done = []
    for t in find_tasks(ids):
        if not t["done"] and not t["deleted"]:
            update_row(TASKS, t["row"], {"完了": "TRUE", "完了日": util.fmt_date(day)})
            done.append(t["content"])
    return done


@locked
def cleanup_stale(day: date) -> int:
    """実行予定日から3日以上経った未完了タスクを、通知なしでバックログへ戻す。"""
    n = 0
    for t in all_tasks():
        if not t["done"] and not t["deleted"] and util.is_stale(t["scheduled"], day):
            update_row(TASKS, t["row"], {"実行予定日": ""})
            n += 1
    return n


@locked
def completed_count(start: date, end: date) -> int:
    n = 0
    for t in all_tasks():
        d = util.parse_date_any(t["done_date"])
        if t["done"] and d and start <= d < end:
            n += 1
    return n


# ---------------------------------------------------------------- 日記・体調


@locked
def diary_on(day: date) -> dict | None:
    rows = between(DIARY, day, day + timedelta(days=1))
    return rows[-1][1] if rows else None


@locked
def health_score_on(day: date) -> int | None:
    scores = []
    for _, rec in between(HEALTH, day, day + timedelta(days=1)):
        s = util.to_int(rec["体調スコア"])
        if s:
            scores.append(s)
    return round(sum(scores) / len(scores)) if scores else None
