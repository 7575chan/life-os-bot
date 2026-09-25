import asyncio
import json
from datetime import date, datetime

import pytest

import ai_tools
import config
import notes
import settings
import sheets
import task_sync
import util
import writing_log
from tests.fake_sheets import FakeSheets

LEDGER, TASKS, HOUSE, OPLOG = sheets.LEDGER, sheets.TASKS, sheets.HOUSEHOLD, sheets.OPLOG
TODAY = date(2026, 10, 12)


def run(coro):
    return asyncio.run(coro)


def call(tb, name, **args):
    return run(tb.execute(name, args))


def parsed(tb, name, **args):
    out = call(tb, name, **args)
    return json.loads(out) if isinstance(out, str) else out


def err(tb, name, **args):
    with pytest.raises(ai_tools.ToolError) as e:
        call(tb, name, **args)
    return str(e.value)


@pytest.fixture
def env(tmp_path, monkeypatch):
    fs = FakeSheets().install(monkeypatch)
    for d in ("06-Life-OS/05-private", "06-Life-OS/13-im-the-ceo", "00inbox", "01🗃Task"):
        (tmp_path / d).mkdir(parents=True)
    store = notes.GuardedStore(notes.LocalStore(tmp_path))
    monkeypatch.setattr(notes, "get_store", lambda: store)
    monkeypatch.setattr(util, "now", lambda: datetime(2026, 10, 12, 12, 0, tzinfo=config.TZ))
    monkeypatch.setattr(settings, "_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(task_sync, "run_safely", lambda: setattr(fs, "sync_calls", fs.sync_calls + 1))
    fs.seed(LEDGER,
            {"日付": "2026-10-05", "種別": "経費", "金額（円）": 1200, "内容": "サーバー代"},
            {"日付": "2026-10-10", "種別": "売上", "金額（円）": 50000, "内容": "BOOTH売上"},
            {"日付": "2026-09-20", "種別": "経費", "金額（円）": 800, "内容": "ドメイン"})
    fs.seed(TASKS, {"ID": "lo-aaaa0001", "登録日": "2026-10-01", "内容": "請求書を出す", "優先度": "高"})
    return type("Env", (), {"fs": fs, "root": tmp_path, "store": store, "tb": ai_tools.Toolbox(TODAY)})()


# ---------------------------------------------------------------- 純粋関数


def test_resolve_sheet():
    assert ai_tools.resolve_sheet("07-ledger") == LEDGER and ai_tools.resolve_sheet(" 07-LEDGER ") == LEDGER
    assert ai_tools.resolve_sheet("ledger") == LEDGER and ai_tools.resolve_sheet("today-task") == TASKS
    for bad in ("project", "", "帳簿", "99-none"):  # 複数に当てはまる・空・存在しない
        with pytest.raises(ai_tools.ToolError):
            ai_tools.resolve_sheet(bad)


def test_clean_values():
    assert ai_tools.clean_values(LEDGER, {"金額（円）": "1,200円", "内容": "x"}) == {"金額（円）": 1200, "内容": "x"}
    assert ai_tools.clean_values(TASKS, {"完了": True}) == {"完了": "TRUE"} and ai_tools.clean_values(TASKS, {"完了": False}) == {"完了": ""}
    assert ai_tools.clean_values(LEDGER, {"金額（円）": "たくさん"}) == {"金額（円）": "たくさん"}  # 数字にできなければそのまま
    for bad in ({}, None, "x", {"存在しない列": 1}, {"内容": ["a"]}):
        with pytest.raises(ai_tools.ToolError):
            ai_tools.clean_values(LEDGER, bad)
    with pytest.raises(ai_tools.ToolError):
        ai_tools.clean_values(TASKS, {"ID": "x"}, forbid=("ID",))


def test_same_ignores_number_formatting():
    assert ai_tools.same(1200, "1,200") and ai_tools.same("1200円", 1200) and ai_tools.same(" a ", "a")
    assert not ai_tools.same(1200, 1201) and not ai_tools.same("a", "b")


def test_clean_setting():
    assert ai_tools.clean_setting("idea_tags", ["#小説", "開発", "小説"]) == ["小説", "開発"]  # # を取り、重複を除く
    assert ai_tools.clean_setting("summary_length", " 2行 ") == "2行"
    for key, value in [("unknown", "x"), ("idea_tags", []), ("idea_tags", "小説"), ("idea_tags", [""]), ("idea_tags", ["あ" * 21]),
                       ("summary_length", ""), ("summary_length", ["2行"]), ("summary_length", "あ" * 31), ("idea_tags", [1])]:
        with pytest.raises(ai_tools.ToolError):
            ai_tools.clean_setting(key, value)


def test_cap_and_short():
    assert ai_tools.cap("あ" * 20, 10).startswith("あ" * 10) and "ここまで" in ai_tools.cap("あ" * 20, 10)
    assert ai_tools.short({"a": "x" * 50, "b": ""}, 10) == "x" * 10 + "…"


def test_every_tool_has_a_handler_and_a_schema():
    for t in ai_tools.TOOLS:
        assert hasattr(ai_tools.Toolbox, "_t_" + t["name"]) and t["input_schema"]["type"] == "object"
    assert {t["name"] for t in ai_tools.TOOLS} == {"search_sheet", "read_sheet_rows", "append_row", "update_row", "delete_row", "undo_operation",
                                                   "search_notes", "read_note", "append_note", "read_writing_progress", "update_setting"}


# ---------------------------------------------------------------- 読み取り


def test_search_sheet_and_words(env):
    out = parsed(env.tb, "search_sheet", sheet="ledger", query="経費 サーバー")
    assert out["found"] == 1 and out["rows"] == [{"row": 2, "values": {"日付": "2026-10-05", "種別": "経費", "金額（円）": 1200, "内容": "サーバー代"}}]
    assert parsed(env.tb, "search_sheet", sheet="ledger", query="ｂｏｏｔｈ")["found"] == 1  # 全角・大文字小文字を区別しない
    assert parsed(env.tb, "search_sheet", sheet="ledger", query="存在しない言葉")["found"] == 0
    assert "空" in err(env.tb, "search_sheet", sheet="ledger", query="  ")


def test_read_sheet_rows_by_date_and_last_n(env):
    out = parsed(env.tb, "read_sheet_rows", sheet="07-ledger", from_date="2026-10-01", to_date="2026-10-31")
    assert [r["row"] for r in out["rows"]] == [2, 3] and out["total_in_range"] == 2
    assert [r["row"] for r in parsed(env.tb, "read_sheet_rows", sheet="07-ledger", last_n=1)["rows"]] == [4]
    assert parsed(env.tb, "read_sheet_rows", sheet="01-today-task", from_date="2026-10-01")["rows"][0]["row"] == 2  # 登録日で絞る
    assert "YYYY-MM-DD" in err(env.tb, "read_sheet_rows", sheet="07-ledger", from_date="先月")


def test_results_are_capped(env):
    env.fs.seed(LEDGER, *[{"日付": "2026-10-01", "種別": "経費", "金額（円）": 1, "内容": "あ" * 400} for _ in range(100)])
    out = call(env.tb, "read_sheet_rows", sheet="07-ledger", last_n=100)
    assert len(out) <= ai_tools.MAX_RESULT_CHARS + 100 and "ここまで" in out


# ---------------------------------------------------------------- 変更（操作ログが先）


def test_append_row_logs_first_and_coerces(env):
    out = parsed(env.tb, "append_row", sheet="07-ledger", values={"日付": "2026-10-11", "種別": "経費", "金額（円）": "3,000円", "内容": "書籍"})
    assert out["ok"] and out["row"] == 5 and env.fs.data[LEDGER][-1]["金額（円）"] == 3000
    entry = env.fs.log_entries()[0]
    assert (entry["操作"], entry["シート"], json.loads(entry["変更後"])["内容"]) == ("追記", LEDGER, "書籍")
    assert [c[0] for c in env.fs.calls][:2] == ["append", "append"]  # 操作ログ → 本体の順
    assert env.fs.calls[0][1] == OPLOG and env.fs.calls[1][1] == LEDGER
    assert env.tb.ops == [("07-ledger に1行追加（2026-10-11 経費 3000 書籍）", True)]


def test_append_row_refuses_read_only_sheets(env):
    assert "Bot だけ" in err(env.tb, "append_row", sheet="14-ai", values={"操作": "x"})
    assert "13-im-the-ceo の部屋" in err(env.tb, "append_row", sheet="13-im-the-ceo", values={"内容": "方針"})
    assert env.fs.data[OPLOG] == [] and env.fs.data[sheets.DIRECTIVES] == []


def test_append_task_goes_through_add_task_and_syncs(env):
    out = parsed(env.tb, "append_row", sheet="01-today-task", values={"内容": "電話する", "期限": "2026-10-20", "優先度": "高"})
    assert out["ok"] and env.fs.calls[-1] == ("add_task", "電話する", "14-ai") and env.fs.sync_calls == 1
    assert env.fs.data[TASKS][-1]["期限"] == "2026-10-20"
    assert env.fs.log_entries()[0]["操作"] == "追記（タスク）" and env.tb.ops[0][1] is False  # タスクの追加は取り消せない
    assert "内容" in err(env.tb, "append_row", sheet="01-today-task", values={"期限": "2026-10-20"})
    assert "変更できません" in err(env.tb, "append_row", sheet="01-today-task", values={"内容": "x", "完了": "TRUE"})


def test_update_row_requires_matching_expect(env):
    assert "expect" in err(env.tb, "update_row", sheet="07-ledger", row=2, values={"金額（円）": 1500}, expect="")
    msg = err(env.tb, "update_row", sheet="07-ledger", row=2, values={"金額（円）": 1500}, expect="BOOTH")
    assert "一致しません" in msg and "サーバー代" in msg  # 今の内容を教える
    assert "データがありません" in err(env.tb, "update_row", sheet="07-ledger", row=9, values={"金額（円）": 1}, expect="x")
    assert env.fs.data[LEDGER][0]["金額（円）"] == 1200 and env.fs.data[OPLOG] == []  # 何も変わらない


def test_update_row_logs_before_values_before_changing(env, monkeypatch):
    seen = []
    real = env.fs.update_row

    def spy(name, row, data):
        seen.append(len(env.fs.data[OPLOG]))  # 変更の時点で、操作ログはすでにある
        real(name, row, data)

    monkeypatch.setattr(sheets, "update_row", spy)
    out = parsed(env.tb, "update_row", sheet="07-ledger", row=2, values={"金額（円）": "1,500"}, expect="サーバー")
    assert out["ok"] and seen == [1] and env.fs.data[LEDGER][0]["金額（円）"] == 1500
    e = env.fs.log_entries()[0]
    assert (e["操作"], e["行"]) == ("修正", 2) and json.loads(e["変更前"]) == {"金額（円）": 1200} and json.loads(e["変更後"]) == {"金額（円）": 1500}
    assert env.tb.ops[0][1] is True


def test_update_row_without_change_is_not_logged(env):
    out = parsed(env.tb, "update_row", sheet="07-ledger", row=2, values={"金額（円）": "1,200"}, expect="サーバー")
    assert "変更はありません" in out["note"] and env.fs.data[OPLOG] == [] and env.tb.ops == []


def test_update_task_syncs_and_protects_id(env):
    parsed(env.tb, "update_row", sheet="01-today-task", row=2, values={"完了": True}, expect="請求書")
    assert env.fs.data[TASKS][0]["完了"] == "TRUE" and env.fs.sync_calls == 1
    assert "変更できません" in err(env.tb, "update_row", sheet="01-today-task", row=2, values={"ID": "x"}, expect="請求書")


def test_delete_row_logs_the_whole_row(env):
    out = parsed(env.tb, "delete_row", sheet="07-ledger", row=3, expect="BOOTH")
    assert out["ok"] and out["deleted"]["内容"] == "BOOTH売上" and len(env.fs.data[LEDGER]) == 2
    e = env.fs.log_entries()[0]
    assert (e["操作"], e["行"]) == ("削除", 3) and json.loads(e["変更前"])["金額（円）"] == 50000
    assert "1つ小さく" in out["note"]


def test_delete_guards(env):
    assert "Obsidian のタスク棚" in err(env.tb, "delete_row", sheet="01-today-task", row=2, expect="請求書")
    assert "一致しません" in err(env.tb, "delete_row", sheet="07-ledger", row=2, expect="BOOTH")
    assert len(env.fs.data[LEDGER]) == 3 and env.fs.data[OPLOG] == []


def test_delete_and_write_caps(env):
    env.fs.seed(HOUSE, *[{"日付": "2026-10-01", "金額（円）": i, "ジャンル": f"g{i}", "区分": "娯楽"} for i in range(1, 15)])
    for i in range(ai_tools.MAX_DELETES):
        parsed(env.tb, "delete_row", sheet="06-household-accounts", row=2, expect="娯楽")
    assert "5 件まで" in err(env.tb, "delete_row", sheet="06-household-accounts", row=2, expect="娯楽")
    for i in range(ai_tools.MAX_WRITES - ai_tools.MAX_DELETES):
        parsed(env.tb, "append_row", sheet="05-private", values={"内容": f"メモ{i}"})
    assert "10 件まで" in err(env.tb, "append_row", sheet="05-private", values={"内容": "もう1つ"})


def test_nothing_changes_when_the_log_cannot_be_written(env, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("sheet down")

    monkeypatch.setattr(sheets, "log_operation", boom)
    for name, args in [("delete_row", dict(sheet="07-ledger", row=3, expect="BOOTH")),
                       ("update_row", dict(sheet="07-ledger", row=2, values={"金額（円）": 1}, expect="サーバー")),
                       ("append_row", dict(sheet="07-ledger", values={"内容": "x"}))]:
        err(env.tb, name, **args)
    assert len(env.fs.data[LEDGER]) == 3 and env.fs.data[LEDGER][0]["金額（円）"] == 1200 and env.tb.ops == []  # 記録できなければ変更しない


# ---------------------------------------------------------------- 取り消し


def log_rows(env):
    return [r for r, _ in env.fs.records(OPLOG)]


def test_undo_append(env):
    parsed(env.tb, "append_row", sheet="07-ledger", values={"日付": "2026-10-11", "種別": "経費", "金額（円）": 100, "内容": "テスト"})
    out = parsed(env.tb, "undo_operation", log_row=log_rows(env)[0])
    assert out["ok"] and len(env.fs.data[LEDGER]) == 3
    assert env.fs.log_entries()[-1]["操作"] == "取り消し（ログ2）"
    assert "すでに取り消して" in err(env.tb, "undo_operation", log_row=2)  # 二重には取り消さない


def test_undo_needs_the_row_number_in_the_log(env, monkeypatch):
    real = env.fs.update_row

    def fail_on_oplog(name, row, data):
        if name == OPLOG:
            raise RuntimeError("write quota")  # 行番号を補えなかった
        real(name, row, data)

    monkeypatch.setattr(sheets, "update_row", fail_on_oplog)
    out = parsed(env.tb, "append_row", sheet="07-ledger", values={"内容": "テスト"})
    assert out["ok"] and len(env.fs.data[LEDGER]) == 4  # 追記そのものは成功している
    assert "行番号が記録されていない" in err(env.tb, "undo_operation", log_row=2)


def test_undo_update_restores_before_values(env):
    parsed(env.tb, "update_row", sheet="07-ledger", row=2, values={"金額（円）": 1500, "内容": "サーバー代（年額）"}, expect="サーバー")
    parsed(env.tb, "undo_operation", log_row=2)
    assert (env.fs.data[LEDGER][0]["金額（円）"], env.fs.data[LEDGER][0]["内容"]) == (1200, "サーバー代")


def test_undo_delete_reinserts_at_the_same_row(env):
    parsed(env.tb, "delete_row", sheet="07-ledger", row=3, expect="BOOTH")
    assert [r["内容"] for r in env.fs.data[LEDGER]] == ["サーバー代", "ドメイン"]
    out = parsed(env.tb, "undo_operation", log_row=2)
    assert "3行目に戻しました" in out["note"]
    assert [r["内容"] for r in env.fs.data[LEDGER]] == ["サーバー代", "BOOTH売上", "ドメイン"] and env.fs.data[LEDGER][1]["金額（円）"] == 50000


def test_undo_refuses_when_the_row_changed_afterwards(env):
    parsed(env.tb, "update_row", sheet="07-ledger", row=2, values={"金額（円）": 1500}, expect="サーバー")
    env.fs.data[LEDGER][0]["金額（円）"] = 9999  # その後、手で書き換えた
    assert "取り消せません" in err(env.tb, "undo_operation", log_row=2)
    assert env.fs.data[LEDGER][0]["金額（円）"] == 9999


def test_undo_unsupported_and_missing(env):
    parsed(env.tb, "append_row", sheet="01-today-task", values={"内容": "電話"})
    assert "取り消しに対応していません" in err(env.tb, "undo_operation", log_row=2)  # タスクの追加
    assert "ありません" in err(env.tb, "undo_operation", log_row=99) and "整数" in err(env.tb, "undo_operation", log_row="x")


def test_undo_setting(env):
    parsed(env.tb, "update_setting", key="idea_tags", value=["小説", "開発"])
    parsed(env.tb, "undo_operation", log_row=2)
    assert settings.get("idea_tags") == settings.DEFAULTS["idea_tags"]
    parsed(env.tb, "update_setting", key="summary_length", value="2")  # 数字だけの文字列も、文字列のまま戻る
    parsed(env.tb, "undo_operation", log_row=4)
    assert settings.get("summary_length") == "3行"


def test_undo_setting_refuses_when_changed_afterwards(env):
    parsed(env.tb, "update_setting", key="summary_length", value="2行")
    settings.set_value("summary_length", "5行")
    assert "取り消せません" in err(env.tb, "undo_operation", log_row=2)


# ---------------------------------------------------------------- ノート（GuardedStore を通る）


def test_search_and_read_notes(env):
    (env.root / "06-Life-OS/05-private/Inbox.md").write_text("# Inbox\n\n駅前のカフェに行きたい\n", encoding="utf-8")
    (env.root / "00inbox/メモ.md").write_text("カフェの営業時間\n", encoding="utf-8")
    life = parsed(env.tb, "search_notes", query="カフェ")
    assert [n["path"] for n in life["notes"]] == ["06-Life-OS/05-private/Inbox.md"]  # 既定は 06-Life-OS の中だけ
    vault = parsed(env.tb, "search_notes", query="カフェ", scope="vault")
    assert {n["path"] for n in vault["notes"]} == {"06-Life-OS/05-private/Inbox.md", "00inbox/メモ.md"}
    assert "カフェの営業時間" in call(env.tb, "read_note", path="00inbox/メモ.md")  # 読み取りは Vault 全体
    assert "見つかりません" in err(env.tb, "read_note", path="00inbox/無い.md")
    assert "許可されていない" in err(env.tb, "read_note", path="../secret.md")


def test_read_note_is_capped(env):
    (env.root / "06-Life-OS/05-private/long.md").write_text("あ" * 20_000, encoding="utf-8")
    assert len(call(env.tb, "read_note", path="06-Life-OS/05-private/long.md")) < ai_tools.NOTE_READ_LIMIT + 100


def test_append_note_inside_life_os(env):
    (env.root / "06-Life-OS/05-private/Inbox.md").write_text("# Inbox\n\n古い内容\n", encoding="utf-8")
    out = parsed(env.tb, "append_note", path="06-Life-OS/05-private/Inbox.md", text="追記した内容")
    text = (env.root / "06-Life-OS/05-private/Inbox.md").read_text(encoding="utf-8")
    assert out["ok"] and text.endswith("追記した内容\n") and "古い内容" in text
    e = env.fs.log_entries()[0]
    assert (e["操作"], e["シート"], e["変更後"]) == ("ノート追記", "06-Life-OS/05-private/Inbox.md", "追記した内容")
    assert env.tb.ops[0][1] is False


def test_append_note_creates_a_missing_note(env):
    parsed(env.tb, "append_note", path="06-Life-OS/14-ai/作業メモ.md", text="はじめてのメモ")
    assert (env.root / "06-Life-OS/14-ai/作業メモ.md").read_text(encoding="utf-8") == "# 作業メモ\n\nはじめてのメモ\n"


@pytest.mark.parametrize("path", ["00inbox/メモ.md", "01🗃Task/Project/x.md", "06-Life-OS", "../x.md", "/etc/x.md"])
def test_append_note_outside_life_os_is_denied_before_logging(env, path):
    with pytest.raises(ai_tools.ToolError):
        call(env.tb, "append_note", path=path if path.endswith(".md") else path + ".md", text="書き込み")
    assert env.fs.data[OPLOG] == []  # 許可外は、操作ログも書かずに止まる
    assert not (env.root / "00inbox/メモ.md").exists()


def test_append_note_special_denials(env):
    assert "タスク" in err(env.tb, "append_note", path=config.OBSIDIAN_TASK_FILE, text="- [ ] x")
    assert "13-im-the-ceo" in err(env.tb, "append_note", path="06-Life-OS/13-im-the-ceo/CEO-Directives.md", text="方針を書き換える")
    assert ".md" in err(env.tb, "append_note", path="06-Life-OS/x.txt", text="x") and "空" in err(env.tb, "append_note", path="06-Life-OS/x.md", text=" ")
    assert not (env.root / "06-Life-OS/13-im-the-ceo/CEO-Directives.md").exists() and env.fs.data[OPLOG] == []


# ---------------------------------------------------------------- 執筆記録・設定・実行


def test_read_writing_progress(env, monkeypatch):
    wl = writing_log.WritingLog({"A": writing_log.Work("A", "A", 10000, date(2026, 12, 31))},
                                [writing_log.Snapshot(datetime(2026, 10, 10, 23), {"A": 1000}), writing_log.Snapshot(datetime(2026, 10, 11, 23), {"A": 1800})])
    monkeypatch.setattr(writing_log, "fetch", lambda: wl)
    text = call(env.tb, "read_writing_progress", work="A")
    assert "総文字数 1,800" in text and "目標 10,000（18%）" in text
    out = parsed(env.tb, "read_writing_progress")
    assert out["works"] == [{"name": "A", "total": 1800, "target": 10000, "deadline": "2026-12-31"}]
    assert "照合できません" in err(env.tb, "read_writing_progress", work="存在しない作品")
    monkeypatch.setattr(writing_log, "fetch", lambda: None)
    assert "設定されていません" in err(env.tb, "read_writing_progress")
    assert env.fs.data[OPLOG] == []  # 読み取りだけ。何も書かない


def test_update_setting(env):
    out = parsed(env.tb, "update_setting", key="idea_tags", value=["#小説", "TRPG", "開発"])
    assert out["after"] == ["小説", "TRPG", "開発"] and settings.get("idea_tags") == ["小説", "TRPG", "開発"]
    assert env.tb.ops == [("設定 idea_tags: 小説、TRPG、仕事、日常、開発 → 小説、TRPG、開発", True)]
    assert "すでにその設定" in parsed(env.tb, "update_setting", key="idea_tags", value=["小説", "TRPG", "開発"])["note"]
    assert "変更できる設定" in err(env.tb, "update_setting", key="CLAUDE_MODEL", value="x")
    assert len(env.fs.data[OPLOG]) == 1


def test_execute_errors(env, monkeypatch):
    assert "未知のツール" in err(env.tb, "drop_database")
    with pytest.raises(TypeError):  # 引数の間違いは、そのまま（run_tools がモデルに見せる）
        call(env.tb, "search_sheet", sheet="07-ledger", query="x", extra="y")

    def boom(name):
        raise ConnectionError("quota")

    monkeypatch.setattr(sheets, "records", boom)
    assert "ConnectionError: quota" in err(env.tb, "search_sheet", sheet="07-ledger", query="x")


def test_footer(env):
    assert env.tb.footer() == ""
    parsed(env.tb, "append_row", sheet="07-ledger", values={"内容": "テスト"})
    parsed(env.tb, "append_note", path="06-Life-OS/14-ai/x.md", text="メモ")
    f = env.tb.footer()
    assert f.startswith("\n\n🗂️ 操作ログ（14-ai シートに記録しました）\n・07-ledger に1行追加") and "・06-Life-OS/14-ai/x.md に追記" in f
    assert f.endswith("取り消したいときは「さっきの取り消して」と送ってください。")
    only_note = ai_tools.Toolbox(TODAY)
    parsed(only_note, "append_note", path="06-Life-OS/14-ai/y.md", text="メモ")
    assert "取り消したいとき" not in only_note.footer()  # 取り消せる操作が無いときは、案内しない


def test_only_life_os_files_are_written_by_note_tools(env):
    parsed(env.tb, "append_note", path="06-Life-OS/14-ai/a.md", text="x")
    written = {p.relative_to(env.root).as_posix() for p in env.root.rglob("*") if p.is_file()}
    assert written == {"06-Life-OS/14-ai/a.md"}
