"""`05-private`: 私生活メモ・独り言（SPEC §5）。**ふだんはサイレント**（リアクションだけ）。

- Obsidian の `06-Life-OS/05-private/Inbox.md` の見出し直下に、`## YYYY-MM-DD HH:MM` ＋タグ行＋本文を挿入（最新が上）
- `05-private` シートにも記録する（日時 / 内容 / タグ）
- タグは AI が候補（settings の private_tags）から自動で付ける。本文に自分で書いた `#タグ` も生かす
- 「行きたいカフェ教えて」「買いたいものリスト出して」のように、過去のメモを呼び出す頼みだけ、Inbox.md の中身から返信する
  （問い合わせ自体はメモに残さない。添付がある投稿は、問い合わせでもメモとして残す）
- 画像は `05-private/attachments/` に保存して埋め込む（09-idea と同じ処理）
- リアクション: 📝 保存した / 💬 過去のメモに返信した / ⚠️ 保存はしたが、シートへの記録に失敗した
"""
from __future__ import annotations

import asyncio
import logging

import claude_client
import notes
import settings
import sheets
import util
import vault_paths
from handlers import idea

log = logging.getLogger("life-os.private")

KIND_MEMO, KIND_QUERY = "memo", "query"
CONTEXT_LIMIT = 20_000

_write_lock = asyncio.Lock()  # Inbox.md の「読む→書き換える」を1件ずつにする

_PROMPT = """次は、ユーザーが「私生活のメモ・独り言」の部屋に投稿した内容です。JSONだけを返してください。
{{
  "kind": 「行きたいって言ってたカフェ教えて」「買いたいものリスト出して」「前に書いた友達との予定は？」のように、過去のメモを取り出して見せてほしいという頼みなら "{query}"。それ以外（メモ・独り言・感想・予定・独り言の疑問文）はすべて "{memo}"。迷ったら "{memo}",
  "tags": 内容に合うタグを、次の候補から1〜3個（先頭の # は付けない）: {candidates}。どれも合わなければ ["{default}"],
  "search": kind が "{query}" のとき、過去のメモから探す言葉を1〜3個（空白区切り）。無ければ null
}}

投稿:
{text}"""

_ANSWER = """ユーザーが、私生活メモの部屋（Inbox）に、過去の投稿を取り出してほしいと頼んでいます。

【ユーザーの頼み】
{text}

【Inbox のメモ（見出しは投稿日時。新しいものが上）】
{context}"""

_ANSWER_SYSTEM = """今回は、ユーザーが過去のメモの呼び出しを頼んでいる。メモに書かれた内容だけをもとに、頼まれたものを簡潔に抜き出して答える（箇条書きでよい。投稿日を添えてよい）。
評価・説教・助言・提案は書かない。メモに無いことは推測せず、「メモには見当たりませんでした」と正直に言う。"""


# ---------------------------------------------------------------- 純粋関数


def normalize_kind(kind, has_attachments: bool) -> str:
    """問い合わせと判断するのは、AI がはっきり query と答え、添付がないときだけ。それ以外はメモとして残す。"""
    return KIND_QUERY if kind == KIND_QUERY and not has_attachments else KIND_MEMO


def split_entries(text: str) -> list[str]:
    """Inbox.md を、`## ` 見出しごとの投稿に分ける（先頭の `# Inbox` などは除く）。"""
    entries: list[str] = []
    for line in (text or "").split("\n"):
        if line.startswith("## "):
            entries.append(line)
        elif entries:
            entries[-1] += "\n" + line
    return [e.strip("\n") for e in entries]


def build_context(text: str, words: list[str], limit: int = CONTEXT_LIMIT) -> str:
    """返信の材料にする投稿を選ぶ。探す言葉を含む投稿を優先し、残りの枠を新しい投稿で埋める（並びは元のまま）。"""
    entries = split_entries(text)
    if not entries:
        return "（メモはまだありません）"
    keys = [w.lower() for w in words if w]
    chosen: set[int] = set()
    used = 0
    hits = [i for i, e in enumerate(entries) if keys and any(k in e.lower() for k in keys)]
    for i in hits + [i for i in range(len(entries)) if i not in hits]:
        size = len(entries[i]) + 2
        if used + size > limit:
            continue
        chosen.add(i)
        used += size
    return "\n\n".join(entries[i] for i in sorted(chosen)) or entries[0][:limit]


# ---------------------------------------------------------------- 処理


async def _classify(text: str) -> dict:
    fallback = {"kind": KIND_MEMO, "tags": [], "search": None}
    got = await claude_client.complete_json(
        _PROMPT.format(query=KIND_QUERY, memo=KIND_MEMO, candidates="、".join(settings.get("private_tags")),
                       default=idea.DEFAULT_TAG, text=text), fallback)
    return {**fallback, **got} if isinstance(got, dict) else fallback


async def _answer(message, text: str, info: dict) -> None:
    inbox = await asyncio.to_thread(notes.get_store().read, vault_paths.inbox())
    words = str(info.get("search") or "").split()
    prompt = _ANSWER.format(text=text, context=build_context(inbox or "", words))
    answer = await claude_client.complete(prompt, extra_system=_ANSWER_SYSTEM, max_tokens=1200)
    await util.ack(message, "💬")
    await util.send_long(message.channel, answer)


async def handle(message) -> None:
    text = (message.content or "").strip()
    if not text and not message.attachments:
        return
    now = util.now()
    info = await _classify(text) if text else {"kind": KIND_MEMO, "tags": [], "search": None}
    if normalize_kind(info.get("kind"), bool(message.attachments)) == KIND_QUERY:
        await _answer(message, text, info)
        return

    tags = idea.clean_tags(info.get("tags"), settings.get("private_tags"), idea.explicit_tags(text))
    files = await idea._save_attachments(message, now, path_fn=vault_paths.private_attachment)
    entry = idea.format_entry(now, tags, text, files)

    async with _write_lock:  # Obsidian を先に書く（失敗したらシートにも記録しない）
        await asyncio.to_thread(notes.get_store().prepend_entry, vault_paths.inbox(), entry, "Inbox")
    await util.ack(message, "📝")

    try:
        await asyncio.to_thread(sheets.append, sheets.PRIVATE, {
            "日時": util.fmt_datetime(now), "内容": idea.sheet_content(text, files),
            "タグ": " ".join(f"#{t}" for t in tags)})
    except Exception:  # noqa: BLE001
        log.warning("私生活メモをシートに記録できませんでした（Obsidian には保存済み）", exc_info=True)
        await util.ack(message, "⚠️")
