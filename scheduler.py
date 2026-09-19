"""定時ジョブ（JST）: 朝の案内、夜の振り返りの問いかけ、タスク棚の同期（SPEC §5・§6・§7）。

- 朝・夜は、同じ日に二重投稿しないよう state に実行済みを記録する。
- 起動が定時に間に合わなかった場合の「後追い投稿」はしない（思いがけない時間に投稿しないため）。
- ジョブ内の例外はログに残し、ループは止めない。
"""
import logging

import discord
from discord.ext import tasks

import asyncio
import config
import state
import task_sync
import util
from handlers import looking_back, today_task

log = logging.getLogger("life-os.scheduler")
_client: discord.Client | None = None


def already_posted(messages, bot_id: int, prefix: str, day) -> bool:
    """bot_id が、day（JST）に prefix で始まる投稿をすでにしているか。別の場所の Bot が先に投稿していた場合の重複防止。"""
    for m in messages:
        if m.author.id == bot_id and m.content.startswith(prefix) and m.created_at.astimezone(config.TZ).date() == day:
            return True
    return False


async def _posted_today(ch, prefix: str, day) -> bool:
    try:
        history = [m async for m in ch.history(limit=30)]
    except Exception:  # noqa: BLE001  履歴を読めなくても、投稿を止めない
        log.warning("投稿履歴を確認できませんでした", exc_info=True)
        return False
    return already_posted(history, _client.user.id, prefix, day)


def find_channel(key: str):
    name = config.CHANNELS[key]
    for guild in _client.guilds:
        ch = discord.utils.get(guild.text_channels, name=name)
        if ch:
            return ch
    log.error("チャンネル %r が見つかりません", name)
    return None


async def post_morning(force: bool = False) -> bool:
    day = util.today()
    if not force and not state.mark_done(f"morning:{day}"):
        return False
    ch = find_channel("today")
    if ch is None:
        return False
    if not force and await _posted_today(ch, today_task.MORNING_PREFIX, day):
        log.info("朝の案内は、別の場所ですでに投稿されているため、投稿しません")
        return False
    text, tasks_ = await today_task.build_morning(day)
    msg = await util.send_long(ch, text)
    state.put_pending(msg.id, ch.id, "today_list", {"items": [{"id": t["id"], "content": t["content"]} for t in tasks_]})
    return True


async def post_evening(force: bool = False) -> bool:
    day = util.today()
    if not force and not state.mark_done(f"evening:{day}"):
        return False
    ch = find_channel("lookback")
    if ch is None:
        return False
    if not force and await _posted_today(ch, looking_back.EVENING_PREFIX, day):
        log.info("夜の問いかけは、別の場所ですでに投稿されているため、投稿しません")
        return False
    await ch.send(await looking_back.prompt_text())
    return True


@tasks.loop(time=config.MORNING_TIME)
async def morning_job():
    try:
        await post_morning()
    except Exception:  # noqa: BLE001
        log.exception("morning job failed")


@tasks.loop(time=config.EVENING_TIME)
async def evening_job():
    try:
        await post_evening()
    except Exception:  # noqa: BLE001
        log.exception("evening job failed")


@tasks.loop(minutes=10)
async def sync_job():
    await asyncio.to_thread(task_sync.run_safely)


def start(client: discord.Client) -> None:
    global _client
    _client = client
    for job in (morning_job, evening_job, sync_job):
        job.before_loop(client.wait_until_ready)
        job.start()
