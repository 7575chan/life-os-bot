"""`02-health` の取扱マニュアル（handlers.health.update_manual / 「取扱マニュアル」の投稿）。"""
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
from handlers import health
from tests.fake_sheets import FakeSheets

NOW = datetime(2026, 9, 27, 20, 0, tzinfo=config.TZ)
MANUAL = vault_paths.health_manual()
PREVIOUS = vault_paths.health_manual_previous()


def run(coro):
    return asyncio.run(coro)


class Msg:
    def __init__(self, content=""):
        self.content, self.attachments = content, []
        self.reactions, self.sent = [], []
        self.channel = NS(send=self._send)

    async def _send(self, text, **k):
        self.sent.append(text)

    async def add_reaction(self, emoji):
        self.reactions.append(emoji)


@pytest.fixture
def env(tmp_path, monkeypatch):
    fs = FakeSheets().install(monkeypatch)
    (tmp_path / "06-Life-OS" / "02-health").mkdir(parents=True)
    (tmp_path / MANUAL).write_text("", encoding="utf-8")
    store = notes.GuardedStore(notes.LocalStore(tmp_path))
    e = NS(fs=fs, root=tmp_path, store=store, prompts=[], answer="・睡眠が長い日は、ご機嫌度が高めです", fail_ai=False)

    async def fake_complete(prompt, **kw):
        e.prompts.append(prompt)
        if e.fail_ai:
            raise RuntimeError("api down")
        return e.answer

    def between(name, start, end, date_col=None):  # sheets.between は、records の日付で絞る
        col = sheets.SCHEMA[name][0]
        return [(r, rec) for r, rec in fs.records(name) if (d := util.parse_date_any(rec[col])) and start <= d < end]

    monkeypatch.setattr(sheets, "between", between)
    monkeypatch.setattr(notes, "get_store", lambda: store)
    monkeypatch.setattr(claude_client, "complete", fake_complete)
    monkeypatch.setattr(util, "now", lambda: NOW)
    fs.seed(sheets.HEALTH,
            {"日時": "2026-09-20 08:00", "睡眠時間": "7", "体調スコア": 2, "AI判定": "🟡 ふつう"},
            {"日時": "2026-09-22 08:00", "睡眠時間": "10", "体調スコア": 4, "AI判定": "🟢 良好"},
            {"日時": "2026-05-01 08:00", "睡眠時間": "6", "体調スコア": 1, "AI判定": "🔴 警戒"})  # 90日より前
    fs.seed(sheets.DIARY, {"日付": "2026-09-20", "ご機嫌度": 2}, {"日付": "2026-09-22", "ご機嫌度": 4})
    e.text = lambda: (tmp_path / MANUAL).read_text(encoding="utf-8")
    return e


def test_update_writes_the_manual_and_a_report_sheet_row(env):
    body = run(health.update_manual(NOW.date()))
    assert body == env.answer
    assert env.text() == f"# 自分取扱マニュアル\n\n更新: 2026-09-27 20:00\n\n{env.answer}\n"
    rows = env.fs.data[sheets.REPORTS]
    assert len(rows) == 1 and (rows[0]["発行日"], rows[0]["種別"]) == ("2026-09-27", "取扱マニュアル") and env.answer in rows[0]["本文"]


def test_only_the_last_90_days_and_the_computed_table_go_to_the_ai(env):
    run(health.update_manual(NOW.date()))
    prompt = env.prompts[0]
    assert "2026-09-20 08:00" in prompt and "2026-09-22 08:00" in prompt and "2026-05-01" not in prompt  # 90日より前は渡さない
    assert "'under'" in prompt and "'over'" in prompt and "数字は与えられた集計をそのまま使う" in prompt  # 集計は、コードで計算したものを渡す


def test_previous_content_is_kept_before_overwriting(env):
    (env.root / MANUAL).write_text("# 自分取扱マニュアル\n\n手で書き足したメモ\n", encoding="utf-8")
    run(health.update_manual(NOW.date()))
    assert (env.root / PREVIOUS).read_text(encoding="utf-8") == "# 自分取扱マニュアル\n\n手で書き足したメモ\n"
    assert "手で書き足したメモ" not in env.text() and env.answer in env.text()
    env.answer = "・2回目の内容"
    run(health.update_manual(NOW.date()))
    assert "・睡眠が長い日" in (env.root / PREVIOUS).read_text(encoding="utf-8")  # 前回の版だけを残す（1世代）


def test_no_backup_when_the_manual_was_empty(env):
    run(health.update_manual(NOW.date()))
    assert not (env.root / PREVIOUS).exists()


def test_does_not_overwrite_when_the_backup_fails(env, monkeypatch):
    (env.root / MANUAL).write_text("大事な手書き\n", encoding="utf-8")
    real = env.store.write

    def write(path, text):
        if path == PREVIOUS:
            raise OSError("drive down")
        return real(path, text)

    monkeypatch.setattr(env.store, "write", write)
    with pytest.raises(OSError):
        run(health.update_manual(NOW.date()))
    assert env.text() == "大事な手書き\n" and env.fs.data[sheets.REPORTS] == []  # 残せなければ、上書きしない


def test_does_not_overwrite_with_an_empty_answer(env):
    (env.root / MANUAL).write_text("いまの内容\n", encoding="utf-8")
    env.answer = "  "
    with pytest.raises(ValueError):
        run(health.update_manual(NOW.date()))
    assert env.text() == "いまの内容\n" and not (env.root / PREVIOUS).exists()


def test_ai_failure_changes_nothing(env):
    (env.root / MANUAL).write_text("いまの内容\n", encoding="utf-8")
    env.fail_ai = True
    with pytest.raises(RuntimeError):
        run(health.update_manual(NOW.date()))
    assert env.text() == "いまの内容\n" and env.fs.data[sheets.REPORTS] == []


def test_sheet_failure_keeps_the_note(env, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("sheet down")

    monkeypatch.setattr(sheets, "append", boom)
    assert run(health.update_manual(NOW.date())) == env.answer and env.answer in env.text()


def test_saying_the_word_in_02_health_updates_the_manual(env):
    m = Msg("取扱マニュアルを更新して")
    run(health.handle(m))
    assert m.reactions == ["📖"] and m.sent[0].startswith("自分取扱マニュアルを更新しました📖\n\n") and env.answer in m.sent[0]
    assert env.answer in env.text() and len(env.fs.data[sheets.REPORTS]) == 1


def test_only_the_health_room_is_written(env):
    (env.root / MANUAL).write_text("いまの内容\n", encoding="utf-8")
    run(health.update_manual(NOW.date()))
    written = {p.relative_to(env.root).as_posix() for p in env.root.rglob("*") if p.is_file()}
    assert written == {MANUAL, PREVIOUS}
