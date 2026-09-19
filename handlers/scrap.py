"""`08-scrap`: URL を貼るだけで、本文を取得し、3行要約とタグを付けて保存する（SPEC §5）。

- Obsidian: `06-Life-OS/08-scrap/<日付>_<タイトル>.md`（メタデータ・要約・本文全文・アイキャッチ1枚）。画像は `attachments/`
- シート `08-scrap`: 日時 / タイトル / URL / 3行要約 / タグ / Obsidianリンク
- タグ: 投稿時に `#タグ` を書けばそれだけを使う。無ければ AI が3〜5個付ける
- Bot の返信に「#イラスト を追加」「タグを #設定 に変更」「#AI を削除」と返信すると、ノートとシートのタグを直す
- 本文を取得できなかったとき（ログインが必要など）は、URL とタイトルだけ保存して知らせる
"""
from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from datetime import datetime
from urllib.parse import urlsplit

import claude_client
import notes
import settings
import sheets
import state
import util
import vault_paths
import webfetch
from handlers.idea import explicit_tags, norm_tag

log = logging.getLogger("life-os.scrap")

KIND = "scrap"
MAX_URLS = 3
MAX_TAGS = 10
DEFAULT_TAG = "スクラップ"
BODY_LIMIT = 200_000
AI_TEXT_LIMIT = 12_000
_IMAGE_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp"}

_PROMPT = """次は、ユーザーが保存した Web ページの情報です。JSONだけを返してください。
{{
  "title": ページの内容を表す短いタイトル（元のタイトルが適切ならそのまま。60字以内）,
  "summary": 内容の要約を{lines}行の配列（各行80字以内。書かれている事実だけ。感想・評価・助言は書かない）,
  "tags": 内容に合うタグを{count}（先頭の # は付けない。短い名詞。日本語でよい）
}}

元のタイトル: {title}
URL: {url}
本文:
{text}"""


# ---------------------------------------------------------------- 純粋関数


def clean_tags(tags, limit: int = MAX_TAGS) -> list[str]:
    out: list[str] = []
    for t in tags if isinstance(tags, list) else []:
        t = norm_tag(t)
        t = re.sub(r"[\s#]+", "", t)
        if t and t not in out:
            out.append(t)
    return out[:limit]


def written_tags(text: str) -> list[str]:
    """投稿に書かれた `#タグ`（URL の #断片は含めない）。"""
    return clean_tags(explicit_tags(webfetch.strip_urls(text)))


def parse_tag_edit(text: str) -> tuple[str, list[str]] | None:
    """返信からタグの直し方を読む。(mode, tags)。mode: add / set / remove。タグ（#付き）が無ければ None。"""
    t = unicodedata.normalize("NFKC", text)
    tags = clean_tags(explicit_tags(t))
    if not tags:
        return None
    if re.search(r"削除|外し|消し|除い|取っ|取り|いらない|不要", t):
        return "remove", tags
    if re.search(r"変更|変え|置き換|入れ替|にして|に直し|だけ|のみ|にする", t) and not re.search(r"追加|足し|加え|付け|つけ", t):
        return "set", tags
    return "add", tags


def apply_tag_edit(current: list[str], mode: str, tags: list[str]) -> list[str]:
    if mode == "set":
        return clean_tags(tags)
    if mode == "remove":
        return [t for t in current if t not in tags]
    return clean_tags(list(current) + tags)


def summary_lines(raw, want: int = 3) -> list[str]:
    lines = [re.sub(r"\s+", " ", str(x)).strip("・-• 　") for x in raw] if isinstance(raw, list) else []
    return [x for x in lines if x][: max(want, 1)]


def ai_line_count() -> int:
    m = re.search(r"\d+", str(settings.get("summary_length")))
    return int(m.group(0)) if m else 3


def note_name(day: str, title: str) -> str:
    return f"{day}_{util.safe_filename(title, 50)}"


def render_note(*, title: str, url: str, day: str, tags: list[str], summary: list[str], body: str,
                image_name: str | None, notes_: list[str]) -> str:
    fm_title = title.replace('"', "'").replace("\n", " ")
    lines = ["---", f'title: "{fm_title}"', f"url: {url}", f"date: {day}", f"tags: [{', '.join(tags)}]", "---", "",
             f"# {title}", "", "タグ: " + " ".join(f"#{t}" for t in tags), ""]
    if image_name:
        lines += [f"![[{image_name}]]", ""]
    for n in notes_:
        lines += [f"> ⚠️ {n}", ""]
    if summary:
        lines += ["## 3行要約", ""] + [f"- {s}" for s in summary] + [""]
    if body:
        lines += ["## 本文", "", body[:BODY_LIMIT], ""]
    return "\n".join(lines).rstrip("\n") + "\n"


def retag_note(text: str, tags: list[str]) -> str:
    """ノート内のタグ（メタデータの `tags:` 行と、本文先頭の `タグ:` 行）だけを書き換える。"""
    text = re.sub(r"^tags:.*$", f"tags: [{', '.join(tags)}]", text, count=1, flags=re.M)
    return re.sub(r"^タグ:.*$", "タグ: " + " ".join(f"#{t}" for t in tags), text, count=1, flags=re.M)


def format_reply(title: str, summary: list[str], tags: list[str], notes_: list[str]) -> str:
    lines = [f"📰 {title}"]
    lines += [f"・{s}" for s in summary]
    lines += [f"⚠️ {n}" for n in notes_]
    lines.append("タグ: " + (" ".join(f"#{t}" for t in tags) or "なし"))
    lines.append("（タグを直すときは、この返信に「#イラスト を追加」「タグを #設定 に変更」「#AI を削除」と返信してください）")
    return "\n".join(lines)


# ---------------------------------------------------------------- 処理


async def _summarize(art: webfetch.Article, url: str) -> dict:
    lines = ai_line_count()
    fallback = {"title": art.title, "summary": [], "tags": []}
    if not art.text:
        return fallback
    got = await claude_client.complete_json(
        _PROMPT.format(lines=lines, count=str(settings.get("scrap_tag_count")), title=art.title or "(なし)", url=url,
                       text=art.text[:AI_TEXT_LIMIT]), fallback, max_tokens=1200)
    return got if isinstance(got, dict) else fallback


async def _unique_path(store, day: str, title: str) -> str:
    base = note_name(day, title)
    for i in range(1, 20):
        path = vault_paths.scrap(base if i == 1 else f"{base}-{i}")
        if await asyncio.to_thread(store.read, path) is None:
            return path
    return vault_paths.scrap(f"{base}-{util.now():%H%M%S}")


async def _save_eyecatch(store, art: webfetch.Article, base: str) -> str | None:
    if not art.image_url:
        return None
    try:
        data, ctype = await asyncio.to_thread(webfetch.fetch_image, art.image_url)
        name = f"{base}{_IMAGE_EXT.get(ctype, '.img')}"
        await asyncio.to_thread(store.write_bytes, vault_paths.scrap_attachment(name), data, ctype)
        return name
    except Exception:  # noqa: BLE001  アイキャッチが保存できなくても、記事は保存する
        log.warning("アイキャッチを保存できませんでした: %s", art.image_url, exc_info=True)
        return None


async def _save_one(message, url: str, user_tags: list[str]) -> None:
    store = notes.get_store()
    channel = message.channel
    failed = False
    try:
        art = await asyncio.to_thread(webfetch.fetch_article, url)
    except webfetch.UnsafeUrl as e:  # 安全のために拒否した URL は、保存せずに断る
        await util.ack(message, "🚫")
        await channel.send(f"🚫 この URL は保存できません: {e}")
        return
    except webfetch.FetchError as e:  # 404・時間切れなど: ブックマークとして URL だけ保存する
        art, failed = webfetch.Article(title="", final_url=url, notes=[f"{e}。URL だけ保存しました"]), True
    notes_ = list(art.notes)
    ai = await _summarize(art, url)
    title = (str(ai.get("title") or "").strip() or art.title or urlsplit(url).hostname or "ページ")[:80]
    summary = summary_lines(ai.get("summary"), ai_line_count())
    if art.text and not summary:  # AI が使えなかったとき: 本文の冒頭を要約の代わりにする
        summary = [re.sub(r"\s+", " ", art.text)[:120]]
    tags = user_tags or clean_tags(ai.get("tags"), 5) or [DEFAULT_TAG]

    now = util.now()
    day = util.fmt_date(now.date())
    path = await _unique_path(store, day, title)
    image = await _save_eyecatch(store, art, path.rsplit("/", 1)[-1][:-3])
    note = render_note(title=title, url=url, day=day, tags=tags, summary=summary, body=art.text,
                       image_name=image, notes_=notes_)
    link = await asyncio.to_thread(store.write, path, note)  # Obsidian を先に書く。失敗したら何も記録しない
    await util.ack(message, "📰")

    warn = failed or bool(notes_)
    try:
        await asyncio.to_thread(sheets.append, sheets.ARTICLES, {
            "日時": util.fmt_datetime(now), "タイトル": title, "URL": url, "3行要約": "\n".join(summary),
            "タグ": " ".join(f"#{t}" for t in tags), "Obsidianリンク": link})
    except Exception:  # noqa: BLE001
        warn = True
        log.warning("記事をシートに記録できませんでした（Obsidian には保存済み）", exc_info=True)
    if warn:
        await util.ack(message, "⚠️")
    reply = await channel.send(format_reply(title, summary, tags, notes_))
    state.put_pending(reply.id, channel.id, KIND, {"url": url, "note": path, "title": title, "tags": tags})


async def _edit_tags(message, pending: dict) -> None:
    channel = message.channel
    edit = parse_tag_edit(message.content)
    if edit is None:
        await channel.send("タグを # 付きで教えてください（例: 「#イラスト を追加」「タグを #設定 に変更」「#AI を削除」）")
        return
    mode, tags = edit
    payload = pending["payload"]
    store = notes.get_store()
    note = await asyncio.to_thread(store.read, payload["note"])
    if note is None:
        await channel.send("保存したノートが見つかりませんでした（移動・削除された可能性があります）。")
        return
    new_tags = apply_tag_edit(payload["tags"], mode, tags)
    await asyncio.to_thread(store.write, payload["note"], retag_note(note, new_tags))  # Obsidian を先に
    try:
        rows = await asyncio.to_thread(sheets.records, sheets.ARTICLES)
        hit = [r for r, rec in rows if rec["URL"] == payload["url"]]
        if hit:
            await asyncio.to_thread(sheets.update_row, sheets.ARTICLES, hit[-1], {"タグ": " ".join(f"#{t}" for t in new_tags)})
    except Exception:  # noqa: BLE001
        log.warning("シートのタグを更新できませんでした（ノートは更新済み）", exc_info=True)
        await util.ack(message, "⚠️")
    payload["tags"] = new_tags
    state.put_pending(pending["message_id"], pending["channel_id"], KIND, payload)
    await util.ack(message, "🏷️")
    await channel.send("タグを更新しました：" + (" ".join(f"#{t}" for t in new_tags) or "なし"))


async def handle(message) -> None:
    text = (message.content or "").strip()
    if not text:
        return
    ref = getattr(message, "reference", None)
    if ref and ref.message_id:  # Bot の返信へのリプライ = タグの直し
        pending = state.get_pending(ref.message_id)
        if pending and pending["kind"] == KIND:
            await _edit_tags(message, pending)
            return
    urls = webfetch.find_urls(text)
    if not urls:
        await message.channel.send("URL を貼ると、要約してタグを付けて保存します🔖（例: https://… #デザイン）")
        return
    tags = written_tags(text)
    for url in urls[:MAX_URLS]:
        await _save_one(message, url, tags)
    if len(urls) > MAX_URLS:
        await message.channel.send(f"URL は一度に{MAX_URLS}件まで保存します。残りは、もう一度送ってください。")
