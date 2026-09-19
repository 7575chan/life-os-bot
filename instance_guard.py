"""多重起動の防止（マニュアル 8-11 の発展版）。Bot が二重に動いて、通知や返信が重複するのを防ぐ。

2段構え:
1. ProcessLock — 同じパソコンの中の二重起動を、OS のファイルロックで防ぐ。プロセスが異常終了しても
   OS がロックを自動で解除するので、古い PID ファイルが残る問題がない。
2. Lease — パソコンと VM など、別の場所で同時に動くのを防ぐ。`06-Life-OS/14-ai` フォルダの
   メタデータ（ファイルの中身は変わらず、Obsidian には見えない）に「使用中の印」を書き、30秒ごとに更新する。
   印は90秒で期限切れになるので、Bot がクラッシュしても自動で解除される。
"""
import asyncio
import logging
import socket
import sys
import time
import uuid
from pathlib import Path

import config
import notes_policy

log = logging.getLogger("life-os.instance")

TTL = 90.0  # 印の有効期間（秒）
HEARTBEAT = 30.0  # 印の更新間隔（秒）
SETTLE = 3.0  # 起動時に、同時に起動した相手がいないか確かめるまでの待ち時間（秒）
KEYS = ("lease_id", "lease_host", "lease_ts")

# 終了コード: systemd の `RestartPreventExitStatus=3` と対応。すでに別の Bot が動いているときは再起動を繰り返さない。
EXIT_ALREADY_RUNNING = 3
EXIT_LOST = 2


class AlreadyRunning(RuntimeError):
    """すでに別の Bot が動いている。メッセージはそのままユーザーに表示できる。"""


# ---------------------------------------------------------------- 1. 同じパソコン内: ファイルロック

if sys.platform == "win32":
    import msvcrt

    def _lock(fh) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(fh) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock(fh) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fh) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


class ProcessLock:
    """先頭1バイトをロックし、2バイト目以降に PID を書く（Windows ではロック中の領域を他のプロセスが読めないため）。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._fh = None

    def acquire(self) -> bool:
        import os

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)
        fh = open(self.path, "r+b")
        if fh.seek(0, 2) == 0:
            fh.write(b"L")
            fh.flush()
        try:
            _lock(fh)
        except OSError:
            fh.close()
            return False
        fh.seek(1)
        fh.truncate(1)
        fh.write(str(os.getpid()).encode())
        fh.flush()
        self._fh = fh
        return True

    def holder_pid(self) -> int | None:
        """ロックしているプロセスの PID（分かる場合）。"""
        try:
            with open(self.path, "rb") as fh:
                fh.seek(1)
                return int(fh.read().decode() or 0) or None
        except (OSError, ValueError):
            return None

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            self._fh.seek(1)
            self._fh.truncate(1)
            self._fh.flush()
            _unlock(self._fh)
        except OSError:
            pass
        finally:
            self._fh.close()
            self._fh = None


# ---------------------------------------------------------------- 2. 別の場所: 期限つきの「使用中の印」


def decide(props: dict, me: str, now: float, ttl: float = TTL) -> str:
    """印の状態を判定する純関数。 "free"（空き・期限切れ）/ "mine"（自分の印）/ "taken"（他が使用中）。"""
    lid = props.get("lease_id")
    if not lid:
        return "free"
    if lid == me:
        return "mine"
    try:
        ts = float(props.get("lease_ts", "0"))
    except ValueError:
        return "free"
    return "free" if now - ts >= ttl else "taken"


def should_stop(last_ok: float | None, now: float, ttl: float = TTL) -> bool:
    """印を更新できない状態が続いたら止まる（更新できないまま動き続けると、他が引き継いで二重になりうるため）。"""
    return last_ok is not None and now - last_ok > ttl - 10


class Lease:
    def __init__(self, store, path: str | None = None, ident: str | None = None, clock=time.time, sleep=time.sleep):
        self.store = store
        self.path = path or notes_policy.lifeos(config.CHANNELS["ai"])
        self.id = ident or uuid.uuid4().hex[:12]
        self.host = socket.gethostname()[:40]
        self.clock, self.sleep = clock, sleep
        self.last_ok: float | None = None
        self.lost = False

    def _read(self) -> dict:
        return self.store.get_props(self.path)

    def _write(self) -> None:
        self.store.set_props(self.path, {"lease_id": self.id, "lease_host": self.host, "lease_ts": str(int(self.clock()))})

    def _describe(self, props: dict) -> str:
        try:
            age = int(self.clock() - float(props.get("lease_ts", "0")))
        except ValueError:
            age = -1
        where = props.get("lease_host") or "別の場所"
        return (f"すでに別の場所（{where}）で Bot が動いています（最後の更新: {age}秒前）。"
                f"そちらを停止してから、もう一度起動してください。"
                f"相手がクラッシュした場合は、約{int(TTL)}秒で自動的に解除されます。")

    def acquire(self, same_host_ok: bool = False) -> None:
        """same_host_ok: このパソコン/VM の ProcessLock を取得済みのときだけ True にする。
        同じホストの印が残っていれば、前回この場所で動いていた Bot が異常終了した跡なので、
        期限を待たずに引き継ぐ（systemd の自動再起動を、すぐに成功させるため）。"""
        props = self._read()
        st = decide(props, self.id, self.clock())
        if st == "taken" and same_host_ok and props.get("lease_host") == self.host:
            log.warning("同じホストの前回の印が残っていたので引き継ぎます（前回は異常終了した可能性があります）")
            st = "free"
        if st == "taken":
            raise AlreadyRunning(self._describe(props))
        self._write()
        self.sleep(SETTLE)  # ほぼ同時に起動した相手がいれば、後から書いた方が残るので、確かめる
        props = self._read()
        if props.get("lease_id") != self.id:
            raise AlreadyRunning(self._describe(props))
        self.last_ok = self.clock()

    def renew(self) -> bool:
        """印を更新する。他の Bot に引き継がれていたら False（自分は止まるべき）。通信エラーは例外。"""
        props = self._read()
        if decide(props, self.id, self.clock()) == "taken":
            self.lost = True
            return False
        self._write()
        self.last_ok = self.clock()
        return True

    def release(self) -> None:
        """自分の印だけを消す（他の Bot の印は消さない）。失敗しても終了処理は続ける。"""
        try:
            if self._read().get("lease_id") == self.id:
                self.store.set_props(self.path, {k: None for k in KEYS})
        except Exception:  # noqa: BLE001
            log.warning("使用中の印を消せませんでした（%s 秒後に自動で期限切れになります）", int(TTL))


def start_heartbeat(client, lease: Lease):
    """30秒ごとに印を更新する。引き継がれた、または更新できない状態が続いたら Bot を停止する。"""
    from discord.ext import tasks

    @tasks.loop(seconds=HEARTBEAT)
    async def beat():
        try:
            ok = await asyncio.to_thread(lease.renew)
        except Exception:  # noqa: BLE001
            log.warning("使用中の印を更新できませんでした", exc_info=True)
            if should_stop(lease.last_ok, lease.clock()):
                log.critical("印を更新できない状態が続いたため、二重起動を避けて停止します")
                lease.lost = True
                await client.close()
            return
        if not ok:
            log.critical("別の場所で Bot が起動したため、二重起動を避けて停止します")
            await client.close()

    beat.start()
    return beat
