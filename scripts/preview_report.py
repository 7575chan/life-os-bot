"""週次・月次レポートの試し作り。Discord には投稿せず、ノートにもシートにも保存しない（読み取りと、記事を選ぶ AI の呼び出しだけ。
取扱マニュアルの更新はしないので、その1行は出ない）。

    PYTHONUTF8=1 PYTHONIOENCODING=utf-8 .venv/Scripts/python scripts/preview_report.py weekly [YYYY-MM-DD]
    PYTHONUTF8=1 PYTHONIOENCODING=utf-8 .venv/Scripts/python scripts/preview_report.py monthly [YYYY-MM-DD]

日付は、レポートを発行する日（週次は日曜、月次は月末日）。省略すると今日。
"""
import asyncio
import os
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")
if os.getenv("USE_TRUSTSTORE") == "1":
    import truststore

    truststore.inject_into_ssl()

import report  # noqa: E402
import sheets  # noqa: E402
import util  # noqa: E402


async def main(kind: str, day: date) -> None:
    await asyncio.to_thread(sheets.ensure_schema)
    rep = await report.generate(kind, day, update_manual=False)  # 取扱マニュアル（Health-Manual.md）も更新しない
    print(rep.text)
    print("\n" + "-" * 40 + f"\n（保存する場合のファイル名: 06-Life-OS/04-report/{rep.period.filename}.md）")


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in ("weekly", "monthly"):
        raise SystemExit(__doc__)
    day = util.parse_date_any(sys.argv[2]) if len(sys.argv) > 2 else util.today()
    if day is None:
        raise SystemExit("日付は YYYY-MM-DD で指定してください")
    asyncio.run(main(sys.argv[1], day))
