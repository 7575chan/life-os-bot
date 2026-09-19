from datetime import date

import task_sync as ts

TODAY = date(2026, 9, 19)


def sheet(tid, content="タスク", done=False, deleted=False, scheduled="", due="", priority="", created="2026-09-01",
          done_date=""):
    return {"id": tid, "content": content, "done": done, "deleted": deleted, "scheduled": scheduled, "due": due,
            "priority": priority, "created": created, "done_date": done_date, "source": "", "row": 2}


def snap_of(*tasks):
    return {t["id"]: ts.digest(t) for t in tasks}


# ---------------------------------------------------------------- 記法


def test_parse_tasks_plugin_line():
    t = ts.parse_line("- [ ] 原稿を書く 🆔 lo-a1 ⏫ ➕ 2026-09-01 ⏳ 2026-09-20 📅 2026-09-30 #小説")
    assert t["content"] == "原稿を書く #小説"
    assert (t["id"], t["priority"], t["created"], t["scheduled"], t["due"]) == ("lo-a1", "高", "2026-09-01", "2026-09-20", "2026-09-30")
    assert not t["done"]


def test_parse_done_and_variants():
    t = ts.parse_line("  - [x] 終わった ✅ 2026-09-18 🔼")
    assert t["done"] and t["done_date"] == "2026-09-18" and t["priority"] == "中" and t["indent"] == "  "
    assert ts.parse_line("* [X] 別記号")["done"]
    assert ts.parse_line("ただの行") is None
    assert ts.parse_line("- [ ]") is None


def test_unknown_tokens_stay_in_description():
    t = ts.parse_line("- [ ] 週報 🔁 every week ⏳ 2026-09-21")
    assert "🔁 every week" in t["content"] and t["scheduled"] == "2026-09-21"


def test_render_roundtrip_canonical_order():
    line = "- [ ] 書く 🆔 lo-a1 ⏫ ➕ 2026-09-01 ⏳ 2026-09-20 📅 2026-09-30"
    assert ts.render(ts.parse_line(line)) == line
    done = ts.render({"content": "済", "done": True, "id": "lo-b", "done_date": "2026-09-19"})
    assert done == "- [x] 済 🆔 lo-b ✅ 2026-09-19"


def test_digest_ignores_whitespace_and_irrelevant_fields():
    a = {"content": "a  b", "done": False, "scheduled": "", "due": "", "priority": ""}
    b = {"content": "a b", "done": False, "scheduled": "", "due": "", "priority": "", "created": "x", "id": "1"}
    assert ts.digest(a) == ts.digest(b)
    assert ts.digest(a) != ts.digest({**a, "done": True})


# ---------------------------------------------------------------- 同期計画


def test_new_sheet_task_is_added_under_bot_section():
    text = "# タスク\n\n- [ ] 手書きの予定 🆔 lo-h1\n"
    p = ts.plan(text, [sheet("lo-h1", "手書きの予定"), sheet("lo-n1", "Botが作った", scheduled="2026-09-20", priority="高")],
                snap_of(sheet("lo-h1", "手書きの予定")), TODAY)
    assert p.file_changed
    assert p.text.startswith("# タスク\n\n- [ ] 手書きの予定 🆔 lo-h1\n")
    assert "## 🤖 Life-OS" in p.text
    assert "- [ ] Botが作った 🆔 lo-n1 ⏫ ➕ 2026-09-01 ⏳ 2026-09-20" in p.text
    assert p.snapshot["lo-n1"]


def test_new_task_appended_to_existing_section_end_not_after_next_heading():
    text = "## 🤖 Life-OS\n\n- [ ] 既存 🆔 lo-e1\n\n## メモ\n自由記述\n"
    p = ts.plan(text, [sheet("lo-e1", "既存"), sheet("lo-n2", "新規")], snap_of(sheet("lo-e1", "既存")), TODAY)
    lines = p.text.split("\n")
    assert lines.index("- [ ] 新規 🆔 lo-n2 ➕ 2026-09-01") < lines.index("## メモ")
    assert "自由記述" in p.text


def test_user_written_task_gets_id_and_is_added_to_sheet():
    text = "- [ ] 手で書いたタスク ⏳ 2026-09-20 📅 2026-09-25 ⏫\n"
    p = ts.plan(text, [], {}, TODAY)
    assert p.file_changed and "🆔 lo-" in p.text
    (added,) = p.sheet_adds
    assert added["content"] == "手で書いたタスク" and added["scheduled"] == "2026-09-20" and added["due"] == "2026-09-25"
    assert added["priority"] == "高" and added["source"] == "obsidian" and added["id"].startswith("lo-")
    assert added["id"] in p.text


def test_no_change_means_no_write():
    t = sheet("lo-a1", "書く", scheduled="2026-09-20")
    text = "# 見出し\n- [ ] 書く 🆔 lo-a1 ➕ 2026-09-01 ⏳ 2026-09-20\n"
    p = ts.plan(text, [t], snap_of(t), TODAY)
    assert not p.file_changed and p.text == text and not p.sheet_adds and not p.sheet_updates


def test_only_file_changed_updates_sheet():
    old = sheet("lo-a1", "旧")
    text = "- [ ] 新しい内容 🆔 lo-a1 ➕ 2026-09-01\n"
    p = ts.plan(text, [old], snap_of(old), TODAY)
    assert p.sheet_updates and p.sheet_updates[0][0] == "lo-a1" and p.sheet_updates[0][1]["content"] == "新しい内容"
    assert not p.file_changed


def test_only_sheet_changed_updates_file():
    old = sheet("lo-a1", "書く")
    changed = sheet("lo-a1", "書く", scheduled="2026-09-22", priority="高")
    text = "- [ ] 書く 🆔 lo-a1 ➕ 2026-09-01\n"
    p = ts.plan(text, [changed], snap_of(old), TODAY)
    assert p.file_changed and "⏳ 2026-09-22" in p.text and "⏫" in p.text
    assert not p.sheet_updates


def test_sheet_completion_reaches_file_with_done_date():
    old = sheet("lo-a1", "書く")
    done = sheet("lo-a1", "書く", done=True, done_date="2026-09-19")
    p = ts.plan("- [ ] 書く 🆔 lo-a1 ➕ 2026-09-01\n", [done], snap_of(old), TODAY)
    assert "- [x] 書く" in p.text and "✅ 2026-09-19" in p.text


def test_file_completion_reaches_sheet_and_gets_today():
    old = sheet("lo-a1", "書く")
    p = ts.plan("- [x] 書く 🆔 lo-a1 ➕ 2026-09-01\n", [old], snap_of(old), TODAY)
    assert "✅ 2026-09-19" in p.text
    (tid, fields), = p.sheet_updates
    assert fields["done"] is True and fields["done_date"] == "2026-09-19"


def test_conflict_obsidian_wins_but_completion_always_wins():
    base = sheet("lo-a1", "元")
    sheet_side = sheet("lo-a1", "シートで編集", done=True, done_date="2026-09-18")
    text = "- [ ] Obsidianで編集 🆔 lo-a1 ➕ 2026-09-01\n"
    p = ts.plan(text, [sheet_side], snap_of(base), TODAY)
    assert "Obsidianで編集" in p.text and "シートで編集" not in p.text  # Obsidian 優先
    assert "- [x] " in p.text  # 完了はシート側の完了が勝つ
    (tid, fields), = p.sheet_updates
    assert fields["content"] == "Obsidianで編集" and fields["done"] is True


def test_completion_is_never_reverted():
    done = sheet("lo-a1", "済", done=True, done_date="2026-09-18")
    p = ts.plan("- [ ] 済 🆔 lo-a1\n", [done], {}, TODAY)  # 初回（スナップショット無し）でも完了は維持
    assert "- [x] 済" in p.text


def test_line_deleted_in_obsidian_marks_sheet_deleted():
    t = sheet("lo-a1", "消す")
    p = ts.plan("# 空\n", [t], snap_of(t), TODAY)
    assert p.sheet_updates == [("lo-a1", {"deleted": True})]
    assert not p.file_changed


def test_deleted_sheet_rows_are_ignored():
    t = sheet("lo-a1", "消した", deleted=True)
    p = ts.plan("", [t], {}, TODAY)
    assert not p.file_changed and not p.sheet_adds and "lo-a1" not in p.snapshot


def test_old_completed_tasks_are_not_synced():
    old = sheet("lo-a1", "昔", done=True, done_date="2026-06-01")
    p = ts.plan("", [old], {}, TODAY)
    assert not p.file_changed and not p.snapshot
    p2 = ts.plan("- [x] 昔 🆔 lo-a1 ✅ 2026-06-01\n", [old], snap_of(old), TODAY)
    assert not p2.file_changed and not p2.sheet_updates


def test_other_lines_are_preserved_exactly():
    text = "# 私のタスク\n\nメモ書き\n- 普通の箇条書き\n- [ ] 手書き 🆔 lo-h1\n\n> 引用\n"
    p = ts.plan(text, [sheet("lo-h1", "手書き"), sheet("lo-n1", "新規")], snap_of(sheet("lo-h1", "手書き")), TODAY)
    for keep in ("# 私のタスク", "メモ書き", "- 普通の箇条書き", "> 引用"):
        assert keep in p.text.split("\n")


def test_duplicate_id_lines_use_first_only():
    text = "- [ ] A 🆔 lo-d1\n- [ ] B 🆔 lo-d1\n"
    t = sheet("lo-d1", "A")
    p = ts.plan(text, [t], snap_of(t), TODAY)
    assert not p.sheet_updates


def test_ids_assigned_do_not_collide_with_sheet_ids():
    text = "\n".join(f"- [ ] タスク{i}" for i in range(30)) + "\n"
    p = ts.plan(text, [sheet("lo-zzzzzz", "x")], {}, TODAY)
    ids = [a["id"] for a in p.sheet_adds]
    assert len(set(ids)) == 30 and "lo-zzzzzz" not in ids
