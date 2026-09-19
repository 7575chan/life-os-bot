"""同期ソフト（Google Drive for Desktop）の「作成直後の古い内容の書き戻し」への備え。

実機で確認した現象: 中継が作ったファイルに、作成の数秒〜20秒後に、パソコンの同期ソフトが「作成直後の内容」を
クラウドへ書き戻し、その間に Bot が追記した内容が消える。
"""
import pytest

import notes
from notes import DriveStore, GuardedStore
from notes_policy import LIFEOS_DIR
from tests.test_drive_store_relay import FakeFiles, FakeRelay, FakeSvc

PATH = f"{LIFEOS_DIR}/10-project-novel/Novel_月の庭.md"


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def env():
    files = FakeFiles()
    files.add("LIFE", LIFEOS_DIR, None, folder=True)
    files.add("D-idea", "09-idea", "LIFE", folder=True)
    files.add("F-old", "Old.md", "D-idea", content=b"old")
    clock, timers = Clock(), []
    relay = FakeRelay(files)
    store = GuardedStore(DriveStore("LIFE", "", "", relay=relay, svc=FakeSvc(files), clock=clock,
                                    schedule=lambda delay, fn: timers.append((delay, fn))))
    fid = lambda: next(i for i, v in files.items.items() if v["name"] == "Novel_月の庭.md")

    def run_timers():
        due = list(timers)
        timers.clear()
        for _, fn in due:
            fn()
        return len(due)

    def sync_reverts_to(content: bytes):
        files.items[fid()]["content"] = content  # 同期ソフトが、古い内容をクラウドに書き戻した

    return store, files, clock, timers, fid, run_timers, sync_reverts_to


def cloud(files, fid):
    return files.items[fid()]["content"].decode()


def test_revert_by_the_sync_client_is_detected_and_repaired(env):
    store, files, clock, timers, fid, run_timers, revert = env
    store.append_entry(PATH, "## 1回目\n最初", "月の庭")
    first = files.items[fid()]["content"]
    store.append_entry(PATH, "## 2回目\n次", "月の庭")  # 作成の数秒後に追記
    assert sorted(d for d, _ in timers) == [15.0, 35.0, 65.0]  # 追記のたびに、15・35・65秒後の確認を予約する
    revert(first)  # 数秒後、同期ソフトが作成直後の内容を書き戻す
    assert "次" not in cloud(files, fid)
    clock.t += 15
    run_timers()  # 15秒後の確認
    assert "最初" in cloud(files, fid) and "次" in cloud(files, fid)  # 書き直されている


def test_reads_during_the_window_use_our_last_write_not_the_stale_cloud(env):
    store, files, clock, timers, fid, run_timers, revert = env
    store.append_entry(PATH, "## 1回目\n最初", "月の庭")
    first = files.items[fid()]["content"]
    store.append_entry(PATH, "## 2回目\n次", "月の庭")
    revert(first)
    text = store.read(PATH)
    assert "最初" in text and "次" in text  # クラウドは古いが、自分が書いた内容を返す
    store.append_entry(PATH, "## 3回目\nさらに", "月の庭")  # 古い内容をもとに追記して、2回目が消えることはない
    assert all(w in cloud(files, fid) for w in ("最初", "次", "さらに"))


def test_a_second_revert_after_the_first_repair_is_also_repaired(env):
    store, files, clock, timers, fid, run_timers, revert = env
    store.append_entry(PATH, "## 1回目\n最初", "月の庭")
    first = files.items[fid()]["content"]
    store.append_entry(PATH, "## 2回目\n次", "月の庭")
    revert(first)
    run_timers()
    assert "次" in cloud(files, fid)
    revert(first)  # 65秒後にも、もう一度書き戻された
    clock.t += 60
    store._b._verify(PATH)
    assert "次" in cloud(files, fid)


def test_unknown_content_is_never_overwritten(env):
    """Obsidian などで編集された内容は、書き戻しではないので、書き直さない。"""
    store, files, clock, timers, fid, run_timers, revert = env
    store.append_entry(PATH, "## 1回目\n最初", "月の庭")
    store.append_entry(PATH, "## 2回目\n次", "月の庭")
    revert("ユーザーが Obsidian で書いた別の内容".encode())
    updates_before = len(files.updates)
    run_timers()
    assert cloud(files, fid) == "ユーザーが Obsidian で書いた別の内容" and len(files.updates) == updates_before


def test_no_verification_or_cache_for_files_not_created_by_us(env):
    store, files, clock, timers, fid, run_timers, revert = env
    store.write(f"{LIFEOS_DIR}/09-idea/Old.md", "更新")
    assert timers == [] and store._b._recent == {}  # 以前からあるファイルは、対象外（書き戻しは起きない）
    assert store.read(f"{LIFEOS_DIR}/09-idea/Old.md") == "更新"


def test_after_the_window_reads_go_to_the_cloud_again(env):
    store, files, clock, timers, fid, run_timers, revert = env
    store.write(PATH, "作成時")
    clock.t += DriveStore.SETTLE_SECONDS + 1
    files.items[fid()]["content"] = "ユーザーが編集".encode()
    assert store.read(PATH) == "ユーザーが編集"  # 90秒を過ぎたら、クラウドの内容を読む（ユーザーの編集を尊重）


def test_updates_after_the_window_schedule_nothing(env):
    store, files, clock, timers, fid, run_timers, revert = env
    store.write(PATH, "作成時")
    clock.t += DriveStore.SETTLE_SECONDS + 1
    store.write(PATH, "90秒後の更新")
    assert timers == []


def test_verification_is_a_noop_when_the_file_was_deleted_or_renamed(env):
    store, files, clock, timers, fid, run_timers, revert = env
    store.append_entry(PATH, "## 1回目\n最初", "月の庭")
    store.append_entry(PATH, "## 2回目\n次", "月の庭")
    store.delete(PATH)
    run_timers()  # 例外にならない
    assert store._b._recent == {}


def test_drive_calls_are_serialized_with_a_lock(env):
    store, *_ = env
    for name in ("read", "write", "write_bytes", "rename", "delete", "list", "modified", "get_props", "set_props", "search", "_verify"):
        assert hasattr(getattr(DriveStore, name), "__wrapped__"), name  # スレッドをまたいで接続を使わない


def test_timer_scheduler_runs_callbacks_and_swallows_errors(caplog):
    import logging
    import threading

    s = DriveStore("LIFE", "", "", svc=object())
    done = threading.Event()
    s._start_timer(0.01, done.set)
    assert done.wait(2)

    failed, uncaught = threading.Event(), []
    old_hook = threading.excepthook
    threading.excepthook = lambda args: uncaught.append(args)  # スレッドの未処理の例外を記録する
    try:
        def boom():
            failed.set()
            raise RuntimeError("boom")

        with caplog.at_level(logging.WARNING, logger="life-os.notes"):
            s._start_timer(0.01, boom)
            assert failed.wait(2)
            for _ in range(50):  # ログが出るまで少し待つ
                if caplog.records:
                    break
                threading.Event().wait(0.02)
    finally:
        threading.excepthook = old_hook
    assert uncaught == []  # 例外でスレッドが落ちない
    assert any("書き戻しの確認に失敗" in r.message for r in caplog.records)  # 警告として記録される
