"""手書きのタスクページ（00Task Inbox-2026.md）の読み込み（SPEC §6.4）。Bot が書けるのはチェックと ⏳ だけ。"""
import json
from datetime import date

import pytest

import inbox_sync
import notes
import notes_policy as policy
import sheets
import state
from notes_policy import AccessDenied, INBOX_FILE

TODAY = date(2026, 10, 17)


def sheet(tid, content, done=False, deleted=False, scheduled="", due="", priority=""):
    return {"id": tid, "content": content, "done": done, "deleted": deleted, "scheduled": scheduled, "due": due,
            "priority": priority, "source": inbox_sync.SOURCE, "row": 2, "done_date": "", "created": "2026-10-01"}


# ---------------------------------------------------------------- 方針（書けるのはチェックと ⏳ だけ）


def test_inbox_file_default_and_policy():
    assert INBOX_FILE == "01🗃Task/00Task Inbox-2026.md"
    assert policy.check(INBOX_FILE, "read") == INBOX_FILE
    assert policy.check(INBOX_FILE, "write_marks") == INBOX_FILE
    for op in ("write", "rename", "delete"):
        with pytest.raises(AccessDenied):
            policy.check(INBOX_FILE, op)
    for other in ("01🗃Task/01 Life-OS-Task.md", "06-Life-OS/09-idea/Ideas.md", "02 📝 memo/a.md"):
        with pytest.raises(AccessDenied):
            policy.check(other, "write_marks")


@pytest.mark.parametrize("old,new", [
    ("- [ ] a", "- [x] a"),
    ("- [ ] a", "- [ ] a ⏳ 2026-10-20"),
    ("- [ ] a ⏳ 2026-10-18 📅 2026-10-30", "- [ ] a ⏳ 2026-10-20 📅 2026-10-30"),
    ("- [ ] a ⏳ 2026-10-18", "- [ ] a"),
    ("# 見出し\n- [ ] a\nメモ", "# 見出し\n- [x] a\nメモ"),
])
def test_marks_only_allows_check_and_scheduled(old, new):
    assert notes.marks_only_problem(old, new) is None


@pytest.mark.parametrize("old,new", [
    ("- [ ] a", "- [ ] b"),
    ("- [x] a", "- [ ] a"),
    ("- [ ] a", "- [ ] a\n- [ ] b"),
    ("メモ", "メモ2"),
    ("- [ ] a", "  - [ ] a"),
    ("- [ ] a 📅 2026-10-30", "- [ ] a 📅 2026-10-31"),
    ("- [ ] a", "- [ ] a ✅ 2026-10-17"),
])
def test_marks_only_rejects_anything_else(old, new):
    assert notes.marks_only_problem(old, new)


@pytest.fixture
def vault(tmp_path):
    (tmp_path / "01🗃Task").mkdir()
    (tmp_path / INBOX_FILE).write_text("- [ ] 原稿\n- [ ] 電話\n", encoding="utf-8")
    return tmp_path


def test_guarded_write_marks_writes_only_marks(vault):
    st = notes.GuardedStore(notes.LocalStore(vault))
    st.write_marks(INBOX_FILE, "- [x] 原稿\n- [ ] 電話 ⏳ 2026-10-20\n")
    assert (vault / INBOX_FILE).read_text(encoding="utf-8") == "- [x] 原稿\n- [ ] 電話 ⏳ 2026-10-20\n"


def test_guarded_write_marks_refuses_other_changes(vault, caplog):
    st = notes.GuardedStore(notes.LocalStore(vault))
    with pytest.raises(AccessDenied):
        st.write_marks(INBOX_FILE, "- [ ] 原稿を書き換え\n- [ ] 電話\n")
    with pytest.raises(AccessDenied):
        st.write(INBOX_FILE, "- [x] 原稿\n- [ ] 電話\n")  # ふつうの書き込みは不可
    assert (vault / INBOX_FILE).read_text(encoding="utf-8") == "- [ ] 原稿\n- [ ] 電話\n"


# ---------------------------------------------------------------- 計画（純粋）


def test_new_unchecked_lines_become_sheet_tasks():
    p = inbox_sync.plan("# メモ\n- [ ] 原稿 📅 2026-10-30\n- [x] 済んだ\n- [ ] 電話 ⏫", [], {}, TODAY)
    assert [(a["content"], a["due"], a["priority"]) for a in p.adds] == [("原稿", "2026-10-30", ""), ("電話", "", "高")]
    assert p.text == "# メモ\n- [ ] 原稿 📅 2026-10-30\n- [x] 済んだ\n- [ ] 電話 ⏫"  # ページは変えない
    assert set(p.snapshot) == {"原稿", "電話"}


def test_done_on_discord_only_checks_the_box():
    text = "- [ ] 原稿 📅 2026-10-30"
    p = inbox_sync.plan(text, [sheet("lo-1", "原稿", done=True, due="2026-10-30")], {"原稿": {"id": "lo-1", "scheduled": ""}}, TODAY)
    assert p.text == "- [x] 原稿 📅 2026-10-30" and p.updates == []
    assert notes.marks_only_problem(text, p.text) is None


def test_done_in_obsidian_completes_sheet():
    p = inbox_sync.plan("- [x] 原稿", [sheet("lo-1", "原稿")], {"原稿": {"id": "lo-1", "scheduled": ""}}, TODAY)
    assert p.updates == [("lo-1", {"done": True, "done_date": "2026-10-17"})]


def test_scheduled_changed_on_discord_is_written_to_page():
    text = "- [ ] 原稿 ⏳ 2026-10-17"
    p = inbox_sync.plan(text, [sheet("lo-1", "原稿", scheduled="2026-10-20")],
                        {"原稿": {"id": "lo-1", "scheduled": "2026-10-17"}}, TODAY)
    assert p.text == "- [ ] 原稿 ⏳ 2026-10-20" and p.updates == []
    assert p.snapshot["原稿"]["scheduled"] == "2026-10-20"
    p2 = inbox_sync.plan("- [ ] 電話", [sheet("lo-2", "電話", scheduled="2026-10-21")],
                         {"電話": {"id": "lo-2", "scheduled": ""}}, TODAY)
    assert p2.text == "- [ ] 電話 ⏳ 2026-10-21"
    p3 = inbox_sync.plan("- [ ] 電話 ⏳ 2026-10-21", [sheet("lo-2", "電話", scheduled="")],
                         {"電話": {"id": "lo-2", "scheduled": "2026-10-21"}}, TODAY)
    assert p3.text == "- [ ] 電話"  # 棚に戻した → ⏳ を消す


def test_scheduled_changed_in_page_wins():
    p = inbox_sync.plan("- [ ] 原稿 ⏳ 2026-10-25", [sheet("lo-1", "原稿", scheduled="2026-10-20")],
                        {"原稿": {"id": "lo-1", "scheduled": "2026-10-17"}}, TODAY)
    assert p.text == "- [ ] 原稿 ⏳ 2026-10-25"
    assert p.updates == [("lo-1", {"scheduled": "2026-10-25"})]


def test_removed_line_marks_sheet_deleted_and_text_is_from_page():
    p = inbox_sync.plan("- [ ] 原稿 第2稿", [sheet("lo-1", "原稿"), sheet("lo-2", "電話")],
                        {"原稿": {"id": "lo-1", "scheduled": ""}, "電話": {"id": "lo-2", "scheduled": ""}}, TODAY)
    # 文を書き換えた行は新しいタスク、元の文の行は消えた扱い
    assert [a["content"] for a in p.adds] == ["原稿 第2稿"]
    assert sorted(p.updates) == [("lo-1", {"deleted": True}), ("lo-2", {"deleted": True})]


# ---------------------------------------------------------------- run（ページ → シート）


class Store:
    def __init__(self, text):
        self.text, self.writes = text, []

    def read(self, path):
        assert path == INBOX_FILE
        return self.text

    def write_marks(self, path, text):
        self.writes.append(text)
        self.text = text


@pytest.fixture
def kv(monkeypatch):
    data = {}
    monkeypatch.setattr(state, "get_kv", lambda k, default=None: data.get(k, default))
    monkeypatch.setattr(state, "set_kv", lambda k, v: data.__setitem__(k, v))
    monkeypatch.setattr(inbox_sync.util, "activity_day", lambda at=None: TODAY)
    return data


def test_run_adds_then_checks_box(monkeypatch, kv):
    added, updated = [], []
    monkeypatch.setattr(sheets, "add_task", lambda content, **kw: added.append((content, kw)))
    monkeypatch.setattr(sheets, "update_task", lambda tid, **f: updated.append((tid, f)))
    st = Store("- [ ] 原稿\n")
    r = inbox_sync.run(st, [])
    assert r == {"page_written": False, "added": 1, "updated": 0}
    assert added[0][0] == "原稿" and added[0][1]["source"] == "inbox"
    tid = added[0][1]["task_id"]
    assert json.loads(kv[inbox_sync.SNAPSHOT_KEY]) == {"原稿": {"id": tid, "scheduled": ""}}
    r = inbox_sync.run(st, [sheet(tid, "原稿", done=True)] + [{**sheet("lo-x", "他"), "source": "discord"}])
    assert st.writes == ["- [x] 原稿\n"] and r["page_written"]


def test_run_missing_page_does_nothing(kv):
    class Missing(Store):
        def read(self, path):
            return None
    assert inbox_sync.run(Missing(""), []) is None


def test_failed_page_write_keeps_sheet_going_and_retries(monkeypatch, kv):
    updated = []
    monkeypatch.setattr(sheets, "update_task", lambda tid, **f: updated.append((tid, f)))
    kv[inbox_sync.SNAPSHOT_KEY] = json.dumps({"原稿": {"id": "lo-1", "scheduled": ""}, "電話": {"id": "lo-2", "scheduled": ""}})

    class Broken(Store):
        def write_marks(self, path, text):
            raise AccessDenied("test")

    st = Broken("- [ ] 原稿\n- [x] 電話\n")
    tasks = [sheet("lo-1", "原稿", scheduled="2026-10-20"), sheet("lo-2", "電話")]
    r = inbox_sync.run(st, tasks)
    assert r["page_written"] is False
    assert updated == [("lo-2", {"done": True, "done_date": "2026-10-17"})]  # ページで完了 → シートへ
    snap = json.loads(kv[inbox_sync.SNAPSHOT_KEY])
    assert snap["原稿"]["scheduled"] == ""  # 次の同期でも「Discord で変えた」と分かる
    st2 = Store(st.text)
    inbox_sync.run(st2, tasks)
    assert st2.writes == ["- [ ] 原稿 ⏳ 2026-10-20\n- [x] 電話\n"]


def test_duplicates_from_an_interrupted_import_are_adopted_and_cleaned():
    """最初の取り込みが途中で止まり、シートにだけ残ったタスク（スナップショットに無い）を使い直し、二重の分は削除にする。"""
    tasks = [sheet("lo-old", "タンス黒の中身を整理する", scheduled="2026-10-21"),
             sheet("lo-dup", "タンス黒の中身を整理する", scheduled="2026-10-21"),
             sheet("lo-k", "開業届を検索する", scheduled="2026/10/17"),
             sheet("lo-gone", "ページに無いタスク")]
    text = "- [ ] タンス黒の中身を整理する ⏳ 2026-10-24 📅 2026-10-31\n- [x] 開業届を検索する ⏳ 2026-10-17 ✅ 2026-10-22"
    p = inbox_sync.plan(text, tasks, {}, TODAY)
    assert p.adds == []  # 新しく足さない
    ups = dict(p.updates)
    assert ups["lo-old"] == {"scheduled": "2026-10-24", "due": "2026-10-31"}
    assert ups["lo-k"] == {"done": True, "done_date": "2026-10-22"}
    assert ups["lo-dup"] == {"deleted": True} and ups["lo-gone"] == {"deleted": True}
    assert p.snapshot["タンス黒の中身を整理する"]["id"] == "lo-old"


def test_sheet_date_format_does_not_cause_endless_updates():
    tasks = [sheet("lo-1", "原稿", scheduled="2026/10/20", due="2026/10/31")]
    p = inbox_sync.plan("- [ ] 原稿 ⏳ 2026-10-20 📅 2026-10-31", tasks,
                        {"原稿": {"id": "lo-1", "scheduled": "2026-10-20"}}, TODAY)
    assert p.updates == [] and p.text == "- [ ] 原稿 ⏳ 2026-10-20 📅 2026-10-31"


def test_empty_page_never_deletes():
    p = inbox_sync.plan("", [sheet("lo-1", "原稿")], {"原稿": {"id": "lo-1", "scheduled": ""}}, TODAY)
    assert p.updates == [] and p.snapshot == {"原稿": {"id": "lo-1", "scheduled": ""}}
