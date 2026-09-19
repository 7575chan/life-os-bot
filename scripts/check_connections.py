"""接続確認: Discord / Claude API / Bot 用シート / 執筆記録シート。秘密の値は表示しない。

    .venv/Scripts/python scripts/check_connections.py
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")
if os.getenv("USE_TRUSTSTORE") == "1":
    import truststore

    truststore.inject_into_ssl()

import httpx  # noqa: E402

import config  # noqa: E402

failed = 0


def report(ok: bool, name: str, detail: str = ""):
    global failed
    failed += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} {name} {detail}")


def check_discord():
    print("[Discord]")
    h = {"Authorization": f"Bot {config.DISCORD_TOKEN}"}
    with httpx.Client(timeout=20, base_url="https://discord.com/api/v10", headers=h) as c:
        r = c.get("/users/@me")
        if r.status_code != 200:
            report(False, "BOT トークン", f"(HTTP {r.status_code}: トークンが無効です)")
            return
        report(True, "BOT トークン", f"(BOT 名: {r.json().get('username')})")
        app = c.get("/applications/@me")
        if app.status_code == 200:
            flags = app.json().get("flags", 0)
            has = bool(flags & (1 << 18)) or bool(flags & (1 << 19))  # GATEWAY_MESSAGE_CONTENT(_LIMITED)
            report(has, "メッセージ内容の読み取り権限 (Message Content Intent)",
                   "" if has else "(Developer Portal の Bot ページで有効にしてください)")
        guilds = c.get("/users/@me/guilds").json()
        report(bool(guilds), "サーバーへの参加", f"({len(guilds)} サーバー)")
        want = set(config.CHANNELS.values())
        for g in guilds:
            chans = c.get(f"/guilds/{g['id']}/channels")
            if chans.status_code != 200:
                report(False, "チャンネル一覧の取得", f"(HTTP {chans.status_code})")
                continue
            names = {ch["name"] for ch in chans.json() if ch["type"] == 0}
            missing = sorted(want - names)
            report(not missing, f"14チャンネルの存在（{g['name']}）",
                   "" if not missing else f"(見つからない: {', '.join(missing)})")


def check_claude():
    print("[Claude API]")
    try:
        from anthropic import Anthropic

        r = Anthropic(api_key=config.ANTHROPIC_API_KEY).messages.create(
            model=config.CLAUDE_MODEL, max_tokens=16, messages=[{"role": "user", "content": "OKとだけ返して"}])
        report(True, f"API 呼び出し（モデル {config.CLAUDE_MODEL}）", f"(応答: {r.content[0].text.strip()[:20]!r})")
    except Exception as e:  # noqa: BLE001
        report(False, "API 呼び出し", f"({type(e).__name__}: {str(e)[:160]})")


def check_sheets():
    print("[Google スプレッドシート]")
    import gspread

    gc = gspread.service_account(filename=config.CREDENTIALS_FILE)
    for label, key, editable in (("Bot 用シート", config.GOOGLE_SHEET_ID, True), ("執筆記録シート", config.WORDCOUNT_SHEET_ID, False)):
        try:
            book = gc.open_by_key(key)
            tabs = [w.title for w in book.worksheets()]
            report(True, f"{label} を開く", f"({len(tabs)} タブ)")
            if not editable:
                tab = config.WORDCOUNT_TAB
                report(tab in tabs, f"タブ「{tab}」の存在", "" if tab in tabs else f"(タブ一覧: {tabs[:6]})")
        except Exception as e:  # noqa: BLE001
            report(False, f"{label} を開く", f"({type(e).__name__}: {str(e)[:140]})")


def main() -> int:
    for fn in (check_discord, check_claude, check_sheets):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            report(False, fn.__name__, f"({type(e).__name__}: {str(e)[:140]})")
    print(f"\n{'すべて成功' if not failed else str(failed) + ' 件の失敗'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
