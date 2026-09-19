"""Bot 本体: イベント受信とチャンネルごとのルーティング。"""
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")
if os.getenv("USE_TRUSTSTORE") == "1":
    # 開発用パソコンのみ: 証明書検証は無効にせず、OS の証明書ストアを使う（本番の GCP VM では設定しない）
    import truststore

    truststore.inject_into_ssl()

import asyncio
import logging

import discord

import config
import gas_relay
import instance_guard
import notes
import notes_policy
import scheduler
import sheets
import state
from handlers import health, idea, looking_back, project, scrap, today_task

log = logging.getLogger("life-os")

REQUIRED = ("DISCORD_TOKEN", "ANTHROPIC_API_KEY", "GOOGLE_SHEET_ID")


def build_routes() -> dict:
    """実装済みのチャンネルだけ。ほかは、実装されるまで何もしない（記録したように見せない）。"""
    c = config.CHANNELS
    return {c["today"]: today_task.handle, c["health"]: health.handle, c["lookback"]: looking_back.handle,
            c["idea"]: idea.handle, c["scrap"]: scrap.handle,
            c["novel"]: project.make_handler("novel"), c["trpg"]: project.make_handler("trpg"),
            c["others"]: project.make_handler("others")}


class LifeOS(discord.Client):
    def __init__(self, lease=None):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.routes = build_routes()
        self.lease = lease

    async def setup_hook(self):
        result = await asyncio.to_thread(sheets.ensure_schema)
        for key in ("created", "headers_written", "columns_added", "refused"):
            if result[key]:
                log.info("シートの準備 %s: %s", key, result[key])
        if result["refused"]:
            log.warning("想定外の内容があるシートは使いません: %s", result["refused"])
        state.purge_old()
        if self.lease is not None:
            instance_guard.start_heartbeat(self, self.lease)
        scheduler.start(self)

    async def on_ready(self):
        log.info("logged in as %s", self.user)

    async def on_message(self, message: discord.Message):
        if message.author.bot or not isinstance(message.channel, discord.TextChannel):
            return
        handler = self.routes.get(message.channel.name)
        if handler is None:
            return
        started = time.monotonic()
        log.info("#%s: 処理を開始します（%d文字, 添付%d件）", message.channel.name, len(message.content or ""), len(message.attachments))
        try:
            await handler(message)
            log.info("#%s: 処理が完了しました（%.1f秒）", message.channel.name, time.monotonic() - started)
        except notes_policy.AccessDenied:
            log.exception("access denied in #%s", message.channel.name)
            await message.channel.send("そのファイルは触らない設定になっています。")
        except (gas_relay.RelayError, sheets.SheetError) as e:
            log.exception("storage error in #%s", message.channel.name)
            await message.channel.send(f"うまく記録できませんでした。少し待ってからもう一度送ってください（原因: {type(e).__name__}）")
        except Exception as e:  # noqa: BLE001
            log.exception("handler failed in #%s", message.channel.name)
            await message.channel.send(f"うまく記録できませんでした。もう一度送ってください（原因: {type(e).__name__}）")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    missing = [k for k in REQUIRED if not getattr(config, k)]
    if missing:
        raise SystemExit(f".env に未入力の項目があります: {', '.join(missing)}")

    # 多重起動の防止（1: このパソコン内、2: パソコンと VM など別の場所どうし）
    lock = instance_guard.ProcessLock(config.DATA_DIR / "bot.lock")
    if not lock.acquire():
        pid = lock.holder_pid()
        print(f"すでにこのパソコンで Bot が起動しています{f'（PID {pid}）' if pid else ''}。二重には起動しません。", file=sys.stderr)
        sys.exit(instance_guard.EXIT_ALREADY_RUNNING)
    lease = instance_guard.Lease(notes.get_store())
    try:
        lease.acquire(same_host_ok=True)  # ProcessLock を取得済みなので、同じホストの古い印は引き継いでよい
    except instance_guard.AlreadyRunning as e:
        lock.release()
        print(str(e), file=sys.stderr)
        sys.exit(instance_guard.EXIT_ALREADY_RUNNING)
    except Exception as e:  # noqa: BLE001
        lock.release()
        raise SystemExit(f"使用中の印を確認できませんでした（{type(e).__name__}）。Drive への接続を確認してください。") from None
    try:
        LifeOS(lease).run(config.DISCORD_TOKEN, log_handler=None)
    finally:
        lease.release()
        lock.release()
    if lease.lost:
        sys.exit(instance_guard.EXIT_LOST)


if __name__ == "__main__":
    main()
