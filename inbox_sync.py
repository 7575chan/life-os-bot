"""手書きのタスクページ（`01🗃Task/00Task Inbox-2026.md`）の読み込み（SPEC §6.4）。

- ユーザーが Obsidian に手で書いた `- [ ]` の行を、スプレッドシートのタスク（出典 `inbox`）として持つ
- Bot がこのページに書いてよいのは「チェック（[ ]→[x]）」と「⏳ 日付」だけ。書き込みは
  `GuardedStore.write_marks` を通し、それ以外の違いがあれば書かずに止まる
- このページには 🆔 を書かないので、タスクは「文」（日付・優先度などの印を除いた部分）で見分ける
- 文・📅・優先度はページが正。完了はどちらかで完了すれば完了。⏳ は最後に変えたほうが正（スナップショットで判定）
- タスク棚（01 Life-OS-Task）には書き写さない（task_sync は出典 `inbox` のタスクを棚に出さない）
"""
from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field

import notes_policy
import sheets
import state
import util

log = logging.getLogger("life-os.inbox")

SOURCE = "inbox"
SNAPSHOT_KEY = "inbox_snapshot"
_CHECK = re.compile(r"^(\s*[-*+]\s+\[) (\])")
_SCHED = re.compile(r"(\s*)⏳️?\s*\d{4}-\d{2}-\d{2}")


def enabled() -> bool:
    return bool(notes_policy.INBOX_FILE)


def key(content: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", content or "")).strip()


def _iso(value: str) -> str:
    d = util.parse_date_any(value)
    return util.fmt_date(d) if d else ""


def set_check(line: str) -> str:
    return _CHECK.sub(r"\1x\2", line, count=1)


def set_scheduled(line: str, day: str) -> str:
    """その行の ⏳ を day にする（空なら消す）。⏳ が無ければ行末に足す。"""
    if _SCHED.search(line):
        return _SCHED.sub((lambda m: f"{m.group(1)}⏳ {day}") if day else "", line, count=1)
    return f"{line.rstrip()} ⏳ {day}" if day else line


@dataclass
class InboxPlan:
    text: str
    adds: list[dict] = field(default_factory=list)
    updates: list[tuple[str, dict]] = field(default_factory=list)
    snapshot: dict = field(default_factory=dict)


def plan(text: str, sheet_tasks: list[dict], snapshot: dict, today) -> InboxPlan:
    """ページの内容・シートのタスク（出典 inbox）・前回のスナップショットから、ページの新しい内容とシートの変更を決める。"""
    from task_sync import parse_line  # 循環を避ける

    lines = text.split("\n") if text else []
    by_id = {t["id"]: t for t in sheet_tasks if t.get("id")}
    taken = set(by_id)
    # スナップショットとつながっていないシートのタスク（取り込みが途中で止まって二重になった分など）。
    # 同じ文の行があれば、新しく足さずにそれを使う（未完了を優先）。
    linked = {e.get("id") for e in snapshot.values()}
    orphans: dict[str, list[dict]] = {}
    for t_ in sorted(sheet_tasks, key=lambda x: (bool(x.get("deleted")), bool(x.get("done")))):
        if t_.get("id") and t_["id"] not in linked:
            orphans.setdefault(key(t_.get("content", "")), []).append(t_)
    new_snap: dict[str, dict] = {}
    adds: list[dict] = []
    updates: list[tuple[str, dict]] = []
    for i, line in enumerate(lines):
        t = parse_line(line)
        if t is None or not t["content"]:
            continue
        k = key(t["content"])
        if k in new_snap:
            continue  # 同じ文の行は最初の1行だけ
        entry = snapshot.get(k) or {}
        s = by_id.get(entry.get("id", ""))
        if s is None and orphans.get(k):
            s = orphans[k].pop(0)
            entry = {"id": s["id"], "scheduled": _iso(s.get("scheduled", ""))}  # 日付が違えばページが正
        if s is None:
            if t["done"]:
                continue  # 最初から完了の行は取り込まない
            tid = sheets.new_task_id(taken)
            taken.add(tid)
            adds.append({"id": tid, "content": t["content"], "scheduled": t["scheduled"], "due": t["due"],
                         "priority": t["priority"], "created": t["created"] or util.fmt_date(today)})
            new_snap[k] = {"id": tid, "scheduled": t["scheduled"]}
            continue
        change: dict = {}
        if s.get("deleted"):
            change["deleted"] = False  # 消えた行が戻ってきた
        if t["done"] and not s["done"]:
            change.update(done=True, done_date=t["done_date"] or util.fmt_date(today))
        elif s["done"] and not t["done"]:
            lines[i] = line = set_check(line)  # Discord で完了 → チェックだけ付ける
        prev, fs, ss = entry.get("scheduled", ""), t["scheduled"], _iso(s.get("scheduled", ""))
        final = fs
        if fs != ss:
            if ss != prev and fs == prev:  # Discord で日付を変えた → ページの ⏳ を書き換える
                lines[i] = set_scheduled(line, ss)
                final = ss
            else:  # ページで変えた（両方変わったときもページが正）
                change["scheduled"] = fs
        if (s.get("content") or "") != t["content"]:
            change["content"] = t["content"]
        if _iso(s.get("due", "")) != t["due"]:
            change["due"] = t["due"]
        if (s.get("priority") or "") != t["priority"]:
            change["priority"] = t["priority"]
        if change:
            updates.append((s["id"], change))
        new_snap[k] = {"id": s["id"], "scheduled": final}
    used = {e["id"] for e in new_snap.values()}
    if not new_snap and not any(parse_line(x) for x in lines):  # 空で読めたとき（読み取りの失敗など）は消さない
        return InboxPlan(text="\n".join(lines), adds=adds, updates=updates, snapshot=dict(snapshot))
    for s in sheet_tasks:  # ページのどの行ともつながらない、未完了のタスク（消えた行・二重に取り込まれた分）
        if s.get("id") and s["id"] not in used and not s["done"] and not s.get("deleted"):
            updates.append((s["id"], {"deleted": True}))
    return InboxPlan(text="\n".join(lines), adds=adds, updates=updates, snapshot=new_snap)


def run(store, sheet_tasks: list[dict]) -> dict | None:
    """同期の中から呼ぶ（task_sync.run）。ページが無い・読めないときは何もしない。"""
    if not enabled():
        return None
    path = notes_policy.INBOX_FILE
    text = store.read(path)
    if text is None:
        log.warning("手書きのタスクページが見つかりません: %s（.env の OBSIDIAN_INBOX_FILE を確かめてください）", path)
        return None
    mine = [t for t in sheet_tasks if t.get("source") == SOURCE]
    snapshot = json.loads(state.get_kv(SNAPSHOT_KEY, "{}") or "{}")
    p = plan(text, mine, snapshot, util.activity_day())
    written = p.text == text
    if not written:
        if store.read(path) != text:  # 読んだあとにユーザーが書き換えた → 次の同期でやり直す
            log.info("手書きのタスクページが同期中に書き換えられたので、次回にやり直します")
            return None
        try:
            store.write_marks(path, p.text)
            written = True
        except Exception:  # noqa: BLE001  シートの記録は進め、ページへの ⏳ は次の同期でもう一度書く
            log.exception("手書きのタスクページに書けませんでした（次の同期でもう一度書きます）")
            for k, entry in p.snapshot.items():
                if k in snapshot:
                    entry["scheduled"] = snapshot[k].get("scheduled", "")
    for t in p.adds:
        sheets.add_task(t["content"], scheduled=t["scheduled"], due=t["due"], priority=t["priority"],
                        source=SOURCE, task_id=t["id"], created=t["created"])
    for tid, fields in p.updates:
        sheets.update_task(tid, **fields)
    state.set_kv(SNAPSHOT_KEY, json.dumps(p.snapshot, ensure_ascii=False))
    return {"page_written": written and p.text != text, "added": len(p.adds), "updated": len(p.updates)}
