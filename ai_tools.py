"""`14-ai` の tool_use で使うツール（SPEC §5）。総合秘書が、シートとノートを検索・参照・変更する。

安全のための決まり（コードで強制するもの）:
- Obsidian の読み書きは、すべて `notes.GuardedStore`（`notes_policy`）を通す。読み取りは Vault 全体、書き込みは `06-Life-OS/` の中だけ。
  ツール側で許可の判定を持たない（許可外は AccessDenied → ツールのエラーとして、モデルに伝える）
- シートの変更・削除は、実行**前**に `14-ai` シート（操作ログ）へ変更前の値を書く。書けなければ変更しない
- 変更・削除は、対象の行に含まれる文字（`expect`）を添えさせ、行がずれていたら実行しない（古い行番号で別の行を消さないため）
- 1回のメッセージで実行できる変更は 10 件、削除は 5 件まで
- 操作ログのシートと `13-im-the-ceo` のシートは、変更できない。タスクの削除はできない（Obsidian のタスク棚で行う）
- 方針の部屋（`13-im-the-ceo`）と、タスク棚のファイルには、ノートの追記ができない（方針は最優先の指示として全ての AI 呼び出しに入るため、
  ノートやシートに書かれた文章から書き換えられないようにする）
- 執筆記録シートは読み取りだけ
- 「システムのカスタマイズ」は `data/settings.json`（許可されたキーだけ）。コードは変えない
"""
from __future__ import annotations

import asyncio
import json
import logging
import unicodedata

import claude_client
import notes
import notes_policy
import settings
import sheets
import task_sync
import util
import vault_paths
import writing_log

log = logging.getLogger("life-os.ai_tools")

MAX_RESULT_CHARS = 12_000  # 1回のツールの結果としてモデルに返す文字数の上限
MAX_ROWS = 100
MAX_WRITES = 10
MAX_DELETES = 5
NOTE_READ_LIMIT = 8_000
DATE_COLS = ("日付", "日時", "登録日", "発行日")

READ_ONLY_SHEETS = {
    sheets.OPLOG: "操作ログ（14-ai シート）は Bot だけが書き込みます",
    sheets.DIRECTIVES: "13-im-the-ceo のシートは CEO-Directives.md と同期しているため、13-im-the-ceo の部屋に投稿して追加してください",
}
NO_DELETE_SHEETS = {sheets.TASKS: "タスクの削除は、Obsidian のタスク棚（01 Life-OS-Task.md）でその行を消してください（消すと同期されます）"}
NOTE_WRITE_DENIED = {
    notes_policy.TASK_FILE: "タスクのファイルにノートは追記できません。タスクは append_row（01-today-task）で追加してください",
}


class ToolError(Exception):
    """モデルに伝えるエラー（メッセージは、モデルがユーザーに説明できる日本語にする）。"""


# ---------------------------------------------------------------- ツールの定義（Claude API の tools）

_SHEET = {"type": "string", "description": "シート名。チャンネル名と同じ（例: 07-ledger, 06-household-accounts, 01-today-task）"}
_EXPECT = {"type": "string", "description": "対象の行に含まれている文字（内容や日付の一部）。直前に読んだ行の内容から取る。行がずれていたら実行しない"}

TOOLS: list[dict] = [
    {"name": "search_sheet", "description": "シートの全行から、言葉（空白区切りは AND）を含む行を探す。行番号つきで返す。",
     "input_schema": {"type": "object", "properties": {"sheet": _SHEET, "query": {"type": "string"}, "limit": {"type": "integer"}},
                      "required": ["sheet", "query"]}},
    {"name": "read_sheet_rows", "description": "シートの行を読む。日付の範囲（YYYY-MM-DD）か、新しい方から last_n 件。行番号つきで返す。",
     "input_schema": {"type": "object", "properties": {"sheet": _SHEET, "from_date": {"type": "string"}, "to_date": {"type": "string"},
                                                       "last_n": {"type": "integer"}}, "required": ["sheet"]}},
    {"name": "append_row", "description": "シートに1行追加する。values は {列名: 値}。01-today-task は 内容・実行予定日・期限・優先度 だけ指定する。",
     "input_schema": {"type": "object", "properties": {"sheet": _SHEET, "values": {"type": "object"}}, "required": ["sheet", "values"]}},
    {"name": "update_row", "description": "シートの1行を修正する。values は 変更する列だけの {列名: 新しい値}。",
     "input_schema": {"type": "object", "properties": {"sheet": _SHEET, "row": {"type": "integer"}, "values": {"type": "object"},
                                                       "expect": _EXPECT}, "required": ["sheet", "row", "values", "expect"]}},
    {"name": "delete_row", "description": "シートの1行を削除する（操作ログに残るので、undo_operation で戻せる）。",
     "input_schema": {"type": "object", "properties": {"sheet": _SHEET, "row": {"type": "integer"}, "expect": _EXPECT},
                      "required": ["sheet", "row", "expect"]}},
    {"name": "undo_operation", "description": "操作ログ（14-ai シート）の行番号を指定して、その操作（追記・修正・削除・設定変更）を取り消す。",
     "input_schema": {"type": "object", "properties": {"log_row": {"type": "integer"}}, "required": ["log_row"]}},
    {"name": "search_notes", "description": "Obsidian のノートを全文検索する。scope は life_os（Bot の部屋のノート。既定）か vault（Vault 全体）。",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}, "scope": {"type": "string", "enum": ["life_os", "vault"]},
                                                       "limit": {"type": "integer"}}, "required": ["query"]}},
    {"name": "read_note", "description": "Obsidian のノートを読む（Vault 内のどこでも可）。path は Vault からの相対パス。",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "append_note", "description": "ノートの末尾に追記する（06-Life-OS/ の中だけ。ほかは実行されない）。ファイルが無ければ作る。",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "text": {"type": "string"}}, "required": ["path", "text"]}},
    {"name": "read_writing_progress", "description": "執筆記録シートを読む（読み取りだけ）。work を省くと全作品の一覧。",
     "input_schema": {"type": "object", "properties": {"work": {"type": "string"}}}},
    {"name": "update_setting", "description": "システムのカスタマイズ（実行時設定）を変える。key は summary_length（08-scrap の要約の長さ。例: 2行）/ "
                                            "scrap_tag_count（08-scrap のタグの数。例: 3〜5個）/ idea_tags（09-idea のタグ候補。文字列の配列）/ "
                                            "private_tags（05-private のタグ候補。文字列の配列）。配列は、変更後の全体を渡す。",
     "input_schema": {"type": "object", "properties": {"key": {"type": "string"}, "value": {}}, "required": ["key", "value"]}},
]
TOOL_NAMES = {t["name"] for t in TOOLS}


# ---------------------------------------------------------------- 純粋関数


def norm(text) -> str:
    return unicodedata.normalize("NFKC", str(text)).strip().lower()


def resolve_sheet(name) -> str:
    """シート名（チャンネル名）に解決する。`ledger` のような短い呼び方も、1つに決まるときだけ受け付ける。"""
    n = norm(name)
    for s in sheets.SCHEMA:
        if s.lower() == n:
            return s
    hits = [s for s in sheets.SCHEMA if n and n in s.lower()]
    if len(hits) == 1:
        return hits[0]
    raise ToolError(f"シート名が決まりません: {name!r}。使えるシート: {', '.join(sheets.SCHEMA)}")


def date_column(sheet: str) -> str | None:
    return next((c for c in sheets.SCHEMA[sheet] if c in DATE_COLS), None)


def visible(rec: dict) -> dict:
    """空でない列だけ。"""
    return {k: v for k, v in rec.items() if str(v).strip()}


def row_text(rec: dict) -> str:
    return " ".join(str(v) for v in rec.values())


def same(a, b) -> bool:
    """セルの値が同じか（数値は、カンマや円の違いを無視する）。"""
    if str(a).strip() == str(b).strip():
        return True
    x, y = util.to_int(a), util.to_int(b)
    return x is not None and x == y


def coerce(sheet: str, col: str, value):
    """モデルの渡した値をセルに入れる形にする。金額は数値、真偽値は TRUE/空。"""
    if isinstance(value, bool):
        return "TRUE" if value else ""
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        raise ToolError(f"列「{col}」の値は、文字か数字にしてください")
    if col == "金額（円）":
        n = util.to_int(str(value).replace("¥", "").replace("￥", ""))
        return n if n is not None else str(value)
    return value if isinstance(value, (int, float)) else str(value)


def clean_values(sheet: str, values, *, forbid: tuple[str, ...] = ()) -> dict:
    if not isinstance(values, dict) or not values:
        raise ToolError("values には {列名: 値} を1つ以上渡してください")
    cols = sheets.SCHEMA[sheet]
    bad = [k for k in values if k not in cols]
    if bad:
        raise ToolError(f"シート {sheet} に列 {bad} はありません。列: {cols}")
    denied = [k for k in values if k in forbid]
    if denied:
        raise ToolError(f"列 {denied} は変更できません")
    return {k: coerce(sheet, k, v) for k, v in values.items()}


def clean_setting(key, value):
    """update_setting の値を検証する。許可されたキーだけ。配列は文字列の配列（先頭の # は取る）。"""
    if key not in settings.ALLOWED_KEYS:
        raise ToolError(f"変更できる設定は {sorted(settings.ALLOWED_KEYS)} だけです")
    if isinstance(settings.DEFAULTS[key], list):
        if not isinstance(value, list) or not value or len(value) > 20:
            raise ToolError(f"{key} は、1〜20個の文字列の配列にしてください（変更後の全体を渡す）")
        out = []
        for v in value:
            t = str(v).strip().lstrip("#＃").strip()
            if not t or len(t) > 20 or not isinstance(v, str):
                raise ToolError(f"{key} の要素は、20文字以内の文字列にしてください")
            if t not in out:
                out.append(t)
        return out
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 30:
        raise ToolError(f"{key} は、30文字以内の文字列にしてください")
    return value.strip()


def cap(text: str, limit: int = MAX_RESULT_CHARS) -> str:
    return text if len(text) <= limit else text[:limit] + "\n…（長いので、ここまでです。範囲を絞ってもう一度読んでください）"


def short(rec: dict, n: int = 40) -> str:
    t = " ".join(str(v).strip() for v in rec.values() if str(v).strip())
    return t if len(t) <= n else t[:n] + "…"


# ---------------------------------------------------------------- ツールの実行


class Toolbox:
    """1回のメッセージの間だけ使う。実行した操作を覚えておき、返信の末尾の「操作ログ」に使う。"""

    def __init__(self, today=None):
        self.today = today or util.today()
        self.ops: list[tuple[str, bool]] = []  # (表示する1行, 取り消せるか)
        self._writes = 0
        self._deletes = 0

    async def execute(self, name: str, args: dict):
        if name not in TOOL_NAMES:
            raise ToolError(f"未知のツールです: {name}")
        try:
            out = await asyncio.to_thread(getattr(self, "_t_" + name), **(args or {}))
        except notes_policy.AccessDenied as e:
            raise ToolError(f"許可されていない操作です（書き込みは 06-Life-OS/ の中だけ）: {e}") from None
        except (ToolError, TypeError):
            raise
        except Exception as e:  # noqa: BLE001  シート・Drive の一時的な失敗など。モデルにそのまま伝える
            log.warning("ツール %s が失敗しました", name, exc_info=True)
            raise ToolError(f"{type(e).__name__}: {e}") from None
        return out if isinstance(out, str) else cap(json.dumps(out, ensure_ascii=False))

    def footer(self) -> str:
        """返信の末尾に付ける操作ログ（モデルの文章とは別に、実行した変更を必ず示す）。"""
        if not self.ops:
            return ""
        lines = ["🗂️ 操作ログ（14-ai シートに記録しました）"] + [f"・{t}" for t, _ in self.ops]
        if any(u for _, u in self.ops):
            lines.append("取り消したいときは「さっきの取り消して」と送ってください。")
        return "\n\n" + "\n".join(lines)  # 返信の本文との間に、空行を1行入れる

    # ---- シートの読み取り

    def _t_search_sheet(self, sheet, query, limit=20):
        name = resolve_sheet(sheet)
        words = norm(query).split()
        if not words:
            raise ToolError("query が空です")
        hits = [(r, rec) for r, rec in sheets.records(name) if all(w in norm(row_text(rec)) for w in words)]
        n = min(max(int(limit), 1), MAX_ROWS)
        return {"sheet": name, "columns": sheets.SCHEMA[name], "found": len(hits),
                "rows": [{"row": r, "values": visible(rec)} for r, rec in hits[-n:]]}

    def _t_read_sheet_rows(self, sheet, from_date=None, to_date=None, last_n=30):
        name = resolve_sheet(sheet)
        rows = sheets.records(name)
        if from_date or to_date:
            col = date_column(name)
            if col is None:
                raise ToolError(f"シート {name} には日付の列がありません。last_n で読んでください")
            lo, hi = util.parse_date_any(from_date or ""), util.parse_date_any(to_date or "")
            if (from_date and lo is None) or (to_date and hi is None):
                raise ToolError("日付は YYYY-MM-DD で指定してください")
            rows = [(r, rec) for r, rec in rows
                    if (d := util.parse_date_any(rec.get(col, ""))) and (lo is None or d >= lo) and (hi is None or d <= hi)]
        n = min(max(int(last_n), 1), MAX_ROWS)
        return {"sheet": name, "columns": sheets.SCHEMA[name], "total_in_range": len(rows),
                "rows": [{"row": r, "values": visible(rec)} for r, rec in rows[-n:]]}

    # ---- シートの変更（変更前に操作ログを書く）

    def _take_write(self, delete: bool = False) -> None:
        if self._writes >= MAX_WRITES:
            raise ToolError(f"1回のメッセージで実行できる変更は {MAX_WRITES} 件までです。続きは、もう一度送ってください")
        if delete and self._deletes >= MAX_DELETES:
            raise ToolError(f"1回のメッセージで削除できるのは {MAX_DELETES} 件までです。対象を確認してから、分けて送ってください")
        self._writes += 1
        self._deletes += 1 if delete else 0

    def _writable(self, sheet) -> str:
        name = resolve_sheet(sheet)
        if name in READ_ONLY_SHEETS:
            raise ToolError(READ_ONLY_SHEETS[name])
        return name

    def _find(self, name: str, row) -> dict:
        try:
            row = int(row)
        except (TypeError, ValueError):
            raise ToolError("row は行番号（整数）で指定してください") from None
        rec = dict(sheets.records(name)).get(row)
        if rec is None:
            raise ToolError(f"{name} の {row} 行目に、データがありません。search_sheet か read_sheet_rows で、行番号を確かめてください")
        return rec

    @staticmethod
    def _check_expect(rec: dict, row, expect) -> None:
        if not isinstance(expect, str) or not norm(expect):
            raise ToolError("expect（対象の行に含まれている文字）を渡してください")
        if norm(expect) not in norm(row_text(rec)):
            raise ToolError(f"{row} 行目の内容が「{expect}」と一致しません（今の内容: {short(rec, 80)}）。"
                            "行がずれた可能性があるので、読み直してから、もう一度実行してください")

    def _t_append_row(self, sheet, values):
        name = self._writable(sheet)
        if name == sheets.TASKS:
            vals = clean_values(name, values, forbid=("ID", "登録日", "完了", "完了日", "出典"))
            content = str(vals.get("内容", "")).strip()
            if not content:
                raise ToolError("タスクには 内容 が必要です")
            self._take_write()
            sheets.log_operation("追記（タスク）", name, "", "", vals)
            tid = sheets.add_task(content, scheduled=str(vals.get("実行予定日", "")), due=str(vals.get("期限", "")),
                                  priority=str(vals.get("優先度", "")), source="14-ai")
            task_sync.run_safely()
            self.ops.append((f"{name} にタスクを追加（{short(vals)}）", False))
            return {"ok": True, "task_id": tid, "note": "タスク棚（Obsidian）にも同期しました。タスクの追加は、取り消し操作の対象外です"}
        vals = clean_values(name, values)
        self._take_write()
        log_row = sheets.log_operation("追記", name, "", "", vals)
        row = sheets.append(name, vals)
        try:  # 追記する行の番号は、書いてからでないと分からない。取り消しに使うので、ログの「行」に補う
            if row and row > 1:
                sheets.update_row(sheets.OPLOG, log_row, {"行": row})
        except Exception:  # noqa: BLE001  補えなくても、追記は済んでいる（この操作だけ、取り消しできなくなる）
            log.warning("操作ログの行番号を補えませんでした", exc_info=True)
        self.ops.append((f"{name} に1行追加（{short(vals)}）", True))
        return {"ok": True, "row": row, "log_row": log_row}

    def _apply_update(self, name: str, row: int, vals: dict) -> None:
        sheets.update_row(name, row, vals)
        if name == sheets.TASKS:
            task_sync.run_safely()  # シートの変更を、Obsidian のタスク棚に反映する

    def _t_update_row(self, sheet, row, values, expect):
        name = self._writable(sheet)
        rec = self._find(name, row)
        self._check_expect(rec, row, expect)
        vals = clean_values(name, values, forbid=("ID",) if name == sheets.TASKS else ())
        before = {k: rec.get(k, "") for k in vals}
        if all(same(before[k], vals[k]) for k in vals):
            return {"ok": True, "note": "すでにその内容なので、変更はありません"}
        self._take_write()
        log_row = sheets.log_operation("修正", name, int(row), before, vals)
        self._apply_update(name, int(row), vals)
        self.ops.append((f"{name} {row}行目を修正（{short(before)} → {short(vals)}）", True))
        return {"ok": True, "row": int(row), "log_row": log_row, "before": before}

    def _t_delete_row(self, sheet, row, expect):
        name = self._writable(sheet)
        if name in NO_DELETE_SHEETS:
            raise ToolError(NO_DELETE_SHEETS[name])
        rec = self._find(name, row)
        self._check_expect(rec, row, expect)
        self._take_write(delete=True)
        log_row = sheets.log_operation("削除", name, int(row), visible(rec), "")
        sheets.delete_row(name, int(row))
        self.ops.append((f"{name} {row}行目を削除（{short(rec)}）", True))
        return {"ok": True, "deleted": visible(rec), "log_row": log_row,
                "note": "これより下の行の番号は1つ小さくなりました。続けて操作するときは、読み直してください"}

    def _t_undo_operation(self, log_row):
        try:
            log_row = int(log_row)
        except (TypeError, ValueError):
            raise ToolError("log_row は操作ログの行番号（整数）で指定してください") from None
        entries = dict(sheets.records(sheets.OPLOG))
        e = entries.get(log_row)
        if e is None:
            raise ToolError(f"操作ログの {log_row} 行目がありません。read_sheet_rows で 14-ai シートを読んで、行番号を確かめてください")
        op, sheet, row = str(e["操作"]).strip(), str(e["シート"]).strip(), util.to_int(e["行"])
        if any(str(x["操作"]).startswith(f"取り消し（ログ{log_row}）") for x in entries.values()):
            raise ToolError(f"ログ {log_row} の操作は、すでに取り消してあります")
        before, after = _loads(e["変更前"]), _loads(e["変更後"])
        if op in ("追記", "修正", "削除") and row is None:
            raise ToolError(f"ログ {log_row} には行番号が記録されていないため、取り消せません。シートで直接直してください")
        self._take_write()
        undo_op = f"取り消し（ログ{log_row}）"
        if op == "追記":
            name = resolve_sheet(sheet)
            rec = self._find(name, row)
            if not all(same(rec.get(k, ""), v) for k, v in after.items()):
                raise ToolError(f"{name} の {row} 行目は、その後に変わっているため、取り消せません（今の内容: {short(rec, 80)}）")
            sheets.log_operation(undo_op, name, row, visible(rec), "")
            sheets.delete_row(name, row)
            text = f"{name} {row}行目の追加を取り消し（削除）"
        elif op == "修正":
            name = resolve_sheet(sheet)
            rec = self._find(name, row)
            if not all(same(rec.get(k, ""), v) for k, v in after.items()):
                raise ToolError(f"{name} の {row} 行目は、その後に変わっているため、取り消せません（今の内容: {short(rec, 80)}）")
            sheets.log_operation(undo_op, name, row, {k: rec.get(k, "") for k in before}, before)
            self._apply_update(name, row, before)
            text = f"{name} {row}行目の修正を取り消し（元の値に戻しました）"
        elif op == "削除":
            name = resolve_sheet(sheet)
            last = max((r for r, _ in sheets.records(name)), default=1)
            at = min(row or 2, last + 1)
            sheets.log_operation(undo_op, name, at, "", before)
            sheets.insert_row(name, at, before)
            text = f"{name} の削除を取り消し（{at}行目に戻しました）"
        elif op == "設定変更":
            key = str(e["行"]).strip()
            if sheet != "settings":
                raise ToolError("この設定の取り消しには対応していません")
            restored = clean_setting(key, before)  # 設定の値は JSON で記録してある（文字列も "…" の形）
            if settings.get(key) != after:
                raise ToolError(f"設定 {key} は、その後に変わっているため、取り消せません")
            sheets.log_operation(undo_op, "settings", key, json.dumps(after, ensure_ascii=False), json.dumps(restored, ensure_ascii=False))
            settings.set_value(key, restored)
            text = f"設定 {key} の変更を取り消し"
        else:
            raise ToolError(f"この操作（{op}）は、取り消しに対応していません")
        self.ops.append((text, False))
        return {"ok": True, "note": text}

    # ---- ノート

    def _t_search_notes(self, query, scope="life_os", limit=10):
        if not norm(query):
            raise ToolError("query が空です")
        where = notes_policy.LIFEOS_DIR if scope != "vault" else ""
        hits = notes.get_store().search(str(query), min(max(int(limit), 1), 20), where)
        return {"scope": scope, "found": len(hits), "notes": [{"path": h["path"], "snippet": h["snippet"]} for h in hits]}

    def _t_read_note(self, path):
        text = notes.get_store().read(path)
        if text is None:
            raise ToolError(f"ノートが見つかりません: {path}")
        return cap(text, NOTE_READ_LIMIT)

    def _t_append_note(self, path, text):
        if not isinstance(text, str) or not text.strip():
            raise ToolError("text が空です")
        if not isinstance(path, str) or not path.endswith(".md"):
            raise ToolError("path は .md のノートにしてください")
        p = notes_policy.normalize(path)
        if p in NOTE_WRITE_DENIED:
            raise ToolError(NOTE_WRITE_DENIED[p])
        if p.startswith(vault_paths.room("ceo") + "/"):
            raise ToolError("方針（13-im-the-ceo）のノートには、ここから追記できません。13-im-the-ceo の部屋に投稿して追加してください")
        store = notes.get_store()
        notes_policy.check(p, "write")  # 許可外なら、ログを書く前にここで止まる
        self._take_write()
        sheets.log_operation("ノート追記", p, "", "", text[:2000])
        store.append_entry(p, text.strip(), p.rsplit("/", 1)[-1][:-3])
        self.ops.append((f"{p} に追記（{text.strip()[:40]}{'…' if len(text.strip()) > 40 else ''}）", False))
        return {"ok": True, "path": p, "note": "ノートへの追記は、取り消し操作の対象外です"}

    # ---- 執筆記録（読み取りだけ）・設定

    def _t_read_writing_progress(self, work=None):
        wl = writing_log.fetch()
        if wl is None:
            raise ToolError("執筆記録シートが設定されていません")
        latest = wl.snaps[-1] if wl.snaps else None
        if work:
            p = writing_log.progress_summary(wl, str(work), self.today)
            if p is None:
                raise ToolError(f"作品「{work}」を執筆記録シートの作品と照合できません。作品: {[w.name for w in wl.works.values()]}")
            return writing_log.format_progress(p)
        return {"as_of": str(latest.day) if latest else None,
                "works": [{"name": w.name, "total": latest.totals.get(w.name, 0) if latest else 0, "target": w.target,
                           "deadline": str(w.deadline) if w.deadline else None} for w in wl.works.values()]}

    def _t_update_setting(self, key, value):
        new = clean_setting(key, value)
        old = settings.get(key)
        if old == new:
            return {"ok": True, "note": "すでにその設定です"}
        self._take_write()
        sheets.log_operation("設定変更", "settings", key, json.dumps(old, ensure_ascii=False), json.dumps(new, ensure_ascii=False))
        settings.set_value(key, new)
        self.ops.append((f"設定 {key}: {_show(old)} → {_show(new)}", True))
        return {"ok": True, "key": key, "before": old, "after": new, "note": "次の投稿から反映されます"}


def _loads(cell):
    """操作ログのセル（JSON）を読み戻す。JSON でなければ、そのままの文字列。"""
    try:
        return json.loads(cell)
    except (TypeError, ValueError):
        return cell


def _show(v) -> str:
    return "、".join(v) if isinstance(v, list) else str(v)
