"""DriveStore と GAS 中継の連携: 偽の Drive と偽の中継で、誰が何を実行するかを検証する。

- 既存ファイル → サービスアカウントが更新（中継は呼ばない）
- 存在しないファイル → 中継が作成（サービスアカウントは何も作成しない）
- 許可外パス → 中継にもサービスアカウントにも届かない
"""
import re

import pytest
from googleapiclient.http import MediaInMemoryUpload

import gas_relay
import notes
from notes import DriveStore, GuardedStore
from notes_policy import LIFEOS_DIR, TASK_FILE, AccessDenied

FOLDER = DriveStore.FOLDER


class _Exec:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


class FakeFiles:
    """Drive の files() の最小の偽物。**create は実装しない**（サービスアカウントが作成したら AttributeError）。"""

    def __init__(self):
        self.items: dict[str, dict] = {}
        self.updates: list[tuple[str, bytes | None]] = []

    def add(self, fid, name, parent, folder=False, content=b""):
        self.items[fid] = {"id": fid, "name": name, "mimeType": FOLDER if folder else "text/markdown",
                           "parents": [parent] if parent else [], "content": content,
                           "webViewLink": f"https://drive/{fid}", "modifiedTime": "2026-09-19T00:00:00Z"}

    def list(self, q, **kw):
        m = re.match(r"'([^']+)' in parents and name='(.*)' and trashed=false$", q)
        if m:
            parent, name = m.group(1), m.group(2).replace("\\'", "'")
            return _Exec({"files": [dict(i) for i in self.items.values() if parent in i["parents"] and i["name"] == name]})
        m = re.match(r"'([^']+)' in parents and trashed=false$", q)
        if m:
            return _Exec({"files": [dict(i) for i in self.items.values() if m.group(1) in i["parents"]]})
        raise AssertionError(f"想定外のクエリ: {q}")

    def get(self, fileId, **kw):
        return _Exec(dict(self.items[fileId]))

    def get_media(self, fileId, **kw):
        return _Exec(self.items[fileId]["content"])

    def update(self, fileId, body=None, media_body=None, **kw):
        data = None
        if media_body is not None:
            assert isinstance(media_body, MediaInMemoryUpload)
            data = media_body.getbytes(0, media_body.size())
            self.items[fileId]["content"] = data
        self.updates.append((fileId, data))
        return _Exec({"id": fileId, "webViewLink": f"https://drive/{fileId}"})


class FakeSvc:
    def __init__(self, files):
        self._files = files

    def files(self):
        return self._files


class FakeRelay:
    def __init__(self, files: FakeFiles, status="created"):
        self.files, self.status, self.calls = files, status, []

    def _result(self, path):
        if self.status == "exists":
            return {"ok": False, "status": "exists", "id": "F-ideas"}
        return {"ok": True, "status": "created", "url": f"https://new/{path}"}

    def create_file(self, path, text):
        self.calls.append(("create_file", path, text))
        return self._result(path)

    def create_bytes(self, path, data, mime="application/octet-stream"):
        self.calls.append(("create_bytes", path, data, mime))
        return self._result(path)

    def rename(self, old, new):
        self.calls.append(("rename", old, new))
        return {"ok": True, "status": "renamed"}

    def trash(self, path):
        self.calls.append(("trash", path))
        return {"ok": True, "status": "trashed"}


@pytest.fixture
def env():
    files = FakeFiles()
    files.add("LIFE", LIFEOS_DIR, None, folder=True)
    files.add("D-idea", "09-idea", "LIFE", folder=True)
    files.add("F-ideas", "Ideas.md", "D-idea", content="旧".encode())
    files.add("TASK", "01 Life-OS-Task.md", None, content=b"")
    relay = FakeRelay(files)
    store = GuardedStore(DriveStore("LIFE", "TASK", "", relay=relay, svc=FakeSvc(files)))
    return store, files, relay


def test_existing_file_is_updated_by_service_account_not_relay(env):
    store, files, relay = env
    store.write(f"{LIFEOS_DIR}/09-idea/Ideas.md", "# Ideas\n新")
    assert files.items["F-ideas"]["content"] == "# Ideas\n新".encode()
    assert relay.calls == []
    assert files.updates and files.updates[0][0] == "F-ideas"


def test_missing_file_is_created_by_relay_only(env):
    store, files, relay = env
    link = store.write(f"{LIFEOS_DIR}/09-idea/New.md", "本文")
    assert relay.calls == [("create_file", f"{LIFEOS_DIR}/09-idea/New.md", "本文")]
    assert files.updates == []  # サービスアカウントは何も書いていない
    assert link == f"https://new/{LIFEOS_DIR}/09-idea/New.md"


def test_missing_parent_folder_is_left_to_relay(env):
    store, files, relay = env
    store.write(f"{LIFEOS_DIR}/10-project-novel/Novel_未命名_20260919.md", "始まり")
    assert relay.calls[0][:2] == ("create_file", f"{LIFEOS_DIR}/10-project-novel/Novel_未命名_20260919.md")


def test_bytes_go_through_relay(env):
    store, files, relay = env
    store.write_bytes(f"{LIFEOS_DIR}/08-scrap/attachments/a.png", b"\x89PNG", "image/png")
    assert relay.calls == [("create_bytes", f"{LIFEOS_DIR}/08-scrap/attachments/a.png", b"\x89PNG", "image/png")]


def test_relay_reports_exists_falls_back_to_update(env):
    store, files, relay = env
    relay.status = "exists"
    # Drive の反映遅延で一覧に出ていなかった、という状況: 先に見えないファイル名を指定して create → exists
    files.items["F-late"] = {**files.items["F-ideas"], "id": "F-late", "name": "Late.md", "content": b""}
    orig = files.items.pop("F-late")
    calls = {"n": 0}
    real_list = files.list

    def flaky_list(q, **kw):  # 最初の1回は見えない
        calls["n"] += 1
        if calls["n"] <= 2:
            return _Exec({"files": [i for i in real_list(q).execute()["files"] if i["name"] != "Late.md"]})
        files.items["F-late"] = orig
        return real_list(q, **kw)

    files.list = flaky_list
    store.write(f"{LIFEOS_DIR}/09-idea/Late.md", "遅延")
    assert relay.calls[0][0] == "create_file"
    assert files.items["F-late"]["content"] == "遅延".encode()


def test_task_file_updated_by_id(env):
    store, files, relay = env
    store.write(TASK_FILE, "- [ ] a 🆔 lo-1\n")
    assert files.items["TASK"]["content"] == "- [ ] a 🆔 lo-1\n".encode()
    assert relay.calls == []


@pytest.mark.parametrize("path", ["00inbox/x.md", "01🗃Task/Project/p.md", "01🗃Task/02 New.md", "x.md",
                                  f"{LIFEOS_DIR}/../00inbox/x.md"])
def test_denied_paths_never_reach_relay_or_drive(env, path):
    store, files, relay = env
    with pytest.raises(AccessDenied):
        store.write(path, "x")
    with pytest.raises(AccessDenied):
        store.write_bytes(path, b"x")
    with pytest.raises(AccessDenied):
        store.rename(path, f"{LIFEOS_DIR}/moved.md")
    with pytest.raises(AccessDenied):
        store.delete(path)
    assert relay.calls == [] and files.updates == []


def test_rename_and_delete_use_relay(env):
    store, files, relay = env
    store.rename(f"{LIFEOS_DIR}/09-idea/Ideas.md", f"{LIFEOS_DIR}/09-idea/Ideas2.md")
    store.delete(f"{LIFEOS_DIR}/09-idea/Ideas2.md")
    assert relay.calls == [("rename", f"{LIFEOS_DIR}/09-idea/Ideas.md", f"{LIFEOS_DIR}/09-idea/Ideas2.md"),
                           ("trash", f"{LIFEOS_DIR}/09-idea/Ideas2.md")]


def test_rename_to_outside_is_denied(env):
    store, files, relay = env
    with pytest.raises(AccessDenied):
        store.rename(f"{LIFEOS_DIR}/09-idea/Ideas.md", "00inbox/Ideas.md")
    assert relay.calls == []


def test_task_file_cannot_be_renamed_or_trashed(env):
    store, files, relay = env
    with pytest.raises(AccessDenied):
        store.rename(TASK_FILE, f"{LIFEOS_DIR}/t.md")
    with pytest.raises(AccessDenied):
        store.delete(TASK_FILE)
    assert relay.calls == []


def test_creating_without_relay_fails_clearly(env):
    _, files, _ = env
    store = GuardedStore(DriveStore("LIFE", "TASK", "", relay=None, svc=FakeSvc(files)))
    with pytest.raises(gas_relay.RelayError, match="未設定"):
        store.write(f"{LIFEOS_DIR}/09-idea/New.md", "x")
    store.write(f"{LIFEOS_DIR}/09-idea/Ideas.md", "更新は中継なしで可能")  # 既存の更新は動く
    assert files.items["F-ideas"]["content"] == "更新は中継なしで可能".encode()


def test_reading_existing_and_missing(env):
    store, files, _ = env
    assert store.read(f"{LIFEOS_DIR}/09-idea/Ideas.md") == "旧"
    assert store.read(f"{LIFEOS_DIR}/09-idea/none.md") is None
    assert store.read(f"{LIFEOS_DIR}/nodir/none.md") is None


def test_service_account_class_has_no_create_path():
    import inspect

    assert "files().create" not in inspect.getsource(DriveStore)
