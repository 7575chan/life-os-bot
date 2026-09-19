"""SQLite 状態ストア: Bot メッセージと処理文脈の対応、定時ジョブの二重実行防止、簡易KV。"""
import json
import sqlite3
import threading
import time

import config

_conn = sqlite3.connect(config.DATA_DIR / "state.db", check_same_thread=False)
_lock = threading.Lock()
_conn.executescript(
    """
    CREATE TABLE IF NOT EXISTS pending (
        message_id INTEGER PRIMARY KEY,
        channel_id INTEGER NOT NULL,
        kind TEXT NOT NULL,
        payload TEXT NOT NULL,
        created_at REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """
)

PENDING_TTL = 30 * 24 * 3600


def put_pending(message_id: int, channel_id: int, kind: str, payload: dict):
    with _lock:
        _conn.execute(
            "INSERT OR REPLACE INTO pending VALUES (?,?,?,?,?)",
            (message_id, channel_id, kind, json.dumps(payload, ensure_ascii=False), time.time()),
        )
        _conn.commit()


def _row_to_dict(row):
    return {"message_id": row[0], "channel_id": row[1], "kind": row[2], "payload": json.loads(row[3]), "created_at": row[4]}


def get_pending(message_id: int) -> dict | None:
    with _lock:
        row = _conn.execute("SELECT * FROM pending WHERE message_id=?", (message_id,)).fetchone()
    return _row_to_dict(row) if row else None


def latest_pending(channel_id: int, kind: str, max_age_hours: float = 24) -> dict | None:
    cutoff = time.time() - max_age_hours * 3600
    with _lock:
        row = _conn.execute(
            "SELECT * FROM pending WHERE channel_id=? AND kind=? AND created_at>=? ORDER BY created_at DESC LIMIT 1",
            (channel_id, kind, cutoff),
        ).fetchone()
    return _row_to_dict(row) if row else None


def delete_pending(message_id: int):
    with _lock:
        _conn.execute("DELETE FROM pending WHERE message_id=?", (message_id,))
        _conn.commit()


def purge_old():
    with _lock:
        _conn.execute("DELETE FROM pending WHERE created_at<?", (time.time() - PENDING_TTL,))
        _conn.commit()


def get_kv(key: str, default: str | None = None) -> str | None:
    with _lock:
        row = _conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_kv(key: str, value: str):
    with _lock:
        _conn.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (key, value))
        _conn.commit()


def mark_done(key: str) -> bool:
    """未実行なら記録して True、実行済みなら False（定時ジョブの二重投稿防止）。"""
    with _lock:
        row = _conn.execute("SELECT 1 FROM kv WHERE key=?", (key,)).fetchone()
        if row:
            return False
        _conn.execute("INSERT INTO kv VALUES (?,?)", (key, "1"))
        _conn.commit()
        return True
