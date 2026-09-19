import os
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

TZ = ZoneInfo("Asia/Tokyo")
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5")

CREDENTIALS_FILE = str(BASE_DIR / os.getenv("CREDENTIALS_FILE", "credentials.json"))

OBSIDIAN_BACKEND = os.getenv("OBSIDIAN_BACKEND", "drive")  # drive | local (local は開発用)
OBSIDIAN_TASK_FILE = os.getenv("OBSIDIAN_TASK_FILE", "01🗃Task/01 Life-OS-Task.md")

# local: Vault のパス (開発用)
OBSIDIAN_VAULT_DIR = Path(os.getenv("OBSIDIAN_VAULT_DIR", "./vault"))
if not OBSIDIAN_VAULT_DIR.is_absolute():
    OBSIDIAN_VAULT_DIR = BASE_DIR / OBSIDIAN_VAULT_DIR

# drive: 認証はサービスアカウント (CREDENTIALS_FILE) のみ。ID は Drive の URL から取得する
OBSIDIAN_VAULT_FOLDER_ID = os.getenv("OBSIDIAN_VAULT_FOLDER_ID", "")  # Vault ルート（閲覧者。読み取り・検索用）
OBSIDIAN_LIFEOS_FOLDER_ID = os.getenv("OBSIDIAN_LIFEOS_FOLDER_ID", "")  # `06-Life-OS`（編集者。書き込み用）
OBSIDIAN_TASKFILE_ID = os.getenv("OBSIDIAN_TASKFILE_ID", "")  # `01 Life-OS-Task.md`（編集者）

# GAS 中継: 新規ファイル作成・フォルダ作成・改名・ゴミ箱移動をユーザー権限で行う (SPEC §2.5)
GAS_RELAY_URL = os.getenv("GAS_RELAY_URL", "")
GAS_RELAY_TOKEN = os.getenv("GAS_RELAY_TOKEN", "")  # 秘密情報。ログに出さない

WORDCOUNT_SHEET_ID = os.getenv("WORDCOUNT_SHEET_ID", "")
WORDCOUNT_TAB = os.getenv("WORDCOUNT_TAB", "シート1")

WEATHER_LAT = float(os.getenv("WEATHER_LAT", "35.68"))
WEATHER_LON = float(os.getenv("WEATHER_LON", "139.76"))


def _parse_time(value: str) -> time:
    h, m = value.split(":")
    return time(int(h), int(m), tzinfo=TZ)


MORNING_TIME = _parse_time(os.getenv("MORNING_TIME", "08:00"))
EVENING_TIME = _parse_time(os.getenv("EVENING_TIME", "21:00"))
REPORT_TIME = _parse_time(os.getenv("REPORT_TIME", "20:00"))

# キー: 内部名, 値: Discordチャンネル名
CHANNELS = {
    "today": os.getenv("CHANNEL_TODAY", "01-today-task"),
    "health": os.getenv("CHANNEL_HEALTH", "02-health"),
    "lookback": os.getenv("CHANNEL_LOOKBACK", "03-looking-back"),
    "report": os.getenv("CHANNEL_REPORT", "04-report"),
    "private": os.getenv("CHANNEL_PRIVATE", "05-private"),
    "household": os.getenv("CHANNEL_HOUSEHOLD", "06-household-accounts"),
    "ledger": os.getenv("CHANNEL_LEDGER", "07-ledger"),
    "scrap": os.getenv("CHANNEL_SCRAP", "08-scrap"),
    "idea": os.getenv("CHANNEL_IDEA", "09-idea"),
    "novel": os.getenv("CHANNEL_NOVEL", "10-project-novel"),
    "trpg": os.getenv("CHANNEL_TRPG", "11-project-trpg"),
    "others": os.getenv("CHANNEL_OTHERS", "12-project-others"),
    "ceo": os.getenv("CHANNEL_CEO", "13-im-the-ceo"),
    "ai": os.getenv("CHANNEL_AI", "14-ai"),
}

# プロジェクト部屋: 内部名 -> ノートのファイル名プレフィックス
PROJECT_PREFIX = {"novel": "Novel", "trpg": "TRPG", "others": "Project"}
