"""Obsidian Vault へのアクセスポリシー（読み取りは Vault 全体で許可、書き込み系は許可リストのみ）。

書き込み・変更・新規作成・改名・削除を許可するのは次の2つだけ。それ以外は拒否する（拒否がデフォルト）。
  1. `06-Life-OS/` 配下のすべてのファイル・フォルダ
  2. `01🗃Task/01 Life-OS-Task.md`（タスク棚。内容の書き換えのみ。改名・削除は不可）

読み取り(read / list)は Vault 内であれば許可する。ただし Vault の外へ出るパス（.. や絶対パス）は拒否する。
手動管理している他のノートを Bot が誤って変更しないための最終防壁。NoteStore の全操作がここを通る。
判定ロジックを他の場所に複製しないこと。
"""
import logging
import re
import unicodedata

import config

LIFEOS_DIR = "06-Life-OS"
TASK_FILE = unicodedata.normalize("NFC", config.OBSIDIAN_TASK_FILE)
VAULT_ROOT = ""  # Vault のルート（list のみ許可）

READ_OPS = {"read", "list"}
WRITE_OPS = {"write", "rename", "delete"}
OPS = READ_OPS | WRITE_OPS
_TASK_FILE_WRITE_OPS = {"write"}  # タスク棚は内容の書き換えのみ

log = logging.getLogger("life-os.guard")
if not any(isinstance(h, logging.FileHandler) for h in log.handlers):
    _fh = logging.FileHandler(config.DATA_DIR / "guard.log", encoding="utf-8")
    _fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(_fh)
    log.setLevel(logging.INFO)


class AccessDenied(PermissionError):
    """ポリシーに反する操作。捕捉して処理を安全に中断すること。"""


_DRIVE_LETTER = re.compile(r"^[A-Za-z]:")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def normalize(path: str) -> str:
    """Vault ルートからの相対 POSIX パスに正規化する。不正な形式は AccessDenied。"""
    if not isinstance(path, str) or not path.strip():
        raise AccessDenied("空のパス")
    p = unicodedata.normalize("NFC", path)
    if "\\" in p or _CONTROL.search(p):
        raise AccessDenied("使用できない文字を含むパス")
    if p.startswith("/") or _DRIVE_LETTER.match(p):
        raise AccessDenied("絶対パスは使用できません")
    parts = p.split("/")
    if any(seg in ("", ".", "..") for seg in parts):
        raise AccessDenied("相対指定（.. や空の区切り）は使用できません")
    return "/".join(parts)


def _in_life_os(n: str) -> bool:
    return n == LIFEOS_DIR or n.startswith(LIFEOS_DIR + "/")


def _decide(path: str, op: str) -> tuple[str, str | None]:
    """(正規化パス, 拒否理由 or None)。"""
    if op not in OPS:
        raise ValueError(f"未知の操作: {op}")
    if path in (VAULT_ROOT, "."):
        return VAULT_ROOT, None if op == "list" else "Vault のルートは一覧のみ可能です"
    n = normalize(path)
    if op in READ_OPS:
        return n, None
    # ---- 書き込み系
    if n == TASK_FILE:
        return n, None if op in _TASK_FILE_WRITE_OPS else "タスク棚は内容の書き換えのみ可能です"
    if n == LIFEOS_DIR:
        return n, "06-Life-OS フォルダ自体は変更できません"
    if _in_life_os(n):
        return n, None
    return n, "書き込み許可リスト外です"


def check(path: str, op: str) -> str:
    """許可されていれば正規化済みパスを返す。許可外は AccessDenied（ERRORログも記録）。"""
    try:
        n, reason = _decide(path, op)
    except AccessDenied as e:
        log.error("DENY op=%s path=%r reason=%s", op, path, e)
        raise
    if reason:
        log.error("DENY op=%s path=%r reason=%s", op, path, reason)
        raise AccessDenied(f"{reason}: {op} {path!r}")
    return n


def is_allowed(path: str, op: str = "read") -> bool:
    """ログを残さずに判定だけ行う（解決後パスの再検証用）。"""
    try:
        return _decide(path, op)[1] is None
    except AccessDenied:
        return False


def lifeos(*parts: str) -> str:
    """`06-Life-OS/...` のパスを組み立てる。"""
    return "/".join((LIFEOS_DIR, *parts))
