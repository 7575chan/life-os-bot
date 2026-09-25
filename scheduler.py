"""定時ジョブ（JST）: 朝の案内、昨日の執筆実績、夜の振り返りの問いかけ、週次・月次レポート、タスク棚の同期（SPEC §5・§6・§7）。

- 朝・夜・レポートは、同じ日に二重投稿しないよう state に実行済みを記録する。
- 起動が定時に間に合わなかった場合の「後追い投稿」はしない（思いがけない時間に投稿しないため）。
- ジョブ内の例外はログに残し、ループは止めない。
"""
import logging

import discord
from discord.ext import tasks

import asyncio
import config
import report
import state
import task_sync
import util
import writing_log
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


async def post_report(kind: str, force: bool = False) -> bool:
    """週次（weekly）・月次（monthly）レポートを 04-report に投稿し、ノートとシートに保存する。"""
    day = util.today()
    if not force and not state.mark_done(f"report:{kind}:{day}"):
        return False
    ch = find_channel("report")
    if ch is None:
        return False
    prefix = report.period_for(kind, day).prefix
    if not force and await _posted_today(ch, prefix, day):
        log.info("%s は、別の場所ですでに投稿されているため、投稿しません", prefix)
        return False
    rep = await report.generate(kind, day)
    failed = await asyncio.to_thread(report.save, rep, day)  # 保存に失敗しても、投稿は出す（失敗は本文の末尾で知らせる）
    text = rep.text + (f"\n\n※{'・'.join(failed)}への保存に失敗しました。" if failed else "")
    await util.send_long(ch, text)
    return True


WRITING_WINDOW_HOURS = 4  # 投稿の時刻から何時間までなら、投稿する（記録が遅れた日の、思いがけない時間の投稿を避ける）


def writing_due(now, at, hours: int = WRITING_WINDOW_HOURS) -> bool:
    """now（JST）が、`at`（WRITING_TIME）から `hours` 時間以内か。"""
    minutes = now.hour * 60 + now.minute
    start = at.hour * 60 + at.minute
    return start <= minutes < start + hours * 60


async def post_writing(force: bool = False) -> bool:
    """昨日書いた文字数（執筆記録シートの、今日の行 − 昨日の行）を 01-today-task に投稿する。

    記録は毎日 09:44 前後なので、朝 08:00 の案内には載せられない。WRITING_TIME（既定 10:00）から、今日の行が入るまで
    10分ごとに確認し、入ったら1回だけ投稿する。増えていない日・記録が無い日は投稿しない（責めない）。"""
    day = util.today()
    key = f"writing:{day}"
    if not force and (not writing_due(util.now(), config.WRITING_TIME) or state.get_kv(key)):
        return False
    log_ = await asyncio.to_thread(writing_log.fetch)
    stats = writing_log.yesterday_stats(log_, day) if log_ is not None else None
    if stats is None:
        return False  # 未設定、または今日の行がまだ無い。次の周期で確認する
    text = writing_log.format_morning(stats, day)
    if not text:
        if not force:
            state.set_kv(key, "1")  # 今日の行を確認済み。増えていない日は、何も投稿しない
        return False
    ch = find_channel("today")
    if ch is None:
        return False
    if not force and await _posted_today(ch, writing_log.YESTERDAY_PREFIX, day):
        log.info("昨日の執筆実績は、別の場所ですでに投稿されているため、投稿しません")
        state.set_kv(key, "1")
        return False
    await util.send_long(ch, text)
    if not force:
        state.set_kv(key, "1")
    return True


@tasks.loop(minutes=10)
async def writing_job():
    try:
        await post_writing()
    except Exception:  # noqa: BLE001  シートを読めない日は、次の周期でやり直す
        log.exception("writing job failed")


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


@tasks.loop(time=config.REPORT_TIME)
async def report_job():
    for kind in report.kinds_due(util.today()):  # 日曜は週次、月末日は月次（両方の日は両方）
        try:
            await post_report(kind)
        except Exception:  # noqa: BLE001  片方が失敗しても、もう片方は投稿する
            log.exception("report job (%s) failed", kind)


@tasks.loop(minutes=10)
async def sync_job():
    await asyncio.to_thread(task_sync.run_safely)


def start(client: discord.Client) -> None:
    global _client
    _client = client
    for job in (morning_job, evening_job, report_job, writing_job, sync_job):
        job.before_loop(client.wait_until_ready)
        job.start()
