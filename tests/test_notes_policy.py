"""アクセスポリシー: 読み取りは Vault 全体で可、書き込み系は 06-Life-OS/ とタスク棚のみ。"""
import logging
import os
import unicodedata

import pytest

import notes
import notes_policy as policy
from notes_policy import LIFEOS_DIR, TASK_FILE, AccessDenied

PROTECTED = [
    "01🗃Task/Project/novel.md",
    "01🗃Task/Archive/old.md",
    "01🗃Task/other.md",
    "01🗃Task/01 Life-OS-Task.md.bak",
    "00inbox/a.md",
    "02 📝 memo/a.md",
    "03-Writing/a.md",
    "90💻project/a.md",
    "Ideas.md",
    f"{LIFEOS_DIR}-evil/x.md",  # 前方一致すり抜け
    "06-life-os/x.md",  # 大文字小文字違い
]
WRITE_OPS = ["write", "rename", "delete"]


def test_task_file_name_has_no_spaces_around_emoji():
    assert TASK_FILE == "01🗃Task/01 Life-OS-Task.md"


# ---------------------------------------------------------------- 書き込み許可


@pytest.mark.parametrize("path", [f"{LIFEOS_DIR}/09-idea/Ideas.md", f"{LIFEOS_DIR}/a/b/c.md", f"{LIFEOS_DIR}/x.md"])
def test_life_os_writable(path):
    for op in ("read", "write", "rename", "delete", "list"):
        assert policy.check(path, op) == path


def test_task_file_content_write_only():
    assert policy.check(TASK_FILE, "read") == TASK_FILE
    assert policy.check(TASK_FILE, "write") == TASK_FILE
    for op in ("rename", "delete"):
        with pytest.raises(AccessDenied):
            policy.check(TASK_FILE, op)


def test_life_os_folder_itself_not_modifiable():
    assert policy.check(LIFEOS_DIR, "list") == LIFEOS_DIR
    for op in WRITE_OPS:
        with pytest.raises(AccessDenied):
            policy.check(LIFEOS_DIR, op)


def test_vault_root_list_only():
    assert policy.check("", "list") == ""
    for op in WRITE_OPS + ["read"]:
        with pytest.raises(AccessDenied):
            policy.check("", op)


# ---------------------------------------------------------------- 許可外: 読み取りは可、書き込み系は不可


@pytest.mark.parametrize("path", PROTECTED)
def test_protected_readable(path):
    assert policy.check(path, "read") == path
    assert policy.check(path, "list") == path


@pytest.mark.parametrize("path", PROTECTED)
@pytest.mark.parametrize("op", WRITE_OPS)
def test_protected_not_writable(path, op):
    with pytest.raises(AccessDenied):
        policy.check(path, op)


@pytest.mark.parametrize(
    "path",
    [
        f"{LIFEOS_DIR}/../00inbox/x.md",
        f"{LIFEOS_DIR}/../01🗃Task/Project/x.md",
        "../06-Life-OS/x.md",
        f"/{LIFEOS_DIR}/x.md",
        f"C:/{LIFEOS_DIR}/x.md",
        f"{LIFEOS_DIR}\\..\\00inbox\\x.md",
        f"{LIFEOS_DIR}//x.md",
        f"{LIFEOS_DIR}/./x.md",
        f"{LIFEOS_DIR}/x.md\x00.txt",
        "   ",
    ],
)
@pytest.mark.parametrize("op", ["read", "write", "list"])
def test_escape_attempts_denied_for_all_ops(path, op):
    with pytest.raises(AccessDenied):
        policy.check(path, op)


def test_unicode_normalization():
    nfd = unicodedata.normalize("NFD", f"{LIFEOS_DIR}/がぎぐ.md")
    assert nfd != f"{LIFEOS_DIR}/がぎぐ.md"
    assert policy.check(nfd, "write") == f"{LIFEOS_DIR}/がぎぐ.md"


def test_non_string_denied():
    with pytest.raises(AccessDenied):
        policy.check(None, "read")  # type: ignore[arg-type]


def test_write_denial_is_logged(caplog):
    with caplog.at_level(logging.ERROR, logger="life-os.guard"):
        with pytest.raises(AccessDenied):
            policy.check("00inbox/secret.md", "write")
    assert any("DENY" in r.message and "00inbox/secret.md" in r.message for r in caplog.records)


def test_read_of_protected_is_not_logged_as_denial(caplog):
    with caplog.at_level(logging.ERROR, logger="life-os.guard"):
        policy.check("00inbox/secret.md", "read")
    assert not caplog.records


# ---------------------------------------------------------------- 実ファイルでの動作 (LocalStore)


@pytest.fixture
def vault(tmp_path):
    (tmp_path / LIFEOS_DIR).mkdir()
    for d in ("00inbox", "01🗃Task/Project", "01🗃Task/Archive"):
        (tmp_path / d).mkdir(parents=True)
    (tmp_path / "00inbox/secret.md").write_text("秘密 keyword", encoding="utf-8")
    (tmp_path / "01🗃Task/Project/p.md").write_text("手動管理 keyword", encoding="utf-8")
    (tmp_path / "01🗃Task/other.md").write_text("他のファイル", encoding="utf-8")
    (tmp_path / TASK_FILE).write_text("", encoding="utf-8")
    return tmp_path


@pytest.fixture
def store(vault):
    return notes.GuardedStore(notes.LocalStore(vault))


def test_write_and_read_in_life_os(store, vault):
    store.write(f"{LIFEOS_DIR}/09-idea/Ideas.md", "# Ideas\n")
    assert store.read(f"{LIFEOS_DIR}/09-idea/Ideas.md") == "# Ideas\n"


def test_task_file_can_be_written(store, vault):
    store.write(TASK_FILE, "- [ ] a\n")
    assert (vault / TASK_FILE).read_text(encoding="utf-8") == "- [ ] a\n"


@pytest.mark.parametrize("path", ["00inbox/secret.md", "01🗃Task/Project/p.md", "01🗃Task/other.md"])
def test_protected_files_readable_but_never_changed(store, vault, path):
    before = (vault / path).read_text(encoding="utf-8")
    assert store.read(path) == before  # 読み取りは許可
    with pytest.raises(AccessDenied):
        store.write(path, "上書き")
    with pytest.raises(AccessDenied):
        store.delete(path)
    with pytest.raises(AccessDenied):
        store.rename(path, f"{LIFEOS_DIR}/moved.md")
    assert (vault / path).read_text(encoding="utf-8") == before
    assert not (vault / LIFEOS_DIR / "moved.md").exists()


def test_cannot_create_new_file_outside(store, vault):
    for p in ("00inbox/new.md", "01🗃Task/02 New.md", "new-at-root.md"):
        with pytest.raises(AccessDenied):
            store.write(p, "x")
        with pytest.raises(AccessDenied):
            store.write_bytes(p.replace(".md", ".png"), b"x")
        assert not (vault / p).exists()


def test_rename_out_of_life_os_denied(store, vault):
    store.write(f"{LIFEOS_DIR}/a.md", "x")
    with pytest.raises(AccessDenied):
        store.rename(f"{LIFEOS_DIR}/a.md", "00inbox/a.md")
    assert (vault / LIFEOS_DIR / "a.md").exists()
    assert not (vault / "00inbox/a.md").exists()


def test_task_file_cannot_be_renamed_or_deleted(store, vault):
    with pytest.raises(AccessDenied):
        store.rename(TASK_FILE, f"{LIFEOS_DIR}/tasks.md")
    with pytest.raises(AccessDenied):
        store.delete(TASK_FILE)
    assert (vault / TASK_FILE).exists()


def test_list_whole_vault_allowed(store):
    listed = store.list("")
    assert "00inbox/secret.md" in listed and "01🗃Task/Project/p.md" in listed
    assert store.list("01🗃Task/Project") == ["01🗃Task/Project/p.md"]


def test_search_scopes(store):
    store.write(f"{LIFEOS_DIR}/n.md", "keyword ここにある")
    default = {h["path"] for h in store.search("keyword")}
    assert default == {f"{LIFEOS_DIR}/n.md"}  # 既定は 06-Life-OS のみ
    whole = {h["path"] for h in store.search("keyword", scope="")}
    assert {"00inbox/secret.md", "01🗃Task/Project/p.md", f"{LIFEOS_DIR}/n.md"} <= whole


def test_delete_moves_to_trash(store, vault):
    store.write(f"{LIFEOS_DIR}/x.md", "bye")
    store.delete(f"{LIFEOS_DIR}/x.md")
    assert not (vault / LIFEOS_DIR / "x.md").exists()
    assert list((vault / LIFEOS_DIR / ".trash").glob("*_x.md"))


def test_symlink_write_escape_denied(store, vault):
    link = vault / LIFEOS_DIR / "link"
    try:
        os.symlink(vault / "00inbox", link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("シンボリックリンクを作成できない環境")
    with pytest.raises(AccessDenied):
        store.write(f"{LIFEOS_DIR}/link/new.md", "x")
    with pytest.raises(AccessDenied):
        store.write(f"{LIFEOS_DIR}/link/secret.md", "上書き")
    assert not (vault / "00inbox/new.md").exists()
    assert (vault / "00inbox/secret.md").read_text(encoding="utf-8") == "秘密 keyword"


def test_denied_write_never_reaches_backend(vault):
    calls = []

    class Spy(notes.LocalStore):
        def write(self, path, text):
            calls.append(path)
            return super().write(path, text)

        def delete(self, path):
            calls.append(path)

        def rename(self, old, new):
            calls.append(old)

    st = notes.GuardedStore(Spy(vault))
    for fn, args in ((st.write, ("00inbox/x.md", "x")), (st.delete, ("00inbox/secret.md",)),
                     (st.rename, ("00inbox/secret.md", f"{LIFEOS_DIR}/s.md"))):
        with pytest.raises(AccessDenied):
            fn(*args)
    assert calls == []
