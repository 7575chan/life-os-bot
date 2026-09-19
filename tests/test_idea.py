import asyncio
from datetime import datetime
from types import SimpleNamespace as NS

import pytest

import claude_client
import config
import notes
import settings
import sheets
import task_sync
import vault_paths
from handlers import idea

IDEAS = vault_paths.ideas()
TAGS = ["小説", "TRPG", "仕事", "日常", "開発"]
NOW = datetime(2026, 9, 20, 8, 15, 30, tzinfo=config.TZ)


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- 純粋関数


def test_clean_tags_prefers_written_then_ai_within_candidates():
    assert idea.clean_tags(["小説", "TRPG"], TAGS, []) == ["小説", "TRPG"]
    assert idea.clean_tags(["#小説", "ＴＲＰＧ", "存在しないタグ"], TAGS, []) == ["小説", "TRPG"]  # 全角・#付き・候補外
    assert idea.clean_tags(["仕事"], TAGS, ["自分のタグ"]) == ["自分のタグ", "仕事"]  # 書いたタグを生かす
    assert idea.clean_tags(["小説", "小説", "仕事", "開発", "日常"], TAGS, []) == ["小説", "仕事", "開発"]  # 重複なし・最大3個
    assert idea.clean_tags(None, TAGS, []) == ["日常"] and idea.clean_tags("壊れた応答", TAGS, []) == ["日常"]


def test_explicit_hashtags():
    assert idea.explicit_tags("こんな話 #小説 #新作 です") == ["小説", "新作"]
    assert idea.explicit_tags("ヘッダー## や 色#fff は対象外") == []
    assert idea.explicit_tags("#最初のタグ") == ["最初のタグ"]


def test_escape_body_only_touches_heading_like_lines():
    assert idea.escape_body("## 見出し風\n#小説 タグ\n普通の行\n  # 字下げ") == "\\## 見出し風\n#小説 タグ\n普通の行\n  \\# 字下げ"


def test_format_entry_layout():
    files = [{"name": "a.png", "saved": True, "image": True, "reason": ""},
             {"name": "b.pdf", "saved": False, "image": False, "reason": "画像以外のファイル"}]
    text = idea.format_entry(NOW, ["小説", "仕事"], "本文\n二行目", files)
    assert text == ("## 2026-09-20 08:15\n#小説 #仕事\n本文\n二行目\n![[a.png]]\n📎 b.pdf（保存していません: 画像以外のファイル）")
    assert idea.format_entry(NOW, ["日常"], "", []) == "## 2026-09-20 08:15\n#日常"


def test_attachment_name_uses_safe_extension():
    assert idea.attachment_name(NOW, 1, "スケッチ.PNG", "image/png") == "20260920-081530-1.png"
    assert idea.attachment_name(NOW, 2, "image", "image/jpeg") == "20260920-081530-2.jpg"
    assert idea.attachment_name(NOW, 3, "x.exe", None) == "20260920-081530-3"  # 危険な拡張子は使わない


def test_sheet_content():
    assert idea.sheet_content("本文", [{"name": "a.png"}]) == "本文\n[添付: a.png]"
    assert idea.sheet_content("", [{"name": "a.png"}]) == "[添付: a.png]"


# ---------------------------------------------------------------- 処理（偽の Discord・AI・シート、実際のファイル）


class Att:
    def __init__(self, filename, content_type, data=b"x", size=None):
        self.filename, self.content_type, self.data = filename, content_type, data
        self.size = len(data) if size is None else size

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
    (tmp_path / "06-Life-OS" / "09-idea").mkdir(parents=True)
    (tmp_path / "06-Life-OS" / "09-idea" / "Ideas.md").write_text("", encoding="utf-8")
    store = notes.GuardedStore(notes.LocalStore(tmp_path))
    e = NS(store=store, root=tmp_path, sheet_rows=[], tasks=[], syncs=[], ai_calls=[],
           ai=lambda text: {"tags": ["小説"], "task": None}, sheet_fail=False)

    async def fake_ai(prompt, fallback, **kw):
        e.ai_calls.append(prompt)
        return e.ai(prompt)

    def fake_append(name, data):
        if e.sheet_fail:
            raise RuntimeError("sheet down")
        e.sheet_rows.append((name, data))
        return 2

    monkeypatch.setattr(notes, "get_store", lambda: store)
    monkeypatch.setattr(claude_client, "complete_json", fake_ai)
    monkeypatch.setattr(sheets, "append", fake_append)
    monkeypatch.setattr(sheets, "add_task", lambda content, **kw: e.tasks.append((content, kw)) or "lo-x")
    monkeypatch.setattr(task_sync, "run_safely", lambda: e.syncs.append(1))
    monkeypatch.setattr(settings, "get", lambda key: TAGS if key == "idea_tags" else None)
    monkeypatch.setattr(idea, "_write_lock", asyncio.Lock())
    e.ideas = lambda: (tmp_path / IDEAS).read_text(encoding="utf-8")
    return e


def test_records_silently_newest_first(env):
    m1, m2 = Msg("最初のアイデア"), Msg("あとのアイデア")
    run(idea.handle(m1))
    run(idea.handle(m2))
    text = env.ideas()
    assert text.startswith("# Ideas\n\n## ")
    assert text.index("あとのアイデア") < text.index("最初のアイデア")  # 最新が上
    assert "#小説" in text
    assert m1.channel.sent == [] and m2.channel.sent == []  # 完全サイレント（返信しない）
    assert m1.reactions == ["📝"]
    name, row = env.sheet_rows[0]
    assert name == "09-idea" and row["タグ"] == "#小説" and row["内容"] == "最初のアイデア"


def test_no_task_means_no_task_and_no_clipboard_reaction(env):
    m = Msg("こんな展開はどうだろう")
    run(idea.handle(m))
    assert env.tasks == [] and env.syncs == [] and m.reactions == ["📝"]


def test_action_item_is_linked_to_backlog(env):
    env.ai = lambda t: {"tags": ["日常"], "task": "牛乳を買う"}
    m = Msg("牛乳を買う。あと小説の設定も考えたい")
    run(idea.handle(m))
    assert env.tasks == [("牛乳を買う", {"source": "09-idea"})]  # 実行予定日なし＝バックログ
    assert env.syncs == [1] and m.reactions == ["📝", "📋"]
    assert "牛乳を買う" in env.ideas()  # メモとしても残る
    assert m.channel.sent == []


def test_ai_failure_still_saves_with_default_tag(env):
    env.ai = lambda t: {"tags": [], "task": None}  # complete_json のフォールバック値
    m = Msg("AI が使えなくても保存する")
    run(idea.handle(m))
    assert "#日常" in env.ideas() and "AI が使えなくても保存する" in env.ideas() and m.reactions == ["📝"]


def test_written_hashtag_kept_and_out_of_list_ai_tag_dropped(env):
    env.ai = lambda t: {"tags": ["勝手なタグ", "仕事"], "task": None}
    run(idea.handle(Msg("#新作 メモ")))
    assert "#新作 #仕事" in env.ideas() and "勝手なタグ" not in env.ideas()


def test_heading_like_lines_do_not_break_the_file_structure(env):
    run(idea.handle(Msg("## 偽の見出し\n本文")))
    text = env.ideas()
    assert text.count("\n## ") == 1  # 見出しは日時の1つだけ
    assert "\\## 偽の見出し" in text


def test_image_is_saved_and_embedded(env):
    m = Msg("スケッチ", [Att("手書き.png", "image/png", b"\x89PNG-data")])
    run(idea.handle(m))
    saved = list((env.root / "06-Life-OS" / "09-idea" / "attachments").glob("*.png"))
    assert len(saved) == 1 and saved[0].read_bytes() == b"\x89PNG-data"
    assert f"![[{saved[0].name}]]" in env.ideas()
    assert env.sheet_rows[0][1]["内容"].endswith(f"[添付: {saved[0].name}]")


def test_image_only_message_skips_ai_and_gets_default_tag(env):
    run(idea.handle(Msg("", [Att("s.jpg", "image/jpeg")])))
    assert env.ai_calls == [] and "#日常" in env.ideas() and "![[" in env.ideas()


def test_oversized_and_non_image_attachments_are_recorded_but_not_saved(env):
    big = Att("big.png", "image/png", b"x", size=idea.MAX_IMAGE_BYTES + 1)
    pdf = Att("資料.pdf", "application/pdf", b"%PDF")
    m = Msg("添付いろいろ", [big, pdf])
    run(idea.handle(m))
    text = env.ideas()
    assert "5MBを超えています" in text and "📎 資料.pdf（保存していません: 画像以外のファイル）" in text
    assert not (env.root / "06-Life-OS" / "09-idea" / "attachments").exists()
    assert m.reactions == ["📝"]


def test_image_save_failure_keeps_the_note(env, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk")

    monkeypatch.setattr(env.store, "write_bytes", boom)
    m = Msg("画像は失敗しても本文は残す", [Att("a.png", "image/png")])
    run(idea.handle(m))
    assert "画像は失敗しても本文は残す" in env.ideas() and "保存に失敗しました" in env.ideas() and m.reactions == ["📝"]


def test_obsidian_failure_records_nothing_elsewhere(env, monkeypatch):
    def boom(*a, **k):
        raise OSError("drive down")

    monkeypatch.setattr(env.store, "prepend_entry", boom)
    m = Msg("保存できない")
    with pytest.raises(OSError):
        run(idea.handle(m))
    assert env.sheet_rows == [] and env.tasks == [] and m.reactions == []  # Obsidian が先。失敗したら何も記録しない


def test_sheet_failure_keeps_obsidian_and_warns(env):
    env.sheet_fail = True
    env.ai = lambda t: {"tags": ["日常"], "task": "電話する"}
    m = Msg("保存はできた")
    run(idea.handle(m))
    assert "保存はできた" in env.ideas() and m.reactions == ["📝", "📋", "⚠️"]
    assert m.channel.sent == []


def test_task_failure_warns_but_note_is_saved(env, monkeypatch):
    env.ai = lambda t: {"tags": ["日常"], "task": "電話する"}
    monkeypatch.setattr(sheets, "add_task", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    m = Msg("電話する")
    run(idea.handle(m))
    assert m.reactions == ["📝", "⚠️"] and "電話する" in env.ideas()


def test_empty_message_is_ignored(env):
    m = Msg("   ")
    run(idea.handle(m))
    assert env.ideas() == "" and env.sheet_rows == [] and m.reactions == []


def test_concurrent_messages_are_not_lost(env):
    async def many():
        await asyncio.gather(*(idea.handle(Msg(f"同時のメモ{i}")) for i in range(8)))

    run(many())
    text = env.ideas()
    assert all(f"同時のメモ{i}" in text for i in range(8)) and text.count("\n## ") == 8
    assert len(env.sheet_rows) == 8


def test_only_the_idea_room_paths_are_written(env):
    run(idea.handle(Msg("書き込み先の確認", [Att("a.png", "image/png")])))
    written = {p.relative_to(env.root).as_posix() for p in env.root.rglob("*") if p.is_file()}
    assert all(p.startswith("06-Life-OS/09-idea/") for p in written), written
