"""`09-idea`: 思考の何でもゴミ箱。**完全サイレント**（質問・提案・返信はしない。リアクションだけ）。

- Obsidian の `06-Life-OS/09-idea/Ideas.md` の見出し直下に、`## YYYY-MM-DD HH:MM` ＋タグ＋本文を挿入（最新が上）
- タグは AI が候補（settings の idea_tags）から自動で付ける。本文に自分で書いた `#タグ` も生かす
- 「〜を買う」「〜に連絡する」のような、やる行動が書かれていればバックログに連携する
- 画像は Obsidian の `09-idea/attachments/` に保存して埋め込む（Discord の添付 URL は失効するため）
- リアクション: 📝 保存した / 📋 タスクにも連携した / ⚠️ 保存はしたが、シートかタスクへの連携に失敗した
"""
from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from datetime import datetime

import claude_client
import notes
import settings
import sheets
import task_sync
import util
import vault_paths

log = logging.getLogger("life-os.idea")

SOURCE = "09-idea"
DEFAULT_TAG = "日常"
MAX_TAGS = 3
MAX_IMAGE_BYTES = 5 * 1024 * 1024  # GAS 中継の上限
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
_TYPE_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp"}
_HASHTAG = re.compile(r"(?<![\w#])#([^\s#　]+)")
_HEADING_LIKE = re.compile(r"^(\s*)(#{1,6})(\s)")

_write_lock = asyncio.Lock()  # Ideas.md の「読む→書き換える」を1件ずつにする

_PROMPT = """次は、ユーザーが「アイデアの何でもゴミ箱」に投げたメモです。JSONだけを返してください。
{{
  "tags": 内容に合うタグを、次の候補から1〜3個（先頭の # は付けない）: {candidates}。どれも合わなければ ["{default}"],
  "task": 「〜を買う」「〜に連絡する」「〜を予約する」のように、ユーザー自身がやる具体的な行動が書かれているときだけ、その行動を短い一文で。創作のアイデア・感想・迷いは、タスクにしない。無ければ null
}}

メモ:
{text}"""


# ---------------------------------------------------------------- 純粋関数


def norm_tag(tag: str) -> str:
    return unicodedata.normalize("NFKC", str(tag)).strip().lstrip("#＃").strip()


def explicit_tags(text: str) -> list[str]:
    """本文に自分で書いた `#タグ`。"""
    return [norm_tag(t) for t in _HASHTAG.findall(text) if norm_tag(t)]


def clean_tags(ai_tags, candidates: list[str], written: list[str]) -> list[str]:
    """本文に書かれたタグ（そのまま生かす）＋ AI が選んだ候補内のタグ。重複なし・最大3個・空なら既定。"""
    allowed = {norm_tag(c).lower(): norm_tag(c) for c in candidates}
    out: list[str] = []
    for t in written:
        if t and t not in out:
            out.append(t)
    for t in ai_tags if isinstance(ai_tags, list) else []:
        key = norm_tag(t).lower()
        if key in allowed and allowed[key] not in out:
            out.append(allowed[key])
    out = out[:MAX_TAGS]
    return out or [DEFAULT_TAG]


def escape_body(text: str) -> str:
    """本文の行頭の `# ` `## ` を、Ideas.md の見出し構造と混ざらないようにエスケープする。`#タグ` はそのまま。"""
    return "\n".join(_HEADING_LIKE.sub(r"\1\\\2\3", line) for line in text.split("\n"))


def format_entry(now: datetime, tags: list[str], text: str, files: list[dict]) -> str:
    """Ideas.md に挿入する1件分。files: {"name", "saved": bool, "image": bool, "reason": str}"""
    lines = [f"## {now:%Y-%m-%d %H:%M}", " ".join(f"#{t}" for t in tags)]
    if text:
        lines.append(escape_body(text))
    for f in files:
        if f["saved"] and f["image"]:
            lines.append(f"![[{f['name']}]]")
        elif f["saved"]:
            lines.append(f"📎 {f['name']}")
        else:
            lines.append(f"📎 {f['name']}（保存していません: {f['reason']}）")
    return "\n".join(lines)


def attachment_name(now: datetime, index: int, filename: str, content_type: str | None) -> str:
    ext = ""
    m = re.search(r"\.[A-Za-z0-9]{1,5}$", filename or "")
    if m and m.group(0).lower() in IMAGE_EXTS:
        ext = m.group(0).lower()
    elif (content_type or "").split(";")[0] in _TYPE_EXT:
        ext = _TYPE_EXT[(content_type or "").split(";")[0]]
    return f"{now:%Y%m%d-%H%M%S}-{index}{ext}"


def sheet_content(text: str, files: list[dict]) -> str:
    extra = [f"[添付: {f['name']}]" for f in files]
    return "\n".join([t for t in [text, " ".join(extra)] if t])


# ---------------------------------------------------------------- 処理


async def _classify(text: str) -> dict:
    fallback = {"tags": [], "task": None}
    got = await claude_client.complete_json(
        _PROMPT.format(candidates="、".join(settings.get("idea_tags")), default=DEFAULT_TAG, text=text), fallback)
    return got if isinstance(got, dict) else fallback


async def _save_attachments(message, now: datetime) -> list[dict]:
    store = notes.get_store()
    out = []
    for i, a in enumerate(message.attachments, 1):
        is_image = ((a.content_type or "").split(";")[0] in _TYPE_EXT) or (
            re.search(r"\.[A-Za-z0-9]{1,5}$", a.filename or "") is not None
            and re.search(r"\.[A-Za-z0-9]{1,5}$", a.filename).group(0).lower() in IMAGE_EXTS)
        name = attachment_name(now, i, a.filename, a.content_type)
        entry = {"name": name if is_image else (a.filename or name), "saved": False, "image": is_image, "reason": ""}
        if not is_image:
            entry["reason"] = "画像以外のファイル"
        elif (a.size or 0) > MAX_IMAGE_BYTES:
            entry["reason"] = "5MBを超えています"
        else:
            try:
                data = await a.read()
                await asyncio.to_thread(store.write_bytes, vault_paths.idea_attachment(name), data,
                                        (a.content_type or "image/png").split(";")[0])
                entry["saved"] = True
            except Exception:  # noqa: BLE001  画像が保存できなくても、メモ本体は残す
                log.warning("画像の保存に失敗しました: %s", a.filename, exc_info=True)
                entry["reason"] = "保存に失敗しました"
        out.append(entry)
    return out


async def handle(message) -> None:
    text = (message.content or "").strip()
    if not text and not message.attachments:
        return
    now = util.now()
    info = await _classify(text) if text else {"tags": [], "task": None}
    tags = clean_tags(info.get("tags"), settings.get("idea_tags"), explicit_tags(text))
    files = await _save_attachments(message, now)
    entry = format_entry(now, tags, text, files)

    async with _write_lock:  # Obsidian を先に書く（失敗したらシートにも記録しない）
        await asyncio.to_thread(notes.get_store().prepend_entry, vault_paths.ideas(), entry, "Ideas")
    await util.ack(message, "📝")

    warn = False
    try:
        await asyncio.to_thread(sheets.append, sheets.IDEAS, {
            "日時": util.fmt_datetime(now), "タグ": " ".join(f"#{t}" for t in tags), "内容": sheet_content(text, files)})
    except Exception:  # noqa: BLE001
        warn = True
        log.warning("アイデアをシートに記録できませんでした（Obsidian には保存済み）", exc_info=True)
    task = str(info.get("task") or "").strip()
    if task:
        try:
            await asyncio.to_thread(sheets.add_task, task, source=SOURCE)
            await asyncio.to_thread(task_sync.run_safely)
            await util.ack(message, "📋")
        except Exception:  # noqa: BLE001
            warn = True
            log.warning("タスクへの連携に失敗しました", exc_info=True)
    if warn:
        await util.ack(message, "⚠️")
