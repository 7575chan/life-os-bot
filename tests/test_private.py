import asyncio
from datetime import datetime
from types import SimpleNamespace as NS

import pytest

import claude_client
import config
import notes
import settings
import sheets
import util
import vault_paths
from handlers import private

INBOX = vault_paths.inbox()
TAGS = ["行きたい", "買いたい", "予定", "感想", "日常"]


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- 純粋関数


def test_normalize_kind_defaults_to_memo():
    assert private.normalize_kind("query", False) == "query"
    assert private.normalize_kind("memo", False) == "memo"
    assert private.normalize_kind(None, False) == "memo" and private.normalize_kind("壊れた応答", False) == "memo"
    assert private.normalize_kind("query", True) == "memo"  # 添付つきの投稿は、問い合わせに見えてもメモとして残す


def test_split_entries_skips_preamble_and_keeps_bodies():
    text = "# Inbox\n\n## 2026-09-20 10:00\n#日常\nあとのメモ\n\n## 2026-09-19 09:00\n#行きたい\n最初のメモ\n"
    assert private.split_entries(text) == ["## 2026-09-20 10:00\n#日常\nあとのメモ", "## 2026-09-19 09:00\n#行きたい\n最初のメモ"]
    assert private.split_entries("") == [] and private.split_entries("# Inbox\n") == []


def test_build_context_prefers_matches_then_recent_and_keeps_order():
    text = "# Inbox\n\n" + "\n\n".join(f"## 2026-09-{d:02d} 10:00\n#日常\n{'カフェ' if d == 1 else '雑談'}{'あ' * 50}" for d in (5, 4, 3, 2, 1))
    small = private.build_context(text, ["カフェ"], limit=200)  # 1件が約 40 文字。枠は 4 件ほど
    assert "カフェ" in small  # いちばん古くても、探す言葉を含む投稿は入る
    assert small.index("09-05") < small.index("09-01")  # 並びは元のまま（新しいものが上）
    assert len(small) <= 200
    assert private.build_context(text, [], limit=10_000).count("## ") == 5
    assert private.build_context("", ["カフェ"]) == "（メモはまだありません）"


def test_build_context_never_returns_empty_for_one_huge_entry():
    text = "## 2026-09-20 10:00\n" + "あ" * 500
    assert private.build_context(text, [], limit=100).startswith("## 2026-09-20")


# ---------------------------------------------------------------- 処理（偽の Discord・AI・シート、実際のファイル）


class Att:
    def __init__(self, filename, content_type, data=b"x"):
        self.filename, self.content_type, self.data, self.size = filename, content_type, data, len(data)

    async def read(self):
        return self.data


class Msg:
    def __init__(self, content="", attachments=None):
        self.content, self.attachments = content, attachments or []
        self.reactions, self.channel = [], NS(sent=[])
        self.channel.send = self._send

    async def _send(self, *a, **k):
        self.channel.sent.append((a, k))

    async def add_reaction(self, emoji):
        self.reactions.append(emoji)


@pytest.fixture
def env(tmp_path, monkeypatch):
    (tmp_path / "06-Life-OS" / "05-private").mkdir(parents=True)
    (tmp_path / "06-Life-OS" / "05-private" / "Inbox.md").write_text("", encoding="utf-8")
    store = notes.GuardedStore(notes.LocalStore(tmp_path))
    e = NS(store=store, root=tmp_path, sheet_rows=[], ai_calls=[], answers=[], sheet_fail=False,
           ai=lambda text: {"kind": "memo", "tags": ["日常"], "search": None})

    async def fake_ai(prompt, fallback, **kw):
        e.ai_calls.append(prompt)
        return e.ai(prompt)

    async def fake_complete(prompt, **kw):
        e.answers.append((prompt, kw))
        return "・カフェ「ひなた」（9/19）"

    def fake_append(name, data):
        if e.sheet_fail:
            raise RuntimeError("sheet down")
        e.sheet_rows.append((name, data))
        return 2

    monkeypatch.setattr(notes, "get_store", lambda: store)
    monkeypatch.setattr(claude_client, "complete_json", fake_ai)
    monkeypatch.setattr(claude_client, "complete", fake_complete)
    monkeypatch.setattr(sheets, "append", fake_append)
    monkeypatch.setattr(settings, "get", lambda key: TAGS if key == "private_tags" else None)
    monkeypatch.setattr(private, "_write_lock", asyncio.Lock())
    monkeypatch.setattr(util, "now", lambda: datetime(2026, 9, 20, 8, 15, tzinfo=config.TZ))
    e.inbox = lambda: (tmp_path / INBOX).read_text(encoding="utf-8")
    return e


def test_records_silently_newest_first(env):
    m1, m2 = Msg("駅前のカフェに行きたい"), Msg("新しいシャツがほしい")
    env.ai = lambda t: {"kind": "memo", "tags": ["行きたい"], "search": None}
    run(private.handle(m1))
    run(private.handle(m2))
    text = env.inbox()
    assert text.startswith("# Inbox\n\n## 2026-09-20 08:15\n#行きたい")
    assert text.index("新しいシャツがほしい") < text.index("駅前のカフェに行きたい")  # 最新が上
    assert m1.channel.sent == [] and m2.channel.sent == []  # ふだんはサイレント
    assert m1.reactions == ["📝"] and env.answers == []
    name, row = env.sheet_rows[0]
    assert name == "05-private" and row == {"日時": "2026-09-20 08:15", "内容": "駅前のカフェに行きたい", "タグ": "#行きたい"}


def test_existing_content_is_kept(env):
    (env.root / INBOX).write_text("# Inbox\n\n## 2026-09-01 10:00\n#日常\n前からあるメモ\n", encoding="utf-8")
    run(private.handle(Msg("あたらしいメモ")))
    text = env.inbox()
    assert "前からあるメモ" in text and text.index("あたらしいメモ") < text.index("前からあるメモ")


def test_query_replies_from_inbox_and_is_not_recorded(env):
    (env.root / INBOX).write_text("# Inbox\n\n## 2026-09-19 09:00\n#行きたい\n駅前のカフェ「ひなた」に行きたい\n", encoding="utf-8")
    env.ai = lambda t: {"kind": "query", "tags": [], "search": "カフェ"}
    before = env.inbox()
    m = Msg("行きたいって言ってたカフェ教えて")
    run(private.handle(m))
    assert m.reactions == ["💬"] and len(m.channel.sent) == 1
    prompt, kw = env.answers[0]
    assert "カフェ「ひなた」" in prompt and "行きたいって言ってたカフェ教えて" in prompt
    assert "推測せず" in kw["extra_system"]
    assert env.inbox() == before and env.sheet_rows == []  # 問い合わせ自体は残さない


def test_query_with_attachment_is_kept_as_memo(env):
    env.ai = lambda t: {"kind": "query", "tags": ["日常"], "search": "カフェ"}
    m = Msg("このカフェどこだっけ", [Att("店.png", "image/png", b"PNG")])
    run(private.handle(m))
    assert m.reactions == ["📝"] and env.answers == [] and "このカフェどこだっけ" in env.inbox()
    assert len(list((env.root / "06-Life-OS" / "05-private" / "attachments").glob("*.png"))) == 1


def test_ai_failure_saves_as_memo_with_default_tag(env):
    env.ai = lambda t: {"kind": "memo", "tags": [], "search": None}  # complete_json のフォールバック値
    m = Msg("AI が使えなくても保存する")
    run(private.handle(m))
    assert "#日常" in env.inbox() and "AI が使えなくても保存する" in env.inbox() and m.reactions == ["📝"]


def test_written_hashtag_kept_and_out_of_list_ai_tag_dropped(env):
    env.ai = lambda t: {"kind": "memo", "tags": ["勝手なタグ", "予定"], "search": None}
    run(private.handle(Msg("#友達 土曜に会う")))
    assert "#友達 #予定" in env.inbox() and "勝手なタグ" not in env.inbox()


def test_heading_like_lines_do_not_break_the_file_structure(env):
    run(private.handle(Msg("## 偽の見出し\n本文")))
    text = env.inbox()
    assert text.count("\n## ") == 1 and "\\## 偽の見出し" in text


def test_image_only_message_skips_ai(env):
    m = Msg("", [Att("s.jpg", "image/jpeg")])
    run(private.handle(m))
    assert env.ai_calls == [] and "#日常" in env.inbox() and "![[" in env.inbox() and m.reactions == ["📝"]


def test_obsidian_failure_records_nothing_elsewhere(env, monkeypatch):
    def boom(*a, **k):
        raise OSError("drive down")

    monkeypatch.setattr(env.store, "prepend_entry", boom)
    m = Msg("保存できない")
    with pytest.raises(OSError):
        run(private.handle(m))
    assert env.sheet_rows == [] and m.reactions == []  # Obsidian が先。失敗したら何も記録しない


def test_sheet_failure_keeps_obsidian_and_warns(env):
    env.sheet_fail = True
    m = Msg("保存はできた")
    run(private.handle(m))
    assert "保存はできた" in env.inbox() and m.reactions == ["📝", "⚠️"] and m.channel.sent == []


def test_empty_message_is_ignored(env):
    m = Msg("   ")
    run(private.handle(m))
    assert env.inbox() == "" and env.sheet_rows == [] and m.reactions == []


def test_concurrent_messages_are_not_lost(env):
    async def many():
        await asyncio.gather(*(private.handle(Msg(f"同時のメモ{i}")) for i in range(8)))

    run(many())
    text = env.inbox()
    assert all(f"同時のメモ{i}" in text for i in range(8)) and text.count("\n## ") == 8 and len(env.sheet_rows) == 8


def test_only_the_private_room_paths_are_written(env):
    run(private.handle(Msg("書き込み先の確認", [Att("a.png", "image/png")])))
    written = {p.relative_to(env.root).as_posix() for p in env.root.rglob("*") if p.is_file()}
    assert all(p.startswith("06-Life-OS/05-private/") for p in written), written
