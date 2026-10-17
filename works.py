"""作品管理シート（人生のすべてのプロジェクト。SPEC §5 03_CREATIVE「作品管理シート」）。

- 正本はユーザーが手で管理している `WORKS_SHEET_ID` の `WORKS_TAB`（既定 `Main`）タブ。列は見出しの名前で探す
- Bot が書いてよいのは `今やる？` `工程` `ストップ理由` `最終更新日` `次やること` の5列だけ（`締切` は読むだけ）（`完了度` は進捗バーの数式なので書かない）。
  行の追加・削除・並べ替えはしない。書く直前にその行のタイトルを読み直し、違えば書かない。書く前に操作ログ（`14-ai`）に残す
- シートは1回の操作につき1回だけ読み、5分間覚える（Sheets API の読み取り上限。SPEC §7）
"""
from __future__ import annotations

import logging
import re
import threading
import time
import unicodedata
from datetime import date, timedelta

import config
import sheets
import util

log = logging.getLogger("life-os.works")

COL_NO, COL_NOW, COL_KIND, COL_TITLE, COL_STATUS = "#", "今やる？", "プロジェクト", "タイトル", "状況"
COL_STAGE, COL_STOP, COL_UPDATED = "工程", "ストップ理由", "最終更新日"
COL_NEXT, COL_DEADLINE = "次やること", "締切"  # 締切は読むだけ（SPEC「プロジェクトボード」）
WRITABLE = (COL_NOW, COL_STAGE, COL_STOP, COL_UPDATED, COL_NEXT)
_KEYS = {COL_NOW: "now", COL_STAGE: "stage", COL_STOP: "stop", COL_UPDATED: "updated", COL_NEXT: "next"}
NOW_MAX = 3
YES, NO = "YES", "-"
CONTINUING_KINDS = ("私生活",)  # 完成の無い「続ける」型（要件定義書 3-9）
SOURCE_PREFIX = "作品#"
_TTL = 300.0

_lock = threading.RLock()
_cache: dict = {"at": 0.0, "header": [], "projects": []}
_ws = None


class WorksError(Exception):
    """シートに書けなかった（タイトルが変わっていた・列が無いなど）。メッセージはそのまま返信に使う。"""


def enabled() -> bool:
    return bool(config.WORKS_SHEET_ID)


# ---------------------------------------------------------------- 純粋関数


def _num(v) -> int | None:
    s = unicodedata.normalize("NFKC", str(v or "")).replace(",", "").strip()
    try:
        return int(float(s)) if s else None
    except ValueError:
        return None


def parse(values: list[list[str]]) -> tuple[list[str], list[dict]]:
    """シートの値（1行目が見出し）→ (見出し, プロジェクトの一覧)。タイトルが空の行は無視する。"""
    if not values:
        return [], []
    header = [str(h).strip() for h in values[0]]
    idx = {h: i for i, h in enumerate(header) if h}

    def get(row, name):
        i = idx.get(name)
        return str(row[i]).strip() if i is not None and i < len(row) and row[i] is not None else ""

    out = []
    for r, row in enumerate(values[1:], start=2):
        title = get(row, COL_TITLE)
        if not title:
            continue
        out.append({
            "row": r, "no": _num(get(row, COL_NO)), "title": title, "now": get(row, COL_NOW).upper() == YES,
            "kind": get(row, COL_KIND), "status": get(row, COL_STATUS), "stage": get(row, COL_STAGE),
            "stop": get(row, COL_STOP), "chars": _num(get(row, "現在の文字数")), "target": _num(get(row, "目標文字数")),
            "url": get(row, "URL"), "sheet_url": get(row, "シート"), "monetize": get(row, "収益化"),
            "updated": get(row, COL_UPDATED), "next": get(row, COL_NEXT), "deadline": get(row, COL_DEADLINE),
        })
    return header, out


_STRIP = re.compile(r"[\s『』「」【】（）()\"'“”‘’・!！?？〜~:：、。,.]")


def norm(text: str) -> str:
    return _STRIP.sub("", unicodedata.normalize("NFKC", str(text or "")).lower())


_NO_RE = re.compile(r"^[#＃]?\s*(\d+)\s*番?$")


def find(query: str, projects: list[dict]) -> list[dict]:
    """「4番」「#4」「4」なら # の番号で、それ以外はタイトルの一部で探す（記号・空白は無視）。完全一致があればそれだけ。"""
    q = unicodedata.normalize("NFKC", str(query or "")).strip()
    m = _NO_RE.match(q)
    if m:
        return [p for p in projects if p["no"] == int(m.group(1))]
    key = norm(q)
    if not key:
        return []
    exact = [p for p in projects if norm(p["title"]) == key]
    return exact or [p for p in projects if key in norm(p["title"])]


def label(p: dict) -> str:
    return f"#{p['no']} {p['title']}" if p.get("no") is not None else p["title"]


def is_continuing(p: dict) -> bool:
    return p.get("kind", "") in CONTINUING_KINDS


def source_of(p: dict) -> str:
    return f"{SOURCE_PREFIX}{p['no']}"


def task_content(p: dict, what: str) -> str:
    return f"{p['title']}｜{what}"


def week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def week_count(p: dict, tasks: list[dict], day: date) -> int:
    """続ける型: 今週（月曜〜。活動日）完了した、そのプロジェクトのタスクの数。"""
    start = week_start(day)
    n = 0
    for t in tasks:
        d = util.parse_date_any(t.get("done_date", ""))
        if (t.get("done") and t.get("source") == source_of(p) and str(t.get("content", "")).startswith(p["title"])
                and d and start <= d <= day):
            n += 1
    return n


def chars_text(p: dict) -> str:
    if p.get("chars") is not None and p.get("target"):
        return f"{p['chars']:,}／{p['target']:,}字"
    return ""


def line(p: dict, tasks: list[dict] | None = None, day: date | None = None) -> str:
    """一覧の1行。続ける型は今週の回数、それ以外は工程（と文字数）。"""
    bits = [b for b in (p.get("kind"), p.get("status")) if b]
    head = f"・{label(p)}" + (f"（{' / '.join(bits)}）" if bits else "")
    if is_continuing(p):
        if tasks is not None and day is not None:
            return head + f" 今週 {week_count(p, tasks, day)}回"
        return head
    tail = [x for x in (f"工程: {p['stage']}" if p.get("stage") else "", chars_text(p)) if x]
    if p.get("stop"):
        tail.append(f"止まっている理由: {p['stop']}")
    return head + ((" " + " / ".join(tail)) if tail else "")


def now_projects(projects: list[dict]) -> list[dict]:
    return [p for p in projects if p["now"]]


def morning_section(projects: list[dict], tasks: list[dict], day: date) -> str | None:
    """朝の案内の「📂 今やるプロジェクト」。タイトルと工程（続ける型は今週の回数）、次やること、まだ来ていない最初の締切。"""
    import board  # 循環を避ける
    rows = []
    for p in now_projects(projects):
        if is_continuing(p):
            row = f"・{p['title']}（今週 {week_count(p, tasks, day)}回）"
        else:
            row = f"・{p['title']}" + (f"（{p['stage']}）" if p.get("stage") else "")
        extra = [x for x in (f"次: {p['next']}" if p.get("next") else "", board.next_milestone(p, day)) if x]
        rows.append(row + (" ／ " + " ／ ".join(extra) if extra else ""))
    return ("📂 今やるプロジェクト：\n" + "\n".join(rows)) if rows else None


# ---------------------------------------------------------------- シートの読み書き


def _worksheet():
    global _ws
    if _ws is None:
        _ws = sheets.client().open_by_key(config.WORKS_SHEET_ID).worksheet(config.WORKS_TAB)
    return _ws


def load(refresh: bool = False) -> list[dict]:
    """`Main` タブのプロジェクト一覧（5分間覚える）。"""
    with _lock:
        if not refresh and time.time() - _cache["at"] < _TTL:
            return [dict(p) for p in _cache["projects"]]
        header, projects = parse(_worksheet().get_all_values())
        _cache.update(at=time.time(), header=header, projects=projects)
        return [dict(p) for p in projects]


def clear_cache() -> None:
    with _lock:
        _cache.update(at=0.0, header=[], projects=[])


def write(p: dict, op: str, **fields) -> None:
    """決まった5列だけを書く。fields のキーは列名（今やる？/工程/ストップ理由/最終更新日/次やること）。"""
    bad = [k for k in fields if k not in WRITABLE]
    if bad:
        raise ValueError(f"作品管理シートの {bad} には書けません")
    with _lock:
        ws_ = _worksheet()
        header = [str(h).strip() for h in ws_.row_values(1)]
        missing = [k for k in fields if k not in header]
        if missing:
            raise WorksError(f"作品管理シートに「{'」「'.join(missing)}」の列が見当たりません（見出しの名前を確かめてください）")
        current = ws_.row_values(p["row"])
        t_col = header.index(COL_TITLE) if COL_TITLE in header else None
        title_now = current[t_col].strip() if t_col is not None and t_col < len(current) else ""
        if title_now != p["title"]:
            clear_cache()
            raise WorksError(f"表の並びが変わっていたので、書き込みをやめました（{p['row']}行目は『{title_now or '空'}』）。もう一度どうぞ")
        before = {k: (current[header.index(k)] if header.index(k) < len(current) else "") for k in fields}
        sheets.log_operation(op, f"作品管理シート/{config.WORKS_TAB}", p["row"], before, fields)  # 書けなければ変更しない
        from gspread.utils import rowcol_to_a1
        text = [{"range": rowcol_to_a1(p["row"], header.index(k) + 1), "values": [[v]]}
                for k, v in fields.items() if k != COL_UPDATED]
        date_ = [{"range": rowcol_to_a1(p["row"], header.index(k) + 1), "values": [[v]]}
                 for k, v in fields.items() if k == COL_UPDATED]
        if text:
            ws_.batch_update(text, value_input_option="RAW")
        if date_:
            ws_.batch_update(date_, value_input_option="USER_ENTERED")  # 日付として入れる（ほかの行と同じ形）
        for cp in _cache["projects"]:
            if cp["row"] == p["row"]:
                for k, v in fields.items():
                    cp[_KEYS[k]] = (v == YES) if k == COL_NOW else v


def today_text() -> str:
    return util.fmt_date(util.activity_day()).replace("-", "/")


def touch(p: dict, op: str = "最終更新日（作品管理シート）") -> None:
    write(p, op, **{COL_UPDATED: today_text()})


def touch_tasks(tasks: list[dict]) -> list[dict]:
    """プロジェクトのタスク（出典 `作品#番号`）が完了したら、そのプロジェクトの最終更新日を書く。
    完了したタスクが `次やること` と同じなら、`次やること` を空にする（SPEC「プロジェクトボード」）。
    書いたプロジェクトを返す。失敗しても止めない。"""
    if not enabled():
        return []
    nos = {t.get("source", "")[len(SOURCE_PREFIX):]: t for t in tasks
           if str(t.get("source", "")).startswith(SOURCE_PREFIX)}
    if not nos:
        return []
    touched = []
    try:
        projects = load()
        for no, t in nos.items():
            for p in projects:
                content = str(t.get("content", ""))
                if str(p["no"]) == no and content.startswith(p["title"]):
                    what = content[len(p["title"]):].lstrip("｜|").strip()
                    fields = {COL_UPDATED: today_text()}
                    if p.get("next") and norm(p["next"]) == norm(what):
                        fields[COL_NEXT] = ""
                    write(p, "最終更新日（作品管理シート）" if len(fields) == 1 else "完了したので次やることを空にする（作品管理シート）",
                          **fields)
                    touched.append(p)
    except Exception:  # noqa: BLE001
        log.warning("作品管理シートの最終更新日を書けませんでした", exc_info=True)
    return touched


_GID = re.compile(r"[#&?]gid=(\d+)")


def outline(p: dict, limit: int = 15) -> list[str]:
    """`シート` 列が同じブックのタブを指していれば、その「見出し1/2/3 ＋ 完了」の表のうち、未完了の行。"""
    url = p.get("sheet_url", "")
    m = _GID.search(url)
    if not m or config.WORKS_SHEET_ID not in url:
        return []
    try:
        ws_ = _worksheet().spreadsheet.get_worksheet_by_id(int(m.group(1)))
        values = ws_.get_all_values()
    except Exception:  # noqa: BLE001
        log.warning("作品ごとのタブを読めませんでした", exc_info=True)
        return []
    return parse_outline(values)[:limit]


def parse_outline(values: list[list[str]]) -> list[str]:
    head = next((i for i, r in enumerate(values) if "見出し1" in [c.strip() for c in r] and "完了" in [c.strip() for c in r]), None)
    if head is None:
        return []
    cols = [c.strip() for c in values[head]]
    hcols = [cols.index(h) for h in ("見出し1", "見出し2", "見出し3") if h in cols]
    done = cols.index("完了")
    out = []
    for r in values[head + 1:]:
        name = next((r[i].strip() for i in reversed(hcols) if i < len(r) and r[i].strip()), "")
        flag = r[done].strip().upper() if done < len(r) else ""
        if name and flag != "TRUE":
            out.append(name)
    return out
