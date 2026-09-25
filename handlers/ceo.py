"""`13-im-the-ceo`: 方針・宣言の部屋（SPEC §5）。**サイレント**（リアクションだけ）。

- 投稿を、`06-Life-OS/13-im-the-ceo/CEO-Directives.md` の見出し直下に `## YYYY-MM-DD HH:MM` ＋本文で挿入する（最新が上）
  と、`13-im-the-ceo` シート（日時 / 内容）に記録する
- この内容は、全ての Claude の呼び出しのシステムプロンプトに**最優先**で入る（`claude_client.ceo_directives`。
  最新が上なので、長くなっても新しい方針が切り捨てられない。60秒キャッシュ。書き込み後はすぐ再読込する）
- 新しい方針が古い方針と食い違うときは、新しい方をとる（システムプロンプトに書いてある）
- リアクション: 👑 記録した / ⚠️ 記録はしたが、シートに書けなかった。Obsidian に書けなかったときだけ、共通のエラーメッセージ
"""
from __future__ import annotations

import asyncio
import logging

import claude_client
import notes
import sheets
import util
import vault_paths
from handlers import idea

log = logging.getLogger("life-os.ceo")

_write_lock = asyncio.Lock()  # CEO-Directives.md の「読む→書き換える」を1件ずつにする


def format_entry(now, text: str) -> str:
    return f"## {now:%Y-%m-%d %H:%M}\n{idea.escape_body(text)}"


async def handle(message) -> None:
    text = (message.content or "").strip()
    if not text:
        return
    now = util.now()
    async with _write_lock:  # Obsidian を先に書く（失敗したらシートにも記録しない）
        await asyncio.to_thread(notes.get_store().prepend_entry, vault_paths.ceo_directives(), format_entry(now, text), "CEO-Directives")
    claude_client.invalidate_ceo_cache()  # 次の Claude の呼び出しから、この方針を使う
    await util.ack(message, "👑")
    try:
        await asyncio.to_thread(sheets.append, sheets.DIRECTIVES, {"日時": util.fmt_datetime(now), "内容": text})
    except Exception:  # noqa: BLE001
        log.warning("方針をシートに記録できませんでした（Obsidian には保存済み）", exc_info=True)
        await util.ack(message, "⚠️")
