"""DriveStore と GAS 中継の連携: 偽の Drive と偽の中継で、誰が何を実行するかを検証する。

- 既存ファイル → サービスアカウントが更新（中継は呼ばない）
- 存在しないファイル → 中継が作成（サービスアカウントは何も作成しない）
- 許可外パス → 中継にもサービスアカウントにも届かない
"""
import re

import httplib2
import pytest
from googleapiclient.errors import HttpError
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
        self.hidden: set[str] = set()  # 検索（list）にはまだ出ない ID（Drive の反映遅れ）。ID での取得（get）はできる

    def add(self, fid, name, parent, folder=False, content=b""):
        self.items[fid] = {"id": fid, "name": name, "mimeType": FOLDER if folder else "text/markdown",
                           "parents": [parent] if parent else [], "content": content,
                           "webViewLink": f"https://drive/{fid}", "modifiedTime": "2026-09-19T00:00:00Z", "trashed": False}

    def list(self, q, **kw):
        m = re.match(r"'([^']+)' in parents and name='(.*)' and trashed=false$", q)
        if m:
            parent, name = m.group(1), m.group(2).replace("\\'", "'")
            return _Exec({"files": [dict(i) for i in self.items.values() if parent in i["parents"] and i["name"] == name
                                    and i["id"] not in self.hidden and not i["trashed"]]})
        m = re.match(r"'([^']+)' in parents and trashed=false$", q)
        if m:
            return _Exec({"files": [dict(i) for i in self.items.values() if m.group(1) in i["parents"]
                                    and i["id"] not in self.hidden and not i["trashed"]]})
        raise AssertionError(f"想定外のクエリ: {q}")

    def get(self, fileId, **kw):
        if fileId not in self.items:
            raise HttpError(httplib2.Response({"status": 404}), b"not found")
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
    """GAS 中継の偽物。実際にファイルを作る。作ったファイルは、Drive の検索にすぐには出ない（lag=True）。"""

    def __init__(self, files: FakeFiles, status="created", lag=True):
        self.files, self.status, self.lag, self.calls, self.n = files, status, lag, [], 0

    def _walk(self, path, create):
        """path の親フォルダ ID と、そのファイル名（既存のファイルは、検索に出ていなくても見つける）。"""
        parts = path.split("/")
        parent = "LIFE"  # 先頭は 06-Life-OS
        for part in parts[1:-1]:
            hit = next((i for i in self.files.items.values() if part == i["name"] and parent in i["parents"]
                        and i["mimeType"] == FOLDER), None)
            if hit is None:
                self.n += 1
                fid = f"D-new{self.n}"
                self.files.add(fid, part, parent, folder=True)
                hit = self.files.items[fid]
            parent = hit["id"]
        return parent, parts[-1]

    def _result(self, path, content):
        parent, name = self._walk(path, True)
        existing = next((i for i in self.files.items.values() if i["name"] == name and parent in i["parents"]
                         and i["mimeType"] != FOLDER), None)
        if existing is not None:
            return {"ok": False, "status": "exists", "id": existing["id"]}
        self.n += 1
        fid = f"F-new{self.n}"
        self.files.add(fid, name, parent, content=content)
        if self.lag:
            self.files.hidden.add(fid)
        return {"ok": True, "status": "created", "id": fid, "url": f"https://new/{path}"}

    def create_file(self, path, text):
        self.calls.append(("create_file", path, text))
        return self._result(path, text.encode("utf-8"))

    def create_bytes(self, path, data, mime="application/octet-stream"):
        self.calls.append(("create_bytes", path, data, mime))
        return self._result(path, data)

    def rename(self, old, new):
        self.calls.append(("rename", old, new))
        return {"ok": True, "status": "renamed"}

    def trash(self, path):
        self.calls.append(("trash", path))
        parent, name = self._walk(path, False)
        for i in self.files.items.values():
            if i["name"] == name and parent in i["parents"]:
                i["trashed"] = True
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


def test_relay_says_exists_but_search_cannot_see_it_then_content_is_written_by_id(env):
    """検索の反映遅れで見えなかった既存ファイル。以前は、何も書かずに成功として返していた（書き込みが黙って失われた）。"""
    store, files, relay = env
    files.add("F-late", "Late.md", "D-idea", content=b"")
    files.hidden.add("F-late")  # 検索には出ない
    store.write(f"{LIFEOS_DIR}/09-idea/Late.md", "遅延")
    assert relay.calls[0][0] == "create_file"  # 見えなかったので、中継に作成を頼んだ → exists が返る
    assert files.items["F-late"]["content"] == "遅延".encode()  # 返ってきた ID で更新された


def test_relay_says_exists_without_an_id_is_an_error_not_a_silent_success(env):
    store, files, relay = env
    relay._result = lambda path, content: {"ok": False, "status": "exists"}
    files.add("F-late", "Late.md", "D-idea", content=b"old")
    files.hidden.add("F-late")
    with pytest.raises(gas_relay.RelayError, match="更新できませんでした"):
        store.write(f"{LIFEOS_DIR}/09-idea/Late.md", "新")
    assert files.items["F-late"]["content"] == b"old"


def test_a_file_just_created_is_usable_before_search_can_see_it(env):
    """作った直後の read-modify-write（追記）で、以前の記録が消えたり、追記が失われたりしない。"""
    store, files, relay = env
    path = f"{LIFEOS_DIR}/10-project-novel/Novel_月の庭.md"
    store.append_entry(path, "## 1回目\n最初の記録", "月の庭")
    assert [c[0] for c in relay.calls] == ["create_file"]
    new_id = next(i for i in files.hidden)
    assert store.read(path).startswith("# 月の庭\n\n## 1回目")  # 検索に出ていなくても読める
    store.append_entry(path, "## 2回目\n次の記録", "月の庭")
    assert [c[0] for c in relay.calls] == ["create_file"]  # 2回目は作成し直さず、更新（サービスアカウント）
    assert files.updates[-1][0] == new_id
    text = files.items[new_id]["content"].decode()
    assert "最初の記録" in text and "次の記録" in text and text.index("最初の記録") < text.index("次の記録")
    store.append_entry(path, "## 3回目\nさらに", "月の庭")
    assert files.items[new_id]["content"].decode().count("\n## ") == 3


def test_listing_includes_files_search_has_not_indexed_yet(env):
    store, files, relay = env
    store.write(f"{LIFEOS_DIR}/10-project-novel/Novel_A.md", "a")
    store.write(f"{LIFEOS_DIR}/10-project-novel/Novel_B.md", "b")
    listed = store.list(f"{LIFEOS_DIR}/10-project-novel")
    assert listed == [f"{LIFEOS_DIR}/10-project-novel/Novel_A.md", f"{LIFEOS_DIR}/10-project-novel/Novel_B.md"]
    assert store.list(f"{LIFEOS_DIR}/09-idea") == [f"{LIFEOS_DIR}/09-idea/Ideas.md"]  # 別のフォルダには混ざらない


def test_remembered_id_is_forgotten_when_the_file_is_deleted_or_trashed(env):
    store, files, relay = env
    path = f"{LIFEOS_DIR}/09-idea/Tmp.md"
    store.write(path, "一時")
    assert store.read(path) == "一時"
    store.delete(path)
    assert store.read(path) is None  # ゴミ箱に入ったファイルは、覚えていても使わない
    store2 = env[0]
    files.items[next(i for i, v in files.items.items() if v["name"] == "Tmp.md")]["trashed"] = True
    assert store2.list(f"{LIFEOS_DIR}/09-idea") == [f"{LIFEOS_DIR}/09-idea/Ideas.md"]


def test_remembered_id_is_forgotten_when_drive_says_not_found(env):
    store, files, relay = env
    path = f"{LIFEOS_DIR}/09-idea/Gone.md"
    store.write(path, "消える")
    files.items.pop(next(i for i, v in files.items.items() if v["name"] == "Gone.md"))  # Drive 側で完全に消えた
    assert store.read(path) == "消える"  # 作成から90秒以内は、自分が書いた内容を返す（同期ソフトの書き戻しへの備え）
    store._b._clock = lambda: __import__("time").time() + 1000  # 90秒を過ぎたら、クラウドを見に行く
    assert store.read(path) is None  # 例外にならず、「無い」として扱う


def test_rename_carries_the_remembered_id_to_the_new_name(env):
    store, files, relay = env
    old, new = f"{LIFEOS_DIR}/10-project-novel/Novel_未命名.md", f"{LIFEOS_DIR}/10-project-novel/Novel_月の庭.md"
    store.write(old, "# 未命名\n記録")
    fid = next(iter(files.hidden))
    store.rename(old, new)  # 中継が改名する（偽物は名前を変えないので、名前だけ変えておく）
    files.items[fid]["name"] = "Novel_月の庭.md"
    assert store._b._known.get(new) == fid and old not in store._b._known
    assert store.read(new) == "# 未命名\n記録"  # 改名直後も、検索を待たずに読める


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
