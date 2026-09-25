"""許可領域 (`06-Life-OS/`) 内のノート配置。パスはすべて notes_policy.lifeos() 経由で組み立てる。"""
import config
from notes_policy import TASK_FILE, lifeos  # noqa: F401  (TASK_FILE は再エクスポート)

_C = config.CHANNELS


def room(key: str) -> str:
    """チャンネル内部名 -> 部屋フォルダ (`06-Life-OS/<チャンネル名>`)。"""
    return lifeos(_C[key])


def health_manual() -> str:
    return lifeos(_C["health"], "Health-Manual.md")


def diary(day: str) -> str:
    return lifeos(_C["lookback"], f"{day}.md")


def report(name: str) -> str:
    return lifeos(_C["report"], f"{name}.md")


def inbox() -> str:
    return lifeos(_C["private"], "Inbox.md")


def private_attachment(name: str) -> str:
    return lifeos(_C["private"], "attachments", name)


def scrap(name: str) -> str:
    return lifeos(_C["scrap"], f"{name}.md")


def scrap_attachment(name: str) -> str:
    return lifeos(_C["scrap"], "attachments", name)


def ideas() -> str:
    return lifeos(_C["idea"], "Ideas.md")


def idea_attachment(name: str) -> str:
    return lifeos(_C["idea"], "attachments", name)


def project(room_key: str, filename: str) -> str:
    return lifeos(_C[room_key], filename)


def project_attachment(room_key: str, name: str) -> str:
    return lifeos(_C[room_key], "attachments", name)


def ceo_directives() -> str:
    return lifeos(_C["ceo"], "CEO-Directives.md")
