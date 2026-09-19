"""`03-looking-back`: 夜の振り返りの記録と、翌日タスクの番号選択（SPEC §5）。"""
import asyncio
from datetime import date, timedelta

import claude_client
import notes
import sheets
import state
import task_sync
import util
import vault_paths
import weather

SOURCE = "03-looking-back"
KIND = "backlog_choice"
EVENING_PREFIX = "今日もお疲れ様でした！ 本日の記録を残しましょう"
CANDIDATE_LIMIT = 10

_PROMPT = """次は、ユーザーが夜の振り返りとして投稿した文章です。JSONだけを返してください。

{{
  "mood": ご機嫌度（1〜5の整数）。文章に書かれていなければ null,
  "formatted": 読みやすく整えた日記本文。元の意図を変えず、評価・助言・説教・励ましを足さない。箇条書きにしてよい,
  "tomorrow_tasks": 「明日〜する」など、文章に明示されている翌日のやることだけの配列。推測で作らない。無ければ空配列,
  "wants_x": 「Xのポスト案」を明示的に求めているか（true/false）,
  "wants_note": 「noteのネタ」を明示的に求めているか（true/false）
}}

投稿:
{text}"""


def selection_target_day(now) -> date:
    """深夜（5時前）の選択は「今日」、それ以外は「明日」の予定日にする。"""
    d = now.date()
    return d if now.hour < 5 else d + timedelta(days=1)


def format_candidates(items: list[dict]) -> str:
    lines = ["📋 明日のタスク候補（バックログ）："]
    lines += [f"{i}. {t['content']}" for i, t in enumerate(items, 1)]
    lines.append("明日やるものを番号で返信してください（例: 1, 3）。ない場合は「なし」で大丈夫です。")
    return "\n".join(lines)


def _fallback(text: str) -> dict:
    return {"mood": util.parse_mood(text), "formatted": text, "tomorrow_tasks": [],
            "wants_x": "ポスト案" in text or "X案" in text, "wants_note": "note案" in text or "noteのネタ" in text}


async def prompt_text() -> str:
    w = await weather.today_weather()
    lines = [EVENING_PREFIX + "🌙"]
    if w:
        lines.append(f"今日の天気: {w}")
    lines.append("ご機嫌度（1〜5）と、今日のことを自由に書いてください。音声入力のテキストでも大丈夫です。")
    return "\n".join(lines)


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
    day = util.fmt_date(now.date())
    parsed = await claude_client.complete_json(_PROMPT.format(text=text), _fallback(text))
    if not isinstance(parsed, dict):
        parsed = _fallback(text)
    mood = util.parse_mood(text) or util.clamp_int(parsed.get("mood"), 1, 5)
    formatted = str(parsed.get("formatted") or text).strip() or text
    tomorrow = [str(t).strip() for t in (parsed.get("tomorrow_tasks") or []) if str(t).strip()][:5]
    w = await weather.today_weather() or ""

    # Obsidian を先に書く（失敗したらシートには記録しない）
    entry = f"## {now:%H:%M}\n" + (f"ご機嫌度: {mood}\n" if mood else "") + (f"天気: {w}\n" if w else "") + "\n" + formatted
    link = await asyncio.to_thread(notes.get_store().append_entry, vault_paths.diary(day), entry, day)
    await asyncio.to_thread(sheets.append, sheets.DIARY, {"日付": day, "ご機嫌度": mood or "", "天気": w,
                                                          "本文": formatted, "Obsidianリンク": link})
    for t in tomorrow:
        await asyncio.to_thread(sheets.add_task, t, source=SOURCE)
    await util.ack(message, "🌙")

    replies = ["今日の記録を残しました。おつかれさまでした🌙"]
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
