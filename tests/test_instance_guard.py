import subprocess
import sys
import textwrap

import pytest

import instance_guard as ig
import notes
import notes_policy as policy
from instance_guard import AlreadyRunning, Lease, ProcessLock, decide, should_stop

ROOT = str(__import__("pathlib").Path(__file__).resolve().parent.parent)


# ---------------------------------------------------------------- 1. 同じパソコン内（ファイルロック）


def try_lock_in_subprocess(path) -> subprocess.CompletedProcess:
    code = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {ROOT!r})
        from instance_guard import ProcessLock
        lock = ProcessLock({str(path)!r})
        if lock.acquire():
            print("ACQUIRED"); lock.release()
        else:
            print("BLOCKED", lock.holder_pid())
    """)
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)


def test_second_process_is_blocked_while_first_holds_lock(tmp_path):
    path = tmp_path / "bot.lock"
    first = ProcessLock(path)
    assert first.acquire()
    try:
        out = try_lock_in_subprocess(path).stdout.strip()
        assert out.startswith("BLOCKED")
        assert first.holder_pid() is not None and out.endswith(str(first.holder_pid()))
    finally:
        first.release()
    assert try_lock_in_subprocess(path).stdout.strip() == "ACQUIRED"  # 解放後は起動できる


def test_lock_is_released_when_the_holder_process_dies(tmp_path):
    path = tmp_path / "bot.lock"
    holder = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(f"""
            import sys, time
            sys.path.insert(0, {ROOT!r})
            from instance_guard import ProcessLock
            l = ProcessLock({str(path)!r}); assert l.acquire(); print("HELD", flush=True); time.sleep(60)
        """)], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "HELD"
        assert try_lock_in_subprocess(path).stdout.strip().startswith("BLOCKED")
        holder.kill()  # 異常終了を模擬（ロックを解放する処理は走らない）
        holder.wait(timeout=30)
        assert try_lock_in_subprocess(path).stdout.strip() == "ACQUIRED"  # 古い PID ファイルが残っても起動できる
    finally:
        if holder.poll() is None:
            holder.kill()


def test_same_process_cannot_take_the_lock_twice(tmp_path):
    a, b = ProcessLock(tmp_path / "x.lock"), ProcessLock(tmp_path / "x.lock")
    assert a.acquire()
    assert not b.acquire()
    a.release()
    assert b.acquire()
    b.release()


# ---------------------------------------------------------------- 2. 別の場所（使用中の印）


def test_decide():
    now = 1000.0
    assert decide({}, "me", now) == "free"
    assert decide({"lease_id": "me", "lease_ts": "999"}, "me", now) == "mine"
    assert decide({"lease_id": "other", "lease_ts": "950"}, "me", now) == "taken"  # 50秒前 → 使用中
    assert decide({"lease_id": "other", "lease_ts": "900"}, "me", now) == "free"  # 100秒前 → 期限切れ
    assert decide({"lease_id": "other", "lease_ts": "910"}, "me", now) == "free"  # ちょうど90秒 → 期限切れ
    assert decide({"lease_id": "other", "lease_ts": "broken"}, "me", now) == "free"
    assert decide({"lease_id": "other"}, "me", now) == "free"


def test_should_stop_when_renewal_keeps_failing():
    assert not should_stop(None, 500)
    assert not should_stop(1000, 1060)  # 60秒失敗 → まだ待つ
    assert should_stop(1000, 1081)  # 81秒（TTL-10）を超えたら止まる


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


class FakeStore:
    """共有の場所（Drive のフォルダのメタデータ）の偽物。複数の Lease から同じものを見る。"""

    def __init__(self):
        self.props = {}
        self.fail = False

    def get_props(self, path):
        if self.fail:
            raise OSError("network")
        return dict(self.props)

    def set_props(self, path, props):
        if self.fail:
            raise OSError("network")
        for k, v in props.items():
            if v is None:
                self.props.pop(k, None)
            else:
                self.props[k] = str(v)


def lease(store, clock, ident):
    return Lease(store, path="06-Life-OS/14-ai", ident=ident, clock=clock, sleep=clock.sleep)


def test_first_instance_acquires_and_second_is_refused():
    store, clock = FakeStore(), Clock()
    a = lease(store, clock, "aaa")
    a.acquire()
    assert store.props["lease_id"] == "aaa"
    b = lease(store, clock, "bbb")
    with pytest.raises(AlreadyRunning) as e:
        b.acquire()
    assert "別の場所" in str(e.value) and "停止してから" in str(e.value)
    assert store.props["lease_id"] == "aaa"  # 拒否された側は印を書き換えない


def test_takeover_after_the_lease_expires():
    store, clock = FakeStore(), Clock()
    a = lease(store, clock, "aaa")
    a.acquire()
    clock.t += ig.TTL + 1  # a がクラッシュして更新が止まった
    b = lease(store, clock, "bbb")
    b.acquire()
    assert store.props["lease_id"] == "bbb"
    assert a.renew() is False and a.lost  # 復帰した a は引き継がれたことに気付いて止まる


def test_renew_keeps_the_lease_alive():
    store, clock = FakeStore(), Clock()
    a = lease(store, clock, "aaa")
    a.acquire()
    for _ in range(10):  # 5分間、30秒ごとに更新
        clock.t += ig.HEARTBEAT
        assert a.renew()
    b = lease(store, clock, "bbb")
    with pytest.raises(AlreadyRunning):
        b.acquire()


def test_simultaneous_start_only_one_survives():
    store, clock = FakeStore(), Clock()
    a, b = lease(store, clock, "aaa"), lease(store, clock, "bbb")
    real_sleep = clock.sleep

    def a_sleep(s):  # a が待っている間に b が書き込む（ほぼ同時の起動）
        b._write()
        real_sleep(s)

    a.sleep = a_sleep
    with pytest.raises(AlreadyRunning):
        a.acquire()  # 後から書いた b が残るので、a は退く
    assert store.props["lease_id"] == "bbb"


def test_release_clears_only_own_lease():
    store, clock = FakeStore(), Clock()
    a = lease(store, clock, "aaa")
    a.acquire()
    a.release()
    assert store.props == {}
    b = lease(store, clock, "bbb")
    b.acquire()
    a.release()  # a は自分の印ではないので消さない
    assert store.props["lease_id"] == "bbb"


def test_release_never_raises_even_if_storage_is_down():
    store, clock = FakeStore(), Clock()
    a = lease(store, clock, "aaa")
    a.acquire()
    store.fail = True
    a.release()  # 例外を出さず、終了処理を続ける


def test_renew_error_propagates_so_the_heartbeat_can_decide():
    store, clock = FakeStore(), Clock()
    a = lease(store, clock, "aaa")
    a.acquire()
    store.fail = True
    with pytest.raises(OSError):
        a.renew()
    assert a.last_ok is not None


def test_lease_path_defaults_to_the_14_ai_room_folder():
    assert Lease(FakeStore()).path == "06-Life-OS/14-ai"


# ---------------------------------------------------------------- 保存先のメタデータ（書き込み制限の対象）


def test_props_are_guarded_like_writes(tmp_path):
    (tmp_path / "06-Life-OS" / "14-ai").mkdir(parents=True)
    (tmp_path / "00inbox").mkdir()
    st = notes.GuardedStore(notes.LocalStore(tmp_path))
    st.set_props("06-Life-OS/14-ai", {"lease_id": "x"})
    assert st.get_props("06-Life-OS/14-ai") == {"lease_id": "x"}
    st.set_props("06-Life-OS/14-ai", {"lease_id": None})
    assert st.get_props("06-Life-OS/14-ai") == {}
    with pytest.raises(policy.AccessDenied):
        st.set_props("00inbox", {"lease_id": "x"})  # 許可外への書き込みは拒否
    assert st.get_props("00inbox") == {}  # 読み取りは可


def test_drive_props_use_app_properties_and_never_touch_content():
    calls = []

    class Files:
        def get(self, fileId, fields=None, **kw):
            calls.append(("get", fileId, fields))
            return _Exec({"appProperties": {"lease_id": "abc"}})

        def update(self, fileId, body=None, media_body=None, **kw):
            calls.append(("update", fileId, body, media_body))
            return _Exec({"id": fileId})

        def list(self, q, **kw):
            import re

            m = re.match(r"'([^']+)' in parents and name='(.*)' and trashed=false$", q)
            table = {("LIFE", "14-ai"): {"id": "D-ai", "name": "14-ai", "mimeType": notes.DriveStore.FOLDER, "parents": ["LIFE"]}}
            hit = table.get((m.group(1), m.group(2))) if m else None
            return _Exec({"files": [hit] if hit else []})

    class Svc:
        def files(self):
            return Files()

    st = notes.GuardedStore(notes.DriveStore("LIFE", "", "", svc=Svc()))
    assert st.get_props("06-Life-OS/14-ai") == {"lease_id": "abc"}
    st.set_props("06-Life-OS/14-ai", {"lease_id": "zzz", "lease_ts": None})
    kind, fid, body, media = calls[-1]
    assert (kind, fid, body["appProperties"], media) == ("update", "D-ai", {"lease_id": "zzz", "lease_ts": None}, None)
    with pytest.raises(policy.AccessDenied):
        st.set_props("00inbox/x", {"a": "b"})


class _Exec:
    def __init__(self, v):
        self.v = v

    def execute(self):
        return self.v


# ---------------------------------------------------------------- systemd の再起動対応（同じホストの古い印）


def test_same_host_stale_lease_is_taken_over_immediately_when_lock_is_held():
    store, clock = FakeStore(), Clock()
    crashed = lease(store, clock, "old")
    crashed.host = "life-os-bot"
    crashed.acquire()  # 前回の Bot（クラッシュして印だけが残った）
    clock.t += 5  # 5秒後に systemd が再起動
    new = lease(store, clock, "new")
    new.host = "life-os-bot"
    with pytest.raises(AlreadyRunning):
        new.acquire()  # ProcessLock を持っていない（same_host_ok なし）場合は、期限まで待つ
    new.acquire(same_host_ok=True)
    assert store.props["lease_id"] == "new"


def test_other_host_lease_is_never_taken_over_even_with_the_flag():
    store, clock = FakeStore(), Clock()
    pc = lease(store, clock, "pc")
    pc.host = "MY-PC"
    pc.acquire()
    vm = lease(store, clock, "vm")
    vm.host = "life-os-bot"
    with pytest.raises(AlreadyRunning):
        vm.acquire(same_host_ok=True)
    assert store.props["lease_id"] == "pc"


def test_exit_codes_match_the_systemd_unit():
    assert ig.EXIT_ALREADY_RUNNING == 3 and ig.EXIT_LOST == 2
