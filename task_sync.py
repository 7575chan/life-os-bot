"""タスク棚の双方向同期: Obsidian `01🗃Task/01 Life-OS-Task.md` ⇄ スプレッドシート「タスク」（SPEC §6）。

Obsidian Tasks プラグインの記法で読み書きする。競合時は Obsidian を優先し、完了はどちらかが完了なら完了。
`plan()` は純関数（入出力なし）で、`run()` が実際の読み書きを行う。
"""
import hashlib
import json
import logging
import re
import threading
from dataclasses import dataclass, field
from datetime import date

import notes
import sheets
import state
import util
from notes_policy import TASK_FILE

log = logging.getLogger("life-os.task_sync")

SECTION = "## 🤖 Life-OS"
STALE_DONE_DAYS = 30
SNAPSHOT_KEY = "task_sync:snapshot"

_LINE = re.compile(r"^(?P<indent>\s*)[-*] \[(?P<mark>[ xX])\] (?P<body>.*)$")
_ID = re.compile(r"🆔\s*(\S+)")
_DATES = {"created": "➕", "start": "🛫", "scheduled": "⏳", "due": "📅", "done_date": "✅"}
_PRIO_IN = {"🔺": "高", "⏫": "高", "🔼": "中", "🔽": "低", "⏬": "低"}
_PRIO_OUT = {"高": "⏫", "中": "🔼", "低": "🔽"}
_lock = threading.Lock()


# ---------------------------------------------------------------- Tasks プラグイン記法


def parse_line(line: str) -> dict | None:
    """タスク行を辞書にする。タスク行でなければ None。未知のトークン（🔁 や #タグ）は説明文に残す。"""
    m = _LINE.match(line)
    if not m:
        return None
    body = m["body"]
    t = {"indent": m["indent"], "done": m["mark"] != " ", "id": "", "priority": "", "scheduled": "", "due": "",
         "created": "", "start": "", "done_date": ""}
    for key, emoji in _DATES.items():
        mm = re.search(rf"{emoji}️?\s*(\d{{4}}-\d{{2}}-\d{{2}})", body)
        if mm:
            t[key] = mm.group(1)
            body = body.replace(mm.group(0), " ")
    mm = _ID.search(body)
    if mm:
        t["id"] = mm.group(1)
        body = body.replace(mm.group(0), " ")
    for emoji, prio in _PRIO_IN.items():
        if emoji in body:
            t["priority"] = t["priority"] or prio
            body = body.replace(emoji + "️", " ").replace(emoji, " ")
    t["content"] = re.sub(r"\s+", " ", body).strip()
    return t


def render(t: dict) -> str:
    """Tasks プラグインの標準的な順序: 説明、🆔、優先度、➕、🛫、⏳、📅、✅。"""
    parts = [t["content"]]
    if t.get("id"):
        parts.append(f"🆔 {t['id']}")
    if t.get("priority") in _PRIO_OUT:
        parts.append(_PRIO_OUT[t["priority"]])
    for key in ("created", "start", "scheduled", "due", "done_date"):
        if t.get(key):
            parts.append(f"{_DATES[key]} {t[key]}")
    return f"{t.get('indent', '')}- [{'x' if t['done'] else ' '}] " + " ".join(parts)


def digest(t: dict) -> str:
    """同期で比較する項目だけのハッシュ（内容・完了・予定日・期限・優先度）。"""
    key = [re.sub(r"\s+", " ", t["content"]).strip(), bool(t["done"]), t.get("scheduled", ""), t.get("due", ""),
           t.get("priority", "")]
    return hashlib.sha1(json.dumps(key, ensure_ascii=False).encode()).hexdigest()[:12]


def _is_stale_done(t: dict, today: date) -> bool:
    d = util.parse_date_any(t.get("done_date", ""))
    return bool(t["done"] and d and (today - d).days > STALE_DONE_DAYS)


# ---------------------------------------------------------------- 同期計画（純関数）


@dataclass
class SyncPlan:
    text: str
    sheet_adds: list[dict] = field(default_factory=list)
    sheet_updates: list[tuple[str, dict]] = field(default_factory=list)
    snapshot: dict = field(default_factory=dict)
    file_changed: bool = False


def _norm_sheet(s: dict) -> dict:
    return {k: s.get(k, "") for k in ("id", "content", "done", "scheduled", "due", "priority", "created", "done_date")}


def plan(file_text: str, sheet_tasks: list[dict], snapshot: dict, today: date) -> SyncPlan:
    lines = file_text.split("\n") if file_text else []
    sheet_ids = {t["id"] for t in sheet_tasks if t["id"]}
    taken = set(sheet_ids)
    file_by_id: dict[str, tuple[int, dict]] = {}
    dirty: set[int] = set()  # 書き換えが必要な行（ID の付与を含む）

    for idx, line in enumerate(lines):
        t = parse_line(line)
        if t is None or not t["content"]:
            continue
        if not t["id"]:
            t["id"] = sheets.new_task_id(taken)
            taken.add(t["id"])
            if not t["created"]:
                t["created"] = util.fmt_date(today)
            dirty.add(idx)
        if t["id"] in file_by_id:  # 同じ ID の重複行は最初の1行だけ同期対象にする
            continue
        file_by_id[t["id"]] = (idx, t)

    deleted_ids = {t["id"] for t in sheet_tasks if t["deleted"]}
    sheet_by_id = {t["id"]: _norm_sheet(t) for t in sheet_tasks if t["id"] and not t["deleted"]}
    new_snap: dict[str, str] = {}
    adds: list[dict] = []
    updates: list[tuple[str, dict]] = []
    new_lines: list[str] = []

    for tid in sorted(set(file_by_id) | set(sheet_by_id)):
        if tid in deleted_ids:
            continue
        f = file_by_id.get(tid)
        s = sheet_by_id.get(tid)
        if f and s:
            idx, ft = f
            if _is_stale_done(ft, today) and _is_stale_done(s, today):
                if tid in snapshot:
                    new_snap[tid] = snapshot[tid]
                continue
            hf, hs, hp = digest(ft), digest(s), snapshot.get(tid)
            if hf == hs or hp == hs:  # 一致、または Obsidian 側だけが変更された
                merged = dict(ft)
            elif hp == hf:  # シート側だけが変更された
                merged = {**ft, **{k: s[k] for k in ("content", "done", "scheduled", "due", "priority", "done_date")}}
            else:  # 両側が変更（または初回）: Obsidian を優先。ただし完了はどちらかが完了なら完了
                merged = dict(ft)
                if s["done"] and not ft["done"]:
                    merged["done"], merged["done_date"] = True, s["done_date"]
            if merged["done"] and not merged.get("done_date"):
                merged["done_date"] = util.fmt_date(today)
            if not merged["done"]:
                merged["done_date"] = ""
            merged.setdefault("created", ft.get("created") or s.get("created") or util.fmt_date(today))
            if digest(merged) != hf or idx in dirty or merged.get("done_date") != ft.get("done_date"):
                lines[idx] = render(merged)
            if digest(merged) != hs or merged.get("done_date") != s.get("done_date"):
                updates.append((tid, {k: merged[k] for k in ("content", "done", "scheduled", "due", "priority", "done_date")}))
            new_snap[tid] = digest(merged)
        elif f:  # Obsidian にだけある: 新規（または、シートから消えていたので戻す）
            idx, ft = f
            if _is_stale_done(ft, today):
                continue
            if ft["done"] and not ft.get("done_date"):
                ft["done_date"] = util.fmt_date(today)
            lines[idx] = render(ft)
            adds.append({**ft, "source": "obsidian"})
            new_snap[tid] = digest(ft)
        else:  # シートにだけある
            if tid in snapshot:  # 以前は同期済み → Obsidian 側で行が削除された
                updates.append((tid, {"deleted": True}))
            elif not _is_stale_done(s, today):  # Bot 側で作られた新規タスク → 棚へ追加
                new_lines.append(render({**s, "indent": ""} | {"created": s.get("created") or util.fmt_date(today)}))
                new_snap[tid] = digest(s)

    if new_lines:
        lines = _insert_into_section(lines, new_lines)
    new_text = "\n".join(lines)
    if new_text and not new_text.endswith("\n"):
        new_text += "\n"
    changed = new_text != file_text and (new_text.rstrip("\n") != file_text.rstrip("\n") or bool(new_lines))
    return SyncPlan(text=new_text if changed else file_text, sheet_adds=adds, sheet_updates=updates,
                    snapshot=new_snap, file_changed=changed)


def _insert_into_section(lines: list[str], new_lines: list[str]) -> list[str]:
    """`## 🤖 Life-OS` 見出しの節の末尾に追記する。見出しが無ければファイル末尾に作る。"""
    head = next((i for i, l in enumerate(lines) if l.strip() == SECTION), None)
    if head is None:
        while lines and not lines[-1].strip():
            lines.pop()
        return lines + ([""] if lines else []) + [SECTION, ""] + new_lines
    end = next((i for i in range(head + 1, len(lines)) if lines[i].startswith("#")), len(lines))
    insert_at = end
    while insert_at > head + 1 and not lines[insert_at - 1].strip():
        insert_at -= 1
    if insert_at == head + 1:
        new_lines = [""] + new_lines
    return lines[:insert_at] + new_lines + ([""] if end < len(lines) and insert_at == end else []) + lines[insert_at:]


# ---------------------------------------------------------------- 実行


def run(store=None) -> dict:
    """同期を1回実行する（同期関数）。Obsidian を先に書き、その後シートを更新する。"""
    store = store or notes.get_store()
    with _lock:
        for attempt in range(3):
            before = store.modified(TASK_FILE)
            text = store.read(TASK_FILE) or ""
            sheet_tasks = sheets.all_tasks()
            snapshot = json.loads(state.get_kv(SNAPSHOT_KEY, "{}"))
            p = plan(text, sheet_tasks, snapshot, util.today())
            if p.file_changed:
                if store.modified(TASK_FILE) != before:  # 読み込み後に Obsidian 側で編集された → 再計算
                    log.info("task file changed during sync; retry %d", attempt + 1)
                    continue
                store.write(TASK_FILE, p.text)
            for t in p.sheet_adds:
                sheets.add_task(t["content"], scheduled=t.get("scheduled", ""), due=t.get("due", ""),
                                priority=t.get("priority", ""), source=t.get("source", "obsidian"), task_id=t["id"],
                                done=t["done"], done_date=t.get("done_date", ""), created=t.get("created") or None)
            for tid, fields in p.sheet_updates:
                sheets.update_task(tid, **fields)
            state.set_kv(SNAPSHOT_KEY, json.dumps(p.snapshot))
            return {"file_written": p.file_changed, "sheet_added": len(p.sheet_adds), "sheet_updated": len(p.sheet_updates)}
    raise RuntimeError("タスク棚の同期が競合しました（次回に再試行します）")


def run_safely() -> dict | None:
    """失敗しても呼び出し元の処理を止めない（ログのみ）。"""
    try:
        return run()
    except Exception:  # noqa: BLE001
        log.exception("task sync failed")
        return None
