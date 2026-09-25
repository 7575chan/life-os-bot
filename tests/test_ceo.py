import asyncio
from datetime import datetime
from types import SimpleNamespace as NS

import pytest

import claude_client
import config
import notes
import sheets
import util
import vault_paths
from handlers import ceo

DIRECTIVES = vault_paths.ceo_directives()
NOW = datetime(2026, 10, 12, 9, 30, tzinfo=config.TZ)


def run(coro):
    return asyncio.run(coro)


class Msg:
    def __init__(self, content=""):
        self.content, self.attachments = content, []
        self.reactions, self.channel = [], NS(sent=[])
        self.channel.send = self._send

    async def _send(self, *a, **k):
        self.channel.sent.append((a, k))

    async def add_reaction(self, emoji):
        self.reactions.append(emoji)


@pytest.fixture
def env(tmp_path, monkeypatch):
    (tmp_path / "06-Life-OS" / "13-im-the-ceo").mkdir(parents=True)
    (tmp_path / DIRECTIVES).write_text("", encoding="utf-8")
    store = notes.GuardedStore(notes.LocalStore(tmp_path))
    e = NS(store=store, root=tmp_path, rows=[], invalidated=[], sheet_fail=False)

    def fake_append(name, data):
        if e.sheet_fail:
            raise RuntimeError("sheet down")
        e.rows.append((name, data))
        return 2

    monkeypatch.setattr(notes, "get_store", lambda: store)
    monkeypatch.setattr(sheets, "append", fake_append)
    monkeypatch.setattr(util, "now", lambda: NOW)
    monkeypatch.setattr(claude_client, "invalidate_ceo_cache", lambda: e.invalidated.append(1))
    monkeypatch.setattr(ceo, "_write_lock", asyncio.Lock())
    e.text = lambda: (tmp_path / DIRECTIVES).read_text(encoding="utf-8")
    return e


def test_records_directive_newest_first_and_silently(env):
    m1, m2 = Msg("今年の目標: 自社プロダクトで月30万円"), Msg("方針変更: 今期はリリースに集中する")
    run(ceo.handle(m1))
    run(ceo.handle(m2))
    text = env.text()
    assert text.startswith("# CEO-Directives\n\n## 2026-10-12 09:30\n方針変更")
    assert text.index("方針変更") < text.index("今年の目標")  # 最新が上（長くなっても、新しい方針が切り捨てられない）
    assert m1.reactions == ["👑"] and m1.channel.sent == [] and m2.channel.sent == []
    assert env.rows[0] == ("13-im-the-ceo", {"日時": "2026-10-12 09:30", "内容": "今年の目標: 自社プロダクトで月30万円"})


def test_cache_is_invalidated_so_the_next_call_uses_the_new_directive(env):
    run(ceo.handle(Msg("方針")))
    assert env.invalidated == [1]


def test_existing_content_is_kept(env):
    (env.root / DIRECTIVES).write_text("# CEO-Directives\n\n## 2026-09-01 08:00\n前からある方針\n", encoding="utf-8")
    run(ceo.handle(Msg("あたらしい方針")))
    text = env.text()
    assert "前からある方針" in text and text.index("あたらしい方針") < text.index("前からある方針")


def test_heading_like_lines_do_not_break_the_file(env):
    run(ceo.handle(Msg("## 偽の見出し\n本文")))
    assert env.text().count("\n## ") == 1 and "\\## 偽の見出し" in env.text()


def test_obsidian_failure_records_nothing_elsewhere(env, monkeypatch):
    def boom(*a, **k):
        raise OSError("drive down")

    monkeypatch.setattr(env.store, "prepend_entry", boom)
    m = Msg("保存できない")
    with pytest.raises(OSError):
        run(ceo.handle(m))
    assert env.rows == [] and m.reactions == [] and env.invalidated == []


def test_sheet_failure_keeps_the_directive_and_warns(env):
    env.sheet_fail = True
    m = Msg("保存はできた")
    run(ceo.handle(m))
    assert "保存はできた" in env.text() and m.reactions == ["👑", "⚠️"] and env.invalidated == [1]


def test_empty_message_is_ignored(env):
    m = Msg("  ")
    run(ceo.handle(m))
    assert env.text() == "" and env.rows == [] and m.reactions == []


def test_concurrent_posts_are_not_lost(env):
    async def many():
        await asyncio.gather(*(ceo.handle(Msg(f"同時の方針{i}")) for i in range(6)))

    run(many())
    assert all(f"同時の方針{i}" in env.text() for i in range(6)) and env.text().count("\n## ") == 6


def test_only_the_ceo_room_is_written(env):
    run(ceo.handle(Msg("書き込み先の確認")))
    written = {p.relative_to(env.root).as_posix() for p in env.root.rglob("*") if p.is_file()}
    assert written == {DIRECTIVES}


def test_system_prompt_puts_the_directives_first_and_says_newer_wins(monkeypatch):
    async def directives():
        return "## 2026-10-12 09:30\n新しい方針\n\n## 2026-09-01 08:00\n古い方針"

    monkeypatch.setattr(claude_client, "ceo_directives", directives)
    prompt = asyncio.run(claude_client.system_prompt("追加の指示"))
    assert prompt.index("【CEO方針") < prompt.index("追加の指示") and "新しい方をとる" in prompt and "新しい方針" in prompt
    assert prompt.index("09:30\n新しい方針") < prompt.index("08:00\n古い方針")  # 方針の並びは、ファイルのまま（最新が上）
