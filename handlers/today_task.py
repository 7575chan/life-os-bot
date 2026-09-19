"""`01-today-task`: 朝の案内、緊急タスクの追加、完了の記録（SPEC §5）。"""
import asyncio
import re
from datetime import date, timedelta

import sheets
import state
import task_sync
import util
import writing_log

MAIN_MAX = 3
_DONE = re.compile(r"^\s*(?:完了|done|かんりょう)[\s:：]*(.+)$", re.I)
_BULLET = re.compile(r"^\s*(?:[-*・•]|\[[ xX]?\])\s*")
SOURCE = "01-today-task"
MORNING_PREFIX = "おはようございます！ 本日のタスク案内"


def format_task_list(tasks: list[dict]) -> str:
    """メイン(最大3件)とサブ(無制限)を、通し番号つきで表示する。"""
    main, sub = tasks[:MAIN_MAX], tasks[MAIN_MAX:]
    lines: list[str] = []
    if main:
        lines.append("本日のメインタスク（最大3つ）：")
        lines += [f"{i}. {t['content']}" for i, t in enumerate(main, 1)]
    if sub:
        lines.append("")
        lines.append("サブタスク（気分に合わせて）：")
        lines += [f"{i}. {t['content']}" for i, t in enumerate(sub, MAIN_MAX + 1)]
    return "\n".join(lines)


def compose_morning(tasks: list[dict], writing_block: str | None, health_line: str | None) -> str:
    parts = [MORNING_PREFIX + " ☀️"]
    if writing_block:
        parts.append(writing_block)
    if health_line:
        parts.append(health_line)
    if tasks:
        parts.append(format_task_list(tasks))
        parts.append("（※未完了のタスクは、3日たつとバックログへ静かに移ります。「完了 1,3」で完了にできます）")
    else:
        parts.append("今日のタスクはまだありません。ここに書き込むと、今日のタスクに追加されます。夜の振り返りで、明日やるものを選ぶこともできます🌙")
    return "\n\n".join(parts)


def health_line(score: int | None, mood: str | None) -> str | None:
    bits = []
    if score:
        bits.append(f"体調スコア {score}")
    if mood:
        bits.append(f"ご機嫌度 {mood}")
    return ("🩺 昨日: " + " / ".join(bits)) if bits else None


async def build_morning(day: date) -> tuple[str, list[dict]]:
    """朝の案内の本文と、番号に対応するタスク一覧。日付が変わった直後の整理（3日たった未完了の退避）を先に行う。"""
    await asyncio.to_thread(sheets.cleanup_stale, day)
    await asyncio.to_thread(task_sync.run_safely)
    tasks = await asyncio.to_thread(sheets.today_tasks, day)
    y = day - timedelta(days=1)
    block = await asyncio.to_thread(writing_log.yesterday_block, day)
    score = await asyncio.to_thread(sheets.health_score_on, y)
    diary = await asyncio.to_thread(sheets.diary_on, y)
    line = health_line(score, (diary or {}).get("ご機嫌度") or None)
    return compose_morning(tasks, block, line), tasks


async def post_list(channel, header: str | None = None) -> None:
    """現在の今日のタスクを投稿し、番号に対応する状態を保存する。"""
    tasks = await asyncio.to_thread(sheets.today_tasks, util.today())
    body = format_task_list(tasks) if tasks else "今日のタスクは、今のところありません。"
    msg = await util.send_long(channel, (header + "\n\n" if header else "") + body)
    state.put_pending(msg.id, channel.id, "today_list", {"items": [{"id": t["id"], "content": t["content"]} for t in tasks]})


def _lines(text: str) -> list[str]:
    out = []
    for raw in text.splitlines():
        line = _BULLET.sub("", raw).strip()
        if line:
            out.append(line)
    return out


async def handle(message) -> None:
    text = message.content.strip()
    if not text:
        return
    channel = message.channel
    m = _DONE.match(text)
    if m:
        nums = util.parse_numbers(m.group(1))
        if not nums:
            await channel.send("番号で教えてください（例: 完了 1,3）。")
            return
        pending = state.latest_pending(channel.id, "today_list", max_age_hours=36)
        if pending:
            items = pending["payload"]["items"]
        else:
            items = [{"id": t["id"], "content": t["content"]} for t in await asyncio.to_thread(sheets.today_tasks, util.today())]
        ids = [items[n - 1]["id"] for n in nums if 1 <= n <= len(items)]
        done = await asyncio.to_thread(sheets.complete_tasks, ids) if ids else []
        if not done:
            await channel.send("その番号のタスクは見当たりませんでした。もう一度どうぞ。")
            return
        await asyncio.to_thread(task_sync.run_safely)
        await util.ack(message, "✅")
        await post_list(channel, "✅ 完了にしました！\n" + "\n".join(f"・{c}" for c in done) + "\nおつかれさまです。")
        return

    # それ以外の文は「今日絶対やる緊急タスク」。1行1タスク。
    added = []
    for line in _lines(text):
        await asyncio.to_thread(sheets.add_task, line, scheduled=util.fmt_date(util.today()), priority="高", source=SOURCE)
        added.append(line)
    if not added:
        return
    await asyncio.to_thread(task_sync.run_safely)
    await util.ack(message, "📝")
    await post_list(channel, "今日のタスクに追加しました：\n" + "\n".join(f"・{a}" for a in added))
