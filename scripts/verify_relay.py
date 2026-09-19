"""GAS 中継の実機検証（SPEC §2.5）。GAS をデプロイし、.env に GAS_RELAY_URL / GAS_RELAY_TOKEN を設定してから実行する。

    .venv/Scripts/python scripts/verify_relay.py

検証用ファイルは `06-Life-OS/14-ai/` に作り、最後にゴミ箱へ移す。トークンは表示しない。
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if os.getenv("USE_TRUSTSTORE") == "1" or Path(__file__).resolve().parent.parent.joinpath(".env").exists():
    try:  # 開発用パソコン向け: 証明書検証は無効にせず、OS の証明書ストアを使う
        from dotenv import load_dotenv

        load_dotenv(Path(__file__).resolve().parent.parent / ".env")
        if os.getenv("USE_TRUSTSTORE") == "1":
            import truststore

            truststore.inject_into_ssl()
    except ImportError:
        pass

import config  # noqa: E402
import gas_relay  # noqa: E402
import notes  # noqa: E402
from notes_policy import AccessDenied  # noqa: E402

BASE = "06-Life-OS/14-ai/_relay_verify"
results: list[tuple[str, bool, str]] = []


def step(name: str, fn):
    try:
        detail = fn()
        results.append((name, True, detail or ""))
        print(f"  OK   {name} {detail or ''}")
    except Exception as e:  # noqa: BLE001
        results.append((name, False, f"{type(e).__name__}: {str(e)[:150]}"))
        print(f"  FAIL {name} -> {type(e).__name__}: {str(e)[:150]}")


def main() -> int:
    if not (config.GAS_RELAY_URL and config.GAS_RELAY_TOKEN):
        print("GAS_RELAY_URL / GAS_RELAY_TOKEN が .env に設定されていません")
        return 2
    store = notes.get_store()
    relay = store._b.relay  # noqa: SLF001  (検証用: 生の中継を直接呼ぶ)
    a, b = f"{BASE}.md", f"{BASE}_renamed.md"
    print("GAS 中継の実機検証")

    def create():
        store.write(a, "# 検証\n作成 🗃")
        assert store.read(a) == "# 検証\n作成 🗃"

    def update_by_service_account():
        # 中継が作ったファイル(ユーザー所有)を、サービスアカウントが更新できること
        store.write(a, "# 検証\n更新 日本語")
        assert store.read(a) == "# 検証\n更新 日本語"

    def no_overwrite():
        res = relay.create_file(a, "上書きされてはいけない")
        assert res.get("status") == "exists", res
        assert store.read(a) == "# 検証\n更新 日本語"

    def bytes_file():
        store.write_bytes(f"{BASE}_bin.png", b"\x89PNG\r\n\x1a\n\x00\xff", "image/png")

    def rename():
        store.rename(a, b)
        assert store.read(a) is None and store.read(b) == "# 検証\n更新 日本語"

    def trash():
        store.delete(b)
        store.delete(f"{BASE}_bin.png")
        assert store.read(b) is None

    def denied(path):
        def run():
            res = relay._post({"op": "create_file", "path": path, "content": "x"})  # noqa: SLF001
            assert res.get("status") == "denied", res
            return f"(status={res['status']})"
        return run

    def bad_token():
        bad = gas_relay.GasRelay(config.GAS_RELAY_URL, "wrong-token-for-verification")
        res = bad._post({"op": "create_file", "path": f"{BASE}_x.md", "content": "x"})  # noqa: SLF001
        assert res.get("status") == "unauthorized", res
        return f"(status={res['status']})"

    def bot_side_guard():
        try:
            store.write("00inbox/_relay_verify.md", "x")
        except AccessDenied:
            return "(中継を呼ぶ前に拒否)"
        raise AssertionError("許可外に書けてしまった")

    step("1 新規作成（中継）→ 読み取り", create)
    step("2 既存ファイルの更新（サービスアカウント）", update_by_service_account)
    step("3 既存ファイルは上書きされない（exists）", no_overwrite)
    step("4 画像などバイナリの作成", bytes_file)
    step("5 改名", rename)
    step("6 ゴミ箱へ移動", trash)
    step("7 GAS 側の拒否: 許可外パス", denied("00inbox/_relay_verify.md"))
    step("8 GAS 側の拒否: .. によるすり抜け", denied("06-Life-OS/../00inbox/_relay_verify.md"))
    step("9 GAS 側の拒否: 06-Life-OS 直下（ルート）", denied("06-Life-OS"))
    step("10 GAS 側の拒否: タスク棚", denied("01🗃Task/01 Life-OS-Task.md"))
    step("11 不正なトークンは拒否", bad_token)
    step("12 Bot 側のガード", bot_side_guard)

    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} 成功")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
