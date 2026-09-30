"""`03-looking-back`: 夜の振り返りの記録と、翌日タスクの番号選択（SPEC §5）。"""
import asyncio
import logging
from datetime import date, datetime, timedelta

import claude_client
import done_list
import notes
import sheets
import state
import task_dates
import task_sync
import util
import vault_paths
import weather
from handlers import private

log = logging.getLogger(__name__)

SOURCE = "03-looking-back"
KIND = "backlog_choice"
EVENING_PREFIX = "今日もお疲れ様でした！ 本日の記録を残しましょう"
CANDIDATE_LIMIT = 10

_PROMPT = """次は、ユーザーが夜の振り返りとして投稿した文章です。JSONだけを返してください。

{{
  "mood": ご機嫌度（1〜5の整数）。文章に書かれていなければ null,
  "formatted": 読みやすく整えた日記本文。元の意図を変えず、評価・助言・説教・励ましを足さない。箇条書きにしてよい,
  "tomorrow_tasks": 「明日〜する」など、文章に明示されているこれからのやることだけの配列。推測で作らない。無ければ空配列。「明日」「9/25」「〜まで」など日付の言葉は、投稿にあるとおりに文中へ残す,
  "wants_x": 「Xのポスト案」を明示的に求めているか（true/false）,
  "wants_note": 「noteのネタ」を明示的に求めているか（true/false）
}}

投稿:
{text}"""


def selection_target_day(now) -> date:
    """深夜（5時前）の選択は「今日」、それ以外は「明日」の予定日にする。"""
    d = now.date()
    return d if now.hour < 5 else d + timedelta(days=1)


def journal_day(now: datetime) -> date:
    """日記の日付。23:00 の問いかけに日付が変わってから答えることがあるので、5時前は前日にする。"""
    d = now.date()
    return d - timedelta(days=1) if now.hour < 5 else d


def format_done(items: list[str]) -> str | None:
    return ("✅ 今日やったこと（05-private より）：\n" + "\n".join(f"・{t}" for t in items)) if items else None


def format_candidates(items: list[dict]) -> str:
    lines = ["📋 明日のタスク候補（バックログ）："]
    lines += [f"{i}. {t['content']}" for i, t in enumerate(items, 1)]
    lines.append("明日やるものを番号で返信してください（例: 1, 3）。ない場合は「なし」で大丈夫です。")
    return "\n".join(lines)


def _fallback(text: str) -> dict:
    return {"mood": util.parse_mood(text), "formatted": text, "tomorrow_tasks": [],
            "wants_x": "ポスト案" in text or "X案" in text, "wants_note": "note案" in text or "noteのネタ" in text}


async def build_prompt() -> tuple[str, list[str]]:
    """夜の問いかけの本文と、載せた完了タスクの ID（朝の案内で重ねて出さないために覚える）。"""
    w = await weather.today_weather()
    day = journal_day(util.now())
    completed = await done_list.completed_on(day)
    sections = [s for s in (done_list.format_items(done_list.TODAY_TITLE, [t["content"] for t in completed]),
                            format_done(await private.today_done(day))) if s]
    lines = [EVENING_PREFIX + "🌙"]
    if w:
        lines.append(f"今日の天気: {w}")
    if sections:
        lines += ["", "\n\n".join(sections), ""]
    lines.append("ご機嫌度（1〜5）と、今日のことを自由に書いてください。音声入力のテキストでも大丈夫です。")
    return "\n".join(lines), [t["id"] for t in completed]


async def prompt_text() -> str:
    return (await build_prompt())[0]


async def diary_lists(day: date) -> str:
    """日記に書き足す「今日完了したタスク」「今日やったこと」（その日の日記にまだ書いていない分だけ）。"""
    parts = []
    for kind, title, items in (
            ("tasks", "### ✅ 今日完了したタスク", [t["content"] for t in await done_list.completed_on(day)]),
            ("done", "### ✅ 今日やったこと", await private.today_done(day))):
        new = done_list.diary_new(day, kind, items)
        if new:
            parts.append(title + "\n" + "\n".join(f"- {i}" for i in new))
    return "\n\n".join(parts)


def remember_diary_lists(day: date, text: str) -> None:
    """書いた一覧を覚える（Obsidian に書けたあとで呼ぶ）。"""
    kind = None
    for line in text.splitlines():
        if line.startswith("### "):
            kind = "tasks" if "タスク" in line else "done"
        elif line.startswith("- ") and kind:
            done_list.remember_diary(day, kind, [line[2:]])


async def handle(message) -> None:
    text = message.content.strip()
    if not text:
        return
    ch = message.channel
    pending = None
    ref = getattr(message, "reference", None)
    if ref and ref.message_id:  # 提示メッセージへの返信
        p = state.get_pending(ref.message_id)
        if p and p["kind"] == KIND:
            pending = p
    if pending is None:  # 返信でなくても、直近12時間以内の提示があれば番号を受け付ける
        pending = state.latest_pending(ch.id, KIND, max_age_hours=12)
    if pending and (util.is_none_choice(text) or util.parse_numbers(text) is not None):
        await _select(message, pending, text)
        return
    await _journal(message, text)


async def _select(message, pending: dict, text: str) -> None:
    ch = message.channel
    items = pending["payload"]["items"]
    state.delete_pending(pending["message_id"])
    if util.is_none_choice(text):
        await ch.send("了解です。ゆっくり休んでくださいね🌙")
        return
    nums = util.parse_numbers(text) or []
    ids = [items[n - 1]["id"] for n in nums if 1 <= n <= len(items)]
    day = selection_target_day(util.now())
    done = await asyncio.to_thread(sheets.schedule_tasks, ids, day) if ids else []
    if not done:
        state.put_pending(pending["message_id"], ch.id, KIND, pending["payload"])  # もう一度選べるように戻す
        await ch.send("その番号は見当たりませんでした。もう一度どうぞ。（ない場合は「なし」で大丈夫です）")
        return
    await asyncio.to_thread(task_sync.run_safely)
    await util.ack(message, "✅")
    label = "明日" if day > util.today() else "今日"
    await ch.send(f"✅ {label}のタスクにセットしました：\n" + "\n".join(f"・{c}" for c in done) + "\nおやすみなさい🌙")


async def _journal(message, text: str) -> None:
    ch = message.channel
    now = util.now()
    base = journal_day(now)
    day = util.fmt_date(base)
    parsed = await claude_client.complete_json(_PROMPT.format(text=text), _fallback(text))
    if not isinstance(parsed, dict):
        parsed = _fallback(text)
    mood = util.parse_mood(text) or util.clamp_int(parsed.get("mood"), 1, 5)
    formatted = str(parsed.get("formatted") or text).strip() or text
    tomorrow = [str(t).strip() for t in (parsed.get("tomorrow_tasks") or []) if str(t).strip()][:5]
    w = await weather.today_weather() or ""

    # Obsidian を先に書く（失敗したらシートには記録しない）
    lists = await diary_lists(base)  # 今日完了したタスク・今日やったことも日記に残す（まだ書いていない分だけ）
    entry = f"## {now:%H:%M}\n" + (f"ご機嫌度: {mood}\n" if mood else "") + (f"天気: {w}\n" if w else "") + "\n" + formatted
    if lists:
        entry += "\n\n" + lists
    link = await asyncio.to_thread(notes.get_store().append_entry, vault_paths.diary(day), entry, day)
    if lists:
        remember_diary_lists(base, lists)
    await asyncio.to_thread(sheets.append, sheets.DIARY, {"日付": day, "ご機嫌度": mood or "", "天気": w,
                                                          "本文": formatted, "Obsidianリンク": link})
    dated = []
    for t in tomorrow:
        ext = task_dates.extract(t, base)  # 「明日」「9/25まで」などが書かれていれば、実行日・期限に反映する
        await asyncio.to_thread(sheets.add_task, ext.content, scheduled=task_dates.iso(ext.scheduled),
                                due=task_dates.iso(ext.due), source=SOURCE)
        if ext.scheduled or ext.due:
            dated.append(f"・{ext.content}（{task_dates.describe(ext.scheduled, ext.due)}）")
    await util.ack(message, "🌙")

    replies = ["今日の記録を残しました。おつかれさまでした🌙"]
    if dated:
        replies.append("📌 日付を設定しました：\n" + "\n".join(dated))
    if parsed.get("wants_x"):
        replies.append("【Xのポスト案】\n" + await claude_client.complete(
            f"次の出来事や気持ちから、Xのポスト案を3つ、各140字以内で作ってください。\n\n{formatted}", max_tokens=700))
    if parsed.get("wants_note"):
        replies.append("【noteのネタ】\n" + await claude_client.complete(
            f"次の出来事や気持ちから、noteの記事ネタを3つ、タイトル案と一言の要点で作ってください。\n\n{formatted}", max_tokens=700))
    await util.send_long(ch, "\n\n".join(replies))

    await asyncio.to_thread(task_sync.run_safely)
    backlog = await asyncio.to_thread(sheets.backlog_tasks, CANDIDATE_LIMIT)
    if backlog:
        msg = await ch.send(format_candidates(backlog))
        state.put_pending(msg.id, ch.id, KIND, {"items": [{"id": t["id"], "content": t["content"]} for t in backlog]})
